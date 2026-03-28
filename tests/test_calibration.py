import unittest

from daytrading_bot.backtest import BacktestReport
from daytrading_bot.calibration import _score_report
from daytrading_bot.config import BotConfig


class CalibrationTests(unittest.TestCase):
    def test_score_report_prefers_positive_expectancy_and_profit_factor(self) -> None:
        config = BotConfig()
        weak_report = BacktestReport(
            ending_equity=99.0,
            total_trades=4,
            win_rate=0.25,
            profit_factor=0.6,
            max_drawdown_pct=0.03,
            days_tested=2,
            trades_per_day=2.0,
            gross_profit_eur=1.0,
            gross_loss_eur=2.5,
            expectancy_eur=-0.375,
            expectancy_r=-0.2,
            average_hold_minutes=40.0,
            exit_distribution=[],
            setup_performance=[],
            trade_logs=[],
        )
        strong_report = BacktestReport(
            ending_equity=103.0,
            total_trades=4,
            win_rate=0.5,
            profit_factor=1.8,
            max_drawdown_pct=0.02,
            days_tested=2,
            trades_per_day=2.0,
            gross_profit_eur=3.6,
            gross_loss_eur=2.0,
            expectancy_eur=0.4,
            expectancy_r=0.35,
            average_hold_minutes=35.0,
            exit_distribution=[],
            setup_performance=[],
            trade_logs=[],
        )

        self.assertGreater(_score_report(config, strong_report), _score_report(config, weak_report))


if __name__ == "__main__":
    unittest.main()
