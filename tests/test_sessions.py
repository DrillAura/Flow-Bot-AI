import unittest
from datetime import datetime

from daytrading_bot.config import BotConfig
from daytrading_bot.sessions import is_hard_flat_time, is_trade_window


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = BotConfig()

    def test_trade_window_accepts_morning_session(self) -> None:
        moment = datetime(2026, 3, 23, 9, 15, tzinfo=self.config.timezone)
        self.assertTrue(is_trade_window(moment, self.config))

    def test_trade_window_rejects_midday_gap(self) -> None:
        moment = datetime(2026, 3, 23, 12, 0, tzinfo=self.config.timezone)
        self.assertFalse(is_trade_window(moment, self.config))

    def test_hard_flat_triggers_at_or_after_cutoff(self) -> None:
        moment = datetime(2026, 3, 23, 21, 30, tzinfo=self.config.timezone)
        self.assertTrue(is_hard_flat_time(moment, self.config))


if __name__ == "__main__":
    unittest.main()
