from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from daytrading_bot.backtest import BacktestReport, BacktestTradeLog
from daytrading_bot.config import BotConfig, ThreeCommasConfig
from daytrading_bot.history import LocalPairHistory, slice_histories_by_timerange
from daytrading_bot.models import Candle
from daytrading_bot.research import (
    build_parameter_variants,
    build_walk_forward_folds,
    run_walk_forward,
    run_walk_forward_optimization,
)
from tests.helpers import make_candle


def build_history(symbol: str = "XBTEUR", days: int = 15) -> LocalPairHistory:
    start = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
    candles_1m: list[Candle] = []
    candles_15m: list[Candle] = []
    price_1m = 100.0
    price_15m = 100.0
    total_hours = days * 24
    for hour in range(total_hours):
        ts = start + timedelta(hours=hour)
        price_1m += 0.1
        candles_1m.append(make_candle(ts, price_1m, volume=100.0, high_offset=0.2, low_offset=0.15))
        if hour % 1 == 0:
            price_15m += 0.3
            candles_15m.append(make_candle(ts, price_15m, volume=140.0, high_offset=0.35, low_offset=0.20))
    return LocalPairHistory(symbol=symbol, candles_1m=tuple(candles_1m), candles_15m=tuple(candles_15m))


class FakeBacktester:
    def __init__(self, bot_config: BotConfig, execution_config: ThreeCommasConfig) -> None:
        self.bot_config = bot_config
        self.execution_config = execution_config

    def run_histories_window(
        self,
        histories: dict[str, LocalPairHistory],
        start: datetime | None = None,
        end: datetime | None = None,
        warmup: timedelta = timedelta(0),
    ) -> BacktestReport:
        favorable = (
            self.bot_config.recovery_min_score <= 55.0
            and self.bot_config.recovery_min_adx_15m <= 16.0
        )
        if favorable:
            logs = [
                BacktestTradeLog(
                    pair="XBTEUR",
                    setup_type="recovery_reclaim",
                    regime_label="recovery",
                    quality="A",
                    score=75.0,
                    entry_ts="2026-03-06T10:00:00Z",
                    exit_ts="2026-03-06T11:00:00Z",
                    hold_minutes=60.0,
                    entry_price=100.0,
                    exit_price=101.5,
                    initial_stop_price=99.0,
                    final_stop_price=100.5,
                    pnl_eur=1.5,
                    r_multiple=1.5,
                    exit_reason="time_stop",
                    reason_code="recovery_reclaim:100.20",
                    trailing_enabled=True,
                ),
                BacktestTradeLog(
                    pair="XBTEUR",
                    setup_type="recovery_reclaim",
                    regime_label="recovery",
                    quality="A",
                    score=75.0,
                    entry_ts="2026-03-06T12:00:00Z",
                    exit_ts="2026-03-06T13:00:00Z",
                    hold_minutes=60.0,
                    entry_price=101.0,
                    exit_price=102.0,
                    initial_stop_price=100.0,
                    final_stop_price=101.0,
                    pnl_eur=1.0,
                    r_multiple=1.0,
                    exit_reason="take_profit",
                    reason_code="recovery_reclaim:101.10",
                    trailing_enabled=True,
                ),
            ]
        else:
            logs = []
        summary = {
            "ending_equity": 100.0 + sum(log.pnl_eur for log in logs),
            "total_trades": len(logs),
            "win_rate": 1.0 if logs else 0.0,
            "profit_factor": 2.0 if logs else 0.0,
            "max_drawdown_pct": 0.01 if logs else 0.0,
            "days_tested": 1,
            "trades_per_day": float(len(logs)),
            "gross_profit_eur": sum(log.pnl_eur for log in logs if log.pnl_eur > 0.0),
            "gross_loss_eur": sum(abs(log.pnl_eur) for log in logs if log.pnl_eur < 0.0),
            "expectancy_eur": (sum(log.pnl_eur for log in logs) / len(logs)) if logs else 0.0,
            "expectancy_r": (sum(log.r_multiple for log in logs) / len(logs)) if logs else 0.0,
            "average_hold_minutes": (sum(log.hold_minutes for log in logs) / len(logs)) if logs else 0.0,
            "exit_distribution": [],
            "setup_performance": [],
        }
        return BacktestReport(trade_logs=logs, **summary)


class ResearchTests(unittest.TestCase):
    def test_slice_histories_by_timerange_applies_warmup(self) -> None:
        history = build_history(days=3)
        histories = {"XBTEUR": history}
        start = history.candles_1m[30].ts
        end = history.candles_1m[60].ts

        sliced = slice_histories_by_timerange(histories, start=start, end=end, warmup=timedelta(hours=6))

        window = sliced["XBTEUR"]
        self.assertEqual(window.candles_1m[0].ts, start - timedelta(hours=6))
        self.assertLess(window.candles_1m[-1].ts, end)
        self.assertGreater(len(window.candles_1m), 0)

    def test_build_parameter_variants_includes_both_scopes(self) -> None:
        breakout = build_parameter_variants("breakout", profile="fast")
        recovery = build_parameter_variants("recovery", profile="fast")
        both = build_parameter_variants("both", profile="fast")

        self.assertTrue(all(variant.setup_scope == "breakout" for variant in breakout))
        self.assertTrue(all("trail_activation_r" in variant.params for variant in breakout))
        self.assertTrue(all(variant.setup_scope == "recovery" for variant in recovery))
        self.assertTrue(all("recovery_min_score" in variant.params for variant in recovery))
        self.assertEqual(len(both), len(breakout) + len(recovery))

    def test_walk_forward_runner_selects_best_variant_and_aggregates_oos(self) -> None:
        histories = {"XBTEUR": build_history(days=15)}
        folds = build_walk_forward_folds(histories, train_days=5, test_days=2, step_days=2)
        self.assertGreater(len(folds), 0)
        report = run_walk_forward(
            histories,
            BotConfig(),
            ThreeCommasConfig(secret="secret", bot_uuid="bot", mode="paper"),
            setup_scope="recovery",
            profile="fast",
            objective="expectancy_eur",
            train_days=5,
            test_days=2,
            step_days=2,
            top_n=2,
            warmup=timedelta(hours=12),
            backtester_factory=FakeBacktester,
        )

        self.assertFalse(report.insufficient_history)
        self.assertGreater(len(report.folds), 0)
        self.assertGreater(report.aggregate_oos_total_trades, 0)
        self.assertGreater(report.aggregate_oos_expectancy_eur, 0.0)
        self.assertIn("recovery:", next(iter(report.best_variant_frequency.keys())))
        self.assertTrue(
            all(
                fold.best_variant is not None and fold.best_variant.params["recovery_min_score"] == 55.0
                for fold in report.folds
            )
        )

    def test_walk_forward_optimization_ranks_variants_by_oos_performance(self) -> None:
        histories = {"XBTEUR": build_history(days=15)}
        report = run_walk_forward_optimization(
            histories,
            BotConfig(),
            ThreeCommasConfig(secret="secret", bot_uuid="bot", mode="paper"),
            setup_scope="recovery",
            profile="fast",
            objective="expectancy_eur",
            train_days=5,
            test_days=2,
            step_days=2,
            top_n=2,
            warmup=timedelta(hours=12),
            backtester_factory=FakeBacktester,
        )

        self.assertFalse(report.insufficient_history)
        self.assertGreater(report.variants_tested, 0)
        self.assertGreater(report.eligible_variants, 0)
        self.assertGreater(len(report.top_results), 0)
        self.assertEqual(report.top_results[0].params["recovery_min_score"], 55.0)
        self.assertGreater(report.top_results[0].aggregate_oos_expectancy_eur, 0.0)


if __name__ == "__main__":
    unittest.main()
