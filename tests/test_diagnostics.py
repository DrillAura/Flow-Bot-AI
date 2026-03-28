import tempfile
import unittest
from pathlib import Path

from daytrading_bot.config import BotConfig
from daytrading_bot.diagnostics import run_signal_diagnostics
from daytrading_bot.storage import history_csv_path, write_csv_candles
from tests.helpers import build_default_universe_contexts


class DiagnosticsTests(unittest.TestCase):
    def test_signal_diagnostics_reports_contexts_and_rejections(self) -> None:
        bot_config = BotConfig()
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = Path(tempdir)
            for symbol, context in build_default_universe_contexts(bot_config).items():
                write_csv_candles(history_csv_path(data_dir, symbol, 1), list(context.candles_1m))
                write_csv_candles(history_csv_path(data_dir, symbol, 15), list(context.candles_15m))

            report = run_signal_diagnostics(data_dir, bot_config)
            self.assertGreater(report.total_contexts, 0)
            self.assertIn("XBTEUR", report.pair_context_counts)
            self.assertIsInstance(report.rejection_counts, dict)
            self.assertIn("history_15m", report.filter_stats)
            self.assertGreater(report.filter_stats["history_15m"].passed, 0)
            self.assertGreaterEqual(report.filter_stats["spread_bps"].coverage_rate, 0.0)


if __name__ == "__main__":
    unittest.main()
