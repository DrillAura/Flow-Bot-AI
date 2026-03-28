from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import BotConfig
from .history import load_local_histories, strategy_warmup_cursor
from .kraken import KrakenPublicClient
from .sessions import localize, session_label
from .strategy import BreakoutPullbackStrategy


@dataclass(frozen=True)
class SignalDebugBucket:
    contexts: int
    setups_found: int
    setup_rate: float
    first_failures: dict[str, int]
    leading_failure: str
    leading_failure_rate: float


@dataclass(frozen=True)
class SignalDebugReport:
    total_contexts: int
    in_session_contexts: int
    setups_found: int
    global_first_failures: dict[str, int]
    pair_session_buckets: dict[str, dict[str, SignalDebugBucket]]


@dataclass(frozen=True)
class GoLiveGate:
    name: str
    passed: bool
    actual: float | int | bool | str
    threshold: str


@dataclass(frozen=True)
class PairForwardSummary:
    closed_trades: int
    wins: int
    losses: int
    net_pnl_eur: float


@dataclass(frozen=True)
class ForwardTestReport:
    source_exists: bool
    events_loaded: int
    closed_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    gross_profit_eur: float
    gross_loss_eur: float
    net_pnl_eur: float
    ending_equity: float
    max_drawdown_pct: float
    overnight_positions: int
    average_hold_minutes: float
    trade_days: int
    forward_days: int
    unclosed_entries: int
    orphan_exit_events: int
    rejection_counts: dict[str, int]
    exit_reason_counts: dict[str, int]
    pair_breakdown: dict[str, PairForwardSummary]
    gates: dict[str, GoLiveGate]
    go_live_ready: bool


@dataclass(frozen=True)
class _ClosedTrade:
    pair: str
    entry_ts: datetime
    exit_ts: datetime
    pnl_eur: float
    exit_reason: str

    @property
    def hold_minutes(self) -> float:
        return max((self.exit_ts - self.entry_ts).total_seconds() / 60.0, 0.0)


def run_signal_debug_report(data_dir: Path, bot_config: BotConfig) -> SignalDebugReport:
    histories = load_local_histories(data_dir, [pair.symbol for pair in bot_config.pairs])
    index_limit = min(len(history.candles_1m) for history in histories.values())
    kraken = KrakenPublicClient()
    strategy = BreakoutPullbackStrategy(bot_config)

    total_contexts = 0
    in_session_contexts = 0
    setups_found = 0
    global_first_failures: Counter[str] = Counter()
    bucket_counts: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_empty_signal_bucket))

    for cursor in range(strategy_warmup_cursor(), index_limit):
        for symbol, history in histories.items():
            latest_close = history.candles_1m[cursor].close
            context = history.context_at(cursor, kraken.synthetic_order_book(symbol, latest_close))
            moment = context.candles_1m[-1].ts
            bucket_name = session_label(moment, bot_config)

            total_contexts += 1
            if bucket_name != "off_hours":
                in_session_contexts += 1

            bucket = bucket_counts[symbol][bucket_name]
            bucket["contexts"] += 1

            evaluation = strategy.evaluate_detailed(context)
            if evaluation.intent is not None:
                setups_found += 1
                bucket["setups_found"] += 1
                continue

            first_failure = next((check.reason or check.name for check in evaluation.checks if not check.passed), "unknown")
            global_first_failures[first_failure] += 1
            bucket["first_failures"][first_failure] += 1

    pair_session_buckets: dict[str, dict[str, SignalDebugBucket]] = {}
    for symbol, sessions in bucket_counts.items():
        pair_session_buckets[symbol] = {
            name: _build_signal_bucket(bucket)
            for name, bucket in sorted(sessions.items())
        }

    return SignalDebugReport(
        total_contexts=total_contexts,
        in_session_contexts=in_session_contexts,
        setups_found=setups_found,
        global_first_failures=dict(global_first_failures),
        pair_session_buckets=pair_session_buckets,
    )


def run_forward_test_report(telemetry_path: Path, bot_config: BotConfig) -> ForwardTestReport:
    if not telemetry_path.exists():
        return _empty_forward_report(source_exists=False, bot_config=bot_config)

    events = _load_telemetry_events(telemetry_path)
    if not events:
        return _empty_forward_report(source_exists=True, bot_config=bot_config)

    open_trade: dict[str, Any] | None = None
    closed_trades: list[_ClosedTrade] = []
    rejection_counts: Counter[str] = Counter()
    exit_reason_counts: Counter[str] = Counter()
    orphan_exit_events = 0

    first_event_ts = _parse_event_ts(events[0]["ts"])
    last_event_ts = first_event_ts
    for event in events:
        event_ts = _parse_event_ts(event["ts"])
        last_event_ts = event_ts
        event_type = event.get("event_type", "")
        payload = event.get("payload", {}) or {}

        if event_type == "entry_rejected":
            rejection_counts[str(payload.get("reason", "unknown"))] += 1
            continue

        if event_type == "entry_sent":
            intent = payload.get("intent", {}) or {}
            open_trade = {
                "pair": str(intent.get("pair", "unknown")),
                "entry_ts": event_ts,
            }
            continue

        if event_type not in {"exit_sent", "kill_switch_exit"}:
            continue

        if open_trade is None:
            orphan_exit_events += 1
            continue

        reason = str(payload.get("reason", "kill_switch_exit" if event_type == "kill_switch_exit" else "unknown"))
        pnl_eur = float(payload.get("pnl_eur", 0.0))
        closed_trades.append(
            _ClosedTrade(
                pair=open_trade["pair"],
                entry_ts=open_trade["entry_ts"],
                exit_ts=event_ts,
                pnl_eur=pnl_eur,
                exit_reason=reason,
            )
        )
        exit_reason_counts[reason] += 1
        open_trade = None

    wins = sum(1 for trade in closed_trades if trade.pnl_eur > 0)
    losses = sum(1 for trade in closed_trades if trade.pnl_eur < 0)
    gross_profit = sum(trade.pnl_eur for trade in closed_trades if trade.pnl_eur > 0)
    gross_loss = sum(abs(trade.pnl_eur) for trade in closed_trades if trade.pnl_eur < 0)
    net_pnl = gross_profit - gross_loss

    equity = bot_config.initial_equity_eur
    hwm = equity
    max_drawdown_pct = 0.0
    overnight_positions = 0
    hold_minutes_total = 0.0
    trade_days: set[date] = set()
    pair_stats: dict[str, dict[str, float | int]] = defaultdict(lambda: {"closed_trades": 0, "wins": 0, "losses": 0, "net_pnl_eur": 0.0})

    for trade in closed_trades:
        hold_minutes_total += trade.hold_minutes
        equity += trade.pnl_eur
        hwm = max(hwm, equity)
        if hwm > 0:
            max_drawdown_pct = max(max_drawdown_pct, (hwm - equity) / hwm)

        entry_local = localize(trade.entry_ts, bot_config)
        exit_local = localize(trade.exit_ts, bot_config)
        trade_days.add(exit_local.date())
        if entry_local.date() != exit_local.date():
            overnight_positions += 1

        pair_bucket = pair_stats[trade.pair]
        pair_bucket["closed_trades"] += 1
        pair_bucket["net_pnl_eur"] += trade.pnl_eur
        if trade.pnl_eur > 0:
            pair_bucket["wins"] += 1
        elif trade.pnl_eur < 0:
            pair_bucket["losses"] += 1

    closed_trade_count = len(closed_trades)
    win_rate = (wins / closed_trade_count) if closed_trade_count else 0.0
    if gross_loss == 0:
        profit_factor = float("inf") if gross_profit > 0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss
    average_hold_minutes = (hold_minutes_total / closed_trade_count) if closed_trade_count else 0.0

    first_local = localize(first_event_ts, bot_config)
    last_local = localize(last_event_ts, bot_config)
    forward_days = (last_local.date() - first_local.date()).days + 1

    pair_breakdown = {
        pair: PairForwardSummary(
            closed_trades=int(values["closed_trades"]),
            wins=int(values["wins"]),
            losses=int(values["losses"]),
            net_pnl_eur=float(values["net_pnl_eur"]),
        )
        for pair, values in pair_stats.items()
    }

    gates = {
        "win_rate": GoLiveGate(
            name="win_rate",
            passed=win_rate >= bot_config.min_win_rate_gate,
            actual=round(win_rate, 4),
            threshold=f">= {bot_config.min_win_rate_gate:.2f}",
        ),
        "profit_factor": GoLiveGate(
            name="profit_factor",
            passed=profit_factor >= bot_config.min_profit_factor_gate,
            actual=round(profit_factor, 4) if profit_factor != float("inf") else "inf",
            threshold=f">= {bot_config.min_profit_factor_gate:.2f}",
        ),
        "max_drawdown": GoLiveGate(
            name="max_drawdown",
            passed=max_drawdown_pct <= bot_config.max_drawdown_pct,
            actual=round(max_drawdown_pct, 4),
            threshold=f"<= {bot_config.max_drawdown_pct:.2f}",
        ),
        "trade_count": GoLiveGate(
            name="trade_count",
            passed=closed_trade_count >= bot_config.forward_test_min_trades,
            actual=closed_trade_count,
            threshold=f">= {bot_config.forward_test_min_trades}",
        ),
        "no_overnight_positions": GoLiveGate(
            name="no_overnight_positions",
            passed=overnight_positions == 0,
            actual=overnight_positions == 0,
            threshold="must be true",
        ),
        "net_pnl_positive": GoLiveGate(
            name="net_pnl_positive",
            passed=net_pnl > 0.0,
            actual=round(net_pnl, 4),
            threshold="> 0.0",
        ),
    }

    return ForwardTestReport(
        source_exists=True,
        events_loaded=len(events),
        closed_trades=closed_trade_count,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        profit_factor=profit_factor,
        gross_profit_eur=gross_profit,
        gross_loss_eur=gross_loss,
        net_pnl_eur=net_pnl,
        ending_equity=equity,
        max_drawdown_pct=max_drawdown_pct,
        overnight_positions=overnight_positions,
        average_hold_minutes=average_hold_minutes,
        trade_days=len(trade_days),
        forward_days=forward_days,
        unclosed_entries=1 if open_trade is not None else 0,
        orphan_exit_events=orphan_exit_events,
        rejection_counts=dict(rejection_counts),
        exit_reason_counts=dict(exit_reason_counts),
        pair_breakdown=pair_breakdown,
        gates=gates,
        go_live_ready=all(gate.passed for gate in gates.values()),
    )


def _empty_signal_bucket() -> dict[str, Any]:
    return {
        "contexts": 0,
        "setups_found": 0,
        "first_failures": Counter(),
    }


def _build_signal_bucket(bucket: dict[str, Any]) -> SignalDebugBucket:
    contexts = int(bucket["contexts"])
    setups_found = int(bucket["setups_found"])
    first_failures = dict(bucket["first_failures"])
    leading_failure = ""
    leading_failure_rate = 0.0
    if first_failures:
        leading_failure, count = max(first_failures.items(), key=lambda item: item[1])
        leading_failure_rate = count / max(contexts - setups_found, 1)
    return SignalDebugBucket(
        contexts=contexts,
        setups_found=setups_found,
        setup_rate=(setups_found / contexts) if contexts else 0.0,
        first_failures=first_failures,
        leading_failure=leading_failure,
        leading_failure_rate=leading_failure_rate,
    )


def _load_telemetry_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def _parse_event_ts(raw_ts: str) -> datetime:
    return datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))


def _empty_forward_report(source_exists: bool, bot_config: BotConfig) -> ForwardTestReport:
    gates = {
        "win_rate": GoLiveGate("win_rate", False, 0.0, f">= {bot_config.min_win_rate_gate:.2f}"),
        "profit_factor": GoLiveGate("profit_factor", False, 0.0, f">= {bot_config.min_profit_factor_gate:.2f}"),
        "max_drawdown": GoLiveGate("max_drawdown", True, 0.0, f"<= {bot_config.max_drawdown_pct:.2f}"),
        "trade_count": GoLiveGate("trade_count", False, 0, f">= {bot_config.forward_test_min_trades}"),
        "no_overnight_positions": GoLiveGate("no_overnight_positions", True, True, "must be true"),
        "net_pnl_positive": GoLiveGate("net_pnl_positive", False, 0.0, "> 0.0"),
    }
    return ForwardTestReport(
        source_exists=source_exists,
        events_loaded=0,
        closed_trades=0,
        wins=0,
        losses=0,
        win_rate=0.0,
        profit_factor=0.0,
        gross_profit_eur=0.0,
        gross_loss_eur=0.0,
        net_pnl_eur=0.0,
        ending_equity=bot_config.initial_equity_eur,
        max_drawdown_pct=0.0,
        overnight_positions=0,
        average_hold_minutes=0.0,
        trade_days=0,
        forward_days=0,
        unclosed_entries=0,
        orphan_exit_events=0,
        rejection_counts={},
        exit_reason_counts={},
        pair_breakdown={},
        gates=gates,
        go_live_ready=False,
    )
