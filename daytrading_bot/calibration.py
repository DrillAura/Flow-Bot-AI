from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from .backtest import BacktestReport, CsvBacktester
from .config import BotConfig, ThreeCommasConfig
from .history import load_local_histories
from .research import CalibrationObjective, ParameterVariant, SetupScope, build_parameter_variants, score_backtest_report


@dataclass(frozen=True)
class CalibrationRow:
    score: float
    recovery_min_adx_15m: float
    recovery_max_ema_gap_pct: float
    recovery_compression_atr_multiple: float
    trail_activation_r: float
    ending_equity: float
    total_trades: int
    trades_per_day: float
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    expectancy_eur: float
    expectancy_r: float
    variant_id: str = ""
    setup_scope: str = "recovery"
    objective: str = "hybrid"
    params: dict[str, float | int | str] = field(default_factory=dict)


@dataclass(frozen=True)
class CalibrationReport:
    variants_tested: int
    top_results: list[CalibrationRow]
    zero_trade_variants: int = 0
    eligible_variants: int = 0
    setup_scope: str = "recovery"
    objective: str = "hybrid"


def run_calibration(
    data_dir: Path,
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    top_n: int = 10,
    profile: str = "fast",
    setup_scope: SetupScope = "recovery",
    objective: CalibrationObjective = "hybrid",
) -> CalibrationReport:
    rows: list[CalibrationRow] = []
    histories = load_local_histories(data_dir, [pair.symbol for pair in bot_config.pairs])
    variants = build_parameter_variants(setup_scope, profile)
    for variant in variants:
        candidate = replace(bot_config, **variant.params)
        report = CsvBacktester(candidate, execution_config.with_mode("paper")).run_histories(histories)
        rows.append(_build_calibration_row(candidate, report, variant, objective))

    zero_trade_variants = sum(1 for row in rows if row.total_trades == 0)
    eligible_variants = sum(1 for row in rows if row.total_trades >= bot_config.calibration_min_trades)
    tradeful_rows = [row for row in rows if row.total_trades > 0]
    ranked = sorted(tradeful_rows or rows, key=lambda row: row.score, reverse=True)
    return CalibrationReport(
        variants_tested=len(rows),
        top_results=ranked[:top_n],
        zero_trade_variants=zero_trade_variants,
        eligible_variants=eligible_variants,
        setup_scope=setup_scope,
        objective=objective,
    )


def _build_calibration_row(
    bot_config: BotConfig,
    report: BacktestReport,
    variant: ParameterVariant,
    objective: CalibrationObjective,
) -> CalibrationRow:
    return CalibrationRow(
        score=score_backtest_report(bot_config, report, objective=objective),
        recovery_min_adx_15m=float(bot_config.recovery_min_adx_15m),
        recovery_max_ema_gap_pct=float(bot_config.recovery_max_ema_gap_pct),
        recovery_compression_atr_multiple=float(bot_config.recovery_compression_atr_multiple),
        trail_activation_r=float(bot_config.trail_activation_r),
        ending_equity=report.ending_equity,
        total_trades=report.total_trades,
        trades_per_day=report.trades_per_day,
        win_rate=report.win_rate,
        profit_factor=report.profit_factor,
        max_drawdown_pct=report.max_drawdown_pct,
        expectancy_eur=report.expectancy_eur,
        expectancy_r=report.expectancy_r,
        variant_id=variant.variant_id,
        setup_scope=variant.setup_scope,
        objective=objective,
        params=dict(variant.params),
    )


def _score_report(bot_config: BotConfig, report: BacktestReport, objective: CalibrationObjective = "hybrid") -> float:
    return score_backtest_report(bot_config, report, objective=objective)
