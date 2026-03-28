from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import MarketContext
from .strategy import StrategyCheck, StrategyEvaluation
from .telemetry import JsonlTelemetry


@dataclass(frozen=True)
class SignalObservatorySummary:
    source_exists: bool
    observed_signals: int
    tradable_signals: int
    tradable_rate: float
    decision_rejections: int
    pair_breakdown: list[dict[str, Any]]
    regime_breakdown: list[dict[str, Any]]
    setup_breakdown: list[dict[str, Any]]
    rejection_breakdown: list[dict[str, Any]]
    decision_breakdown: list[dict[str, Any]]


class SignalObservatory:
    def __init__(self, telemetry: JsonlTelemetry) -> None:
        self.telemetry = telemetry

    def capture(
        self,
        observations: Iterable[tuple[MarketContext, StrategyEvaluation]],
        *,
        moment: datetime,
        session_open: bool,
        active_trade_present: bool,
        closed_trade_this_tick: bool,
    ) -> None:
        for context, evaluation in observations:
            snapshot = evaluation.snapshot
            payload = {
                "pair": context.symbol,
                "market_ts": moment.isoformat(),
                "session_open": session_open,
                "active_trade_present": active_trade_present,
                "closed_trade_this_tick": closed_trade_this_tick,
                "tradable": evaluation.intent is not None,
                "regime_label": _extract_regime_label(evaluation.checks),
                "setup_type": evaluation.intent.setup_type if evaluation.intent is not None else None,
                "strategy_id": evaluation.intent.strategy_id if evaluation.intent is not None else None,
                "strategy_family": evaluation.intent.strategy_family if evaluation.intent is not None else None,
                "quality": evaluation.intent.quality if evaluation.intent is not None else None,
                "score": evaluation.intent.score if evaluation.intent is not None else None,
                "reason_code": evaluation.intent.reason_code if evaluation.intent is not None else None,
                "rejection_reasons": list(evaluation.rejection_reasons),
                "checks": [
                    {
                        "name": check.name,
                        "passed": check.passed,
                        "threshold": check.threshold,
                        "value": check.value,
                        "reason": check.reason,
                    }
                    for check in evaluation.checks
                ],
                "snapshot": (
                    {
                        "atr_pct_15m": snapshot.atr_pct_15m,
                        "spread_bps": snapshot.spread_bps,
                        "vol_z_5m": snapshot.vol_z_5m,
                        "adx_15m": snapshot.adx_15m,
                        "ema20_15m": snapshot.ema20_15m,
                        "ema50_15m": snapshot.ema50_15m,
                        "vwap_dist_bps": snapshot.vwap_dist_bps,
                        "imbalance_1m": snapshot.imbalance_1m,
                    }
                    if snapshot is not None
                    else None
                ),
            }
            self.telemetry.log("signal_observed", payload, event_ts=moment)


def run_signal_observatory_report(telemetry_path: Path) -> SignalObservatorySummary:
    events = _load_events(telemetry_path)
    observed = [event for event in events if event.get("event_type") == "signal_observed"]
    decision_rejections = [
        event for event in events if event.get("event_type") == "entry_rejected" and event.get("payload", {}).get("reason")
    ]
    pair_counter: Counter[str] = Counter()
    regime_counter: Counter[str] = Counter()
    setup_counter: Counter[str] = Counter()
    rejection_counter: Counter[str] = Counter()
    decision_counter: Counter[str] = Counter()
    tradable = 0

    for event in observed:
        payload = event.get("payload", {}) or {}
        pair_counter[str(payload.get("pair", "unknown"))] += 1
        regime_counter[str(payload.get("regime_label", "unknown"))] += 1
        if payload.get("tradable"):
            tradable += 1
            setup_counter[str(payload.get("setup_type") or "unknown")] += 1
        for reason in payload.get("rejection_reasons", []) or []:
            rejection_counter[str(reason)] += 1

    for event in decision_rejections:
        payload = event.get("payload", {}) or {}
        decision_counter[str(payload.get("reason", "unknown"))] += 1

    observed_count = len(observed)
    return SignalObservatorySummary(
        source_exists=telemetry_path.exists(),
        observed_signals=observed_count,
        tradable_signals=tradable,
        tradable_rate=(tradable / observed_count) if observed_count else 0.0,
        decision_rejections=len(decision_rejections),
        pair_breakdown=_counter_rows(pair_counter),
        regime_breakdown=_counter_rows(regime_counter),
        setup_breakdown=_counter_rows(setup_counter),
        rejection_breakdown=_counter_rows(rejection_counter),
        decision_breakdown=_counter_rows(decision_counter),
    )


def _extract_regime_label(checks: tuple[StrategyCheck, ...]) -> str:
    for check in checks:
        if check.name == "long_regime":
            return str(check.value or "unknown")
        if check.name in {"breakout_regime", "recovery_regime"} and check.value:
            return str(check.value)
    return "unknown"


def _counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    total = sum(counter.values())
    rows: list[dict[str, Any]] = []
    for label, count in counter.most_common():
        rows.append(
            {
                "label": label,
                "value": count,
                "share": (count / total) if total else 0.0,
            }
        )
    return rows


def _load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events
