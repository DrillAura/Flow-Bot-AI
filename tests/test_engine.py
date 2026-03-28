import tempfile
import unittest
from datetime import datetime

from daytrading_bot.config import BotConfig, ThreeCommasConfig
from daytrading_bot.backtest import build_backtest_trade_logs
from daytrading_bot.engine import BotEngine
from daytrading_bot.models import DayTradeIntent, MarketContext, OrderBookSnapshot
from daytrading_bot.telemetry import InMemoryTelemetry
from tests.helpers import build_context, build_recovery_context


class _StubStrategy:
    def __init__(self, intent: DayTradeIntent | None) -> None:
        self.intent = intent

    def evaluate(self, context: MarketContext) -> DayTradeIntent | None:
        return self.intent


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.bot_config = BotConfig(telemetry_path=f"{self.tempdir.name}/events.jsonl")
        self.exec_config = ThreeCommasConfig(secret="secret", bot_uuid="bot", mode="paper")
        self.engine = BotEngine(self.bot_config, self.exec_config)

    def test_engine_enters_trade_when_context_is_valid(self) -> None:
        context = build_context()
        moment = datetime(2026, 3, 23, 9, 30, tzinfo=self.bot_config.timezone)
        events = self.engine.process_market([context], available_eur=100.0, moment=moment)
        self.assertEqual(events[0]["type"], "entry")
        self.assertIsNotNone(self.engine.risk.state.active_trade)

    def test_engine_exits_at_hard_flat(self) -> None:
        context = build_context()
        entry_moment = datetime(2026, 3, 23, 9, 30, tzinfo=self.bot_config.timezone)
        self.engine.process_market([context], available_eur=100.0, moment=entry_moment)
        self.assertIsNotNone(self.engine.risk.state.active_trade)

        active = self.engine.risk.state.active_trade
        exit_context = MarketContext(
            symbol=context.symbol,
            candles_1m=context.candles_1m,
            candles_5m=context.candles_5m,
            candles_15m=context.candles_15m,
            order_book=OrderBookSnapshot(
                symbol=context.symbol,
                best_bid=active.entry_price + 0.5,
                best_ask=active.entry_price + 0.55,
                bid_volume_top5=context.order_book.bid_volume_top5,
                ask_volume_top5=context.order_book.ask_volume_top5,
            ),
            atr_pct_history_15m=context.atr_pct_history_15m,
        )
        exit_moment = datetime(2026, 3, 23, 21, 31, tzinfo=self.bot_config.timezone)
        events = self.engine.process_market([exit_context], available_eur=100.0, moment=exit_moment)
        self.assertEqual(events[0]["type"], "session_flat")
        self.assertIsNone(self.engine.risk.state.active_trade)

    def test_engine_does_not_reenter_on_same_tick_after_stop_exit(self) -> None:
        context = build_context()
        entry_moment = datetime(2026, 3, 23, 9, 30, tzinfo=self.bot_config.timezone)
        self.engine.process_market([context], available_eur=100.0, moment=entry_moment)
        active = self.engine.risk.state.active_trade
        self.assertIsNotNone(active)

        active.stop_price = active.entry_price + 1.0
        stop_context = build_context()
        stop_context = MarketContext(
            symbol=stop_context.symbol,
            candles_1m=stop_context.candles_1m,
            candles_5m=stop_context.candles_5m,
            candles_15m=stop_context.candles_15m,
            order_book=OrderBookSnapshot(
                symbol=stop_context.symbol,
                best_bid=active.entry_price - 0.5,
                best_ask=active.entry_price - 0.45,
                bid_volume_top5=stop_context.order_book.bid_volume_top5,
                ask_volume_top5=stop_context.order_book.ask_volume_top5,
            ),
            atr_pct_history_15m=stop_context.atr_pct_history_15m,
        )

        events = self.engine.process_market([stop_context], available_eur=100.0, moment=datetime(2026, 3, 23, 10, 0, tzinfo=self.bot_config.timezone))
        self.assertEqual(events[0]["type"], "protective_stop")
        self.assertEqual(len(events), 1)
        self.assertIsNone(self.engine.risk.state.active_trade)

    def test_recovery_trade_uses_time_decay_exit(self) -> None:
        context = build_recovery_context()
        entry_moment = datetime(2026, 3, 23, 15, 0, tzinfo=self.bot_config.timezone)
        entry_events = self.engine.process_market([context], available_eur=100.0, moment=entry_moment)
        self.assertEqual(entry_events[0]["type"], "entry")

        active = self.engine.risk.state.active_trade
        self.assertIsNotNone(active)
        self.assertEqual(active.setup_type, "recovery_reclaim")

        decay_context = MarketContext(
            symbol=context.symbol,
            candles_1m=context.candles_1m,
            candles_5m=context.candles_5m,
            candles_15m=context.candles_15m,
            order_book=OrderBookSnapshot(
                symbol=context.symbol,
                best_bid=active.entry_price + 0.01,
                best_ask=active.entry_price + 0.03,
                bid_volume_top5=context.order_book.bid_volume_top5,
                ask_volume_top5=context.order_book.ask_volume_top5,
            ),
            atr_pct_history_15m=context.atr_pct_history_15m,
        )
        exit_moment = datetime(2026, 3, 23, 15, 31, tzinfo=self.bot_config.timezone)
        exit_events = self.engine.process_market([decay_context], available_eur=100.0, moment=exit_moment)
        self.assertEqual(exit_events[0]["type"], "time_decay_exit")
        self.assertIsNone(self.engine.risk.state.active_trade)

    def test_break_even_requires_price_to_clear_fee_buffer(self) -> None:
        context = build_recovery_context()
        entry_moment = datetime(2026, 3, 23, 15, 0, tzinfo=self.bot_config.timezone)
        entry_events = self.engine.process_market([context], available_eur=100.0, moment=entry_moment)
        self.assertEqual(entry_events[0]["type"], "entry")

        active = self.engine.risk.state.active_trade
        self.assertIsNotNone(active)
        active.initial_stop_price = active.entry_price - 0.05
        active.stop_price = active.initial_stop_price
        original_stop = active.stop_price

        current_price = active.entry_price + 0.04
        self.assertGreaterEqual(
            (current_price - active.entry_price) / (active.entry_price - active.initial_stop_price),
            active.break_even_trigger_r,
        )
        self.assertLess(current_price, active.entry_price * (1.0 + self.bot_config.break_even_fee_buffer_pct))

        manage_context = MarketContext(
            symbol=context.symbol,
            candles_1m=context.candles_1m,
            candles_5m=context.candles_5m,
            candles_15m=context.candles_15m,
            order_book=OrderBookSnapshot(
                symbol=context.symbol,
                best_bid=current_price - 0.01,
                best_ask=current_price,
                bid_volume_top5=context.order_book.bid_volume_top5,
                ask_volume_top5=context.order_book.ask_volume_top5,
            ),
            atr_pct_history_15m=context.atr_pct_history_15m,
        )

        events = self.engine.process_market(
            [manage_context],
            available_eur=100.0,
            moment=datetime(2026, 3, 23, 15, 5, tzinfo=self.bot_config.timezone),
        )
        self.assertEqual(events, [])
        self.assertIsNotNone(self.engine.risk.state.active_trade)
        self.assertEqual(self.engine.risk.state.active_trade.stop_price, original_stop)

    def test_live_mode_rejects_b_quality_recovery_entry(self) -> None:
        telemetry = InMemoryTelemetry()
        live_engine = BotEngine(
            self.bot_config,
            ThreeCommasConfig(secret="secret", bot_uuid="bot", mode="live", allow_live=True),
            telemetry=telemetry,
        )
        live_engine.strategy = _StubStrategy(
            DayTradeIntent(
                pair="XBTEUR",
                entry_zone=35000.0,
                stop_price=34600.0,
                trail_activation_r=0.9,
                max_hold_min=75,
                budget_eur=0.0,
                reason_code="recovery_reclaim:34990.0",
                score=65.0,
                quality="B",
                setup_type="recovery_reclaim",
                regime_label="recovery",
                break_even_trigger_r=0.6,
                time_decay_minutes=30,
                time_decay_min_r=0.15,
            )
        )

        events = live_engine.process_market(
            [build_context()],
            available_eur=100.0,
            moment=datetime(2026, 3, 23, 9, 30, tzinfo=self.bot_config.timezone),
        )
        self.assertEqual(events, [])
        self.assertIsNone(live_engine.risk.state.active_trade)
        self.assertEqual(len(telemetry.events), 1)
        self.assertEqual(telemetry.events[0]["event_type"], "entry_rejected")
        self.assertEqual(telemetry.events[0]["payload"]["reason"], "live_requires_a_recovery_quality")

    def test_build_backtest_trade_logs_extracts_exit_details(self) -> None:
        logs = build_backtest_trade_logs(
            [
                {
                    "event_type": "exit_sent",
                    "payload": {
                        "pair": "XBTEUR",
                        "setup_type": "recovery_reclaim",
                        "regime_label": "recovery",
                        "quality": "B",
                        "score": 61.5,
                        "entry_market_ts": "2026-03-23T15:00:00+01:00",
                        "market_ts": "2026-03-23T15:31:00+01:00",
                        "hold_minutes": 31.0,
                        "entry_price": 100.0,
                        "price": 99.8,
                        "initial_stop_price": 99.2,
                        "stop_price": 99.9,
                        "pnl_eur": -0.4,
                        "r_multiple": -0.25,
                        "reason": "time_decay_exit",
                        "reason_code": "recovery_reclaim:100.10",
                        "trailing_enabled": False,
                    },
                }
            ]
        )
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].exit_reason, "time_decay_exit")
        self.assertEqual(logs[0].setup_type, "recovery_reclaim")

    def test_paper_execution_logs_slippage_fees_and_replay_metrics(self) -> None:
        context = build_context()
        entry_moment = datetime(2026, 3, 23, 9, 30, tzinfo=self.bot_config.timezone)
        entry_events = self.engine.process_market([context], available_eur=100.0, moment=entry_moment)
        self.assertEqual(entry_events[0]["type"], "entry")
        active = self.engine.risk.state.active_trade
        self.assertIsNotNone(active)
        self.assertGreater(active.entry_fee_eur, 0.0)
        self.assertGreaterEqual(active.entry_slippage_bps, self.bot_config.paper_min_entry_slippage_bps)
        self.assertTrue(active.replay_points)

        exit_context = MarketContext(
            symbol=context.symbol,
            candles_1m=context.candles_1m,
            candles_5m=context.candles_5m,
            candles_15m=context.candles_15m,
            order_book=OrderBookSnapshot(
                symbol=context.symbol,
                best_bid=active.entry_price + 0.9,
                best_ask=active.entry_price + 0.95,
                bid_volume_top5=context.order_book.bid_volume_top5,
                ask_volume_top5=context.order_book.ask_volume_top5,
            ),
            atr_pct_history_15m=context.atr_pct_history_15m,
        )
        exit_events = self.engine.process_market([exit_context], available_eur=100.0, moment=datetime(2026, 3, 23, 21, 31, tzinfo=self.bot_config.timezone))
        self.assertEqual(exit_events[0]["type"], "session_flat")
        exit_payload = self.engine.telemetry.events[-1]["payload"]
        self.assertIn("mae_r", exit_payload)
        self.assertIn("mfe_r", exit_payload)
        self.assertIn("total_fee_eur", exit_payload)
        self.assertIn("entry_slippage_bps", exit_payload)
        self.assertIn("exit_slippage_bps", exit_payload)
        self.assertTrue(exit_payload["replay_points"])

    def test_pair_specific_execution_profile_changes_entry_slippage(self) -> None:
        xbt_context = build_context("XBTEUR")
        fet_context = build_context("FETEUR")
        xbt_intent = self.engine.strategy.evaluate(xbt_context)
        fet_intent = self.engine.strategy.evaluate(fet_context)

        self.assertIsNotNone(xbt_intent)
        self.assertIsNotNone(fet_intent)

        xbt_estimate = self.engine._estimate_entry_execution(xbt_intent, xbt_context)  # type: ignore[arg-type]
        fet_estimate = self.engine._estimate_entry_execution(fet_intent, fet_context)  # type: ignore[arg-type]

        self.assertLess(xbt_estimate.slippage_bps, fet_estimate.slippage_bps)
        self.assertGreater(xbt_estimate.maker_probability, fet_estimate.maker_probability)


if __name__ == "__main__":
    unittest.main()
