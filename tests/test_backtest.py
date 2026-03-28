import unittest
from datetime import datetime, timedelta, timezone

from daytrading_bot.backtest import BacktestTradeLog, CsvBacktester, summarize_trade_logs
from daytrading_bot.config import BotConfig, ThreeCommasConfig
from daytrading_bot.history import LocalPairHistory
from tests.helpers import make_candle


class BacktestTests(unittest.TestCase):
    def test_run_histories_window_returns_empty_report_for_empty_slice(self) -> None:
        start = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
        candles_1m = [make_candle(start + timedelta(minutes=index), 100.0 + index * 0.01) for index in range(200)]
        candles_15m = [make_candle(start + timedelta(minutes=15 * index), 100.0 + index * 0.1) for index in range(80)]
        histories = {
            "XBTEUR": LocalPairHistory(symbol="XBTEUR", candles_1m=tuple(candles_1m), candles_15m=tuple(candles_15m))
        }
        backtester = CsvBacktester(BotConfig(), ThreeCommasConfig(mode="paper"))
        report = backtester.run_histories_window(
            histories,
            start=start + timedelta(days=2),
            end=start + timedelta(days=3),
            warmup=timedelta(hours=12),
        )

        self.assertEqual(report.total_trades, 0)
        self.assertEqual(report.trade_logs, [])
        self.assertEqual(report.ending_equity, 100.0)

    def test_summarize_trade_logs_builds_expectancy_and_exit_distribution(self) -> None:
        summary = summarize_trade_logs(
            [
                BacktestTradeLog(
                    pair="XBTEUR",
                    setup_type="recovery_reclaim",
                    regime_label="recovery",
                    quality="A",
                    score=74.0,
                    entry_ts="2026-03-23T08:30:00Z",
                    exit_ts="2026-03-23T09:00:00Z",
                    hold_minutes=30.0,
                    entry_price=100.0,
                    exit_price=101.0,
                    initial_stop_price=99.0,
                    final_stop_price=100.2,
                    pnl_eur=1.2,
                    r_multiple=1.0,
                    exit_reason="time_stop",
                    reason_code="recovery_reclaim:100.50",
                    trailing_enabled=True,
                ),
                BacktestTradeLog(
                    pair="XBTEUR",
                    setup_type="recovery_reclaim",
                    regime_label="recovery",
                    quality="B",
                    score=63.0,
                    entry_ts="2026-03-23T10:00:00Z",
                    exit_ts="2026-03-23T10:20:00Z",
                    hold_minutes=20.0,
                    entry_price=102.0,
                    exit_price=101.0,
                    initial_stop_price=100.5,
                    final_stop_price=100.5,
                    pnl_eur=-0.6,
                    r_multiple=-0.4,
                    exit_reason="time_decay_exit",
                    reason_code="recovery_reclaim:101.80",
                    trailing_enabled=False,
                ),
                BacktestTradeLog(
                    pair="ETHEUR",
                    setup_type="breakout_pullback",
                    regime_label="bullish",
                    quality="A",
                    score=81.0,
                    entry_ts="2026-03-23T11:00:00Z",
                    exit_ts="2026-03-23T11:45:00Z",
                    hold_minutes=45.0,
                    entry_price=200.0,
                    exit_price=198.0,
                    initial_stop_price=197.0,
                    final_stop_price=197.5,
                    pnl_eur=-0.3,
                    r_multiple=-0.2,
                    exit_reason="protective_stop",
                    reason_code="breakout_pullback:199.50",
                    trailing_enabled=False,
                ),
            ]
        )

        self.assertAlmostEqual(summary.expectancy_eur, 0.1)
        self.assertAlmostEqual(summary.expectancy_r, 0.13333333333333333)
        self.assertEqual(summary.exit_distribution[0].exit_reason, "protective_stop")
        self.assertEqual(len(summary.setup_performance), 2)
        recovery = next(item for item in summary.setup_performance if item.setup_type == "recovery_reclaim")
        self.assertAlmostEqual(recovery.expectancy_eur, 0.3)
        self.assertAlmostEqual(recovery.expectancy_r, 0.3)
        self.assertEqual(recovery.exit_distribution[0].count, 1)


if __name__ == "__main__":
    unittest.main()
