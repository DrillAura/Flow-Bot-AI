from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Iterable

from .config import BotConfig, ThreeCommasConfig
from .execution import ThreeCommasSignalClient
from .indicators import atr, ema, last_value
from .models import ActiveTrade, DayTradeIntent, MarketContext
from .risk import RiskController
from .sessions import is_hard_flat_time, is_trade_window
from .shadow_portfolios import ShadowPortfolioLab
from .signal_observatory import SignalObservatory
from .strategy import BreakoutPullbackStrategy, TradingStrategy
from .strategy_lab import StrategyPaperLab, StrategyRuntimeSelector
from .telemetry import JsonlTelemetry


@dataclass(frozen=True)
class _ExecutionEstimate:
    fill_price: float
    fee_rate: float
    fee_eur: float
    slippage_bps: float
    maker_probability: float
    liquidity_role: str


class BotEngine:
    def __init__(
        self,
        bot_config: BotConfig,
        execution_config: ThreeCommasConfig,
        telemetry: JsonlTelemetry | None = None,
        enable_research: bool = False,
        strategy: TradingStrategy | None = None,
    ) -> None:
        self.bot_config = bot_config
        self.execution_config = execution_config
        self.execution = ThreeCommasSignalClient(bot_config, execution_config)
        self.risk = RiskController(bot_config)
        self.strategy_router = StrategyRuntimeSelector(bot_config, execution_config)
        self._strategy_pinned = strategy is not None
        if strategy is not None:
            self.strategy = strategy
            self.strategy_router.active_strategy_id = getattr(strategy, "strategy_id", bot_config.active_strategy_id)
            self.strategy_router.strategy = strategy
        else:
            self.strategy = self.strategy_router.strategy or BreakoutPullbackStrategy(bot_config)
        self.telemetry = telemetry or JsonlTelemetry(bot_config.telemetry_path)
        self.enable_research = enable_research
        self.signal_observatory = SignalObservatory(self.telemetry) if enable_research else None
        self.shadow_lab = ShadowPortfolioLab(bot_config, self.telemetry) if enable_research else None
        self.strategy_lab = StrategyPaperLab(bot_config, self.telemetry) if enable_research else None

    def process_market(
        self,
        contexts: Iterable[MarketContext],
        available_eur: float,
        moment: datetime,
    ) -> list[dict[str, str]]:
        contexts = list(contexts)
        events: list[dict[str, str]] = []
        context_map = {context.symbol: context for context in contexts}
        self.risk.roll_day(moment)
        closed_trade_this_tick = False
        session_open = is_trade_window(moment, self.bot_config)

        if not self._strategy_pinned and self.strategy is not self.strategy_router.strategy:
            self._strategy_pinned = True
        if not self._strategy_pinned:
            self.strategy_router.maybe_refresh(active_trade_present=self.risk.state.active_trade is not None)
            self.strategy = self.strategy_router.strategy

        if self.shadow_lab is not None:
            self.shadow_lab.process_market(contexts, moment)
        if self.strategy_lab is not None:
            self.strategy_lab.process_market(contexts, moment)

        if self.risk.state.active_trade is not None:
            managed_event = self._manage_active_trade(context_map, moment)
            if managed_event is not None:
                events.append(managed_event)
                closed_trade_this_tick = self.risk.state.active_trade is None

        evaluations = self._evaluate_contexts(contexts) if session_open else []
        if self.signal_observatory is not None and evaluations:
            self.signal_observatory.capture(
                evaluations,
                moment=moment,
                session_open=session_open,
                active_trade_present=self.risk.state.active_trade is not None,
                closed_trade_this_tick=closed_trade_this_tick,
            )

        if self.risk.state.active_trade is not None or closed_trade_this_tick:
            return events

        if not session_open:
            return events

        candidates: list[DayTradeIntent] = []
        for _, evaluation in evaluations:
            if evaluation.intent is not None:
                intent = evaluation.intent
                candidates.append(intent)

        if not candidates:
            return events

        best = max(candidates, key=lambda item: item.score)
        can_open, reason = self.risk.can_open_trade(moment, best.quality)
        if not can_open:
            self.telemetry.log(
                "entry_rejected",
                {
                    "pair": best.pair,
                    "reason": reason,
                    "setup_type": best.setup_type,
                    "quality": best.quality,
                    "score": best.score,
                    "reason_code": best.reason_code,
                },
                event_ts=moment,
            )
            return events

        budget = self.risk.position_budget(best.entry_zone, best.stop_price, available_eur)
        pair_config = self.bot_config.pair_by_symbol(best.pair)
        if budget < pair_config.min_notional_eur:
            self.telemetry.log("entry_rejected", {"pair": best.pair, "reason": "min_notional", "budget": budget})
            return events

        final_intent = replace(best, budget_eur=round(budget, 2))
        entry_context = context_map.get(final_intent.pair)
        if entry_context is None:
            return events
        entry_execution = self._estimate_entry_execution(final_intent, entry_context)
        live_allowed, live_reason = self.execution.validate_entry_intent(final_intent)
        if not live_allowed:
            self.telemetry.log(
                "entry_rejected",
                {
                    "pair": final_intent.pair,
                    "reason": live_reason,
                    "setup_type": final_intent.setup_type,
                    "quality": final_intent.quality,
                    "score": final_intent.score,
                },
                event_ts=moment,
            )
            return events
        response = self.execution.send(self.execution.build_entry_payload(final_intent))
        trade = ActiveTrade(
            pair=final_intent.pair,
            entry_ts=moment,
            entry_price=entry_execution.fill_price,
            initial_stop_price=final_intent.stop_price,
            stop_price=final_intent.stop_price,
            budget_eur=final_intent.budget_eur,
            reason_code=final_intent.reason_code,
            max_hold_min=final_intent.max_hold_min,
            trail_activation_r=final_intent.trail_activation_r,
            setup_type=final_intent.setup_type,
            regime_label=final_intent.regime_label,
            strategy_id=final_intent.strategy_id,
            strategy_family=final_intent.strategy_family,
            quality=final_intent.quality,
            score=final_intent.score,
            break_even_trigger_r=final_intent.break_even_trigger_r,
            time_decay_minutes=final_intent.time_decay_minutes,
            time_decay_min_r=final_intent.time_decay_min_r,
            entry_liquidity_role=entry_execution.liquidity_role,
            entry_fee_rate=entry_execution.fee_rate,
            expected_exit_fee_rate=self.bot_config.paper_taker_fee_rate if self.execution_config.mode != "live" else self.bot_config.quote_fee_rate,
            entry_fee_eur=entry_execution.fee_eur,
            entry_slippage_bps=entry_execution.slippage_bps,
            entry_maker_probability=entry_execution.maker_probability,
        )
        trade.best_price_seen = trade.entry_price
        trade.worst_price_seen = trade.entry_price
        trade.append_replay_point(moment, trade.entry_price, realized_pnl_hint=-trade.entry_fee_eur)
        self.risk.record_trade_opened(trade, moment)
        self.telemetry.log(
            "entry_sent",
            {
                "intent": final_intent,
                "response": response,
                "market_ts": moment.isoformat(),
                "fill_price": round(entry_execution.fill_price, 8),
                "fee_rate": round(entry_execution.fee_rate, 6),
                "fee_eur": round(entry_execution.fee_eur, 6),
                "slippage_bps": round(entry_execution.slippage_bps, 4),
                "maker_probability": round(entry_execution.maker_probability, 4),
                "liquidity_role": entry_execution.liquidity_role,
            },
            event_ts=moment,
        )
        events.append({"type": "entry", "pair": final_intent.pair})
        return events

    def _evaluate_contexts(self, contexts: Iterable[MarketContext]) -> list[tuple[MarketContext, object]]:
        evaluations: list[tuple[MarketContext, object]] = []
        for context in contexts:
            if hasattr(self.strategy, "evaluate_detailed"):
                evaluation = self.strategy.evaluate_detailed(context)
            else:
                intent = self.strategy.evaluate(context)
                evaluation = type(
                    "FallbackEvaluation",
                    (),
                    {"intent": intent, "snapshot": None, "rejection_reasons": tuple(), "checks": tuple()},
                )()
            evaluations.append((context, evaluation))
        return evaluations

    def _manage_active_trade(self, context_map: dict[str, MarketContext], moment: datetime) -> dict[str, str] | None:
        trade = self.risk.state.active_trade
        if trade is None:
            return None
        context = context_map.get(trade.pair)
        if context is None:
            return None

        current_price = context.order_book.best_bid
        exit_execution = self._estimate_exit_execution(trade, context)
        self._update_trade_replay(trade, context, moment)
        unrealized = trade.unrealized_pnl(exit_execution.fill_price, fee_rate=exit_execution.fee_rate)
        r_multiple = trade.r_multiple(current_price)
        self.risk.mark_to_market(unrealized, moment)

        if self.risk.state.lock_state == "killed":
            response = self.execution.send(self.execution.build_disable_payload(market_close=True))
            pnl = self._estimate_realized_pnl(trade, exit_execution.fill_price, exit_execution.fee_rate)
            trade.exit_liquidity_role = exit_execution.liquidity_role
            trade.exit_slippage_bps = exit_execution.slippage_bps
            trade.exit_maker_probability = exit_execution.maker_probability
            trade.append_replay_point(moment, exit_execution.fill_price, realized_pnl_hint=pnl)
            self.risk.record_trade_closed(pnl, moment)
            realized_r = trade.r_multiple(exit_execution.fill_price)
            self.telemetry.log("kill_switch_exit", self._build_exit_payload_log(trade, exit_execution, pnl, "kill_switch_exit", response, moment, realized_r), event_ts=moment)
            return {"type": "kill_switch_exit", "pair": trade.pair}

        self._update_protective_stop(trade, context, r_multiple)
        hard_flat = is_hard_flat_time(moment, self.bot_config)
        timed_out = moment >= (trade.entry_ts + timedelta(minutes=trade.max_hold_min))
        time_decay = self._time_decay_triggered(trade, moment, r_multiple)
        stopped = current_price <= trade.stop_price
        if not (hard_flat or timed_out or time_decay or stopped):
            return None

        response = self.execution.send(self.execution.build_exit_payload(trade, current_price))
        pnl = self._estimate_realized_pnl(trade, exit_execution.fill_price, exit_execution.fee_rate)
        trade.exit_liquidity_role = exit_execution.liquidity_role
        trade.exit_slippage_bps = exit_execution.slippage_bps
        trade.exit_maker_probability = exit_execution.maker_probability
        trade.append_replay_point(moment, exit_execution.fill_price, realized_pnl_hint=pnl)
        self.risk.record_trade_closed(pnl, moment)
        reason = "session_flat" if hard_flat else "time_stop" if timed_out else "time_decay_exit" if time_decay else "protective_stop"
        realized_r = trade.r_multiple(exit_execution.fill_price)
        self.telemetry.log("exit_sent", self._build_exit_payload_log(trade, exit_execution, pnl, reason, response, moment, realized_r), event_ts=moment)
        return {"type": reason, "pair": trade.pair}

    def _update_protective_stop(self, trade: ActiveTrade, context: MarketContext, r_multiple: float) -> None:
        current_price = context.order_book.best_bid
        if r_multiple >= trade.break_even_trigger_r:
            fee_buffer_pct = max(
                self.bot_config.break_even_fee_buffer_pct,
                (self.bot_config.quote_fee_rate * 2.0) + 0.0005,
            )
            break_even = trade.entry_price * (1.0 + fee_buffer_pct)
            if current_price >= break_even:
                trade.stop_price = max(trade.stop_price, break_even)

        if r_multiple < trade.trail_activation_r:
            return

        closes_5m = [c.close for c in context.candles_5m]
        ema9 = last_value(ema(closes_5m, 9))
        atr5 = last_value(atr(context.candles_5m, 14))
        if ema9 is None or atr5 is None:
            return

        trade.trailing_enabled = True
        trailing_stop = ema9 - (self.bot_config.trail_atr_multiple * atr5)
        trade.stop_price = max(trade.stop_price, trailing_stop)

    def _time_decay_triggered(self, trade: ActiveTrade, moment: datetime, r_multiple: float) -> bool:
        if trade.time_decay_minutes <= 0:
            return False
        decay_deadline = trade.entry_ts + timedelta(minutes=trade.time_decay_minutes)
        return moment >= decay_deadline and r_multiple < trade.time_decay_min_r

    def _build_exit_payload_log(
        self,
        trade: ActiveTrade,
        exit_execution: _ExecutionEstimate,
        pnl: float,
        reason: str,
        response: dict[str, object],
        moment: datetime,
        r_multiple: float,
    ) -> dict[str, object]:
        return {
            "pair": trade.pair,
            "market_ts": moment.isoformat(),
            "entry_market_ts": trade.entry_ts.isoformat(),
            "price": exit_execution.fill_price,
            "entry_price": trade.entry_price,
            "initial_stop_price": trade.initial_stop_price,
            "stop_price": trade.stop_price,
            "budget_eur": trade.budget_eur,
            "reason": reason,
            "pnl_eur": pnl,
            "reason_code": trade.reason_code,
            "setup_type": trade.setup_type,
            "regime_label": trade.regime_label,
            "strategy_id": trade.strategy_id,
            "strategy_family": trade.strategy_family,
            "quality": trade.quality,
            "score": trade.score,
            "hold_minutes": max((moment - trade.entry_ts).total_seconds() / 60.0, 0.0),
            "r_multiple": r_multiple,
            "trailing_enabled": trade.trailing_enabled,
            "mae_r": trade.mae_r,
            "mfe_r": trade.mfe_r,
            "entry_fee_rate": trade.entry_fee_rate,
            "entry_fee_eur": trade.entry_fee_eur,
            "exit_fee_rate": exit_execution.fee_rate,
            "exit_fee_eur": exit_execution.fee_eur,
            "total_fee_eur": trade.entry_fee_eur + exit_execution.fee_eur,
            "entry_slippage_bps": trade.entry_slippage_bps,
            "exit_slippage_bps": exit_execution.slippage_bps,
            "entry_liquidity_role": trade.entry_liquidity_role,
            "exit_liquidity_role": exit_execution.liquidity_role,
            "entry_maker_probability": trade.entry_maker_probability,
            "exit_maker_probability": exit_execution.maker_probability,
            "replay_points": list(trade.replay_points[-180:]),
            "response": response,
        }

    def _estimate_realized_pnl(self, trade: ActiveTrade, exit_price: float, exit_fee_rate: float) -> float:
        gross = trade.budget_eur * ((exit_price / trade.entry_price) - 1.0)
        fees = trade.entry_fee_eur + (trade.budget_eur * exit_fee_rate)
        return gross - fees

    def _update_trade_replay(self, trade: ActiveTrade, context: MarketContext, moment: datetime) -> None:
        candle = context.candles_1m[-1] if context.candles_1m else None
        if candle is None:
            high_price = context.order_book.best_bid
            low_price = context.order_book.best_bid
        else:
            high_price = max(context.order_book.best_bid, candle.high)
            low_price = min(context.order_book.best_bid, candle.low)
        trade.update_extrema(high_price, low_price)
        trade.append_replay_point(moment, context.order_book.best_bid)

    def _estimate_entry_execution(self, intent: DayTradeIntent, context: MarketContext) -> _ExecutionEstimate:
        pair_config = self.bot_config.pair_by_symbol(intent.pair)
        if self.execution_config.mode == "live":
            return _ExecutionEstimate(
                fill_price=intent.entry_zone,
                fee_rate=self.bot_config.quote_fee_rate,
                fee_eur=intent.budget_eur * self.bot_config.quote_fee_rate,
                slippage_bps=0.0,
                maker_probability=0.0,
                liquidity_role="live_estimate",
            )
        spread_bps = max(context.order_book.spread_bps, 0.0)
        maker_probability = self._paper_maker_probability(
            context=context,
            quality=intent.quality,
            cap=pair_config.paper_entry_maker_probability_cap,
        )
        blended_fee_rate = self._blended_fee_rate(maker_probability)
        slippage_bps = max(
            pair_config.paper_min_entry_slippage_bps,
            spread_bps * pair_config.paper_entry_slippage_spread_weight * max(0.25, 1.0 - maker_probability),
        )
        reference_price = max(intent.entry_zone, context.order_book.best_ask)
        fill_price = reference_price * (1.0 + (slippage_bps / 10_000.0))
        return _ExecutionEstimate(
            fill_price=fill_price,
            fee_rate=blended_fee_rate,
            fee_eur=intent.budget_eur * blended_fee_rate,
            slippage_bps=slippage_bps,
            maker_probability=maker_probability,
            liquidity_role="maker_blend" if maker_probability >= 0.20 else "taker_bias",
        )

    def _estimate_exit_execution(self, trade: ActiveTrade, context: MarketContext) -> _ExecutionEstimate:
        pair_config = self.bot_config.pair_by_symbol(trade.pair)
        if self.execution_config.mode == "live":
            return _ExecutionEstimate(
                fill_price=context.order_book.best_bid,
                fee_rate=self.bot_config.quote_fee_rate,
                fee_eur=trade.budget_eur * self.bot_config.quote_fee_rate,
                slippage_bps=0.0,
                maker_probability=0.0,
                liquidity_role="live_estimate",
            )
        maker_probability = self._paper_maker_probability(
            context=context,
            quality=trade.quality,
            cap=pair_config.paper_exit_maker_probability_cap,
        )
        blended_fee_rate = self._blended_fee_rate(maker_probability)
        slippage_bps = max(
            pair_config.paper_min_exit_slippage_bps,
            max(context.order_book.spread_bps, 0.0) * pair_config.paper_exit_slippage_spread_weight * max(0.35, 1.0 - maker_probability),
        )
        fill_price = context.order_book.best_bid * (1.0 - (slippage_bps / 10_000.0))
        return _ExecutionEstimate(
            fill_price=fill_price,
            fee_rate=blended_fee_rate,
            fee_eur=trade.budget_eur * blended_fee_rate,
            slippage_bps=slippage_bps,
            maker_probability=maker_probability,
            liquidity_role="maker_blend" if maker_probability >= 0.12 else "taker_bias",
        )

    def _paper_maker_probability(self, *, context: MarketContext, quality: str, cap: float) -> float:
        spread_score = max(0.0, min((10.0 - context.order_book.spread_bps) / 10.0, 1.0))
        imbalance_score = max(0.0, min((context.order_book.imbalance - 1.0) / 0.5, 1.0))
        quality_bonus = {"A": 0.14, "B": 0.07, "C": 0.02}.get(str(quality or "").upper(), 0.0)
        probability = 0.04 + (spread_score * 0.16) + (imbalance_score * 0.14) + quality_bonus
        return max(0.0, min(cap, probability))

    def _blended_fee_rate(self, maker_probability: float) -> float:
        return (
            maker_probability * self.bot_config.paper_maker_fee_rate
            + (1.0 - maker_probability) * self.bot_config.paper_taker_fee_rate
        )
