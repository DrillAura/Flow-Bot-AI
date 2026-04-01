from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def build_fast_research_lab_payload(strategy_lab: dict[str, Any], telemetry_path: Path) -> dict[str, Any]:
    fast_signal_rows = _load_fast_signal_rows(telemetry_path)
    strategies = [
        {
            "strategy_id": str(row.get("strategy_id") or "n/a"),
            "label": str(row.get("label") or row.get("strategy_id") or "n/a"),
            "family": str(row.get("family") or "unknown"),
            "strategy_type": str(row.get("strategy_type") or row.get("strategy_id") or "unknown"),
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
    micro_signals = _load_recent_micro_signals(fast_signal_rows)
    signal_metrics = _load_micro_signal_metrics(fast_signal_rows)
    compare_payload = _build_fast_compare_payload(strategies, fast_signal_rows, signal_metrics)
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
            "observed_signals": signal_metrics["observed"],
            "paper_candidates": signal_metrics["paper_candidates"],
            "rejection_events": signal_metrics["micro_rejections"],
            "observed_pairs": compare_payload["summary"]["observed_pairs"],
            "observed_setup_types": compare_payload["summary"]["observed_setup_types"],
        },
        "champion_strategy_id": strategy_lab.get("current_paper_strategy_id"),
        "live_candidate_strategy_id": strategy_lab.get("current_live_strategy_id"),
        "strategies": strategies,
        "experiments": experiments,
        "micro_signals": micro_signals,
        "signals": signal_metrics,
        "compare": compare_payload,
        "drilldown": {
            "families": compare_payload["family_rows"][:6],
            "pairs": compare_payload["pair_rows"][:8],
            "rejections": compare_payload["rejection_leaderboard"][:8],
            "strategies": compare_payload["strategy_rows"][:8],
            "summary_cards": compare_payload["summary_cards"],
        },
        "drilldown_summary": compare_payload["summary_cards"],
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


def _build_fast_compare_payload(
    strategies: list[dict[str, Any]],
    signal_rows: list[dict[str, Any]],
    signal_metrics: dict[str, int],
) -> dict[str, Any]:
    by_strategy_id = {row["strategy_id"]: row for row in strategies}
    strategy_rows: list[dict[str, Any]] = []
    family_rows: list[dict[str, Any]] = []
    pair_counter: Counter[str] = Counter()
    setup_counter: Counter[str] = Counter()
    strategy_signal_counter: Counter[str] = Counter()
    pair_signal_counter: Counter[str] = Counter()
    pair_tradable_counter: Counter[str] = Counter()
    setup_tradable_counter: Counter[str] = Counter()
    rejection_counter: Counter[str] = Counter()
    strategy_pair_counter: dict[str, Counter[str]] = {}
    strategy_rejection_counter: dict[str, Counter[str]] = {}
    pair_setup_counter: dict[str, Counter[str]] = {}
    pair_rejection_counter: dict[str, Counter[str]] = {}
    setup_pair_counter: dict[str, Counter[str]] = {}
    setup_rejection_counter: dict[str, Counter[str]] = {}
    pair_metrics: dict[str, list[float]] = defaultdict(list)
    setup_metrics: dict[str, list[float]] = defaultdict(list)

    for row in signal_rows:
        pair = row["pair"]
        setup_type = row["setup_type"]
        strategy_id = row["strategy_id"]
        pair_counter[pair] += 1
        setup_counter[setup_type] += 1
        strategy_signal_counter[strategy_id] += 1
        pair_signal_counter[pair] += 1
        if row["tradable"]:
            pair_tradable_counter[pair] += 1
            setup_tradable_counter[setup_type] += 1
        for reason in row["rejection_reasons"]:
            rejection_counter[reason] += 1
            (strategy_rejection_counter.setdefault(strategy_id, Counter()))[reason] += 1
            (pair_rejection_counter.setdefault(pair, Counter()))[reason] += 1
            (setup_rejection_counter.setdefault(setup_type, Counter()))[reason] += 1
        strategy_pair_counter.setdefault(strategy_id, Counter())[pair] += 1
        pair_setup_counter.setdefault(pair, Counter())[setup_type] += 1
        setup_pair_counter.setdefault(setup_type, Counter())[pair] += 1
        pair_metrics[pair].extend([row["change_1s_bps"], row["change_5s_bps"], row["spread_bps"], row["imbalance_1m"]])
        setup_metrics[setup_type].extend([row["change_1s_bps"], row["change_5s_bps"], row["spread_bps"], row["imbalance_1m"]])

    for row in strategies:
        sid = row["strategy_id"]
        signals = [signal for signal in signal_rows if signal["strategy_id"] == sid]
        pair_counts = strategy_pair_counter.get(sid, Counter())
        rejection_counts = strategy_rejection_counter.get(sid, Counter())
        top_pair = pair_counts.most_common(1)[0][0] if pair_counts else "n/a"
        top_rejection = rejection_counts.most_common(1)[0][0] if rejection_counts else "n/a"
        strategy_rows.append(
            {
                **row,
                "observed_signals": len(signals),
                "tradable_signals": sum(1 for signal in signals if signal["tradable"]),
                "rejection_events": sum(len(signal["rejection_reasons"]) for signal in signals),
                "top_pair": top_pair,
                "top_rejection": top_rejection,
                "avg_change_1s_bps": _avg([signal["change_1s_bps"] for signal in signals]),
                "avg_change_5s_bps": _avg([signal["change_5s_bps"] for signal in signals]),
                "avg_spread_bps": _avg([signal["spread_bps"] for signal in signals]),
                "avg_imbalance_1m": _avg([signal["imbalance_1m"] for signal in signals]),
            }
        )

    for row in strategies:
        setup_type = row["strategy_type"]
        signals = [signal for signal in signal_rows if signal["setup_type"] == setup_type]
        pair_counts = setup_pair_counter.get(setup_type, Counter())
        rejection_counts = setup_rejection_counter.get(setup_type, Counter())
        top_pair = pair_counts.most_common(1)[0][0] if pair_counts else "n/a"
        top_rejection = rejection_counts.most_common(1)[0][0] if rejection_counts else "n/a"
        family_rows.append(
            {
                "strategy_id": row["strategy_id"],
                "label": row["label"],
                "family": row["family"],
                "strategy_type": setup_type,
                "closed_trades": row["closed_trades"],
                "win_rate": row["win_rate"],
                "profit_factor": row["profit_factor"],
                "expectancy_eur": row["expectancy_eur"],
                "score": row["score"],
                "eligible_for_promotion": row["eligible_for_promotion"],
                "promotion_allowed": row["promotion_allowed"],
                "observed_signals": len(signals),
                "tradable_signals": sum(1 for signal in signals if signal["tradable"]),
                "rejection_events": sum(len(signal["rejection_reasons"]) for signal in signals),
                "top_pair": top_pair,
                "top_rejection": top_rejection,
                "avg_change_1s_bps": _avg([signal["change_1s_bps"] for signal in signals]),
                "avg_change_5s_bps": _avg([signal["change_5s_bps"] for signal in signals]),
                "avg_spread_bps": _avg([signal["spread_bps"] for signal in signals]),
                "avg_imbalance_1m": _avg([signal["imbalance_1m"] for signal in signals]),
            }
        )

    pair_rows = [
        {
            "pair": pair,
            "observed_signals": count,
            "tradable_signals": pair_tradable_counter.get(pair, 0),
            "rejection_events": sum(len(signal["rejection_reasons"]) for signal in signal_rows if signal["pair"] == pair),
            "top_setup_type": pair_setup_counter.get(pair, Counter()).most_common(1)[0][0] if pair_setup_counter.get(pair) else "n/a",
            "top_rejection": pair_rejection_counter.get(pair, Counter()).most_common(1)[0][0] if pair_rejection_counter.get(pair) else "n/a",
            "avg_change_1s_bps": _avg([signal["change_1s_bps"] for signal in signal_rows if signal["pair"] == pair]),
            "avg_change_5s_bps": _avg([signal["change_5s_bps"] for signal in signal_rows if signal["pair"] == pair]),
            "avg_spread_bps": _avg([signal["spread_bps"] for signal in signal_rows if signal["pair"] == pair]),
            "avg_imbalance_1m": _avg([signal["imbalance_1m"] for signal in signal_rows if signal["pair"] == pair]),
            "setup_types": sorted(pair_setup_counter.get(pair, Counter()).keys()),
            "rejection_share": (sum(len(signal["rejection_reasons"]) for signal in signal_rows if signal["pair"] == pair) / max(signal_metrics["micro_rejections"], 1)),
        }
        for pair, count in pair_counter.most_common()
    ]
    pair_rows.sort(key=lambda row: (row["observed_signals"], row["tradable_signals"]), reverse=True)

    rejection_leaderboard = [
        {
            "reason": reason,
            "count": count,
            "share": (count / max(signal_metrics["micro_rejections"], 1)),
            "label": reason.replace("_", " "),
        }
        for reason, count in rejection_counter.most_common()
    ]
    sorted_family_rows = sorted(family_rows, key=lambda row: (row["score"], row["observed_signals"]), reverse=True)
    top_family = sorted_family_rows[0] if sorted_family_rows else {}
    top_pair = pair_rows[0] if pair_rows else {}
    top_rejection = rejection_leaderboard[0] if rejection_leaderboard else {}
    compact_summary = [
        {
            "title": "Observed signals",
            "detail": f"{signal_metrics['observed']} micro signals seen, {signal_metrics['paper_candidates']} tradable.",
            "severity": "info",
        },
        {
            "title": "Top family",
            "detail": f"{top_family.get('label', 'n/a')} | score {top_family.get('score', 0.0):.2f} | tradable {top_family.get('tradable_signals', 0)}",
            "severity": "good" if float(top_family.get("score", 0.0) or 0.0) > 0 else "info",
        },
        {
            "title": "Top pair",
            "detail": f"{top_pair.get('pair', 'n/a')} | signals {top_pair.get('observed_signals', 0)} | tradable {top_pair.get('tradable_signals', 0)}",
            "severity": "info",
        },
        {
            "title": "Top rejection",
            "detail": f"{top_rejection.get('reason', 'n/a')} | count {top_rejection.get('count', 0)}",
            "severity": "warn" if top_rejection else "info",
        },
    ]
    return {
        "summary": {
            "observed_pairs": len(pair_rows),
            "observed_setup_types": len(setup_counter),
            "strategy_setup_types": len({row["strategy_type"] for row in strategies}),
            "top_family": top_family.get("label") or "n/a",
            "top_pair": top_pair.get("pair") or "n/a",
            "top_rejection": top_rejection.get("reason") or "n/a",
        },
        "strategy_rows": sorted(strategy_rows, key=lambda row: (row["score"], row["observed_signals"]), reverse=True),
        "family_rows": sorted_family_rows,
        "pair_rows": pair_rows,
        "rejection_leaderboard": rejection_leaderboard,
        "summary_cards": compact_summary,
    }


def _load_recent_micro_signals(rows_or_path: Path | list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _load_fast_signal_rows(rows_or_path) if isinstance(rows_or_path, Path) else rows_or_path
    return sorted(rows, key=lambda row: row["market_ts"], reverse=True)[:30]


def _load_fast_signal_rows(path: Path) -> list[dict[str, Any]]:
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
                    "strategy_family": str(payload.get("strategy_family") or "unknown"),
                    "setup_type": str(payload.get("setup_type") or "unknown"),
                    "regime_label": str(payload.get("regime_label") or "unknown"),
                    "tradable": bool(payload.get("tradable")),
                    "change_1s_bps": float((windows.get("1S") or {}).get("change_pct") or 0.0) * 100.0,
                    "change_5s_bps": float((windows.get("5S") or {}).get("change_pct") or 0.0) * 100.0,
                    "spread_bps": float((payload.get("snapshot") or {}).get("spread_bps") or 0.0),
                    "imbalance_1m": float((payload.get("snapshot") or {}).get("imbalance_1m") or 0.0),
                    "market_ts": str(payload.get("market_ts") or event.get("ts") or ""),
                    "rejection_reasons": [str(reason) for reason in (payload.get("rejection_reasons") or []) if str(reason).strip()],
                }
            )
    return rows


def _load_micro_signal_metrics(rows_or_path: Path | list[dict[str, Any]]) -> dict[str, int]:
    rows = _load_fast_signal_rows(rows_or_path) if isinstance(rows_or_path, Path) else rows_or_path
    if not rows:
        return {"observed": 0, "paper_candidates": 0, "micro_rejections": 0}
    observed = 0
    paper_candidates = 0
    rejection_counter: Counter[str] = Counter()
    for row in rows:
        strategy_id = str(row.get("strategy_id") or "")
        strategy_family = str(row.get("strategy_family") or "")
        if "fast" not in strategy_id and strategy_family != "fast_trading":
            continue
        observed += 1
        if bool(row.get("tradable")):
            paper_candidates += 1
        for reason in row.get("rejection_reasons") or []:
            rejection_counter[str(reason)] += 1
    return {
        "observed": observed,
        "paper_candidates": paper_candidates,
        "micro_rejections": sum(rejection_counter.values()),
    }


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)
