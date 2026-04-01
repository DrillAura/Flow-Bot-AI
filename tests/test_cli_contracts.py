import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daytrading_bot.indicators import atr, last_value
from daytrading_bot.models import Candle, MarketContext, OrderBookSnapshot
from daytrading_bot.config import BotConfig
from daytrading_bot.storage import history_csv_path, write_csv_candles
from tests.helpers import build_context, build_default_universe_contexts, build_recovery_context


ROOT = Path(__file__).resolve().parents[1]


class CliContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot_config = BotConfig()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workdir = Path(self.tempdir.name)
        self.positive_dir = self.workdir / "positive"
        self.negative_dir = self.workdir / "negative"
        self.positive_dir.mkdir(parents=True, exist_ok=True)
        self.negative_dir.mkdir(parents=True, exist_ok=True)
        self._seed_positive_fixtures(self.positive_dir)
        self._seed_negative_fixtures(self.negative_dir)
        self.forward_telemetry = self.workdir / "forward.jsonl"
        self.forward_telemetry.write_text(
            "\n".join(json.dumps(event) for event in self._forward_events()) + "\n",
            encoding="utf-8-sig",
        )

    def test_backtest_calibrate_and_debug_contracts(self) -> None:
        backtest_positive = self._run_cli_json("backtest", "--data-dir", str(self.positive_dir))
        self.assertEqual(self._sum_exit_distribution(backtest_positive["exit_distribution"]), backtest_positive["total_trades"])
        self.assertEqual(self._sum_setup_totals(backtest_positive["setup_performance"]), backtest_positive["total_trades"])

        backtest_negative = self._run_cli_json("backtest", "--data-dir", str(self.negative_dir))
        self.assertEqual(backtest_negative["total_trades"], 0)
        self.assertEqual(backtest_negative["trade_logs"], [])

        calibrate = self._run_cli_json("calibrate", "--data-dir", str(self.positive_dir), "--top", "3", "--profile", "fast")
        self.assertEqual(calibrate["variants_tested"], 8)
        self.assertIn("zero_trade_variants", calibrate)
        self.assertIn("eligible_variants", calibrate)
        scores = [row["score"] for row in calibrate["top_results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all("expectancy_eur" in row and "expectancy_r" in row for row in calibrate["top_results"]))

        positive_debug = self._run_cli_json("debug-signals", "--data-dir", str(self.positive_dir))
        negative_debug = self._run_cli_json("debug-signals", "--data-dir", str(self.negative_dir))
        self.assertGreater(positive_debug["setups_found"], 0)
        self.assertEqual(negative_debug["setups_found"], 0)

    def test_forward_report_and_live_scan_contracts(self) -> None:
        forward_report = self._run_cli_json("forward-report", "--telemetry-path", str(self.forward_telemetry))
        self.assertEqual(forward_report["closed_trades"], 2)
        self.assertIn("profit_factor", forward_report["gates"])
        self.assertTrue(forward_report["gates"]["profit_factor"]["passed"])

        live_scan = self._run_cli_json(
            "live-scan",
            "--available-eur",
            "100",
            "--duration-seconds",
            "5",
            "--max-messages",
            "20",
            "--bootstrap-dir",
            str(self.positive_dir),
            "--mode",
            "paper",
        )
        self.assertTrue(live_scan["preflight"]["armed"])
        self.assertEqual(live_scan["report"]["status"], "ok")
        self.assertGreater(live_scan["report"]["contexts_built"], 0)

    def test_history_status_and_walk_forward_contracts(self) -> None:
        history_status = self._run_cli_json(
            "history-status",
            "--data-dir",
            str(self.positive_dir),
            "--train-days",
            "1",
            "--test-days",
            "1",
        )
        self.assertTrue(history_status["sufficient_history"])
        self.assertIn("XBTEUR", history_status["pair_status"])

        walk_forward = self._run_cli_json(
            "walk-forward",
            "--data-dir",
            str(self.positive_dir),
            "--setup",
            "both",
            "--profile",
            "fast",
            "--objective",
            "hybrid",
            "--train-days",
            "1",
            "--test-days",
            "1",
            "--top",
            "1",
        )
        self.assertIn("folds", walk_forward)
        self.assertFalse(walk_forward["insufficient_history"])

        walk_forward_optimize = self._run_cli_json(
            "walk-forward-optimize",
            "--data-dir",
            str(self.positive_dir),
            "--setup",
            "both",
            "--profile",
            "fast",
            "--objective",
            "hybrid",
            "--train-days",
            "1",
            "--test-days",
            "1",
            "--top",
            "2",
        )
        self.assertFalse(walk_forward_optimize["insufficient_history"])
        self.assertGreater(walk_forward_optimize["variants_tested"], 0)
        self.assertIn("top_results", walk_forward_optimize)

    def test_monitor_and_dashboard_contracts(self) -> None:
        state_path = self.workdir / "supervisor_state.json"
        dashboard_path = self.workdir / "supervisor_dashboard.html"
        state_path.write_text(
            json.dumps(
                {
                    "status": "waiting_for_history",
                    "stopped_reason": "awaiting_next_cycle",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "supervisor_pid": None,
                    "supervisor_stop_path": str(self.workdir / "supervisor.stop"),
                    "paper_forward_pid": None,
                    "paper_forward_stop_path": str(self.workdir / "paper_forward.stop"),
                    "history_progress": {
                        "required_days": 13,
                        "available_days": 1.5,
                        "remaining_days": 11.5,
                        "progress_pct": 11.5,
                        "cycles_observed": 2,
                        "avg_growth_days_per_cycle": 0.1,
                        "avg_growth_days_per_hour": 0.05,
                        "estimated_cycles_to_ready": 115.0,
                        "estimated_seconds_to_ready": 828000.0,
                        "estimated_ready_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "daily_summary": {
                        "date": "2026-03-23",
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "supervisor_status": "waiting_for_history",
                        "progress_pct": 11.5,
                        "available_days": 1.5,
                        "required_days": 13,
                        "eta": None,
                        "last_errors": ["temporary ssl timeout"],
                        "gate_status": "waiting_for_history",
                        "gate_ready": False,
                        "gate_blockers": ["local_oos_history_not_ready"],
                        "paper_forward_status": "idle",
                        "research_scan_status": "skipped",
                        "research_scan_last_run_at": datetime.now(timezone.utc).isoformat(),
                        "research_scan_last_error": None,
                    },
                    "daily_summary_json_path": str(self.workdir / "supervisor_daily_summary.json"),
                    "daily_summary_markdown_path": str(self.workdir / "supervisor_daily_summary_2026-03-23.md"),
                    "dashboard_path": str(dashboard_path),
                    "state_path": str(state_path),
                    "research_scan": {
                        "enabled": True,
                        "session_open": False,
                        "should_run": False,
                        "ran": False,
                        "status": "skipped",
                        "stopped_reason": "session_closed",
                        "requested_duration_seconds": 90,
                        "requested_max_messages": 0,
                        "requested_available_eur": 100.0,
                        "started_at": None,
                        "finished_at": None,
                        "live_scan_report": None,
                    },
                    "last_prepare_report": {"ready_for_paper_forward": False},
                    "launch_report": None,
                }
            ),
            encoding="utf-8",
        )

        monitor = self._run_cli_json("monitor-supervisor", "--state-path", str(state_path))
        self.assertIn("daily_summary", monitor)
        self.assertEqual(monitor["daily_summary"]["supervisor_status"], "waiting_for_history")
        self.assertIn("research_scan", monitor)
        self.assertEqual(monitor["research_scan"]["status"], "skipped")

        dashboard = self._run_cli_json(
            "render-supervisor-dashboard",
            "--state-path",
            str(state_path),
            "--output-path",
            str(dashboard_path),
        )
        self.assertEqual(dashboard["status"], "ok")
        self.assertTrue(dashboard_path.exists())

    def test_personal_journal_presets_contract(self) -> None:
        payload = self._run_cli_json("personal-journal-presets")

        self.assertIn("presets", payload)
        self.assertGreaterEqual(len(payload["presets"]), 4)
        self.assertIn("preset_id", payload["presets"][0])

    def test_append_personal_trade_can_use_preset_contract(self) -> None:
        journal_path = self.workdir / "personal_trades.jsonl"
        payload = self._run_cli_json(
            "append-personal-trade",
            "--path",
            str(journal_path),
            "--preset",
            "sol_swing_4h",
            "--pnl-eur",
            "12.5",
            "--pnl-pct",
            "2.8",
            "--fees-eur",
            "0.4",
            "--entry-ts",
            "2026-03-30T08:00:00+00:00",
            "--exit-ts",
            "2026-03-30T12:00:00+00:00",
            "--entry-price",
            "120",
            "--exit-price",
            "125",
            "--size-notional-eur",
            "100",
        )

        self.assertEqual(payload["instrument"], "SOL")
        self.assertEqual(payload["strategy_name"], "manual_swing")
        self.assertEqual(payload["timeframe"], "4H")

    def _run_cli_json(self, *args: str) -> dict:
        env = os.environ.copy()
        env["BOT_PAIRS"] = "XBTEUR,ETHEUR,SOLEUR"
        completed = subprocess.run(
            [sys.executable, "-m", "daytrading_bot.cli", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return json.loads(completed.stdout)

    def _seed_positive_fixtures(self, data_dir: Path) -> None:
        contexts = build_default_universe_contexts(self.bot_config)
        for symbol, context in contexts.items():
            candles_1m, candles_15m = self._build_positive_series(context)
            write_csv_candles(history_csv_path(data_dir, symbol, 1), candles_1m)
            write_csv_candles(history_csv_path(data_dir, symbol, 15), candles_15m)

    def _seed_negative_fixtures(self, data_dir: Path) -> None:
        for symbol in (pair.symbol for pair in self.bot_config.pairs):
            context = self._build_no_trade_context(symbol)
            write_csv_candles(history_csv_path(data_dir, symbol, 1), list(context.candles_1m))
            write_csv_candles(history_csv_path(data_dir, symbol, 15), list(context.candles_15m))

    def _forward_events(self) -> list[dict]:
        return [
            {
                "ts": "2026-03-23T08:25:00Z",
                "event_type": "entry_rejected",
                "payload": {"pair": "XBTEUR", "reason": "min_notional"},
            },
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
                "ts": "2026-03-23T21:50:00Z",
                "event_type": "entry_sent",
                "payload": {"intent": {"pair": "ETHEUR"}, "response": {"ok": True, "dry_run": True}},
            },
            {
                "ts": "2026-03-24T06:10:00Z",
                "event_type": "exit_sent",
                "payload": {"pair": "ETHEUR", "reason": "session_flat", "pnl_eur": -2.0, "response": {"ok": True, "dry_run": True}},
            },
        ]

    def _build_positive_series(self, context: MarketContext) -> tuple[list[Candle], list[Candle]]:
        prefix_15m = self._make_bullish_prefix(140, start=datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc))
        prefix_1m = self._expand_15m_to_1m(prefix_15m)
        tail_shift = prefix_15m[-1].ts + timedelta(minutes=15) - context.candles_15m[0].ts
        tail_15m = [self._shift_candle(candle, tail_shift) for candle in context.candles_15m]
        tail_1m = [self._shift_candle(candle, tail_shift) for candle in context.candles_1m]
        return prefix_1m + tail_1m, prefix_15m + tail_15m

    def _build_no_trade_context(self, symbol: str) -> MarketContext:
        start = datetime(2026, 3, 23, 7, 0, tzinfo=timezone.utc)
        candles_15m: list[Candle] = []
        candles_5m: list[Candle] = []
        candles_1m: list[Candle] = []

        price_15 = 100.0
        for index in range(80):
            price_15 += 0.015 if index % 2 == 0 else -0.012
            candles_15m.append(self._candle(start + timedelta(minutes=15 * index), price_15, 90.0, 0.20, 0.18))

        price_5 = 100.0
        for index in range(40):
            price_5 += 0.01 if index % 2 == 0 else -0.009
            candles_5m.append(self._candle(start + timedelta(minutes=5 * index), price_5, 70.0, 0.12, 0.11))

        price_1 = 100.0
        for index in range(1300):
            price_1 += 0.003 if index % 2 == 0 else -0.0025
            candles_1m.append(self._candle(start + timedelta(minutes=index), price_1, 35.0, 0.05, 0.05))

        order_book = OrderBookSnapshot(
            symbol=symbol,
            best_bid=candles_5m[-1].close - 0.01,
            best_ask=candles_5m[-1].close + 0.01,
            bid_volume_top5=500.0,
            ask_volume_top5=520.0,
        )
        atr_values = atr(candles_15m, 14)
        atr_current = last_value(atr_values) or 0.5
        atr_pct_current = 100.0 * atr_current / candles_15m[-1].close
        atr_history = [atr_pct_current * ratio for ratio in [0.8 + (i * 0.003) for i in range(80)]]

        return MarketContext(
            symbol=symbol,
            candles_1m=candles_1m,
            candles_5m=candles_5m,
            candles_15m=candles_15m,
            order_book=order_book,
            atr_pct_history_15m=atr_history,
        )

    @staticmethod
    def _candle(ts: datetime, close: float, volume: float, high_offset: float, low_offset: float) -> Candle:
        return Candle(
            ts=ts,
            open=close - 0.10,
            high=close + high_offset,
            low=close - low_offset,
            close=close,
            volume=volume,
        )

    def _expand_15m_to_1m(self, candles_15m: list[Candle]) -> list[Candle]:
        candles_1m: list[Candle] = []
        for candle in candles_15m:
            closes = [
                candle.open + ((candle.close - candle.open) * fraction)
                for fraction in (1 / 15, 2 / 15, 3 / 15, 4 / 15, 5 / 15, 6 / 15, 7 / 15, 8 / 15, 9 / 15, 10 / 15, 11 / 15, 12 / 15, 13 / 15, 14 / 15, 1.0)
            ]
            for index, close_price in enumerate(closes):
                open_value = candle.open if index == 0 else candles_1m[-1].close
                high = max(open_value, close_price)
                low = min(open_value, close_price)
                if index == 7:
                    high = max(high, candle.high)
                    low = min(low, candle.low)
                candles_1m.append(
                    Candle(
                        ts=candle.ts + timedelta(minutes=index),
                        open=open_value,
                        high=high,
                        low=low,
                        close=close_price,
                        volume=candle.volume / 15.0,
                    )
                )
        return candles_1m

    @staticmethod
    def _make_bullish_prefix(count: int, start: datetime) -> list[Candle]:
        candles: list[Candle] = []
        price = 80.0
        for index in range(count):
            price += 0.3
            candles.append(
                Candle(
                    ts=start + timedelta(minutes=15 * index),
                    open=price - 0.25,
                    high=price + 0.45,
                    low=price - 0.30,
                    close=price,
                    volume=120.0,
                )
            )
        return candles

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

    @staticmethod
    def _sum_exit_distribution(rows: list[dict]) -> int:
        return sum(int(row["count"]) for row in rows)

    @staticmethod
    def _sum_setup_totals(rows: list[dict]) -> int:
        return sum(int(row["total_trades"]) for row in rows)


if __name__ == "__main__":
    unittest.main()
