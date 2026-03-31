from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request

from .indicators import atr
from .models import Candle, MarketContext, OrderBookSnapshot, PriceSample
from .storage import load_csv_candles, merge_candles, write_csv_candles


TIMEFRAME_WINDOWS: tuple[tuple[str, float | None], ...] = (
    ("1S", 1.0 / 60.0),
    ("5S", 5.0 / 60.0),
    ("1H", 60),
    ("5H", 5 * 60),
    ("9H", 9 * 60),
    ("12H", 12 * 60),
    ("1D", 24 * 60),
    ("7D", 7 * 24 * 60),
    ("30D", 30 * 24 * 60),
    ("60D", 60 * 24 * 60),
    ("90D", 90 * 24 * 60),
    ("365D", 365 * 24 * 60),
    ("MAX", None),
)


@dataclass(frozen=True)
class KrakenPairMetadata:
    altname: str
    wsname: str
    ordermin: float
    costmin: float
    tick_size: float
    pair_decimals: int
    lot_decimals: int
    status: str


@dataclass
class KrakenOrderBook:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    timestamp: datetime | None = None

    def apply_message(self, payload: dict[str, Any]) -> None:
        for level in payload.get("bids", []):
            self._apply_level(self.bids, level)
        for level in payload.get("asks", []):
            self._apply_level(self.asks, level)
        timestamp = payload.get("timestamp")
        if timestamp:
            self.timestamp = parse_rfc3339(timestamp)

    def to_snapshot(self, depth: int = 5) -> OrderBookSnapshot | None:
        if not self.bids or not self.asks:
            return None
        bids = sorted(self.bids.items(), key=lambda item: item[0], reverse=True)[:depth]
        asks = sorted(self.asks.items(), key=lambda item: item[0])[:depth]
        return OrderBookSnapshot(
            symbol=self.symbol,
            best_bid=bids[0][0],
            best_ask=asks[0][0],
            bid_volume_top5=sum(qty for _, qty in bids),
            ask_volume_top5=sum(qty for _, qty in asks),
        )

    @staticmethod
    def _apply_level(side: dict[float, float], level: dict[str, Any]) -> None:
        price = float(level["price"])
        qty = float(level["qty"])
        if qty <= 0:
            side.pop(price, None)
            return
        side[price] = qty


class KrakenPublicClient:
    api_base = "https://api.kraken.com/0/public"

    def fetch_asset_pairs(self, pairs: list[str]) -> dict[str, KrakenPairMetadata]:
        query = parse.urlencode({"pair": ",".join(pairs)})
        with request.urlopen(f"{self.api_base}/AssetPairs?{query}", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        result: dict[str, KrakenPairMetadata] = {}
        for key, value in payload["result"].items():
            result[key] = KrakenPairMetadata(
                altname=value["altname"],
                wsname=value["wsname"],
                ordermin=float(value["ordermin"]),
                costmin=float(value["costmin"]),
                tick_size=float(value["tick_size"]),
                pair_decimals=int(value["pair_decimals"]),
                lot_decimals=int(value["lot_decimals"]),
                status=value["status"],
            )
        return result

    def fetch_ohlc(self, pair: str, interval: int = 1, since: int | None = None) -> tuple[list[Candle], int | None]:
        params: dict[str, Any] = {"pair": pair, "interval": interval}
        if since is not None:
            params["since"] = since
        query = parse.urlencode(params)
        with request.urlopen(f"{self.api_base}/OHLC?{query}", timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        result = payload["result"]
        series_key = self._result_series_key(result, pair)
        rows = result.get(series_key, [])
        candles = self.parse_ohlc_rows(rows)
        last = result.get("last")
        return candles, int(last) if last is not None else None

    def fetch_ticker(self, pair: str) -> dict[str, float]:
        query = parse.urlencode({"pair": pair})
        with request.urlopen(f"{self.api_base}/Ticker?{query}", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        result = payload.get("result") or {}
        if not result:
            raise RuntimeError(f"Kraken ticker response for {pair} did not contain market data")
        ticker = next(iter(result.values()))
        ask = float(ticker["a"][0])
        bid = float(ticker["b"][0])
        last = float(ticker["c"][0])
        open_price = float(ticker["o"])
        volume_24h = float(ticker["v"][1])
        high_24h = float(ticker["h"][1])
        low_24h = float(ticker["l"][1])
        trades_24h = float(ticker["t"][1])
        vwap_24h = float(ticker["p"][1])
        return {
            "ask": ask,
            "bid": bid,
            "last": last,
            "open": open_price,
            "volume_24h": volume_24h,
            "high_24h": high_24h,
            "low_24h": low_24h,
            "trades_24h": trades_24h,
            "vwap_24h": vwap_24h,
        }

    @staticmethod
    def build_timeframe_profiles(
        candles_1m: list[Candle],
        *,
        live_price: float | None = None,
        live_ts: datetime | None = None,
        micro_samples: list[PriceSample] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not candles_1m:
            return {}

        end_ts = candles_1m[-1].ts
        anchor_price = live_price if live_price is not None else candles_1m[-1].close
        now = live_ts or datetime.now(timezone.utc)
        profiles: dict[str, dict[str, Any]] = {}
        for label, minutes in TIMEFRAME_WINDOWS:
            if minutes is not None and minutes < 1.0 and micro_samples:
                profile = _profile_from_price_samples(
                    label=label,
                    minutes=minutes,
                    samples=micro_samples,
                    fallback_price=anchor_price,
                    now=now,
                )
                if profile is not None:
                    profiles[label] = profile
                    continue
            if minutes is None:
                window = list(candles_1m)
                coverage_pct = 1.0
                available_minutes = max((window[-1].ts - window[0].ts).total_seconds() / 60.0, 0.0)
            else:
                cutoff = end_ts - timedelta(minutes=minutes)
                window = [candle for candle in candles_1m if candle.ts >= cutoff]
                if not window:
                    window = [candles_1m[-1]]
                available_minutes = max((window[-1].ts - window[0].ts).total_seconds() / 60.0, 0.0)
                coverage_pct = min(available_minutes / minutes, 1.0) if minutes > 0 else 1.0

            first = window[0]
            last = window[-1]
            open_price = first.open if first.open > 0 else first.close
            close_price = anchor_price if live_price is not None else last.close
            high = max(candle.high for candle in window)
            low = min(candle.low for candle in window)
            volume = sum(candle.volume for candle in window)
            change_pct = ((close_price - open_price) / open_price * 100.0) if open_price > 0 else None
            range_pct = ((high - low) / close_price * 100.0) if close_price > 0 else None
            trend_per_hour = (change_pct / max(available_minutes / 60.0, 1e-9)) if change_pct is not None else None
            series = _compress_series([candle.close for candle in window], target_points=48)
            if live_price is not None and (not series or series[-1] != live_price):
                series = series + [live_price]
            profiles[label] = {
                "label": label,
                "minutes": minutes,
                "available_minutes": round(available_minutes, 2),
                "coverage_pct": round(coverage_pct, 4),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "change_pct": change_pct,
                "range_pct": range_pct,
                "volume": volume,
                "first_ts": first.ts.isoformat(),
                "last_ts": last.ts.isoformat(),
                "freshness_seconds": max((now - last.ts.astimezone(timezone.utc)).total_seconds(), 0.0),
                "trend_per_hour": trend_per_hour,
                "series": series,
                "available": minutes is None or coverage_pct >= 0.995,
            }
        return profiles

    def write_ohlc_csv(self, pair: str, interval: int, output_path: Path, since: int | None = None) -> int | None:
        candles, last = self.fetch_ohlc(pair, interval=interval, since=since)
        if not candles:
            return last
        write_csv_candles(output_path, candles)
        return last

    def sync_ohlc_csv(self, pair: str, interval: int, output_path: Path) -> dict[str, int | str]:
        candles, last = self.fetch_ohlc(pair, interval=interval)
        existing: list[Candle] = []
        repaired = False
        if output_path.exists():
            try:
                existing = load_csv_candles(output_path)
            except Exception:
                existing = []
                repaired = True

        if not candles and existing:
            return {
                "pair": pair,
                "interval": interval,
                "existing_rows": len(existing),
                "fetched_rows": 0,
                "merged_rows": len(existing),
                "last": last or 0,
                "repaired": int(repaired),
                "written_rows": 0,
                "status": "skipped_empty_fetch",
            }

        merged = merge_candles(existing, candles)
        if merged:
            write_csv_candles(output_path, merged)
        return {
            "pair": pair,
            "interval": interval,
            "existing_rows": len(existing),
            "fetched_rows": len(candles),
            "merged_rows": len(merged),
            "last": last or 0,
            "repaired": int(repaired),
            "written_rows": len(merged) if merged else 0,
            "status": "written" if merged else "skipped_empty_fetch",
        }

    @staticmethod
    def parse_ohlc_rows(rows: list[list[Any]]) -> list[Candle]:
        candles: list[Candle] = []
        for row in rows:
            ts = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
            candles.append(
                Candle(
                    ts=ts,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[6]),
                )
            )
        return candles

    @staticmethod
    def _result_series_key(result: dict[str, Any], pair: str) -> str:
        non_meta_keys = [key for key in result.keys() if key != "last"]
        if not non_meta_keys:
            raise RuntimeError(f"Kraken OHLC response for {pair} did not contain candle data")
        for key in non_meta_keys:
            if key == pair or key.replace("X", "").replace("Z", "") == pair:
                return key
        return non_meta_keys[0]

    @staticmethod
    def synthetic_order_book(symbol: str, close_price: float, spread_bps: float = 6.0) -> OrderBookSnapshot:
        half_spread = (spread_bps / 10_000.0) * close_price / 2.0
        return OrderBookSnapshot(
            symbol=symbol,
            best_bid=close_price - half_spread,
            best_ask=close_price + half_spread,
            bid_volume_top5=5_000.0,
            ask_volume_top5=4_000.0,
        )


class KrakenMarketStore:
    def __init__(self, pair_metadata_by_symbol: dict[str, KrakenPairMetadata]) -> None:
        self.pair_metadata_by_symbol = pair_metadata_by_symbol
        self.ws_to_symbol = {metadata.wsname: metadata.altname for metadata in pair_metadata_by_symbol.values()}
        self.books = {symbol: KrakenOrderBook(symbol=symbol) for symbol in pair_metadata_by_symbol.keys()}
        self.candles: dict[str, dict[int, list[Candle]]] = {symbol: {1: [], 5: [], 15: []} for symbol in pair_metadata_by_symbol.keys()}
        self.micro_samples: dict[str, list[PriceSample]] = {symbol: [] for symbol in pair_metadata_by_symbol.keys()}

    def websocket_symbols(self) -> list[str]:
        return [metadata.wsname for metadata in self.pair_metadata_by_symbol.values()]

    def seed_history(self, symbol: str, interval: int, candles: list[Candle], maxlen: int = 500) -> None:
        self.candles[symbol][interval] = candles[-maxlen:]

    def apply_ws_message(self, message: dict[str, Any]) -> set[str]:
        channel = message.get("channel")
        data = message.get("data")
        if not channel or not data:
            return set()
        updated: set[str] = set()

        if channel == "book":
            payload = data[0] if isinstance(data, list) else data
            symbol = self._symbol_from_ws(payload["symbol"])
            self.books[symbol].apply_message(payload)
            updated.add(symbol)
        elif channel == "ohlc":
            for payload in data if isinstance(data, list) else [data]:
                symbol = self._symbol_from_ws(payload["symbol"])
                interval = int(payload["interval"])
                candle = ws_ohlc_to_candle(payload)
                self._upsert_candle(symbol, interval, candle)
                updated.add(symbol)
        elif channel == "ticker":
            payload = data[0] if isinstance(data, list) else data
            symbol = self._symbol_from_ws(payload["symbol"])
            snapshot = self.books[symbol]
            snapshot.apply_message(
                {
                    "bids": [{"price": payload["bid"], "qty": payload.get("bid_qty", 0)}],
                    "asks": [{"price": payload["ask"], "qty": payload.get("ask_qty", 0)}],
                    "timestamp": payload.get("timestamp"),
                }
            )
            self._record_micro_sample(
                symbol,
                price=float(payload.get("last", payload.get("bid", payload.get("ask", 0.0))) or 0.0),
                bid=float(payload.get("bid", 0.0) or 0.0),
                ask=float(payload.get("ask", 0.0) or 0.0),
                timestamp=payload.get("timestamp"),
            )
            updated.add(symbol)
        return updated

    def build_contexts(self) -> list[MarketContext]:
        contexts: list[MarketContext] = []
        for symbol in self.pair_metadata_by_symbol.keys():
            book = self.books[symbol].to_snapshot()
            candles_1m = self.candles[symbol][1]
            candles_5m = self.candles[symbol][5]
            candles_15m = self.candles[symbol][15]
            if book is None or len(candles_1m) < 20 or len(candles_5m) < 30 or len(candles_15m) < 60:
                continue
            atr_values = atr(candles_15m, 14)
            atr_history = [100.0 * value / candle.close for value, candle in zip(atr_values, candles_15m) if value is not None and candle.close > 0]
            micro_samples = list(self.micro_samples[symbol][-900:])
            analysis_windows = KrakenPublicClient.build_timeframe_profiles(
                candles_1m[-500:],
                live_price=(micro_samples[-1].price if micro_samples else None),
                live_ts=(micro_samples[-1].ts if micro_samples else None),
                micro_samples=micro_samples,
            )
            contexts.append(
                MarketContext(
                    symbol=symbol,
                    candles_1m=candles_1m[-60:],
                    candles_5m=candles_5m[-80:],
                    candles_15m=candles_15m[-120:],
                    order_book=book,
                    atr_pct_history_15m=atr_history[-200:],
                    micro_samples=micro_samples,
                    analysis_windows=analysis_windows,
                )
            )
        return contexts

    def _upsert_candle(self, symbol: str, interval: int, candle: Candle, maxlen: int = 500) -> None:
        bucket = self.candles[symbol][interval]
        if bucket and bucket[-1].ts == candle.ts:
            bucket[-1] = candle
        else:
            bucket.append(candle)
        if len(bucket) > maxlen:
            del bucket[:-maxlen]

    def _symbol_from_ws(self, ws_symbol: str) -> str:
        if ws_symbol not in self.ws_to_symbol:
            raise KeyError(f"Unsupported websocket symbol: {ws_symbol}")
        return self.ws_to_symbol[ws_symbol]

    def _record_micro_sample(
        self,
        symbol: str,
        *,
        price: float,
        bid: float,
        ask: float,
        timestamp: str | None,
        maxlen: int = 7200,
    ) -> None:
        if price <= 0:
            return
        sample_ts = parse_rfc3339(timestamp) if timestamp else datetime.now(timezone.utc)
        bucket = self.micro_samples[symbol]
        sample = PriceSample(ts=sample_ts, price=price, bid=bid or None, ask=ask or None)
        if bucket and bucket[-1].ts == sample.ts:
            bucket[-1] = sample
        else:
            bucket.append(sample)
        if len(bucket) > maxlen:
            del bucket[:-maxlen]


def parse_rfc3339(value: str) -> datetime:
    cleaned = value.replace("Z", "+00:00")
    if "." in cleaned:
        date_part, rest = cleaned.split(".", 1)
        fraction, offset = rest.split("+", 1) if "+" in rest else rest.split("-", 1)
        sign = "+" if "+" in rest else "-"
        fraction = fraction[:6].ljust(6, "0")
        cleaned = f"{date_part}.{fraction}{sign}{offset}"
    return datetime.fromisoformat(cleaned)


def ws_ohlc_to_candle(payload: dict[str, Any]) -> Candle:
    interval_begin = payload.get("interval_begin") or payload.get("timestamp")
    return Candle(
        ts=parse_rfc3339(interval_begin),
        open=float(payload["open"]),
        high=float(payload["high"]),
        low=float(payload["low"]),
        close=float(payload["close"]),
        volume=float(payload["volume"]),
    )


def _profile_from_price_samples(
    *,
    label: str,
    minutes: float,
    samples: list[PriceSample],
    fallback_price: float,
    now: datetime,
) -> dict[str, Any] | None:
    if not samples:
        return None
    seconds = max(minutes * 60.0, 1.0)
    cutoff = now.astimezone(timezone.utc) - timedelta(seconds=seconds)
    window = [sample for sample in samples if sample.ts >= cutoff]
    if not window:
        window = [samples[-1]]
    available_minutes = max((window[-1].ts - window[0].ts).total_seconds() / 60.0, 0.0)
    coverage_pct = min((available_minutes * 60.0) / seconds, 1.0) if seconds > 0 else 1.0
    open_price = window[0].price
    close_price = window[-1].price or fallback_price
    high = max(sample.price for sample in window)
    low = min(sample.price for sample in window)
    change_pct = ((close_price - open_price) / open_price * 100.0) if open_price > 0 else None
    range_pct = ((high - low) / close_price * 100.0) if close_price > 0 else None
    trend_per_hour = (change_pct / max(available_minutes / 60.0, 1e-9)) if change_pct is not None else None
    return {
        "label": label,
        "minutes": minutes,
        "available_minutes": round(available_minutes, 4),
        "coverage_pct": round(coverage_pct, 4),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close_price,
        "change_pct": change_pct,
        "range_pct": range_pct,
        "volume": None,
        "first_ts": window[0].ts.isoformat(),
        "last_ts": window[-1].ts.isoformat(),
        "freshness_seconds": max((now.astimezone(timezone.utc) - window[-1].ts.astimezone(timezone.utc)).total_seconds(), 0.0),
        "trend_per_hour": trend_per_hour,
        "series": _compress_series([sample.price for sample in window], target_points=48),
        "available": coverage_pct >= 0.70,
    }


def _compress_series(values: list[float], target_points: int = 48) -> list[float]:
    if not values:
        return []
    if len(values) <= target_points:
        return [round(value, 6) for value in values]
    if target_points <= 1:
        return [round(values[-1], 6)]
    last_index = len(values) - 1
    step = last_index / float(target_points - 1)
    return [round(values[round(index * step)], 6) for index in range(target_points)]
