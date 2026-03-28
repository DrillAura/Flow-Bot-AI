from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

import websockets

from .config import BotConfig, ThreeCommasConfig
from .engine import BotEngine
from .indicators import aggregate_candles
from .kraken import KrakenMarketStore, KrakenPairMetadata, KrakenPublicClient
from .storage import history_csv_path, load_csv_candles


@dataclass(frozen=True)
class LiveScanReport:
    status: str
    error: str
    messages_seen: int
    contexts_built: int
    events_emitted: int
    reconnects: int
    ending_equity: float
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float


class KrakenLiveScanner:
    ws_url = "wss://ws.kraken.com/v2"

    def __init__(
        self,
        bot_config: BotConfig,
        execution_config: ThreeCommasConfig,
        bootstrap_dir: str | None = None,
        stop_file: str | None = None,
    ) -> None:
        self.bot_config = bot_config
        self.execution_config = execution_config
        self.kraken = KrakenPublicClient()
        self.engine = BotEngine(bot_config, execution_config, enable_research=True)
        self.store: KrakenMarketStore | None = None
        self.bootstrap_dir = Path(bootstrap_dir) if bootstrap_dir else None
        self.stop_file = Path(stop_file) if stop_file else None
        self.bootstrap_errors: list[str] = []

    def bootstrap(self) -> KrakenMarketStore:
        self.bootstrap_errors.clear()
        try:
            metadata_raw = self.kraken.fetch_asset_pairs([pair.symbol for pair in self.bot_config.pairs])
        except Exception:
            if self.bootstrap_dir is None:
                raise
            metadata_raw = self._fallback_pair_metadata()
            self.bootstrap_errors.append("asset_pairs REST bootstrap failed; using static pair metadata fallback")
        by_altname = {metadata.altname: metadata for metadata in metadata_raw.values()}
        store = KrakenMarketStore(by_altname)
        loaded_any = False
        for pair in self.bot_config.pairs:
            candles_1m = self._load_bootstrap_candles(pair.symbol, interval=1)
            candles_15m = self._load_bootstrap_candles(pair.symbol, interval=15)
            if candles_1m:
                store.seed_history(pair.symbol, 1, candles_1m)
                store.seed_history(pair.symbol, 5, aggregate_candles(candles_1m, 5))
                loaded_any = True
            if candles_15m:
                store.seed_history(pair.symbol, 15, candles_15m)
                loaded_any = True
            if not candles_1m and not candles_15m:
                self.bootstrap_errors.append(f"{pair.symbol}: no bootstrap history available")
        if not loaded_any:
            raise RuntimeError("Unable to bootstrap any Kraken market history")
        self.store = store
        return store

    def _fallback_pair_metadata(self) -> dict[str, KrakenPairMetadata]:
        metadata: dict[str, KrakenPairMetadata] = {}
        for pair in self.bot_config.pairs:
            base = pair.symbol[:-3]
            if base == "XBT":
                ws_base = "XBT"
            else:
                ws_base = base
            metadata[pair.symbol] = KrakenPairMetadata(
                altname=pair.symbol,
                wsname=f"{ws_base}/EUR",
                ordermin=0.00005,
                costmin=0.45,
                tick_size=0.1,
                pair_decimals=1,
                lot_decimals=8,
                status="online",
            )
        return metadata

    def _load_bootstrap_candles(self, symbol: str, interval: int):
        try:
            candles, _ = self.kraken.fetch_ohlc(symbol, interval=interval)
            return candles
        except Exception:
            if self.bootstrap_dir is None:
                self.bootstrap_errors.append(f"{symbol} interval {interval}: Kraken bootstrap failed")
                raise
            return self._load_csv_fallback(symbol, interval)

    def _load_csv_fallback(self, symbol: str, interval: int):
        candidate_paths = [history_csv_path(self.bootstrap_dir, symbol, interval)]
        if interval != 1:
            candidate_paths.append(history_csv_path(self.bootstrap_dir, symbol, 1))

        for csv_path in candidate_paths:
            if not csv_path.exists():
                self.bootstrap_errors.append(f"{symbol} interval {interval}: no CSV fallback at {csv_path}")
                continue
            try:
                candles = load_csv_candles(csv_path)
            except Exception as exc:
                self.bootstrap_errors.append(f"{symbol} interval {interval}: CSV fallback failed: {exc}")
                continue
            if not candles:
                self.bootstrap_errors.append(f"{symbol} interval {interval}: CSV fallback empty at {csv_path}")
                continue
            if csv_path == history_csv_path(self.bootstrap_dir, symbol, 1) and interval != 1:
                return aggregate_candles(candles, interval)
            return candles
        return []

    async def run(
        self,
        available_eur: float,
        duration_seconds: int = 60,
        max_messages: int | None = None,
    ) -> LiveScanReport:
        store = self.store or self.bootstrap()
        start = time.monotonic()
        messages_seen = 0
        contexts_seen = 0
        events_emitted = 0
        reconnects = 0
        status = "ok"
        error = ""
        while not self._stop(messages_seen, start, duration_seconds, max_messages, self.stop_file):
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                ) as websocket:
                    await self._subscribe(websocket, store.websocket_symbols())
                    async for raw_message in websocket:
                        message = json.loads(raw_message)
                        messages_seen += 1
                        updated_symbols = store.apply_ws_message(message)
                        if not updated_symbols:
                            if self._stop(messages_seen, start, duration_seconds, max_messages, self.stop_file):
                                break
                            continue

                        contexts = store.build_contexts()
                        contexts_seen = max(contexts_seen, len(contexts))
                        if contexts:
                            moment = max(context.candles_1m[-1].ts for context in contexts)
                            events = self.engine.process_market(contexts, available_eur=available_eur, moment=moment)
                            events_emitted += len(events)
                            for event in events:
                                self.engine.telemetry.log("live_event", event)

                        if self._stop(messages_seen, start, duration_seconds, max_messages, self.stop_file):
                            break
                if self._stop(messages_seen, start, duration_seconds, max_messages, self.stop_file):
                    break
            except Exception as exc:
                reconnects += 1
                status = "degraded"
                error = str(exc)
                self.engine.telemetry.log("live_scan_error", {"error": error, "reconnects": reconnects})
                if self._stop(messages_seen, start, duration_seconds, max_messages, self.stop_file):
                    break
                await asyncio.sleep(1.0)
                continue

        state = self.engine.risk.state
        return LiveScanReport(
            status=status,
            error=error,
            messages_seen=messages_seen,
            contexts_built=contexts_seen,
            events_emitted=events_emitted,
            reconnects=reconnects,
            ending_equity=state.equity,
            win_rate=state.win_rate,
            profit_factor=state.profit_factor,
            max_drawdown_pct=self.engine.risk.max_drawdown_pct,
        )

    async def _subscribe(self, websocket: websockets.ClientConnection, ws_symbols: list[str]) -> None:
        subscriptions = [
            {"method": "subscribe", "params": {"channel": "ticker", "symbol": ws_symbols, "snapshot": True}},
            {"method": "subscribe", "params": {"channel": "book", "symbol": ws_symbols, "depth": 10, "snapshot": True}},
            {"method": "subscribe", "params": {"channel": "ohlc", "symbol": ws_symbols, "interval": 1, "snapshot": True}},
            {"method": "subscribe", "params": {"channel": "ohlc", "symbol": ws_symbols, "interval": 5, "snapshot": True}},
            {"method": "subscribe", "params": {"channel": "ohlc", "symbol": ws_symbols, "interval": 15, "snapshot": True}},
        ]
        for payload in subscriptions:
            await websocket.send(json.dumps(payload))

    @staticmethod
    def _stop(messages_seen: int, start: float, duration_seconds: int, max_messages: int | None, stop_file: Path | None = None) -> bool:
        if stop_file is not None and stop_file.exists():
            return True
        if max_messages is not None and messages_seen >= max_messages:
            return True
        if duration_seconds <= 0:
            return False
        return (time.monotonic() - start) >= duration_seconds


def run_live_scanner(
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    available_eur: float,
    duration_seconds: int,
    max_messages: int | None = None,
    bootstrap_dir: str | None = None,
    stop_file: str | None = None,
) -> LiveScanReport:
    scanner = KrakenLiveScanner(bot_config, execution_config, bootstrap_dir=bootstrap_dir, stop_file=stop_file)
    return asyncio.run(scanner.run(available_eur=available_eur, duration_seconds=duration_seconds, max_messages=max_messages))
