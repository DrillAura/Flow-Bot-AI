from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import BotConfig
from .history import load_local_histories, strategy_warmup_cursor
from .kraken import KrakenPublicClient
from .strategy import BreakoutPullbackStrategy, StrategyCheck


@dataclass(frozen=True)
class FilterDiagnostic:
    threshold: str
    passed: int
    failed: int
    skipped: int
    pass_rate: float
    coverage_rate: float


@dataclass(frozen=True)
class SignalDiagnosticsReport:
    total_contexts: int
    setups_found: int
    setup_rate: float
    pair_context_counts: dict[str, int]
    pair_setup_counts: dict[str, int]
    rejection_counts: dict[str, int]
    filter_stats: dict[str, FilterDiagnostic]


def run_signal_diagnostics(data_dir: Path, bot_config: BotConfig) -> SignalDiagnosticsReport:
    histories = load_local_histories(data_dir, [pair.symbol for pair in bot_config.pairs])
    index_limit = min(len(history.candles_1m) for history in histories.values())
    kraken = KrakenPublicClient()
    strategy = BreakoutPullbackStrategy(bot_config)

    total_contexts = 0
    setups_found = 0
    pair_context_counts: Counter[str] = Counter()
    pair_setup_counts: Counter[str] = Counter()
    rejection_counts: Counter[str] = Counter()
    filter_pass_counts: Counter[str] = Counter()
    filter_fail_counts: Counter[str] = Counter()
    filter_thresholds: dict[str, str] = {}

    for cursor in range(strategy_warmup_cursor(), index_limit):
        for symbol, history in histories.items():
            latest_close = history.candles_1m[cursor].close
            context = history.context_at(cursor, kraken.synthetic_order_book(symbol, latest_close))
            total_contexts += 1
            pair_context_counts[symbol] += 1
            evaluation = strategy.evaluate_detailed(context)
            _record_filter_checks(evaluation.checks, filter_pass_counts, filter_fail_counts, filter_thresholds)
            if evaluation.intent is not None:
                setups_found += 1
                pair_setup_counts[symbol] += 1
            else:
                rejection_counts.update(evaluation.rejection_reasons)

    setup_rate = (setups_found / total_contexts) if total_contexts else 0.0
    filter_stats = _build_filter_stats(total_contexts, filter_pass_counts, filter_fail_counts, filter_thresholds)
    return SignalDiagnosticsReport(
        total_contexts=total_contexts,
        setups_found=setups_found,
        setup_rate=setup_rate,
        pair_context_counts=dict(pair_context_counts),
        pair_setup_counts=dict(pair_setup_counts),
        rejection_counts=dict(rejection_counts),
        filter_stats=filter_stats,
    )


def _record_filter_checks(
    checks: tuple[StrategyCheck, ...],
    pass_counts: Counter[str],
    fail_counts: Counter[str],
    thresholds: dict[str, str],
) -> None:
    for check in checks:
        thresholds.setdefault(check.name, check.threshold)
        if check.passed:
            pass_counts[check.name] += 1
        else:
            fail_counts[check.name] += 1


def _build_filter_stats(
    total_contexts: int,
    pass_counts: Counter[str],
    fail_counts: Counter[str],
    thresholds: dict[str, str],
) -> dict[str, FilterDiagnostic]:
    stats: dict[str, FilterDiagnostic] = {}
    for name in sorted(thresholds.keys()):
        passed = pass_counts[name]
        failed = fail_counts[name]
        evaluated = passed + failed
        skipped = max(total_contexts - evaluated, 0)
        stats[name] = FilterDiagnostic(
            threshold=thresholds[name],
            passed=passed,
            failed=failed,
            skipped=skipped,
            pass_rate=(passed / evaluated) if evaluated else 0.0,
            coverage_rate=(evaluated / total_contexts) if total_contexts else 0.0,
        )
    return stats
