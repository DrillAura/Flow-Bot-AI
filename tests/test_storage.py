import tempfile
import unittest
from pathlib import Path

from daytrading_bot.storage import history_csv_path, load_interval_candles, write_csv_candles
from tests.helpers import build_context


class StorageTests(unittest.TestCase):
    def test_history_csv_path_separates_15m_files(self) -> None:
        data_dir = Path("C:/tmp/data")
        self.assertEqual(str(history_csv_path(data_dir, "XBTEUR", 1)), "C:\\tmp\\data\\XBTEUR.csv")
        self.assertEqual(str(history_csv_path(data_dir, "XBTEUR", 15)), "C:\\tmp\\data\\XBTEUR.15m.csv")

    def test_load_interval_candles_falls_back_to_aggregated_1m_history(self) -> None:
        context = build_context()
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = Path(tempdir)
            write_csv_candles(history_csv_path(data_dir, "XBTEUR", 1), list(context.candles_1m))

            candles_15m = load_interval_candles(data_dir, "XBTEUR", 15)

            self.assertGreater(len(candles_15m), 0)
            self.assertEqual(candles_15m[0].ts, context.candles_1m[0].ts)


if __name__ == "__main__":
    unittest.main()
