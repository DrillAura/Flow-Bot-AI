import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch

from daytrading_bot.kraken import KrakenMarketStore, KrakenOrderBook, KrakenPairMetadata, KrakenPublicClient, ws_ohlc_to_candle
from daytrading_bot.storage import load_csv_candles
from tests.helpers import build_context


class KrakenTests(unittest.TestCase):
    def test_parse_ohlc_rows(self) -> None:
        rows = [[1774168800, "100.0", "101.0", "99.5", "100.5", "100.1", "12.5", 42]]
        candles = KrakenPublicClient.parse_ohlc_rows(rows)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].open, 100.0)
        self.assertEqual(candles[0].close, 100.5)
        self.assertEqual(candles[0].volume, 12.5)

    def test_order_book_applies_snapshot_and_update(self) -> None:
        book = KrakenOrderBook(symbol="XBTEUR")
        book.apply_message(
            {
                "bids": [{"price": 100.0, "qty": 5.0}, {"price": 99.5, "qty": 4.0}],
                "asks": [{"price": 100.5, "qty": 3.0}, {"price": 101.0, "qty": 2.0}],
                "timestamp": "2026-03-23T09:00:00.123456Z",
            }
        )
        book.apply_message(
            {
                "bids": [{"price": 100.0, "qty": 6.0}, {"price": 99.5, "qty": 0}],
                "asks": [{"price": 100.5, "qty": 0}, {"price": 100.6, "qty": 1.5}],
                "timestamp": "2026-03-23T09:00:01.123456Z",
            }
        )
        snapshot = book.to_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.best_bid, 100.0)
        self.assertEqual(snapshot.best_ask, 100.6)
        self.assertAlmostEqual(snapshot.bid_volume_top5, 6.0)

    def test_market_store_builds_contexts_from_seeded_history(self) -> None:
        metadata = {
            "XBTEUR": KrakenPairMetadata("XBTEUR", "XBT/EUR", 0.00005, 0.45, 0.1, 1, 8, "online"),
        }
        store = KrakenMarketStore(metadata)
        context = build_context()
        store.seed_history("XBTEUR", 1, list(context.candles_1m))
        store.seed_history("XBTEUR", 5, list(context.candles_5m))
        store.seed_history("XBTEUR", 15, list(context.candles_15m))
        store.apply_ws_message(
            {
                "channel": "book",
                "type": "snapshot",
                "data": [
                    {
                        "symbol": "XBT/EUR",
                        "bids": [{"price": context.order_book.best_bid, "qty": 10.0}],
                        "asks": [{"price": context.order_book.best_ask, "qty": 11.0}],
                        "timestamp": "2026-03-23T09:00:00.123456Z",
                    }
                ],
            }
        )
        contexts = store.build_contexts()
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].symbol, "XBTEUR")

    def test_ws_ohlc_to_candle(self) -> None:
        candle = ws_ohlc_to_candle(
            {
                "interval_begin": "2026-03-23T09:00:00.123456789Z",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 15.0,
            }
        )
        self.assertEqual(candle.ts.tzinfo, timezone.utc)
        self.assertEqual(candle.close, 100.5)

    def test_sync_ohlc_csv_merges_existing_and_new_rows(self) -> None:
        client = KrakenPublicClient()
        base_context = build_context()
        incoming = list(base_context.candles_1m[-2:])
        existing = [base_context.candles_1m[-3], base_context.candles_1m[-2]]

        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "XBTEUR.csv"
            from daytrading_bot.storage import write_csv_candles

            write_csv_candles(path, existing)

            original = client.fetch_ohlc
            client.fetch_ohlc = lambda pair, interval=1, since=None: (incoming, 1234567890)  # type: ignore[assignment]
            try:
                result = client.sync_ohlc_csv("XBTEUR", interval=1, output_path=path)
            finally:
                client.fetch_ohlc = original  # type: ignore[assignment]

            merged = load_csv_candles(path)
            self.assertEqual(result["existing_rows"], 2)
            self.assertEqual(result["fetched_rows"], 2)
            self.assertEqual(result["merged_rows"], 3)
            self.assertEqual(len(merged), 3)

    def test_sync_ohlc_csv_skips_empty_fetch_without_overwriting_existing_file(self) -> None:
        client = KrakenPublicClient()
        base_context = build_context()
        existing = list(base_context.candles_1m[-3:-1])

        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "XBTEUR.csv"
            from daytrading_bot.storage import write_csv_candles

            write_csv_candles(path, existing)
            original_text = path.read_text(encoding="utf-8")

            original = client.fetch_ohlc
            client.fetch_ohlc = lambda pair, interval=1, since=None: ([], 1234567890)  # type: ignore[assignment]
            try:
                result = client.sync_ohlc_csv("XBTEUR", interval=1, output_path=path)
            finally:
                client.fetch_ohlc = original  # type: ignore[assignment]

            self.assertEqual(result["status"], "skipped_empty_fetch")
            self.assertEqual(result["written_rows"], 0)
            self.assertEqual(path.read_text(encoding="utf-8"), original_text)

    def test_sync_ohlc_csv_repairs_corrupt_existing_file(self) -> None:
        client = KrakenPublicClient()
        base_context = build_context()
        incoming = list(base_context.candles_1m[-2:])

        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "XBTEUR.csv"
            path.write_text("broken,content\n", encoding="utf-8")

            original = client.fetch_ohlc
            client.fetch_ohlc = lambda pair, interval=1, since=None: (incoming, 1234567890)  # type: ignore[assignment]
            try:
                result = client.sync_ohlc_csv("XBTEUR", interval=1, output_path=path)
            finally:
                client.fetch_ohlc = original  # type: ignore[assignment]

            merged = load_csv_candles(path)
            self.assertEqual(result["status"], "written")
            self.assertEqual(result["repaired"], 1)
            self.assertEqual(len(merged), 2)

    def test_load_csv_candles_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "missing.csv"
            self.assertEqual(load_csv_candles(path), [])


if __name__ == "__main__":
    unittest.main()
