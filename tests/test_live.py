import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from daytrading_bot.config import BotConfig, ThreeCommasConfig
from daytrading_bot.kraken import KrakenMarketStore, KrakenPairMetadata
from daytrading_bot.live import KrakenLiveScanner
from daytrading_bot.storage import history_csv_path, write_csv_candles
from tests.helpers import build_context


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, payload):
        self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


class FailingWebSocket:
    async def __aenter__(self):
        raise TimeoutError("handshake failed")

    async def __aexit__(self, exc_type, exc, tb):
        return False


class LiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.bot_config = BotConfig(telemetry_path=f"{self.tempdir.name}/events.jsonl")
        self.exec_config = ThreeCommasConfig(secret="secret", bot_uuid="bot", mode="paper")

    def _seeded_scanner(self) -> KrakenLiveScanner:
        scanner = KrakenLiveScanner(self.bot_config, self.exec_config, bootstrap_dir=self.tempdir.name)
        context = build_context()
        metadata = {
            "XBTEUR": KrakenPairMetadata("XBTEUR", "XBT/EUR", 0.00005, 0.45, 0.1, 1, 8, "online"),
        }
        store = KrakenMarketStore(metadata)
        store.seed_history("XBTEUR", 1, list(context.candles_1m))
        store.seed_history("XBTEUR", 5, list(context.candles_5m))
        store.seed_history("XBTEUR", 15, list(context.candles_15m))
        store.books["XBTEUR"].apply_message(
            {
                "bids": [{"price": context.order_book.best_bid, "qty": 10.0}],
                "asks": [{"price": context.order_book.best_ask, "qty": 11.0}],
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
        scanner.store = store
        return scanner

    def test_live_scanner_reconnects_after_transient_failure(self) -> None:
        scanner = self._seeded_scanner()
        update_message = json.dumps(
            {
                "channel": "book",
                "data": [
                    {
                        "symbol": "XBT/EUR",
                        "bids": [{"price": 100.0, "qty": 12.0}],
                        "asks": [{"price": 100.5, "qty": 13.0}],
                        "timestamp": "2026-03-23T09:00:00Z",
                    }
                ],
            }
        )
        connect_calls = []

        def fake_connect(*args, **kwargs):
            connect_calls.append(1)
            if len(connect_calls) == 1:
                return FailingWebSocket()
            return FakeWebSocket([update_message])

        with patch("daytrading_bot.live.websockets.connect", side_effect=fake_connect):
            report = asyncio.run(scanner.run(available_eur=100.0, duration_seconds=5, max_messages=1))

        self.assertEqual(report.reconnects, 1)
        self.assertEqual(report.messages_seen, 1)
        self.assertEqual(report.status, "degraded")
        self.assertGreaterEqual(report.contexts_built, 1)

    def test_bootstrap_uses_interval_specific_csv_fallback(self) -> None:
        single_pair_config = BotConfig(
            telemetry_path=f"{self.tempdir.name}/events.jsonl",
            pairs=(self.bot_config.pair_by_symbol("XBTEUR"),),
        )
        scanner = KrakenLiveScanner(single_pair_config, self.exec_config, bootstrap_dir=self.tempdir.name)
        context = build_context()
        write_csv_candles(history_csv_path(Path(self.tempdir.name), "XBTEUR", 1), list(context.candles_1m))
        write_csv_candles(history_csv_path(Path(self.tempdir.name), "XBTEUR", 15), list(context.candles_15m))

        metadata = {
            "XBTEUR": KrakenPairMetadata("XBTEUR", "XBT/EUR", 0.00005, 0.45, 0.1, 1, 8, "online"),
        }
        with patch.object(scanner.kraken, "fetch_asset_pairs", return_value=metadata), patch.object(
            scanner.kraken,
            "fetch_ohlc",
            side_effect=RuntimeError("REST unavailable"),
        ):
            store = scanner.bootstrap()

        self.assertEqual(len(store.candles["XBTEUR"][1]), 500)
        self.assertEqual(store.candles["XBTEUR"][1][-1].ts, context.candles_1m[-1].ts)
        self.assertEqual(len(store.candles["XBTEUR"][15]), len(context.candles_15m))

    def test_bootstrap_uses_raw_dedicated_15m_csv_without_reaggregation(self) -> None:
        single_pair_config = BotConfig(
            telemetry_path=f"{self.tempdir.name}/events.jsonl",
            pairs=(self.bot_config.pair_by_symbol("XBTEUR"),),
        )
        scanner = KrakenLiveScanner(single_pair_config, self.exec_config, bootstrap_dir=self.tempdir.name)
        context = build_context()
        write_csv_candles(history_csv_path(Path(self.tempdir.name), "XBTEUR", 15), list(context.candles_15m[:2]))

        metadata = {
            "XBTEUR": KrakenPairMetadata("XBTEUR", "XBT/EUR", 0.00005, 0.45, 0.1, 1, 8, "online"),
        }
        with patch.object(scanner.kraken, "fetch_asset_pairs", return_value=metadata), patch.object(
            scanner.kraken,
            "fetch_ohlc",
            side_effect=RuntimeError("REST unavailable"),
        ):
            candles = scanner._load_bootstrap_candles("XBTEUR", interval=15)

        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].close, context.candles_15m[0].close)

    def test_zero_duration_does_not_stop_live_scan_loop(self) -> None:
        self.assertFalse(KrakenLiveScanner._stop(messages_seen=0, start=0.0, duration_seconds=0, max_messages=None))
        self.assertTrue(KrakenLiveScanner._stop(messages_seen=1, start=0.0, duration_seconds=0, max_messages=1))
        stop_file = Path(self.tempdir.name) / "live.stop"
        stop_file.write_text("stop", encoding="utf-8")
        self.assertTrue(KrakenLiveScanner._stop(messages_seen=0, start=0.0, duration_seconds=0, max_messages=None, stop_file=stop_file))

    def test_bootstrap_falls_back_to_static_pair_metadata_when_asset_pairs_rest_fails(self) -> None:
        single_pair_config = BotConfig(
            telemetry_path=f"{self.tempdir.name}/events.jsonl",
            pairs=(self.bot_config.pair_by_symbol("XBTEUR"),),
        )
        scanner = KrakenLiveScanner(single_pair_config, self.exec_config, bootstrap_dir=self.tempdir.name)
        context = build_context()
        write_csv_candles(history_csv_path(Path(self.tempdir.name), "XBTEUR", 1), list(context.candles_1m))
        write_csv_candles(history_csv_path(Path(self.tempdir.name), "XBTEUR", 15), list(context.candles_15m))

        with patch.object(scanner.kraken, "fetch_asset_pairs", side_effect=RuntimeError("metadata rest unavailable")), patch.object(
            scanner.kraken,
            "fetch_ohlc",
            side_effect=RuntimeError("REST unavailable"),
        ):
            store = scanner.bootstrap()

        self.assertIn("asset_pairs REST bootstrap failed; using static pair metadata fallback", scanner.bootstrap_errors)
        self.assertEqual(store.websocket_symbols(), ["XBT/EUR"])


if __name__ == "__main__":
    unittest.main()
