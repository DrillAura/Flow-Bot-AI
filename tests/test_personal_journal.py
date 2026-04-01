import json
import tempfile
import unittest
from pathlib import Path

from daytrading_bot.personal_journal import (
    append_personal_trade,
    build_personal_journal_payload,
    build_personal_trade_entry,
    ensure_personal_journal_path,
    list_personal_journal_presets,
    resolve_personal_journal_preset,
    run_personal_journal_report,
)


class PersonalJournalTests(unittest.TestCase):
    def test_append_and_report_personal_trades(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "journal" / "personal_trades.jsonl"
            ensure_personal_journal_path(path)
            append_personal_trade(
                path,
                build_personal_trade_entry(
                    market="crypto",
                    instrument="SOL",
                    venue="Kraken",
                    side="long",
                    strategy_name="manual_swing",
                    setup_family="swing",
                    timeframe="4H",
                    status="closed",
                    entry_ts="2026-03-30T08:00:00+00:00",
                    exit_ts="2026-03-30T12:00:00+00:00",
                    entry_price=120.0,
                    exit_price=126.0,
                    pnl_eur=14.5,
                    pnl_pct=3.2,
                    fees_eur=0.6,
                    size_notional_eur=100.0,
                    confidence_before=62,
                    confidence_after=74,
                    lesson="Trend hat sauber getragen",
                    notes="Plan eingehalten",
                    tags=["swing", "crypto"],
                    mistakes=["late_exit"],
                ),
            )
            append_personal_trade(
                path,
                build_personal_trade_entry(
                    market="fx",
                    instrument="XAUUSD",
                    venue="Broker",
                    side="long",
                    strategy_name="micro_trial",
                    setup_family="fast",
                    timeframe="1M",
                    status="closed",
                    entry_ts="2026-03-30T13:00:00+00:00",
                    exit_ts="2026-03-30T13:12:00+00:00",
                    entry_price=2200.0,
                    exit_price=2198.0,
                    pnl_eur=-6.0,
                    pnl_pct=-0.4,
                    fees_eur=0.2,
                    size_notional_eur=150.0,
                    confidence_before=55,
                    confidence_after=48,
                    lesson="Stop zu spaet respektiert",
                    notes="zu aggressiver Entry",
                    tags=["gold", "fast"],
                    mistakes=["late_stop"],
                ),
            )

            summary = run_personal_journal_report(path)
            payload = build_personal_journal_payload(summary)

        self.assertTrue(summary.source_exists)
        self.assertEqual(summary.total_trades, 2)
        self.assertEqual(summary.closed_trades, 2)
        self.assertAlmostEqual(summary.win_rate, 0.5)
        self.assertAlmostEqual(summary.net_pnl_eur, 8.5)
        self.assertEqual(payload["summary"]["total_entries"], 2)
        self.assertEqual(payload["entries"][0]["source"], "manual")
        self.assertIn("title", payload["strategy_notes"][0])
        self.assertIn("detail", payload["learning_points"][0])
        self.assertIsInstance(payload["beginner_notes"][0], dict)
        self.assertTrue(summary.setup_families)
        self.assertTrue(summary.venues)
        self.assertTrue(summary.timeframes)
        self.assertTrue(summary.recommendations)
        self.assertTrue(payload["presets"])
        self.assertEqual(payload["setup_families"][0]["label"], "swing")
        self.assertEqual(payload["timeframe_breakdown"][0]["label"], "4H")

    def test_ensure_personal_journal_creates_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "nested" / "journal.jsonl"
            created = ensure_personal_journal_path(path)
            self.assertTrue(created.exists())
            self.assertEqual(created.read_text(encoding="utf-8"), "")

    def test_personal_journal_presets_are_dashboard_friendly(self) -> None:
        presets = list_personal_journal_presets()

        self.assertGreaterEqual(len(presets), 4)
        self.assertIn("preset_id", presets[0])
        self.assertIn("strategy_name", presets[0])
        self.assertIn("beginner_hint", presets[0])
        self.assertTrue(any(preset["preset_id"] == "sol_swing_4h" for preset in presets))
        self.assertTrue(any(preset["preset_id"] == "btc_micro_1m" for preset in presets))

    def test_build_personal_trade_entry_can_fill_from_preset(self) -> None:
        entry = build_personal_trade_entry(
            preset_id="sol_swing_4h",
            market="",
            instrument="",
            venue="",
            side="",
            strategy_name="",
            setup_family="",
            timeframe="",
            status="",
            entry_ts="2026-03-30T08:00:00+00:00",
            exit_ts="2026-03-30T12:00:00+00:00",
            entry_price=120.0,
            exit_price=126.0,
            pnl_eur=14.5,
            pnl_pct=3.2,
            fees_eur=0.6,
            size_notional_eur=100.0,
            confidence_before=62,
            confidence_after=74,
            lesson="Trend hat sauber getragen",
            notes="Plan eingehalten",
        )

        self.assertEqual(entry.instrument, "SOL")
        self.assertEqual(entry.strategy_name, "manual_swing")
        self.assertEqual(entry.timeframe, "4H")
        self.assertIn("sol", [tag.lower() for tag in entry.tags])

    def test_resolve_personal_journal_preset_returns_none_for_unknown(self) -> None:
        self.assertIsNone(resolve_personal_journal_preset("does_not_exist"))


if __name__ == "__main__":
    unittest.main()
