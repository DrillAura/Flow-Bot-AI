from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .config import BotConfig
from .indicators import adx, atr, ema, is_rising, last_value, percentile_rank, rolling_high, rolling_vwap, rolling_zscore, rsi
from .models import DayTradeIntent, MarketContext, VolatilitySnapshot


@dataclass(frozen=True)
class StrategyCheck:
    name: str
    passed: bool
    threshold: str
    value: float | int | str | bool | None = None
    reason: str | None = None


@dataclass(frozen=True)
class StrategyEvaluation:
    intent: DayTradeIntent | None
    snapshot: VolatilitySnapshot | None
    rejection_reasons: tuple[str, ...]
    checks: tuple[StrategyCheck, ...]


@dataclass(frozen=True)
class RegimeAssessment:
    label: str
    ema_gap_pct: float
    price_15m: float


@dataclass(frozen=True)
class SetupCandidate:
    intent: DayTradeIntent | None
    checks: tuple[StrategyCheck, ...]


def _session_candles_5m(context: MarketContext, config: BotConfig) -> list:
    latest_ts = context.candles_5m[-1].ts
    latest_local = latest_ts.astimezone(config.timezone)
    for window in config.trade_windows:
        start_local = datetime.combine(latest_local.date(), window.start, tzinfo=config.timezone)
        end_local = datetime.combine(latest_local.date(), window.end, tzinfo=config.timezone)
        if start_local <= latest_local <= end_local:
            start_ts = start_local.astimezone(latest_ts.tzinfo)
            return [candle for candle in context.candles_5m if candle.ts >= start_ts]
    return []


def _window_change_bps(window: dict[str, float | int | str | bool | None]) -> float:
    return float(window.get("change_pct") or 0.0) * 100.0


def _window_range_bps(window: dict[str, float | int | str | bool | None]) -> float:
    return float(window.get("range_pct") or 0.0) * 100.0


class TradingStrategy(Protocol):
    strategy_id: str
    strategy_family: str

    def evaluate(self, context: MarketContext) -> DayTradeIntent | None: ...

    def evaluate_detailed(self, context: MarketContext) -> StrategyEvaluation: ...


class BreakoutPullbackStrategy:
    def __init__(
        self,
        config: BotConfig,
        *,
        strategy_id: str = "champion_breakout",
        strategy_family: str = "breakout_recovery",
    ) -> None:
        self.config = config
        self.strategy_id = strategy_id
        self.strategy_family = strategy_family

    def evaluate(self, context: MarketContext) -> DayTradeIntent | None:
        return self.evaluate_detailed(context).intent

    def evaluate_detailed(self, context: MarketContext) -> StrategyEvaluation:
        checks: list[StrategyCheck] = []
        rejection_reasons = self._history_rejections(context, checks)
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=None, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        snapshot = self._build_snapshot(context)
        regime = self._assess_regime(context, snapshot)
        rejection_reasons.extend(self._market_rejections(context, snapshot, regime, checks))
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        candidates: list[SetupCandidate] = []
        breakout_candidate = self._evaluate_breakout_candidate(context, snapshot, regime)
        checks.extend(breakout_candidate.checks)
        if breakout_candidate.intent is not None:
            candidates.append(breakout_candidate)

        recovery_candidate = self._evaluate_recovery_candidate(context, snapshot, regime)
        checks.extend(recovery_candidate.checks)
        if recovery_candidate.intent is not None:
            candidates.append(recovery_candidate)

        if not candidates:
            rejection_reasons.extend(
                check.reason
                for check in checks
                if not check.passed and check.reason is not None and check.reason not in rejection_reasons
            )
            if not rejection_reasons:
                rejection_reasons.append("no_pattern_match")
            return StrategyEvaluation(
                intent=None,
                snapshot=snapshot,
                rejection_reasons=tuple(rejection_reasons),
                checks=tuple(checks),
            )

        best = max(candidates, key=lambda candidate: candidate.intent.score)
        return StrategyEvaluation(
            intent=best.intent,
            snapshot=snapshot,
            rejection_reasons=(),
            checks=tuple(checks),
        )

    def _history_rejections(self, context: MarketContext, checks: list[StrategyCheck]) -> list[str]:
        reasons: list[str] = []
        checks.append(
            StrategyCheck(
                name="history_1m",
                passed=len(context.candles_1m) >= 20,
                threshold=">= 20 candles",
                value=len(context.candles_1m),
                reason="insufficient_history_1m",
            )
        )
        if len(context.candles_1m) < 20:
            reasons.append("insufficient_history_1m")
        checks.append(
            StrategyCheck(
                name="history_5m",
                passed=len(context.candles_5m) >= 30,
                threshold=">= 30 candles",
                value=len(context.candles_5m),
                reason="insufficient_history_5m",
            )
        )
        if len(context.candles_5m) < 30:
            reasons.append("insufficient_history_5m")
        checks.append(
            StrategyCheck(
                name="history_15m",
                passed=len(context.candles_15m) >= 60,
                threshold=">= 60 candles",
                value=len(context.candles_15m),
                reason="insufficient_history_15m",
            )
        )
        if len(context.candles_15m) < 60:
            reasons.append("insufficient_history_15m")
        return reasons

    def _build_snapshot(self, context: MarketContext) -> VolatilitySnapshot:
        closes_15m = [candle.close for candle in context.candles_15m]
        closes_5m = [candle.close for candle in context.candles_5m]
        ema20_15m = ema(closes_15m, 20)
        ema50_15m = ema(closes_15m, 50)
        adx_15m = adx(context.candles_15m, 14)
        atr_15m = atr(context.candles_15m, 14)
        vwap_20 = rolling_vwap(context.candles_5m, 20)
        close_5 = context.candles_5m[-1].close
        vwap_dist_bps = abs(close_5 - vwap_20) / max(close_5, 1e-9) * 10_000
        volume_z = last_value(rolling_zscore([c.volume for c in context.candles_5m], 20)) or 0.0
        atr_last = last_value(atr_15m) or 0.0
        atr_pct_15m = atr_last / max(closes_15m[-1], 1e-9) * 100

        return VolatilitySnapshot(
            pair=context.symbol,
            ts=context.candles_5m[-1].ts,
            atr_pct_15m=atr_pct_15m,
            spread_bps=context.order_book.spread_bps,
            vol_z_5m=volume_z,
            adx_15m=last_value(adx_15m) or 0.0,
            ema20_15m=last_value(ema20_15m) or 0.0,
            ema50_15m=last_value(ema50_15m) or 0.0,
            vwap_dist_bps=vwap_dist_bps,
            imbalance_1m=context.order_book.imbalance,
        )

    def _assess_regime(self, context: MarketContext, snapshot: VolatilitySnapshot) -> RegimeAssessment:
        closes_15m = [candle.close for candle in context.candles_15m]
        ema20_values = ema(closes_15m, 20)
        ema50_values = ema(closes_15m, 50)
        ema20_last = last_value(ema20_values)
        ema50_last = last_value(ema50_values)
        if ema20_last is None or ema50_last is None:
            return RegimeAssessment(label="bearish", ema_gap_pct=1.0, price_15m=closes_15m[-1])

        price_15m = closes_15m[-1]
        ema_gap_pct = max((ema50_last - ema20_last) / max(ema50_last, 1e-9), 0.0)
        price_buffer = (last_value(atr(context.candles_15m, 14)) or 0.0) * self.config.recovery_price_reclaim_buffer_atr
        bullish = (
            ema20_last > ema50_last
            and is_rising(ema20_values)
            and is_rising(ema50_values)
            and snapshot.adx_15m >= self.config.min_adx_15m
        )
        if bullish:
            return RegimeAssessment(label="bullish", ema_gap_pct=ema_gap_pct, price_15m=price_15m)

        recovery = (
            is_rising(ema20_values)
            and price_15m >= (ema20_last - price_buffer)
            and snapshot.adx_15m >= self.config.recovery_min_adx_15m
            and ema_gap_pct <= self.config.recovery_max_ema_gap_pct
        )
        if recovery:
            return RegimeAssessment(label="recovery", ema_gap_pct=ema_gap_pct, price_15m=price_15m)

        return RegimeAssessment(label="bearish", ema_gap_pct=ema_gap_pct, price_15m=price_15m)

    def _market_rejections(
        self,
        context: MarketContext,
        snapshot: VolatilitySnapshot,
        regime: RegimeAssessment,
        checks: list[StrategyCheck],
    ) -> list[str]:
        reasons: list[str] = []
        checks.append(
            StrategyCheck(
                name="long_regime",
                passed=regime.label in {"bullish", "recovery"},
                threshold="bullish or recovery",
                value=regime.label,
                reason="unsupported_long_regime",
            )
        )
        if regime.label not in {"bullish", "recovery"}:
            reasons.append("unsupported_long_regime")

        checks.append(
            StrategyCheck(
                name="spread_bps",
                passed=snapshot.spread_bps <= self.config.max_spread_bps,
                threshold=f"<= {self.config.max_spread_bps:.2f} bps",
                value=round(snapshot.spread_bps, 4),
                reason="spread_too_wide",
            )
        )
        if snapshot.spread_bps > self.config.max_spread_bps:
            reasons.append("spread_too_wide")

        checks.append(
            StrategyCheck(
                name="imbalance_1m",
                passed=snapshot.imbalance_1m >= 1.10,
                threshold=">= 1.10",
                value=round(snapshot.imbalance_1m, 4),
                reason="imbalance_too_low",
            )
        )
        if snapshot.imbalance_1m < 1.10:
            reasons.append("imbalance_too_low")

        has_shock = self._has_shock_candle(context)
        checks.append(
            StrategyCheck(
                name="recent_shock_candle",
                passed=not has_shock,
                threshold=f"no 1m candle > {self.config.shock_candle_atr_multiple:.2f}x ATR in last 3 bars",
                value=has_shock,
                reason="recent_shock_candle",
            )
        )
        if has_shock:
            reasons.append("recent_shock_candle")
        return reasons

    def _evaluate_breakout_candidate(
        self,
        context: MarketContext,
        snapshot: VolatilitySnapshot,
        regime: RegimeAssessment,
    ) -> SetupCandidate:
        if regime.label != "bullish":
            return SetupCandidate(intent=None, checks=())

        checks: list[StrategyCheck] = [
            StrategyCheck(
                name="breakout_regime",
                passed=True,
                threshold="regime must be bullish",
                value=regime.label,
            )
        ]

        atr_pct_rank = percentile_rank(context.atr_pct_history_15m, snapshot.atr_pct_15m)
        checks.append(
            StrategyCheck(
                name="breakout_atr_percentile_15m",
                passed=self.config.atr_percentile_min <= atr_pct_rank <= self.config.atr_percentile_max,
                threshold=f"{self.config.atr_percentile_min:.1f}-{self.config.atr_percentile_max:.1f}",
                value=round(atr_pct_rank, 4),
                reason="breakout_atr_percentile_out_of_range",
            )
        )
        checks.append(
            StrategyCheck(
                name="breakout_volume_zscore_5m",
                passed=snapshot.vol_z_5m >= self.config.min_volume_zscore,
                threshold=f">= {self.config.min_volume_zscore:.2f}",
                value=round(snapshot.vol_z_5m, 4),
                reason="breakout_volume_zscore_too_low",
            )
        )
        if not checks[-2].passed or not checks[-1].passed:
            return SetupCandidate(intent=None, checks=tuple(checks))

        breakout = self._detect_breakout_pullback(context)
        checks.append(
            StrategyCheck(
                name="breakout_pullback_pattern",
                passed=breakout is not None,
                threshold="breakout detected and pullback confirmed within 3 bars",
                value="detected" if breakout is not None else "missing",
                reason="no_breakout_pullback",
            )
        )
        if breakout is None:
            return SetupCandidate(intent=None, checks=tuple(checks))

        entry_price, breakout_level, pullback_low, atr5 = breakout
        stop_price = self._compute_stop_price(entry_price, pullback_low, atr5, setup_type="breakout_pullback")
        checks.append(
            StrategyCheck(
                name="breakout_valid_stop",
                passed=stop_price < entry_price,
                threshold="stop_price < entry_price",
                value=round(entry_price - stop_price, 8),
                reason="invalid_stop",
            )
        )
        if stop_price >= entry_price:
            return SetupCandidate(intent=None, checks=tuple(checks))

        score = self._score_setup(snapshot, regime.label, "breakout_pullback")
        intent = DayTradeIntent(
            pair=context.symbol,
            entry_zone=entry_price,
            stop_price=stop_price,
            trail_activation_r=self.config.trail_activation_r,
            max_hold_min=self.config.max_hold_minutes,
            budget_eur=0.0,
            reason_code=f"breakout_pullback:{breakout_level:.2f}",
            score=score,
            quality="A" if score >= 75 else "B",
            setup_type="breakout_pullback",
            regime_label=regime.label,
            strategy_id=self.strategy_id,
            strategy_family=self.strategy_family,
            break_even_trigger_r=self.config.break_even_trigger_r,
        )
        return SetupCandidate(intent=intent, checks=tuple(checks))

    def _evaluate_recovery_candidate(
        self,
        context: MarketContext,
        snapshot: VolatilitySnapshot,
        regime: RegimeAssessment,
    ) -> SetupCandidate:
        if regime.label not in {"bullish", "recovery"}:
            return SetupCandidate(intent=None, checks=())

        checks: list[StrategyCheck] = [
            StrategyCheck(
                name="recovery_regime",
                passed=True,
                threshold="regime must be bullish or recovery",
                value=regime.label,
            )
        ]

        atr_pct_rank = percentile_rank(context.atr_pct_history_15m, snapshot.atr_pct_15m)
        checks.append(
            StrategyCheck(
                name="recovery_atr_percentile_15m",
                passed=self.config.recovery_atr_percentile_min <= atr_pct_rank <= self.config.recovery_atr_percentile_max,
                threshold=f"{self.config.recovery_atr_percentile_min:.1f}-{self.config.recovery_atr_percentile_max:.1f}",
                value=round(atr_pct_rank, 4),
                reason="recovery_atr_percentile_out_of_range",
            )
        )
        if not checks[-1].passed:
            return SetupCandidate(intent=None, checks=tuple(checks))

        recovery = self._detect_recovery_reclaim(context)
        checks.append(
            StrategyCheck(
                name="recovery_reclaim_pattern",
                passed=recovery is not None,
                threshold="5m reclaim above prior high after compressed pullback",
                value="detected" if recovery is not None else "missing",
                reason="no_recovery_reclaim",
            )
        )
        if recovery is None:
            return SetupCandidate(intent=None, checks=tuple(checks))

        entry_price, reclaim_level, pullback_low, atr5 = recovery
        stop_price = self._compute_stop_price(entry_price, pullback_low, atr5, setup_type="recovery_reclaim")
        checks.append(
            StrategyCheck(
                name="recovery_valid_stop",
                passed=stop_price < entry_price,
                threshold="stop_price < entry_price",
                value=round(entry_price - stop_price, 8),
                reason="invalid_stop",
            )
        )
        if stop_price >= entry_price:
            return SetupCandidate(intent=None, checks=tuple(checks))

        score = self._score_setup(snapshot, regime.label, "recovery_reclaim")
        quality = self.config.classify_quality(score)
        checks.append(
            StrategyCheck(
                name="recovery_min_score",
                passed=score >= self.config.recovery_min_score,
                threshold=f">= {self.config.recovery_min_score:.2f}",
                value=round(score, 4),
                reason="recovery_score_too_low",
            )
        )
        if score < self.config.recovery_min_score:
            return SetupCandidate(intent=None, checks=tuple(checks))
        intent = DayTradeIntent(
            pair=context.symbol,
            entry_zone=entry_price,
            stop_price=stop_price,
            trail_activation_r=self.config.recovery_trail_activation_r,
            max_hold_min=self.config.recovery_max_hold_minutes,
            budget_eur=0.0,
            reason_code=f"recovery_reclaim:{reclaim_level:.2f}",
            score=score,
            quality=quality,
            setup_type="recovery_reclaim",
            regime_label=regime.label,
            strategy_id=self.strategy_id,
            strategy_family=self.strategy_family,
            break_even_trigger_r=self.config.recovery_break_even_trigger_r,
            time_decay_minutes=self.config.recovery_time_decay_minutes,
            time_decay_min_r=self.config.recovery_time_decay_min_r,
        )
        return SetupCandidate(intent=intent, checks=tuple(checks))

    def _has_shock_candle(self, context: MarketContext) -> bool:
        atr_1m = last_value(atr(context.candles_1m, 14))
        if atr_1m is None or atr_1m <= 0:
            return False
        return any(candle.range > (self.config.shock_candle_atr_multiple * atr_1m) for candle in context.candles_1m[-3:])

    def _detect_breakout_pullback(self, context: MarketContext) -> tuple[float, float, float, float] | None:
        candles_5m = context.candles_5m
        closes_5m = [c.close for c in candles_5m]
        ema9_5m = ema(closes_5m, 9)
        atr_5m = atr(candles_5m, 14)
        volume_z = rolling_zscore([c.volume for c in candles_5m], 20)
        latest_index = len(candles_5m) - 1
        latest = candles_5m[-1]
        latest_ema9 = last_value(ema9_5m)
        latest_atr5 = last_value(atr_5m)
        current_vwap = rolling_vwap(candles_5m, 20)

        if latest_ema9 is None or latest_atr5 is None:
            return None

        start_index = max(20, latest_index - 3)
        for breakout_index in range(start_index, latest_index):
            breakout_bar = candles_5m[breakout_index]
            breakout_level = rolling_high(candles_5m, 20, breakout_index)
            breakout_vol = volume_z[breakout_index]
            if breakout_vol is None or breakout_vol < self.config.breakout_volume_zscore:
                continue
            if breakout_bar.close <= breakout_level:
                continue

            post_breakout = candles_5m[breakout_index + 1 : latest_index + 1]
            if not post_breakout or len(post_breakout) > 3:
                continue

            pullback_low = min(candle.low for candle in post_breakout)
            if pullback_low > breakout_level + (0.25 * latest_atr5):
                continue
            if pullback_low < breakout_level - (0.35 * latest_atr5):
                continue
            if latest.close <= latest_ema9:
                continue
            if latest.close <= breakout_level:
                continue
            if latest.close <= latest.open:
                continue
            if abs(latest.low - current_vwap) > (0.35 * latest_atr5) and latest.low > breakout_level + (0.25 * latest_atr5):
                continue
            return context.order_book.best_ask, breakout_level, pullback_low, latest_atr5
        return None

    def _detect_recovery_reclaim(self, context: MarketContext) -> tuple[float, float, float, float] | None:
        candles_5m = context.candles_5m
        closes_5m = [c.close for c in candles_5m]
        ema9_5m = ema(closes_5m, 9)
        atr_5m = atr(candles_5m, 14)
        latest = candles_5m[-1]
        prev = candles_5m[-2]
        prev2 = candles_5m[-3]
        latest_ema9 = last_value(ema9_5m)
        latest_atr5 = last_value(atr_5m)

        if latest_ema9 is None or latest_atr5 is None:
            return None

        reclaim_buffer = latest_atr5 * self.config.recovery_ema_reclaim_buffer_atr
        compression = max(candle.high for candle in candles_5m[-4:-1]) - min(candle.low for candle in candles_5m[-4:-1])
        if compression > (self.config.recovery_compression_atr_multiple * latest_atr5):
            return None
        if latest.close <= (prev.high - reclaim_buffer):
            return None
        if latest.close <= latest_ema9:
            return None
        if latest.close <= latest.open:
            return None
        if latest.low < (min(prev.low, prev2.low) - reclaim_buffer):
            return None
        if latest.low < latest_ema9 - (self.config.recovery_ema_reclaim_buffer_atr * latest_atr5):
            return None
        if latest.close <= rolling_vwap(candles_5m, 20):
            return None

        return context.order_book.best_ask, prev.high, min(prev.low, prev2.low), latest_atr5

    def _compute_stop_price(self, entry_price: float, pullback_low: float, atr5: float, setup_type: str) -> float:
        stop_atr_multiple = self.config.stop_atr_multiple
        max_stop_pct = self.config.max_stop_pct
        if setup_type == "recovery_reclaim":
            stop_atr_multiple = self.config.recovery_stop_atr_multiple
            max_stop_pct = self.config.recovery_max_stop_pct
        structural_stop = pullback_low
        atr_stop = entry_price - (stop_atr_multiple * atr5)
        raw_stop = min(structural_stop, atr_stop)
        min_stop = entry_price * (1.0 - max_stop_pct)
        return max(raw_stop, min_stop)

    def _score_setup(self, snapshot: VolatilitySnapshot, regime_label: str, pattern: str) -> float:
        trend_baseline = self.config.recovery_min_adx_15m if regime_label == "recovery" else self.config.min_adx_15m
        trend_score = min(max(snapshot.adx_15m - trend_baseline, 0.0) * 2.2, 25.0)
        volume_score = min(max(snapshot.vol_z_5m, 0.0) * 6.0, 15.0)
        spread_score = max(0.0, 15.0 - snapshot.spread_bps)
        imbalance_score = min(max(snapshot.imbalance_1m - 1.0, 0.0) * 30.0, 20.0)
        vwap_score = max(0.0, 15.0 - min(snapshot.vwap_dist_bps, 15.0))
        regime_bonus = 12.0 if regime_label == "bullish" else 8.0 if regime_label == "recovery" else 0.0
        pattern_bonus = 12.0 if pattern == "breakout_pullback" else 10.0
        return min(
            100.0,
            trend_score + volume_score + spread_score + imbalance_score + vwap_score + regime_bonus + pattern_bonus,
        )


class OpeningRangeBreakoutStrategy:
    def __init__(
        self,
        config: BotConfig,
        *,
        strategy_id: str = "opening_range_breakout",
        strategy_family: str = "opening_range",
    ) -> None:
        self.config = config
        self.strategy_id = strategy_id
        self.strategy_family = strategy_family
        self._helper = BreakoutPullbackStrategy(config)

    def evaluate(self, context: MarketContext) -> DayTradeIntent | None:
        return self.evaluate_detailed(context).intent

    def evaluate_detailed(self, context: MarketContext) -> StrategyEvaluation:
        checks: list[StrategyCheck] = []
        rejection_reasons = self._helper._history_rejections(context, checks)
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=None, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        snapshot = self._helper._build_snapshot(context)
        regime = self._helper._assess_regime(context, snapshot)
        rejection_reasons.extend(self._helper._market_rejections(context, snapshot, regime, checks))
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        candidate = self._evaluate_candidate(context, snapshot, regime)
        checks.extend(candidate.checks)
        if candidate.intent is None:
            rejection_reasons.extend(
                check.reason
                for check in checks
                if not check.passed and check.reason is not None and check.reason not in rejection_reasons
            )
            if not rejection_reasons:
                rejection_reasons.append("no_opening_range_breakout")
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        return StrategyEvaluation(intent=candidate.intent, snapshot=snapshot, rejection_reasons=(), checks=tuple(checks))

    def _evaluate_candidate(
        self,
        context: MarketContext,
        snapshot: VolatilitySnapshot,
        regime: RegimeAssessment,
    ) -> SetupCandidate:
        session_candles = _session_candles_5m(context, self.config)
        checks: list[StrategyCheck] = [
            StrategyCheck(
                name="orb_regime",
                passed=regime.label == "bullish",
                threshold="regime must be bullish",
                value=regime.label,
                reason="orb_requires_bullish_regime",
            ),
            StrategyCheck(
                name="orb_session_ready",
                passed=len(session_candles) > self.config.opening_range_bars_5m,
                threshold=f"> {self.config.opening_range_bars_5m} 5m bars in active session",
                value=len(session_candles),
                reason="orb_session_not_ready",
            ),
        ]
        if any(not check.passed for check in checks):
            return SetupCandidate(intent=None, checks=tuple(checks))

        opening_range = session_candles[: self.config.opening_range_bars_5m]
        latest = session_candles[-1]
        closes_5m = [candle.close for candle in context.candles_5m]
        ema9_values = ema(closes_5m, 9)
        atr_5m = atr(context.candles_5m, 14)
        volume_z = last_value(rolling_zscore([c.volume for c in context.candles_5m], 20)) or 0.0
        latest_ema9 = last_value(ema9_values)
        latest_atr5 = last_value(atr_5m)

        if latest_ema9 is None or latest_atr5 is None:
            checks.append(
                StrategyCheck(
                    name="orb_indicator_readiness",
                    passed=False,
                    threshold="EMA9 and ATR14 available",
                    value="missing",
                    reason="orb_indicators_not_ready",
                )
            )
            return SetupCandidate(intent=None, checks=tuple(checks))

        range_high = max(candle.high for candle in opening_range)
        range_low = min(candle.low for candle in opening_range)
        checks.extend(
            [
                StrategyCheck(
                    name="orb_volume_zscore",
                    passed=volume_z >= self.config.opening_range_volume_zscore,
                    threshold=f">= {self.config.opening_range_volume_zscore:.2f}",
                    value=round(volume_z, 4),
                    reason="orb_volume_too_low",
                ),
                StrategyCheck(
                    name="orb_breakout_close",
                    passed=latest.close > range_high and latest.close > latest_ema9 and latest.close > latest.open,
                    threshold="close above opening range high and EMA9",
                    value=round(latest.close, 4),
                    reason="orb_no_breakout_close",
                ),
                StrategyCheck(
                    name="orb_orderbook_imbalance",
                    passed=snapshot.imbalance_1m >= 1.06,
                    threshold=">= 1.06",
                    value=round(snapshot.imbalance_1m, 4),
                    reason="orb_imbalance_too_low",
                ),
            ]
        )
        if any(not check.passed for check in checks):
            return SetupCandidate(intent=None, checks=tuple(checks))

        entry_price = context.order_book.best_ask
        raw_stop = min(range_low, latest.low, entry_price - latest_atr5)
        stop_price = max(raw_stop, entry_price * (1.0 - self.config.max_stop_pct))
        checks.append(
            StrategyCheck(
                name="orb_valid_stop",
                passed=stop_price < entry_price,
                threshold="stop_price < entry_price",
                value=round(entry_price - stop_price, 8),
                reason="orb_invalid_stop",
            )
        )
        if stop_price >= entry_price:
            return SetupCandidate(intent=None, checks=tuple(checks))

        score = min(
            100.0,
            40.0
            + min(max(snapshot.adx_15m - self.config.min_adx_15m, 0.0) * 2.0, 18.0)
            + min(max(volume_z - self.config.opening_range_volume_zscore, 0.0) * 8.0, 16.0)
            + min(max(snapshot.imbalance_1m - 1.0, 0.0) * 25.0, 14.0)
            + max(0.0, 12.0 - snapshot.spread_bps),
        )
        quality = self.config.classify_quality(score)
        intent = DayTradeIntent(
            pair=context.symbol,
            entry_zone=entry_price,
            stop_price=stop_price,
            trail_activation_r=max(self.config.trail_activation_r, 1.5),
            max_hold_min=self.config.opening_range_max_hold_minutes,
            budget_eur=0.0,
            reason_code=f"opening_range_breakout:{range_high:.2f}",
            score=score,
            quality=quality,
            setup_type="opening_range_breakout",
            regime_label="opening_range",
            strategy_id=self.strategy_id,
            strategy_family=self.strategy_family,
            break_even_trigger_r=max(self.config.break_even_trigger_r, 1.1),
            time_decay_minutes=45,
            time_decay_min_r=0.20,
        )
        return SetupCandidate(intent=intent, checks=tuple(checks))


class TrendContinuationPullbackStrategy:
    def __init__(
        self,
        config: BotConfig,
        *,
        strategy_id: str = "trend_continuation_pullback",
        strategy_family: str = "trend_continuation",
    ) -> None:
        self.config = config
        self.strategy_id = strategy_id
        self.strategy_family = strategy_family
        self._helper = BreakoutPullbackStrategy(config)

    def evaluate(self, context: MarketContext) -> DayTradeIntent | None:
        return self.evaluate_detailed(context).intent

    def evaluate_detailed(self, context: MarketContext) -> StrategyEvaluation:
        checks: list[StrategyCheck] = []
        rejection_reasons = self._helper._history_rejections(context, checks)
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=None, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        snapshot = self._helper._build_snapshot(context)
        regime = self._helper._assess_regime(context, snapshot)
        rejection_reasons.extend(self._helper._market_rejections(context, snapshot, regime, checks))
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        candidate = self._evaluate_candidate(context, snapshot, regime)
        checks.extend(candidate.checks)
        if candidate.intent is None:
            rejection_reasons.extend(
                check.reason
                for check in checks
                if not check.passed and check.reason is not None and check.reason not in rejection_reasons
            )
            if not rejection_reasons:
                rejection_reasons.append("no_trend_continuation_pullback")
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        return StrategyEvaluation(intent=candidate.intent, snapshot=snapshot, rejection_reasons=(), checks=tuple(checks))

    def _evaluate_candidate(
        self,
        context: MarketContext,
        snapshot: VolatilitySnapshot,
        regime: RegimeAssessment,
    ) -> SetupCandidate:
        closes_5m = [candle.close for candle in context.candles_5m]
        ema9_values = ema(closes_5m, 9)
        ema20_values = ema(closes_5m, 20)
        atr_5m = atr(context.candles_5m, 14)
        volume_z = rolling_zscore([c.volume for c in context.candles_5m], 20)
        latest = context.candles_5m[-1]
        prev = context.candles_5m[-2]
        prev2 = context.candles_5m[-3]
        latest_ema9 = last_value(ema9_values)
        latest_ema20 = last_value(ema20_values)
        latest_atr5 = last_value(atr_5m)
        latest_volume_z = last_value(volume_z) or 0.0
        current_vwap = rolling_vwap(context.candles_5m, 20)

        checks: list[StrategyCheck] = [
            StrategyCheck(
                name="tcp_regime",
                passed=regime.label == "bullish" and snapshot.adx_15m >= self.config.trend_pullback_min_adx_15m,
                threshold=f"bullish with ADX >= {self.config.trend_pullback_min_adx_15m:.1f}",
                value=f"{regime.label}/{snapshot.adx_15m:.2f}",
                reason="tcp_requires_strong_trend",
            )
        ]
        if not checks[-1].passed:
            return SetupCandidate(intent=None, checks=tuple(checks))

        if latest_ema9 is None or latest_ema20 is None or latest_atr5 is None:
            checks.append(
                StrategyCheck(
                    name="tcp_indicator_readiness",
                    passed=False,
                    threshold="EMA9, EMA20 and ATR14 available",
                    value="missing",
                    reason="tcp_indicators_not_ready",
                )
            )
            return SetupCandidate(intent=None, checks=tuple(checks))

        pullback_floor = max(latest_ema9, min(latest_ema20, current_vwap + (0.25 * latest_atr5)))
        recent_high = max(candle.high for candle in context.candles_5m[-10:-1])
        checks.extend(
            [
                StrategyCheck(
                    name="tcp_volume_zscore",
                    passed=latest_volume_z >= self.config.trend_pullback_volume_zscore,
                    threshold=f">= {self.config.trend_pullback_volume_zscore:.2f}",
                    value=round(latest_volume_z, 4),
                    reason="tcp_volume_too_low",
                ),
                StrategyCheck(
                    name="tcp_pullback_zone",
                    passed=min(prev.low, prev2.low) <= (pullback_floor + (0.45 * latest_atr5)),
                    threshold="prior bar revisits EMA20/VWAP pullback zone",
                    value=round(min(prev.low, prev2.low), 4),
                    reason="tcp_no_pullback_zone_touch",
                ),
                StrategyCheck(
                    name="tcp_reclaim_close",
                    passed=latest.close > latest_ema9 and latest.close > prev.high and latest.close > latest.open,
                    threshold="latest close reclaims above prior high and EMA9",
                    value=round(latest.close, 4),
                    reason="tcp_no_reclaim_close",
                ),
                StrategyCheck(
                    name="tcp_range_expansion",
                    passed=latest.close >= recent_high - (0.20 * latest_atr5),
                    threshold="latest close near fresh continuation high",
                    value=round(recent_high, 4),
                    reason="tcp_no_continuation_high",
                ),
            ]
        )
        if any(not check.passed for check in checks):
            return SetupCandidate(intent=None, checks=tuple(checks))

        entry_price = context.order_book.best_ask
        raw_stop = min(prev.low, prev2.low, entry_price - latest_atr5)
        stop_price = max(raw_stop, entry_price * (1.0 - self.config.max_stop_pct))
        checks.append(
            StrategyCheck(
                name="tcp_valid_stop",
                passed=stop_price < entry_price,
                threshold="stop_price < entry_price",
                value=round(entry_price - stop_price, 8),
                reason="tcp_invalid_stop",
            )
        )
        if stop_price >= entry_price:
            return SetupCandidate(intent=None, checks=tuple(checks))

        score = min(
            100.0,
            38.0
            + min(max(snapshot.adx_15m - self.config.trend_pullback_min_adx_15m, 0.0) * 2.1, 16.0)
            + min(max(latest_volume_z - self.config.trend_pullback_volume_zscore, 0.0) * 7.0, 14.0)
            + min(max(snapshot.imbalance_1m - 1.0, 0.0) * 28.0, 14.0)
            + max(0.0, 14.0 - snapshot.spread_bps)
            + 10.0,
        )
        quality = self.config.classify_quality(score)
        intent = DayTradeIntent(
            pair=context.symbol,
            entry_zone=entry_price,
            stop_price=stop_price,
            trail_activation_r=max(self.config.trail_activation_r, 1.3),
            max_hold_min=self.config.trend_pullback_max_hold_minutes,
            budget_eur=0.0,
            reason_code=f"trend_continuation_pullback:{prev.high:.2f}",
            score=score,
            quality=quality,
            setup_type="trend_continuation_pullback",
            regime_label="trend_continuation",
            strategy_id=self.strategy_id,
            strategy_family=self.strategy_family,
            break_even_trigger_r=max(self.config.break_even_trigger_r, 0.9),
            time_decay_minutes=40,
            time_decay_min_r=0.18,
        )
        return SetupCandidate(intent=intent, checks=tuple(checks))


class FastMicroScalpStrategy:
    def __init__(
        self,
        config: BotConfig,
        *,
        strategy_id: str = "fast_imbalance_scalp",
        strategy_family: str = "fast_trading",
    ) -> None:
        self.config = config
        self.strategy_id = strategy_id
        self.strategy_family = strategy_family
        self._helper = BreakoutPullbackStrategy(config)

    def evaluate(self, context: MarketContext) -> DayTradeIntent | None:
        return self.evaluate_detailed(context).intent

    def evaluate_detailed(self, context: MarketContext) -> StrategyEvaluation:
        checks: list[StrategyCheck] = []
        rejection_reasons = self._helper._history_rejections(context, checks)
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=None, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        snapshot = self._helper._build_snapshot(context)
        regime = self._helper._assess_regime(context, snapshot)
        windows = context.analysis_windows or {}
        micro_count = len(context.micro_samples)

        checks.extend(
            [
                StrategyCheck(
                    name="fast_micro_samples",
                    passed=micro_count >= self.config.fast_min_micro_samples,
                    threshold=f">= {self.config.fast_min_micro_samples}",
                    value=micro_count,
                    reason="fast_not_enough_micro_samples",
                ),
                StrategyCheck(
                    name="fast_regime",
                    passed=regime.label in {"bullish", "recovery"} and snapshot.adx_15m >= self.config.fast_min_adx_15m,
                    threshold=f"bullish/recovery and ADX >= {self.config.fast_min_adx_15m:.1f}",
                    value=f"{regime.label}/{snapshot.adx_15m:.2f}",
                    reason="fast_regime_not_supported",
                ),
                StrategyCheck(
                    name="fast_spread_bps",
                    passed=snapshot.spread_bps <= min(self.config.max_spread_bps, self.config.fast_max_spread_bps),
                    threshold=f"<= {min(self.config.max_spread_bps, self.config.fast_max_spread_bps):.2f} bps",
                    value=round(snapshot.spread_bps, 4),
                    reason="fast_spread_too_wide",
                ),
                StrategyCheck(
                    name="fast_imbalance",
                    passed=snapshot.imbalance_1m >= self.config.fast_min_imbalance,
                    threshold=f">= {self.config.fast_min_imbalance:.2f}",
                    value=round(snapshot.imbalance_1m, 4),
                    reason="fast_imbalance_too_low",
                ),
            ]
        )
        fast_1s = windows.get("1S") or {}
        fast_5s = windows.get("5S") or {}
        change_1s_bps = _window_change_bps(fast_1s)
        change_5s_bps = _window_change_bps(fast_5s)
        range_5s_bps = _window_range_bps(fast_5s)
        checks.extend(
            [
                StrategyCheck(
                    name="fast_window_1s",
                    passed=bool(fast_1s.get("available")),
                    threshold="1S profile available",
                    value=fast_1s.get("available"),
                    reason="fast_1s_window_unavailable",
                ),
                StrategyCheck(
                    name="fast_window_5s",
                    passed=bool(fast_5s.get("available")),
                    threshold="5S profile available",
                    value=fast_5s.get("available"),
                    reason="fast_5s_window_unavailable",
                ),
                StrategyCheck(
                    name="fast_change_1s_bps",
                    passed=change_1s_bps >= self.config.fast_min_change_1s_bps,
                    threshold=f">= {self.config.fast_min_change_1s_bps:.2f} bps",
                    value=round(change_1s_bps, 4),
                    reason="fast_1s_thrust_too_low",
                ),
                StrategyCheck(
                    name="fast_change_5s_bps",
                    passed=change_5s_bps >= self.config.fast_min_change_5s_bps,
                    threshold=f">= {self.config.fast_min_change_5s_bps:.2f} bps",
                    value=round(change_5s_bps, 4),
                    reason="fast_5s_thrust_too_low",
                ),
                StrategyCheck(
                    name="fast_range_5s_bps",
                    passed=0.0 < range_5s_bps <= self.config.fast_max_range_5s_bps,
                    threshold=f"0 < range <= {self.config.fast_max_range_5s_bps:.2f} bps",
                    value=round(range_5s_bps, 4),
                    reason="fast_5s_range_out_of_bounds",
                ),
            ]
        )
        latest_candle = context.candles_1m[-1]
        ema9_1m = last_value(ema([candle.close for candle in context.candles_1m], 9))
        checks.append(
            StrategyCheck(
                name="fast_1m_structure",
                passed=ema9_1m is not None and latest_candle.close >= ema9_1m and latest_candle.close >= latest_candle.open,
                threshold="latest 1m close above EMA9 and above open",
                value=round(latest_candle.close, 4),
                reason="fast_1m_structure_not_confirmed",
            )
        )
        if any(not check.passed for check in checks):
            rejection_reasons.extend(
                check.reason
                for check in checks
                if not check.passed and check.reason is not None and check.reason not in rejection_reasons
            )
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        atr_1m = last_value(atr(context.candles_1m, 14)) or max(latest_candle.range, latest_candle.close * 0.0012)
        entry_price = context.order_book.best_ask
        stop_price = max(
            entry_price - (self.config.fast_stop_atr_multiple * atr_1m),
            entry_price * (1.0 - self.config.fast_max_stop_pct),
        )
        checks.append(
            StrategyCheck(
                name="fast_valid_stop",
                passed=stop_price < entry_price,
                threshold="stop_price < entry_price",
                value=round(entry_price - stop_price, 8),
                reason="fast_invalid_stop",
            )
        )
        if stop_price >= entry_price:
            rejection_reasons.append("fast_invalid_stop")
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        score = min(
            100.0,
            42.0
            + min(max(change_1s_bps - self.config.fast_min_change_1s_bps, 0.0) * 4.0, 12.0)
            + min(max(change_5s_bps - self.config.fast_min_change_5s_bps, 0.0) * 3.0, 14.0)
            + min(max(snapshot.imbalance_1m - 1.0, 0.0) * 32.0, 16.0)
            + max(0.0, 8.0 - snapshot.spread_bps)
            + min(max(snapshot.adx_15m - self.config.fast_min_adx_15m, 0.0) * 1.5, 10.0)
        )
        quality = self.config.classify_quality(score)
        intent = DayTradeIntent(
            pair=context.symbol,
            entry_zone=entry_price,
            stop_price=stop_price,
            trail_activation_r=self.config.fast_trail_activation_r,
            max_hold_min=self.config.fast_max_hold_minutes,
            budget_eur=0.0,
            reason_code=f"fast_micro_scalp:{change_1s_bps:.2f}/{change_5s_bps:.2f}",
            score=score,
            quality=quality,
            setup_type="fast_micro_scalp",
            regime_label="fast_trading",
            strategy_id=self.strategy_id,
            strategy_family=self.strategy_family,
            break_even_trigger_r=self.config.fast_break_even_trigger_r,
            time_decay_minutes=self.config.fast_time_decay_minutes,
            time_decay_min_r=self.config.fast_time_decay_min_r,
        )
        return StrategyEvaluation(intent=intent, snapshot=snapshot, rejection_reasons=(), checks=tuple(checks))


class FastLiquiditySweepReclaimStrategy:
    def __init__(
        self,
        config: BotConfig,
        *,
        strategy_id: str = "fast_liquidity_sweep_reclaim",
        strategy_family: str = "fast_trading",
    ) -> None:
        self.config = config
        self.strategy_id = strategy_id
        self.strategy_family = strategy_family
        self._helper = BreakoutPullbackStrategy(config)

    def evaluate(self, context: MarketContext) -> DayTradeIntent | None:
        return self.evaluate_detailed(context).intent

    def evaluate_detailed(self, context: MarketContext) -> StrategyEvaluation:
        checks: list[StrategyCheck] = []
        rejection_reasons = self._helper._history_rejections(context, checks)
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=None, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        snapshot = self._helper._build_snapshot(context)
        regime = self._helper._assess_regime(context, snapshot)
        windows = context.analysis_windows or {}
        micro_count = len(context.micro_samples)
        fast_1s = windows.get("1S") or {}
        fast_5s = windows.get("5S") or {}
        change_1s_bps = _window_change_bps(fast_1s)
        change_5s_bps = _window_change_bps(fast_5s)
        range_5s_bps = _window_range_bps(fast_5s)
        lookback = max(self.config.fast_sweep_lookback_bars, 3)
        latest_candle = context.candles_1m[-1]
        sweep_window = list(context.candles_1m[-3:-1])
        sweep_candle = min(sweep_window, key=lambda candle: candle.low)
        reference_segment = list(context.candles_1m[-(lookback + 3):-3])
        reference_sample = reference_segment[-3:] if len(reference_segment) >= 3 else reference_segment
        reference_low = min((candle.low for candle in reference_sample), default=sweep_candle.low)
        ema9_1m = last_value(ema([candle.close for candle in context.candles_1m], 9))

        checks.extend(
            [
                StrategyCheck(
                    name="fast_micro_samples",
                    passed=micro_count >= self.config.fast_min_micro_samples,
                    threshold=f">= {self.config.fast_min_micro_samples}",
                    value=micro_count,
                    reason="fast_not_enough_micro_samples",
                ),
                StrategyCheck(
                    name="fast_regime",
                    passed=regime.label in {"bullish", "recovery"} and snapshot.adx_15m >= self.config.fast_min_adx_15m,
                    threshold=f"bullish/recovery and ADX >= {self.config.fast_min_adx_15m:.1f}",
                    value=f"{regime.label}/{snapshot.adx_15m:.2f}",
                    reason="fast_regime_not_supported",
                ),
                StrategyCheck(
                    name="fast_spread_bps",
                    passed=snapshot.spread_bps <= min(self.config.max_spread_bps, self.config.fast_max_spread_bps),
                    threshold=f"<= {min(self.config.max_spread_bps, self.config.fast_max_spread_bps):.2f} bps",
                    value=round(snapshot.spread_bps, 4),
                    reason="fast_spread_too_wide",
                ),
                StrategyCheck(
                    name="fast_window_1s",
                    passed=bool(fast_1s.get("available")),
                    threshold="1S profile available",
                    value=fast_1s.get("available"),
                    reason="fast_1s_window_unavailable",
                ),
                StrategyCheck(
                    name="fast_window_5s",
                    passed=bool(fast_5s.get("available")),
                    threshold="5S profile available",
                    value=fast_5s.get("available"),
                    reason="fast_5s_window_unavailable",
                ),
                StrategyCheck(
                    name="fast_change_1s_bps",
                    passed=change_1s_bps >= (self.config.fast_min_change_1s_bps * 0.9),
                    threshold=f">= {(self.config.fast_min_change_1s_bps * 0.9):.2f} bps",
                    value=round(change_1s_bps, 4),
                    reason="fast_1s_thrust_too_low",
                ),
                StrategyCheck(
                    name="fast_change_5s_bps",
                    passed=change_5s_bps >= (self.config.fast_min_change_5s_bps * 0.9),
                    threshold=f">= {(self.config.fast_min_change_5s_bps * 0.9):.2f} bps",
                    value=round(change_5s_bps, 4),
                    reason="fast_5s_thrust_too_low",
                ),
                StrategyCheck(
                    name="fast_range_5s_bps",
                    passed=0.0 < range_5s_bps <= self.config.fast_max_range_5s_bps,
                    threshold=f"0 < range <= {self.config.fast_max_range_5s_bps:.2f} bps",
                    value=round(range_5s_bps, 4),
                    reason="fast_5s_range_out_of_bounds",
                ),
                StrategyCheck(
                    name="fast_imbalance",
                    passed=snapshot.imbalance_1m >= (self.config.fast_min_imbalance * 0.96),
                    threshold=f">= {(self.config.fast_min_imbalance * 0.96):.2f}",
                    value=round(snapshot.imbalance_1m, 4),
                    reason="fast_imbalance_too_low",
                ),
                StrategyCheck(
                    name="fast_sweep_reference",
                    passed=bool(reference_segment),
                    threshold=f"{lookback} prior 1m bars",
                    value=len(reference_segment),
                    reason="fast_sweep_reference_missing",
                ),
                StrategyCheck(
                    name="fast_liquidity_sweep",
                    passed=sweep_candle.low < reference_low,
                    threshold="sweep candle low below prior 1m lows",
                    value=round(sweep_candle.low - reference_low, 6),
                    reason="fast_no_liquidity_sweep",
                ),
                StrategyCheck(
                    name="fast_reclaim_close",
                    passed=latest_candle.close >= reference_low and latest_candle.close >= sweep_candle.close,
                    threshold="latest close reclaims prior low and sweep close",
                    value=round(latest_candle.close, 6),
                    reason="fast_no_sweep_reclaim",
                ),
                StrategyCheck(
                    name="fast_1m_structure",
                    passed=ema9_1m is not None and latest_candle.close >= ema9_1m and latest_candle.close >= latest_candle.open,
                    threshold="latest 1m close above EMA9 and above open",
                    value=round(latest_candle.close, 4),
                    reason="fast_1m_structure_not_confirmed",
                ),
            ]
        )
        if any(not check.passed for check in checks):
            rejection_reasons.extend(
                check.reason
                for check in checks
                if not check.passed and check.reason is not None and check.reason not in rejection_reasons
            )
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        atr_1m = last_value(atr(context.candles_1m, 14)) or max(latest_candle.range, latest_candle.close * 0.0010)
        entry_price = context.order_book.best_ask
        stop_price = max(
            sweep_candle.low - (0.15 * atr_1m),
            entry_price * (1.0 - self.config.fast_max_stop_pct),
        )
        checks.append(
            StrategyCheck(
                name="fast_valid_stop",
                passed=stop_price < entry_price,
                threshold="stop_price < entry_price",
                value=round(entry_price - stop_price, 8),
                reason="fast_invalid_stop",
            )
        )
        if stop_price >= entry_price:
            rejection_reasons.append("fast_invalid_stop")
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        sweep_depth_bps = max(((reference_low - sweep_candle.low) / max(entry_price, 1e-9)) * 10_000.0, 0.0)
        reclaim_strength_bps = max(((latest_candle.close - reference_low) / max(entry_price, 1e-9)) * 10_000.0, 0.0)
        score = min(
            100.0,
            40.0
            + min(sweep_depth_bps * 0.8, 14.0)
            + min(reclaim_strength_bps * 0.9, 12.0)
            + min(max(change_1s_bps - 1.0, 0.0) * 4.0, 10.0)
            + min(max(change_5s_bps - 2.0, 0.0) * 2.6, 10.0)
            + min(max(snapshot.imbalance_1m - 1.0, 0.0) * 28.0, 14.0)
            + max(0.0, 8.0 - snapshot.spread_bps)
        )
        quality = self.config.classify_quality(score)
        intent = DayTradeIntent(
            pair=context.symbol,
            entry_zone=entry_price,
            stop_price=stop_price,
            trail_activation_r=self.config.fast_trail_activation_r,
            max_hold_min=self.config.fast_max_hold_minutes,
            budget_eur=0.0,
            reason_code=f"fast_liquidity_sweep_reclaim:{sweep_depth_bps:.2f}/{reclaim_strength_bps:.2f}",
            score=score,
            quality=quality,
            setup_type="fast_liquidity_sweep_reclaim",
            regime_label="fast_trading",
            strategy_id=self.strategy_id,
            strategy_family=self.strategy_family,
            break_even_trigger_r=self.config.fast_break_even_trigger_r,
            time_decay_minutes=self.config.fast_time_decay_minutes,
            time_decay_min_r=self.config.fast_time_decay_min_r,
        )
        return StrategyEvaluation(intent=intent, snapshot=snapshot, rejection_reasons=(), checks=tuple(checks))


class FastVwapReclaimScalpStrategy:
    def __init__(
        self,
        config: BotConfig,
        *,
        strategy_id: str = "fast_vwap_reclaim_scalp",
        strategy_family: str = "fast_trading",
    ) -> None:
        self.config = config
        self.strategy_id = strategy_id
        self.strategy_family = strategy_family
        self._helper = BreakoutPullbackStrategy(config)

    def evaluate(self, context: MarketContext) -> DayTradeIntent | None:
        return self.evaluate_detailed(context).intent

    def evaluate_detailed(self, context: MarketContext) -> StrategyEvaluation:
        checks: list[StrategyCheck] = []
        rejection_reasons = self._helper._history_rejections(context, checks)
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=None, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        snapshot = self._helper._build_snapshot(context)
        regime = self._helper._assess_regime(context, snapshot)
        windows = context.analysis_windows or {}
        micro_count = len(context.micro_samples)
        fast_1s = windows.get("1S") or {}
        fast_5s = windows.get("5S") or {}
        change_1s_bps = _window_change_bps(fast_1s)
        change_5s_bps = _window_change_bps(fast_5s)
        range_5s_bps = _window_range_bps(fast_5s)
        latest_candle = context.candles_1m[-1]
        vwap_1m = rolling_vwap(context.candles_1m, 20)
        reclaim_threshold = vwap_1m * (1.0 + (self.config.fast_vwap_reclaim_buffer_bps / 10_000.0))
        prior_reclaim_segment = list(context.candles_1m[-4:-1])
        volume_z_1m = last_value(rolling_zscore([candle.volume for candle in context.candles_1m], 20)) or 0.0

        checks.extend(
            [
                StrategyCheck(
                    name="fast_micro_samples",
                    passed=micro_count >= self.config.fast_min_micro_samples,
                    threshold=f">= {self.config.fast_min_micro_samples}",
                    value=micro_count,
                    reason="fast_not_enough_micro_samples",
                ),
                StrategyCheck(
                    name="fast_regime",
                    passed=regime.label in {"bullish", "recovery"} and snapshot.adx_15m >= self.config.fast_min_adx_15m,
                    threshold=f"bullish/recovery and ADX >= {self.config.fast_min_adx_15m:.1f}",
                    value=f"{regime.label}/{snapshot.adx_15m:.2f}",
                    reason="fast_regime_not_supported",
                ),
                StrategyCheck(
                    name="fast_spread_bps",
                    passed=snapshot.spread_bps <= min(self.config.max_spread_bps, self.config.fast_max_spread_bps),
                    threshold=f"<= {min(self.config.max_spread_bps, self.config.fast_max_spread_bps):.2f} bps",
                    value=round(snapshot.spread_bps, 4),
                    reason="fast_spread_too_wide",
                ),
                StrategyCheck(
                    name="fast_window_1s",
                    passed=bool(fast_1s.get("available")),
                    threshold="1S profile available",
                    value=fast_1s.get("available"),
                    reason="fast_1s_window_unavailable",
                ),
                StrategyCheck(
                    name="fast_window_5s",
                    passed=bool(fast_5s.get("available")),
                    threshold="5S profile available",
                    value=fast_5s.get("available"),
                    reason="fast_5s_window_unavailable",
                ),
                StrategyCheck(
                    name="fast_change_1s_bps",
                    passed=change_1s_bps >= (self.config.fast_min_change_1s_bps * 0.8),
                    threshold=f">= {(self.config.fast_min_change_1s_bps * 0.8):.2f} bps",
                    value=round(change_1s_bps, 4),
                    reason="fast_1s_thrust_too_low",
                ),
                StrategyCheck(
                    name="fast_change_5s_bps",
                    passed=change_5s_bps >= (self.config.fast_min_change_5s_bps * 0.8),
                    threshold=f">= {(self.config.fast_min_change_5s_bps * 0.8):.2f} bps",
                    value=round(change_5s_bps, 4),
                    reason="fast_5s_thrust_too_low",
                ),
                StrategyCheck(
                    name="fast_range_5s_bps",
                    passed=0.0 < range_5s_bps <= self.config.fast_max_range_5s_bps,
                    threshold=f"0 < range <= {self.config.fast_max_range_5s_bps:.2f} bps",
                    value=round(range_5s_bps, 4),
                    reason="fast_5s_range_out_of_bounds",
                ),
                StrategyCheck(
                    name="fast_imbalance",
                    passed=snapshot.imbalance_1m >= (self.config.fast_min_imbalance * 0.94),
                    threshold=f">= {(self.config.fast_min_imbalance * 0.94):.2f}",
                    value=round(snapshot.imbalance_1m, 4),
                    reason="fast_imbalance_too_low",
                ),
                StrategyCheck(
                    name="fast_vwap_reclaim",
                    passed=latest_candle.close >= reclaim_threshold,
                    threshold=f"latest close >= VWAP + {self.config.fast_vwap_reclaim_buffer_bps:.2f} bps",
                    value=round(latest_candle.close, 6),
                    reason="fast_vwap_not_reclaimed",
                ),
                StrategyCheck(
                    name="fast_recent_dip_below_vwap",
                    passed=any(candle.close < vwap_1m for candle in prior_reclaim_segment),
                    threshold="recent 1m closes dipped below VWAP before reclaim",
                    value=round(min((candle.close for candle in prior_reclaim_segment), default=latest_candle.close), 6),
                    reason="fast_vwap_reclaim_missing_dip",
                ),
                StrategyCheck(
                    name="fast_volume_zscore_1m",
                    passed=volume_z_1m >= self.config.fast_vwap_min_volume_zscore,
                    threshold=f">= {self.config.fast_vwap_min_volume_zscore:.2f}",
                    value=round(volume_z_1m, 4),
                    reason="fast_vwap_volume_too_low",
                ),
            ]
        )
        if any(not check.passed for check in checks):
            rejection_reasons.extend(
                check.reason
                for check in checks
                if not check.passed and check.reason is not None and check.reason not in rejection_reasons
            )
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        atr_1m = last_value(atr(context.candles_1m, 14)) or max(latest_candle.range, latest_candle.close * 0.0010)
        entry_price = context.order_book.best_ask
        structure_low = min(candle.low for candle in prior_reclaim_segment) if prior_reclaim_segment else latest_candle.low
        stop_price = max(
            structure_low - (0.12 * atr_1m),
            entry_price * (1.0 - self.config.fast_max_stop_pct),
        )
        checks.append(
            StrategyCheck(
                name="fast_valid_stop",
                passed=stop_price < entry_price,
                threshold="stop_price < entry_price",
                value=round(entry_price - stop_price, 8),
                reason="fast_invalid_stop",
            )
        )
        if stop_price >= entry_price:
            rejection_reasons.append("fast_invalid_stop")
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        reclaim_bps = max(((latest_candle.close - vwap_1m) / max(entry_price, 1e-9)) * 10_000.0, 0.0)
        score = min(
            100.0,
            42.0
            + min(reclaim_bps * 1.2, 14.0)
            + min(max(volume_z_1m - self.config.fast_vwap_min_volume_zscore, 0.0) * 10.0, 12.0)
            + min(max(change_1s_bps - 0.8, 0.0) * 3.2, 10.0)
            + min(max(change_5s_bps - 2.4, 0.0) * 2.3, 10.0)
            + min(max(snapshot.imbalance_1m - 1.0, 0.0) * 26.0, 12.0)
            + max(0.0, 8.0 - snapshot.spread_bps)
        )
        quality = self.config.classify_quality(score)
        intent = DayTradeIntent(
            pair=context.symbol,
            entry_zone=entry_price,
            stop_price=stop_price,
            trail_activation_r=self.config.fast_trail_activation_r,
            max_hold_min=self.config.fast_max_hold_minutes,
            budget_eur=0.0,
            reason_code=f"fast_vwap_reclaim_scalp:{reclaim_bps:.2f}/{volume_z_1m:.2f}",
            score=score,
            quality=quality,
            setup_type="fast_vwap_reclaim_scalp",
            regime_label="fast_trading",
            strategy_id=self.strategy_id,
            strategy_family=self.strategy_family,
            break_even_trigger_r=self.config.fast_break_even_trigger_r,
            time_decay_minutes=self.config.fast_time_decay_minutes,
            time_decay_min_r=self.config.fast_time_decay_min_r,
        )
        return StrategyEvaluation(intent=intent, snapshot=snapshot, rejection_reasons=(), checks=tuple(checks))


class MeanReversionVwapStrategy:
    def __init__(
        self,
        config: BotConfig,
        *,
        strategy_id: str = "mean_reversion_vwap",
        strategy_family: str = "mean_reversion",
    ) -> None:
        self.config = config
        self.strategy_id = strategy_id
        self.strategy_family = strategy_family

    def evaluate(self, context: MarketContext) -> DayTradeIntent | None:
        return self.evaluate_detailed(context).intent

    def evaluate_detailed(self, context: MarketContext) -> StrategyEvaluation:
        checks: list[StrategyCheck] = []
        rejection_reasons = BreakoutPullbackStrategy(self.config)._history_rejections(context, checks)
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=None, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        snapshot = self._build_snapshot(context)
        rejection_reasons.extend(self._market_rejections(context, snapshot, checks))
        if rejection_reasons:
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        candidate = self._evaluate_reclaim_candidate(context, snapshot)
        checks.extend(candidate.checks)
        if candidate.intent is None:
            rejection_reasons.extend(
                check.reason
                for check in checks
                if not check.passed and check.reason is not None and check.reason not in rejection_reasons
            )
            if not rejection_reasons:
                rejection_reasons.append("no_mean_reversion_pattern")
            return StrategyEvaluation(intent=None, snapshot=snapshot, rejection_reasons=tuple(rejection_reasons), checks=tuple(checks))

        return StrategyEvaluation(intent=candidate.intent, snapshot=snapshot, rejection_reasons=(), checks=tuple(checks))

    def _build_snapshot(self, context: MarketContext) -> VolatilitySnapshot:
        closes_15m = [candle.close for candle in context.candles_15m]
        closes_5m = [candle.close for candle in context.candles_5m]
        ema20_15m = ema(closes_15m, 20)
        ema50_15m = ema(closes_15m, 50)
        adx_15m = adx(context.candles_15m, 14)
        atr_15m = atr(context.candles_15m, 14)
        vwap_20 = rolling_vwap(context.candles_5m, 20)
        close_5 = context.candles_5m[-1].close
        vwap_dist_bps = abs(close_5 - vwap_20) / max(close_5, 1e-9) * 10_000
        volume_z = last_value(rolling_zscore([c.volume for c in context.candles_5m], 20)) or 0.0
        atr_last = last_value(atr_15m) or 0.0
        atr_pct_15m = atr_last / max(closes_15m[-1], 1e-9) * 100
        return VolatilitySnapshot(
            pair=context.symbol,
            ts=context.candles_5m[-1].ts,
            atr_pct_15m=atr_pct_15m,
            spread_bps=context.order_book.spread_bps,
            vol_z_5m=volume_z,
            adx_15m=last_value(adx_15m) or 0.0,
            ema20_15m=last_value(ema20_15m) or 0.0,
            ema50_15m=last_value(ema50_15m) or 0.0,
            vwap_dist_bps=vwap_dist_bps,
            imbalance_1m=context.order_book.imbalance,
        )

    def _market_rejections(
        self,
        context: MarketContext,
        snapshot: VolatilitySnapshot,
        checks: list[StrategyCheck],
    ) -> list[str]:
        reasons: list[str] = []
        checks.append(
            StrategyCheck(
                name="mr_spread_bps",
                passed=snapshot.spread_bps <= min(self.config.max_spread_bps, 10.0),
                threshold=f"<= {min(self.config.max_spread_bps, 10.0):.2f} bps",
                value=round(snapshot.spread_bps, 4),
                reason="mr_spread_too_wide",
            )
        )
        if not checks[-1].passed:
            reasons.append("mr_spread_too_wide")

        atr_pct_rank = percentile_rank(context.atr_pct_history_15m, snapshot.atr_pct_15m)
        checks.append(
            StrategyCheck(
                name="mr_atr_percentile_15m",
                passed=35.0 <= atr_pct_rank <= 92.0,
                threshold="35.0-92.0",
                value=round(atr_pct_rank, 4),
                reason="mr_atr_percentile_out_of_range",
            )
        )
        if not checks[-1].passed:
            reasons.append("mr_atr_percentile_out_of_range")

        has_shock = BreakoutPullbackStrategy(self.config)._has_shock_candle(context)
        checks.append(
            StrategyCheck(
                name="mr_recent_shock_candle",
                passed=not has_shock,
                threshold=f"no 1m candle > {self.config.shock_candle_atr_multiple:.2f}x ATR in last 3 bars",
                value=has_shock,
                reason="mr_recent_shock_candle",
            )
        )
        if has_shock:
            reasons.append("mr_recent_shock_candle")
        return reasons

    def _evaluate_reclaim_candidate(self, context: MarketContext, snapshot: VolatilitySnapshot) -> SetupCandidate:
        candles_5m = context.candles_5m
        closes_5m = [candle.close for candle in candles_5m]
        ema9_values = ema(closes_5m, 9)
        rsi_values = rsi(closes_5m, 7)
        atr_5m = atr(candles_5m, 14)
        latest = candles_5m[-1]
        prev = candles_5m[-2]
        latest_ema9 = last_value(ema9_values)
        latest_rsi = last_value(rsi_values)
        latest_atr5 = last_value(atr_5m)
        vwap_20 = rolling_vwap(candles_5m, 20)

        checks: list[StrategyCheck] = []
        if latest_ema9 is None or latest_rsi is None or latest_atr5 is None:
            checks.append(
                StrategyCheck(
                    name="mr_indicator_readiness",
                    passed=False,
                    threshold="EMA9, RSI7 and ATR14 available",
                    value="missing",
                    reason="mr_indicators_not_ready",
                )
            )
            return SetupCandidate(intent=None, checks=tuple(checks))

        vwap_gap_bps = ((vwap_20 - prev.close) / max(prev.close, 1e-9)) * 10_000
        checks.append(
            StrategyCheck(
                name="mr_vwap_dislocation",
                passed=vwap_gap_bps >= 6.0,
                threshold=">= 6.0 bps below VWAP on prior bar",
                value=round(vwap_gap_bps, 4),
                reason="mr_no_vwap_dislocation",
            )
        )
        checks.append(
            StrategyCheck(
                name="mr_rsi_oversold",
                passed=latest_rsi <= 52.0,
                threshold="<= 52.0",
                value=round(latest_rsi, 4),
                reason="mr_rsi_too_hot",
            )
        )
        checks.append(
            StrategyCheck(
                name="mr_reclaim_close",
                passed=latest.close > vwap_20 and latest.close > latest_ema9 and latest.close > latest.open,
                threshold="close back above VWAP and EMA9",
                value=round(latest.close, 4),
                reason="mr_no_reclaim_close",
            )
        )
        checks.append(
            StrategyCheck(
                name="mr_orderbook_imbalance",
                passed=snapshot.imbalance_1m >= 1.03,
                threshold=">= 1.03",
                value=round(snapshot.imbalance_1m, 4),
                reason="mr_imbalance_too_low",
            )
        )
        if any(not check.passed for check in checks):
            return SetupCandidate(intent=None, checks=tuple(checks))

        pullback_low = min(prev.low, latest.low)
        stop_price = max(min(pullback_low, latest.close - (0.9 * latest_atr5)), latest.close * (1.0 - 0.0095))
        if stop_price >= context.order_book.best_ask:
            checks.append(
                StrategyCheck(
                    name="mr_valid_stop",
                    passed=False,
                    threshold="stop_price < entry_price",
                    value=round(context.order_book.best_ask - stop_price, 8),
                    reason="mr_invalid_stop",
                )
            )
            return SetupCandidate(intent=None, checks=tuple(checks))

        score = min(
            100.0,
            max(0.0, 18.0 - snapshot.spread_bps)
            + min(max(snapshot.vol_z_5m, 0.0) * 4.0, 12.0)
            + min(max(snapshot.imbalance_1m - 1.0, 0.0) * 40.0, 18.0)
            + max(0.0, 58.0 - latest_rsi)
            + max(0.0, min(vwap_gap_bps, 18.0))
            + 12.0,
        )
        quality = self.config.classify_quality(score)
        intent = DayTradeIntent(
            pair=context.symbol,
            entry_zone=context.order_book.best_ask,
            stop_price=stop_price,
            trail_activation_r=0.95,
            max_hold_min=60,
            budget_eur=0.0,
            reason_code=f"mean_reversion_vwap:{vwap_20:.2f}",
            score=score,
            quality=quality,
            setup_type="mean_reversion_vwap",
            regime_label="mean_reversion",
            strategy_id=self.strategy_id,
            strategy_family=self.strategy_family,
            break_even_trigger_r=0.65,
            time_decay_minutes=25,
            time_decay_min_r=0.10,
        )
        return SetupCandidate(intent=intent, checks=tuple(checks))
