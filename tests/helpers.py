from __future__ import annotations

from datetime import datetime, timedelta, timezone

from daytrading_bot.config import BotConfig
from daytrading_bot.indicators import atr, last_value
from daytrading_bot.models import Candle, MarketContext, OrderBookSnapshot, PriceSample


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


def build_fast_micro_context(symbol: str = "XBTEUR") -> MarketContext:
    context = build_context(symbol)
    base_ts = context.candles_1m[-1].ts
    latest = context.candles_1m[-1].close
    micro_samples = [
        PriceSample(ts=base_ts - timedelta(seconds=7), price=latest - 0.16, bid=latest - 0.18, ask=latest - 0.14),
        PriceSample(ts=base_ts - timedelta(seconds=6), price=latest - 0.13, bid=latest - 0.15, ask=latest - 0.11),
        PriceSample(ts=base_ts - timedelta(seconds=5), price=latest - 0.10, bid=latest - 0.12, ask=latest - 0.08),
        PriceSample(ts=base_ts - timedelta(seconds=4), price=latest - 0.07, bid=latest - 0.09, ask=latest - 0.05),
        PriceSample(ts=base_ts - timedelta(seconds=3), price=latest - 0.04, bid=latest - 0.06, ask=latest - 0.02),
        PriceSample(ts=base_ts - timedelta(seconds=2), price=latest + 0.01, bid=latest - 0.01, ask=latest + 0.03),
        PriceSample(ts=base_ts - timedelta(seconds=1), price=latest + 0.05, bid=latest + 0.03, ask=latest + 0.07),
        PriceSample(ts=base_ts, price=latest + 0.08, bid=latest + 0.06, ask=latest + 0.10),
    ]
    return MarketContext(
        symbol=context.symbol,
        candles_1m=context.candles_1m,
        candles_5m=context.candles_5m,
        candles_15m=context.candles_15m,
        order_book=context.order_book,
        atr_pct_history_15m=context.atr_pct_history_15m,
        micro_samples=micro_samples,
        analysis_windows={
            "1S": {"available": True, "change_pct": 0.020, "range_pct": 0.010},
            "5S": {"available": True, "change_pct": 0.045, "range_pct": 0.020},
        },
    )


def build_fast_sweep_context(symbol: str = "XBTEUR") -> MarketContext:
    context = build_fast_micro_context(symbol)
    candles_1m = list(context.candles_1m)
    base_ts = candles_1m[-1].ts
    base_price = candles_1m[-1].close
    sweep_sequence = [
        Candle(ts=base_ts - timedelta(minutes=5), open=base_price - 0.10, high=base_price + 0.06, low=base_price - 0.22, close=base_price - 0.02, volume=52.0),
        Candle(ts=base_ts - timedelta(minutes=4), open=base_price - 0.08, high=base_price + 0.08, low=base_price - 0.25, close=base_price + 0.01, volume=54.0),
        Candle(ts=base_ts - timedelta(minutes=3), open=base_price - 0.04, high=base_price + 0.10, low=base_price - 0.20, close=base_price + 0.03, volume=57.0),
        Candle(ts=base_ts - timedelta(minutes=2), open=base_price - 0.02, high=base_price + 0.12, low=base_price - 3.20, close=base_price - 0.12, volume=70.0),
        Candle(ts=base_ts - timedelta(minutes=1), open=base_price - 0.05, high=base_price + 0.30, low=base_price - 0.08, close=base_price + 0.18, volume=88.0),
        Candle(ts=base_ts, open=base_price + 0.10, high=base_price + 0.44, low=base_price + 0.02, close=base_price + 0.30, volume=94.0),
    ]
    candles_1m[-6:] = sweep_sequence
    order_book = OrderBookSnapshot(
        symbol=symbol,
        best_bid=sweep_sequence[-1].close - 0.02,
        best_ask=sweep_sequence[-1].close + 0.02,
        bid_volume_top5=5_900.0,
        ask_volume_top5=4_350.0,
    )
    return MarketContext(
        symbol=context.symbol,
        candles_1m=candles_1m,
        candles_5m=context.candles_5m,
        candles_15m=context.candles_15m,
        order_book=order_book,
        atr_pct_history_15m=context.atr_pct_history_15m,
        micro_samples=context.micro_samples,
        analysis_windows={
            "1S": {"available": True, "change_pct": 0.023, "range_pct": 0.010},
            "5S": {"available": True, "change_pct": 0.049, "range_pct": 0.021},
        },
    )


def build_fast_vwap_context(symbol: str = "XBTEUR") -> MarketContext:
    context = build_fast_micro_context(symbol)
    candles_1m = list(context.candles_1m)
    base_ts = candles_1m[-1].ts
    anchor = candles_1m[-24].close
    custom_segment: list[Candle] = []
    for index in range(20):
        close = anchor + (0.01 * (index % 3))
        volume = 46.0 + (index % 4)
        custom_segment.append(
            Candle(
                ts=base_ts - timedelta(minutes=23 - index),
                open=close - 0.03,
                high=close + 0.10,
                low=close - 0.10,
                close=close,
                volume=volume,
            )
        )
    dip_reference = anchor + 0.02
    custom_segment[-3] = Candle(ts=base_ts - timedelta(minutes=2), open=dip_reference + 0.03, high=dip_reference + 0.08, low=dip_reference - 0.18, close=dip_reference - 0.10, volume=54.0)
    custom_segment[-2] = Candle(ts=base_ts - timedelta(minutes=1), open=dip_reference - 0.04, high=dip_reference + 0.05, low=dip_reference - 0.12, close=dip_reference - 0.02, volume=56.0)
    custom_segment[-1] = Candle(ts=base_ts, open=dip_reference + 0.01, high=dip_reference + 0.36, low=dip_reference - 0.03, close=dip_reference + 0.28, volume=120.0)
    candles_1m[-20:] = custom_segment[-20:]
    order_book = OrderBookSnapshot(
        symbol=symbol,
        best_bid=custom_segment[-1].close - 0.02,
        best_ask=custom_segment[-1].close + 0.02,
        bid_volume_top5=5_750.0,
        ask_volume_top5=4_500.0,
    )
    micro_samples = [
        PriceSample(ts=base_ts - timedelta(seconds=7), price=custom_segment[-1].close - 0.12, bid=custom_segment[-1].close - 0.14, ask=custom_segment[-1].close - 0.10),
        PriceSample(ts=base_ts - timedelta(seconds=6), price=custom_segment[-1].close - 0.10, bid=custom_segment[-1].close - 0.12, ask=custom_segment[-1].close - 0.08),
        PriceSample(ts=base_ts - timedelta(seconds=5), price=custom_segment[-1].close - 0.07, bid=custom_segment[-1].close - 0.09, ask=custom_segment[-1].close - 0.05),
        PriceSample(ts=base_ts - timedelta(seconds=4), price=custom_segment[-1].close - 0.04, bid=custom_segment[-1].close - 0.06, ask=custom_segment[-1].close - 0.02),
        PriceSample(ts=base_ts - timedelta(seconds=3), price=custom_segment[-1].close - 0.01, bid=custom_segment[-1].close - 0.03, ask=custom_segment[-1].close + 0.01),
        PriceSample(ts=base_ts - timedelta(seconds=2), price=custom_segment[-1].close + 0.02, bid=custom_segment[-1].close, ask=custom_segment[-1].close + 0.04),
        PriceSample(ts=base_ts - timedelta(seconds=1), price=custom_segment[-1].close + 0.05, bid=custom_segment[-1].close + 0.03, ask=custom_segment[-1].close + 0.07),
        PriceSample(ts=base_ts, price=custom_segment[-1].close + 0.08, bid=custom_segment[-1].close + 0.06, ask=custom_segment[-1].close + 0.10),
    ]
    return MarketContext(
        symbol=context.symbol,
        candles_1m=candles_1m,
        candles_5m=context.candles_5m,
        candles_15m=context.candles_15m,
        order_book=order_book,
        atr_pct_history_15m=context.atr_pct_history_15m,
        micro_samples=micro_samples,
        analysis_windows={
            "1S": {"available": True, "change_pct": 0.019, "range_pct": 0.009},
            "5S": {"available": True, "change_pct": 0.042, "range_pct": 0.018},
        },
    )
