from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PersonalTradeEntry:
    trade_id: str
    logged_at: str
    market: str
    instrument: str
    venue: str
    side: str
    strategy_name: str
    setup_family: str
    timeframe: str
    status: str
    entry_ts: str | None
    exit_ts: str | None
    entry_price: float | None
    exit_price: float | None
    pnl_eur: float
    pnl_pct: float | None
    fees_eur: float
    size_notional_eur: float | None
    confidence_before: int | None
    confidence_after: int | None
    lesson: str
    notes: str
    tags: tuple[str, ...]
    mistakes: tuple[str, ...]


@dataclass(frozen=True)
class PersonalJournalSummary:
    source_exists: bool
    journal_path: str
    total_trades: int
    closed_trades: int
    open_trades: int
    win_rate: float
    net_pnl_eur: float
    average_pnl_eur: float
    average_pnl_pct: float | None
    average_hold_minutes: float | None
    markets: list[dict[str, Any]]
    instruments: list[dict[str, Any]]
    strategies: list[dict[str, Any]]
    mistakes: list[dict[str, Any]]
    tags: list[dict[str, Any]]
    recent_trades: list[dict[str, Any]]
    beginner_summary: list[str]


def build_personal_journal_payload(summary: PersonalJournalSummary) -> dict[str, Any]:
    entries = list(summary.recent_trades)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "title": "Personal Trading Journal",
            "subtitle": "Manuelle Echtgeld- und Lerntrades als zusaetzliche Realwelt-Datenquelle",
            "total_entries": summary.total_trades,
            "winning_entries": int(round(summary.win_rate * summary.closed_trades)),
            "losing_entries": max(summary.closed_trades - int(round(summary.win_rate * summary.closed_trades)), 0),
            "win_rate": summary.win_rate,
            "realized_pnl_eur": summary.net_pnl_eur,
            "largest_win_eur": max((float(entry.get("pnl_eur") or 0.0) for entry in entries), default=0.0),
            "largest_loss_eur": min((float(entry.get("pnl_eur") or 0.0) for entry in entries), default=0.0),
            "active_strategies": len(summary.strategies),
            "tracked_assets": len(summary.instruments),
            "kpis": [
                {"label": "Closed trades", "value": summary.closed_trades},
                {"label": "Win rate", "value": round(summary.win_rate * 100.0, 2)},
                {"label": "Net PnL EUR", "value": summary.net_pnl_eur},
                {"label": "Avg hold min", "value": summary.average_hold_minutes or 0.0},
            ],
        },
        "entries": [
            {
                "title": f"{entry.get('instrument')} {entry.get('strategy_name')}".strip(),
                "asset": entry.get("instrument"),
                "market": entry.get("market"),
                "strategy": entry.get("strategy_name"),
                "setup_family": entry.get("setup_family"),
                "timeframe": entry.get("timeframe"),
                "pnl_eur": entry.get("pnl_eur"),
                "pnl_pct": entry.get("pnl_pct"),
                "confidence": (entry.get("confidence_after") or entry.get("confidence_before") or 0) / 100.0,
                "tags": entry.get("tags") or [],
                "lesson": entry.get("lesson") or "",
                "notes": entry.get("notes") or "",
                "status": entry.get("status") or "closed",
                "trade_id": entry.get("trade_id"),
                "side": entry.get("side"),
                "entry_ts": entry.get("entry_ts"),
                "exit_ts": entry.get("exit_ts"),
                "source": "manual",
            }
            for entry in entries
        ],
        "strategy_notes": [
            {
                "title": row["label"],
                "strategy": row["label"],
                "detail": f"Trades {row['trades']} | Win Rate {row['win_rate'] * 100.0:.1f}% | Net {row['net_pnl_eur']:.2f} EUR",
                "takeaway": "Beobachte, ob diese Strategie unter Druck konsistent bleibt.",
                "net_pnl_eur": row["net_pnl_eur"],
                "win_rate": row["win_rate"],
                "trades": row["trades"],
            }
            for row in summary.strategies[:8]
        ],
        "learning_points": [
            {
                "title": row["label"],
                "detail": f"{row['value']}x im Journal markiert",
                "takeaway": "Haeufige Fehler zuerst systematisch abstellen.",
                "count": row["value"],
            }
            for row in summary.mistakes[:8]
        ],
        "asset_breakdown": [{"label": row["label"], "value": row["value"]} for row in summary.instruments[:10]],
        "beginner_notes": [
            {
                "term": "Journal insight",
                "simple": line,
            }
            for line in summary.beginner_summary
        ],
    }


def ensure_personal_journal_path(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    return path


def append_personal_trade(path: Path, entry: PersonalTradeEntry) -> dict[str, Any]:
    ensure_personal_journal_path(path)
    payload = asdict(entry)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    return payload


def build_personal_trade_entry(
    *,
    market: str,
    instrument: str,
    venue: str,
    side: str,
    strategy_name: str,
    setup_family: str,
    timeframe: str,
    status: str,
    entry_ts: str | None,
    exit_ts: str | None,
    entry_price: float | None,
    exit_price: float | None,
    pnl_eur: float,
    pnl_pct: float | None,
    fees_eur: float,
    size_notional_eur: float | None,
    confidence_before: int | None,
    confidence_after: int | None,
    lesson: str,
    notes: str,
    tags: list[str] | tuple[str, ...] | None = None,
    mistakes: list[str] | tuple[str, ...] | None = None,
) -> PersonalTradeEntry:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry_key = (entry_ts or "na").replace(":", "").replace("-", "").replace("T", "_").replace("+00:00", "z")
    trade_id = f"{market.lower()}_{instrument.lower().replace('/', '-')}_{entry_key}"
    return PersonalTradeEntry(
        trade_id=trade_id,
        logged_at=now,
        market=str(market),
        instrument=str(instrument),
        venue=str(venue),
        side=str(side),
        strategy_name=str(strategy_name),
        setup_family=str(setup_family),
        timeframe=str(timeframe),
        status=str(status),
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_price=entry_price,
        exit_price=exit_price,
        pnl_eur=float(pnl_eur),
        pnl_pct=(float(pnl_pct) if pnl_pct is not None else None),
        fees_eur=float(fees_eur),
        size_notional_eur=(float(size_notional_eur) if size_notional_eur is not None else None),
        confidence_before=confidence_before,
        confidence_after=confidence_after,
        lesson=str(lesson or ""),
        notes=str(notes or ""),
        tags=tuple(_normalize_list(tags)),
        mistakes=tuple(_normalize_list(mistakes)),
    )


def run_personal_journal_report(path: Path) -> PersonalJournalSummary:
    entries = _load_entries(path)
    closed = [entry for entry in entries if entry.status.lower() == "closed"]
    open_trades = [entry for entry in entries if entry.status.lower() != "closed"]
    market_counter: Counter[str] = Counter()
    instrument_counter: Counter[str] = Counter()
    strategy_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0})
    mistake_counter: Counter[str] = Counter()
    tag_counter: Counter[str] = Counter()
    hold_minutes: list[float] = []

    for entry in entries:
        market_counter[entry.market] += 1
        instrument_counter[entry.instrument] += 1
        for tag in entry.tags:
            tag_counter[tag] += 1
        for mistake in entry.mistakes:
            mistake_counter[mistake] += 1
        bucket = strategy_stats[entry.strategy_name or "unknown"]
        bucket["trades"] += 1
        bucket["net_pnl"] += entry.pnl_eur
        if entry.pnl_eur > 0:
            bucket["wins"] += 1
        elif entry.pnl_eur < 0:
            bucket["losses"] += 1
        hold = _hold_minutes(entry.entry_ts, entry.exit_ts)
        if hold is not None:
            hold_minutes.append(hold)

    closed_count = len(closed)
    wins = sum(1 for entry in closed if entry.pnl_eur > 0)
    net_pnl = sum(entry.pnl_eur for entry in entries)
    avg_pnl = (net_pnl / closed_count) if closed_count else 0.0
    pct_values = [entry.pnl_pct for entry in closed if entry.pnl_pct is not None]
    beginner_summary = _build_beginner_summary(
        total=len(entries),
        closed_count=closed_count,
        wins=wins,
        net_pnl=net_pnl,
        mistake_counter=mistake_counter,
    )

    return PersonalJournalSummary(
        source_exists=path.exists(),
        journal_path=str(path),
        total_trades=len(entries),
        closed_trades=closed_count,
        open_trades=len(open_trades),
        win_rate=(wins / closed_count) if closed_count else 0.0,
        net_pnl_eur=round(net_pnl, 4),
        average_pnl_eur=round(avg_pnl, 4),
        average_pnl_pct=(round(sum(pct_values) / len(pct_values), 4) if pct_values else None),
        average_hold_minutes=(round(sum(hold_minutes) / len(hold_minutes), 2) if hold_minutes else None),
        markets=_counter_rows(market_counter),
        instruments=_counter_rows(instrument_counter),
        strategies=[
            {
                "label": label,
                "trades": int(values["trades"]),
                "win_rate": (values["wins"] / values["trades"]) if values["trades"] else 0.0,
                "net_pnl_eur": round(values["net_pnl"], 4),
            }
            for label, values in sorted(strategy_stats.items(), key=lambda item: item[1]["net_pnl"], reverse=True)
        ],
        mistakes=_counter_rows(mistake_counter),
        tags=_counter_rows(tag_counter),
        recent_trades=[
            {
                "trade_id": entry.trade_id,
                "market": entry.market,
                "instrument": entry.instrument,
                "side": entry.side,
                "strategy_name": entry.strategy_name,
                "setup_family": entry.setup_family,
                "timeframe": entry.timeframe,
                "status": entry.status,
                "pnl_eur": entry.pnl_eur,
                "pnl_pct": entry.pnl_pct,
                "lesson": entry.lesson,
                "notes": entry.notes,
                "tags": list(entry.tags),
                "mistakes": list(entry.mistakes),
                "entry_ts": entry.entry_ts,
                "exit_ts": entry.exit_ts,
                "confidence_before": entry.confidence_before,
                "confidence_after": entry.confidence_after,
            }
            for entry in sorted(entries, key=lambda item: item.logged_at, reverse=True)[:15]
        ],
        beginner_summary=beginner_summary,
    )


def _load_entries(path: Path) -> list[PersonalTradeEntry]:
    if not path.exists():
        return []
    entries: list[PersonalTradeEntry] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            payload = json.loads(raw)
            entries.append(
                PersonalTradeEntry(
                    trade_id=str(payload.get("trade_id", "")),
                    logged_at=str(payload.get("logged_at", "")),
                    market=str(payload.get("market", "unknown")),
                    instrument=str(payload.get("instrument", "unknown")),
                    venue=str(payload.get("venue", "")),
                    side=str(payload.get("side", "long")),
                    strategy_name=str(payload.get("strategy_name", "")),
                    setup_family=str(payload.get("setup_family", "")),
                    timeframe=str(payload.get("timeframe", "")),
                    status=str(payload.get("status", "closed")),
                    entry_ts=(str(payload["entry_ts"]) if payload.get("entry_ts") else None),
                    exit_ts=(str(payload["exit_ts"]) if payload.get("exit_ts") else None),
                    entry_price=(float(payload["entry_price"]) if payload.get("entry_price") is not None else None),
                    exit_price=(float(payload["exit_price"]) if payload.get("exit_price") is not None else None),
                    pnl_eur=float(payload.get("pnl_eur", 0.0)),
                    pnl_pct=(float(payload["pnl_pct"]) if payload.get("pnl_pct") is not None else None),
                    fees_eur=float(payload.get("fees_eur", 0.0)),
                    size_notional_eur=(float(payload["size_notional_eur"]) if payload.get("size_notional_eur") is not None else None),
                    confidence_before=(int(payload["confidence_before"]) if payload.get("confidence_before") is not None else None),
                    confidence_after=(int(payload["confidence_after"]) if payload.get("confidence_after") is not None else None),
                    lesson=str(payload.get("lesson", "")),
                    notes=str(payload.get("notes", "")),
                    tags=tuple(_normalize_list(payload.get("tags"))),
                    mistakes=tuple(_normalize_list(payload.get("mistakes"))),
                )
            )
    return entries


def _normalize_list(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = values.split(",")
    return [str(value).strip() for value in values if str(value).strip()]


def _counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    total = sum(counter.values())
    return [
        {"label": label, "value": count, "share": (count / total) if total else 0.0}
        for label, count in counter.most_common()
    ]


def _hold_minutes(entry_ts: str | None, exit_ts: str | None) -> float | None:
    if not entry_ts or not exit_ts:
        return None
    try:
        start = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
        end = datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max((end - start).total_seconds() / 60.0, 0.0)


def _build_beginner_summary(
    *,
    total: int,
    closed_count: int,
    wins: int,
    net_pnl: float,
    mistake_counter: Counter[str],
) -> list[str]:
    items = [
        f"Erfasste Trades: {total}. Geschlossene Trades: {closed_count}.",
        f"Netto-PnL ueber alle manuell eingetragenen Trades: {net_pnl:.2f} EUR.",
    ]
    if closed_count:
        items.append(f"Win Rate auf geschlossenen Trades: {(wins / closed_count) * 100.0:.1f}%.")
    if mistake_counter:
        top = mistake_counter.most_common(1)[0][0]
        items.append(f"Haefigster eigener Fehler laut Journal: {top}.")
    else:
        items.append("Noch keine markierten Fehler im Journal. Das ist meist ein Zeichen fuer zu wenig Rueckschau, nicht fuer fehlerfreies Trading.")
    return items
