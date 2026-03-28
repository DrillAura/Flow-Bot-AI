import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from daytrading_bot.config import BotConfig, ThreeCommasConfig
from daytrading_bot.live import LiveScanReport
from daytrading_bot.models import Candle
from daytrading_bot.research import WalkForwardOptimizationReport, WalkForwardReport
from daytrading_bot.storage import history_csv_path, write_csv_candles
from daytrading_bot.workflows import (
    CaptureUntilReadyReport,
    HistoryStatusReport,
    PaperForwardLaunchReport,
    SupervisorEnsureReport,
    PaperForwardGateReport,
    PreparePaperForwardReport,
    run_ensure_supervisor,
    run_monitor_supervisor,
    run_history_status,
    run_paper_forward_gate,
    run_paper_forward_supervisor,
    run_prepare_paper_forward,
    run_stop_runtime,
    run_supervisor_watchdog,
    run_sync_history_until_ready,
)
from tests.helpers import build_default_universe_contexts


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot_config = BotConfig()
        self.exec_config = ThreeCommasConfig(mode="paper")

    def test_history_status_reports_insufficient_short_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = Path(tempdir)
            self._seed_short_histories(data_dir)
            report = run_history_status(data_dir, self.bot_config, train_days=10, test_days=3)

        self.assertFalse(report.sufficient_history)
        self.assertIn("XBTEUR", report.pair_status)
        self.assertGreater(report.pair_status["XBTEUR"].candles_1m, 0)

    def test_paper_forward_gate_returns_ready_when_e2e_and_walk_forward_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = Path(tempdir) / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            self._seed_long_histories(data_dir)
            telemetry_path = Path(tempdir) / "forward.jsonl"
            telemetry_path.write_text(
                "\n".join(json.dumps(event) for event in self._forward_events()) + "\n",
                encoding="utf-8-sig",
            )

            report = run_paper_forward_gate(
                data_dir,
                self.bot_config,
                self.exec_config,
                telemetry_path=telemetry_path,
                train_days=1,
                test_days=1,
                skip_e2e_unit=True,
                e2e_runner=lambda skip: {"results": [{"name": "mock", "ok": True}]},
                walk_forward_runner=lambda *args, **kwargs: WalkForwardReport(
                    folds=[],
                    aggregate_oos_profit_factor=1.4,
                    aggregate_oos_expectancy_eur=0.3,
                    aggregate_oos_expectancy_r=0.2,
                    aggregate_oos_max_drawdown_pct=0.01,
                    aggregate_oos_total_trades=5,
                    best_variant_frequency={"variant-a": 1},
                    insufficient_history=False,
                    objective="hybrid",
                    setup_scope="both",
                ),
            )

        self.assertTrue(report.e2e_ok)
        self.assertTrue(report.history_status.sufficient_history)
        self.assertFalse(report.walk_forward_report.insufficient_history)
        self.assertTrue(report.ready_to_start_paper_forward)
        self.assertEqual(report.forward_report.closed_trades, 2)

    def test_capture_until_ready_stops_after_becoming_ready(self) -> None:
        statuses = [
            self._mock_history_status(False, 1.2),
            self._mock_history_status(False, 1.3),
            self._mock_history_status(True, 13.1),
        ]
        sync_calls: list[int] = []
        sleep_calls: list[int] = []

        def status_runner(*args, **kwargs):
            return statuses.pop(0)

        def sync_runner(*args, **kwargs):
            sync_calls.append(1)
            return [{"cycle": len(sync_calls)}]

        report = run_sync_history_until_ready(
            Path("unused"),
            self.bot_config,
            train_days=10,
            test_days=3,
            poll_seconds=15,
            max_cycles=5,
            status_runner=status_runner,
            sync_runner=sync_runner,
            sleep_fn=sleep_calls.append,
        )

        self.assertTrue(report.ready)
        self.assertEqual(report.stopped_reason, "ready")
        self.assertEqual(report.cycles_run, 2)
        self.assertEqual(len(report.cycle_reports), 2)
        self.assertEqual(sync_calls, [1, 1])
        self.assertEqual(sleep_calls, [15])

    def test_prepare_paper_forward_short_circuits_until_history_is_ready(self) -> None:
        capture_report = run_sync_history_until_ready(
            Path("unused"),
            self.bot_config,
            train_days=10,
            test_days=3,
            max_cycles=0,
            status_runner=lambda *args, **kwargs: self._mock_history_status(False, 1.1),
            sync_runner=lambda *args, **kwargs: [],
        )

        report = run_prepare_paper_forward(
            Path("unused"),
            self.bot_config,
            self.exec_config,
            telemetry_path=Path("unused.jsonl"),
            train_days=10,
            test_days=3,
            max_cycles=0,
            capture_runner=lambda *args, **kwargs: capture_report,
        )

        self.assertFalse(report.ready_for_paper_forward)
        self.assertIsNone(report.walk_forward_optimization)
        self.assertIsNone(report.paper_forward_gate)

    def test_prepare_paper_forward_runs_optimization_and_gate_after_ready_capture(self) -> None:
        ready_status = self._mock_history_status(True, 13.5)
        capture_report = run_sync_history_until_ready(
            Path("unused"),
            self.bot_config,
            train_days=10,
            test_days=3,
            max_cycles=0,
            status_runner=lambda *args, **kwargs: ready_status,
            sync_runner=lambda *args, **kwargs: [],
        )
        optimization_calls: list[int] = []
        gate_calls: list[int] = []

        report = run_prepare_paper_forward(
            Path("unused"),
            self.bot_config,
            self.exec_config,
            telemetry_path=Path("unused.jsonl"),
            train_days=10,
            test_days=3,
            capture_runner=lambda *args, **kwargs: capture_report,
            optimization_runner=lambda *args, **kwargs: self._mock_optimization_report(optimization_calls),
            gate_runner=lambda *args, **kwargs: self._mock_gate_report(gate_calls),
        )

        self.assertTrue(report.ready_for_paper_forward)
        self.assertIsNotNone(report.walk_forward_optimization)
        self.assertIsNotNone(report.paper_forward_gate)
        self.assertEqual(optimization_calls, [1])
        self.assertEqual(gate_calls, [1])

    def test_supervisor_launches_paper_forward_when_gate_is_green(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "state.json"
            launch_calls: list[int] = []
            report = run_paper_forward_supervisor(
                Path(tempdir),
                self.bot_config,
                self.exec_config,
                telemetry_path=Path(tempdir) / "telemetry.jsonl",
                max_supervisor_cycles=1,
                state_path=state_path,
                prepare_runner=lambda *args, **kwargs: self._mock_ready_prepare_report(),
                launcher=lambda **kwargs: self._mock_launch_report(launch_calls),
            )
            self.assertTrue(state_path.exists())

        self.assertEqual(report.status, "paper_forward_started")
        self.assertEqual(launch_calls, [1])

    def test_supervisor_writes_daily_summary_and_dashboard_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "state.json"
            report = run_paper_forward_supervisor(
                Path(tempdir),
                self.bot_config,
                self.exec_config,
                telemetry_path=Path(tempdir) / "telemetry.jsonl",
                max_supervisor_cycles=1,
                state_path=state_path,
                prepare_runner=lambda *args, **kwargs: self._mock_not_ready_prepare_report(),
                launcher=lambda **kwargs: self._mock_launch_report([]),
            )
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("daily_summary", state_payload)
            self.assertTrue(Path(report.daily_summary_json_path).exists())
            self.assertTrue(Path(report.daily_summary_markdown_path).exists())
            self.assertTrue(Path(report.dashboard_path).exists())
            self.assertEqual(state_payload["daily_summary"]["supervisor_status"], "waiting_for_history")

    def test_supervisor_waits_when_history_is_not_ready(self) -> None:
        sleep_calls: list[int] = []
        report = run_paper_forward_supervisor(
            Path("unused"),
            self.bot_config,
            self.exec_config,
            telemetry_path=Path("unused.jsonl"),
            max_supervisor_cycles=1,
            supervisor_poll_seconds=30,
            prepare_runner=lambda *args, **kwargs: self._mock_not_ready_prepare_report(),
            launcher=lambda **kwargs: self._mock_launch_report([]),
            sleep_fn=sleep_calls.append,
        )

        self.assertEqual(report.status, "waiting_for_history")
        self.assertEqual(report.supervisor_cycles, 1)

    def test_supervisor_runs_research_scan_when_enabled_and_session_open(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir, patch("daytrading_bot.workflows.is_trade_window", return_value=True):
            state_path = Path(tempdir) / "state.json"
            report = run_paper_forward_supervisor(
                Path(tempdir),
                self.bot_config,
                self.exec_config,
                telemetry_path=Path(tempdir) / "telemetry.jsonl",
                max_supervisor_cycles=1,
                state_path=state_path,
                enable_research_scans=True,
                research_scan_duration_seconds=5,
                research_scan_max_messages=12,
                research_scan_min_interval_seconds=0,
                prepare_runner=lambda *args, **kwargs: self._mock_not_ready_prepare_report(),
                launcher=lambda **kwargs: self._mock_launch_report([]),
                research_scan_runner=lambda *args, **kwargs: LiveScanReport(
                    status="ok",
                    error="",
                    messages_seen=12,
                    contexts_built=3,
                    events_emitted=2,
                    reconnects=0,
                    ending_equity=100.0,
                    win_rate=0.5,
                    profit_factor=1.2,
                    max_drawdown_pct=0.01,
                ),
            )
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertIsNotNone(report.research_scan)
        self.assertTrue(report.research_scan.ran)
        self.assertEqual(report.research_scan.status, "ok")
        self.assertEqual(report.daily_summary.research_scan_status, "ok")
        self.assertIn("research_scan", state_payload)
        self.assertEqual(state_payload["research_scan"]["live_scan_report"]["messages_seen"], 12)

    def test_monitor_supervisor_reports_progress_and_process_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "state.json"
            stop_path = Path(tempdir) / "supervisor.stop"
            state_path.write_text(
                json.dumps(
                    {
                        "status": "waiting_for_history",
                        "stopped_reason": "max_cycles_reached",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "supervisor_pid": os.getpid(),
                        "supervisor_stop_path": str(stop_path),
                        "paper_forward_pid": None,
                        "paper_forward_stop_path": str(Path(tempdir) / "paper_forward.stop"),
                        "history_progress": {
                            "required_days": 13,
                            "available_days": 1.5,
                            "remaining_days": 11.5,
                            "progress_pct": 11.54,
                            "cycles_observed": 3,
                            "avg_growth_days_per_cycle": 0.2,
                            "avg_growth_days_per_hour": 0.1,
                            "estimated_cycles_to_ready": 57.5,
                            "estimated_seconds_to_ready": 414000.0,
                            "estimated_ready_at": datetime.now(timezone.utc).isoformat(),
                        },
                        "daily_summary": {
                            "date": "2026-03-27",
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "supervisor_status": "waiting_for_history",
                            "progress_pct": 11.54,
                            "available_days": 1.5,
                            "required_days": 13,
                            "eta": datetime.now(timezone.utc).isoformat(),
                            "last_errors": [],
                            "gate_status": "waiting_for_history",
                            "gate_ready": False,
                            "gate_blockers": ["local_oos_history_not_ready"],
                            "paper_forward_status": "idle",
                            "research_scan_status": "ok",
                            "research_scan_last_run_at": datetime.now(timezone.utc).isoformat(),
                            "research_scan_last_error": None,
                            "strategy_lab_status": "active",
                            "strategy_lab_champion": "mean_reversion_vwap",
                            "strategy_lab_last_promotion_reason": "paper_promoted_to_mean_reversion_vwap",
                        },
                        "research_scan": {
                            "enabled": True,
                            "session_open": True,
                            "should_run": True,
                            "ran": True,
                            "status": "ok",
                            "stopped_reason": "ok",
                            "requested_duration_seconds": 5,
                            "requested_max_messages": 12,
                            "requested_available_eur": 100.0,
                            "started_at": datetime.now(timezone.utc).isoformat(),
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                            "live_scan_report": {"messages_seen": 12},
                        },
                        "strategy_lab": {
                            "current_paper_strategy_id": "mean_reversion_vwap",
                            "promotion_reason": "paper_promoted_to_mean_reversion_vwap",
                            "strategies": [{"strategy_id": "mean_reversion_vwap"}],
                        },
                        "last_prepare_report": {"ready_for_paper_forward": False},
                    }
                ),
                encoding="utf-8",
            )

            report = run_monitor_supervisor(state_path)

        self.assertTrue(report.state_exists)
        self.assertEqual(report.status, "waiting_for_history")
        self.assertIsNotNone(report.history_progress)
        self.assertTrue(report.supervisor.alive)
        self.assertFalse(report.supervisor.stop_requested)
        self.assertIsNotNone(report.research_scan)
        self.assertEqual(report.research_scan.status, "ok")
        self.assertEqual(report.daily_summary.strategy_lab_status, "active")
        self.assertEqual(report.daily_summary.strategy_lab_champion, "mean_reversion_vwap")
        self.assertIsNotNone(report.strategy_lab)
        self.assertEqual(report.strategy_lab["current_paper_strategy_id"], "mean_reversion_vwap")

    def test_stop_runtime_creates_stop_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            supervisor_stop = Path(tempdir) / "supervisor.stop"
            paper_stop = Path(tempdir) / "paper_forward.stop"
            state_path = Path(tempdir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "status": "waiting_for_history",
                        "stopped_reason": "max_cycles_reached",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "supervisor_pid": None,
                        "supervisor_stop_path": str(supervisor_stop),
                        "paper_forward_pid": None,
                        "paper_forward_stop_path": str(paper_stop),
                        "history_progress": None,
                        "last_prepare_report": {"ready_for_paper_forward": False},
                    }
                ),
                encoding="utf-8",
            )

            report = run_stop_runtime(state_path, scope="all", grace_seconds=0, force=False)

            self.assertTrue(supervisor_stop.exists())
            self.assertTrue(paper_stop.exists())
        self.assertTrue(report.requested)
        self.assertTrue(report.supervisor.stop_requested)
        self.assertTrue(report.paper_forward.stop_requested)

    def test_ensure_supervisor_returns_running_when_process_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "status": "waiting_for_history",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "supervisor_pid": os.getpid(),
                        "supervisor_stop_path": str(Path(tempdir) / "supervisor.stop"),
                        "paper_forward_pid": None,
                        "paper_forward_stop_path": str(Path(tempdir) / "paper_forward.stop"),
                    }
                ),
                encoding="utf-8",
            )

            report = run_ensure_supervisor(
                Path(tempdir),
                self.bot_config,
                self.exec_config,
                telemetry_path=Path(tempdir) / "telemetry.jsonl",
                state_path=state_path,
            )

        self.assertFalse(report.launched)
        self.assertTrue(report.supervisor_running)
        self.assertEqual(report.reason, "already_running")

    def test_ensure_supervisor_starts_when_state_is_missing(self) -> None:
        launch_calls: list[int] = []

        def launcher(**kwargs):
            launch_calls.append(1)
            return SupervisorEnsureReport(
                state_path=str(kwargs["state_path"]),
                launched=True,
                supervisor_running=True,
                reason="started",
                pid=999,
                command=["python"],
                stdout_path="stdout.log",
                stderr_path="stderr.log",
            )

        report = run_ensure_supervisor(
            Path("unused"),
            self.bot_config,
            self.exec_config,
            telemetry_path=Path("unused.jsonl"),
            state_path=Path("missing.json"),
            launcher=launcher,
        )

        self.assertTrue(report.launched)
        self.assertEqual(launch_calls, [1])

    def test_watchdog_calls_ensure_and_tracks_launches(self) -> None:
        ensure_results = [
            SupervisorEnsureReport(
                state_path="state.json",
                launched=True,
                supervisor_running=True,
                reason="started",
                pid=1001,
                command=["python"],
                stdout_path="stdout.log",
                stderr_path="stderr.log",
            ),
            SupervisorEnsureReport(
                state_path="state.json",
                launched=False,
                supervisor_running=True,
                reason="already_running",
                pid=1001,
                command=[],
                stdout_path="stdout.log",
                stderr_path="stderr.log",
            ),
        ]
        sleep_calls: list[int] = []

        def ensure_runner(*args, **kwargs):
            return ensure_results.pop(0)

        report = run_supervisor_watchdog(
            Path("unused"),
            self.bot_config,
            self.exec_config,
            telemetry_path=Path("unused.jsonl"),
            state_path=Path("state.json"),
            watchdog_poll_seconds=5,
            max_cycles=2,
            sleep_fn=sleep_calls.append,
            ensure_runner=ensure_runner,
        )

        self.assertEqual(report.cycles, 2)
        self.assertEqual(report.launched_count, 1)
        self.assertEqual(report.last_ensure.reason, "already_running")
        self.assertEqual(sleep_calls, [5])

    def test_capture_until_ready_tolerates_transient_sync_errors(self) -> None:
        statuses = [
            self._mock_history_status(False, 1.2),
            self._mock_history_status(True, 13.2),
        ]
        sync_calls = {"count": 0}

        def status_runner(*args, **kwargs):
            return statuses.pop(0)

        def sync_runner(*args, **kwargs):
            sync_calls["count"] += 1
            if sync_calls["count"] == 1:
                raise RuntimeError("temporary ssl timeout")
            return [{"cycle": sync_calls["count"]}]

        report = run_sync_history_until_ready(
            Path("unused"),
            self.bot_config,
            train_days=10,
            test_days=3,
            poll_seconds=0,
            max_cycles=3,
            max_consecutive_errors=2,
            status_runner=status_runner,
            sync_runner=sync_runner,
        )

        self.assertTrue(report.ready)
        self.assertEqual(report.error_count, 1)
        self.assertEqual(report.cycles_run, 2)
        self.assertEqual(report.cycle_reports[0].error, "temporary ssl timeout")

    def _seed_short_histories(self, data_dir: Path) -> None:
        contexts = build_default_universe_contexts(self.bot_config)
        for symbol, context in contexts.items():
            write_csv_candles(history_csv_path(data_dir, symbol, 1), list(context.candles_1m))
            write_csv_candles(history_csv_path(data_dir, symbol, 15), list(context.candles_15m))

    def _seed_long_histories(self, data_dir: Path) -> None:
        contexts = build_default_universe_contexts(self.bot_config)
        for symbol, context in contexts.items():
            prefix_15m = self._make_bullish_prefix(120, context.candles_15m[0].ts - timedelta(hours=30))
            prefix_1m = self._expand_15m_to_1m(prefix_15m)
            tail_shift = prefix_15m[-1].ts + timedelta(minutes=15) - context.candles_15m[0].ts
            tail_15m = [self._shift_candle(candle, tail_shift) for candle in context.candles_15m]
            tail_1m = [self._shift_candle(candle, tail_shift) for candle in context.candles_1m]
            write_csv_candles(history_csv_path(data_dir, symbol, 1), prefix_1m + tail_1m)
            write_csv_candles(history_csv_path(data_dir, symbol, 15), prefix_15m + tail_15m)

    @staticmethod
    def _forward_events() -> list[dict]:
        return [
            {
                "ts": "2026-03-23T08:30:00Z",
                "event_type": "entry_sent",
                "payload": {"intent": {"pair": "XBTEUR"}, "response": {"ok": True, "dry_run": True}},
            },
            {
                "ts": "2026-03-23T09:00:00Z",
                "event_type": "exit_sent",
                "payload": {"pair": "XBTEUR", "reason": "time_stop", "pnl_eur": 4.0, "response": {"ok": True, "dry_run": True}},
            },
            {
                "ts": "2026-03-23T10:00:00Z",
                "event_type": "entry_sent",
                "payload": {"intent": {"pair": "ETHEUR"}, "response": {"ok": True, "dry_run": True}},
            },
            {
                "ts": "2026-03-23T10:45:00Z",
                "event_type": "exit_sent",
                "payload": {"pair": "ETHEUR", "reason": "protective_stop", "pnl_eur": -1.0, "response": {"ok": True, "dry_run": True}},
            },
        ]

    @staticmethod
    def _make_bullish_prefix(count: int, start) -> list[Candle]:
        candles: list[Candle] = []
        price = 80.0
        for index in range(count):
            price += 0.3
            ts = start + timedelta(minutes=15 * index)
            candles.append(Candle(ts=ts, open=price - 0.25, high=price + 0.45, low=price - 0.30, close=price, volume=120.0))
        return candles

    @staticmethod
    def _expand_15m_to_1m(candles_15m: list[Candle]) -> list[Candle]:
        candles_1m: list[Candle] = []
        for candle in candles_15m:
            closes = [candle.open + ((candle.close - candle.open) * fraction) for fraction in (1 / 15, 2 / 15, 3 / 15, 4 / 15, 5 / 15, 6 / 15, 7 / 15, 8 / 15, 9 / 15, 10 / 15, 11 / 15, 12 / 15, 13 / 15, 14 / 15, 1.0)]
            for index, close_price in enumerate(closes):
                open_value = candle.open if index == 0 else candles_1m[-1].close
                high = max(open_value, close_price)
                low = min(open_value, close_price)
                if index == 7:
                    high = max(high, candle.high)
                    low = min(low, candle.low)
                candles_1m.append(Candle(ts=candle.ts + timedelta(minutes=index), open=open_value, high=high, low=low, close=close_price, volume=candle.volume / 15.0))
        return candles_1m

    @staticmethod
    def _expand_5m_to_1m(candles_5m: list[Candle]) -> list[Candle]:
        candles_1m: list[Candle] = []
        for candle in candles_5m:
            closes = [candle.open + ((candle.close - candle.open) * fraction) for fraction in (0.2, 0.4, 0.6, 0.8, 1.0)]
            for index, close_price in enumerate(closes):
                open_value = candle.open if index == 0 else candles_1m[-1].close
                high = max(open_value, close_price)
                low = min(open_value, close_price)
                if index == 2:
                    high = max(high, candle.high)
                    low = min(low, candle.low)
                candles_1m.append(Candle(ts=candle.ts + timedelta(minutes=index), open=open_value, high=high, low=low, close=close_price, volume=candle.volume / 5.0))
        return candles_1m

    @staticmethod
    def _shift_candle(candle: Candle, delta: timedelta) -> Candle:
        return Candle(
            ts=candle.ts + delta,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
        )

    def _mock_history_status(self, ready: bool, available_days: float):
        return HistoryStatusReport(
            data_dir="unused",
            train_days=10,
            test_days=3,
            required_days=13,
            available_days=available_days,
            sufficient_history=ready,
            pair_status={},
        )

    @staticmethod
    def _mock_optimization_report(calls: list[int]) -> WalkForwardOptimizationReport:
        calls.append(1)
        return WalkForwardOptimizationReport(
            variants_tested=8,
            top_results=[],
            zero_trade_variants=0,
            eligible_variants=4,
            insufficient_history=False,
            objective="hybrid",
            setup_scope="both",
            train_days=10,
            test_days=3,
            step_days=None,
        )

    def _mock_gate_report(self, calls: list[int]):
        calls.append(1)
        return PaperForwardGateReport(
            e2e_ok=True,
            e2e_results=[{"name": "mock", "ok": True}],
            history_status=HistoryStatusReport(
                data_dir="unused",
                train_days=10,
                test_days=3,
                required_days=13,
                available_days=13.5,
                sufficient_history=True,
                pair_status={},
            ),
            walk_forward_report=WalkForwardReport(
                folds=[],
                aggregate_oos_profit_factor=1.5,
                aggregate_oos_expectancy_eur=0.2,
                aggregate_oos_expectancy_r=0.1,
                aggregate_oos_max_drawdown_pct=0.01,
                aggregate_oos_total_trades=5,
                best_variant_frequency={"variant-a": 1},
                insufficient_history=False,
                objective="hybrid",
                setup_scope="both",
            ),
            forward_report=self._mock_forward_report(),
            ready_to_start_paper_forward=True,
        )

    @staticmethod
    def _mock_forward_report():
        from daytrading_bot.reporting import ForwardTestReport, GoLiveGate

        return ForwardTestReport(
            source_exists=True,
            events_loaded=0,
            closed_trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            profit_factor=0.0,
            gross_profit_eur=0.0,
            gross_loss_eur=0.0,
            net_pnl_eur=0.0,
            ending_equity=100.0,
            max_drawdown_pct=0.0,
            overnight_positions=0,
            average_hold_minutes=0.0,
            trade_days=0,
            forward_days=0,
            unclosed_entries=0,
            orphan_exit_events=0,
            rejection_counts={},
            exit_reason_counts={},
            pair_breakdown={},
            gates={
                "win_rate": GoLiveGate(name="win_rate", passed=False, actual=0.0, threshold=">= 0.55"),
            },
            go_live_ready=False,
        )

    def _mock_ready_prepare_report(self) -> object:
        ready_status = self._mock_history_status(True, 13.5)
        return PreparePaperForwardReport(
            capture_report=CaptureUntilReadyReport(
                data_dir="unused",
                train_days=10,
                test_days=3,
                poll_seconds=0,
                max_cycles=1,
                ready=True,
                stopped_reason="already_ready",
                cycles_run=0,
                error_count=0,
                initial_history_status=ready_status,
                final_history_status=ready_status,
                cycle_reports=[],
            ),
            walk_forward_optimization=self._mock_optimization_report([]),
            paper_forward_gate=self._mock_gate_report([]),
            ready_for_paper_forward=True,
        )

    def _mock_not_ready_prepare_report(self) -> object:
        not_ready = self._mock_history_status(False, 1.4)
        return PreparePaperForwardReport(
            capture_report=CaptureUntilReadyReport(
                data_dir="unused",
                train_days=10,
                test_days=3,
                poll_seconds=0,
                max_cycles=1,
                ready=False,
                stopped_reason="max_cycles_reached",
                cycles_run=1,
                error_count=0,
                initial_history_status=not_ready,
                final_history_status=not_ready,
                cycle_reports=[],
            ),
            walk_forward_optimization=None,
            paper_forward_gate=None,
            ready_for_paper_forward=False,
        )

    @staticmethod
    def _mock_launch_report(calls: list[int]) -> PaperForwardLaunchReport:
        calls.append(1)
        return PaperForwardLaunchReport(
            started=True,
            pid=12345,
            command=["python", "-m", "daytrading_bot.cli", "live-scan"],
            stdout_path="stdout.log",
            stderr_path="stderr.log",
            reason="started",
        )


if __name__ == "__main__":
    unittest.main()
