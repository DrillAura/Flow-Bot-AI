from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import product
from typing import Literal

from .backtest import BacktestReport, BacktestTradeLog, CsvBacktester, summarize_trade_logs
from .config import BotConfig, ThreeCommasConfig
from .history import LocalPairHistory, history_bounds

SetupScope = Literal["breakout", "recovery", "both"]
CalibrationObjective = Literal["hybrid", "profit_factor", "expectancy_eur", "expectancy_r"]


@dataclass(frozen=True)
class ParameterVariant:
    variant_id: str
    setup_scope: SetupScope
    params: dict[str, float | int | str]


@dataclass(frozen=True)
class WalkForwardFold:
    fold_id: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


@dataclass(frozen=True)
class WalkForwardFoldResult:
    fold: WalkForwardFold
    variants_tested: int
    zero_trade_variants: int
    best_variant: ParameterVariant | None
    train_report: BacktestReport
    test_report: BacktestReport


@dataclass(frozen=True)
class WalkForwardReport:
    folds: list[WalkForwardFoldResult]
    aggregate_oos_profit_factor: float
    aggregate_oos_expectancy_eur: float
    aggregate_oos_expectancy_r: float
    aggregate_oos_max_drawdown_pct: float
    aggregate_oos_total_trades: int
    best_variant_frequency: dict[str, int]
    insufficient_history: bool
    objective: CalibrationObjective
    setup_scope: SetupScope


@dataclass(frozen=True)
class WalkForwardOptimizationRow:
    variant_id: str
    setup_scope: SetupScope
    objective: CalibrationObjective
    score: float
    folds_evaluated: int
    aggregate_oos_total_trades: int
    aggregate_oos_profit_factor: float
    aggregate_oos_expectancy_eur: float
    aggregate_oos_expectancy_r: float
    aggregate_oos_max_drawdown_pct: float
    params: dict[str, float | int | str]


@dataclass(frozen=True)
class WalkForwardOptimizationReport:
    variants_tested: int
    top_results: list[WalkForwardOptimizationRow]
    zero_trade_variants: int
    eligible_variants: int
    insufficient_history: bool
    objective: CalibrationObjective
    setup_scope: SetupScope
    train_days: int
    test_days: int
    step_days: int | None


def build_parameter_variants(setup_scope: SetupScope, profile: str = "fast") -> list[ParameterVariant]:
    scopes: tuple[SetupScope, ...]
    if setup_scope == "both":
        scopes = ("breakout", "recovery")
    else:
        scopes = (setup_scope,)

    variants: list[ParameterVariant] = []
    for scope in scopes:
        for variant in _build_scope_variants(scope, profile):
            variants.append(variant)
    return variants


def score_backtest_report(
    bot_config: BotConfig,
    report: BacktestReport,
    objective: CalibrationObjective = "hybrid",
) -> float:
    equity_gain = report.ending_equity - bot_config.initial_equity_eur
    drawdown_penalty = report.max_drawdown_pct * 250.0
    inactivity_penalty = 50.0 if report.total_trades == 0 else 0.0
    low_sample_penalty = max(bot_config.calibration_min_trades - report.total_trades, 0) * 10.0
    negative_expectancy_penalty = 25.0 if report.expectancy_eur <= 0.0 else 0.0

    if objective == "profit_factor":
        return (
            equity_gain
            + min(report.profit_factor, 4.0) * 60.0
            + report.expectancy_eur * 20.0
            + report.expectancy_r * 40.0
            - drawdown_penalty
            - inactivity_penalty
            - low_sample_penalty
            - negative_expectancy_penalty
        )

    if objective == "expectancy_eur":
        return (
            equity_gain
            + report.expectancy_eur * 120.0
            + min(report.profit_factor, 4.0) * 20.0
            + report.expectancy_r * 30.0
            - drawdown_penalty
            - inactivity_penalty
            - low_sample_penalty
            - negative_expectancy_penalty
        )

    if objective == "expectancy_r":
        return (
            equity_gain
            + report.expectancy_r * 180.0
            + min(report.profit_factor, 4.0) * 20.0
            + report.expectancy_eur * 30.0
            - drawdown_penalty
            - inactivity_penalty
            - low_sample_penalty
            - negative_expectancy_penalty
        )

    trade_activity_bonus = min(report.trades_per_day, 3.0) * 5.0
    return (
        equity_gain
        + report.expectancy_eur * 45.0
        + report.expectancy_r * 120.0
        + min(report.profit_factor, 4.0) * 25.0
        + trade_activity_bonus
        - drawdown_penalty
        - inactivity_penalty
        - low_sample_penalty
        - negative_expectancy_penalty
    )


def build_walk_forward_folds(
    histories: dict[str, LocalPairHistory],
    train_days: int,
    test_days: int,
    step_days: int | None = None,
) -> list[WalkForwardFold]:
    if not histories:
        return []
    start, end = history_bounds(histories)
    step_days = test_days if step_days is None else step_days
    folds: list[WalkForwardFold] = []
    fold_index = 0
    while True:
        train_start = start
        train_end = start + timedelta(days=train_days + (fold_index * step_days))
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        folds.append(
            WalkForwardFold(
                fold_id=fold_index + 1,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        fold_index += 1
    return folds


def run_walk_forward(
    histories: dict[str, LocalPairHistory],
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    *,
    setup_scope: SetupScope = "both",
    profile: str = "fast",
    objective: CalibrationObjective = "hybrid",
    train_days: int = 10,
    test_days: int = 3,
    step_days: int | None = None,
    top_n: int = 3,
    warmup: timedelta = timedelta(hours=36),
    backtester_factory: type[CsvBacktester] = CsvBacktester,
) -> WalkForwardReport:
    folds = build_walk_forward_folds(histories, train_days=train_days, test_days=test_days, step_days=step_days)
    if not folds:
        return WalkForwardReport(
            folds=[],
            aggregate_oos_profit_factor=0.0,
            aggregate_oos_expectancy_eur=0.0,
            aggregate_oos_expectancy_r=0.0,
            aggregate_oos_max_drawdown_pct=0.0,
            aggregate_oos_total_trades=0,
            best_variant_frequency={},
            insufficient_history=True,
            objective=objective,
            setup_scope=setup_scope,
        )

    variants = build_parameter_variants(setup_scope, profile)
    fold_results: list[WalkForwardFoldResult] = []
    best_variant_frequency: dict[str, int] = {}
    aggregate_oos_logs: list[BacktestTradeLog] = []

    for fold in folds:
        train_rankings: list[tuple[float, ParameterVariant, BacktestReport]] = []
        zero_trade_variants = 0
        for variant in variants:
            candidate_config = replace(bot_config, **variant.params)
            backtester = backtester_factory(candidate_config, execution_config.with_mode("paper"))
            train_report = backtester.run_histories_window(
                histories,
                start=fold.train_start,
                end=fold.train_end,
                warmup=warmup,
            )
            if train_report.total_trades == 0:
                zero_trade_variants += 1
            score = score_backtest_report(candidate_config, train_report, objective=objective)
            train_rankings.append((score, variant, train_report))

        train_rankings.sort(key=lambda item: item[0], reverse=True)
        top_rankings = train_rankings[: max(top_n, 1)]
        _, best_variant, best_train_report = top_rankings[0]
        best_variant_frequency[best_variant.variant_id] = best_variant_frequency.get(best_variant.variant_id, 0) + 1

        best_candidate_config = replace(bot_config, **best_variant.params)
        best_backtester = backtester_factory(best_candidate_config, execution_config.with_mode("paper"))
        test_report = best_backtester.run_histories_window(
            histories,
            start=fold.test_start,
            end=fold.test_end,
            warmup=warmup,
        )
        aggregate_oos_logs.extend(test_report.trade_logs)
        fold_results.append(
            WalkForwardFoldResult(
                fold=fold,
                variants_tested=len(variants),
                zero_trade_variants=zero_trade_variants,
                best_variant=best_variant,
                train_report=best_train_report,
                test_report=test_report,
            )
        )

    aggregate_summary = _summarize_walk_forward_trade_logs(bot_config, aggregate_oos_logs)
    return WalkForwardReport(
        folds=fold_results,
        aggregate_oos_profit_factor=aggregate_summary["profit_factor"],
        aggregate_oos_expectancy_eur=aggregate_summary["expectancy_eur"],
        aggregate_oos_expectancy_r=aggregate_summary["expectancy_r"],
        aggregate_oos_max_drawdown_pct=aggregate_summary["max_drawdown_pct"],
        aggregate_oos_total_trades=int(aggregate_summary["total_trades"]),
        best_variant_frequency=best_variant_frequency,
        insufficient_history=False,
        objective=objective,
        setup_scope=setup_scope,
    )


def run_walk_forward_optimization(
    histories: dict[str, LocalPairHistory],
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    *,
    setup_scope: SetupScope = "both",
    profile: str = "fast",
    objective: CalibrationObjective = "hybrid",
    train_days: int = 10,
    test_days: int = 3,
    step_days: int | None = None,
    top_n: int = 10,
    warmup: timedelta = timedelta(hours=36),
    backtester_factory: type[CsvBacktester] = CsvBacktester,
) -> WalkForwardOptimizationReport:
    folds = build_walk_forward_folds(histories, train_days=train_days, test_days=test_days, step_days=step_days)
    if not folds:
        return WalkForwardOptimizationReport(
            variants_tested=0,
            top_results=[],
            zero_trade_variants=0,
            eligible_variants=0,
            insufficient_history=True,
            objective=objective,
            setup_scope=setup_scope,
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
        )

    variants = build_parameter_variants(setup_scope, profile)
    rows: list[WalkForwardOptimizationRow] = []
    for variant in variants:
        candidate_config = replace(bot_config, **variant.params)
        backtester = backtester_factory(candidate_config, execution_config.with_mode("paper"))
        aggregate_logs: list[BacktestTradeLog] = []
        total_days_tested = 0
        for fold in folds:
            report = backtester.run_histories_window(
                histories,
                start=fold.test_start,
                end=fold.test_end,
                warmup=warmup,
            )
            aggregate_logs.extend(report.trade_logs)
            total_days_tested += max(report.days_tested, test_days)

        aggregate_report = _build_aggregate_oos_backtest_report(
            candidate_config,
            aggregate_logs,
            days_tested=max(total_days_tested, len(folds) * test_days),
        )
        rows.append(
            WalkForwardOptimizationRow(
                variant_id=variant.variant_id,
                setup_scope=variant.setup_scope,
                objective=objective,
                score=score_backtest_report(candidate_config, aggregate_report, objective=objective),
                folds_evaluated=len(folds),
                aggregate_oos_total_trades=aggregate_report.total_trades,
                aggregate_oos_profit_factor=aggregate_report.profit_factor,
                aggregate_oos_expectancy_eur=aggregate_report.expectancy_eur,
                aggregate_oos_expectancy_r=aggregate_report.expectancy_r,
                aggregate_oos_max_drawdown_pct=aggregate_report.max_drawdown_pct,
                params=dict(variant.params),
            )
        )

    zero_trade_variants = sum(1 for row in rows if row.aggregate_oos_total_trades == 0)
    eligible_variants = sum(1 for row in rows if row.aggregate_oos_total_trades >= bot_config.calibration_min_trades)
    tradeful_rows = [row for row in rows if row.aggregate_oos_total_trades > 0]
    ranked = sorted(tradeful_rows or rows, key=lambda row: row.score, reverse=True)
    return WalkForwardOptimizationReport(
        variants_tested=len(rows),
        top_results=ranked[:top_n],
        zero_trade_variants=zero_trade_variants,
        eligible_variants=eligible_variants,
        insufficient_history=False,
        objective=objective,
        setup_scope=setup_scope,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
    )


def _build_scope_variants(scope: SetupScope, profile: str) -> list[ParameterVariant]:
    normalized = profile.lower()
    if scope == "breakout":
        if normalized == "full":
            return _expand_variants(
                scope,
                {
                    "min_adx_15m": (18.0, 20.0, 22.0),
                    "breakout_volume_zscore": (2.0, 2.2, 2.4),
                    "trail_activation_r": (1.2, 1.4),
                    "stop_atr_multiple": (1.0,),
                    "max_hold_minutes": (90, 120),
                },
            )
        return _expand_variants(
            scope,
            {
                "min_adx_15m": (18.0, 20.0),
                "breakout_volume_zscore": (2.0, 2.2),
                "trail_activation_r": (1.3,),
                "stop_atr_multiple": (1.0,),
                "max_hold_minutes": (120,),
            },
        )

    if normalized == "full":
        return _expand_variants(
            scope,
            {
                "recovery_min_adx_15m": (16.0, 18.0, 20.0),
                "recovery_max_ema_gap_pct": (0.004, 0.006, 0.008),
                "recovery_compression_atr_multiple": (2.3, 2.6),
                "recovery_min_score": (55.0, 60.0),
                "recovery_trail_activation_r": (0.9, 1.1),
            },
        )
    return _expand_variants(
        scope,
        {
            "recovery_min_adx_15m": (16.0, 18.0),
            "recovery_max_ema_gap_pct": (0.006, 0.008),
            "recovery_compression_atr_multiple": (2.5,),
            "recovery_min_score": (55.0, 60.0),
            "recovery_trail_activation_r": (0.9,),
        },
    )


def _expand_variants(scope: SetupScope, grid: dict[str, tuple[float | int, ...]]) -> list[ParameterVariant]:
    keys = tuple(grid.keys())
    variants: list[ParameterVariant] = []
    for values in product(*(grid[key] for key in keys)):
        params = {key: value for key, value in zip(keys, values)}
        variant_id = scope + ":" + ":".join(f"{key}={value}" for key, value in params.items())
        variants.append(ParameterVariant(variant_id=variant_id, setup_scope=scope, params=params))
    return variants


def _summarize_walk_forward_trade_logs(bot_config: BotConfig, trade_logs: list[BacktestTradeLog]) -> dict[str, float]:
    if not trade_logs:
        return {
            "profit_factor": 0.0,
            "expectancy_eur": 0.0,
            "expectancy_r": 0.0,
            "max_drawdown_pct": 0.0,
            "total_trades": 0,
        }

    summary = summarize_trade_logs(trade_logs)
    gross_profit = summary.gross_profit_eur
    gross_loss = summary.gross_loss_eur
    profit_factor = float("inf") if gross_loss == 0.0 and gross_profit > 0.0 else (gross_profit / gross_loss if gross_loss > 0.0 else 0.0)
    ordered_logs = sorted(trade_logs, key=lambda log: _parse_ts(log.exit_ts))
    equity = bot_config.initial_equity_eur
    hwm = equity
    max_drawdown_pct = 0.0
    for log in ordered_logs:
        equity += log.pnl_eur
        hwm = max(hwm, equity)
        if hwm > 0:
            max_drawdown_pct = max(max_drawdown_pct, (hwm - equity) / hwm)
    return {
        "profit_factor": profit_factor,
        "expectancy_eur": summary.expectancy_eur,
        "expectancy_r": summary.expectancy_r,
        "max_drawdown_pct": max_drawdown_pct,
        "total_trades": len(trade_logs),
    }


def _build_aggregate_oos_backtest_report(
    bot_config: BotConfig,
    trade_logs: list[BacktestTradeLog],
    *,
    days_tested: int,
) -> BacktestReport:
    if not trade_logs:
        return BacktestReport(
            ending_equity=bot_config.initial_equity_eur,
            total_trades=0,
            win_rate=0.0,
            profit_factor=0.0,
            max_drawdown_pct=0.0,
            days_tested=days_tested,
            trades_per_day=0.0,
            gross_profit_eur=0.0,
            gross_loss_eur=0.0,
            expectancy_eur=0.0,
            expectancy_r=0.0,
            average_hold_minutes=0.0,
            exit_distribution=[],
            setup_performance=[],
            trade_logs=[],
        )

    summary = summarize_trade_logs(trade_logs)
    gross_profit = summary.gross_profit_eur
    gross_loss = summary.gross_loss_eur
    if gross_loss == 0.0:
        profit_factor = float("inf") if gross_profit > 0.0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss
    wins = sum(1 for log in trade_logs if log.pnl_eur > 0.0)
    ending_equity = bot_config.initial_equity_eur + sum(log.pnl_eur for log in trade_logs)
    max_drawdown_pct = _summarize_walk_forward_trade_logs(bot_config, trade_logs)["max_drawdown_pct"]
    return BacktestReport(
        ending_equity=ending_equity,
        total_trades=len(trade_logs),
        win_rate=wins / len(trade_logs),
        profit_factor=profit_factor,
        max_drawdown_pct=max_drawdown_pct,
        days_tested=days_tested,
        trades_per_day=len(trade_logs) / max(days_tested, 1),
        gross_profit_eur=summary.gross_profit_eur,
        gross_loss_eur=summary.gross_loss_eur,
        expectancy_eur=summary.expectancy_eur,
        expectancy_r=summary.expectancy_r,
        average_hold_minutes=summary.average_hold_minutes,
        exit_distribution=summary.exit_distribution,
        setup_performance=summary.setup_performance,
        trade_logs=trade_logs,
    )


def _parse_ts(raw_ts: str) -> datetime:
    return datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
