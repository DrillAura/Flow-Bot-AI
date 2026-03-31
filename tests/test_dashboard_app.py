import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from daytrading_bot.config import BotConfig
from daytrading_bot.dashboard_app import (
    build_dashboard_overview,
    find_latest_supervisor_state_path,
    query_windows_task,
    serve_dashboard_app,
)
from daytrading_bot.storage import history_csv_path, write_csv_candles
from tests.helpers import build_context, build_default_universe_contexts, build_recovery_context


class DashboardAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot_config = BotConfig()

    def test_find_latest_supervisor_state_path_prefers_latest_run(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            logs_root = Path(tempdir)
            older = logs_root / "supervisor_watchdog_20260326_100000"
            newer = logs_root / "supervisor_watchdog_20260327_100000"
            older.mkdir(parents=True, exist_ok=True)
            newer.mkdir(parents=True, exist_ok=True)
            older_state = older / "supervisor_state.json"
            newer_state = newer / "supervisor_state.json"
            older_state.write_text("{}", encoding="utf-8")
            newer_state.write_text("{}", encoding="utf-8")
            os.utime(older_state, (1_000_000_000, 1_000_000_000))
            os.utime(newer_state, (1_100_000_000, 1_100_000_000))

            latest = find_latest_supervisor_state_path(logs_root)

        self.assertEqual(latest, newer_state)

    def test_build_dashboard_overview_uses_state_history_and_task_data(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_dir = root / "data"
            logs_root = root / "logs" / "ops"
            run_dir = logs_root / "supervisor_watchdog_20260327_120000"
            run_dir.mkdir(parents=True, exist_ok=True)
            self._seed_histories(data_dir)
            state_path = run_dir / "supervisor_state.json"
            self._write_state(state_path)

            with patch("daytrading_bot.dashboard_app.query_windows_task", return_value={"task_name": "FlowBotSupervisorWatchdog", "exists": True, "status": "Ready", "last_run": "now", "last_result": "0", "run_as_user": "Home", "details": {}}), patch(
                "daytrading_bot.dashboard_app.load_live_ticker_snapshots",
                return_value=self._mock_market_snapshots(),
            ):
                overview = build_dashboard_overview(
                    bot_config=self.bot_config,
                    data_dir=data_dir,
                    logs_root=logs_root,
                    state_path=state_path,
                )

        self.assertEqual(overview["app"]["name"], "Flow Bot Monitor")
        self.assertEqual(overview["monitor"]["status"], "waiting_for_history")
        self.assertIn("XBTEUR", overview["history_status"]["pair_status"])
        self.assertEqual(len(overview["market"]["pairs"]), len(self.bot_config.pairs))
        self.assertIn("timeframe_options", overview["market"])
        self.assertIn("breadth_rows", overview["market"])
        self.assertIn("selected_symbol", overview["market"])
        self.assertIn("selected_timeframe", overview["market"])
        self.assertTrue(overview["market"]["pairs"][0]["timeframe_profiles"])
        self.assertIn("1S", overview["market"]["pairs"][0]["timeframe_profiles"])
        self.assertIn("5S", overview["market"]["pairs"][0]["timeframe_profiles"])
        self.assertIn("5H", overview["market"]["pairs"][0]["timeframe_profiles"])
        self.assertIn("12H", overview["market"]["pairs"][0]["timeframe_profiles"])
        self.assertIn("1D", overview["market"]["pairs"][0]["timeframe_profiles"])
        self.assertEqual(overview["launch"]["current_phase"], "History Capture")
        self.assertIn("forward_gates", overview["analytics"])
        self.assertIn("trade_analytics", overview)
        self.assertIn("signal_observatory", overview)
        self.assertIn("shadow_portfolios", overview)
        self.assertIn("strategy_lab", overview)
        self.assertIn("filter_options", overview["shadow_portfolios"])
        self.assertIn("copilot", overview)
        self.assertEqual(overview["strategy_lab"]["current_paper_strategy_id"], "mean_reversion_vwap")
        self.assertIn("regime_ready_count", overview["strategy_lab"]["summary"])
        self.assertIn("asset_ready_count", overview["strategy_lab"]["summary"])
        self.assertIn("paper_promotion_cooldown_until", overview["strategy_lab"])
        self.assertIn("filter_options", overview["trade_analytics"])
        self.assertIn("all_trades", overview["trade_analytics"])
        self.assertIn("mae_mfe_points", overview["trade_analytics"])
        self.assertIn("setups", overview["trade_analytics"]["filter_options"])
        self.assertIn("warnings", overview["copilot"])
        self.assertIn("gate_explanations", overview["copilot"])
        if overview["trade_analytics"]["all_trades"]:
            self.assertIn("trade_key", overview["trade_analytics"]["all_trades"][0])
        self.assertEqual(len(overview["recent_runs"]), 1)
        self.assertTrue(overview["last_cycle"]["available"])

    def test_dashboard_server_serves_html_api_and_health(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            data_dir = root / "data"
            logs_root = root / "logs" / "ops"
            run_dir = logs_root / "supervisor_watchdog_20260327_120000"
            run_dir.mkdir(parents=True, exist_ok=True)
            self._seed_histories(data_dir)
            state_path = run_dir / "supervisor_state.json"
            self._write_state(state_path)

            with patch("daytrading_bot.dashboard_app.query_windows_task", return_value={"task_name": "FlowBotSupervisorWatchdog", "exists": True, "status": "Ready", "last_run": "now", "last_result": "0", "run_as_user": "Home", "details": {}}), patch(
                "daytrading_bot.dashboard_app.load_live_ticker_snapshots",
                return_value=self._mock_market_snapshots(),
            ):
                server, url = serve_dashboard_app(
                    bot_config=self.bot_config,
                    data_dir=data_dir,
                    logs_root=logs_root,
                    state_path=state_path,
                    host="127.0.0.1",
                    port=0,
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    index = urlopen(url, timeout=5).read().decode("utf-8")
                    overview = json.loads(urlopen(f"{url}api/overview", timeout=5).read().decode("utf-8"))
                    health = json.loads(urlopen(f"{url}healthz", timeout=5).read().decode("utf-8"))
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

        self.assertIn("Read-Only Trading Cockpit", index)
        self.assertIn("Live Market Strip", index)
        self.assertIn("Market Explorer", index)
        self.assertIn("Launch Phase Lane", index)
        self.assertIn("Signal Observatory", index)
        self.assertIn("Shadow Portfolios", index)
        self.assertIn("Strategy Lab", index)
        self.assertIn("strategy-lab-summary-grid", index)
        self.assertIn("strategy-lab-regime-body", index)
        self.assertIn("market-explorer-title", index)
        self.assertIn("market-asset-select", index)
        self.assertIn("market-search-input", index)
        self.assertIn("market-favorites-toggle", index)
        self.assertIn("market-quick-filter-bar", index)
        self.assertIn("market-sidebar-list", index)
        self.assertIn("market-timeframe-strip", index)
        self.assertIn("market-explorer-summary-grid", index)
        self.assertIn("market-breadth-chart", index)
        self.assertIn("market-window-body", index)
        self.assertIn("shadow-filter-portfolio", index)
        self.assertIn("shadow-filter-behavior", index)
        self.assertIn("shadow-filter-regime", index)
        self.assertIn("Equity and PnL", index)
        self.assertIn("Trading Bot Copilot", index)
        self.assertIn("Warnings and Gate Guide", index)
        self.assertIn("trade-filter-pair", index)
        self.assertIn("trade-filter-setup", index)
        self.assertIn("trade-filter-quality", index)
        self.assertIn("trade-filter-reason", index)
        self.assertIn("trade-timeframe-controls", index)
        self.assertIn("export-trades-button", index)
        self.assertIn("selected-trade-title", index)
        self.assertIn("mae-mfe-chart", index)
        self.assertIn("trade-replay-chart", index)
        self.assertIn("strategy-lab-asset-meta", index)
        self.assertIn("strategy-lab-asset-body", index)
        self.assertEqual(overview["monitor"]["status"], "waiting_for_history")
        self.assertIn("market", overview)
        self.assertIn("trade_analytics", overview)
        self.assertIn("copilot", overview)
        self.assertTrue(health["ok"])

    def test_query_windows_task_decodes_non_utf8_output(self) -> None:
        completed = type(
            "CompletedProcess",
            (),
            {
                "returncode": 0,
                "stdout": "Status: Bereit\r\nLetztes Ergebnis: 0\r\n".encode("cp850"),
                "stderr": b"",
            },
        )()
        with patch("daytrading_bot.dashboard_app.subprocess.run", return_value=completed):
            payload = query_windows_task("FlowBotSupervisorWatchdog")

        self.assertTrue(payload["exists"])
        self.assertEqual(payload["status"], "Bereit")
        self.assertEqual(payload["last_result"], "0")

    def _seed_histories(self, data_dir: Path) -> None:
        contexts = build_default_universe_contexts(self.bot_config)
        data_dir.mkdir(parents=True, exist_ok=True)
        for symbol, context in contexts.items():
            write_csv_candles(history_csv_path(data_dir, symbol, 1), list(context.candles_1m))
            write_csv_candles(history_csv_path(data_dir, symbol, 15), list(context.candles_15m))

    def _mock_market_snapshots(self) -> dict[str, dict[str, float]]:
        snapshots: dict[str, dict[str, float]] = {}
        for index, pair in enumerate(self.bot_config.pairs):
            base = 100.0 + (index * 10.0)
            snapshots[pair.symbol] = {
                "last": base,
                "bid": base - 0.05,
                "ask": base + 0.05,
                "volume_24h": 1000.0 + (index * 100.0),
                "high_24h": base + 2.0,
                "low_24h": base - 2.0,
                "trades_24h": 500.0 + (index * 25.0),
            }
        return snapshots

    def _write_state(self, state_path: Path) -> None:
        state_path.write_text(
            json.dumps(
                {
                    "status": "waiting_for_history",
                    "stopped_reason": "awaiting_next_cycle",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "supervisor_pid": None,
                    "supervisor_stop_path": str(state_path.parent / "supervisor.stop"),
                    "paper_forward_pid": None,
                    "paper_forward_stop_path": str(state_path.parent / "paper_forward.stop"),
                    "history_progress": {
                        "required_days": 13,
                        "available_days": 2.5,
                        "remaining_days": 10.5,
                        "progress_pct": 19.23,
                        "cycles_observed": 4,
                        "avg_growth_days_per_cycle": 0.2,
                        "avg_growth_days_per_hour": 0.1,
                        "estimated_cycles_to_ready": 52.5,
                        "estimated_seconds_to_ready": 378000.0,
                        "estimated_ready_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "daily_summary": {
                        "date": "2026-03-27",
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "supervisor_status": "waiting_for_history",
                        "progress_pct": 19.23,
                        "available_days": 2.5,
                        "required_days": 13,
                        "eta": datetime.now(timezone.utc).isoformat(),
                        "last_errors": ["temporary ssl timeout"],
                        "gate_status": "waiting_for_history",
                        "gate_ready": False,
                        "gate_blockers": ["local_oos_history_not_ready"],
                        "paper_forward_status": "idle",
                        "strategy_lab_status": "active",
                        "strategy_lab_champion": "mean_reversion_vwap",
                        "strategy_lab_last_promotion_reason": "paper_promoted_to_mean_reversion_vwap",
                    },
                    "strategy_lab": {
                        "source_exists": True,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "current_paper_strategy_id": "mean_reversion_vwap",
                        "current_live_strategy_id": "champion_breakout",
                        "recommended_paper_strategy_id": "mean_reversion_vwap",
                        "recommended_live_strategy_id": "champion_breakout",
                        "paper_promotion_applied": True,
                        "live_promotion_applied": False,
                        "promotion_reason": "paper_promoted_to_mean_reversion_vwap",
                        "previous_paper_strategy_id": "champion_breakout",
                        "current_paper_promoted_at": datetime.now(timezone.utc).isoformat(),
                        "paper_promotion_cooldown_until": datetime.now(timezone.utc).isoformat(),
                        "rollback_applied": False,
                        "pinned_paper_strategy_id": None,
                        "strategies": [
                            {
                                "strategy_id": "mean_reversion_vwap",
                                "label": "VWAP Mean Reversion",
                                "family": "mean_reversion",
                                "strategy_type": "mean_reversion_vwap",
                                "closed_trades": 6,
                                "wins": 5,
                                "losses": 1,
                                "win_rate": 0.8333,
                                "profit_factor": 2.1,
                                "expectancy_eur": 0.42,
                                "net_pnl_eur": 2.5,
                                "max_drawdown_pct": 0.01,
                                "average_hold_minutes": 25.0,
                                "distinct_regimes": 2,
                                "dominant_regime_share": 0.5,
                                "score": 18.4,
                                "gates": {},
                                "eligible_for_promotion": True,
                                "latest_activity_ts": datetime.now(timezone.utc).isoformat(),
                                "regime_breakdown": [],
                                "setup_breakdown": [],
                            },
                            {
                                "strategy_id": "champion_breakout",
                                "label": "Champion Breakout",
                                "family": "breakout_recovery",
                                "strategy_type": "breakout_recovery",
                                "closed_trades": 6,
                                "wins": 3,
                                "losses": 3,
                                "win_rate": 0.5,
                                "profit_factor": 1.1,
                                "expectancy_eur": 0.05,
                                "net_pnl_eur": 0.3,
                                "max_drawdown_pct": 0.02,
                                "average_hold_minutes": 28.0,
                                "distinct_regimes": 2,
                                "dominant_regime_share": 0.5,
                                "score": 7.2,
                                "gates": {},
                                "eligible_for_promotion": False,
                                "latest_activity_ts": datetime.now(timezone.utc).isoformat(),
                                "regime_breakdown": [],
                                "setup_breakdown": [],
                            },
                        ],
                    },
                    "daily_summary_json_path": str(state_path.parent / "supervisor_daily_summary.json"),
                    "daily_summary_markdown_path": str(state_path.parent / "supervisor_daily_summary_2026-03-27.md"),
                    "dashboard_path": str(state_path.parent / "supervisor_dashboard.html"),
                    "state_path": str(state_path),
                    "last_prepare_report": {
                        "capture_report": {
                            "cycle_reports": [
                                {
                                    "cycle": 4,
                                    "sync_result": [
                                        {
                                            "intervals": {
                                                "1m": {
                                                    "XBTEUR": {"status": "ok", "existing_rows": 100, "fetched_rows": 5, "merged_rows": 105, "written_rows": 105, "last": 123},
                                                    "ETHEUR": {"status": "ok", "existing_rows": 100, "fetched_rows": 4, "merged_rows": 104, "written_rows": 104, "last": 123},
                                                },
                                                "15m": {
                                                    "XBTEUR": {"status": "ok", "existing_rows": 20, "fetched_rows": 1, "merged_rows": 21, "written_rows": 21, "last": 123},
                                                },
                                            }
                                        }
                                    ],
                                    "history_status": {"required_days": 13, "available_days": 2.5, "sufficient_history": False, "pair_status": {}},
                                    "error": "",
                                }
                            ]
                        },
                        "ready_for_paper_forward": False,
                    },
                    "launch_report": None,
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()

