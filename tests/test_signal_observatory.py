import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from daytrading_bot.config import BotConfig, ThreeCommasConfig
from daytrading_bot.engine import BotEngine
from daytrading_bot.signal_observatory import run_signal_observatory_report
from daytrading_bot.telemetry import InMemoryTelemetry
from tests.helpers import build_context


class SignalObservatoryTests(unittest.TestCase):
    def test_engine_research_logs_signal_observed_and_shadow_entries(self) -> None:
        telemetry = InMemoryTelemetry()
        engine = BotEngine(
            BotConfig(),
            ThreeCommasConfig(secret="secret", bot_uuid="bot", mode="paper"),
            telemetry=telemetry,
            enable_research=True,
        )
        engine.process_market(
            [build_context()],
            available_eur=100.0,
            moment=datetime(2026, 3, 23, 9, 30, tzinfo=engine.bot_config.timezone),
        )
        event_types = [event["event_type"] for event in telemetry.events]
        self.assertIn("signal_observed", event_types)
        self.assertIn("entry_sent", event_types)
        expected_shadow_lanes = sum(1 for runner in engine.shadow_lab.runners if "XBTEUR" in runner["spec"].allowed_symbols)
        self.assertEqual(event_types.count("shadow_entry_sent"), expected_shadow_lanes)

    def test_run_signal_observatory_report_summarizes_events(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "signals.jsonl"
            rows = [
                {
                    "ts": "2026-03-27T10:00:00Z",
                    "event_type": "signal_observed",
                    "payload": {
                        "pair": "XBTEUR",
                        "tradable": True,
                        "regime_label": "bullish",
                        "setup_type": "breakout_pullback",
                        "rejection_reasons": [],
                    },
                },
                {
                    "ts": "2026-03-27T10:01:00Z",
                    "event_type": "signal_observed",
                    "payload": {
                        "pair": "SOLEUR",
                        "tradable": False,
                        "regime_label": "recovery",
                        "setup_type": None,
                        "rejection_reasons": ["recent_shock_candle", "spread_too_wide"],
                    },
                },
                {
                    "ts": "2026-03-27T10:01:00Z",
                    "event_type": "entry_rejected",
                    "payload": {"pair": "SOLEUR", "reason": "daily_loss_limit"},
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            report = run_signal_observatory_report(path)

        self.assertTrue(report.source_exists)
        self.assertEqual(report.observed_signals, 2)
        self.assertEqual(report.tradable_signals, 1)
        self.assertAlmostEqual(report.tradable_rate, 0.5)
        self.assertEqual(report.decision_rejections, 1)
        self.assertEqual(report.pair_breakdown[0]["label"], "XBTEUR")
        rejection_labels = {row["label"] for row in report.rejection_breakdown}
        self.assertIn("recent_shock_candle", rejection_labels)
        decision_labels = {row["label"] for row in report.decision_breakdown}
        self.assertIn("daily_loss_limit", decision_labels)
        self.assertIsInstance(report.analysis_window_coverage, list)


if __name__ == "__main__":
    unittest.main()
