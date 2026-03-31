import unittest
from dataclasses import replace

from daytrading_bot.config import BotConfig
from daytrading_bot.models import MarketContext, OrderBookSnapshot
from daytrading_bot.strategy import (
    BreakoutPullbackStrategy,
    FastLiquiditySweepReclaimStrategy,
    FastMicroScalpStrategy,
    FastVwapReclaimScalpStrategy,
    OpeningRangeBreakoutStrategy,
    TrendContinuationPullbackStrategy,
)
from tests.helpers import (
    build_context,
    build_fast_micro_context,
    build_fast_sweep_context,
    build_fast_vwap_context,
    build_recovery_context,
)


class StrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = BotConfig()
        self.strategy = BreakoutPullbackStrategy(self.config)

    def test_strategy_produces_entry_on_valid_breakout_pullback(self) -> None:
        context = build_context()
        intent = self.strategy.evaluate(context)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.pair, "XBTEUR")
        self.assertGreater(intent.score, 70.0)
        self.assertLess(intent.stop_price, intent.entry_zone)
        self.assertTrue(intent.reason_code.startswith("breakout_pullback:"))

    def test_strategy_produces_entry_on_valid_recovery_reclaim(self) -> None:
        context = build_recovery_context()
        intent = self.strategy.evaluate(context)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.pair, "XBTEUR")
        self.assertLess(intent.stop_price, intent.entry_zone)
        self.assertTrue(intent.reason_code.startswith("recovery_reclaim:"))
        self.assertEqual(intent.setup_type, "recovery_reclaim")
        self.assertEqual(intent.max_hold_min, self.config.recovery_max_hold_minutes)
        self.assertEqual(intent.break_even_trigger_r, self.config.recovery_break_even_trigger_r)

    def test_recovery_strategy_rejects_score_below_minimum(self) -> None:
        strategy = BreakoutPullbackStrategy(replace(self.config, recovery_min_score=75.0))
        evaluation = strategy.evaluate_detailed(build_recovery_context())
        self.assertIsNone(evaluation.intent)
        self.assertIn("recovery_score_too_low", evaluation.rejection_reasons)

    def test_recovery_strategy_accepts_same_context_under_looser_minimum(self) -> None:
        strict_strategy = BreakoutPullbackStrategy(replace(self.config, recovery_min_score=75.0))
        loose_strategy = BreakoutPullbackStrategy(replace(self.config, recovery_min_score=55.0))
        context = build_recovery_context()

        strict_evaluation = strict_strategy.evaluate_detailed(context)
        loose_evaluation = loose_strategy.evaluate_detailed(context)

        self.assertIsNone(strict_evaluation.intent)
        self.assertIsNotNone(loose_evaluation.intent)
        self.assertEqual(loose_evaluation.intent.setup_type, "recovery_reclaim")
        self.assertGreaterEqual(loose_evaluation.intent.score, 55.0)

    def test_strategy_rejects_wide_spread(self) -> None:
        context = build_context()
        context = MarketContext(
            symbol=context.symbol,
            candles_1m=context.candles_1m,
            candles_5m=context.candles_5m,
            candles_15m=context.candles_15m,
            order_book=OrderBookSnapshot(
                symbol=context.symbol,
                best_bid=context.order_book.best_bid - 1.0,
                best_ask=context.order_book.best_ask + 1.0,
                bid_volume_top5=context.order_book.bid_volume_top5,
                ask_volume_top5=context.order_book.ask_volume_top5,
            ),
            atr_pct_history_15m=context.atr_pct_history_15m,
        )
        self.assertIsNone(self.strategy.evaluate(context))

    def test_diagnostics_exposes_spread_rejection_reason(self) -> None:
        context = build_context()
        context = MarketContext(
            symbol=context.symbol,
            candles_1m=context.candles_1m,
            candles_5m=context.candles_5m,
            candles_15m=context.candles_15m,
            order_book=OrderBookSnapshot(
                symbol=context.symbol,
                best_bid=context.order_book.best_bid - 1.0,
                best_ask=context.order_book.best_ask + 1.0,
                bid_volume_top5=context.order_book.bid_volume_top5,
                ask_volume_top5=context.order_book.ask_volume_top5,
            ),
            atr_pct_history_15m=context.atr_pct_history_15m,
        )
        evaluation = self.strategy.evaluate_detailed(context)
        self.assertIsNone(evaluation.intent)
        self.assertIn("spread_too_wide", evaluation.rejection_reasons)

    def test_diagnostics_reports_insufficient_history(self) -> None:
        context = build_context()
        trimmed = MarketContext(
            symbol=context.symbol,
            candles_1m=context.candles_1m[:10],
            candles_5m=context.candles_5m[:10],
            candles_15m=context.candles_15m[:10],
            order_book=context.order_book,
            atr_pct_history_15m=context.atr_pct_history_15m[:5],
        )
        evaluation = self.strategy.evaluate_detailed(trimmed)
        self.assertIsNone(evaluation.intent)
        self.assertIn("insufficient_history_1m", evaluation.rejection_reasons)
        self.assertIn("insufficient_history_5m", evaluation.rejection_reasons)
        self.assertIn("insufficient_history_15m", evaluation.rejection_reasons)

    def test_opening_range_breakout_strategy_produces_entry(self) -> None:
        strategy = OpeningRangeBreakoutStrategy(self.config)
        context = build_context()

        intent = strategy.evaluate(context)

        self.assertIsNotNone(intent)
        self.assertEqual(intent.setup_type, "opening_range_breakout")
        self.assertEqual(intent.regime_label, "opening_range")
        self.assertLess(intent.stop_price, intent.entry_zone)
        self.assertTrue(intent.reason_code.startswith("opening_range_breakout:"))

    def test_trend_continuation_pullback_strategy_produces_entry(self) -> None:
        strategy = TrendContinuationPullbackStrategy(self.config)
        context = build_context()

        intent = strategy.evaluate(context)

        self.assertIsNotNone(intent)
        self.assertEqual(intent.setup_type, "trend_continuation_pullback")
        self.assertEqual(intent.regime_label, "trend_continuation")
        self.assertLess(intent.stop_price, intent.entry_zone)
        self.assertTrue(intent.reason_code.startswith("trend_continuation_pullback:"))

    def test_fast_micro_scalp_strategy_produces_entry_when_microstructure_aligns(self) -> None:
        strategy = FastMicroScalpStrategy(self.config)
        context = build_fast_micro_context()

        intent = strategy.evaluate(context)

        self.assertIsNotNone(intent)
        self.assertEqual(intent.setup_type, "fast_micro_scalp")
        self.assertEqual(intent.regime_label, "fast_trading")
        self.assertLess(intent.stop_price, intent.entry_zone)
        self.assertTrue(intent.reason_code.startswith("fast_micro_scalp:"))

    def test_fast_micro_scalp_strategy_rejects_without_micro_windows(self) -> None:
        strategy = FastMicroScalpStrategy(self.config)
        context = build_context()

        evaluation = strategy.evaluate_detailed(context)

        self.assertIsNone(evaluation.intent)
        self.assertIn("fast_not_enough_micro_samples", evaluation.rejection_reasons)

    def test_fast_liquidity_sweep_reclaim_strategy_produces_entry(self) -> None:
        strategy = FastLiquiditySweepReclaimStrategy(self.config)
        context = build_fast_sweep_context()

        intent = strategy.evaluate(context)

        self.assertIsNotNone(intent)
        self.assertEqual(intent.setup_type, "fast_liquidity_sweep_reclaim")
        self.assertEqual(intent.regime_label, "fast_trading")
        self.assertTrue(intent.reason_code.startswith("fast_liquidity_sweep_reclaim:"))

    def test_fast_vwap_reclaim_scalp_strategy_produces_entry(self) -> None:
        strategy = FastVwapReclaimScalpStrategy(self.config)
        context = build_fast_vwap_context()

        intent = strategy.evaluate(context)

        self.assertIsNotNone(intent)
        self.assertEqual(intent.setup_type, "fast_vwap_reclaim_scalp")
        self.assertEqual(intent.regime_label, "fast_trading")
        self.assertTrue(intent.reason_code.startswith("fast_vwap_reclaim_scalp:"))


if __name__ == "__main__":
    unittest.main()
