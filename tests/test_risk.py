import unittest
from datetime import datetime

from daytrading_bot.config import BotConfig
from daytrading_bot.models import ActiveTrade
from daytrading_bot.risk import RiskController


class RiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = BotConfig()
        self.controller = RiskController(self.config)
        self.moment = datetime(2026, 3, 23, 9, 0, tzinfo=self.config.timezone)

    def test_position_budget_respects_risk_and_position_cap(self) -> None:
        budget = self.controller.position_budget(entry_price=100.0, stop_price=99.0, available_eur=100.0)
        self.assertAlmostEqual(budget, 80.0)

    def test_drawdown_ladder_transitions(self) -> None:
        self.controller.mark_to_market(unrealized_pnl=-2.6, moment=self.moment)
        self.assertEqual(self.controller.state.lock_state, "warning")
        self.controller.mark_to_market(unrealized_pnl=-3.6, moment=self.moment)
        self.assertEqual(self.controller.state.lock_state, "reduced")
        self.controller.mark_to_market(unrealized_pnl=-4.3, moment=self.moment)
        self.assertEqual(self.controller.state.lock_state, "read_only")
        self.controller.mark_to_market(unrealized_pnl=-5.1, moment=self.moment)
        self.assertEqual(self.controller.state.lock_state, "killed")

    def test_warning_state_requires_quality_a(self) -> None:
        self.controller.mark_to_market(unrealized_pnl=-2.6, moment=self.moment)
        can_open, reason = self.controller.can_open_trade(self.moment, quality="B")
        self.assertFalse(can_open)
        self.assertEqual(reason, "warning_state_requires_a_setup")

    def test_daily_stop_after_two_losses(self) -> None:
        trade = ActiveTrade(
            pair="XBTEUR",
            entry_ts=self.moment,
            entry_price=100.0,
            initial_stop_price=99.0,
            stop_price=99.0,
            budget_eur=50.0,
            reason_code="test",
            max_hold_min=120,
            trail_activation_r=1.4,
        )
        self.controller.record_trade_opened(trade, self.moment)
        self.controller.record_trade_closed(-1.0, self.moment)
        self.controller.record_trade_opened(trade, self.moment)
        self.controller.record_trade_closed(-1.0, self.moment)
        can_open, reason = self.controller.can_open_trade(self.moment, quality="A")
        self.assertFalse(can_open)
        self.assertEqual(reason, "consecutive_losses_limit")

    def test_break_even_trade_is_neutral(self) -> None:
        trade = ActiveTrade(
            pair="XBTEUR",
            entry_ts=self.moment,
            entry_price=100.0,
            initial_stop_price=99.0,
            stop_price=99.0,
            budget_eur=50.0,
            reason_code="test",
            max_hold_min=120,
            trail_activation_r=1.4,
        )
        self.controller.record_trade_opened(trade, self.moment)
        self.controller.record_trade_closed(0.0, self.moment)
        self.assertEqual(self.controller.state.wins, 0)
        self.assertEqual(self.controller.state.losses, 0)
        self.assertEqual(self.controller.state.consecutive_losses, 0)
        self.assertEqual(self.controller.state.gross_profit, 0.0)
        self.assertEqual(self.controller.state.gross_loss, 0.0)


if __name__ == "__main__":
    unittest.main()
