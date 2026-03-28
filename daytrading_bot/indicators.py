from __future__ import annotations

from statistics import mean, pstdev
from typing import Sequence

from .models import Candle


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return [None] * len(values)

    multiplier = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    result: list[float | None] = [None] * (period - 1) + [seed]
    previous = seed
    for value in values[period:]:
        previous = ((value - previous) * multiplier) + previous
        result.append(previous)
    return result


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    if not candles:
        return []
    ranges = [candles[0].high - candles[0].low]
    for previous, candle in zip(candles, candles[1:]):
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous.close),
                abs(candle.low - previous.close),
            )
        )
    return ranges


def wilder_smoothing(values: Sequence[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)
    initial = sum(values[:period]) / period
    output: list[float | None] = [None] * (period - 1) + [initial]
    previous = initial
    for value in values[period:]:
        previous = ((previous * (period - 1)) + value) / period
        output.append(previous)
    return output


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    return wilder_smoothing(true_ranges(candles), period)


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    if len(values) < period + 1:
        return [None] * len(values)

    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values, values[1:]):
        delta = current - previous
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result: list[float | None] = [None] * period

    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(100.0 - (100.0 / (1.0 + rs)))

    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100.0 - (100.0 / (1.0 + rs)))
    return result


def adx(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    if len(candles) < (period * 2):
        return [None] * len(candles)

    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for previous, current in zip(candles, candles[1:]):
        up_move = current.high - previous.high
        down_move = previous.low - current.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    tr = true_ranges(candles)
    atr_values = wilder_smoothing(tr, period)
    plus_smoothed = wilder_smoothing(plus_dm, period)
    minus_smoothed = wilder_smoothing(minus_dm, period)

    dx_values: list[float | None] = [None] * len(candles)
    for index in range(len(candles)):
        if (
            atr_values[index] is None
            or plus_smoothed[index] is None
            or minus_smoothed[index] is None
            or atr_values[index] == 0
        ):
            continue
        plus_di = 100.0 * plus_smoothed[index] / atr_values[index]
        minus_di = 100.0 * minus_smoothed[index] / atr_values[index]
        denominator = plus_di + minus_di
        if denominator == 0:
            dx_values[index] = 0.0
        else:
            dx_values[index] = 100.0 * abs(plus_di - minus_di) / denominator

    concrete_dx = [value for value in dx_values if value is not None]
    if len(concrete_dx) < period:
        return [None] * len(candles)

    seed = sum(concrete_dx[:period]) / period
    result: list[float | None] = [None] * (len(dx_values) - len(concrete_dx)) + [seed]
    previous = seed
    for value in concrete_dx[period:]:
        previous = ((previous * (period - 1)) + value) / period
        result.append(previous)

    while len(result) < len(candles):
        result.insert(0, None)
    return result


def rolling_zscore(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 1:
        raise ValueError("period must be > 1")
    output: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < period:
            output.append(None)
            continue
        window = values[index + 1 - period : index + 1]
        sigma = pstdev(window)
        if sigma == 0:
            output.append(0.0)
            continue
        output.append((values[index] - mean(window)) / sigma)
    return output


def percentile_rank(sample: Sequence[float], value: float) -> float:
    filtered = sorted(x for x in sample if x is not None)
    if not filtered:
        return 0.0
    count = sum(1 for x in filtered if x <= value)
    return 100.0 * count / len(filtered)


def rolling_high(candles: Sequence[Candle], window: int, end_index: int) -> float:
    if end_index < window:
        raise ValueError("not enough candles")
    segment = candles[end_index - window : end_index]
    return max(candle.high for candle in segment)


def rolling_vwap(candles: Sequence[Candle], window: int) -> float:
    segment = candles[-window:] if len(candles) >= window else candles
    total_volume = sum(candle.volume for candle in segment)
    if total_volume == 0:
        return segment[-1].close if segment else 0.0
    total_notional = sum(candle.close * candle.volume for candle in segment)
    return total_notional / total_volume


def last_value(values: Sequence[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def is_rising(values: Sequence[float | None], lookback: int = 3) -> bool:
    concrete = [value for value in values if value is not None]
    if len(concrete) < lookback:
        return False
    recent = concrete[-lookback:]
    return all(left < right for left, right in zip(recent, recent[1:]))


def aggregate_candles(candles: Sequence[Candle], minutes: int) -> list[Candle]:
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    if not candles:
        return []

    aggregated: list[Candle] = []
    bucket: list[Candle] = []
    current_bucket = None
    for candle in candles:
        minute = candle.ts.minute - (candle.ts.minute % minutes)
        bucket_key = candle.ts.replace(minute=minute, second=0, microsecond=0)
        if current_bucket is None:
            current_bucket = bucket_key
        if bucket_key != current_bucket:
            aggregated.append(_collapse_bucket(bucket))
            bucket = []
            current_bucket = bucket_key
        bucket.append(candle)
    if bucket:
        aggregated.append(_collapse_bucket(bucket))
    return aggregated


def _collapse_bucket(bucket: Sequence[Candle]) -> Candle:
    first = bucket[0]
    last = bucket[-1]
    return Candle(
        ts=first.ts,
        open=first.open,
        high=max(c.high for c in bucket),
        low=min(c.low for c in bucket),
        close=last.close,
        volume=sum(c.volume for c in bucket),
    )
