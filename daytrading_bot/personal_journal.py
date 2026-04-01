from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PERSONAL_JOURNAL_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "preset_id": "sol_swing_4h",
        "label": "SOL Swing 4H",
        "market": "crypto",
        "instrument": "SOL",
        "venue": "Kraken",
        "side": "long",
        "strategy_name": "manual_swing",
        "setup_family": "swing",
        "timeframe": "4H",
        "status": "closed",
        "tags": ["crypto", "sol", "swing"],
        "beginner_hint": "Nutze das fuer normale Solana-Swingtrades mit wenigen klaren Entscheidungen.",
    },
    {
        "preset_id": "doge_momentum_1h",
        "label": "DOGE Momentum 1H",
        "market": "crypto",
        "instrument": "DOGE",
        "venue": "Kraken",
        "side": "long",
        "strategy_name": "manual_momentum",
        "setup_family": "momentum",
        "timeframe": "1H",
        "status": "closed",
        "tags": ["crypto", "doge", "momentum"],
        "beginner_hint": "Sinnvoll fuer schnellere Dogecoin-Bewegungen, bei denen du Ein- und Ausstieg enger dokumentieren willst.",
    },
    {
        "preset_id": "fet_reclaim_1h",
        "label": "FET Reclaim 1H",
        "market": "crypto",
        "instrument": "FET",
        "venue": "Kraken",
        "side": "long",
        "strategy_name": "manual_reclaim",
        "setup_family": "reclaim",
        "timeframe": "1H",
        "status": "closed",
        "tags": ["crypto", "fet", "reclaim"],
        "beginner_hint": "Gedacht fuer Fetch.ai-Reclaims oder Trend-Wiederaufnahmen mit klarem Invalidierungsniveau.",
    },
    {
        "preset_id": "crypto_swing",
        "label": "Crypto Swing",
        "market": "crypto",
        "venue": "Kraken",
        "side": "long",
        "strategy_name": "manual_swing",
        "setup_family": "swing",
        "timeframe": "4H",
        "status": "closed",
        "tags": ["crypto", "swing"],
        "beginner_hint": "Nutze diesen Preset fuer ruhigere Solana-, Dogecoin- oder Fetch-Swingtrades.",
    },
    {
        "preset_id": "crypto_position",
        "label": "Crypto Position",
        "market": "crypto",
        "venue": "Kraken",
        "side": "long",
        "strategy_name": "manual_position",
        "setup_family": "position",
        "timeframe": "1D",
        "status": "closed",
        "tags": ["crypto", "position"],
        "beginner_hint": "Sinnvoll fuer laenger gehaltene Positionen, bei denen du nur wenige Entscheidungen triffst.",
    },
    {
        "preset_id": "micro_btc_gold",
        "label": "BTC or Gold Micro",
        "market": "fx",
        "venue": "Broker",
        "side": "long",
        "strategy_name": "micro_trial",
        "setup_family": "fast",
        "timeframe": "1M",
        "status": "closed",
        "tags": ["micro", "fast"],
        "beginner_hint": "Gedacht fuer sehr kurze BTC/USD- oder XAU/USD-Mikrotrades mit engem Review danach.",
    },
    {
        "preset_id": "btc_micro_1m",
        "label": "BTC Micro 1M",
        "market": "fx",
        "instrument": "BTCUSD",
        "venue": "Broker",
        "side": "long",
        "strategy_name": "btc_micro_trial",
        "setup_family": "fast",
        "timeframe": "1M",
        "status": "closed",
        "tags": ["btc", "micro", "fast"],
        "beginner_hint": "Fuer sehr kurze BTC/USD-Mikrotrades. Halte Lesson und Fehler hier besonders sauber fest.",
    },
    {
        "preset_id": "xau_micro_1m",
        "label": "XAU Micro 1M",
        "market": "metals",
        "instrument": "XAUUSD",
        "venue": "Broker",
        "side": "long",
        "strategy_name": "xau_micro_trial",
        "setup_family": "fast",
        "timeframe": "1M",
        "status": "closed",
        "tags": ["gold", "micro", "fast"],
        "beginner_hint": "Fuer kurze Gold-Mikrotrades. Vor allem Stop-Disziplin und Exit-Timing dokumentieren.",
    },
    {
        "preset_id": "stocks_swing",
        "label": "Stocks Swing",
        "market": "stocks",
        "venue": "Broker",
        "side": "long",
        "strategy_name": "equity_swing",
        "setup_family": "swing",
        "timeframe": "1D",
        "status": "closed",
        "tags": ["stocks", "swing"],
        "beginner_hint": "Nutze das fuer normale Aktien-Swingtrades, damit diese Daten nicht mit Krypto vermischt werden.",
    },
)


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
    venues: list[dict[str, Any]]
    timeframes: list[dict[str, Any]]
    strategies: list[dict[str, Any]]
    setup_families: list[dict[str, Any]]
    mistakes: list[dict[str, Any]]
    tags: list[dict[str, Any]]
    recent_trades: list[dict[str, Any]]
    recommendations: list[dict[str, str]]
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
        "venue_breakdown": [{"label": row["label"], "value": row["value"]} for row in summary.venues[:10]],
        "timeframe_breakdown": [{"label": row["label"], "value": row["value"]} for row in summary.timeframes[:10]],
        "setup_families": [{"label": row["label"], "value": row["value"]} for row in summary.setup_families[:10]],
        "recommendations": list(summary.recommendations),
        "presets": list_personal_journal_presets(),
        "beginner_notes": [
            {
                "term": "Journal insight",
                "simple": line,
            }
            for line in summary.beginner_summary
        ],
    }


def list_personal_journal_presets() -> list[dict[str, Any]]:
    return [dict(preset) for preset in PERSONAL_JOURNAL_PRESETS]


def resolve_personal_journal_preset(preset_id: str | None) -> dict[str, Any] | None:
    if not preset_id:
        return None
    normalized = str(preset_id).strip().lower()
    for preset in PERSONAL_JOURNAL_PRESETS:
        if str(preset.get("preset_id") or "").lower() == normalized:
            return dict(preset)
    return None


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
    preset_id: str | None = None,
) -> PersonalTradeEntry:
    preset = resolve_personal_journal_preset(preset_id)
    market = str(market or (preset or {}).get("market") or "")
    instrument = str(instrument or (preset or {}).get("instrument") or "")
    venue = str(venue or (preset or {}).get("venue") or "")
    side = str(side or (preset or {}).get("side") or "")
    strategy_name = str(strategy_name or (preset or {}).get("strategy_name") or "")
    setup_family = str(setup_family or (preset or {}).get("setup_family") or "")
    timeframe = str(timeframe or (preset or {}).get("timeframe") or "")
    status = str(status or (preset or {}).get("status") or "")
    if preset and not tags:
        tags = preset.get("tags")

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
    venue_counter: Counter[str] = Counter()
    timeframe_counter: Counter[str] = Counter()
    strategy_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0})
    setup_family_counter: Counter[str] = Counter()
    mistake_counter: Counter[str] = Counter()
    tag_counter: Counter[str] = Counter()
    hold_minutes: list[float] = []

    for entry in entries:
        market_counter[entry.market] += 1
        instrument_counter[entry.instrument] += 1
        venue_counter[entry.venue or "unknown"] += 1
        timeframe_counter[entry.timeframe or "unknown"] += 1
        setup_family_counter[entry.setup_family or "unknown"] += 1
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
    recommendations = _build_recommendations(
        closed_count=closed_count,
        net_pnl=net_pnl,
        strategy_rows=[
            {
                "label": label,
                "trades": int(values["trades"]),
                "win_rate": (values["wins"] / values["trades"]) if values["trades"] else 0.0,
                "net_pnl_eur": round(values["net_pnl"], 4),
            }
            for label, values in sorted(strategy_stats.items(), key=lambda item: item[1]["net_pnl"], reverse=True)
        ],
        setup_family_rows=_counter_rows(setup_family_counter),
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
        venues=_counter_rows(venue_counter),
        timeframes=_counter_rows(timeframe_counter),
        strategies=[
            {
                "label": label,
                "trades": int(values["trades"]),
                "win_rate": (values["wins"] / values["trades"]) if values["trades"] else 0.0,
                "net_pnl_eur": round(values["net_pnl"], 4),
            }
            for label, values in sorted(strategy_stats.items(), key=lambda item: item[1]["net_pnl"], reverse=True)
        ],
        setup_families=_counter_rows(setup_family_counter),
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
        recommendations=recommendations,
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


def _build_recommendations(
    *,
    closed_count: int,
    net_pnl: float,
    strategy_rows: list[dict[str, Any]],
    setup_family_rows: list[dict[str, Any]],
    mistake_counter: Counter[str],
) -> list[dict[str, str]]:
    recommendations: list[dict[str, str]] = []
    if closed_count < 5:
        recommendations.append(
            {
                "title": "Mehr Stichprobe sammeln",
                "detail": "Dein persoenliches Journal ist noch klein. Einzelne Gewinne oder Verluste sind noch nicht belastbar.",
                "action": "Erfasse mindestens 10 bis 15 sauber dokumentierte Trades, bevor du Muster umstellst.",
                "severity": "info",
            }
        )
    if net_pnl < 0:
        recommendations.append(
            {
                "title": "Verlustphase zuerst stabilisieren",
                "detail": f"Dein manuelles Journal liegt aktuell bei {net_pnl:.2f} EUR netto.",
                "action": "Weniger neue Ideen gleichzeitig testen und zuerst die klarsten Fehlerquellen reduzieren.",
                "severity": "warning",
            }
        )
    weak_strategies = [row for row in strategy_rows if row["trades"] >= 2 and (row["net_pnl_eur"] < 0 or row["win_rate"] < 0.45)]
    if weak_strategies:
        weakest = weak_strategies[-1]
        recommendations.append(
            {
                "title": f"Strategie '{weakest['label']}' ueberpruefen",
                "detail": f"Trades {weakest['trades']} | Win Rate {weakest['win_rate'] * 100.0:.1f}% | Net {weakest['net_pnl_eur']:.2f} EUR.",
                "action": "Diese Strategie entweder enger definieren oder vorerst nur im Paper-Modus weiter pruefen.",
                "severity": "warning",
            }
        )
    if setup_family_rows:
        dominant = setup_family_rows[0]
        if dominant["share"] >= 0.6:
            recommendations.append(
                {
                    "title": f"Setup-Familie '{dominant['label']}' dominiert",
                    "detail": f"{dominant['share'] * 100.0:.1f}% deiner manuellen Trades kommen aus einer Familie.",
                    "action": "Pruefe, ob du dadurch Marktphasen zu einseitig spielst und andere gute Setups ignorierst.",
                    "severity": "info",
                }
            )
    if mistake_counter:
        top_mistake, count = mistake_counter.most_common(1)[0]
        recommendations.append(
            {
                "title": f"Top-Fehler: {top_mistake}",
                "detail": f"Dieser Fehler wurde {count}x markiert.",
                "action": "Baue fuer genau diesen Fehler eine feste Vorab-Checkliste oder einen harden Exit-Trigger.",
                "severity": "critical" if count >= 3 else "warning",
            }
        )
    profitable_strategies = [row for row in strategy_rows if row["trades"] >= 2 and row["net_pnl_eur"] > 0 and row["win_rate"] >= 0.5]
    if profitable_strategies:
        strongest = profitable_strategies[0]
        recommendations.append(
            {
                "title": f"Staerkste manuelle Strategie: {strongest['label']}",
                "detail": f"Trades {strongest['trades']} | Win Rate {strongest['win_rate'] * 100.0:.1f}% | Net {strongest['net_pnl_eur']:.2f} EUR.",
                "action": "Diese Regeln explizit aufschreiben und mit dem Bot-Cockpit vergleichen statt nur aus dem Bauch zu handeln.",
                "severity": "good",
            }
        )
    return recommendations[:6]
