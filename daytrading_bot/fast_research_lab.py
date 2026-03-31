from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def build_fast_research_lab_payload(strategy_lab: dict[str, Any], telemetry_path: Path) -> dict[str, Any]:
    strategies = [
        {
            "strategy_id": str(row.get("strategy_id") or "n/a"),
            "label": str(row.get("label") or row.get("strategy_id") or "n/a"),
            "family": str(row.get("family") or "unknown"),
            "closed_trades": int(row.get("closed_trades") or 0),
            "win_rate": float(row.get("win_rate") or 0.0),
            "profit_factor": float(row.get("profit_factor") or 0.0),
            "expectancy_eur": float(row.get("expectancy_eur") or 0.0),
            "score": float(row.get("score") or 0.0),
            "eligible_for_promotion": bool(row.get("eligible_for_promotion")),
            "promotion_allowed": bool(((row.get("gates") or {}).get("promotion_allowed") or {}).get("passed", False)),
        }
        for row in (strategy_lab.get("strategies") or [])
        if str(row.get("family") or "") == "fast_trading"
    ]
    experiments = [
        {
            "strategy_id": row["strategy_id"],
            "label": row["label"],
            "status": "research_only" if not row["promotion_allowed"] else ("eligible" if row["eligible_for_promotion"] else "observing"),
            "score": row["score"],
            "closed_trades": row["closed_trades"],
        }
        for row in strategies
    ]
    micro_signals = _load_recent_micro_signals(telemetry_path)
    signal_metrics = _load_micro_signal_metrics(telemetry_path)
    highest_score = max((row["score"] for row in strategies), default=0.0)
    best_expectancy = max((row["expectancy_eur"] for row in strategies), default=0.0)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "title": "Fast-Trading Research Lane",
            "subtitle": "Micro-Strategien laufen nur im sicheren Paper-/Research-Lab",
            "status": "active" if strategies else "waiting_for_data",
            "strategies_seen": len(strategies),
            "eligible_strategies": sum(1 for row in strategies if row["eligible_for_promotion"]),
            "highest_score": highest_score,
            "best_expectancy_eur": best_expectancy,
        },
        "champion_strategy_id": strategy_lab.get("current_paper_strategy_id"),
        "live_candidate_strategy_id": strategy_lab.get("current_live_strategy_id"),
        "strategies": strategies,
        "experiments": experiments,
        "micro_signals": micro_signals,
        "signals": signal_metrics,
        "beginner_notes": [
            {
                "term": "Fast research lane",
                "simple": "Diese Strategien handeln nur im Paper-Lab und duerfen nicht direkt live promoted werden.",
            },
            {
                "term": "Micro signal",
                "simple": "Ein sehr kurzfristiges Marktsetup auf Basis von 1S- und 5S-Bewegungen.",
            },
        ],
    }


def _load_recent_micro_signals(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            event = json.loads(raw)
            if event.get("event_type") != "signal_observed":
                continue
            payload = event.get("payload", {}) or {}
            windows = payload.get("analysis_windows") or {}
            if "1S" not in windows and "5S" not in windows:
                continue
            rows.append(
                {
                    "pair": str(payload.get("pair") or "unknown"),
                    "strategy_id": str(payload.get("strategy_id") or "n/a"),
                    "regime_label": str(payload.get("regime_label") or "unknown"),
                    "setup_type": str(payload.get("setup_type") or "unknown"),
                    "tradable": bool(payload.get("tradable")),
                    "change_1s_bps": float((windows.get("1S") or {}).get("change_pct") or 0.0) * 100.0,
                    "change_5s_bps": float((windows.get("5S") or {}).get("change_pct") or 0.0) * 100.0,
                    "spread_bps": float((payload.get("snapshot") or {}).get("spread_bps") or 0.0),
                    "imbalance_1m": float((payload.get("snapshot") or {}).get("imbalance_1m") or 0.0),
                    "market_ts": str(payload.get("market_ts") or event.get("ts") or ""),
                }
            )
    return sorted(rows, key=lambda row: row["market_ts"], reverse=True)[:30]


def _load_micro_signal_metrics(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"observed": 0, "paper_candidates": 0, "micro_rejections": 0}
    observed = 0
    paper_candidates = 0
    rejection_counter: Counter[str] = Counter()
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            event = json.loads(raw)
            if event.get("event_type") != "signal_observed":
                continue
            payload = event.get("payload", {}) or {}
            strategy_id = str(payload.get("strategy_id") or "")
            strategy_family = str(payload.get("strategy_family") or "")
            if "fast" not in strategy_id and strategy_family != "fast_trading":
                continue
            observed += 1
            if bool(payload.get("tradable")):
                paper_candidates += 1
            for reason in payload.get("rejection_reasons") or []:
                rejection_counter[str(reason)] += 1
    return {
        "observed": observed,
        "paper_candidates": paper_candidates,
        "micro_rejections": sum(rejection_counter.values()),
    }
