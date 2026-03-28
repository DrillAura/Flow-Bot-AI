from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .indicators import aggregate_candles, atr
from .models import Candle, MarketContext, OrderBookSnapshot
from .storage import load_interval_candles

MIN_1M_CANDLES = 20
MIN_5M_CANDLES = 30
MIN_15M_CANDLES = 60


@dataclass(frozen=True)
class LocalPairHistory:
    symbol: str
    candles_1m: tuple[Candle, ...]
    candles_15m: tuple[Candle, ...]
    candles_5m: tuple[Candle, ...] = field(init=False)
    times_5m: tuple[datetime, ...] = field(init=False, repr=False)
    times_15m: tuple[datetime, ...] = field(init=False, repr=False)
    atr_pct_15m: tuple[float | None, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        candles_5m = tuple(aggregate_candles(self.candles_1m, 5))
        atr_values = atr(self.candles_15m, 14)
        atr_pct_15m = tuple(
            (100.0 * value / candle.close) if value is not None and candle.close > 0 else None
            for value, candle in zip(atr_values, self.candles_15m)
        )
        object.__setattr__(self, "candles_5m", candles_5m)
        object.__setattr__(self, "times_5m", tuple(candle.ts for candle in candles_5m))
        object.__setattr__(self, "times_15m", tuple(candle.ts for candle in self.candles_15m))
        object.__setattr__(self, "atr_pct_15m", atr_pct_15m)

    def context_at(self, cursor: int, order_book: OrderBookSnapshot) -> MarketContext:
        series_1m = self.candles_1m[: cursor + 1]
        moment = series_1m[-1].ts
        cutoff_5m = bisect_right(self.times_5m, moment)
        cutoff_15m = bisect_right(self.times_15m, moment)
        series_5m = self.candles_5m[:cutoff_5m]
        series_15m = self.candles_15m[:cutoff_15m]
        atr_pct_history = [value for value in self.atr_pct_15m[:cutoff_15m] if value is not None]
        return MarketContext(
            symbol=self.symbol,
            candles_1m=series_1m[-60:],
            candles_5m=series_5m[-80:],
            candles_15m=series_15m[-120:],
            order_book=order_book,
            atr_pct_history_15m=atr_pct_history[-200:],
        )

    def window(self, start: datetime | None = None, end: datetime | None = None, warmup: timedelta = timedelta(0)) -> "LocalPairHistory":
        window_start = start - warmup if start is not None else None
        candles_1m = _slice_candles(self.candles_1m, window_start, end)
        candles_15m = _slice_candles(self.candles_15m, window_start, end)
        return LocalPairHistory(symbol=self.symbol, candles_1m=candles_1m, candles_15m=candles_15m)

    def bounds(self) -> tuple[datetime, datetime]:
        if not self.candles_1m:
            raise ValueError(f"history {self.symbol} has no 1m candles")
        return self.candles_1m[0].ts, self.candles_1m[-1].ts


def load_local_histories(data_dir: Path, symbols: list[str]) -> dict[str, LocalPairHistory]:
    histories: dict[str, LocalPairHistory] = {}
    for symbol in symbols:
        candles_1m = tuple(load_interval_candles(data_dir, symbol, 1))
        candles_15m = tuple(load_interval_candles(data_dir, symbol, 15))
        histories[symbol] = LocalPairHistory(symbol=symbol, candles_1m=candles_1m, candles_15m=candles_15m)
    return histories


def slice_histories_by_timerange(
    histories: dict[str, LocalPairHistory],
    start: datetime | None = None,
    end: datetime | None = None,
    warmup: timedelta = timedelta(0),
) -> dict[str, LocalPairHistory]:
    return {symbol: history.window(start=start, end=end, warmup=warmup) for symbol, history in histories.items()}


def history_bounds(histories: dict[str, LocalPairHistory]) -> tuple[datetime, datetime]:
    if not histories:
        raise ValueError("no histories supplied")
    starts: list[datetime] = []
    ends: list[datetime] = []
    for history in histories.values():
        start, end = history.bounds()
        starts.append(start)
        ends.append(end)
    return max(starts), min(ends)


def strategy_warmup_cursor() -> int:
    return max(MIN_1M_CANDLES, MIN_5M_CANDLES * 5) - 1


def _slice_candles(candles: tuple[Candle, ...], start: datetime | None, end: datetime | None) -> tuple[Candle, ...]:
    def _in_window(ts: datetime) -> bool:
        if start is not None and ts < start:
            return False
        if end is not None and ts >= end:
            return False
        return True

    return tuple(candle for candle in candles if _in_window(candle.ts))
