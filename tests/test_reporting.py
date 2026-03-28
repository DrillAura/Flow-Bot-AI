import json
import tempfile
import unittest
from pathlib import Path

from daytrading_bot.config import BotConfig
from daytrading_bot.reporting import run_forward_test_report, run_signal_debug_report
from daytrading_bot.storage import history_csv_path, write_csv_candles
from tests.helpers import build_default_universe_contexts


class ReportingTests(unittest.TestCase):
    def test_signal_debug_report_groups_first_failures_by_pair_and_session(self) -> None:
        bot_config = BotConfig()
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = Path(tempdir)
            for symbol, context in build_default_universe_contexts(bot_config).items():
                write_csv_candles(history_csv_path(data_dir, symbol, 1), list(context.candles_1m))
                write_csv_candles(history_csv_path(data_dir, symbol, 15), list(context.candles_15m))

            report = run_signal_debug_report(data_dir, bot_config)

        self.assertGreater(report.total_contexts, 0)
        self.assertIn("XBTEUR", report.pair_session_buckets)
        self.assertIn("morning", report.pair_session_buckets["XBTEUR"])
        self.assertTrue(report.global_first_failures)

    def test_forward_report_evaluates_go_live_gates_from_telemetry(self) -> None:
        bot_config = BotConfig()
        with tempfile.TemporaryDirectory() as tempdir:
            telemetry_path = Path(tempdir) / "events.jsonl"
            events = [
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
            telemetry_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8-sig")

            report = run_forward_test_report(telemetry_path, bot_config)

        self.assertTrue(report.source_exists)
        self.assertEqual(report.events_loaded, 5)
        self.assertEqual(report.closed_trades, 2)
        self.assertEqual(report.wins, 1)
        self.assertEqual(report.losses, 1)
        self.assertAlmostEqual(report.net_pnl_eur, 2.0)
        self.assertEqual(report.overnight_positions, 1)
        self.assertEqual(report.rejection_counts["min_notional"], 1)
        self.assertTrue(report.gates["profit_factor"].passed)
        self.assertFalse(report.gates["win_rate"].passed)
        self.assertFalse(report.gates["trade_count"].passed)
        self.assertFalse(report.gates["no_overnight_positions"].passed)
        self.assertFalse(report.go_live_ready)


if __name__ == "__main__":
    unittest.main()
