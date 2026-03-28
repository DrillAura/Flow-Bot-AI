from __future__ import annotations

from datetime import datetime, timedelta, timezone

from daytrading_bot.config import BotConfig
from daytrading_bot.indicators import atr, last_value
from daytrading_bot.models import Candle, MarketContext, OrderBookSnapshot


def make_candle(ts: datetime, close: float, volume: float = 100.0, high_offset: float = 0.35, low_offset: float = 0.25) -> Candle:
    return Candle(
        ts=ts,
        open=close - 0.10,
        high=close + high_offset,
        low=close - low_offset,
        close=close,
        volume=volume,
    )


def build_context(symbol: str = "XBTEUR") -> MarketContext:
    start = datetime(2026, 3, 23, 7, 0, tzinfo=timezone.utc)

    candles_15m: list[Candle] = []
    price = 100.0
    for index in range(80):
        price += 0.35
        candles_15m.append(make_candle(start + timedelta(minutes=15 * index), price, volume=150.0, high_offset=0.55, low_offset=0.30))

    candles_5m: list[Candle] = []
    price = 110.0
    for index in range(37):
        price += 0.18
        candles_5m.append(make_candle(start + timedelta(minutes=5 * index), price, volume=100.0, high_offset=0.28, low_offset=0.20))

    breakout_level = max(c.high for c in candles_5m[-20:])
    breakout_close = breakout_level + 0.90
    candles_5m.append(
        Candle(
            ts=start + timedelta(minutes=5 * 37),
            open=breakout_level + 0.10,
            high=breakout_close + 0.25,
            low=breakout_level + 0.05,
            close=breakout_close,
            volume=450.0,
        )
    )
    candles_5m.append(
        Candle(
            ts=start + timedelta(minutes=5 * 38),
            open=breakout_close - 0.20,
            high=breakout_close + 0.10,
            low=breakout_level + 0.08,
            close=breakout_level + 0.20,
            volume=220.0,
        )
    )
    candles_5m.append(
        Candle(
            ts=start + timedelta(minutes=5 * 39),
            open=breakout_level + 0.28,
            high=breakout_level + 1.40,
            low=breakout_level + 0.10,
            close=breakout_level + 1.05,
            volume=360.0,
        )
    )

    candles_1m: list[Candle] = []
    price = candles_5m[-12].close
    for index in range(1300):
        price += 0.05
        candles_1m.append(make_candle(start + timedelta(minutes=index), price, volume=50.0, high_offset=0.10, low_offset=0.08))

    order_book = OrderBookSnapshot(
        symbol=symbol,
        best_bid=candles_5m[-1].close - 0.02,
        best_ask=candles_5m[-1].close + 0.02,
        bid_volume_top5=5_500.0,
        ask_volume_top5=4_500.0,
    )

    atr_values = atr(candles_15m, 14)
    atr_current = last_value(atr_values) or 0.5
    atr_pct_current = 100.0 * atr_current / candles_15m[-1].close
    atr_history = [atr_pct_current * ratio for ratio in [0.40 + (i * 0.01) for i in range(80)]]

    return MarketContext(
        symbol=symbol,
        candles_1m=candles_1m,
        candles_5m=candles_5m,
        candles_15m=candles_15m,
        order_book=order_book,
        atr_pct_history_15m=atr_history,
    )


def build_recovery_context(symbol: str = "XBTEUR") -> MarketContext:
    start = datetime(2026, 3, 23, 7, 0, tzinfo=timezone.utc)

    candles_15m: list[Candle] = []
    price = 125.0
    for index in range(60):
        price -= 0.28
        candles_15m.append(make_candle(start + timedelta(minutes=15 * index), price, volume=130.0, high_offset=0.45, low_offset=0.28))
    for index in range(20):
        price += 0.42
        candles_15m.append(make_candle(start + timedelta(minutes=15 * (60 + index)), price, volume=150.0, high_offset=0.48, low_offset=0.26))

    candles_5m: list[Candle] = []
    price = 112.0
    for index in range(37):
        price += 0.08
        candles_5m.append(make_candle(start + timedelta(minutes=5 * index), price, volume=90.0, high_offset=0.20, low_offset=0.18))

    candles_5m.append(
        Candle(
            ts=start + timedelta(minutes=5 * 37),
            open=price + 0.02,
            high=price + 0.18,
            low=price - 0.20,
            close=price - 0.03,
            volume=88.0,
        )
    )
    candles_5m.append(
        Candle(
            ts=start + timedelta(minutes=5 * 38),
            open=price + 0.01,
            high=price + 0.24,
            low=price - 0.10,
            close=price + 0.10,
            volume=87.0,
        )
    )
    candles_5m.append(
        Candle(
            ts=start + timedelta(minutes=5 * 39),
            open=price + 0.11,
            high=price + 0.58,
            low=price + 0.01,
            close=price + 0.42,
            volume=92.0,
        )
    )

    candles_1m: list[Candle] = []
    price = candles_5m[-12].close
    for index in range(1300):
        price += 0.03
        candles_1m.append(make_candle(start + timedelta(minutes=index), price, volume=48.0, high_offset=0.08, low_offset=0.07))

    order_book = OrderBookSnapshot(
        symbol=symbol,
        best_bid=candles_5m[-1].close - 0.02,
        best_ask=candles_5m[-1].close + 0.02,
        bid_volume_top5=5_400.0,
        ask_volume_top5=4_500.0,
    )

    atr_values = atr(candles_15m, 14)
    atr_current = last_value(atr_values) or 0.5
    atr_pct_current = 100.0 * atr_current / candles_15m[-1].close
    atr_history = [atr_pct_current * ratio for ratio in [0.55 + (i * 0.006) for i in range(80)]]

    return MarketContext(
        symbol=symbol,
        candles_1m=candles_1m,
        candles_5m=candles_5m,
        candles_15m=candles_15m,
        order_book=order_book,
        atr_pct_history_15m=atr_history,
    )


def build_default_universe_contexts(bot_config: BotConfig | None = None) -> dict[str, MarketContext]:
    config = bot_config or BotConfig()
    contexts: dict[str, MarketContext] = {}
    builders = (build_context, build_recovery_context)
    for index, pair in enumerate(config.pairs):
        builder = builders[index % len(builders)]
        contexts[pair.symbol] = builder(pair.symbol)
    return contexts
