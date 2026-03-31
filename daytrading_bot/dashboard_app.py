from __future__ import annotations

import json
import subprocess
import threading
import unicodedata
import webbrowser
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from .config import BotConfig
from .dashboard import load_supervisor_state_payload
from .fast_research_lab import build_fast_research_lab_payload
from .kraken import KrakenPublicClient, TIMEFRAME_WINDOWS
from .models import PriceSample
from .personal_journal import (
    append_personal_trade,
    build_personal_journal_payload,
    build_personal_trade_entry,
    ensure_personal_journal_path,
    run_personal_journal_report,
)
from .reporting import run_forward_test_report
from .shadow_portfolios import run_shadow_portfolio_report
from .signal_observatory import run_signal_observatory_report
from .storage import load_interval_candles
from .workflows import run_history_status, run_monitor_supervisor


_TICKER_CACHE_LOCK = threading.Lock()
_TICKER_CACHE: dict[str, Any] = {
    "captured_at": 0.0,
    "symbols": tuple(),
    "quotes": {},
    "history": {},
}


def find_latest_supervisor_state_path(logs_root: Path) -> Path | None:
    candidates: list[tuple[float, Path]] = []
    for pattern in ("supervisor_watchdog_*", "paper_forward_supervisor_*"):
        for run_dir in logs_root.glob(pattern):
            state_path = run_dir / "supervisor_state.json"
            if state_path.exists():
                candidates.append((state_path.stat().st_mtime, state_path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _infer_project_root(data_dir: Path, logs_root: Path) -> Path:
    if logs_root.name == "ops" and logs_root.parent.name == "logs":
        return logs_root.parent.parent
    return data_dir.parent


def _resolve_telemetry_path(project_root: Path, telemetry_path: str) -> Path:
    path = Path(telemetry_path)
    if path.is_absolute():
        return path
    return project_root / path


def _load_json_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_live_ticker_snapshots(symbols: list[str], ttl_seconds: float = 10.0) -> dict[str, dict[str, float]]:
    now = monotonic()
    symbol_key = tuple(sorted(symbols))
    with _TICKER_CACHE_LOCK:
        if (
            _TICKER_CACHE["quotes"]
            and _TICKER_CACHE["symbols"] == symbol_key
            and now - float(_TICKER_CACHE["captured_at"]) < ttl_seconds
        ):
            return dict(_TICKER_CACHE["quotes"])

    client = KrakenPublicClient()
    quotes: dict[str, dict[str, float]] = {}
    captured_at_iso = datetime.now(timezone.utc).isoformat()
    for symbol in symbols:
        try:
            quotes[symbol] = client.fetch_ticker(symbol)
        except Exception:
            continue

    with _TICKER_CACHE_LOCK:
        _TICKER_CACHE["captured_at"] = now
        _TICKER_CACHE["symbols"] = symbol_key
        _TICKER_CACHE["quotes"] = dict(quotes)
        history = _TICKER_CACHE.setdefault("history", {})
        for symbol, quote in quotes.items():
            bucket = history.setdefault(symbol, [])
            bucket.append(
                {
                    "ts": captured_at_iso,
                    "price": float(quote.get("last") or 0.0),
                    "bid": float(quote.get("bid") or 0.0),
                    "ask": float(quote.get("ask") or 0.0),
                }
            )
            if len(bucket) > 7200:
                del bucket[:-7200]
    return quotes


def load_live_ticker_history(symbol: str) -> list[PriceSample]:
    with _TICKER_CACHE_LOCK:
        rows = list((_TICKER_CACHE.get("history") or {}).get(symbol, []))
    samples: list[PriceSample] = []
    for row in rows:
        raw_ts = row.get("ts")
        if not raw_ts:
            continue
        try:
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        samples.append(
            PriceSample(
                ts=ts,
                price=float(row.get("price") or 0.0),
                bid=float(row.get("bid") or 0.0) or None,
                ask=float(row.get("ask") or 0.0) or None,
            )
        )
    return samples


def _compress_series(values: list[float], target_points: int = 36) -> list[float]:
    if not values:
        return []
    if len(values) <= target_points:
        return [round(value, 6) for value in values]
    if target_points <= 1:
        return [round(values[-1], 6)]
    last_index = len(values) - 1
    step = last_index / float(target_points - 1)
    return [round(values[round(index * step)], 6) for index in range(target_points)]


def _lookback_change_pct(candles: list[Any], minutes: int, current_price: float | None = None) -> float | None:
    if not candles:
        return None
    target_ts = candles[-1].ts - timedelta(minutes=minutes)
    if candles[0].ts > target_ts:
        return None
    baseline = candles[0].close
    for candle in candles:
        if candle.ts <= target_ts:
            baseline = candle.close
            continue
        break
    latest_price = current_price if current_price is not None else candles[-1].close
    if baseline <= 0:
        return None
    return ((latest_price - baseline) / baseline) * 100.0


def _window_range_pct(candles: list[Any]) -> float | None:
    if not candles:
        return None
    high = max(candle.high for candle in candles)
    low = min(candle.low for candle in candles)
    close = candles[-1].close
    if close <= 0:
        return None
    return ((high - low) / close) * 100.0


def _format_pair_market_card(symbol: str, candles_1m: list[Any], live_quote: dict[str, float] | None) -> dict[str, Any]:
    if not candles_1m:
        return {
            "symbol": symbol,
            "price": None,
            "bid": None,
            "ask": None,
            "spread_bps": None,
            "change_1h_pct": None,
            "change_24h_pct": None,
            "range_24h_pct": None,
            "volume_24h": None,
            "trades_24h": None,
            "live_source": "missing",
            "last_candle_ts": None,
            "freshness_seconds": None,
            "sparkline": [],
            "timeframe_profiles": {},
            "window_badges": [],
        }

    now = datetime.now(timezone.utc)
    local_close = candles_1m[-1].close
    bid = live_quote.get("bid") if live_quote else None
    ask = live_quote.get("ask") if live_quote else None
    price = live_quote.get("last", local_close) if live_quote else local_close
    spread_bps = None
    if bid is not None and ask is not None and price and price > 0:
        spread_bps = ((ask - bid) / price) * 10_000.0
    trailing_24h = candles_1m[-1440:] if len(candles_1m) >= 1440 else candles_1m
    return {
        "symbol": symbol,
        "price": price,
        "bid": bid,
        "ask": ask,
        "spread_bps": spread_bps,
        "change_1h_pct": _lookback_change_pct(candles_1m, 60, current_price=price),
        "change_24h_pct": _lookback_change_pct(candles_1m, 24 * 60, current_price=price),
        "range_24h_pct": _window_range_pct(trailing_24h),
        "volume_24h": live_quote.get("volume_24h") if live_quote else sum(candle.volume for candle in trailing_24h),
        "trades_24h": live_quote.get("trades_24h") if live_quote else None,
        "high_24h": live_quote.get("high_24h") if live_quote else max(candle.high for candle in trailing_24h),
        "low_24h": live_quote.get("low_24h") if live_quote else min(candle.low for candle in trailing_24h),
        "live_source": "kraken_rest" if live_quote else "local_history",
        "last_candle_ts": candles_1m[-1].ts.isoformat(),
        "freshness_seconds": max((now - candles_1m[-1].ts.astimezone(timezone.utc)).total_seconds(), 0.0),
        "sparkline": _compress_series([candle.close for candle in candles_1m[-180:]], target_points=42),
        "timeframe_profiles": {},
        "window_badges": [],
    }


def build_market_overview(bot_config: BotConfig, data_dir: Path) -> dict[str, Any]:
    symbols = [pair.symbol for pair in bot_config.pairs]
    live_quotes = load_live_ticker_snapshots(symbols)
    pair_cards = [
        _format_pair_market_card(symbol, load_interval_candles(data_dir, symbol, 1), live_quotes.get(symbol))
        for symbol in symbols
    ]
    timeframe_labels = [label for label, _ in TIMEFRAME_WINDOWS]
    for card in pair_cards:
        candles_1m = load_interval_candles(data_dir, card["symbol"], 1)
        live_quote = live_quotes.get(card["symbol"])
        micro_samples = load_live_ticker_history(card["symbol"])
        profiles = KrakenPublicClient.build_timeframe_profiles(
            candles_1m,
            live_price=live_quote.get("last") if live_quote else None,
            live_ts=micro_samples[-1].ts if micro_samples else None,
            micro_samples=micro_samples,
        )
        card["timeframe_profiles"] = profiles
        card["window_badges"] = [
            {
                "label": label,
                "change_pct": profiles.get(label, {}).get("change_pct"),
                "coverage_pct": profiles.get(label, {}).get("coverage_pct"),
                "available": profiles.get(label, {}).get("available"),
            }
            for label in timeframe_labels
        ]
    strongest = max(
        pair_cards,
        key=lambda item: abs(item.get("change_1h_pct") or 0.0),
        default=None,
    )
    tightest = min(
        [item for item in pair_cards if item.get("spread_bps") is not None],
        key=lambda item: item["spread_bps"],
        default=None,
    )
    breadth_rows: list[dict[str, Any]] = []
    for label in timeframe_labels:
        label_rows = [card["timeframe_profiles"].get(label, {}) for card in pair_cards if card["timeframe_profiles"].get(label)]
        if not label_rows:
            continue
        positive = sum(1 for row in label_rows if (row.get("change_pct") or 0.0) > 0)
        negative = sum(1 for row in label_rows if (row.get("change_pct") or 0.0) < 0)
        neutral = len(label_rows) - positive - negative
        best_row = max(label_rows, key=lambda row: row.get("change_pct") or float("-inf"))
        worst_row = min(label_rows, key=lambda row: row.get("change_pct") or float("inf"))
        avg_change = sum(float(row.get("change_pct") or 0.0) for row in label_rows) / len(label_rows)
        avg_coverage = sum(float(row.get("coverage_pct") or 0.0) for row in label_rows) / len(label_rows)
        breadth_rows.append(
            {
                "label": label,
                "positive": positive,
                "negative": negative,
                "neutral": neutral,
                "avg_change_pct": avg_change,
                "avg_coverage_pct": avg_coverage,
                "best_symbol": best_row.get("symbol") or best_row.get("label") or "n/a",
                "best_change_pct": best_row.get("change_pct"),
                "worst_symbol": worst_row.get("symbol") or worst_row.get("label") or "n/a",
                "worst_change_pct": worst_row.get("change_pct"),
            }
        )

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "pairs": pair_cards,
        "leader_symbol": strongest["symbol"] if strongest else None,
        "tightest_spread_symbol": tightest["symbol"] if tightest else None,
        "selected_symbol": strongest["symbol"] if strongest else (pair_cards[0]["symbol"] if pair_cards else None),
        "selected_timeframe": "1D",
        "timeframe_options": [{"label": label, "minutes": minutes} for label, minutes in TIMEFRAME_WINDOWS],
        "breadth_rows": breadth_rows,
    }


def build_launch_overview(
    *,
    history_status: dict[str, Any],
    state_payload: dict[str, Any],
    forward_report: dict[str, Any],
) -> dict[str, Any]:
    summary = state_payload.get("daily_summary") or {}
    last_prepare = state_payload.get("last_prepare_report") or {}
    optimization = last_prepare.get("walk_forward_optimization") or {}
    gate = last_prepare.get("paper_forward_gate") or {}
    history_ready = bool(history_status.get("sufficient_history"))
    gate_status = str(summary.get("gate_status", "pending"))
    paper_status = str(summary.get("paper_forward_status", "idle"))
    go_live_ready = bool(forward_report.get("go_live_ready"))

    phases: list[dict[str, Any]] = [
        {
            "key": "history_capture",
            "label": "History Capture",
            "status": "completed" if history_ready else "active",
            "headline": f"{history_status.get('available_days', 0):.2f} / {history_status.get('required_days', 13)} days",
            "detail": "Kraken 1m and 15m local history must reach the full 10d/3d OOS window before research can escalate.",
            "completion_pct": float(history_status.get("progress_pct", 0.0) or 0.0),
        },
        {
            "key": "walk_forward",
            "label": "Walk-Forward",
            "status": "pending" if not history_ready else ("completed" if optimization and not optimization.get("insufficient_history", False) else "active"),
            "headline": "OOS folds and variant ranking",
            "detail": "Recovery and breakout variants are ranked only on walk-forward output, not on a single in-sample window.",
            "completion_pct": 100.0 if optimization and not optimization.get("insufficient_history", False) else (0.0 if not history_ready else 55.0),
        },
        {
            "key": "gate",
            "label": "Release Gate",
            "status": "completed" if gate_status == "green" else ("blocked" if gate_status == "red" else ("pending" if gate_status.startswith("waiting") else "active")),
            "headline": gate_status.replace("_", " "),
            "detail": "The E2E harness, forward metrics and OOS checks must all pass before paper-forward is armed.",
            "completion_pct": 100.0 if gate_status == "green" else (0.0 if gate_status.startswith("waiting") else 60.0),
        },
        {
            "key": "paper_forward",
            "label": "Paper Forward",
            "status": "active" if paper_status in {"running", "started"} else ("blocked" if paper_status in {"blocked_by_gate", "launch_failed"} else "pending"),
            "headline": paper_status.replace("_", " "),
            "detail": "The bot starts its next supervised paper phase only after the release gate turns green.",
            "completion_pct": 100.0 if paper_status in {"running", "started"} else 0.0,
        },
        {
            "key": "live_candidate",
            "label": "Live Candidate",
            "status": "ready" if go_live_ready else "pending",
            "headline": "Launch candidate",
            "detail": "This phase remains dormant until paper-forward statistics and gate readiness jointly clear the production threshold.",
            "completion_pct": 100.0 if go_live_ready else 0.0,
        },
    ]

    current_phase = next((phase["label"] for phase in phases if phase["status"] in {"active", "blocked"}), phases[-1]["label"])
    if not history_ready:
        next_action = "Continue background capture until the local 10d/3d OOS history window is complete."
    elif gate_status != "green":
        next_action = "Run walk-forward optimization on the full local history and clear the release gate."
    elif paper_status not in {"running", "started"}:
        next_action = "Arm the next paper-forward cycle and verify live telemetry against the release gate."
    else:
        next_action = "Observe paper-forward telemetry, forward gates and runtime stability before any live escalation."

    return {
        "current_phase": current_phase,
        "next_action": next_action,
        "phases": phases,
        "walk_forward_ready": bool(optimization) and not bool(optimization.get("insufficient_history", False)),
        "gate_ready": bool(summary.get("gate_ready")),
        "paper_forward_status": paper_status,
        "go_live_ready": go_live_ready,
        "gate_blockers": list(summary.get("gate_blockers") or gate.get("failed_conditions") or []),
    }


def build_analytics_overview(
    *,
    recent_runs: list[dict[str, Any]],
    last_cycle: dict[str, Any],
    forward_report: dict[str, Any],
    trade_analytics: dict[str, Any],
) -> dict[str, Any]:
    progress_series = [
        {
            "label": run["name"].replace("supervisor_watchdog_", "").replace("paper_forward_supervisor_", ""),
            "progress_pct": run.get("progress_pct"),
            "available_days": run.get("available_days"),
            "status": run.get("status"),
        }
        for run in reversed(recent_runs)
        if run.get("progress_pct") is not None
    ]
    sync_totals = [
        {
            "interval": interval,
            "written_rows": totals.get("written_rows"),
            "merged_rows": totals.get("merged_rows"),
            "fetched_rows": totals.get("fetched_rows"),
        }
        for interval, totals in sorted((last_cycle.get("interval_totals") or {}).items())
    ]
    forward_gates = [
        {
            "name": name,
            "passed": payload.get("passed"),
            "actual": payload.get("actual"),
            "threshold": payload.get("threshold"),
        }
        for name, payload in sorted((forward_report.get("gates") or {}).items())
    ]
    return {
        "progress_series": progress_series,
        "sync_totals": sync_totals,
        "forward_gates": forward_gates,
        "equity_curve": trade_analytics.get("equity_curve", []),
        "pnl_curve": trade_analytics.get("pnl_curve", []),
        "daily_pnl_series": trade_analytics.get("daily_pnl_series", []),
        "exit_reason_breakdown": trade_analytics.get("exit_reason_breakdown", []),
        "pair_performance": trade_analytics.get("pair_performance", []),
        "quality_breakdown": trade_analytics.get("quality_breakdown", []),
    }


def build_personal_journal_overview(raw_payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    entries = [entry for entry in (payload.get("entries") or []) if isinstance(entry, dict)]
    strategy_notes = [_normalize_journal_learning_entry(entry, fallback_title="Strategy note") for entry in (payload.get("strategy_notes") or []) if isinstance(entry, (dict, str))]
    learning_points = [_normalize_journal_learning_entry(entry, fallback_title="Learning point") for entry in (payload.get("learning_points") or []) if isinstance(entry, (dict, str))]
    asset_breakdown = [entry for entry in (payload.get("asset_breakdown") or []) if isinstance(entry, dict)]
    kpis = summary.get("kpis") if isinstance(summary.get("kpis"), list) else []
    beginner_notes = [_normalize_journal_learning_entry(entry, fallback_title="Beginner note") for entry in (payload.get("beginner_notes") or []) if isinstance(entry, (dict, str))]
    recent_activity = entries[:8]
    return {
        "source_exists": bool(payload),
        "updated_at": payload.get("updated_at"),
        "summary": {
            "title": summary.get("title", "Personal Trading Journal"),
            "subtitle": summary.get("subtitle", "Manuelle Trades, Learnings und Strategien in einer Sammelstelle"),
            "total_entries": int(summary.get("total_entries") or len(entries)),
            "winning_entries": int(summary.get("winning_entries") or sum(1 for entry in entries if float(entry.get("pnl_eur") or 0.0) > 0.0)),
            "losing_entries": int(summary.get("losing_entries") or sum(1 for entry in entries if float(entry.get("pnl_eur") or 0.0) < 0.0)),
            "win_rate": float(summary.get("win_rate") or (sum(1 for entry in entries if float(entry.get("pnl_eur") or 0.0) > 0.0) / len(entries) if entries else 0.0)),
            "realized_pnl_eur": float(summary.get("realized_pnl_eur") or sum(float(entry.get("pnl_eur") or 0.0) for entry in entries)),
            "largest_win_eur": float(summary.get("largest_win_eur") or max((float(entry.get("pnl_eur") or 0.0) for entry in entries), default=0.0)),
            "largest_loss_eur": float(summary.get("largest_loss_eur") or min((float(entry.get("pnl_eur") or 0.0) for entry in entries), default=0.0)),
            "active_strategies": int(summary.get("active_strategies") or len({str(entry.get("strategy") or "n/a") for entry in entries})),
            "tracked_assets": int(summary.get("tracked_assets") or len({str(entry.get("asset") or "n/a") for entry in entries})),
        },
        "kpis": kpis,
        "entries": entries,
        "strategy_notes": strategy_notes,
        "learning_points": learning_points,
        "asset_breakdown": asset_breakdown,
        "beginner_notes": beginner_notes,
        "recent_activity": recent_activity,
        "filter_options": {
            "assets": sorted({str(entry.get("asset") or "").upper() for entry in entries if entry.get("asset")} ),
            "strategies": sorted({str(entry.get("strategy") or "").strip() for entry in entries if entry.get("strategy")} ),
            "tags": sorted({str(tag) for entry in entries for tag in (entry.get("tags") or []) if tag}),
        },
        "charts": {
            "pnl_series": [float(entry.get("pnl_eur") or 0.0) for entry in entries][-30:],
            "confidence_series": [float(entry.get("confidence") or 0.0) for entry in entries][-30:],
            "win_loss_series": [
                {"label": "Wins", "value": sum(1 for entry in entries if float(entry.get("pnl_eur") or 0.0) > 0.0)},
                {"label": "Losses", "value": sum(1 for entry in entries if float(entry.get("pnl_eur") or 0.0) < 0.0)},
            ],
        },
    }


def build_fast_research_lab_overview(
    raw_payload: dict[str, Any] | None,
    *,
    strategy_lab: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    strategies = [row for row in (payload.get("strategies") or []) if isinstance(row, dict)]
    experiments = [row for row in (payload.get("experiments") or []) if isinstance(row, dict)]
    micro_signals = [row for row in (payload.get("micro_signals") or []) if isinstance(row, dict)]
    strategy_lab_payload = strategy_lab if isinstance(strategy_lab, dict) else {}
    champion = str(payload.get("champion_strategy_id") or strategy_lab_payload.get("current_paper_strategy_id") or "n/a")
    live_candidate = str(payload.get("live_candidate_strategy_id") or strategy_lab_payload.get("current_live_strategy_id") or "n/a")
    return {
        "source_exists": bool(payload),
        "updated_at": payload.get("updated_at"),
        "summary": {
            "title": summary.get("title", "Fast-Trading Research Lane"),
            "subtitle": summary.get("subtitle", "Neue Micro-Strategien nur im sicheren Paper-Lab"),
            "status": summary.get("status", "waiting_for_data"),
            "champion_strategy_id": champion,
            "live_candidate_strategy_id": live_candidate,
            "strategies_seen": int(summary.get("strategies_seen") or len(strategies)),
            "eligible_strategies": int(summary.get("eligible_strategies") or sum(1 for row in strategies if bool(row.get("eligible_for_promotion")))),
            "highest_score": float(summary.get("highest_score") or max((float(row.get("score") or 0.0) for row in strategies), default=0.0)),
            "best_expectancy_eur": float(summary.get("best_expectancy_eur") or max((float(row.get("expectancy_eur") or 0.0) for row in strategies), default=0.0)),
        },
        "strategies": strategies,
        "experiments": experiments,
        "micro_signals": micro_signals,
        "filter_options": {
            "families": sorted({str(row.get("family") or "") for row in strategies if row.get("family")} ),
            "statuses": sorted({str(row.get("status") or "") for row in experiments if row.get("status")} ),
            "regimes": sorted({str(row.get("regime_label") or "") for row in micro_signals if row.get("regime_label")} ),
        },
        "ranking": sorted(
            [
                {
                    "strategy_id": str(row.get("strategy_id") or row.get("label") or "n/a"),
                    "label": str(row.get("label") or row.get("strategy_id") or "n/a"),
                    "score": float(row.get("score") or 0.0),
                    "win_rate": float(row.get("win_rate") or 0.0),
                    "profit_factor": float(row.get("profit_factor") or 0.0),
                    "expectancy_eur": float(row.get("expectancy_eur") or 0.0),
                    "status": str(row.get("status") or ("eligible" if row.get("eligible_for_promotion") else "watch")),
                    "notes": str(row.get("notes") or ""),
                }
                for row in strategies
            ],
            key=lambda row: row["score"],
            reverse=True,
        ),
        "beginner_notes": [_normalize_journal_learning_entry(entry, fallback_title="Fast note") for entry in (payload.get("beginner_notes") or []) if isinstance(entry, (dict, str))],
        "signals": {
            "observed": int((payload.get("signals") or {}).get("observed") or payload.get("observed_signals") or len(micro_signals)),
            "paper_candidates": int((payload.get("signals") or {}).get("paper_candidates") or payload.get("paper_candidates") or sum(1 for row in micro_signals if row.get("tradable"))),
            "micro_rejections": int((payload.get("signals") or {}).get("micro_rejections") or payload.get("micro_rejections") or max(len(micro_signals) - sum(1 for row in micro_signals if row.get("tradable")), 0)),
        },
    }


def _normalize_journal_learning_entry(entry: dict[str, Any] | str, *, fallback_title: str) -> dict[str, Any]:
    if isinstance(entry, str):
        return {"title": fallback_title, "detail": entry, "takeaway": ""}
    title = (
        entry.get("title")
        or entry.get("term")
        or entry.get("strategy")
        or entry.get("label")
        or fallback_title
    )
    detail = (
        entry.get("detail")
        or entry.get("note")
        or entry.get("simple")
        or (f"Count {entry.get('value')}" if entry.get("value") is not None else "")
        or "n/a"
    )
    takeaway = (
        entry.get("takeaway")
        or entry.get("reason")
        or entry.get("category")
        or ""
    )
    return {"title": title, "detail": detail, "takeaway": takeaway, **entry}


def build_journal_strategy_alignment_overview(
    personal_journal: dict[str, Any] | None,
    strategy_lab: dict[str, Any] | None,
    fast_research_lab: dict[str, Any] | None,
) -> dict[str, Any]:
    journal = personal_journal if isinstance(personal_journal, dict) else {}
    strategy_payload = strategy_lab if isinstance(strategy_lab, dict) else {}
    fast_payload = fast_research_lab if isinstance(fast_research_lab, dict) else {}
    entries = [entry for entry in (journal.get("entries") or []) if isinstance(entry, dict)]
    strategy_rows = [row for row in (strategy_payload.get("strategies") or []) if isinstance(row, dict)]
    fast_rows = [row for row in (fast_payload.get("strategies") or []) if isinstance(row, dict)]
    family_map = {
        "breakout_recovery": ("breakout", "recovery", "reclaim"),
        "mean_reversion": ("mean", "reversion", "vwap"),
        "opening_range": ("opening range", "orb"),
        "trend_continuation": ("trend", "continuation", "pullback"),
        "fast_trading": ("scalp", "micro", "fast"),
    }
    family_counts: Counter[str] = Counter()
    asset_counts: Counter[str] = Counter()
    mistake_counts: Counter[str] = Counter()
    for entry in entries:
        asset = str(entry.get("asset") or "").upper().strip()
        if asset:
            asset_counts[asset] += 1
        combined = " ".join(
            [
                str(entry.get("strategy") or ""),
                str(entry.get("setup_family") or ""),
                str(entry.get("lesson") or ""),
                str(entry.get("notes") or ""),
                " ".join(str(tag) for tag in (entry.get("tags") or [])),
            ]
        ).lower()
        for family, keywords in family_map.items():
            if any(keyword in combined for keyword in keywords):
                family_counts[family] += 1
        for mistake in entry.get("mistakes") or []:
            mistake_counts[str(mistake)] += 1

    bot_families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in strategy_rows:
        bot_families[str(row.get("family") or "unknown")].append(row)
    family_alignment = [
        {
            "family": family,
            "manual_trades": int(family_counts.get(family, 0)),
            "bot_strategies": len(bot_families.get(family, [])),
            "eligible_strategies": sum(1 for row in bot_families.get(family, []) if bool(row.get("eligible_for_promotion"))),
            "champion_present": any(str(row.get("strategy_id") or "") == str(strategy_payload.get("current_paper_strategy_id") or "") for row in bot_families.get(family, [])),
        }
        for family in family_map
    ]
    tracked_assets = {str(row.get("label") or row.get("asset") or "").upper().replace("EUR", "") for row in (journal.get("asset_breakdown") or []) if isinstance(row, dict)}
    bot_assets = {
        str(row.get("label") or "").upper().replace("EUR", "")
        for row in sum((list(item.get("asset_breakdown") or []) for item in [strategy_payload] if isinstance(item, dict)), [])
        if isinstance(row, dict)
    }
    if not bot_assets:
        bot_assets = {str(row.get("pair") or "").upper().replace("EUR", "") for row in (strategy_rows + fast_rows)}
    asset_alignment = [
        {
            "asset": asset,
            "manual_trades": count,
            "tracked_by_bot": asset in bot_assets or asset in tracked_assets,
            "fast_lane_seen": any(asset in str(row.get("label") or "").upper() or asset in str(row.get("strategy_id") or "").upper() for row in fast_rows),
        }
        for asset, count in asset_counts.most_common(10)
    ]
    guardrail_map = {
        "late_stop": "Harter Stop und Time-Decay muessen frueher greifen.",
        "late_exit": "Gewinne oder Verluste nicht durch Hoffen aussitzen.",
        "fomo": "Nur auf definierte Reclaim-/Pullback-Zonen einsteigen.",
        "overtrade": "Trade-Limit und Cooldown konsequent respektieren.",
        "revenge": "Nach Verlusten keine aggressive Eskalation zulassen.",
        "size": "Risk-per-trade klein halten statt ueber Positionsgroesse zu kompensieren.",
    }
    guardrails = [
        {
            "mistake": mistake,
            "count": count,
            "guardrail": next((text for key, text in guardrail_map.items() if key in mistake.lower()), "Diese Fehlerart braucht eine klarere Pre-Trade-Checkliste."),
        }
        for mistake, count in mistake_counts.most_common(8)
    ]
    strongest_family = family_alignment[0]["family"] if family_alignment else "n/a"
    if family_alignment:
        strongest_family = max(family_alignment, key=lambda row: row["manual_trades"])["family"]
    recommended_focus = "Journal erst befuellen, dann wird die Auswertung belastbar."
    if strongest_family == "fast_trading":
        recommended_focus = "Deine manuellen Muster liegen nah an der Fast-Research-Lane. Beobachte dort Micro-Signale und Rejection-Gruende."
    elif strongest_family == "breakout_recovery":
        recommended_focus = "Deine manuellen Muster deuten auf Breakout-/Recovery-Logik. Vergleiche diese direkt mit dem Champion und den Recovery-Challengern."
    elif strongest_family == "mean_reversion":
        recommended_focus = "Du handelst bereits Mean-Reversion-artig. Beobachte VWAP- und Reclaim-Strategien enger."
    return {
        "summary": {
            "manual_entries": len(entries),
            "matched_families": sum(1 for row in family_alignment if row["manual_trades"] > 0),
            "overlapping_assets": sum(1 for row in asset_alignment if row["tracked_by_bot"]),
            "guardrail_matches": len(guardrails),
            "strongest_family": strongest_family,
            "recommended_focus": recommended_focus,
        },
        "family_alignment": family_alignment,
        "asset_alignment": asset_alignment,
        "guardrails": guardrails,
        "beginner_notes": [
            {
                "term": "Human vs Bot",
                "simple": "Hier siehst du, ob deine manuellen Gewohnheiten eher zu Breakout-, Mean-Reversion- oder Fast-Strategien passen.",
            },
            {
                "term": "Guardrail",
                "simple": "Ein Guardrail ist eine feste Regel, die typische menschliche Fehler wie FOMO oder zu spaete Stops abfangen soll.",
            },
        ],
    }


def _load_dashboard_telemetry_events(path: Path) -> list[dict[str, Any]]:
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


def _parse_dashboard_event_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_trade_analytics(bot_config: BotConfig, telemetry_path: Path) -> dict[str, Any]:
    events = _load_dashboard_telemetry_events(telemetry_path)
    if not events:
        return {
            "source_exists": telemetry_path.exists(),
            "events_loaded": 0,
            "equity_curve": [],
            "pnl_curve": [],
            "daily_pnl_series": [],
            "exit_reason_breakdown": [],
            "pair_performance": [],
            "quality_breakdown": [],
            "mae_mfe_points": [],
            "recent_trades": [],
            "all_trades": [],
            "filter_options": {"pairs": [], "qualities": [], "reasons": [], "setups": [], "limits": [12, 25, 50, 100]},
            "summary": {
                "closed_trades": 0,
                "net_pnl_eur": 0.0,
                "ending_equity": bot_config.initial_equity_eur,
                "avg_pnl_per_trade_eur": 0.0,
                "avg_hold_minutes": 0.0,
                "winning_trades": 0,
                "losing_trades": 0,
                "best_trade_eur": 0.0,
                "worst_trade_eur": 0.0,
                "avg_mae_r": 0.0,
                "avg_mfe_r": 0.0,
                "avg_total_fee_eur": 0.0,
                "avg_total_slippage_bps": 0.0,
            },
        }

    equity = bot_config.initial_equity_eur
    cumulative_pnl = 0.0
    open_trade: dict[str, Any] | None = None
    equity_curve: list[dict[str, Any]] = []
    pnl_curve: list[dict[str, Any]] = []
    daily_pnl: dict[str, dict[str, Any]] = defaultdict(lambda: {"label": "", "value": 0.0, "trades": 0})
    exit_reason_counter: Counter[str] = Counter()
    quality_counter: Counter[str] = Counter()
    pair_performance: dict[str, dict[str, Any]] = defaultdict(lambda: {"label": "", "value": 0.0, "wins": 0, "losses": 0, "trades": 0})
    recent_trades: list[dict[str, Any]] = []
    hold_minutes_total = 0.0
    mae_r_total = 0.0
    mfe_r_total = 0.0
    total_fee_eur_total = 0.0
    total_slippage_bps_total = 0.0
    winning_trades = 0
    losing_trades = 0
    best_trade = None
    worst_trade = None

    for event in events:
        event_type = str(event.get("event_type", ""))
        payload = event.get("payload", {}) or {}
        event_ts = _parse_dashboard_event_ts(event.get("ts"))
        if event_type == "entry_sent":
            intent = payload.get("intent", {}) or {}
            open_trade = {
                "pair": str(intent.get("pair", "unknown")),
                "entry_ts": event_ts,
                "quality": str(intent.get("quality", "n/a")),
                "score": float(intent.get("score", 0.0)),
                "reason_code": str(intent.get("reason_code", "unknown")),
                "budget_eur": float(intent.get("budget_eur", 0.0)),
                "setup_type": str(intent.get("setup_type", "unknown")),
                "regime_label": str(intent.get("regime_label", "unknown")),
                "strategy_id": str(intent.get("strategy_id", "unknown")),
                "entry_fill_price": float(payload.get("fill_price", intent.get("entry_zone", 0.0))),
                "entry_fee_eur": float(payload.get("fee_eur", 0.0)),
                "entry_fee_rate": float(payload.get("fee_rate", 0.0)),
                "entry_slippage_bps": float(payload.get("slippage_bps", 0.0)),
                "entry_liquidity_role": str(payload.get("liquidity_role", "n/a")),
                "entry_maker_probability": float(payload.get("maker_probability", 0.0)),
            }
            continue
        if event_type not in {"exit_sent", "kill_switch_exit"} or open_trade is None:
            continue

        pnl = float(payload.get("pnl_eur", 0.0))
        equity += pnl
        cumulative_pnl += pnl
        reason = str(payload.get("reason", "kill_switch_exit" if event_type == "kill_switch_exit" else "unknown"))
        hold_minutes = max((event_ts - open_trade["entry_ts"]).total_seconds() / 60.0, 0.0)
        mae_r = float(payload.get("mae_r", 0.0))
        mfe_r = float(payload.get("mfe_r", 0.0))
        total_fee_eur = float(payload.get("total_fee_eur", 0.0))
        total_slippage_bps = float(payload.get("entry_slippage_bps", 0.0)) + float(payload.get("exit_slippage_bps", 0.0))
        hold_minutes_total += hold_minutes
        mae_r_total += mae_r
        mfe_r_total += mfe_r
        total_fee_eur_total += total_fee_eur
        total_slippage_bps_total += total_slippage_bps
        if pnl > 0:
            winning_trades += 1
        elif pnl < 0:
            losing_trades += 1
        best_trade = pnl if best_trade is None else max(best_trade, pnl)
        worst_trade = pnl if worst_trade is None else min(worst_trade, pnl)
        local_exit = event_ts.astimezone(bot_config.timezone)
        day_key = local_exit.date().isoformat()
        day_bucket = daily_pnl[day_key]
        day_bucket["label"] = local_exit.strftime("%d.%m")
        day_bucket["value"] += pnl
        day_bucket["trades"] += 1
        exit_reason_counter[reason] += 1
        quality_counter[open_trade["quality"]] += 1

        pair_bucket = pair_performance[open_trade["pair"]]
        pair_bucket["label"] = open_trade["pair"]
        pair_bucket["value"] += pnl
        pair_bucket["trades"] += 1
        if pnl > 0:
            pair_bucket["wins"] += 1
        elif pnl < 0:
            pair_bucket["losses"] += 1

        equity_curve.append(
            {
                "trade_key": f"{open_trade['pair']}|{event_ts.isoformat()}|{reason}|{round(pnl, 4)}",
                "ts": event_ts.isoformat(),
                "label": local_exit.strftime("%d.%m %H:%M"),
                "value": round(equity, 4),
                "pair": open_trade["pair"],
                "quality": open_trade["quality"],
                "reason": reason,
                "pnl_eur": round(pnl, 4),
                "mae_r": round(mae_r, 4),
                "mfe_r": round(mfe_r, 4),
            }
        )
        pnl_curve.append(
            {
                "trade_key": f"{open_trade['pair']}|{event_ts.isoformat()}|{reason}|{round(pnl, 4)}",
                "ts": event_ts.isoformat(),
                "label": local_exit.strftime("%d.%m %H:%M"),
                "value": round(cumulative_pnl, 4),
                "pair": open_trade["pair"],
                "quality": open_trade["quality"],
                "reason": reason,
                "pnl_eur": round(pnl, 4),
                "mae_r": round(mae_r, 4),
                "mfe_r": round(mfe_r, 4),
            }
        )
        recent_trades.append(
            {
                "trade_key": f"{open_trade['pair']}|{event_ts.isoformat()}|{reason}|{round(pnl, 4)}",
                "pair": open_trade["pair"],
                "entry_ts": open_trade["entry_ts"].isoformat(),
                "exit_ts": event_ts.isoformat(),
                "pnl_eur": round(pnl, 4),
                "equity_after": round(equity, 4),
                "hold_minutes": round(hold_minutes, 2),
                "reason": reason,
                "quality": open_trade["quality"],
                "setup_type": open_trade["setup_type"],
                "regime_label": open_trade["regime_label"],
                "strategy_id": open_trade["strategy_id"],
                "score": round(open_trade["score"], 2),
                "reason_code": open_trade["reason_code"],
                "budget_eur": round(open_trade["budget_eur"], 2),
                "entry_price": round(open_trade["entry_fill_price"], 8),
                "exit_price": round(float(payload.get("price", 0.0)), 8),
                "entry_fee_eur": round(open_trade["entry_fee_eur"], 6),
                "entry_fee_rate": round(open_trade["entry_fee_rate"], 6),
                "exit_fee_eur": round(float(payload.get("exit_fee_eur", 0.0)), 6),
                "exit_fee_rate": round(float(payload.get("exit_fee_rate", 0.0)), 6),
                "total_fee_eur": round(total_fee_eur, 6),
                "entry_slippage_bps": round(open_trade["entry_slippage_bps"], 4),
                "exit_slippage_bps": round(float(payload.get("exit_slippage_bps", 0.0)), 4),
                "total_slippage_bps": round(total_slippage_bps, 4),
                "entry_liquidity_role": open_trade["entry_liquidity_role"],
                "exit_liquidity_role": str(payload.get("exit_liquidity_role", "n/a")),
                "entry_maker_probability": round(open_trade["entry_maker_probability"], 4),
                "exit_maker_probability": round(float(payload.get("exit_maker_probability", 0.0)), 4),
                "mae_r": round(mae_r, 4),
                "mfe_r": round(mfe_r, 4),
                "replay_points": list(payload.get("replay_points") or []),
            }
        )
        open_trade = None

    closed_trades = len(recent_trades)
    avg_hold_minutes = (hold_minutes_total / closed_trades) if closed_trades else 0.0
    avg_pnl_per_trade = (cumulative_pnl / closed_trades) if closed_trades else 0.0
    pairs = sorted({trade["pair"] for trade in recent_trades})
    qualities = sorted({trade["quality"] for trade in recent_trades})
    reasons = sorted({trade["reason"] for trade in recent_trades})
    setups = sorted({trade["setup_type"] for trade in recent_trades})
    return {
        "source_exists": True,
        "events_loaded": len(events),
        "equity_curve": equity_curve[-120:],
        "pnl_curve": pnl_curve[-120:],
        "daily_pnl_series": [
            {"label": bucket["label"], "value": round(bucket["value"], 4), "trades": bucket["trades"]}
            for _, bucket in sorted(daily_pnl.items())
        ],
        "exit_reason_breakdown": [
            {"label": label, "value": count}
            for label, count in exit_reason_counter.most_common()
        ],
        "pair_performance": [
            {
                "label": values["label"],
                "value": round(values["value"], 4),
                "trades": values["trades"],
                "wins": values["wins"],
                "losses": values["losses"],
            }
            for _, values in sorted(pair_performance.items(), key=lambda item: item[1]["value"], reverse=True)
        ],
        "quality_breakdown": [
            {"label": label, "value": count}
            for label, count in sorted(quality_counter.items())
        ],
        "mae_mfe_points": [
            {
                "trade_key": trade["trade_key"],
                "label": trade["pair"],
                "pair": trade["pair"],
                "mae_r": trade["mae_r"],
                "mfe_r": trade["mfe_r"],
                "pnl_eur": trade["pnl_eur"],
                "quality": trade["quality"],
                "reason": trade["reason"],
            }
            for trade in recent_trades[-250:]
        ],
        "recent_trades": recent_trades[-12:][::-1],
        "all_trades": recent_trades[-250:][::-1],
        "filter_options": {
            "pairs": pairs,
            "qualities": qualities,
            "reasons": reasons,
            "setups": setups,
            "limits": [12, 25, 50, 100, 250],
        },
        "summary": {
            "closed_trades": closed_trades,
            "net_pnl_eur": round(cumulative_pnl, 4),
            "ending_equity": round(equity, 4),
            "avg_pnl_per_trade_eur": round(avg_pnl_per_trade, 4),
            "avg_hold_minutes": round(avg_hold_minutes, 2),
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "best_trade_eur": round(best_trade or 0.0, 4),
            "worst_trade_eur": round(worst_trade or 0.0, 4),
            "avg_mae_r": round((mae_r_total / closed_trades) if closed_trades else 0.0, 4),
            "avg_mfe_r": round((mfe_r_total / closed_trades) if closed_trades else 0.0, 4),
            "avg_total_fee_eur": round((total_fee_eur_total / closed_trades) if closed_trades else 0.0, 4),
            "avg_total_slippage_bps": round((total_slippage_bps_total / closed_trades) if closed_trades else 0.0, 4),
        },
    }


def build_copilot_overview(
    *,
    history_status: dict[str, Any],
    launch: dict[str, Any],
    market: dict[str, Any],
    forward_report: dict[str, Any],
    trade_analytics: dict[str, Any],
    strategy_lab: dict[str, Any],
) -> dict[str, Any]:
    available_days = float(history_status.get("available_days") or 0.0)
    required_days = int(history_status.get("required_days") or 13)
    leader = market.get("leader_symbol") or "n/a"
    summary = trade_analytics.get("summary", {})
    strategy_rows = list(strategy_lab.get("strategies") or [])
    current_champion = str(strategy_lab.get("current_paper_strategy_id") or "n/a")
    promotion_reason = str(strategy_lab.get("promotion_reason") or "n/a")
    gate_entries = forward_report.get("gates") or {}
    gate_explanations = []
    for name, gate in sorted(gate_entries.items()):
        passed = bool(gate.get("passed"))
        gate_explanations.append(
            {
                "name": name,
                "status": "pass" if passed else "fail",
                "actual": gate.get("actual"),
                "threshold": gate.get("threshold"),
                "simple": (
                    f"{name} ist innerhalb des Zielbereichs."
                    if passed
                    else f"{name} liegt noch nicht im Zielbereich. Erst wenn der Wert die Schwelle {gate.get('threshold')} erreicht, geht der Bot in die naechste Launch-Stufe."
                ),
            }
        )
    warnings = []
    if not history_status.get("sufficient_history"):
        warnings.append(
            {
                "severity": "warn",
                "title": "OOS history incomplete",
                "detail": f"Es fehlen noch {max((required_days - available_days), 0.0):.2f} Tage echte lokale Historie.",
                "simple": "Der Bot darf noch nicht weiter in die naechste Teststufe, weil noch nicht genug Daten gesammelt wurden.",
            }
        )
    if not forward_report.get("go_live_ready"):
        warnings.append(
            {
                "severity": "warn",
                "title": "Live candidate not armed",
                "detail": "Die aktuellen Vorwaertsdaten sind noch kein gruener Live-Kandidat.",
                "simple": "Das ist normal. Erst muessen Paper-Daten und Sicherheitschecks stabil gut sein.",
            }
        )
    if summary.get("worst_trade_eur", 0.0) < 0:
        warnings.append(
            {
                "severity": "bad" if abs(summary.get("worst_trade_eur", 0.0)) > 1.0 else "warn",
                "title": "Trade loss pressure",
                "detail": f"Der schlechteste geloggte Trade liegt bei {summary.get('worst_trade_eur', 0.0):.2f} EUR.",
                "simple": "Schau dir im Journal an, welche Exit-Gruende und Qualitaeten aktuell die groessten Verluste erzeugen.",
            }
        )
    if not strategy_rows:
        warnings.append(
            {
                "severity": "warn",
                "title": "Strategy lab has no paper evidence",
                "detail": "Die parallelen Kandidatenstrategien haben noch nicht genug geschlossene Research-Trades geliefert.",
                "simple": "Noch wird nichts sinnvoll promotet. Der Bot sammelt erst Vergleichsdaten zwischen mehreren Strategien.",
            }
        )
    elif not any(bool(row.get("eligible_for_promotion")) for row in strategy_rows):
        warnings.append(
            {
                "severity": "warn",
                "title": "No challenger cleared the promotion gate",
                "detail": f"Aktueller Champion bleibt {current_champion}, weil noch keine Challenger-Strategie alle Mindest-Gates bestanden hat.",
                "simple": "Der Bot testet mehrere Strategien parallel, aber noch keine ist stark genug, um Champion zu werden.",
            }
        )
    plain_status = (
        f"Der Bot sammelt im Moment noch Daten. Lokal sind {available_days:.2f} von {required_days} Tagen OOS-Historie vorhanden. Aktueller Paper-Champion ist {current_champion}."
        if not history_status.get("sufficient_history")
        else f"Die OOS-Historie ist vollständig. Der nächste Fokus liegt auf Walk-Forward, Gate und Paper-Forward. Aktueller Paper-Champion ist {current_champion}."
    )
    operator_focus = launch.get("next_action") or "Weiter beobachten und auf den nächsten grünen Gate-Schritt warten."
    beginner_terms = [
        {
            "term": "OOS-Daten",
            "simple": "Out-of-sample Daten sind Kursdaten, die nicht zum Einstellen der Strategie benutzt wurden. So prüft man ehrlicher, ob der Bot auch auf unbekannten Daten funktioniert.",
        },
        {
            "term": "Release Gate",
            "simple": "Das Gate ist eine Sicherheitsprüfung. Erst wenn Kennzahlen, Tests und der Runtime-Zustand passen, darf der nächste Launch-Schritt starten.",
        },
        {
            "term": "Paper Forward",
            "simple": "Paper Forward bedeutet: der Bot läuft wie live, aber ohne echtes Geld. So sieht man, ob Signale, Risiko und Ausführung in Echtzeit stabil sind.",
        },
        {
            "term": "Equity",
            "simple": "Equity ist der aktuelle Kontowert. Die Equity-Kurve zeigt, wie sich der Bot-Kontostand über die Zeit entwickelt hat.",
        },
        {
            "term": "PnL",
            "simple": "PnL bedeutet Profit and Loss. Das ist der Gewinn oder Verlust aus Trades, entweder pro Trade oder aufsummiert.",
        },
        {
            "term": "Trade-Journal",
            "simple": "Das Journal ist die Liste der abgeschlossenen Trades. Dort sieht man, welches Pair, welcher Exit-Grund und welche Qualitaet den Gewinn oder Verlust verursacht haben.",
        },
        {
            "term": "Champion / Challenger",
            "simple": "Champion ist die aktuell beste Strategie. Challenger sind Kandidaten, die parallel im Demo-Modus getestet werden. Nur gute Challenger duerfen Champion werden.",
        },
    ]
    recommended_actions = [
        operator_focus,
        f"Beobachte im Marktband zuerst {leader}, weil dieses Pair aktuell die stärkste Kurzfristbewegung zeigt.",
        "Nutze die Journal-Tabelle, um zu sehen, welche Exit-Gründe und Qualitäten den größten Einfluss auf PnL und Drawdown haben.",
        f"Pruefe das Strategy Lab: aktueller Champion {current_champion}, Promotion-Grund {promotion_reason}.",
    ]
    if forward_report.get("go_live_ready"):
        beginner_summary = "Die aktuellen Vorwärtsdaten sind stark genug, um den Live-Kandidaten zu diskutieren. Trotzdem bleibt Paper-Überwachung vor echtem Kapital Pflicht."
    else:
        beginner_summary = "Der Bot ist noch nicht live-reif. Das ist normal: erst Datenbasis, dann OOS-Prüfung, dann Gate, dann Paper-Forward."
    return {
        "plain_status": plain_status,
        "operator_focus": operator_focus,
        "beginner_summary": beginner_summary,
        "recommended_actions": recommended_actions,
        "beginner_terms": beginner_terms,
        "warnings": warnings,
        "gate_explanations": gate_explanations,
        "journal_hint": (
            f"Bisher sind {summary.get('closed_trades', 0)} abgeschlossene Trades in der Telemetrie vorhanden. "
            "Die neuen Equity- und Journal-Charts helfen dir, die Wirkung jedes Trades einfacher nachzuvollziehen."
        ),
    }


def build_strategy_lab_overview(strategy_lab: dict[str, Any]) -> dict[str, Any]:
    strategies = list(strategy_lab.get("strategies") or [])
    eligible = [row for row in strategies if bool(row.get("eligible_for_promotion"))]
    ranked_scores = [
        {
            "label": row.get("label") or row.get("strategy_id") or "unknown",
            "value": round(float(row.get("score", 0.0)), 4),
            "strategy_id": row.get("strategy_id"),
        }
        for row in strategies
    ]
    return {
        "source_exists": bool(strategy_lab.get("source_exists")),
        "generated_at": strategy_lab.get("generated_at"),
        "current_paper_strategy_id": strategy_lab.get("current_paper_strategy_id"),
        "current_live_strategy_id": strategy_lab.get("current_live_strategy_id"),
        "recommended_paper_strategy_id": strategy_lab.get("recommended_paper_strategy_id"),
        "recommended_live_strategy_id": strategy_lab.get("recommended_live_strategy_id"),
        "paper_promotion_applied": bool(strategy_lab.get("paper_promotion_applied")),
        "live_promotion_applied": bool(strategy_lab.get("live_promotion_applied")),
        "promotion_reason": strategy_lab.get("promotion_reason") or "n/a",
        "previous_paper_strategy_id": strategy_lab.get("previous_paper_strategy_id"),
        "current_paper_promoted_at": strategy_lab.get("current_paper_promoted_at"),
        "paper_promotion_cooldown_until": strategy_lab.get("paper_promotion_cooldown_until"),
        "rollback_applied": bool(strategy_lab.get("rollback_applied")),
        "pinned_paper_strategy_id": strategy_lab.get("pinned_paper_strategy_id"),
        "strategies": strategies,
        "filter_options": {
            "strategy_ids": [str(row.get("strategy_id")) for row in strategies if row.get("strategy_id")],
            "families": sorted({str(row.get("family")) for row in strategies if row.get("family")}),
            "types": sorted({str(row.get("strategy_type")) for row in strategies if row.get("strategy_type")}),
        },
        "summary": {
            "strategy_count": len(strategies),
            "eligible_count": len(eligible),
            "regime_ready_count": sum(
                1
                for row in strategies
                if bool((row.get("gates") or {}).get("distinct_regimes", {}).get("passed"))
                and bool((row.get("gates") or {}).get("regime_trade_depth", {}).get("passed"))
                and bool((row.get("gates") or {}).get("regime_concentration", {}).get("passed"))
            ),
            "asset_ready_count": sum(
                1
                for row in strategies
                if bool((row.get("gates") or {}).get("distinct_assets", {}).get("passed"))
                and bool((row.get("gates") or {}).get("asset_trade_depth", {}).get("passed"))
                and bool((row.get("gates") or {}).get("asset_concentration", {}).get("passed"))
            ),
            "best_score": max((float(row.get("score", 0.0)) for row in strategies), default=0.0),
            "current_champion_label": next(
                (
                    str(row.get("label"))
                    for row in strategies
                    if str(row.get("strategy_id")) == str(strategy_lab.get("current_paper_strategy_id"))
                ),
                str(strategy_lab.get("current_paper_strategy_id") or "n/a"),
            ),
            "cooldown_until": strategy_lab.get("paper_promotion_cooldown_until"),
            "rollback_applied": bool(strategy_lab.get("rollback_applied")),
        },
        "ranked_scores": ranked_scores,
    }


def build_dashboard_overview(
    *,
    bot_config: BotConfig,
    data_dir: Path,
    logs_root: Path,
    state_path: Path | None = None,
    task_name: str = "FlowBotSupervisorWatchdog",
    recent_run_limit: int = 8,
) -> dict[str, Any]:
    resolved_state_path = state_path or find_latest_supervisor_state_path(logs_root)
    monitor = asdict(run_monitor_supervisor(resolved_state_path)) if resolved_state_path else {
        "state_path": "",
        "state_exists": False,
        "status": "missing",
        "stopped_reason": "no_supervisor_state_found",
        "updated_at": None,
        "state_age_seconds": None,
        "ready_for_paper_forward": None,
        "history_progress": None,
        "daily_summary": None,
        "daily_summary_json_path": None,
        "daily_summary_markdown_path": None,
        "dashboard_path": None,
        "strategy_lab": None,
        "supervisor": None,
        "paper_forward": None,
    }
    state_payload = load_supervisor_state_payload(resolved_state_path) if resolved_state_path and resolved_state_path.exists() else {}
    history_status = asdict(run_history_status(data_dir, bot_config, train_days=10, test_days=3))
    project_root = _infer_project_root(data_dir, logs_root)
    telemetry_path = _resolve_telemetry_path(project_root, bot_config.telemetry_path)
    strategy_lab_path = _resolve_telemetry_path(project_root, bot_config.strategy_lab_state_path)
    personal_journal_path = _resolve_telemetry_path(project_root, bot_config.personal_journal_path)
    recent_runs = list_recent_runs(logs_root, limit=recent_run_limit)
    last_cycle = summarize_last_cycle(state_payload)
    forward_report = asdict(run_forward_test_report(telemetry_path, bot_config))
    market = build_market_overview(bot_config, data_dir)
    trade_analytics = build_trade_analytics(bot_config, telemetry_path)
    signal_observatory = asdict(run_signal_observatory_report(telemetry_path))
    shadow_portfolios = asdict(run_shadow_portfolio_report(telemetry_path, bot_config))
    standalone_strategy_lab_payload = _load_json_payload(strategy_lab_path)
    strategy_lab_payload = (
        monitor.get("strategy_lab")
        if isinstance(monitor.get("strategy_lab"), dict)
        else state_payload.get("strategy_lab")
    )
    if isinstance(standalone_strategy_lab_payload, dict) and standalone_strategy_lab_payload:
        strategy_lab_payload = standalone_strategy_lab_payload
    elif not isinstance(strategy_lab_payload, dict) or not strategy_lab_payload:
        strategy_lab_payload = standalone_strategy_lab_payload
    strategy_lab = build_strategy_lab_overview(strategy_lab_payload if isinstance(strategy_lab_payload, dict) else {})
    personal_journal_payload = state_payload.get("personal_journal") if isinstance(state_payload, dict) else None
    if not isinstance(personal_journal_payload, dict) or not personal_journal_payload:
        personal_journal_payload = build_personal_journal_payload(run_personal_journal_report(personal_journal_path))
    fast_research_payload = state_payload.get("fast_research_lab") if isinstance(state_payload, dict) else None
    if not isinstance(fast_research_payload, dict) or not fast_research_payload:
        fast_research_payload = build_fast_research_lab_payload(strategy_lab_payload if isinstance(strategy_lab_payload, dict) else {}, telemetry_path)
    personal_journal = build_personal_journal_overview(personal_journal_payload)
    fast_research_lab = build_fast_research_lab_overview(
        fast_research_payload,
        strategy_lab=strategy_lab,
    )
    journal_strategy_alignment = build_journal_strategy_alignment_overview(
        personal_journal,
        strategy_lab,
        fast_research_lab,
    )
    launch = build_launch_overview(
        history_status=history_status,
        state_payload=state_payload,
        forward_report=forward_report,
    )
    analytics = build_analytics_overview(
        recent_runs=recent_runs,
        last_cycle=last_cycle,
        forward_report=forward_report,
        trade_analytics=trade_analytics,
    )
    copilot = build_copilot_overview(
        history_status=history_status,
        launch=launch,
        market=market,
        forward_report=forward_report,
        trade_analytics=trade_analytics,
        strategy_lab=strategy_lab,
    )
    return {
        "app": {
            "name": "Flow Bot Monitor",
            "tagline": "Read-only runtime cockpit for OOS capture, gate readiness, and paper-forward supervision.",
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "timezone": bot_config.timezone_name,
            "mode": "read_only",
        },
        "monitor": monitor,
        "history_status": history_status,
        "market": market,
        "launch": launch,
        "forward_report": forward_report,
        "analytics": analytics,
        "trade_analytics": trade_analytics,
        "signal_observatory": signal_observatory,
        "shadow_portfolios": shadow_portfolios,
        "strategy_lab": strategy_lab,
        "personal_journal": personal_journal,
        "fast_research_lab": fast_research_lab,
        "journal_strategy_alignment": journal_strategy_alignment,
        "copilot": copilot,
        "task": query_windows_task(task_name),
        "recent_runs": recent_runs,
        "last_cycle": last_cycle,
        "state_payload": state_payload,
    }


def serve_dashboard_app(
    *,
    bot_config: BotConfig,
    data_dir: Path,
    logs_root: Path,
    state_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    task_name: str = "FlowBotSupervisorWatchdog",
    open_browser: bool = False,
    idle_shutdown_seconds: int | None = None,
) -> tuple[ThreadingHTTPServer, str]:
    logs_root = logs_root.resolve()
    data_dir = data_dir.resolve()
    if state_path is not None:
        state_path = state_path.resolve()
    project_root = _infer_project_root(data_dir, logs_root)
    personal_journal_path = _resolve_telemetry_path(project_root, bot_config.personal_journal_path)

    activity = {"last_request": monotonic()}

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            activity["last_request"] = monotonic()
            if self.path in {"/", "/index.html"}:
                overview = build_dashboard_overview(
                    bot_config=bot_config,
                    data_dir=data_dir,
                    logs_root=logs_root,
                    state_path=state_path,
                    task_name=task_name,
                )
                self._send_html(render_dashboard_app_html(overview))
                return
            if self.path == "/api/overview":
                overview = build_dashboard_overview(
                    bot_config=bot_config,
                    data_dir=data_dir,
                    logs_root=logs_root,
                    state_path=state_path,
                    task_name=task_name,
                )
                self._send_json(overview)
                return
            if self.path == "/healthz":
                self._send_json({"ok": True, "ts": datetime.now(timezone.utc).isoformat()})
                return
            self.send_error(404, "Not Found")

        def do_POST(self) -> None:  # noqa: N802
            activity["last_request"] = monotonic()
            if self.path == "/api/personal-journal/append":
                try:
                    payload = self._read_json()
                    appended = self._append_personal_journal_entry(payload if isinstance(payload, dict) else {})
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)
                    return
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=500)
                    return
                self._send_json(appended, status=201)
                return
            self.send_error(404, "Not Found")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            encoded = json.dumps(payload, indent=2, default=_json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _read_json(self) -> dict[str, Any]:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
            if content_length <= 0:
                raise ValueError("request body is empty")
            raw = self.rfile.read(content_length).decode("utf-8")
            return json.loads(raw)

        def _append_personal_journal_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
            instrument = str(payload.get("instrument") or "").strip()
            strategy_name = str(payload.get("strategy_name") or "").strip()
            if not instrument:
                raise ValueError("instrument is required")
            if not strategy_name:
                raise ValueError("strategy_name is required")
            ensure_personal_journal_path(personal_journal_path)
            entry = build_personal_trade_entry(
                market=str(payload.get("market") or "crypto").strip(),
                instrument=instrument,
                venue=str(payload.get("venue") or "").strip(),
                side=str(payload.get("side") or "long").strip(),
                strategy_name=strategy_name,
                setup_family=str(payload.get("setup_family") or "manual").strip(),
                timeframe=str(payload.get("timeframe") or "").strip(),
                status=str(payload.get("status") or "closed").strip(),
                entry_ts=self._normalize_optional_timestamp(payload.get("entry_ts")),
                exit_ts=self._normalize_optional_timestamp(payload.get("exit_ts")),
                entry_price=self._coerce_optional_float(payload.get("entry_price")),
                exit_price=self._coerce_optional_float(payload.get("exit_price")),
                pnl_eur=float(payload.get("pnl_eur") or 0.0),
                pnl_pct=self._coerce_optional_float(payload.get("pnl_pct")),
                fees_eur=float(payload.get("fees_eur") or 0.0),
                size_notional_eur=self._coerce_optional_float(payload.get("size_notional_eur")),
                confidence_before=self._coerce_optional_int(payload.get("confidence_before")),
                confidence_after=self._coerce_optional_int(payload.get("confidence_after")),
                lesson=str(payload.get("lesson") or "").strip(),
                notes=str(payload.get("notes") or "").strip(),
                tags=payload.get("tags"),
                mistakes=payload.get("mistakes"),
            )
            appended = append_personal_trade(personal_journal_path, entry)
            summary = run_personal_journal_report(personal_journal_path)
            journal_payload = build_personal_journal_payload(summary)
            return {
                "ok": True,
                "path": str(personal_journal_path),
                "entry": appended,
                "personal_journal": build_personal_journal_overview(journal_payload),
            }

        @staticmethod
        def _coerce_optional_float(value: Any) -> float | None:
            if value in (None, "", "null"):
                return None
            return float(value)

        @staticmethod
        def _coerce_optional_int(value: Any) -> int | None:
            if value in (None, "", "null"):
                return None
            return int(value)

        @staticmethod
        def _normalize_optional_timestamp(value: Any) -> str | None:
            raw = str(value or "").strip()
            if not raw:
                return None
            normalized = raw.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise ValueError(f"invalid timestamp: {raw}") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=bot_config.timezone)
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    actual_host = host if host not in {"0.0.0.0", "::"} else "127.0.0.1"
    url = f"http://{actual_host}:{server.server_port}/"

    if idle_shutdown_seconds and idle_shutdown_seconds > 0:
        def idle_watcher() -> None:
            while True:
                sleep(min(5, max(1, idle_shutdown_seconds)))
                if monotonic() - activity["last_request"] >= idle_shutdown_seconds:
                    server.shutdown()
                    return

        watcher = threading.Thread(target=idle_watcher, daemon=True)
        watcher.start()

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    return server, url


def list_recent_runs(logs_root: Path, limit: int = 8) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for run_dir in sorted(logs_root.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True):
        if not run_dir.is_dir():
            continue
        if not (run_dir.name.startswith("supervisor_watchdog_") or run_dir.name.startswith("paper_forward_supervisor_")):
            continue
        state_path = run_dir / "supervisor_state.json"
        payload: dict[str, Any] = {}
        if state_path.exists():
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
        progress = payload.get("history_progress") or {}
        summary = payload.get("daily_summary") or {}
        runs.append(
            {
                "name": run_dir.name,
                "kind": "watchdog" if run_dir.name.startswith("supervisor_watchdog_") else "supervisor",
                "updated_at": payload.get("updated_at"),
                "status": payload.get("status", "unknown"),
                "available_days": progress.get("available_days"),
                "progress_pct": progress.get("progress_pct"),
                "gate_status": summary.get("gate_status"),
                "paper_forward_status": summary.get("paper_forward_status"),
                "path": str(run_dir),
                "state_path": str(state_path) if state_path.exists() else "",
            }
        )
        if len(runs) >= limit:
            break
    return runs


def summarize_last_cycle(state_payload: dict[str, Any]) -> dict[str, Any]:
    last_prepare = state_payload.get("last_prepare_report") or {}
    capture_report = last_prepare.get("capture_report") or {}
    cycle_reports = capture_report.get("cycle_reports") or []
    if not cycle_reports:
        return {
            "available": False,
            "cycle": None,
            "errors": [],
            "interval_totals": {},
            "pair_deltas": [],
        }
    last_cycle = cycle_reports[-1]
    sync_results = last_cycle.get("sync_result") or []
    interval_totals: dict[str, dict[str, int]] = {}
    pair_deltas: list[dict[str, Any]] = []
    for sync_entry in sync_results:
        intervals = sync_entry.get("intervals") or {}
        for interval_name, interval_payload in intervals.items():
            written_rows = 0
            merged_rows = 0
            fetched_rows = 0
            for pair, pair_payload in interval_payload.items():
                written_rows += int(pair_payload.get("written_rows", 0))
                merged_rows += int(pair_payload.get("merged_rows", 0))
                fetched_rows += int(pair_payload.get("fetched_rows", 0))
                pair_deltas.append(
                    {
                        "interval": interval_name,
                        "pair": pair,
                        "status": pair_payload.get("status"),
                        "existing_rows": pair_payload.get("existing_rows"),
                        "fetched_rows": pair_payload.get("fetched_rows"),
                        "merged_rows": pair_payload.get("merged_rows"),
                        "written_rows": pair_payload.get("written_rows"),
                        "last": pair_payload.get("last"),
                    }
                )
            interval_totals[interval_name] = {
                "written_rows": written_rows,
                "merged_rows": merged_rows,
                "fetched_rows": fetched_rows,
            }
    return {
        "available": True,
        "cycle": last_cycle.get("cycle"),
        "errors": [last_cycle.get("error")] if last_cycle.get("error") else [],
        "interval_totals": interval_totals,
        "pair_deltas": pair_deltas,
    }


def query_windows_task(task_name: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"],
        capture_output=True,
        check=False,
    )
    stdout_text = _decode_windows_output(completed.stdout)
    stderr_text = _decode_windows_output(completed.stderr)
    if completed.returncode != 0:
        return {
            "task_name": task_name,
            "exists": False,
            "status": "missing",
            "error": (stderr_text or stdout_text).strip(),
            "details": {},
        }

    details: dict[str, str] = {}
    normalized: dict[str, str] = {}
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        clean_key = key.strip()
        details[clean_key] = value.strip()
        normalized[_normalize_key(clean_key)] = value.strip()
    return {
        "task_name": task_name,
        "exists": True,
        "status": _first_normalized_value(normalized, "status", "status der geplanten aufgabe", "state") or "unknown",
        "last_run": _first_normalized_value(normalized, "letzte laufzeit", "last run time"),
        "last_result": _first_normalized_value(normalized, "letztes ergebnis", "last result"),
        "run_as_user": _first_normalized_value(normalized, "als benutzer ausfuhren", "run as user"),
        "details": details,
    }


def render_dashboard_app_html(overview: dict[str, Any]) -> str:
    initial_json = json.dumps(overview, default=_json_default)
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Flow Bot Monitor</title>
  <style>
    :root {{
      --bg: #090d12;
      --panel: rgba(15, 20, 27, 0.88);
      --panel-soft: rgba(11, 16, 22, 0.92);
      --line: rgba(255, 255, 255, 0.08);
      --text: #f4f6f8;
      --muted: #93a1ae;
      --amber: #f4b24f;
      --green: #38d39f;
      --red: #ff6d6d;
      --good-bg: rgba(56, 211, 159, 0.14);
      --warn-bg: rgba(244, 178, 79, 0.14);
      --bad-bg: rgba(255, 109, 109, 0.14);
      --shadow: 0 22px 70px rgba(0, 0, 0, 0.34);
      --radius: 24px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(circle at top right, rgba(244,178,79,0.16) 0%, rgba(244,178,79,0) 30%),
        radial-gradient(circle at bottom left, rgba(56,211,159,0.14) 0%, rgba(56,211,159,0) 28%),
        linear-gradient(180deg, #0b1117 0%, #070b10 100%);
      font-family: Bahnschrift, "Segoe UI Variable", Aptos, sans-serif;
      min-height: 100vh;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      background-image:
        linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px);
      background-size: 24px 24px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.9), rgba(0,0,0,0.2));
      pointer-events: none;
    }}
    .shell {{ width: min(1400px, calc(100vw - 28px)); margin: 18px auto 28px; display: grid; gap: 18px; position: relative; z-index: 1; }}
    .hero, .panel {{ border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); box-shadow: var(--shadow); }}
    .hero {{ padding: 24px; background: linear-gradient(135deg, rgba(11,16,22,0.98), rgba(16,23,31,0.86)); display: grid; gap: 18px; }}
    .hero-top {{ display: flex; justify-content: space-between; gap: 18px; flex-wrap: wrap; }}
    .eyebrow {{ color: var(--amber); font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; }}
    h1 {{ margin: 10px 0 8px; font-size: clamp(34px, 5vw, 64px); line-height: 0.95; letter-spacing: -0.05em; }}
    p {{ margin: 0; max-width: 76ch; color: var(--muted); line-height: 1.55; }}
    .status-pills {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    .pill {{ display: inline-flex; align-items: center; padding: 9px 14px; border-radius: 999px; border: 1px solid var(--line); font-size: 12px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; background: rgba(255,255,255,0.03); }}
    .pill.good {{ color: var(--green); background: var(--good-bg); }}
    .pill.warn {{ color: var(--amber); background: var(--warn-bg); }}
    .pill.bad {{ color: var(--red); background: var(--bad-bg); }}
    .hero-grid {{ display: grid; grid-template-columns: 340px minmax(0, 1fr); gap: 18px; }}
    .readiness {{ border: 1px solid var(--line); border-radius: var(--radius); padding: 22px; background: linear-gradient(180deg, rgba(8,12,17,0.94), rgba(11,16,23,0.82)); display: grid; gap: 18px; align-content: start; }}
    .ring {{ width: 220px; height: 220px; margin: 4px auto 0; border-radius: 50%; display: grid; place-items: center; box-shadow: inset 0 0 28px rgba(255,255,255,0.04), 0 0 36px rgba(0,0,0,0.26); }}
    .ring-inner {{ text-align: center; }}
    .ring-inner .value {{ font-size: 42px; font-weight: 800; letter-spacing: -0.06em; }}
    .ring-inner .label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); }}
    .stack, .list {{ display: grid; gap: 10px; }}
    .stack-item, .list-item {{ padding: 12px 14px; border-radius: 18px; border: 1px solid var(--line); background: rgba(255,255,255,0.03); }}
    .stack-item .label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 8px; }}
    .stack-item .big {{ font-size: 24px; font-weight: 800; letter-spacing: -0.04em; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }}
    .metric-card {{ padding: 18px; min-height: 118px; display: grid; gap: 10px; align-content: start; }}
    .metric-card .title {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); }}
    .metric-card .value {{ font-size: 30px; font-weight: 800; letter-spacing: -0.05em; }}
    .metric-card .meta {{ color: var(--muted); font-size: 13px; line-height: 1.4; }}
    .layout {{ display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 18px; }}
    .panel {{ padding: 18px; }}
    .panel-header {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 10px; }}
    .panel h2 {{ margin: 0; font-size: 16px; letter-spacing: 0.01em; }}
    .panel-subtitle {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 10px 8px; text-align: left; border-bottom: 1px solid var(--line); font-size: 13px; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; }}
    .path {{ font-family: "Cascadia Code", Consolas, monospace; color: #cad7e2; word-break: break-all; font-size: 12px; }}
    .footer {{ color: var(--muted); font-size: 12px; text-align: right; padding: 2px 4px 0; }}
    .layout-equal {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }}
    .market-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }}
    .market-card {{ padding: 18px; border-radius: 20px; border: 1px solid var(--line); background: linear-gradient(180deg, rgba(10,14,19,0.96), rgba(12,17,24,0.78)); display: grid; gap: 12px; min-height: 228px; }}
    .market-card.clickable {{ cursor: pointer; transition: transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease; }}
    .market-card.clickable:hover {{ transform: translateY(-2px); border-color: rgba(98,184,255,0.45); box-shadow: 0 18px 40px rgba(0,0,0,0.24); }}
    .market-card.selected {{ border-color: rgba(98,184,255,0.65); box-shadow: 0 0 0 1px rgba(98,184,255,0.18), 0 18px 40px rgba(0,0,0,0.22); }}
    .market-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
    .market-symbol {{ font-size: 24px; font-weight: 800; letter-spacing: -0.04em; }}
    .market-price {{ font-size: 32px; font-weight: 800; letter-spacing: -0.06em; }}
    .market-mini-grid, .forward-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
    .mini-metric {{ padding: 10px 12px; border-radius: 14px; border: 1px solid rgba(255,255,255,0.05); background: rgba(255,255,255,0.025); display: grid; gap: 4px; }}
    .mini-metric .label {{ font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); }}
    .mini-metric .value {{ font-size: 16px; font-weight: 700; }}
    .positive {{ color: var(--green); }}
    .negative {{ color: var(--red); }}
    .neutral {{ color: #cad7e2; }}
    .chip {{ display: inline-flex; align-items: center; padding: 8px 12px; border-radius: 999px; border: 1px solid var(--line); font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; background: rgba(255,255,255,0.03); }}
    .chip.good {{ color: var(--green); background: var(--good-bg); }}
    .chip.warn {{ color: var(--amber); background: var(--warn-bg); }}
    .chip.bad {{ color: var(--red); background: var(--bad-bg); }}
    .chip.blue {{ color: #8cc9ff; background: rgba(98,184,255,0.14); }}
    .window-strip {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .window-chip {{ display: inline-flex; align-items: center; gap: 6px; padding: 7px 10px; border-radius: 999px; border: 1px solid var(--line); background: rgba(255,255,255,0.03); font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }}
    .window-chip.good {{ color: var(--green); background: var(--good-bg); }}
    .window-chip.warn {{ color: var(--amber); background: var(--warn-bg); }}
    .window-chip.bad {{ color: var(--red); background: var(--bad-bg); }}
    .window-chip.neutral {{ color: #c8d6e3; }}
    .market-explorer-shell {{ display: grid; gap: 14px; }}
    .market-explorer-grid {{ display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.85fr); gap: 14px; align-items: start; }}
    .market-explorer-main {{ display: grid; gap: 14px; }}
    .market-explorer-side {{ display: grid; gap: 14px; }}
    .market-explorer-summary {{ display: grid; gap: 12px; }}
    .market-explorer-header {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; flex-wrap: wrap; }}
    .market-explorer-title {{ font-size: 28px; font-weight: 800; letter-spacing: -0.05em; }}
    .market-explorer-subtitle {{ color: var(--muted); font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase; }}
    .timeframe-strip {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .timeframe-btn {{ appearance: none; border: 1px solid var(--line); background: rgba(255,255,255,0.03); color: var(--muted); border-radius: 999px; padding: 9px 12px; font: inherit; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; cursor: pointer; transition: transform 140ms ease, background 140ms ease, color 140ms ease, border-color 140ms ease; }}
    .timeframe-btn.active {{ color: #081018; background: linear-gradient(135deg, #78f2bf, #62b8ff); border-color: transparent; }}
    .timeframe-btn:hover {{ transform: translateY(-1px); }}
    .market-table-scroll {{ overflow-x: auto; }}
    .market-horizon-table {{ width: 100%; border-collapse: collapse; }}
    .market-horizon-table td, .market-horizon-table th {{ padding: 10px 8px; border-bottom: 1px solid var(--line); font-size: 13px; white-space: nowrap; }}
    .market-horizon-table th {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; }}
    .market-horizon-table tr.clickable {{ cursor: pointer; transition: background 140ms ease, transform 140ms ease; }}
    .market-horizon-table tr.clickable:hover {{ background: rgba(255,255,255,0.03); }}
    .market-horizon-table tr.selected {{ background: rgba(98,184,255,0.08); }}
    .market-horizon-table tr.selected td {{ color: #eff7ff; }}
    .market-stat-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
    .market-stat-grid .mini-metric {{ min-height: 72px; }}
    .inspector-chart {{ border: 1px solid var(--line); border-radius: 18px; background: linear-gradient(180deg, rgba(8,12,18,0.96), rgba(12,17,23,0.74)); padding: 14px; min-height: 260px; }}
    .inspector-chart .chart-caption {{ margin-bottom: 10px; }}
    .sparkline {{ width: 100%; height: 70px; border-radius: 14px; overflow: hidden; border: 1px solid rgba(255,255,255,0.04); background: rgba(255,255,255,0.02); }}
    .sparkline svg, .chart svg {{ width: 100%; height: 100%; display: block; }}
    .launch-shell {{ display: grid; gap: 14px; }}
    .launch-summary {{ display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap; align-items: flex-start; }}
    .tiny-label {{ color: var(--amber); font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; }}
    .launch-headline {{ font-size: 28px; font-weight: 800; letter-spacing: -0.04em; }}
    .phase-track {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }}
    .phase-card {{ padding: 16px; border-radius: 20px; border: 1px solid var(--line); background: linear-gradient(180deg, rgba(10,14,20,0.96), rgba(12,17,23,0.82)); display: grid; gap: 10px; min-height: 168px; }}
    .phase-title {{ font-size: 16px; font-weight: 700; }}
    .phase-headline {{ font-size: 18px; font-weight: 700; letter-spacing: -0.03em; }}
    .phase-detail {{ color: var(--muted); font-size: 13px; line-height: 1.45; }}
    .phase-progress {{ height: 6px; border-radius: 999px; background: rgba(255,255,255,0.08); overflow: hidden; }}
    .phase-progress span {{ display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--amber), var(--green)); }}
    .chart-shell {{ display: grid; gap: 14px; }}
    .chart {{ border: 1px solid var(--line); border-radius: 18px; background: linear-gradient(180deg, rgba(8,12,18,0.96), rgba(12,17,23,0.74)); padding: 14px; min-height: 220px; }}
    .chart-caption {{ display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 12px; margin-bottom: 12px; }}
    .chart-legend {{ display: flex; gap: 12px; flex-wrap: wrap; color: var(--muted); font-size: 12px; }}
    .legend-key {{ display: inline-flex; align-items: center; gap: 6px; }}
    .legend-key::before {{ content: ""; width: 10px; height: 10px; border-radius: 50%; background: currentColor; opacity: 0.85; }}
    .gate-list {{ display: grid; gap: 10px; }}
    .gate-item {{ display: grid; grid-template-columns: 1fr auto; gap: 10px; padding: 12px 14px; border-radius: 16px; border: 1px solid var(--line); background: rgba(255,255,255,0.025); align-items: center; }}
    .portfolio-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }}
    .comparison-table {{ width: 100%; border-collapse: collapse; }}
    .comparison-table td, .comparison-table th {{ padding: 10px 8px; border-bottom: 1px solid var(--line); font-size: 13px; vertical-align: top; }}
    .filter-row {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }}
    .filter-control {{ display: grid; gap: 6px; min-width: 140px; }}
    .filter-control label {{ font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); }}
    .filter-control select, .filter-control input, .filter-control textarea {{ appearance: none; border: 1px solid var(--line); background: rgba(255,255,255,0.04); color: var(--text); border-radius: 12px; padding: 10px 12px; font: inherit; }}
    .filter-control textarea {{ min-height: 88px; resize: vertical; }}
    .journal-form-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
    .journal-form-actions {{ display: flex; gap: 10px; align-items: center; justify-content: space-between; flex-wrap: wrap; margin-top: 12px; }}
    .journal-form-status {{ color: var(--muted); font-size: 12px; }}
    .journal-form-status.good {{ color: var(--green); }}
    .journal-form-status.bad {{ color: var(--red); }}
    .market-sidebar-toolbar {{ display: grid; gap: 10px; margin-bottom: 12px; }}
    .market-search-input {{ width: 100%; border: 1px solid var(--line); background: rgba(255,255,255,0.04); color: var(--text); border-radius: 12px; padding: 11px 14px; font: inherit; }}
    .quick-filter-bar {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .quick-filter-btn {{ appearance: none; border: 1px solid var(--line); background: rgba(255,255,255,0.03); color: var(--muted); border-radius: 999px; padding: 8px 12px; font: inherit; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; cursor: pointer; transition: transform 140ms ease, border-color 140ms ease, color 140ms ease, background 140ms ease; }}
    .quick-filter-btn:hover {{ transform: translateY(-1px); color: var(--text); border-color: rgba(255,255,255,0.12); }}
    .quick-filter-btn.active {{ color: var(--text); background: rgba(98,184,255,0.14); border-color: rgba(98,184,255,0.24); }}
    .asset-sidebar-list {{ display: grid; gap: 10px; max-height: 620px; overflow: auto; padding-right: 4px; }}
    .asset-sidebar-item {{ display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: start; padding: 12px 14px; border-radius: 16px; border: 1px solid var(--line); background: linear-gradient(180deg, rgba(10,14,20,0.94), rgba(12,17,24,0.82)); cursor: pointer; transition: transform 140ms ease, border-color 140ms ease, background 140ms ease; }}
    .asset-sidebar-item:hover {{ transform: translateY(-1px); border-color: rgba(255,255,255,0.12); }}
    .asset-sidebar-item.selected {{ border-color: rgba(98,184,255,0.35); background: linear-gradient(180deg, rgba(19,29,43,0.98), rgba(11,17,24,0.88)); }}
    .asset-sidebar-main {{ display: grid; gap: 8px; min-width: 0; }}
    .asset-sidebar-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }}
    .asset-sidebar-symbol {{ font-size: 16px; font-weight: 800; letter-spacing: -0.02em; }}
    .asset-sidebar-price {{ font-size: 15px; font-weight: 700; }}
    .asset-sidebar-meta {{ display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 12px; }}
    .asset-sidebar-badges {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .asset-badge {{ display: inline-flex; align-items: center; gap: 6px; border-radius: 999px; padding: 4px 9px; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; border: 1px solid var(--line); background: rgba(255,255,255,0.03); color: var(--muted); }}
    .asset-badge.good {{ color: var(--green); border-color: rgba(56,211,159,0.24); background: rgba(56,211,159,0.08); }}
    .asset-badge.bad {{ color: var(--red); border-color: rgba(255,109,109,0.24); background: rgba(255,109,109,0.08); }}
    .asset-badge.warn {{ color: var(--amber); border-color: rgba(244,178,79,0.26); background: rgba(244,178,79,0.10); }}
    .asset-favorite-btn {{ appearance: none; border: 1px solid var(--line); background: rgba(255,255,255,0.03); color: var(--muted); border-radius: 999px; width: 36px; height: 36px; cursor: pointer; font-size: 18px; transition: transform 140ms ease, border-color 140ms ease, color 140ms ease, background 140ms ease; }}
    .asset-favorite-btn:hover {{ transform: scale(1.05); border-color: rgba(255,255,255,0.12); color: var(--text); }}
    .asset-favorite-btn.active {{ color: var(--amber); border-color: rgba(244,178,79,0.26); background: rgba(244,178,79,0.10); }}
    .chart-toolbar {{ display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; padding: 12px 14px; border-radius: 18px; border: 1px solid var(--line); background: linear-gradient(180deg, rgba(8,12,18,0.96), rgba(12,17,23,0.74)); }}
    .toolbar-group {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .toolbar-label {{ font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); }}
    .segmented {{ display: inline-flex; gap: 6px; padding: 4px; border-radius: 999px; border: 1px solid var(--line); background: rgba(255,255,255,0.03); }}
    .seg-btn, .action-btn {{ appearance: none; border: 1px solid transparent; background: transparent; color: var(--muted); border-radius: 999px; padding: 9px 14px; font: inherit; font-size: 12px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; cursor: pointer; transition: transform 140ms ease, background 140ms ease, color 140ms ease, border-color 140ms ease; }}
    .seg-btn:hover, .action-btn:hover {{ transform: translateY(-1px); color: var(--text); border-color: rgba(255,255,255,0.08); }}
    .seg-btn.active {{ color: var(--text); background: rgba(98,184,255,0.16); border-color: rgba(98,184,255,0.28); }}
    .action-btn {{ background: rgba(255,255,255,0.04); border-color: var(--line); }}
    .action-btn.active {{ color: var(--amber); border-color: rgba(244,178,79,0.28); background: rgba(244,178,79,0.10); }}
    .action-btn.export {{ color: #8cc9ff; }}
    .action-btn.clear {{ color: var(--amber); }}
    .journal-kpis {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }}
    .trade-list-item.selected {{ border-color: rgba(98,184,255,0.4); background: rgba(98,184,255,0.10); }}
    .trade-list-item {{ cursor: pointer; transition: background 140ms ease, border-color 140ms ease, transform 140ms ease; }}
    .trade-list-item:hover {{ transform: translateY(-1px); border-color: rgba(255,255,255,0.12); }}
    .inspector-shell {{ display: grid; gap: 12px; margin-top: 12px; padding: 14px; border-radius: 18px; border: 1px solid var(--line); background: linear-gradient(180deg, rgba(10,14,20,0.96), rgba(12,17,24,0.82)); }}
    .inspector-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; flex-wrap: wrap; }}
    .inspector-title {{ font-size: 18px; font-weight: 800; letter-spacing: -0.03em; }}
    .detail-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
    .chart-hint {{ color: var(--muted); font-size: 12px; margin-top: 8px; }}
    .marker-hit {{ cursor: pointer; transition: transform 120ms ease; }}
    .marker-hit:hover {{ transform: scale(1.08); }}
    .highlight-note {{ color: #8cc9ff; font-size: 12px; }}
    .inline-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 8px; background: var(--amber); box-shadow: 0 0 0 0 rgba(244,178,79,0.45); animation: pulse 2s infinite; }}
    .inline-dot.good {{ background: var(--green); box-shadow: 0 0 0 0 rgba(56,211,159,0.45); }}
    .inline-dot.bad {{ background: var(--red); box-shadow: 0 0 0 0 rgba(255,109,109,0.45); }}
    @keyframes pulse {{ 0% {{ box-shadow: 0 0 0 0 rgba(244,178,79,0.42); }} 70% {{ box-shadow: 0 0 0 12px rgba(244,178,79,0); }} 100% {{ box-shadow: 0 0 0 0 rgba(244,178,79,0); }} }}
    @media (max-width: 1240px) {{ .journal-kpis, .detail-grid, .portfolio-grid {{ grid-template-columns: 1fr 1fr; }} }}
    @media (max-width: 1240px) {{ .market-grid, .phase-track, .layout-equal {{ grid-template-columns: 1fr 1fr; }} .hero-grid, .layout {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 1080px) {{ .hero-grid, .layout, .layout-equal {{ grid-template-columns: 1fr; }} .metric-grid, .market-grid, .phase-track, .journal-kpis, .detail-grid, .portfolio-grid {{ grid-template-columns: 1fr; }} .ring {{ width: 190px; height: 190px; }} .chart-toolbar {{ align-items: stretch; }} }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="hero-top">
        <div>
          <div class="eyebrow">Flow Bot Monitor | Bybit density x Trade Republic clarity</div>
          <h1>Read-Only Trading Cockpit</h1>
          <p id="hero-tagline"></p>
        </div>
        <div class="status-pills" id="status-pills"></div>
      </div>
      <div class="hero-grid">
        <section class="readiness">
          <div class="panel-subtitle">OOS Readiness</div>
          <div class="ring" id="readiness-ring"><div class="ring-inner"><div class="value" id="readiness-value">0%</div><div class="label">Captured</div></div></div>
          <div class="stack">
            <div class="stack-item"><div class="label">Available vs Required</div><div class="big" id="available-days">0 / 13 days</div></div>
            <div class="stack-item"><div class="label">Estimated Ready Time</div><div class="big" id="eta-value">n/a</div></div>
            <div class="stack-item"><div class="label">Last Refresh</div><div class="big" id="refreshed-at">n/a</div></div>
          </div>
        </section>
        <section class="metric-grid">
          <article class="panel metric-card"><div class="title">Supervisor</div><div class="value" id="supervisor-status">n/a</div><div class="meta" id="supervisor-meta"></div></article>
          <article class="panel metric-card"><div class="title">Gate</div><div class="value" id="gate-status">n/a</div><div class="meta" id="gate-meta"></div></article>
          <article class="panel metric-card"><div class="title">Paper Forward</div><div class="value" id="paper-status">n/a</div><div class="meta" id="paper-meta"></div></article>
          <article class="panel metric-card"><div class="title">Task Scheduler</div><div class="value" id="task-status">n/a</div><div class="meta" id="task-meta"></div></article>
          <article class="panel metric-card"><div class="title">Collection Speed</div><div class="value" id="speed-value">n/a</div><div class="meta" id="speed-meta"></div></article>
          <article class="panel metric-card"><div class="title">Latest Cycle</div><div class="value" id="cycle-value">n/a</div><div class="meta" id="cycle-meta"></div></article>
        </section>
      </div>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div><h2>Live Market Strip</h2><div class="panel-subtitle">Kraken live prices, 1h and 24h moves, local 1m sparklines</div></div>
        <div class="chart-legend" id="market-summary"></div>
      </div>
      <div class="market-grid" id="market-grid"></div>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div><h2>Market Explorer</h2><div class="panel-subtitle">Interactively inspect each asset across 1H through MAX, with live snapshots and horizon breadth</div></div>
      </div>
      <div class="market-explorer-shell">
        <div class="market-explorer-header">
          <div>
            <div class="market-explorer-subtitle">Selected Asset</div>
            <div class="market-explorer-title" id="market-explorer-title">n/a</div>
          </div>
          <div class="filter-row">
            <div class="filter-control"><label for="market-asset-select">Asset</label><select id="market-asset-select"></select></div>
            <div class="filter-control"><label>Timeframe</label><div class="timeframe-strip" id="market-timeframe-strip"></div></div>
          </div>
        </div>
        <div class="market-explorer-grid">
          <div class="market-explorer-main">
            <div class="market-stat-grid" id="market-explorer-summary-grid"></div>
            <div class="inspector-chart"><div class="chart-caption"><span>Selected timeframe chart</span><span id="market-explorer-chart-meta">n/a</span></div><div id="market-explorer-chart"></div></div>
            <div class="market-table-scroll">
              <table class="market-horizon-table">
                <thead><tr><th>Pair</th><th>Price</th><th>Change</th><th>Range</th><th>Coverage</th><th>Volume</th></tr></thead>
                <tbody id="market-explorer-table"></tbody>
              </table>
            </div>
          </div>
          <div class="market-explorer-side">
            <section class="panel" style="padding:16px;">
              <div class="panel-header">
                <div><h2>Asset Navigator</h2><div class="panel-subtitle">Search, favorites and quick filters for the live asset universe</div></div>
                <button type="button" class="action-btn" id="market-favorites-toggle">Favorites only: off</button>
              </div>
              <div class="market-sidebar-toolbar">
                <input id="market-search-input" class="market-search-input" type="search" placeholder="Search symbol, e.g. XRP or SOL" autocomplete="off" />
                <div class="quick-filter-bar" id="market-quick-filter-bar"></div>
                <div class="panel-subtitle" id="market-sidebar-meta">n/a</div>
              </div>
              <div class="asset-sidebar-list" id="market-sidebar-list"></div>
            </section>
            <section class="panel" style="padding:16px;">
              <div class="panel-header"><div><h2>Market Breadth</h2><div class="panel-subtitle">Average horizon change and breadth split across all pairs</div></div></div>
              <div class="chart-shell">
                <div class="chart"><div class="chart-caption"><span>Timeframe breadth</span><span id="market-breadth-meta">n/a</span></div><div id="market-breadth-chart"></div></div>
                <div class="chart"><div class="chart-caption"><span>Window overview</span><span id="market-window-meta">n/a</span></div><table class="comparison-table"><thead><tr><th>Window</th><th>Best</th><th>Worst</th><th>Avg Change</th><th>Coverage</th></tr></thead><tbody id="market-window-body"></tbody></table></div>
              </div>
            </section>
          </div>
        </div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div><h2>Launch Phase Lane</h2><div class="panel-subtitle">Capture, OOS optimization, release gate, paper-forward, live candidate</div></div>
      </div>
      <div class="launch-shell">
        <div class="launch-summary">
          <div><div class="tiny-label">Current Phase</div><div class="launch-headline" id="launch-current-phase">n/a</div></div>
          <div><div class="tiny-label">Next Action</div><p id="launch-next-action"></p></div>
        </div>
        <div class="phase-track" id="launch-track"></div>
      </div>
    </section>
    <section class="layout-equal">
      <section class="panel">
        <div class="panel-header">
          <div><h2>Readiness Trend</h2><div class="panel-subtitle">Progress over recent supervisor and watchdog runs</div></div>
        </div>
        <div class="chart-shell">
          <div class="chart"><div class="chart-caption"><span>OOS capture progress</span><span id="progress-chart-meta">n/a</span></div><div id="progress-chart"></div></div>
          <div class="chart"><div class="chart-caption"><span>Latest sync throughput</span><span id="sync-chart-meta">n/a</span></div><div id="sync-chart"></div></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-header">
          <div><h2>Launch Readiness</h2><div class="panel-subtitle">Forward metrics, gate state and launch candidate health</div></div>
        </div>
        <div class="forward-grid" id="forward-summary-grid"></div>
        <div style="height: 12px"></div>
        <div class="gate-list" id="forward-gate-list"></div>
      </section>
    </section>
    <section class="layout-equal">
      <section class="panel">
        <div class="panel-header">
          <div><h2>Signal Observatory</h2><div class="panel-subtitle">Alle beobachteten Signale, Ablehnungsgruende und tradable Setups im aktuellen Forschungsfenster</div></div>
        </div>
        <div class="forward-grid" id="signal-summary-grid"></div>
        <div style="height:12px"></div>
        <div class="chart-shell">
          <div class="chart"><div class="chart-caption"><span>Observed signal funnel</span><span id="signal-funnel-meta">n/a</span></div><div id="signal-pair-chart"></div></div>
          <div class="chart"><div class="chart-caption"><span>Rejections and regimes</span><span id="signal-rejection-meta">n/a</span></div><div id="signal-rejection-chart"></div><div style="height:12px"></div><div id="signal-regime-chart"></div></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-header">
          <div><h2>Shadow Portfolios</h2><div class="panel-subtitle">Virtuelle Portfolio-Groessen, die dieselben Signale parallel als Test-Lanes handeln</div></div>
        </div>
        <div class="filter-row">
          <div class="filter-control"><label for="shadow-filter-portfolio">Shadow Portfolio</label><select id="shadow-filter-portfolio"></select></div>
          <div class="filter-control"><label for="shadow-filter-behavior">Behavior</label><select id="shadow-filter-behavior"></select></div>
          <div class="filter-control"><label for="shadow-filter-regime">Regime</label><select id="shadow-filter-regime"></select></div>
        </div>
        <div class="portfolio-grid" id="shadow-portfolio-grid"></div>
        <div style="height:12px"></div>
        <div class="chart-shell">
          <div class="chart"><div class="chart-caption"><span>Shadow equity curves</span><span id="shadow-equity-meta">n/a</span></div><div id="shadow-equity-chart"></div></div>
          <div class="chart"><div class="chart-caption"><span>Regime and setup comparison</span><span id="shadow-regime-meta">n/a</span></div><table class="comparison-table"><thead><tr><th>Behavior</th><th>Net PnL</th><th>Trades</th><th>Win Rate</th><th>Avg End Eq</th></tr></thead><tbody id="shadow-behavior-body"></tbody></table><div style="height:12px"></div><table class="comparison-table"><thead><tr><th>Portfolio</th><th>Regime</th><th>Net PnL</th><th>Trades</th><th>Win Rate</th></tr></thead><tbody id="shadow-regime-body"></tbody></table><div style="height:12px"></div><table class="comparison-table"><thead><tr><th>Portfolio</th><th>Setup</th><th>Net PnL</th><th>Trades</th><th>Win Rate</th></tr></thead><tbody id="shadow-setup-body"></tbody></table></div>
        </div>
      </section>
    </section>
    <section class="layout-equal">
      <section class="panel">
        <div class="panel-header">
          <div><h2>Personal Trading Journal</h2><div class="panel-subtitle">Deine manuelle Sammelstelle fuer Echtgeld-, Demo- und Lern-Notizen mit einfachen Auswertungen</div></div>
        </div>
        <div class="forward-grid" id="personal-journal-summary-grid"></div>
        <div style="height:12px"></div>
        <div class="filter-row">
          <div class="filter-control"><label for="journal-filter-asset">Asset</label><select id="journal-filter-asset"></select></div>
          <div class="filter-control"><label for="journal-filter-strategy">Strategy</label><select id="journal-filter-strategy"></select></div>
          <div class="filter-control"><label for="journal-filter-tag">Tag</label><select id="journal-filter-tag"></select></div>
        </div>
        <div style="height:12px"></div>
        <div class="panel-subtitle">Neuen manuellen Trade lokal eintragen. Das landet nur in deinem persoenlichen Journal auf diesem Geraet.</div>
        <div class="journal-form-grid">
          <div class="filter-control"><label for="journal-form-market">Market</label><select id="journal-form-market"><option value="crypto">crypto</option><option value="stocks">stocks</option><option value="fx">fx</option><option value="metals">metals</option><option value="other">other</option></select></div>
          <div class="filter-control"><label for="journal-form-instrument">Instrument</label><input id="journal-form-instrument" type="text" placeholder="z. B. SOL, BTCUSD, XAUUSD" /></div>
          <div class="filter-control"><label for="journal-form-venue">Venue</label><input id="journal-form-venue" type="text" placeholder="Kraken, Broker, Bybit ..." /></div>
          <div class="filter-control"><label for="journal-form-side">Side</label><select id="journal-form-side"><option value="long">long</option><option value="short">short</option></select></div>
          <div class="filter-control"><label for="journal-form-strategy">Strategy name</label><input id="journal-form-strategy" type="text" placeholder="manual_swing, btc_micro ..." /></div>
          <div class="filter-control"><label for="journal-form-setup-family">Setup family</label><input id="journal-form-setup-family" type="text" placeholder="breakout, swing, scalp ..." /></div>
          <div class="filter-control"><label for="journal-form-timeframe">Timeframe</label><input id="journal-form-timeframe" type="text" placeholder="1M, 5M, 4H, 1D ..." /></div>
          <div class="filter-control"><label for="journal-form-status">Status</label><select id="journal-form-status"><option value="closed">closed</option><option value="open">open</option></select></div>
          <div class="filter-control"><label for="journal-form-entry-ts">Entry time</label><input id="journal-form-entry-ts" type="datetime-local" /></div>
          <div class="filter-control"><label for="journal-form-exit-ts">Exit time</label><input id="journal-form-exit-ts" type="datetime-local" /></div>
          <div class="filter-control"><label for="journal-form-entry-price">Entry price</label><input id="journal-form-entry-price" type="number" step="0.0001" /></div>
          <div class="filter-control"><label for="journal-form-exit-price">Exit price</label><input id="journal-form-exit-price" type="number" step="0.0001" /></div>
          <div class="filter-control"><label for="journal-form-pnl-eur">PnL EUR</label><input id="journal-form-pnl-eur" type="number" step="0.01" value="0" /></div>
          <div class="filter-control"><label for="journal-form-pnl-pct">PnL %</label><input id="journal-form-pnl-pct" type="number" step="0.01" /></div>
          <div class="filter-control"><label for="journal-form-size">Size EUR</label><input id="journal-form-size" type="number" step="0.01" /></div>
          <div class="filter-control"><label for="journal-form-confidence-before">Confidence before</label><input id="journal-form-confidence-before" type="number" min="0" max="100" step="1" /></div>
          <div class="filter-control"><label for="journal-form-confidence-after">Confidence after</label><input id="journal-form-confidence-after" type="number" min="0" max="100" step="1" /></div>
          <div class="filter-control"><label for="journal-form-fees">Fees EUR</label><input id="journal-form-fees" type="number" step="0.01" value="0" /></div>
          <div class="filter-control" style="grid-column: span 3;"><label for="journal-form-tags">Tags</label><input id="journal-form-tags" type="text" placeholder="kommagetrennt, z. B. crypto,scalp,breakout" /></div>
          <div class="filter-control" style="grid-column: span 3;"><label for="journal-form-mistakes">Mistakes</label><input id="journal-form-mistakes" type="text" placeholder="kommagetrennt, z. B. late_stop,fomo" /></div>
          <div class="filter-control" style="grid-column: span 3;"><label for="journal-form-lesson">Lesson</label><textarea id="journal-form-lesson" placeholder="Was war das wichtigste Learning aus diesem Trade?"></textarea></div>
          <div class="filter-control" style="grid-column: span 3;"><label for="journal-form-notes">Notes</label><textarea id="journal-form-notes" placeholder="Freie Notizen, Emotionen, Planabweichungen, Makrokontext ..."></textarea></div>
        </div>
        <div class="journal-form-actions">
          <div id="journal-form-status" class="journal-form-status">Noch kein Eintrag gesendet.</div>
          <button id="journal-form-submit" class="quick-filter-btn active" type="button">Save trade to journal</button>
        </div>
        <div class="chart-shell">
          <div class="chart"><div class="chart-caption"><span>Journal PnL and confidence</span><span id="personal-journal-chart-meta">n/a</span></div><div id="personal-journal-pnl-chart"></div><div style="height:12px"></div><div id="personal-journal-confidence-chart"></div></div>
          <div class="chart"><div class="chart-caption"><span>Journal learning and asset mix</span><span id="personal-journal-breakdown-meta">n/a</span></div><div id="personal-journal-winloss-chart"></div><div style="height:12px"></div><div id="personal-journal-asset-chart"></div></div>
        </div>
        <div style="height:12px"></div>
        <div class="list" id="personal-journal-entry-list"></div>
        <div style="height:12px"></div>
        <div class="list" id="personal-journal-learning-list"></div>
      </section>
      <section class="panel">
        <div class="panel-header">
          <div><h2>Fast-Trading Research Lane</h2><div class="panel-subtitle">Sichere Paper-Lane fuer Micro-Strategien, kurze Haltezeiten und schnelle Verhaltensauswertung</div></div>
        </div>
        <div class="forward-grid" id="fast-research-summary-grid"></div>
        <div style="height:12px"></div>
        <div class="filter-row">
          <div class="filter-control"><label for="fast-research-filter-family">Family</label><select id="fast-research-filter-family"></select></div>
          <div class="filter-control"><label for="fast-research-filter-status">Status</label><select id="fast-research-filter-status"></select></div>
        </div>
        <div class="chart-shell">
          <div class="chart"><div class="chart-caption"><span>Micro strategy ranking</span><span id="fast-research-ranking-meta">n/a</span></div><div id="fast-research-ranking-chart"></div></div>
          <div class="chart"><div class="chart-caption"><span>Signals and experiments</span><span id="fast-research-signals-meta">n/a</span></div><div id="fast-research-signals-chart"></div><div style="height:12px"></div><div id="fast-research-experiments-chart"></div></div>
        </div>
        <div style="height:12px"></div>
        <div class="list" id="fast-research-card-list"></div>
      </section>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div><h2>Strategy Lab</h2><div class="panel-subtitle">Champion/Challenger-Auswertung ueber mehrere parallel getestete Strategien im Demo-Modus</div></div>
      </div>
      <div class="forward-grid" id="strategy-lab-summary-grid"></div>
      <div style="height:12px"></div>
      <div class="chart-shell">
        <div class="chart"><div class="chart-caption"><span>Strategy score ranking</span><span id="strategy-lab-meta">n/a</span></div><div id="strategy-lab-score-chart"></div></div>
        <div class="chart"><div class="chart-caption"><span>Promotion status and gates</span><span id="strategy-lab-gate-meta">n/a</span></div><table class="comparison-table"><thead><tr><th>Strategy</th><th>Family</th><th>Closed Trades</th><th>PF</th><th>Win Rate</th><th>Eligible</th></tr></thead><tbody id="strategy-lab-body"></tbody></table></div>
      </div>
      <div style="height:12px"></div>
      <div class="chart-shell">
        <div class="chart"><div class="chart-caption"><span>Regime stability and gate friction</span><span id="strategy-lab-regime-meta">n/a</span></div><table class="comparison-table"><thead><tr><th>Strategy</th><th>Regimes</th><th>Dominant Share</th><th>Regime Gate</th><th>Failed Gates</th></tr></thead><tbody id="strategy-lab-regime-body"></tbody></table></div>
        <div class="chart"><div class="chart-caption"><span>Asset breadth and gate friction</span><span id="strategy-lab-asset-meta">n/a</span></div><table class="comparison-table"><thead><tr><th>Strategy</th><th>Assets</th><th>Dominant Share</th><th>Asset Gate</th><th>Failed Gates</th></tr></thead><tbody id="strategy-lab-asset-body"></tbody></table></div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div><h2>Journal vs Strategy Lab</h2><div class="panel-subtitle">Abgleich zwischen deinen manuellen Mustern, Bot-Strategien und Guardrails</div></div>
      </div>
      <div class="forward-grid" id="journal-alignment-summary-grid"></div>
      <div style="height:12px"></div>
      <div class="chart-shell">
        <div class="chart"><div class="chart-caption"><span>Family alignment</span><span id="journal-alignment-meta">n/a</span></div><table class="comparison-table"><thead><tr><th>Family</th><th>Manual</th><th>Bot</th><th>Eligible</th><th>Champion</th></tr></thead><tbody id="journal-alignment-family-body"></tbody></table></div>
        <div class="chart"><div class="chart-caption"><span>Asset overlap</span><span id="journal-alignment-asset-meta">n/a</span></div><table class="comparison-table"><thead><tr><th>Asset</th><th>Manual trades</th><th>Tracked by bot</th><th>Fast lane seen</th></tr></thead><tbody id="journal-alignment-asset-body"></tbody></table></div>
      </div>
      <div style="height:12px"></div>
      <div class="layout-equal">
        <div class="list" id="journal-alignment-guardrails"></div>
        <div class="list" id="journal-alignment-beginner"></div>
      </div>
    </section>
    <section class="layout-equal">
      <section class="panel">
        <div class="panel-header">
          <div><h2>Equity and PnL</h2><div class="panel-subtitle">Echte Telemetrie-Zeitreihen fuer Kontowert, kumulierten PnL und Tages-PnL</div></div>
        </div>
        <div class="chart-shell">
          <div class="chart"><div class="chart-caption"><span>Equity curve</span><span id="equity-chart-meta">n/a</span></div><div id="equity-chart"></div></div>
          <div class="chart"><div class="chart-caption"><span>Cumulative PnL and daily PnL</span><span id="pnl-chart-meta">n/a</span></div><div id="pnl-chart"></div><div style="height:12px"></div><div id="daily-pnl-chart"></div></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-header">
          <div><h2>Trade Journal Analytics</h2><div class="panel-subtitle">Exit-Verteilungen, Pair-Performance und die letzten geschlossenen Trades</div></div>
        </div>
        <div class="chart-toolbar">
          <div class="toolbar-group">
            <div class="toolbar-label">Timeframe</div>
            <div class="segmented" id="trade-timeframe-controls">
              <button class="seg-btn active" type="button" data-range="7d">7D</button>
              <button class="seg-btn" type="button" data-range="30d">30D</button>
              <button class="seg-btn" type="button" data-range="all">All</button>
            </div>
            <span class="highlight-note" id="trade-selection-note">Kein Trade ausgewaehlt. Klick auf einen Marker oder Journal-Eintrag fuer Details.</span>
          </div>
          <div class="toolbar-group">
            <button class="action-btn export" type="button" id="export-trades-button">Export CSV</button>
            <button class="action-btn clear" type="button" id="clear-trade-selection-button">Clear Selection</button>
          </div>
        </div>
        <div class="filter-row">
          <div class="filter-control"><label for="trade-filter-pair">Pair</label><select id="trade-filter-pair"></select></div>
          <div class="filter-control"><label for="trade-filter-setup">Setup</label><select id="trade-filter-setup"></select></div>
          <div class="filter-control"><label for="trade-filter-quality">Quality</label><select id="trade-filter-quality"></select></div>
          <div class="filter-control"><label for="trade-filter-reason">Exit Reason</label><select id="trade-filter-reason"></select></div>
          <div class="filter-control"><label for="trade-filter-limit">Trade Window</label><select id="trade-filter-limit"></select></div>
        </div>
        <div class="journal-kpis" id="journal-kpis"></div>
        <div class="chart-shell">
          <div class="chart"><div class="chart-caption"><span>Exit reason breakdown</span><span id="exit-reason-meta">n/a</span></div><div id="exit-reason-chart"></div></div>
          <div class="chart"><div class="chart-caption"><span>Pair and setup breakdown</span><span id="pair-performance-meta">n/a</span></div><div id="pair-performance-chart"></div><div style="height:12px"></div><div id="quality-breakdown-chart"></div><div class="chart-hint">Marker in Equity/PnL sind klickbar und springen auf den zugehoerigen Trade.</div></div>
        </div>
        <div style="height:12px"></div>
        <div class="chart-shell">
          <div class="chart"><div class="chart-caption"><span>MAE / MFE profile</span><span id="mae-mfe-meta">n/a</span></div><div id="mae-mfe-chart"></div></div>
          <div class="chart"><div class="chart-caption"><span>Selected trade replay</span><span id="trade-replay-meta">n/a</span></div><div id="trade-replay-chart"></div><div class="chart-hint">Replay zeigt den Trade-Verlauf als R-Multiple ueber die Zeit. MAE = groesster Zwischenverlust, MFE = groesster Zwischengewinn.</div></div>
        </div>
        <div style="height:12px"></div>
        <div class="list" id="recent-trades-list"></div>
        <div class="inspector-shell" id="selected-trade-shell">
          <div class="inspector-head">
            <div>
              <div class="toolbar-label">Selected Trade</div>
              <div class="inspector-title" id="selected-trade-title">Noch kein Trade ausgewaehlt</div>
            </div>
            <span class="chip warn" id="selected-trade-chip">waiting</span>
          </div>
          <div class="detail-grid" id="selected-trade-grid"></div>
          <div class="list" id="selected-trade-notes"></div>
        </div>
      </section>
    </section>
    <section class="panel">
      <div class="panel-header">
        <div><h2>Trading Bot Copilot</h2><div class="panel-subtitle">Klare Operator-Hinweise plus vereinfachte Erklaerungen fuer Anfaenger</div></div>
      </div>
      <div class="layout-equal">
        <section class="panel" style="padding:16px; min-height:0;">
          <div class="stack">
            <div class="stack-item"><div class="label">Plain Status</div><div class="big" id="copilot-plain-status">n/a</div></div>
            <div class="stack-item"><div class="label">Operator Focus</div><div class="big" id="copilot-operator-focus">n/a</div></div>
            <div class="stack-item"><div class="label">Journal Hint</div><div class="big" id="copilot-journal-hint">n/a</div></div>
          </div>
        </section>
        <section class="panel" style="padding:16px; min-height:0;">
          <div class="list" id="copilot-actions"></div>
        </section>
      </div>
      <div style="height:12px"></div>
      <section class="panel" style="padding:16px; min-height:0;">
        <div class="panel-header"><div><h2>Beginner Guide</h2><div class="panel-subtitle">Wichtige Begriffe in einfacher Sprache</div></div></div>
        <div class="list" id="copilot-beginner-guide"></div>
      </section>
      <div style="height:12px"></div>
      <section class="panel" style="padding:16px; min-height:0;">
        <div class="panel-header"><div><h2>Warnings and Gate Guide</h2><div class="panel-subtitle">Was gerade blockiert und wie du die Meldungen lesen solltest</div></div></div>
        <div class="layout-equal">
          <div class="list" id="copilot-warnings"></div>
          <div class="list" id="copilot-gate-guide"></div>
        </div>
      </section>
    </section>
    <section class="layout">
      <section class="panel"><div class="panel-header"><div><h2>Market History Coverage</h2><div class="panel-subtitle">1m and 15m local store</div></div></div><table><thead><tr><th>Pair</th><th>1m</th><th>15m</th><th>Days</th><th>Last Candle</th></tr></thead><tbody id="pair-history-body"></tbody></table></section>
      <section class="panel"><div class="panel-header"><div><h2>Runtime Stack</h2><div class="panel-subtitle">watchdog, supervisor, task, state paths</div></div></div><div class="list" id="runtime-list"></div></section>
    </section>
    <section class="layout">
      <section class="panel"><div class="panel-header"><div><h2>Latest Sync Delta</h2><div class="panel-subtitle">last capture cycle write activity</div></div></div><table><thead><tr><th>Interval</th><th>Pair</th><th>Fetched</th><th>Merged</th><th>Written</th><th>Status</th></tr></thead><tbody id="cycle-delta-body"></tbody></table></section>
      <section class="panel"><div class="panel-header"><div><h2>Errors and Blockers</h2><div class="panel-subtitle">daily summary and gate blockers</div></div></div><div class="list" id="issues-list"></div></section>
    </section>
    <section class="layout">
      <section class="panel"><div class="panel-header"><div><h2>Recent Runs</h2><div class="panel-subtitle">latest watchdog and supervisor directories</div></div></div><div class="list" id="recent-runs"></div></section>
      <section class="panel"><div class="panel-header"><div><h2>Artifacts</h2><div class="panel-subtitle">state, summary, dashboard</div></div></div><div class="list" id="artifact-list"></div></section>
    </section>
    <div class="footer" id="footer-note"></div>
  </main>
  <script>
    const initialOverview = {initial_json};
    const dashboardState = {{
      overview: null,
      filters: {{
        pair: 'all',
        setup: 'all',
        quality: 'all',
        reason: 'all',
        limit: '12',
        range: '7d',
        shadowPortfolio: 'all',
        shadowBehavior: 'all',
        shadowRegime: 'all',
        journalAsset: 'all',
        journalStrategy: 'all',
        journalTag: 'all',
        fastResearchFamily: 'all',
        fastResearchStatus: 'all',
        marketSymbol: 'all',
        marketTimeframe: '1D',
        marketSearch: '',
        marketQuickFilter: 'all',
        marketFavoritesOnly: 'false',
      }},
      marketFavorites: [],
      selectedTradeKey: null,
      bound: false,
    }};
    const FAVORITES_STORAGE_KEY = 'flow_bot_market_favorites';
    function fmtNumber(value, digits = 4) {{
      if (value === null || value === undefined || value === '') return 'n/a';
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed.toFixed(digits) : String(value);
    }}
    function fmtPercent(value) {{
      if (value === null || value === undefined) return 'n/a';
      const parsed = Number(value);
      return Number.isFinite(parsed) ? `${{parsed.toFixed(2)}}%` : String(value);
    }}
    function fmtText(value) {{
      if (value === null || value === undefined || value === '') return 'n/a';
      return String(value);
    }}
    function fmtCompact(value) {{
      if (value === null || value === undefined || value === '') return 'n/a';
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return String(value);
      return new Intl.NumberFormat('de-DE', {{ notation: 'compact', maximumFractionDigits: 2 }}).format(parsed);
    }}
    function fmtPrice(value) {{
      if (value === null || value === undefined || value === '') return 'n/a';
      const parsed = Number(value);
      if (!Number.isFinite(parsed)) return String(value);
      const digits = parsed >= 1000 ? 2 : parsed >= 100 ? 2 : parsed >= 1 ? 4 : 6;
      return parsed.toLocaleString('de-DE', {{ minimumFractionDigits: digits, maximumFractionDigits: digits }});
    }}
    function fmtDateTime(value) {{
      if (!value) return 'n/a';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString('de-DE', {{
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      }});
    }}
    function pillClassFromText(value) {{
      const text = String(value || '').toLowerCase();
      if (text.includes('green') || text.includes('running') || text.includes('ready') || text.includes('started') || text.includes('completed') || text === 'ok') return 'good';
      if (text.includes('waiting') || text.includes('idle') || text.includes('bereit') || text.includes('pending') || text.includes('active') || text.includes('skipped') || text.includes('disabled')) return 'warn';
      if (text.includes('fail') || text.includes('blocked') || text.includes('red') || text.includes('missing') || text.includes('stopped') || text.includes('error') || text.includes('degraded')) return 'bad';
      return 'warn';
    }}
    function changeClass(value) {{
      const parsed = Number(value || 0);
      if (parsed > 0) return 'positive';
      if (parsed < 0) return 'negative';
      return 'neutral';
    }}
    function escapeHtml(value) {{
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}
    function safeLocalStorage(action, fallback) {{
      try {{
        return action();
      }} catch (_error) {{
        return fallback;
      }}
    }}
    function loadFavoriteSymbols() {{
      const raw = safeLocalStorage(() => window.localStorage.getItem(FAVORITES_STORAGE_KEY), null);
      if (!raw) return [];
      try {{
        const values = JSON.parse(raw);
        if (!Array.isArray(values)) return [];
        return values.map(value => String(value || '').toUpperCase()).filter(Boolean);
      }} catch (_error) {{
        return [];
      }}
    }}
    function persistFavoriteSymbols() {{
      safeLocalStorage(() => window.localStorage.setItem(FAVORITES_STORAGE_KEY, JSON.stringify(dashboardState.marketFavorites || [])), null);
    }}
    function favoriteSymbolsSet() {{
      return new Set((dashboardState.marketFavorites || []).map(value => String(value || '').toUpperCase()));
    }}
    function isFavoriteSymbol(symbol) {{
      return favoriteSymbolsSet().has(String(symbol || '').toUpperCase());
    }}
    function toggleFavoriteSymbol(symbol) {{
      const value = String(symbol || '').toUpperCase();
      if (!value) return;
      const favorites = favoriteSymbolsSet();
      if (favorites.has(value)) {{
        favorites.delete(value);
      }} else {{
        favorites.add(value);
      }}
      dashboardState.marketFavorites = [...favorites].sort();
      persistFavoriteSymbols();
    }}
    function normalizeToken(value) {{
      return String(value || '').trim().toLowerCase();
    }}
    function setText(id, value) {{ const node = document.getElementById(id); if (node) node.textContent = value; }}
    function setHTML(id, value) {{ const node = document.getElementById(id); if (node) node.innerHTML = value; }}
    function sparklineSvg(values, stroke) {{
      if (!values || !values.length) return '<svg viewBox="0 0 100 40" preserveAspectRatio="none"></svg>';
      const width = 100;
      const height = 40;
      const min = Math.min(...values);
      const max = Math.max(...values);
      const range = max - min || 1;
      const points = values.map((value, index) => {{
        const x = (index / Math.max(values.length - 1, 1)) * width;
        const y = height - (((value - min) / range) * (height - 6) + 3);
        return `${{x.toFixed(2)}},${{y.toFixed(2)}}`;
      }});
      const area = [`0,${{height}}`].concat(points).concat([`${{width}},${{height}}`]).join(' ');
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none"><polygon fill="${{stroke}}22" points="${{area}}"></polygon><polyline fill="none" stroke="${{stroke}}" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round" points="${{points.join(' ')}}"></polyline></svg>`;
    }}
    function lineChartSvg(series) {{
      if (!series || !series.length) return '<div class="list-item"><strong>No trend data</strong><span>Recent supervisor runs have not produced a progress series yet.</span></div>';
      const width = 1000;
      const height = 220;
      const padX = 40;
      const padY = 18;
      const values = series.map(point => Number(point.progress_pct || 0));
      const min = Math.min(...values, 0);
      const max = Math.max(...values, 100);
      const range = max - min || 1;
      const usableWidth = width - padX * 2;
      const usableHeight = height - padY * 2;
      const points = series.map((point, index) => {{
        const x = padX + (index / Math.max(series.length - 1, 1)) * usableWidth;
        const y = padY + usableHeight - (((Number(point.progress_pct || 0) - min) / range) * usableHeight);
        return {{ x, y, label: point.label }};
      }});
      const path = points.map((point, index) => `${{index === 0 ? 'M' : 'L'}} ${{point.x.toFixed(2)}} ${{point.y.toFixed(2)}}`).join(' ');
      const areaPath = `${{path}} L ${{points[points.length - 1].x.toFixed(2)}} ${{height - padY}} L ${{points[0].x.toFixed(2)}} ${{height - padY}} Z`;
      const guides = [0, 25, 50, 75, 100].map(value => {{
        const y = padY + usableHeight - (((value - min) / range) * usableHeight);
        return `<line x1="${{padX}}" y1="${{y.toFixed(2)}}" x2="${{width - padX}}" y2="${{y.toFixed(2)}}" stroke="rgba(255,255,255,0.08)" stroke-dasharray="3 6"></line>`;
      }}).join('');
      const labels = points.map(point => `<text x="${{point.x.toFixed(2)}}" y="${{height - 4}}" fill="rgba(147,161,174,0.9)" font-size="11" text-anchor="middle">${{escapeHtml(point.label)}}</text>`).join('');
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none">${{guides}}<path d="${{areaPath}}" fill="rgba(56,211,159,0.14)"></path><path d="${{path}}" fill="none" stroke="#38d39f" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>${{labels}}</svg>`;
    }}
    function multiSeriesLineSvg(seriesGroups) {{
      const groups = (seriesGroups || []).filter(group => (group.points || []).length > 0);
      if (!groups.length) return '<div class="list-item"><strong>No shadow equity yet</strong><span>Sobald Shadow-Portfolios echte Test-Trades schliessen, erscheint hier der Vergleich ihrer Equity-Kurven.</span></div>';
      const width = 1000;
      const height = 240;
      const padX = 32;
      const padY = 18;
      const allPoints = groups.flatMap(group => group.points || []);
      const values = allPoints.map(point => Number(point.value || 0));
      const min = Math.min(...values);
      const max = Math.max(...values);
      const range = max - min || 1;
      const maxLength = Math.max(...groups.map(group => (group.points || []).length), 1);
      const usableWidth = width - padX * 2;
      const usableHeight = height - padY * 2;
      const palette = ['#38d39f', '#62b8ff', '#f4b24f', '#ff6d6d', '#c68cff', '#7be0d6'];
      const paths = groups.map((group, index) => {{
        const color = palette[index % palette.length];
        const points = (group.points || []).map((point, pointIndex) => {{
          const x = padX + (pointIndex / Math.max(maxLength - 1, 1)) * usableWidth;
          const y = padY + usableHeight - (((Number(point.value || 0) - min) / range) * usableHeight);
          return {{ x, y }};
        }});
        const path = points.map((point, pointIndex) => `${{pointIndex === 0 ? 'M' : 'L'}} ${{point.x.toFixed(2)}} ${{point.y.toFixed(2)}}`).join(' ');
        return `<path d="${{path}}" fill="none" stroke="${{color}}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>`;
      }}).join('');
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none">${{paths}}</svg>`;
    }}
    function barChartSvg(items) {{
      if (!items || !items.length) return '<div class="list-item"><strong>No sync totals</strong><span>The last capture cycle has not produced interval totals yet.</span></div>';
      const width = 1000;
      const height = 200;
      const padX = 28;
      const baseY = 156;
      const maxValue = Math.max(...items.map(item => Number(item.written_rows || 0)), 1);
      const band = (width - padX * 2) / items.length;
      const bars = items.map((item, index) => {{
        const barWidth = Math.max(band * 0.45, 40);
        const x = padX + index * band + (band - barWidth) / 2;
        const value = Number(item.written_rows || 0);
        const heightPx = (value / maxValue) * 96;
        const y = baseY - heightPx;
        return `<rect x="${{x.toFixed(2)}}" y="${{y.toFixed(2)}}" width="${{barWidth.toFixed(2)}}" height="${{heightPx.toFixed(2)}}" rx="10" fill="rgba(244,178,79,0.88)"></rect><text x="${{(x + barWidth / 2).toFixed(2)}}" y="${{(y - 8).toFixed(2)}}" text-anchor="middle" fill="rgba(244,246,248,0.95)" font-size="12">${{value}}</text><text x="${{(x + barWidth / 2).toFixed(2)}}" y="${{(baseY + 22).toFixed(2)}}" text-anchor="middle" fill="rgba(147,161,174,0.9)" font-size="11">${{escapeHtml(item.interval)}}</text>`;
      }}).join('');
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none"><line x1="${{padX}}" y1="${{baseY}}" x2="${{width - padX}}" y2="${{baseY}}" stroke="rgba(255,255,255,0.12)"></line>${{bars}}</svg>`;
    }}
    function metricLineSvg(series, stroke, fill, markers = []) {{
      if (!series || !series.length) return '<div class="list-item"><strong>No telemetry series</strong><span>Es liegen noch nicht genug Telemetriepunkte fuer diese Kurve vor.</span></div>';
      const width = 1000;
      const height = 220;
      const padX = 32;
      const padY = 16;
      const values = series.map(point => Number(point.value || 0));
      const min = Math.min(...values);
      const max = Math.max(...values);
      const range = max - min || 1;
      const usableWidth = width - padX * 2;
      const usableHeight = height - padY * 2;
      const points = series.map((point, index) => {{
        const x = padX + (index / Math.max(series.length - 1, 1)) * usableWidth;
        const y = padY + usableHeight - (((Number(point.value || 0) - min) / range) * usableHeight);
        return {{ x, y, label: point.label, value: point.value }};
      }});
      const path = points.map((point, index) => `${{index === 0 ? 'M' : 'L'}} ${{point.x.toFixed(2)}} ${{point.y.toFixed(2)}}`).join(' ');
      const areaPath = `${{path}} L ${{points[points.length - 1].x.toFixed(2)}} ${{height - padY}} L ${{points[0].x.toFixed(2)}} ${{height - padY}} Z`;
      const markerHtml = markers.map(marker => {{
        const point = points.find(candidate => candidate.label === marker.label);
        if (!point) return '';
        const color = Number(marker.pnl_eur || 0) >= 0 ? '#38d39f' : '#ff6d6d';
        const selected = marker.trade_key && marker.trade_key === dashboardState.selectedTradeKey;
        const radius = selected ? 8.5 : 5.5;
        const strokeColor = selected ? '#8cc9ff' : 'rgba(9,13,18,0.95)';
        const strokeWidth = selected ? 3 : 2;
        return `<circle class="marker-hit" data-trade-key="${{escapeHtml(marker.trade_key || '')}}" cx="${{point.x.toFixed(2)}}" cy="${{point.y.toFixed(2)}}" r="${{radius}}" fill="${{color}}" stroke="${{strokeColor}}" stroke-width="${{strokeWidth}}"></circle>`;
      }}).join('');
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none"><path d="${{areaPath}}" fill="${{fill}}"></path><path d="${{path}}" fill="none" stroke="${{stroke}}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>${{markerHtml}}</svg>`;
    }}
    function labeledBarChartSvg(items, color) {{
      if (!items || !items.length) return '<div class="list-item"><strong>No distribution yet</strong><span>Diese Auswertung wird sichtbar, sobald Telemetriepunkte vorhanden sind.</span></div>';
      const width = 1000;
      const height = 210;
      const padX = 28;
      const baseY = 160;
      const maxValue = Math.max(...items.map(item => Math.abs(Number(item.value || 0))), 1);
      const band = (width - padX * 2) / items.length;
      const bars = items.map((item, index) => {{
        const barWidth = Math.max(band * 0.5, 40);
        const x = padX + index * band + (band - barWidth) / 2;
        const rawValue = Number(item.value || 0);
        const heightPx = (Math.abs(rawValue) / maxValue) * 92;
        const y = baseY - heightPx;
        const fill = rawValue >= 0 ? color : 'rgba(255,109,109,0.88)';
        return `<rect x="${{x.toFixed(2)}}" y="${{y.toFixed(2)}}" width="${{barWidth.toFixed(2)}}" height="${{heightPx.toFixed(2)}}" rx="10" fill="${{fill}}"></rect><text x="${{(x + barWidth / 2).toFixed(2)}}" y="${{(y - 8).toFixed(2)}}" text-anchor="middle" fill="rgba(244,246,248,0.95)" font-size="12">${{rawValue}}</text><text x="${{(x + barWidth / 2).toFixed(2)}}" y="${{(baseY + 24).toFixed(2)}}" text-anchor="middle" fill="rgba(147,161,174,0.9)" font-size="11">${{escapeHtml(item.label)}}</text>`;
      }}).join('');
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none"><line x1="${{padX}}" y1="${{baseY}}" x2="${{width - padX}}" y2="${{baseY}}" stroke="rgba(255,255,255,0.12)"></line>${{bars}}</svg>`;
    }}
    function maeMfeScatterSvg(items) {{
      if (!items || !items.length) return '<div class="list-item"><strong>No excursion data</strong><span>MAE/MFE wird sichtbar, sobald geschlossene Trades mit Replay-Daten vorliegen.</span></div>';
      const width = 1000;
      const height = 240;
      const padX = 48;
      const padY = 24;
      const maxMfe = Math.max(...items.map(item => Number(item.mfe_r || 0)), 0.5);
      const minMae = Math.min(...items.map(item => Number(item.mae_r || 0)), -0.5);
      const xRange = maxMfe || 1;
      const yRange = Math.abs(minMae) || 1;
      const xAxisY = height - padY;
      const yAxisX = padX;
      const guides = [0.25, 0.5, 1.0, 1.5, 2.0].map(value => {{
        if (value > xRange) return '';
        const x = yAxisX + (value / xRange) * (width - padX * 2);
        return `<line x1="${{x.toFixed(2)}}" y1="${{padY}}" x2="${{x.toFixed(2)}}" y2="${{xAxisY}}" stroke="rgba(255,255,255,0.06)" stroke-dasharray="3 6"></line><text x="${{x.toFixed(2)}}" y="${{height - 6}}" text-anchor="middle" fill="rgba(147,161,174,0.9)" font-size="11">MFE ${{value.toFixed(2)}}R</text>`;
      }}).join('');
      const horizontalGuides = [-0.25, -0.5, -1.0, -1.5, -2.0].map(value => {{
        if (value < minMae) return '';
        const y = padY + ((Math.abs(value) / yRange) * (height - padY * 2));
        return `<line x1="${{yAxisX}}" y1="${{y.toFixed(2)}}" x2="${{width - padX}}" y2="${{y.toFixed(2)}}" stroke="rgba(255,255,255,0.06)" stroke-dasharray="3 6"></line><text x="6" y="${{(y + 4).toFixed(2)}}" fill="rgba(147,161,174,0.9)" font-size="11">MAE ${{value.toFixed(2)}}R</text>`;
      }}).join('');
      const dots = items.map(item => {{
        const x = yAxisX + ((Number(item.mfe_r || 0) / xRange) * (width - padX * 2));
        const y = padY + ((Math.abs(Number(item.mae_r || 0)) / yRange) * (height - padY * 2));
        const pnl = Number(item.pnl_eur || 0);
        const fill = pnl >= 0 ? '#38d39f' : '#ff6d6d';
        const selected = item.trade_key && item.trade_key === dashboardState.selectedTradeKey;
        const radius = selected ? 8 : 6;
        return `<circle class="marker-hit" data-trade-key="${{escapeHtml(item.trade_key || '')}}" cx="${{x.toFixed(2)}}" cy="${{y.toFixed(2)}}" r="${{radius}}" fill="${{fill}}" stroke="${{selected ? '#8cc9ff' : 'rgba(9,13,18,0.95)'}}" stroke-width="${{selected ? 3 : 2}}"></circle>`;
      }}).join('');
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none"><line x1="${{yAxisX}}" y1="${{padY}}" x2="${{yAxisX}}" y2="${{xAxisY}}" stroke="rgba(255,255,255,0.16)"></line><line x1="${{yAxisX}}" y1="${{xAxisY}}" x2="${{width - padX}}" y2="${{xAxisY}}" stroke="rgba(255,255,255,0.16)"></line>${{guides}}${{horizontalGuides}}${{dots}}</svg>`;
    }}
    function replayLineSvg(points) {{
      if (!points || !points.length) return '<div class="list-item"><strong>No replay selected</strong><span>Waehle einen Trade aus dem Journal oder aus den Equity-/PnL-Markern aus.</span></div>';
      const width = 1000;
      const height = 240;
      const padX = 42;
      const padY = 24;
      const values = points.map(point => Number(point.r_multiple || 0));
      const min = Math.min(...values, -0.25);
      const max = Math.max(...values, 0.25);
      const range = (max - min) || 1;
      const usableWidth = width - padX * 2;
      const usableHeight = height - padY * 2;
      const plotPoints = points.map((point, index) => {{
        const x = padX + (index / Math.max(points.length - 1, 1)) * usableWidth;
        const y = padY + usableHeight - (((Number(point.r_multiple || 0) - min) / range) * usableHeight);
        return {{ x, y, point }};
      }});
      const path = plotPoints.map((item, index) => `${{index === 0 ? 'M' : 'L'}} ${{item.x.toFixed(2)}} ${{item.y.toFixed(2)}}`).join(' ');
      const zeroY = padY + usableHeight - (((0 - min) / range) * usableHeight);
      const markers = plotPoints.map((item, index) => {{
        const isFirst = index === 0;
        const isLast = index === plotPoints.length - 1;
        const fill = isFirst ? '#8cc9ff' : isLast ? '#f4b24f' : 'rgba(255,255,255,0.72)';
        return `<circle cx="${{item.x.toFixed(2)}}" cy="${{item.y.toFixed(2)}}" r="${{isFirst || isLast ? 5.5 : 3.5}}" fill="${{fill}}"></circle>`;
      }}).join('');
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none"><line x1="${{padX}}" y1="${{zeroY.toFixed(2)}}" x2="${{width - padX}}" y2="${{zeroY.toFixed(2)}}" stroke="rgba(255,255,255,0.10)" stroke-dasharray="4 6"></line><path d="${{path}}" fill="none" stroke="#8cc9ff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>${{markers}}</svg>`;
    }}
    function setSelectOptions(id, values, currentValue, allLabel) {{
      const node = document.getElementById(id);
      if (!node) return;
      const options = [`<option value="all">${{escapeHtml(allLabel)}}</option>`].concat(values.map(value => `<option value="${{escapeHtml(String(value))}}">${{escapeHtml(String(value))}}</option>`));
      node.innerHTML = options.join('');
      node.value = values.includes(currentValue) || currentValue === 'all' ? currentValue : 'all';
    }}
    function marketWindowClass(changePct) {{
      const value = Number(changePct || 0);
      if (value > 0.35) return 'good';
      if (value < -0.35) return 'bad';
      return 'neutral';
    }}
    function marketQuickFilters() {{
      return [
        {{ key: 'all', label: 'All' }},
        {{ key: 'leaders', label: 'Leaders' }},
        {{ key: 'tight', label: 'Tight' }},
        {{ key: 'volatile', label: 'Volatile' }},
        {{ key: 'covered', label: 'Covered' }},
      ];
    }}
    function profileForTimeframe(row, timeframe) {{
      const profiles = row && row.timeframe_profiles ? row.timeframe_profiles : {{}};
      return profiles[timeframe] || profiles['1D'] || {{}};
    }}
    function filteredMarketPairs(marketState) {{
      const pairs = [...(marketState.pairs || [])];
      const selectedTimeframe = marketState.selectedTimeframe || '1D';
      const search = normalizeToken(dashboardState.filters.marketSearch);
      const quickFilter = dashboardState.filters.marketQuickFilter || 'all';
      const favoritesOnly = dashboardState.filters.marketFavoritesOnly === 'true';
      const favorites = favoriteSymbolsSet();
      const rankedByMove = pairs
        .map(row => {{
          const profile = profileForTimeframe(row, selectedTimeframe);
          return {{
            symbol: row.symbol,
            strength: Math.abs(Number(profile.change_pct ?? row.change_1h_pct ?? 0)),
          }};
        }})
        .sort((left, right) => right.strength - left.strength);
      const leaderCutoffIndex = Math.max(0, Math.min(rankedByMove.length - 1, Math.max(2, Math.ceil(rankedByMove.length / 3) - 1)));
      const leaderThreshold = rankedByMove.length ? Number(rankedByMove[leaderCutoffIndex].strength || 0) : Infinity;
      const leaderSymbols = new Set(rankedByMove.filter(row => row.strength >= leaderThreshold && row.strength > 0).map(row => row.symbol));
      return pairs.filter(row => {{
        const symbol = String(row.symbol || '');
        const profile = profileForTimeframe(row, selectedTimeframe);
        const spread = Number(row.spread_bps ?? Number.POSITIVE_INFINITY);
        const rangePct = Number(profile.range_pct ?? row.range_24h_pct ?? 0);
        const coverage = Number(profile.coverage_pct ?? 0);
        if (search && !normalizeToken(symbol).includes(search)) return false;
        if (favoritesOnly && !favorites.has(symbol)) return false;
        if (quickFilter === 'leaders' && !leaderSymbols.has(symbol)) return false;
        if (quickFilter === 'tight' && !(Number.isFinite(spread) && spread <= 8.0)) return false;
        if (quickFilter === 'volatile' && !(rangePct >= 2.0)) return false;
        if (quickFilter === 'covered' && !(coverage >= 0.65)) return false;
        return true;
      }});
    }}
    function detailedSeriesSvg(values, stroke, title) {{
      if (!values || !values.length) return '<div class="list-item"><strong>No market series</strong><span>Fuer das gewaehlte Zeitfenster sind noch keine Daten verfuegbar.</span></div>';
      const width = 1000;
      const height = 260;
      const padX = 38;
      const padY = 20;
      const min = Math.min(...values);
      const max = Math.max(...values);
      const range = max - min || 1;
      const usableWidth = width - padX * 2;
      const usableHeight = height - padY * 2;
      const points = values.map((value, index) => {{
        const x = padX + (index / Math.max(values.length - 1, 1)) * usableWidth;
        const y = padY + usableHeight - (((Number(value) - min) / range) * usableHeight);
        return {{ x, y, value }};
      }});
      const path = points.map((point, index) => `${{index === 0 ? 'M' : 'L'}} ${{point.x.toFixed(2)}} ${{point.y.toFixed(2)}}`).join(' ');
      const areaPath = `${{path}} L ${{points[points.length - 1].x.toFixed(2)}} ${{height - padY}} L ${{points[0].x.toFixed(2)}} ${{height - padY}} Z`;
      const guides = [min, (min + max) / 2, max].map(value => {{
        const y = padY + usableHeight - (((Number(value) - min) / range) * usableHeight);
        return `<line x1="${{padX}}" y1="${{y.toFixed(2)}}" x2="${{width - padX}}" y2="${{y.toFixed(2)}}" stroke="rgba(255,255,255,0.08)" stroke-dasharray="3 6"></line><text x="${{width - padX + 6}}" y="${{(y + 4).toFixed(2)}}" fill="rgba(147,161,174,0.9)" font-size="11">${{fmtPrice(value)}}</text>`;
      }}).join('');
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="none"><defs><linearGradient id="market-area-${{escapeHtml(String(title || 'series'))}}" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="${{stroke}}" stop-opacity="0.30"></stop><stop offset="100%" stop-color="${{stroke}}" stop-opacity="0.02"></stop></linearGradient></defs>${{guides}}<path d="${{areaPath}}" fill="url(#market-area-${{escapeHtml(String(title || 'series'))}})"></path><path d="${{path}}" fill="none" stroke="${{stroke}}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>${{points.slice(0, 1).map(point => `<circle cx="${{point.x.toFixed(2)}}" cy="${{point.y.toFixed(2)}}" r="4.5" fill="${{stroke}}"></circle>`).join('')}}<circle cx="${{points[points.length - 1].x.toFixed(2)}}" cy="${{points[points.length - 1].y.toFixed(2)}}" r="4.5" fill="${{stroke}}"></circle></svg>`;
    }}
    function journalFormPayload() {{
      const valueOf = id => {{
        const node = document.getElementById(id);
        return node ? node.value : '';
      }};
      return {{
        market: valueOf('journal-form-market'),
        instrument: valueOf('journal-form-instrument'),
        venue: valueOf('journal-form-venue'),
        side: valueOf('journal-form-side'),
        strategy_name: valueOf('journal-form-strategy'),
        setup_family: valueOf('journal-form-setup-family'),
        timeframe: valueOf('journal-form-timeframe'),
        status: valueOf('journal-form-status'),
        entry_ts: normalizeJournalDate(valueOf('journal-form-entry-ts')),
        exit_ts: normalizeJournalDate(valueOf('journal-form-exit-ts')),
        entry_price: normalizeOptionalNumber(valueOf('journal-form-entry-price')),
        exit_price: normalizeOptionalNumber(valueOf('journal-form-exit-price')),
        pnl_eur: Number(valueOf('journal-form-pnl-eur') || 0),
        pnl_pct: normalizeOptionalNumber(valueOf('journal-form-pnl-pct')),
        size_notional_eur: normalizeOptionalNumber(valueOf('journal-form-size')),
        confidence_before: normalizeOptionalInt(valueOf('journal-form-confidence-before')),
        confidence_after: normalizeOptionalInt(valueOf('journal-form-confidence-after')),
        fees_eur: Number(valueOf('journal-form-fees') || 0),
        tags: valueOf('journal-form-tags'),
        mistakes: valueOf('journal-form-mistakes'),
        lesson: valueOf('journal-form-lesson'),
        notes: valueOf('journal-form-notes'),
      }};
    }}
    function normalizeOptionalNumber(value) {{
      if (value === null || value === undefined || value === '') return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }}
    function normalizeOptionalInt(value) {{
      if (value === null || value === undefined || value === '') return null;
      const parsed = parseInt(value, 10);
      return Number.isFinite(parsed) ? parsed : null;
    }}
    function normalizeJournalDate(value) {{
      if (!value) return null;
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? null : date.toISOString();
    }}
    async function submitJournalEntry() {{
      const statusNode = document.getElementById('journal-form-status');
      if (statusNode) {{
        statusNode.textContent = 'Saving journal entry...';
        statusNode.className = 'journal-form-status';
      }}
      try {{
        const response = await fetch('/api/personal-journal/append', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(journalFormPayload()),
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || `HTTP ${{response.status}}`);
        }}
        if (statusNode) {{
          statusNode.textContent = `Saved: ${{payload.entry.instrument}} | ${{payload.entry.strategy_name}}`;
          statusNode.className = 'journal-form-status good';
        }}
        await refreshOverview();
      }} catch (error) {{
        if (statusNode) {{
          statusNode.textContent = `Journal save failed: ${{error}}`;
          statusNode.className = 'journal-form-status bad';
        }}
      }}
    }}
    dashboardState.marketFavorites = loadFavoriteSymbols();
    function bindFilterControls() {{
      if (dashboardState.bound) return;
      [
        ['trade-filter-pair', 'pair'],
        ['trade-filter-setup', 'setup'],
        ['trade-filter-quality', 'quality'],
        ['trade-filter-reason', 'reason'],
        ['trade-filter-limit', 'limit'],
        ['journal-filter-asset', 'journalAsset'],
        ['journal-filter-strategy', 'journalStrategy'],
        ['journal-filter-tag', 'journalTag'],
        ['fast-research-filter-family', 'fastResearchFamily'],
        ['fast-research-filter-status', 'fastResearchStatus'],
      ].forEach(([id, key]) => {{
        const node = document.getElementById(id);
        if (!node) return;
        node.addEventListener('change', event => {{
          dashboardState.filters[key] = event.target.value;
          if (dashboardState.overview) renderTradeDetail(dashboardState.overview);
        }});
      }});
      [
        ['shadow-filter-portfolio', 'shadowPortfolio'],
        ['shadow-filter-behavior', 'shadowBehavior'],
        ['shadow-filter-regime', 'shadowRegime'],
      ].forEach(([id, key]) => {{
        const node = document.getElementById(id);
        if (!node) return;
        node.addEventListener('change', event => {{
          dashboardState.filters[key] = event.target.value;
          if (dashboardState.overview) renderShadowSection(dashboardState.overview);
        }});
      }});
      const marketAssetSelect = document.getElementById('market-asset-select');
      if (marketAssetSelect) {{
        marketAssetSelect.addEventListener('change', event => {{
          dashboardState.filters.marketSymbol = event.target.value;
          if (dashboardState.overview) renderOverview(dashboardState.overview);
        }});
      }}
      const marketSearchInput = document.getElementById('market-search-input');
      if (marketSearchInput) {{
        marketSearchInput.addEventListener('input', event => {{
          dashboardState.filters.marketSearch = event.target.value || '';
          if (dashboardState.overview) renderOverview(dashboardState.overview);
        }});
      }}
      const marketFavoritesToggle = document.getElementById('market-favorites-toggle');
      if (marketFavoritesToggle) {{
        marketFavoritesToggle.addEventListener('click', () => {{
          dashboardState.filters.marketFavoritesOnly = dashboardState.filters.marketFavoritesOnly === 'true' ? 'false' : 'true';
          if (dashboardState.overview) renderOverview(dashboardState.overview);
        }});
      }}
      const marketQuickFilterBar = document.getElementById('market-quick-filter-bar');
      if (marketQuickFilterBar) {{
        marketQuickFilterBar.addEventListener('click', event => {{
          const button = event.target.closest('.quick-filter-btn');
          if (!button) return;
          dashboardState.filters.marketQuickFilter = button.dataset.marketQuickFilter || 'all';
          if (dashboardState.overview) renderOverview(dashboardState.overview);
        }});
      }}
      const marketTimeframeStrip = document.getElementById('market-timeframe-strip');
      if (marketTimeframeStrip) {{
        marketTimeframeStrip.addEventListener('click', event => {{
          const button = event.target.closest('.timeframe-btn');
          if (!button) return;
          dashboardState.filters.marketTimeframe = button.dataset.marketTimeframe || '1D';
          if (dashboardState.overview) renderOverview(dashboardState.overview);
        }});
      }}
      const marketGrid = document.getElementById('market-grid');
      if (marketGrid) {{
        marketGrid.addEventListener('click', event => {{
          const card = event.target.closest('.market-card');
          if (!card || !card.dataset.marketSymbol) return;
          dashboardState.filters.marketSymbol = card.dataset.marketSymbol;
          if (dashboardState.overview) renderOverview(dashboardState.overview);
        }});
      }}
      const marketSidebarList = document.getElementById('market-sidebar-list');
      if (marketSidebarList) {{
        marketSidebarList.addEventListener('click', event => {{
          const favoriteButton = event.target.closest('.asset-favorite-btn');
          if (favoriteButton && favoriteButton.dataset.favoriteSymbol) {{
            toggleFavoriteSymbol(favoriteButton.dataset.favoriteSymbol);
            if (dashboardState.overview) renderOverview(dashboardState.overview);
            return;
          }}
          const item = event.target.closest('.asset-sidebar-item');
          if (!item || !item.dataset.marketSymbol) return;
          dashboardState.filters.marketSymbol = item.dataset.marketSymbol;
          if (dashboardState.overview) renderOverview(dashboardState.overview);
        }});
      }}
      const marketExplorerTable = document.getElementById('market-explorer-table');
      if (marketExplorerTable) {{
        marketExplorerTable.addEventListener('click', event => {{
          const row = event.target.closest('tr[data-market-symbol]');
          if (!row || !row.dataset.marketSymbol) return;
          dashboardState.filters.marketSymbol = row.dataset.marketSymbol;
          if (dashboardState.overview) renderOverview(dashboardState.overview);
        }});
      }}
      const marketWindowBody = document.getElementById('market-window-body');
      if (marketWindowBody) {{
        marketWindowBody.addEventListener('click', event => {{
          const row = event.target.closest('tr[data-market-timeframe]');
          if (!row || !row.dataset.marketTimeframe) return;
          dashboardState.filters.marketTimeframe = row.dataset.marketTimeframe;
          if (dashboardState.overview) renderOverview(dashboardState.overview);
        }});
      }}
      document.querySelectorAll('#trade-timeframe-controls .seg-btn').forEach(node => {{
        node.addEventListener('click', () => {{
          dashboardState.filters.range = node.dataset.range || 'all';
          document.querySelectorAll('#trade-timeframe-controls .seg-btn').forEach(button => button.classList.toggle('active', button === node));
          if (dashboardState.overview) renderTradeDetail(dashboardState.overview);
        }});
      }});
      const exportButton = document.getElementById('export-trades-button');
      if (exportButton) {{
        exportButton.addEventListener('click', () => exportFilteredTradesCsv());
      }}
      const journalSubmitButton = document.getElementById('journal-form-submit');
      if (journalSubmitButton) {{
        journalSubmitButton.addEventListener('click', () => {{
          submitJournalEntry();
        }});
      }}
      const clearButton = document.getElementById('clear-trade-selection-button');
      if (clearButton) {{
        clearButton.addEventListener('click', () => {{
          dashboardState.selectedTradeKey = null;
          if (dashboardState.overview) renderTradeDetail(dashboardState.overview);
        }});
      }}
      ['equity-chart', 'pnl-chart', 'mae-mfe-chart'].forEach(id => {{
        const node = document.getElementById(id);
        if (!node) return;
        node.addEventListener('click', event => {{
          const marker = event.target.closest('.marker-hit');
          if (!marker) return;
          dashboardState.selectedTradeKey = marker.dataset.tradeKey || null;
          if (dashboardState.overview) renderTradeDetail(dashboardState.overview);
        }});
      }});
      dashboardState.bound = true;
    }}
    function filteredTrades(tradeAnalytics) {{
      const trades = [...(tradeAnalytics.all_trades || [])];
      const filters = dashboardState.filters;
      const byPair = filters.pair === 'all' ? trades : trades.filter(trade => trade.pair === filters.pair);
      const bySetup = filters.setup === 'all' ? byPair : byPair.filter(trade => trade.setup_type === filters.setup);
      const byQuality = filters.quality === 'all' ? bySetup : bySetup.filter(trade => trade.quality === filters.quality);
      const byReason = filters.reason === 'all' ? byQuality : byQuality.filter(trade => trade.reason === filters.reason);
      const range = filters.range || 'all';
      let byRange = byReason;
      if (range !== 'all' && byReason.length) {{
        const newest = byReason.reduce((latest, trade) => {{
          const date = new Date(trade.exit_ts);
          return Number.isNaN(date.getTime()) || date <= latest ? latest : date;
        }}, new Date(0));
        const days = range === '7d' ? 7 : range === '30d' ? 30 : null;
        if (days) {{
          const cutoff = new Date(newest.getTime() - (days * 24 * 60 * 60 * 1000));
          byRange = byReason.filter(trade => {{
            const date = new Date(trade.exit_ts);
            return !Number.isNaN(date.getTime()) && date >= cutoff;
          }});
        }}
      }}
      const limit = Number(filters.limit || 12);
      return Number.isFinite(limit) ? byRange.slice(0, limit) : byRange;
    }}
    function findTradeByKey(tradeAnalytics, tradeKey) {{
      return (tradeAnalytics.all_trades || []).find(trade => trade.trade_key === tradeKey) || null;
    }}
    function summarizeReason(reason) {{
      const value = String(reason || 'unknown');
      if (value === 'protective_stop') return 'Der Markt hat den Sicherheits-Stop erreicht. Das ist Verlustbegrenzung, kein Systemfehler.';
      if (value === 'time_stop' || value === 'time_decay_exit') return 'Der Trade hat zu lange kein gutes Momentum gezeigt und wurde deshalb konsequent beendet.';
      if (value === 'hard_flat') return 'Die Session wurde planmaessig glattgestellt, damit nichts ueber Nacht offen bleibt.';
      if (value === 'kill_switch_exit') return 'Die Sicherheitslogik hat alle Positionen geschlossen, um weiteres Risiko sofort zu stoppen.';
      return 'Der Trade wurde nach der aktuellen Exit-Logik des Bots geschlossen.';
    }}
    function exportFilteredTradesCsv() {{
      if (!dashboardState.overview) return;
      const trades = filteredTrades(dashboardState.overview.trade_analytics || {{}});
      const headers = ['trade_key', 'pair', 'quality', 'reason', 'score', 'pnl_eur', 'hold_minutes', 'budget_eur', 'entry_ts', 'exit_ts', 'reason_code'];
      const rows = [headers.join(',')].concat(trades.map(trade => headers.map(header => {{
        const raw = trade[header] ?? '';
        const value = String(raw).replaceAll('"', '""');
        return `"${{value}}"`;
      }}).join(',')));
      const blob = new Blob([rows.join('\\n')], {{ type: 'text/csv;charset=utf-8;' }});
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `flow_bot_trades_${{dashboardState.filters.range || 'all'}}_${{dashboardState.filters.pair || 'all'}}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }}
    function countByLabel(trades, field) {{
      const counts = new Map();
      trades.forEach(trade => {{
        const label = trade[field] || 'n/a';
        counts.set(label, (counts.get(label) || 0) + 1);
      }});
      return [...counts.entries()].map(([label, value]) => ({{ label, value }}));
    }}
    function pairPerformanceFromTrades(trades) {{
      const buckets = new Map();
      trades.forEach(trade => {{
        const key = trade.pair || 'n/a';
        const bucket = buckets.get(key) || {{ label: key, value: 0, trades: 0 }};
        bucket.value += Number(trade.pnl_eur || 0);
        bucket.trades += 1;
        buckets.set(key, bucket);
      }});
      return [...buckets.values()].sort((left, right) => right.value - left.value);
    }}
    function dailyPnlFromTrades(trades) {{
      const buckets = new Map();
      trades.forEach(trade => {{
        const date = new Date(trade.exit_ts);
        const label = Number.isNaN(date.getTime()) ? 'n/a' : date.toLocaleDateString('de-DE', {{ day: '2-digit', month: '2-digit' }});
        const bucket = buckets.get(label) || {{ label, value: 0 }};
        bucket.value += Number(trade.pnl_eur || 0);
        buckets.set(label, bucket);
      }});
      return [...buckets.values()];
    }}
    function buildFilteredMarkers(series, trades) {{
      const pointsByLabel = new Map((series || []).map(point => [point.label, point]));
      const markers = trades.map(trade => {{
        const date = new Date(trade.exit_ts);
        const label = Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('de-DE', {{ day: '2-digit', month: '2-digit' }}) + ' ' + date.toLocaleTimeString('de-DE', {{ hour: '2-digit', minute: '2-digit' }});
        if (!label || !pointsByLabel.has(label)) return null;
        return {{
          label,
          trade_key: trade.trade_key || '',
          pnl_eur: Number(trade.pnl_eur || 0),
          pair: trade.pair || 'n/a',
          reason: trade.reason || 'n/a',
          quality: trade.quality || 'n/a',
        }};
      }}).filter(Boolean);
      const deduped = new Map();
      markers.forEach(marker => {{
        if (!deduped.has(marker.label)) deduped.set(marker.label, marker);
      }});
      return [...deduped.values()];
    }}
    function filteredPersonalJournal(journal) {{
      const activeAsset = dashboardState.filters.journalAsset || 'all';
      const activeStrategy = dashboardState.filters.journalStrategy || 'all';
      const activeTag = dashboardState.filters.journalTag || 'all';
      const entries = (journal.entries || []).filter(entry => {{
        const asset = String(entry.asset || '').toUpperCase();
        const strategy = String(entry.strategy || '').trim();
        const tags = (entry.tags || []).map(tag => String(tag));
        const assetOk = activeAsset === 'all' || asset === activeAsset;
        const strategyOk = activeStrategy === 'all' || strategy === activeStrategy;
        const tagOk = activeTag === 'all' || tags.includes(activeTag);
        return assetOk && strategyOk && tagOk;
      }});
      const sortedEntries = entries.slice().sort((a, b) => new Date(b.entry_ts || b.ts || 0).getTime() - new Date(a.entry_ts || a.ts || 0).getTime());
      const cumulativeSeries = [];
      let cumulative = 0;
      sortedEntries.slice().reverse().forEach((entry, index) => {{
        cumulative += Number(entry.pnl_eur || 0);
        cumulativeSeries.push({{ label: String(entry.entry_ts || entry.ts || ('entry-' + String(index))), value: cumulative }});
      }});
      const confidenceSeries = sortedEntries.slice().reverse().map((entry, index) => ({{
        label: String(entry.entry_ts || entry.ts || ('entry-' + String(index))),
        value: Number(entry.confidence || 0),
      }}));
      return {{
        activeAsset,
        activeStrategy,
        activeTag,
        entries: sortedEntries,
        cumulativeSeries,
        confidenceSeries,
      }};
    }}
    function filteredFastResearchLab(lab) {{
      const activeFamily = dashboardState.filters.fastResearchFamily || 'all';
      const activeStatus = dashboardState.filters.fastResearchStatus || 'all';
      const strategies = (lab.strategies || []).filter(row => {{
        const family = String(row.family || '').trim();
        const status = String(row.status || (row.eligible_for_promotion ? 'eligible' : 'watch')).trim();
        return (activeFamily === 'all' || family === activeFamily) && (activeStatus === 'all' || status === activeStatus);
      }});
      const experiments = (lab.experiments || []).filter(row => (activeStatus === 'all' || String(row.status || '').trim() === activeStatus));
      const ranking = strategies.slice().sort((a, b) => Number(b.score || 0) - Number(a.score || 0));
      return {{
        activeFamily,
        activeStatus,
        strategies: ranking,
        experiments,
      }};
    }}
    function filteredShadowData(shadow) {{
      const filterOptions = shadow.filter_options || {{}};
      const activePortfolio = dashboardState.filters.shadowPortfolio || 'all';
      const activeBehavior = dashboardState.filters.shadowBehavior || 'all';
      const activeRegime = dashboardState.filters.shadowRegime || 'all';
      const portfolios = (shadow.portfolios || []).filter(row => (activePortfolio === 'all' || row.name === activePortfolio) && (activeBehavior === 'all' || row.behavior_profile === activeBehavior));
      const visibleNames = new Set(portfolios.map(row => row.name));
      const equityCurves = (shadow.equity_curves || []).filter(row => visibleNames.has(row.portfolio));
      const regimeRows = (shadow.regime_comparison || []).filter(row => visibleNames.has(row.portfolio) && (activeRegime === 'all' || row.regime_label === activeRegime));
      const setupRows = (shadow.setup_comparison || []).filter(row => visibleNames.has(row.portfolio));
      const behaviorRows = (shadow.behavior_comparison || []).filter(row => activeBehavior === 'all' || row.behavior_profile === activeBehavior);
      return {{
        filterOptions,
        activePortfolio,
        activeBehavior,
        activeRegime,
        portfolios,
        equityCurves,
        behaviorRows,
        regimeRows,
        setupRows,
      }};
    }}
    function renderPersonalJournalSection(overview) {{
      const journal = overview.personal_journal || {{}};
      const journalView = filteredPersonalJournal(journal);
      const summary = journal.summary || {{}};
      const filterOptions = journal.filter_options || {{}};
      setSelectOptions('journal-filter-asset', filterOptions.assets || [], journalView.activeAsset, 'All assets');
      setSelectOptions('journal-filter-strategy', filterOptions.strategies || [], journalView.activeStrategy, 'All strategies');
      setSelectOptions('journal-filter-tag', filterOptions.tags || [], journalView.activeTag, 'All tags');
      setHTML('personal-journal-summary-grid', [
        ['Entries', fmtText(summary.total_entries ?? journalView.entries.length)],
        ['Win Rate', fmtPercent(Number(summary.win_rate || 0) * 100)],
        ['Realized PnL', `${{fmtNumber(summary.realized_pnl_eur, 2)}} EUR`],
        ['Largest Win', `${{fmtNumber(summary.largest_win_eur, 2)}} EUR`],
        ['Largest Loss', `${{fmtNumber(summary.largest_loss_eur, 2)}} EUR`],
        ['Active Strategies', fmtText(summary.active_strategies ?? 0)],
      ].map(([label, value]) => `<div class="mini-metric"><div class="label">${{escapeHtml(label)}}</div><div class="value">${{escapeHtml(value)}}</div></div>`).join(''));
      setHTML('personal-journal-pnl-chart', metricLineSvg(journalView.cumulativeSeries || [], '#f4b24f', 'rgba(244,178,79,0.14)'));
      setHTML('personal-journal-confidence-chart', metricLineSvg(journalView.confidenceSeries || [], '#62b8ff', 'rgba(98,184,255,0.14)'));
      setHTML('personal-journal-winloss-chart', labeledBarChartSvg((journal.charts && journal.charts.win_loss_series) || [], 'rgba(56,211,159,0.88)'));
      setHTML('personal-journal-asset-chart', labeledBarChartSvg(journal.asset_breakdown || [], 'rgba(98,184,255,0.88)'));
      setText('personal-journal-chart-meta', `${{journalView.entries.length}} filtered entries | asset ${{journalView.activeAsset}}`);
      setText('personal-journal-breakdown-meta', `strategies ${{(filterOptions.strategies || []).length}} | tags ${{(filterOptions.tags || []).length}}`);
      setHTML('personal-journal-entry-list', journalView.entries.slice(0, 16).map(entry => `
        <div class="list-item">
          <strong>${{escapeHtml(entry.title || entry.asset || entry.strategy || 'Journal Entry')}}</strong>
          <span>${{escapeHtml(entry.entry_ts || entry.ts || 'n/a')}} | ${{escapeHtml(entry.asset || 'n/a')}} | ${{escapeHtml(entry.strategy || 'n/a')}}</span>
          <span class="path">PnL ${{fmtNumber(entry.pnl_eur, 2)}} EUR | confidence ${{fmtPercent(Number(entry.confidence || 0) * 100)}} | source ${{escapeHtml(entry.source || 'manual')}}</span>
        </div>
      `).join('') || '<div class="list-item"><strong>No journal entries</strong><span>Wenn du spaeter manuelle Trades eintraegst, erscheinen sie hier mit PnL, Strategie und Lernnotizen.</span></div>');
      setHTML('personal-journal-learning-list', [
        ...(journal.strategy_notes || []),
        ...(journal.learning_points || []),
        ...(journal.beginner_notes || []),
      ].slice(0, 12).map(item => `
        <div class="list-item">
          <strong>${{escapeHtml(item.title || item.term || 'Learning')}}</strong>
          <span>${{escapeHtml(item.detail || item.note || item.simple || 'n/a')}}</span>
          <span class="path">${{escapeHtml(item.takeaway || item.reason || item.category || '')}}</span>
        </div>
      `).join('') || '<div class="list-item"><strong>No learnings yet</strong><span>Hier werden spaeter deine Strategie-Notizen und Learnings gebuendelt.</span></div>');
    }}
    function renderFastResearchSection(overview) {{
      const lab = overview.fast_research_lab || {{}};
      const labView = filteredFastResearchLab(lab);
      const summary = lab.summary || {{}};
      const filterOptions = lab.filter_options || {{}};
      setSelectOptions('fast-research-filter-family', filterOptions.families || [], labView.activeFamily, 'All families');
      setSelectOptions('fast-research-filter-status', filterOptions.statuses || [], labView.activeStatus, 'All statuses');
      setHTML('fast-research-summary-grid', [
        ['Strategies', fmtText(summary.strategies_seen ?? labView.strategies.length)],
        ['Eligible', fmtText(summary.eligible_strategies ?? 0)],
        ['Highest Score', fmtNumber(summary.highest_score, 2)],
        ['Best Expectancy', `${{fmtNumber(summary.best_expectancy_eur, 2)}} EUR`],
        ['Champion', fmtText(summary.champion_strategy_id)],
        ['Status', fmtText(summary.status)],
      ].map(([label, value]) => `<div class="mini-metric"><div class="label">${{escapeHtml(label)}}</div><div class="value">${{escapeHtml(value)}}</div></div>`).join(''));
      setHTML('fast-research-ranking-chart', labeledBarChartSvg(labView.strategies.map(row => ({{ label: row.label || row.strategy_id || 'n/a', value: Number(row.score || 0) }})), 'rgba(255,179,71,0.92)'));
      setHTML('fast-research-signals-chart', labeledBarChartSvg([
        {{ label: 'Observed', value: Number(lab.signals && lab.signals.observed || 0) }},
        {{ label: 'Paper', value: Number(lab.signals && lab.signals.paper_candidates || 0) }},
        {{ label: 'Rejects', value: Number(lab.signals && lab.signals.micro_rejections || 0) * -1 }},
      ], 'rgba(98,184,255,0.88)'));
      setHTML('fast-research-experiments-chart', labeledBarChartSvg((lab.experiments || []).map(row => ({{ label: row.label || row.name || row.strategy_id || 'n/a', value: Number(row.net_pnl_eur || row.score || 0) }})), 'rgba(56,211,159,0.88)'));
      setText('fast-research-ranking-meta', `${{labView.strategies.length}} strategies | family ${{labView.activeFamily}}`);
      setText('fast-research-signals-meta', `${{(lab.signals && lab.signals.observed) || 0}} observed signals | status ${{fmtText(summary.status)}}`);
      setHTML('fast-research-card-list', labView.strategies.slice(0, 12).map(row => `
        <div class="list-item">
          <strong>${{escapeHtml(row.label || row.strategy_id || 'n/a')}}</strong>
          <span>${{escapeHtml(row.family || 'n/a')}} | ${{escapeHtml(row.status || 'watch')}} | score ${{fmtNumber(row.score, 2)}}</span>
          <span class="path">PF ${{fmtNumber(row.profit_factor, 2)}} | WR ${{fmtPercent(Number(row.win_rate || 0) * 100)}} | expectancy ${{fmtNumber(row.expectancy_eur, 2)}} EUR</span>
        </div>
      `).join('') || '<div class="list-item"><strong>No fast-research rows</strong><span>Die Fast-Trading-Lane erscheint hier, sobald die Backend-Daten bereitgestellt werden.</span></div>');
    }}
    function renderJournalAlignmentSection(overview) {{
      const alignment = overview.journal_strategy_alignment || {{}};
      const summary = alignment.summary || {{}};
      const familyRows = alignment.family_alignment || [];
      const assetRows = alignment.asset_alignment || [];
      const guardrails = alignment.guardrails || [];
      const beginnerNotes = alignment.beginner_notes || [];
      setHTML('journal-alignment-summary-grid', [
        ['Manual entries', fmtText(summary.manual_entries ?? 0)],
        ['Matched families', fmtText(summary.matched_families ?? 0)],
        ['Asset overlap', fmtText(summary.overlapping_assets ?? 0)],
        ['Guardrails', fmtText(summary.guardrail_matches ?? 0)],
        ['Strongest family', fmtText(summary.strongest_family)],
        ['Recommended focus', fmtText(summary.recommended_focus)],
      ].map(([label, value]) => `<div class="mini-metric"><div class="label">${{escapeHtml(label)}}</div><div class="value">${{escapeHtml(value)}}</div></div>`).join(''));
      setText('journal-alignment-meta', `${{familyRows.length}} mapped families | strongest ${{fmtText(summary.strongest_family)}}`);
      setText('journal-alignment-asset-meta', `${{assetRows.length}} manual assets tracked`);
      setHTML('journal-alignment-family-body', familyRows.map(row => `<tr><td>${{escapeHtml(row.family)}}</td><td>${{fmtText(row.manual_trades)}}</td><td>${{fmtText(row.bot_strategies)}}</td><td>${{fmtText(row.eligible_strategies)}}</td><td>${{row.champion_present ? 'yes' : 'no'}}</td></tr>`).join('') || '<tr><td colspan="5">No family alignment data yet.</td></tr>');
      setHTML('journal-alignment-asset-body', assetRows.map(row => `<tr><td>${{escapeHtml(row.asset)}}</td><td>${{fmtText(row.manual_trades)}}</td><td>${{row.tracked_by_bot ? 'yes' : 'no'}}</td><td>${{row.fast_lane_seen ? 'yes' : 'no'}}</td></tr>`).join('') || '<tr><td colspan="4">No asset overlap data yet.</td></tr>');
      setHTML('journal-alignment-guardrails', guardrails.map(row => `<div class="list-item"><strong>${{escapeHtml(row.mistake)}}</strong><span>${{escapeHtml(row.guardrail)}}</span><span class="path">count ${{fmtText(row.count)}}</span></div>`).join('') || '<div class="list-item"><strong>No mapped mistakes yet</strong><span>Wenn du im Journal Fehler markierst, verknuepft das Cockpit sie hier mit Bot-Guardrails.</span></div>');
      setHTML('journal-alignment-beginner', beginnerNotes.map(row => `<div class="list-item"><strong>${{escapeHtml(row.term || 'Hint')}}</strong><span>${{escapeHtml(row.simple || row.detail || 'n/a')}}</span></div>`).join('') || '<div class="list-item"><strong>No beginner notes yet</strong><span>Die Journal-vs-Bot-Erklaerungen erscheinen hier, sobald Daten vorliegen.</span></div>');
    }}
    function renderShadowSection(overview) {{
      const shadow = overview.shadow_portfolios || {{}};
      const shadowView = filteredShadowData(shadow);
      setSelectOptions('shadow-filter-portfolio', shadowView.filterOptions.portfolios || [], shadowView.activePortfolio, 'All portfolios');
      setSelectOptions('shadow-filter-behavior', shadowView.filterOptions.behaviors || [], shadowView.activeBehavior, 'All behaviors');
      setSelectOptions('shadow-filter-regime', shadowView.filterOptions.regimes || [], shadowView.activeRegime, 'All regimes');
      setHTML('shadow-portfolio-grid', shadowView.portfolios.map(row => `<div class="mini-metric"><div class="label">${{escapeHtml(row.name)}}</div><div class="value">${{fmtNumber(row.ending_equity, 2)}} EUR</div><div class="neutral">${{escapeHtml(row.behavior_profile)}} | scope ${{escapeHtml(row.pair_scope)}} | net ${{fmtNumber(row.net_pnl_eur, 2)}} | wr ${{fmtPercent(Number(row.win_rate || 0) * 100)}} | dd ${{fmtPercent(Number(row.max_drawdown_pct || 0) * 100)}}</div></div>`).join('') || '<div class="list-item"><strong>No shadow data</strong><span>Die Shadow-Portfolios haben fuer diese Filter noch keine Test-Lane-Daten gesammelt.</span></div>');
      setHTML('shadow-equity-chart', multiSeriesLineSvg(shadowView.equityCurves || []));
      setHTML('shadow-behavior-body', (shadowView.behaviorRows || []).slice(0, 12).map(row => `<tr><td>${{escapeHtml(row.behavior_profile)}}</td><td>${{fmtNumber(row.net_pnl_eur, 2)}} EUR</td><td>${{fmtText(row.trades)}}</td><td>${{fmtPercent(Number(row.win_rate || 0) * 100)}}</td><td>${{fmtNumber(row.average_ending_equity, 2)}} EUR</td></tr>`).join('') || '<tr><td colspan="5">No behavior comparison data for this filter.</td></tr>');
      setHTML('shadow-regime-body', (shadowView.regimeRows || []).slice(0, 12).map(row => `<tr><td>${{escapeHtml(row.portfolio)}}</td><td>${{escapeHtml(row.regime_label)}}</td><td>${{fmtNumber(row.net_pnl_eur, 2)}} EUR</td><td>${{fmtText(row.trades)}}</td><td>${{fmtPercent(Number(row.win_rate || 0) * 100)}}</td></tr>`).join('') || '<tr><td colspan="5">No regime comparison data for this filter.</td></tr>');
      setHTML('shadow-setup-body', (shadowView.setupRows || []).slice(0, 12).map(row => `<tr><td>${{escapeHtml(row.portfolio)}}</td><td>${{escapeHtml(row.setup_type)}}</td><td>${{fmtNumber(row.net_pnl_eur, 2)}} EUR</td><td>${{fmtText(row.trades)}}</td><td>${{fmtPercent(Number(row.win_rate || 0) * 100)}}</td></tr>`).join('') || '<tr><td colspan="5">No setup comparison data for this filter.</td></tr>');
      setText('shadow-equity-meta', `${{shadowView.portfolios.length}} portfolios | behavior filter ${{shadowView.activeBehavior}} | regime filter ${{shadowView.activeRegime}}`);
      setText('shadow-regime-meta', `${{shadowView.behaviorRows.length}} behavior rows | ${{shadowView.regimeRows.length}} regime rows | ${{shadowView.setupRows.length}} setup rows`);
    }}
    function selectedMarketState(overview) {{
      const market = overview.market || {{}};
      const pairs = market.pairs || [];
      const availableSymbols = pairs.map(row => row.symbol).filter(Boolean);
      const selectedSymbol = availableSymbols.includes(dashboardState.filters.marketSymbol)
        ? dashboardState.filters.marketSymbol
        : (market.selected_symbol || availableSymbols[0] || 'n/a');
      const timeframeOptions = market.timeframe_options || [];
      const availableTimeframes = timeframeOptions.map(option => option.label);
      const selectedTimeframe = availableTimeframes.includes(dashboardState.filters.marketTimeframe)
        ? dashboardState.filters.marketTimeframe
        : (market.selected_timeframe || '1D');
      dashboardState.filters.marketSymbol = selectedSymbol;
      dashboardState.filters.marketTimeframe = selectedTimeframe;
      const selectedPair = pairs.find(row => row.symbol === selectedSymbol) || pairs[0] || null;
      const profileMap = selectedPair && selectedPair.timeframe_profiles ? selectedPair.timeframe_profiles : {{}};
      const selectedProfile = profileMap[selectedTimeframe] || profileMap['1D'] || null;
      return {{
        market,
        pairs,
        timeframeOptions,
        selectedSymbol,
        selectedTimeframe,
        selectedPair,
        selectedProfile,
      }};
    }}
    function renderMarketSidebar(marketState) {{
      const filteredPairs = filteredMarketPairs(marketState);
      const favorites = favoriteSymbolsSet();
      const quickFilters = marketQuickFilters();
      const searchInput = document.getElementById('market-search-input');
      if (searchInput) {{
        searchInput.value = dashboardState.filters.marketSearch || '';
      }}
      const favoritesToggle = document.getElementById('market-favorites-toggle');
      if (favoritesToggle) {{
        const active = dashboardState.filters.marketFavoritesOnly === 'true';
        favoritesToggle.classList.toggle('active', active);
        favoritesToggle.textContent = active ? 'Favorites only: on' : 'Favorites only: off';
      }}
      setHTML('market-quick-filter-bar', quickFilters.map(filter => `<button type="button" class="quick-filter-btn ${{(dashboardState.filters.marketQuickFilter || 'all') === filter.key ? 'active' : ''}}" data-market-quick-filter="${{escapeHtml(filter.key)}}">${{escapeHtml(filter.label)}}</button>`).join(''));
      setText('market-sidebar-meta', `${{filteredPairs.length}} / ${{marketState.pairs.length}} assets visible | ${{favorites.size}} favorites saved`);
      setHTML('market-sidebar-list', filteredPairs.map(row => {{
        const selected = row.symbol === marketState.selectedSymbol;
        const favorite = favorites.has(String(row.symbol || '').toUpperCase());
        const profile = profileForTimeframe(row, marketState.selectedTimeframe);
        const changePct = Number(profile.change_pct ?? row.change_1h_pct ?? 0);
        const rangePct = Number(profile.range_pct ?? row.range_24h_pct ?? 0);
        const coveragePct = Number(profile.coverage_pct ?? 0) * 100;
        const spreadBps = Number(row.spread_bps ?? 0);
        const historyBadgeClass = coveragePct >= 80 ? 'good' : coveragePct >= 50 ? 'warn' : 'bad';
        return `<div class="asset-sidebar-item ${{selected ? 'selected' : ''}}" data-market-symbol="${{escapeHtml(row.symbol)}}"><div class="asset-sidebar-main"><div class="asset-sidebar-head"><div class="asset-sidebar-symbol">${{escapeHtml(row.symbol)}}</div><div class="asset-sidebar-price">${{fmtPrice(row.price)}}</div></div><div class="asset-sidebar-meta"><span class="${{changeClass(changePct)}}">${{fmtPercent(changePct)}}</span><span>spread ${{fmtNumber(spreadBps, 2)}} bps</span><span>volume ${{fmtCompact(profile.volume ?? row.volume_24h ?? 0)}}</span></div><div class="asset-sidebar-badges"><span class="asset-badge ${{changePct >= 0 ? 'good' : 'bad'}}">${{escapeHtml(marketState.selectedTimeframe)}} ${{fmtPercent(changePct)}}</span><span class="asset-badge warn">range ${{fmtPercent(rangePct)}}</span><span class="asset-badge ${{historyBadgeClass}}">coverage ${{fmtPercent(coveragePct)}}</span></div></div><button type="button" class="asset-favorite-btn ${{favorite ? 'active' : ''}}" data-favorite-symbol="${{escapeHtml(row.symbol)}}" aria-label="Toggle favorite">${{favorite ? '★' : '☆'}}</button></div>`;
      }}).join('') || '<div class="list-item"><strong>No assets match the current filter</strong><span>Leere Suche oder weniger strenge Quick-Filter zeigen wieder mehr Paare an.</span></div>');
    }}
    function renderMarketExplorer(overview) {{
      const marketState = selectedMarketState(overview);
      const {{ market, pairs, timeframeOptions, selectedSymbol, selectedTimeframe, selectedPair, selectedProfile }} = marketState;
      const filteredPairs = filteredMarketPairs(marketState);
      const optionsHtml = pairs.map(row => `<option value="${{escapeHtml(row.symbol)}}">${{escapeHtml(row.symbol)}}</option>`).join('');
      setText('market-explorer-title', selectedSymbol || 'n/a');
      setHTML('market-asset-select', optionsHtml || '<option value="n/a">No assets</option>');
      const assetSelect = document.getElementById('market-asset-select');
      if (assetSelect) {{
        assetSelect.value = selectedSymbol;
      }}
      renderMarketSidebar(marketState);
      setHTML('market-timeframe-strip', timeframeOptions.map(option => `<button type="button" class="timeframe-btn ${{option.label === selectedTimeframe ? 'active' : ''}}" data-market-timeframe="${{escapeHtml(option.label)}}">${{escapeHtml(option.label)}}</button>`).join(''));
      const selectedChange = selectedProfile ? Number(selectedProfile.change_pct || 0) : Number(selectedPair && selectedPair.change_1h_pct || 0);
      const selectedRange = selectedProfile ? Number(selectedProfile.range_pct || 0) : Number(selectedPair && selectedPair.range_24h_pct || 0);
      const selectedCoverage = selectedProfile ? Number(selectedProfile.coverage_pct || 0) * 100 : 0;
      const selectedTrend = selectedProfile ? Number(selectedProfile.trend_per_hour || 0) : 0;
      const selectedFreshness = selectedProfile ? Number(selectedProfile.freshness_seconds || 0) : Number(selectedPair && selectedPair.freshness_seconds || 0);
      const selectedVolume = selectedProfile ? Number(selectedProfile.volume || 0) : Number(selectedPair && selectedPair.volume_24h || 0);
      const selectedSource = selectedPair ? fmtText(selectedPair.live_source) : 'n/a';
      setHTML('market-explorer-summary-grid', [
        ['Live Price', selectedPair ? fmtPrice(selectedPair.price) : 'n/a'],
        ['Selected Change', fmtPercent(selectedChange)],
        ['Range', fmtPercent(selectedRange)],
        ['Coverage', fmtPercent(selectedCoverage)],
        ['Trend / h', `${{fmtNumber(selectedTrend, 2)}}%`],
        ['Volume', fmtCompact(selectedVolume)],
      ].map(([label, value]) => `<div class="mini-metric"><div class="label">${{escapeHtml(label)}}</div><div class="value">${{escapeHtml(value)}}</div></div>`).join(''));
      setText('market-explorer-chart-meta', `${{selectedTimeframe}} | source ${{selectedSource}} | freshness ${{fmtNumber(selectedFreshness, 0)}}s`);
      const stroke = selectedChange >= 0 ? '#38d39f' : '#ff6d6d';
      setHTML('market-explorer-chart', detailedSeriesSvg(selectedProfile && selectedProfile.series ? selectedProfile.series : [], stroke, `${{selectedSymbol}}-${{selectedTimeframe}}`));
      const rows = filteredPairs.map(row => {{
        const profileMap = row.timeframe_profiles || {{}};
        const profile = profileMap[selectedTimeframe] || profileMap['1D'] || {{}};
        const rowChange = Number(profile.change_pct ?? row.change_1h_pct ?? 0);
        const rowRange = Number(profile.range_pct ?? row.range_24h_pct ?? 0);
        const rowCoverage = Number(profile.coverage_pct ?? 0) * 100;
        const rowVolume = Number(profile.volume ?? row.volume_24h ?? 0);
        const selected = row.symbol === selectedSymbol;
        return `<tr class="clickable ${{selected ? 'selected' : ''}}" data-market-symbol="${{escapeHtml(row.symbol)}}"><td><strong>${{escapeHtml(row.symbol)}}</strong><div class="neutral">${{escapeHtml(fmtText(row.live_source))}}</div></td><td>${{fmtPrice(row.price)}}</td><td class="${{changeClass(rowChange)}}">${{fmtPercent(rowChange)}}</td><td>${{fmtPercent(rowRange)}}</td><td>${{fmtPercent(rowCoverage)}}</td><td>${{fmtCompact(rowVolume)}}</td></tr>`;
      }}).join('');
      setHTML('market-explorer-table', rows || '<tr><td colspan="6">No market rows match the current sidebar filters.</td></tr>');
      setText('market-breadth-meta', `${{(market.breadth_rows || []).length}} windows | selected ${{selectedTimeframe}}`);
      setHTML('market-breadth-chart', labeledBarChartSvg((market.breadth_rows || []).map(row => ({{ label: row.label, value: row.avg_change_pct }})), 'rgba(98,184,255,0.88)'));
      const selectedBreadth = (market.breadth_rows || []).find(row => row.label === selectedTimeframe) || null;
      setText('market-window-meta', selectedBreadth ? `up ${{fmtText(selectedBreadth.positive)}} | down ${{fmtText(selectedBreadth.negative)}} | flat ${{fmtText(selectedBreadth.neutral)}}` : 'n/a');
      setHTML('market-window-body', (market.breadth_rows || []).map(row => `<tr class="clickable ${{row.label === selectedTimeframe ? 'selected' : ''}}" data-market-timeframe="${{escapeHtml(row.label)}}"><td><strong>${{escapeHtml(row.label)}}</strong></td><td>${{escapeHtml(fmtText(row.best_symbol))}} ${{fmtPercent(Number(row.best_change_pct || 0))}}</td><td>${{escapeHtml(fmtText(row.worst_symbol))}} ${{fmtPercent(Number(row.worst_change_pct || 0))}}</td><td>${{fmtPercent(Number(row.avg_change_pct || 0))}}</td><td>${{fmtPercent(Number(row.avg_coverage_pct || 0) * 100)}}</td></tr>`).join('') || '<tr><td colspan="5">No horizon rows available.</td></tr>');
    }}
    function renderStrategyLabSection(overview) {{
      const lab = overview.strategy_lab || {{}};
      const summary = lab.summary || {{}};
      const rows = [...(lab.strategies || [])];
      setHTML('strategy-lab-summary-grid', [
        ['Paper Champion', lab.current_paper_strategy_id || 'n/a'],
        ['Live Champion', lab.current_live_strategy_id || 'n/a'],
        ['Eligible Challengers', String(summary.eligible_count || 0)],
        ['Regime Ready', String(summary.regime_ready_count || 0)],
        ['Asset Ready', String(summary.asset_ready_count || 0)],
        ['Cooldown Until', lab.paper_promotion_cooldown_until ? fmtDateTime(lab.paper_promotion_cooldown_until) : 'n/a'],
        ['Promotion', lab.promotion_reason || 'n/a'],
      ].map(([label, value]) => `<div class="mini-metric"><div class="label">${{escapeHtml(label)}}</div><div class="value">${{escapeHtml(value)}}</div></div>`).join(''));
      setHTML('strategy-lab-score-chart', labeledBarChartSvg(lab.ranked_scores || [], 'rgba(255, 179, 71, 0.92)'));
      setText('strategy-lab-meta', `${{rows.length}} strategies | current champion ${{lab.current_paper_strategy_id || 'n/a'}}`);
      setText('strategy-lab-gate-meta', `paper promoted = ${{fmtText(lab.paper_promotion_applied)}} | rollback = ${{fmtText(lab.rollback_applied)}} | live promoted = ${{fmtText(lab.live_promotion_applied)}}`);
      setHTML('strategy-lab-body', rows.map(row => `<tr><td>${{escapeHtml(row.label || row.strategy_id || 'n/a')}}</td><td>${{escapeHtml(row.family || 'n/a')}}</td><td>${{fmtText(row.closed_trades)}}</td><td>${{row.profit_factor === 'inf' ? 'inf' : fmtNumber(row.profit_factor, 2)}}</td><td>${{fmtPercent(Number(row.win_rate || 0) * 100)}}</td><td><span class="chip ${{row.eligible_for_promotion ? 'good' : 'warn'}}">${{row.eligible_for_promotion ? 'eligible' : 'hold'}}</span></td></tr>`).join('') || '<tr><td colspan="6">Noch keine Strategy-Lab-Daten vorhanden.</td></tr>');
      const regimeRows = rows.map(row => {{
        const gates = row.gates || {{}};
        const failed = Object.values(gates).filter(gate => gate && gate.passed === false).map(gate => gate.name);
        const regimeGatePassed = ['distinct_regimes', 'regime_trade_depth', 'regime_concentration'].every(key => !gates[key] || gates[key].passed === true);
        const distinctRegimes = row.distinct_regimes ?? ((row.regime_breakdown || []).length);
        const dominantShare = Number(row.dominant_regime_share || 0);
        return `<tr><td>${{escapeHtml(row.label || row.strategy_id || 'n/a')}}</td><td>${{fmtText(distinctRegimes)}}</td><td>${{fmtPercent(dominantShare * 100)}}</td><td><span class="chip ${{regimeGatePassed ? 'good' : 'warn'}}">${{regimeGatePassed ? 'stable' : 'thin'}}</span></td><td>${{escapeHtml(failed.join(', ') || 'none')}}</td></tr>`;
      }});
      setText('strategy-lab-regime-meta', `${{rows.length}} strategies | distinct regime and concentration gates`);
      setHTML('strategy-lab-regime-body', regimeRows.join('') || '<tr><td colspan="5">Noch keine Regime-Gates sichtbar.</td></tr>');
      const assetRows = rows.map(row => {{
        const gates = row.gates || {{}};
        const failed = ['distinct_assets', 'asset_trade_depth', 'asset_concentration']
          .map(key => gates[key])
          .filter(gate => gate && gate.passed === false)
          .map(gate => gate.name);
        const assetGatePassed = ['distinct_assets', 'asset_trade_depth', 'asset_concentration'].every(key => !gates[key] || gates[key].passed === true);
        const distinctAssets = row.distinct_assets ?? ((row.asset_breakdown || []).length);
        const dominantShare = Number(row.dominant_asset_share || 0);
        return `<tr><td>${{escapeHtml(row.label || row.strategy_id || 'n/a')}}</td><td>${{fmtText(distinctAssets)}}</td><td>${{fmtPercent(dominantShare * 100)}}</td><td><span class="chip ${{assetGatePassed ? 'good' : 'warn'}}">${{assetGatePassed ? 'broad' : 'thin'}}</span></td><td>${{escapeHtml(failed.join(', ') || 'none')}}</td></tr>`;
      }});
      setText('strategy-lab-asset-meta', `${{rows.length}} strategies | distinct asset and concentration gates`);
      setHTML('strategy-lab-asset-body', assetRows.join('') || '<tr><td colspan="5">Noch keine Asset-Gates sichtbar.</td></tr>');
    }}
    function renderTradeDetail(overview) {{
      const tradeAnalytics = overview.trade_analytics || {{}};
      const analytics = overview.analytics || {{}};
      const options = tradeAnalytics.filter_options || {{}};
      setSelectOptions('trade-filter-pair', options.pairs || [], dashboardState.filters.pair, 'All pairs');
      setSelectOptions('trade-filter-setup', options.setups || [], dashboardState.filters.setup, 'All setups');
      setSelectOptions('trade-filter-quality', options.qualities || [], dashboardState.filters.quality, 'All qualities');
      setSelectOptions('trade-filter-reason', options.reasons || [], dashboardState.filters.reason, 'All exit reasons');
      setSelectOptions('trade-filter-limit', (options.limits || [12]).map(String), dashboardState.filters.limit, 'Trade window');
      document.querySelectorAll('#trade-timeframe-controls .seg-btn').forEach(node => node.classList.toggle('active', (node.dataset.range || 'all') === dashboardState.filters.range));
      const trades = filteredTrades(tradeAnalytics);
      if (dashboardState.selectedTradeKey && !trades.some(trade => trade.trade_key === dashboardState.selectedTradeKey)) {{
        dashboardState.selectedTradeKey = null;
      }}
      const exitBreakdown = countByLabel(trades, 'reason');
      const qualityBreakdown = countByLabel(trades, 'quality');
      const pairBreakdown = pairPerformanceFromTrades(trades);
      const setupBreakdown = countByLabel(trades, 'setup_type');
      const dailyBreakdown = dailyPnlFromTrades(trades);
      const filteredMarkers = buildFilteredMarkers(analytics.equity_curve || [], trades);
      const filteredPnlMarkers = buildFilteredMarkers(analytics.pnl_curve || [], trades);
      const totalPnl = trades.reduce((sum, trade) => sum + Number(trade.pnl_eur || 0), 0);
      const winCount = trades.filter(trade => Number(trade.pnl_eur || 0) > 0).length;
      const lossCount = trades.filter(trade => Number(trade.pnl_eur || 0) < 0).length;
      const avgHold = trades.length ? trades.reduce((sum, trade) => sum + Number(trade.hold_minutes || 0), 0) / trades.length : 0;
      const bestTrade = trades.length ? Math.max(...trades.map(trade => Number(trade.pnl_eur || 0))) : 0;
      const worstTrade = trades.length ? Math.min(...trades.map(trade => Number(trade.pnl_eur || 0))) : 0;
      const avgMae = trades.length ? trades.reduce((sum, trade) => sum + Number(trade.mae_r || 0), 0) / trades.length : 0;
      const avgMfe = trades.length ? trades.reduce((sum, trade) => sum + Number(trade.mfe_r || 0), 0) / trades.length : 0;
      const avgFees = trades.length ? trades.reduce((sum, trade) => sum + Number(trade.total_fee_eur || 0), 0) / trades.length : 0;
      const avgSlippage = trades.length ? trades.reduce((sum, trade) => sum + Number(trade.total_slippage_bps || 0), 0) / trades.length : 0;
      const winRate = trades.length ? (winCount / trades.length) * 100 : 0;
      setHTML('equity-chart', metricLineSvg(analytics.equity_curve || [], '#38d39f', 'rgba(56,211,159,0.14)', filteredMarkers));
      setHTML('pnl-chart', metricLineSvg(analytics.pnl_curve || [], '#62b8ff', 'rgba(98,184,255,0.14)', filteredPnlMarkers));
      setHTML('daily-pnl-chart', labeledBarChartSvg(dailyBreakdown, 'rgba(244,178,79,0.88)'));
      setHTML('exit-reason-chart', labeledBarChartSvg(exitBreakdown, 'rgba(255,255,255,0.75)'));
      setHTML('pair-performance-chart', labeledBarChartSvg(pairBreakdown, 'rgba(98,184,255,0.88)'));
      setHTML('quality-breakdown-chart', labeledBarChartSvg(setupBreakdown.length ? setupBreakdown : qualityBreakdown, 'rgba(56,211,159,0.88)'));
      setHTML('mae-mfe-chart', maeMfeScatterSvg(trades));
      setText('exit-reason-meta', `${{trades.length}} filtered trades | win rate ${{fmtPercent(winRate)}}`);
      setText('pair-performance-meta', `filtered pnl = ${{fmtNumber(totalPnl, 2)}} EUR | wins ${{winCount}} / losses ${{lossCount}} | setups ${{setupBreakdown.length}}`);
      setText('mae-mfe-meta', `avg MAE ${{fmtNumber(avgMae, 2)}}R | avg MFE ${{fmtNumber(avgMfe, 2)}}R | avg fees ${{fmtNumber(avgFees, 2)}} EUR`);
      setHTML('journal-kpis', [
        ['Filtered Trades', String(trades.length)],
        ['Win Rate', fmtPercent(winRate)],
        ['Avg Hold', `${{fmtNumber(avgHold, 2)}} min`],
        ['Best / Worst', `${{fmtNumber(bestTrade, 2)}} / ${{fmtNumber(worstTrade, 2)}} EUR`],
        ['Avg MAE / MFE', `${{fmtNumber(avgMae, 2)}}R / ${{fmtNumber(avgMfe, 2)}}R`],
        ['Avg Fee / Slip', `${{fmtNumber(avgFees, 2)}} EUR / ${{fmtNumber(avgSlippage, 2)}} bps`],
      ].map(([label, value]) => `<div class="mini-metric"><div class="label">${{escapeHtml(label)}}</div><div class="value">${{escapeHtml(value)}}</div></div>`).join(''));
      setHTML('recent-trades-list', trades.map(trade => {{
        const selected = trade.trade_key === dashboardState.selectedTradeKey;
        return `<div class="list-item trade-list-item ${{selected ? 'selected' : ''}}" data-trade-key="${{escapeHtml(trade.trade_key || '')}}"><strong>${{escapeHtml(trade.pair)}} | ${{escapeHtml(trade.setup_type || 'setup')}} | ${{escapeHtml(trade.reason)}}</strong><span>quality ${{escapeHtml(trade.quality)}} | score ${{fmtNumber(trade.score, 2)}} | pnl ${{fmtNumber(trade.pnl_eur, 2)}} EUR | hold ${{fmtNumber(trade.hold_minutes, 2)}} min</span><span class="path">${{escapeHtml(trade.reason_code)}}</span></div>`;
      }}).join('') || '<div class="list-item"><strong>No filtered trades</strong><span>Die aktuellen Filter liefern keine abgeschlossenen Trades.</span></div>');
      document.querySelectorAll('.trade-list-item').forEach(node => {{
        node.addEventListener('click', () => {{
          dashboardState.selectedTradeKey = node.dataset.tradeKey || null;
          renderTradeDetail(overview);
        }});
      }});
      const selectedTrade = dashboardState.selectedTradeKey ? findTradeByKey(tradeAnalytics, dashboardState.selectedTradeKey) : null;
      const focusedTrade = selectedTrade || (((dashboardState.filters.pair && dashboardState.filters.pair !== 'all') || (dashboardState.filters.setup && dashboardState.filters.setup !== 'all')) ? (trades[0] || null) : null);
      setText('trade-selection-note', focusedTrade ? `Trade-Fokus: ${{focusedTrade.pair}} | ${{focusedTrade.setup_type || 'setup'}} | ${{focusedTrade.reason}} | pnl ${{fmtNumber(focusedTrade.pnl_eur, 2)}} EUR` : 'Kein Trade ausgewaehlt. Klick auf einen Marker oder Journal-Eintrag fuer Details.');
      setText('selected-trade-title', focusedTrade ? `${{focusedTrade.pair}} | ${{focusedTrade.setup_type || 'setup'}} | ${{focusedTrade.reason}}` : 'Noch kein Trade ausgewaehlt');
      const selectedPnl = focusedTrade ? Number(focusedTrade.pnl_eur || 0) : 0;
      setText('trade-replay-meta', focusedTrade ? `Setup ${{fmtText(focusedTrade.setup_type)}} | MAE ${{fmtNumber(focusedTrade.mae_r, 2)}}R | MFE ${{fmtNumber(focusedTrade.mfe_r, 2)}}R | fee ${{fmtNumber(focusedTrade.total_fee_eur, 2)}} EUR` : 'Waehle einen Trade fuer Replay und Exkursionsprofil');
      setHTML('trade-replay-chart', replayLineSvg(focusedTrade && focusedTrade.replay_points ? focusedTrade.replay_points : []));
      const selectedChip = document.getElementById('selected-trade-chip');
      if (selectedChip) {{
        selectedChip.className = `chip ${{focusedTrade ? (selectedPnl >= 0 ? 'good' : 'bad') : 'warn'}}`;
        selectedChip.textContent = focusedTrade ? (selectedPnl >= 0 ? 'winning trade' : 'losing trade') : 'waiting';
      }}
      setHTML('selected-trade-grid', focusedTrade ? [
        ['Pair', focusedTrade.pair],
        ['Setup', focusedTrade.setup_type || 'n/a'],
        ['Regime', focusedTrade.regime_label || 'n/a'],
        ['Quality', focusedTrade.quality],
        ['Score', fmtNumber(focusedTrade.score, 2)],
        ['PnL', `${{fmtNumber(focusedTrade.pnl_eur, 2)}} EUR`],
        ['Hold', `${{fmtNumber(focusedTrade.hold_minutes, 2)}} min`],
        ['Budget', `${{fmtNumber(focusedTrade.budget_eur, 2)}} EUR`],
        ['MAE / MFE', `${{fmtNumber(focusedTrade.mae_r, 2)}}R / ${{fmtNumber(focusedTrade.mfe_r, 2)}}R`],
        ['Fee / Slip', `${{fmtNumber(focusedTrade.total_fee_eur, 2)}} EUR / ${{fmtNumber(focusedTrade.total_slippage_bps, 2)}} bps`],
        ['Entry', fmtDateTime(focusedTrade.entry_ts)],
        ['Exit', fmtDateTime(focusedTrade.exit_ts)],
      ].map(([label, value]) => `<div class="mini-metric"><div class="label">${{escapeHtml(label)}}</div><div class="value">${{escapeHtml(value)}}</div></div>`).join('') : '<div class="mini-metric"><div class="label">Selection</div><div class="value">No trade selected</div></div>');
      setHTML('selected-trade-notes', focusedTrade ? [
        `<div class="list-item"><strong>Exit interpretation</strong><span>${{escapeHtml(summarizeReason(focusedTrade.reason))}}</span></div>`,
        `<div class="list-item"><strong>Beginner view</strong><span>${{escapeHtml(selectedPnl >= 0 ? 'Dieser Trade hat Geld verdient. Schau auf Pair, Setup und Exit-Grund, um Muster guter Trades zu finden.' : 'Dieser Trade hat Geld verloren. Das ist nur dann problematisch, wenn dieselben Muster haeufig wiederkommen.' )}}</span></div>`,
        `<div class="list-item"><strong>Execution profile</strong><span>Entry ${{escapeHtml(focusedTrade.entry_liquidity_role || 'n/a')}} bei ${{fmtNumber(focusedTrade.entry_slippage_bps, 2)}} bps Slippage. Exit ${{escapeHtml(focusedTrade.exit_liquidity_role || 'n/a')}} bei ${{fmtNumber(focusedTrade.exit_slippage_bps, 2)}} bps.</span></div>`,
        `<div class="list-item"><strong>Replay focus</strong><span>Aktueller Drilldown basiert auf Pair ${{escapeHtml(focusedTrade.pair)}} und Setup ${{escapeHtml(focusedTrade.setup_type || 'n/a')}}. Mit den Filtern oben kannst du das Replay pro Asset und Setup eingrenzen.</span></div>`,
        `<div class="list-item"><strong>Reason code</strong><span class="path">${{escapeHtml(focusedTrade.reason_code || 'n/a')}}</span></div>`,
      ].join('') : '<div class="list-item"><strong>Selection guide</strong><span>Waehle einen Marker oder einen Journal-Eintrag aus, um den einzelnen Trade zu erklaeren und im Kontext der Charts zu sehen.</span></div>');
    }}
    function renderOverview(overview) {{
      dashboardState.overview = overview;
      bindFilterControls();
      const app = overview.app || {{}};
      const monitor = overview.monitor || {{}};
      const summary = monitor.daily_summary || {{}};
      const history = overview.history_status || {{}};
      const progress = monitor.history_progress || history || {{}};
      const task = overview.task || {{}};
      const lastCycle = overview.last_cycle || {{}};
      const market = overview.market || {{}};
      const launch = overview.launch || {{}};
      const forward = overview.forward_report || {{}};
      const analytics = overview.analytics || {{}};
      const readiness = Number(progress.progress_pct || 0);
      setText('hero-tagline', app.tagline || '');
      setText('readiness-value', fmtPercent(readiness));
      setText('available-days', `${{fmtNumber(progress.available_days, 3)}} / ${{fmtText(progress.required_days)}} days`);
      setText('eta-value', fmtDateTime(summary.eta || progress.estimated_ready_at));
      setText('refreshed-at', fmtDateTime(app.refreshed_at));
      const ring = document.getElementById('readiness-ring');
      if (ring) ring.style.background = `radial-gradient(circle closest-side, rgba(10,14,20,0.96) 76%, transparent 77% 100%), conic-gradient(var(--green) ${{Math.max(0, Math.min(readiness, 100)) * 3.6}}deg, rgba(255,255,255,0.08) 0deg 360deg)`;
      setText('supervisor-status', fmtText(monitor.status));
      setText('supervisor-meta', `PID ${{fmtText(monitor.supervisor && monitor.supervisor.pid)}} | updated ${{fmtDateTime(monitor.updated_at)}}`);
      setText('gate-status', fmtText(summary.gate_status));
      setText('gate-meta', `ready = ${{fmtText(summary.gate_ready)}} | blockers = ${{(summary.gate_blockers || []).length}}`);
      setText('paper-status', fmtText(summary.paper_forward_status));
      setText('paper-meta', `pid = ${{fmtText(monitor.paper_forward && monitor.paper_forward.pid)}}`);
      setText('task-status', fmtText(task.status));
      setText('task-meta', `last result = ${{fmtText(task.last_result)}} | last run = ${{fmtDateTime(task.last_run)}}`);
      setText('speed-value', progress.avg_growth_days_per_hour ? `${{Number(progress.avg_growth_days_per_hour).toFixed(4)}} d/h` : 'n/a');
      setText('speed-meta', `cycles = ${{fmtText(progress.cycles_observed)}} | est cycles left = ${{fmtNumber(progress.estimated_cycles_to_ready, 1)}}`);
      setText('cycle-value', lastCycle.available ? `#${{fmtText(lastCycle.cycle)}}` : 'n/a');
      setText('cycle-meta', lastCycle.available ? `intervals tracked = ${{Object.keys(lastCycle.interval_totals || {{}}).length}}` : 'no capture cycle yet');
      const pills = [
        {{ label: `Supervisor | ${{fmtText(monitor.status)}}`, klass: pillClassFromText(monitor.status) }},
        {{ label: `Gate | ${{fmtText(summary.gate_status)}}`, klass: pillClassFromText(summary.gate_status) }},
        {{ label: `Task | ${{fmtText(task.status)}}`, klass: pillClassFromText(task.status) }},
        {{ label: `Paper | ${{fmtText(summary.paper_forward_status)}}`, klass: pillClassFromText(summary.paper_forward_status) }},
        {{ label: `Research | ${{fmtText(summary.research_scan_status)}}`, klass: pillClassFromText(summary.research_scan_status) }},
      ];
      setHTML('status-pills', pills.map(pill => `<span class="pill ${{pill.klass}}">${{pill.label}}</span>`).join(''));
      const marketPairs = market.pairs || [];
      const selectedMarketSymbol = marketPairs.some(row => row.symbol === dashboardState.filters.marketSymbol)
        ? dashboardState.filters.marketSymbol
        : (market.selected_symbol || marketPairs[0] && marketPairs[0].symbol || 'n/a');
      setHTML('market-grid', marketPairs.map(row => {{
        const windowBadges = (row.window_badges || []).map(badge => `<span class="window-chip ${{marketWindowClass(badge.change_pct)}}">${{escapeHtml(badge.label)}} <span class="neutral">${{fmtPercent(Number(badge.change_pct || 0))}}</span></span>`).join('');
        const leaderChip = market.leader_symbol === row.symbol ? '<span class="chip blue">Momentum leader</span>' : '';
        const sourceClass = row.live_source === 'kraken_rest' ? 'good' : 'warn';
        const selected = row.symbol === selectedMarketSymbol;
        return `<article class="market-card clickable ${{selected ? 'selected' : ''}}" data-market-symbol="${{escapeHtml(row.symbol)}}"><div class="market-head"><div><div class="market-symbol">${{escapeHtml(row.symbol)}}</div><div class="neutral"><span class="inline-dot ${{sourceClass}}"></span>${{escapeHtml(row.live_source || 'n/a')}} | candle age ${{fmtNumber(row.freshness_seconds || 0, 0)}}s</div></div>${{leaderChip}}${{selected ? '<span class="chip blue">selected</span>' : ''}}</div><div><div class="market-price">${{fmtPrice(row.price)}}</div><div class="${{changeClass(row.change_1h_pct)}}">1h ${{fmtPercent(row.change_1h_pct)}} | 24h ${{fmtPercent(row.change_24h_pct)}}</div></div><div class="sparkline">${{sparklineSvg(row.sparkline || [], (Number(row.change_1h_pct || 0) >= 0 ? '#38d39f' : '#ff6d6d'))}}</div><div class="market-mini-grid"><div class="mini-metric"><div class="label">Bid / Ask</div><div class="value">${{fmtPrice(row.bid)}} / ${{fmtPrice(row.ask)}}</div></div><div class="mini-metric"><div class="label">Spread</div><div class="value">${{fmtNumber(row.spread_bps, 2)}} bps</div></div><div class="mini-metric"><div class="label">24h range</div><div class="value">${{fmtPercent(row.range_24h_pct)}}</div></div><div class="mini-metric"><div class="label">24h volume</div><div class="value">${{fmtCompact(row.volume_24h)}}</div></div></div><div class="window-strip">${{windowBadges}}</div></article>`;
      }}).join('') || '<div class="list-item"><strong>No live market data</strong><span>Kraken REST snapshots and local candles are currently unavailable.</span></div>');
      const marketSummary = [];
      if (market.leader_symbol) marketSummary.push(`<span class="legend-key" style="color: var(--green)">leader ${{escapeHtml(market.leader_symbol)}}</span>`);
      if (market.tightest_spread_symbol) marketSummary.push(`<span class="legend-key" style="color: #8cc9ff">tightest spread ${{escapeHtml(market.tightest_spread_symbol)}}</span>`);
      marketSummary.push(`<span class="legend-key" style="color: var(--amber)">updated ${{fmtDateTime(market.updated_at)}}</span>`);
      setHTML('market-summary', marketSummary.join(''));
      renderMarketExplorer(overview);
      setText('launch-current-phase', fmtText(launch.current_phase));
      setText('launch-next-action', fmtText(launch.next_action));
      setHTML('launch-track', (launch.phases || []).map(phase => `<article class="phase-card"><div style="display:flex; justify-content:space-between; gap:10px; align-items:flex-start;"><div class="phase-title">${{escapeHtml(phase.label || 'n/a')}}</div><span class="chip ${{pillClassFromText(phase.status)}}">${{escapeHtml(phase.status || 'pending')}}</span></div><div class="phase-headline">${{escapeHtml(phase.headline || '')}}</div><div class="phase-detail">${{escapeHtml(phase.detail || '')}}</div><div class="phase-progress"><span style="width:${{Math.max(0, Math.min(Number(phase.completion_pct || 0), 100))}}%"></span></div></article>`).join(''));
      const progressSeries = analytics.progress_series || [];
      setHTML('progress-chart', lineChartSvg(progressSeries));
      setText('progress-chart-meta', progressSeries.length ? `${{progressSeries.length}} runs tracked` : 'no run history');
      const syncTotals = analytics.sync_totals || [];
      setHTML('sync-chart', barChartSvg(syncTotals));
      setText('sync-chart-meta', syncTotals.length ? `${{syncTotals.length}} intervals in latest cycle` : 'no latest cycle');
      const forwardMetrics = [
        ['Telemetry', forward.source_exists ? 'present' : 'missing'],
        ['Closed trades', fmtText(forward.closed_trades)],
        ['Win rate', fmtPercent((Number(forward.win_rate || 0)) * 100)],
        ['Profit factor', forward.profit_factor === 'inf' ? 'inf' : fmtNumber(forward.profit_factor, 2)],
        ['Max DD', fmtPercent((Number(forward.max_drawdown_pct || 0)) * 100)],
        ['Net PnL', `${{fmtNumber(forward.net_pnl_eur, 2)}} EUR`],
      ];
      setHTML('forward-summary-grid', forwardMetrics.map(([label, value]) => `<div class="mini-metric"><div class="label">${{escapeHtml(label)}}</div><div class="value">${{escapeHtml(value)}}</div></div>`).join(''));
      const forwardGates = analytics.forward_gates || [];
      setHTML('forward-gate-list', forwardGates.map(gate => `<div class="gate-item"><div><strong>${{escapeHtml(gate.name)}}</strong><div class="neutral">actual ${{escapeHtml(fmtText(gate.actual))}} | threshold ${{escapeHtml(fmtText(gate.threshold))}}</div></div><span class="chip ${{gate.passed ? 'good' : 'bad'}}">${{gate.passed ? 'pass' : 'fail'}}</span></div>`).join('') || '<div class="list-item"><strong>No forward gates yet</strong><span>Forward telemetry has not produced launch gate metrics.</span></div>');
      const observatory = overview.signal_observatory || {{}};
      const observedSignals = Number(observatory.observed_signals || 0);
      const tradableSignals = Number(observatory.tradable_signals || 0);
      const decisionRejections = Number(observatory.decision_rejections || 0);
      setHTML('signal-summary-grid', [
        ['Observed', String(observedSignals)],
        ['Tradable', String(tradableSignals)],
        ['Tradable Rate', fmtPercent((Number(observatory.tradable_rate || 0)) * 100)],
        ['Decision Rejects', String(decisionRejections)],
      ].map(([label, value]) => `<div class="mini-metric"><div class="label">${{escapeHtml(label)}}</div><div class="value">${{escapeHtml(value)}}</div></div>`).join(''));
      setHTML('signal-pair-chart', labeledBarChartSvg(observatory.pair_breakdown || [], 'rgba(98,184,255,0.88)'));
      setHTML('signal-rejection-chart', labeledBarChartSvg(observatory.rejection_breakdown || [], 'rgba(255,109,109,0.88)'));
      setHTML('signal-regime-chart', labeledBarChartSvg(observatory.regime_breakdown || [], 'rgba(56,211,159,0.88)'));
      setText('signal-funnel-meta', `${{observedSignals}} observed | tradable ${{tradableSignals}} | setup rows ${{(observatory.setup_breakdown || []).length}}`);
      setText('signal-rejection-meta', `${{decisionRejections}} decision rejects | ${{(observatory.rejection_breakdown || []).length}} rejection buckets | ${{(observatory.analysis_window_coverage || []).length}} active windows`);
      renderShadowSection(overview);
      renderPersonalJournalSection(overview);
      renderFastResearchSection(overview);
      renderStrategyLabSection(overview);
      renderJournalAlignmentSection(overview);
      const tradeAnalytics = overview.trade_analytics || {{}};
      const tradeSummary = tradeAnalytics.summary || {{}};
      setText('equity-chart-meta', `ending equity = ${{fmtNumber(tradeSummary.ending_equity, 2)}} EUR`);
      setText('pnl-chart-meta', `net pnl = ${{fmtNumber(tradeSummary.net_pnl_eur, 2)}} EUR | avg trade = ${{fmtNumber(tradeSummary.avg_pnl_per_trade_eur, 2)}} EUR`);
      setText('pair-performance-meta', `${{fmtText(tradeSummary.closed_trades)}} closed trades | avg hold = ${{fmtNumber(tradeSummary.avg_hold_minutes, 2)}} min`);
      const copilot = overview.copilot || {{}};
      setText('copilot-plain-status', fmtText(copilot.plain_status));
      setText('copilot-operator-focus', fmtText(copilot.operator_focus));
      setText('copilot-journal-hint', fmtText(copilot.journal_hint));
      setHTML('copilot-actions', (copilot.recommended_actions || []).map((item, index) => `<div class="list-item"><strong>Action ${{index + 1}}</strong><span>${{escapeHtml(item)}}</span></div>`).join('') || '<div class="list-item"><strong>No actions</strong><span>Der Copilot hat aktuell keine weiteren Empfehlungen.</span></div>');
      setHTML('copilot-beginner-guide', (copilot.beginner_terms || []).map(item => `<div class="list-item"><strong>${{escapeHtml(item.term)}}</strong><span>${{escapeHtml(item.simple)}}</span></div>`).join('') || '<div class="list-item"><strong>No guide entries</strong><span>Es liegen aktuell keine vereinfachten Erklärungen vor.</span></div>');
      setHTML('copilot-warnings', (copilot.warnings || []).map(item => `<div class="list-item"><strong>${{escapeHtml(item.title)}}</strong><span>${{escapeHtml(item.detail)}}</span><span class="${{pillClassFromText(item.severity)}}">${{escapeHtml(item.simple)}}</span></div>`).join('') || '<div class="list-item"><strong>No warnings</strong><span>Aktuell meldet der Copilot keine zusaetzlichen Warnstufen.</span></div>');
      setHTML('copilot-gate-guide', (copilot.gate_explanations || []).map(item => `<div class="list-item"><strong>${{escapeHtml(item.name)}} | ${{escapeHtml(item.status)}}</strong><span>${{escapeHtml(item.simple)}}</span><span class="path">actual: ${{escapeHtml(fmtText(item.actual))}} | threshold: ${{escapeHtml(fmtText(item.threshold))}}</span></div>`).join('') || '<div class="list-item"><strong>No gate guide</strong><span>Es liegen aktuell keine Gate-Erklaerungen vor.</span></div>');
      renderTradeDetail(overview);
      const pairStatus = history.pair_status || {{}};
      setHTML('pair-history-body', Object.entries(pairStatus).map(([symbol, row]) => `<tr><td>${{escapeHtml(symbol)}}</td><td>${{fmtText(row.candles_1m)}}</td><td>${{fmtText(row.candles_15m)}}</td><td>${{fmtNumber(row.span_days, 3)}}</td><td>${{fmtDateTime(row.last_ts)}}</td></tr>`).join('') || '<tr><td colspan="5">No history loaded.</td></tr>');
      const runtimeItems = [
        {{ label: 'Watchdog and task', lines: [`task status: ${{fmtText(task.status)}}`, `run as: ${{fmtText(task.run_as_user)}}`, `last result: ${{fmtText(task.last_result)}}`] }},
        {{ label: 'Supervisor process', lines: [`pid: ${{fmtText(monitor.supervisor && monitor.supervisor.pid)}}`, `alive: ${{fmtText(monitor.supervisor && monitor.supervisor.alive)}}`, `stop requested: ${{fmtText(monitor.supervisor && monitor.supervisor.stop_requested)}}`] }},
        {{ label: 'Paper-forward process', lines: [`pid: ${{fmtText(monitor.paper_forward && monitor.paper_forward.pid)}}`, `alive: ${{fmtText(monitor.paper_forward && monitor.paper_forward.alive)}}`, `status: ${{fmtText(summary.paper_forward_status)}}`] }},
        {{ label: 'Research scan', lines: [`status: ${{fmtText(summary.research_scan_status)}}`, `last run: ${{fmtDateTime(summary.research_scan_last_run_at)}}`, `last error: ${{fmtText(summary.research_scan_last_error || 'none')}}`] }},
      ];
      setHTML('runtime-list', runtimeItems.map(item => `<div class="list-item"><strong>${{escapeHtml(item.label)}}</strong>${{item.lines.map(line => `<span>${{escapeHtml(line)}}</span>`).join('')}}</div>`).join(''));
      const cycleRows = (lastCycle.pair_deltas || []).map(row => `<tr><td>${{escapeHtml(fmtText(row.interval))}}</td><td>${{escapeHtml(fmtText(row.pair))}}</td><td>${{escapeHtml(fmtText(row.fetched_rows))}}</td><td>${{escapeHtml(fmtText(row.merged_rows))}}</td><td>${{escapeHtml(fmtText(row.written_rows))}}</td><td>${{escapeHtml(fmtText(row.status))}}</td></tr>`);
      setHTML('cycle-delta-body', cycleRows.join('') || '<tr><td colspan="6">No cycle deltas yet.</td></tr>');
      const issues = [...(summary.last_errors || []), ...(summary.gate_blockers || []), ...((launch.gate_blockers || []))];
      const uniqueIssues = [...new Set(issues)];
      setHTML('issues-list', uniqueIssues.map(item => `<div class="list-item"><strong>${{escapeHtml(item)}}</strong><span>Current monitoring issue or launch blocker from the latest summary and gate state.</span></div>`).join('') || '<div class="list-item"><strong>No current issues</strong><span>The current summary does not report errors.</span></div>');
      setHTML('recent-runs', (overview.recent_runs || []).map(run => `<div class="list-item"><strong>${{escapeHtml(fmtText(run.name))}}</strong><span>${{escapeHtml(fmtText(run.kind))}} | ${{escapeHtml(fmtText(run.status))}} | ${{escapeHtml(fmtPercent(run.progress_pct))}}</span><span class="path">${{escapeHtml(fmtText(run.path))}}</span></div>`).join('') || '<div class="list-item"><strong>No recent runs</strong><span>No watchdog or supervisor directories found.</span></div>');
      const artifacts = [
        ['State path', monitor.state_path],
        ['Summary JSON', monitor.daily_summary_json_path],
        ['Summary Markdown', monitor.daily_summary_markdown_path],
        ['Static dashboard', monitor.dashboard_path],
      ];
      setHTML('artifact-list', artifacts.map(([label, value]) => `<div class="list-item"><strong>${{escapeHtml(label)}}</strong><span class="path">${{escapeHtml(fmtText(value))}}</span></div>`).join(''));
      setText('footer-note', `Read-only local web app | auto-refresh 15s | timezone ${{fmtText(app.timezone)}} | next stage ${{fmtText(launch.current_phase)}}`);
    }}
    async function refreshOverview() {{
      try {{
        const response = await fetch('/api/overview', {{ cache: 'no-store' }});
        const overview = await response.json();
        renderOverview(overview);
      }} catch (error) {{
        setText('footer-note', `Dashboard refresh failed: ${{error}}`);
      }}
    }}
    renderOverview(initialOverview);
    setInterval(refreshOverview, 15000);
  </script>
</body>
</html>
'''


def _normalize_key(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_value.lower().split())


def _first_normalized_value(normalized: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = normalized.get(_normalize_key(name))
        if value:
            return value
    return None


def _decode_windows_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for encoding in ("utf-8", "cp850", "cp1252", "latin-1"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")
