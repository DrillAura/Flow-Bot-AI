from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BotConfig, ThreeCommasConfig
from .telemetry import InMemoryTelemetry, JsonlTelemetry


@dataclass(frozen=True)
class ShadowPortfolioSpec:
    name: str
    initial_equity_eur: float


@dataclass(frozen=True)
class ShadowPortfolioSummary:
    name: str
    initial_equity_eur: float
    ending_equity: float
    net_pnl_eur: float
    closed_trades: int
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    average_hold_minutes: float
    average_mae_r: float
    average_mfe_r: float
    average_total_fee_eur: float
    average_total_slippage_bps: float


@dataclass(frozen=True)
class ShadowPortfolioReport:
    source_exists: bool
    portfolios: list[ShadowPortfolioSummary]
    equity_curves: list[dict[str, Any]]
    regime_comparison: list[dict[str, Any]]
    setup_comparison: list[dict[str, Any]]
    filter_options: dict[str, list[str]]


class ShadowPortfolioLab:
    def __init__(self, bot_config: BotConfig, telemetry: JsonlTelemetry) -> None:
        self.bot_config = bot_config
        self.telemetry = telemetry
        self.runners = [self._build_runner(spec) for spec in build_shadow_portfolio_specs(bot_config)]

    def process_market(self, contexts, moment: datetime) -> None:
        for runner in self.runners:
            before = len(runner["telemetry"].events)
            engine = runner["engine"]
            engine.process_market(contexts, available_eur=engine.risk.state.equity, moment=moment)
            for event in runner["telemetry"].events[before:]:
                mapped = _map_shadow_event(
                    event,
                    portfolio_name=runner["spec"].name,
                    initial_equity=runner["spec"].initial_equity_eur,
                    ending_equity=engine.risk.state.equity,
                    max_drawdown_pct=engine.risk.max_drawdown_pct,
                )
                if mapped is None:
                    continue
                event_type, payload = mapped
                self.telemetry.log(event_type, payload, event_ts=moment)

    def _build_runner(self, spec: ShadowPortfolioSpec) -> dict[str, Any]:
        from .engine import BotEngine

        portfolio_config = replace(self.bot_config, initial_equity_eur=spec.initial_equity_eur)
        portfolio_telemetry = InMemoryTelemetry()
        engine = BotEngine(
            portfolio_config,
            ThreeCommasConfig(mode="paper", allow_live=False),
            telemetry=portfolio_telemetry,
            enable_research=False,
        )
        return {
            "spec": spec,
            "telemetry": portfolio_telemetry,
            "engine": engine,
        }


def build_shadow_portfolio_specs(bot_config: BotConfig) -> list[ShadowPortfolioSpec]:
    specs: list[ShadowPortfolioSpec] = []
    for size in bot_config.shadow_portfolio_sizes_eur:
        specs.append(
            ShadowPortfolioSpec(
                name=f"shadow_{int(size):04d}",
                initial_equity_eur=float(size),
            )
        )
    return specs


def run_shadow_portfolio_report(telemetry_path: Path, bot_config: BotConfig) -> ShadowPortfolioReport:
    events = _load_events(telemetry_path)
    specs = build_shadow_portfolio_specs(bot_config)
    initial_map = {spec.name: spec.initial_equity_eur for spec in specs}
    portfolio_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    regime_groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"net_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    setup_groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"net_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})

    for event in events:
        event_type = str(event.get("event_type", ""))
        if not event_type.startswith("shadow_"):
            continue
        payload = event.get("payload", {}) or {}
        portfolio = str(payload.get("portfolio_name", "unknown"))
        portfolio_events[portfolio].append(event)
        if event_type in {"shadow_exit_sent", "shadow_kill_switch_exit"}:
            regime_key = (portfolio, str(payload.get("regime_label", "unknown")))
            setup_key = (portfolio, str(payload.get("setup_type", "unknown")))
            pnl = float(payload.get("pnl_eur", 0.0))
            for key, groups in ((regime_key, regime_groups), (setup_key, setup_groups)):
                bucket = groups[key]
                bucket["net_pnl"] += pnl
                bucket["trades"] += 1
                if pnl > 0:
                    bucket["wins"] += 1
                elif pnl < 0:
                    bucket["losses"] += 1

    summaries = [
        _summarize_shadow_portfolio(spec.name, initial_map.get(spec.name, spec.initial_equity_eur), portfolio_events.get(spec.name, []))
        for spec in specs
    ]
    equity_curves = [
        {
            "portfolio": summary.name,
            "points": _equity_curve_points(portfolio_events.get(summary.name, []), initial_map.get(summary.name, summary.initial_equity_eur)),
        }
        for summary in summaries
    ]
    regime_comparison = [
        {
            "portfolio": portfolio,
            "regime_label": regime,
            "net_pnl_eur": round(values["net_pnl"], 4),
            "trades": values["trades"],
            "win_rate": (values["wins"] / values["trades"]) if values["trades"] else 0.0,
        }
        for (portfolio, regime), values in sorted(regime_groups.items())
    ]
    setup_comparison = [
        {
            "portfolio": portfolio,
            "setup_type": setup,
            "net_pnl_eur": round(values["net_pnl"], 4),
            "trades": values["trades"],
            "win_rate": (values["wins"] / values["trades"]) if values["trades"] else 0.0,
        }
        for (portfolio, setup), values in sorted(setup_groups.items())
    ]
    return ShadowPortfolioReport(
        source_exists=telemetry_path.exists(),
        portfolios=summaries,
        equity_curves=equity_curves,
        regime_comparison=regime_comparison,
        setup_comparison=setup_comparison,
        filter_options={
            "portfolios": [summary.name for summary in summaries],
            "regimes": sorted({row["regime_label"] for row in regime_comparison}),
            "setups": sorted({row["setup_type"] for row in setup_comparison}),
        },
    )


def _summarize_shadow_portfolio(name: str, initial_equity: float, events: list[dict[str, Any]]) -> ShadowPortfolioSummary:
    exits = [event for event in events if event.get("event_type") in {"shadow_exit_sent", "shadow_kill_switch_exit"}]
    gross_profit = sum(float(event.get("payload", {}).get("pnl_eur", 0.0)) for event in exits if float(event.get("payload", {}).get("pnl_eur", 0.0)) > 0.0)
    gross_loss = sum(abs(float(event.get("payload", {}).get("pnl_eur", 0.0))) for event in exits if float(event.get("payload", {}).get("pnl_eur", 0.0)) < 0.0)
    ending_equity = initial_equity
    max_drawdown_pct = 0.0
    if exits:
        last_payload = exits[-1].get("payload", {}) or {}
        ending_equity = float(last_payload.get("portfolio_equity", initial_equity))
        max_drawdown_pct = max(float(event.get("payload", {}).get("portfolio_max_drawdown_pct", 0.0)) for event in exits)
    wins = sum(1 for event in exits if float(event.get("payload", {}).get("pnl_eur", 0.0)) > 0.0)
    losses = sum(1 for event in exits if float(event.get("payload", {}).get("pnl_eur", 0.0)) < 0.0)
    closed_trades = len(exits)
    avg_hold = (
        sum(float(event.get("payload", {}).get("hold_minutes", 0.0)) for event in exits) / closed_trades
        if closed_trades
        else 0.0
    )
    avg_mae_r = (
        sum(float(event.get("payload", {}).get("mae_r", 0.0)) for event in exits) / closed_trades
        if closed_trades
        else 0.0
    )
    avg_mfe_r = (
        sum(float(event.get("payload", {}).get("mfe_r", 0.0)) for event in exits) / closed_trades
        if closed_trades
        else 0.0
    )
    avg_total_fee_eur = (
        sum(float(event.get("payload", {}).get("total_fee_eur", 0.0)) for event in exits) / closed_trades
        if closed_trades
        else 0.0
    )
    avg_total_slippage_bps = (
        sum(
            float(event.get("payload", {}).get("entry_slippage_bps", 0.0))
            + float(event.get("payload", {}).get("exit_slippage_bps", 0.0))
            for event in exits
        )
        / closed_trades
        if closed_trades
        else 0.0
    )
    if gross_loss == 0.0:
        profit_factor = float("inf") if gross_profit > 0.0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss
    return ShadowPortfolioSummary(
        name=name,
        initial_equity_eur=round(initial_equity, 4),
        ending_equity=round(ending_equity, 4),
        net_pnl_eur=round(ending_equity - initial_equity, 4),
        closed_trades=closed_trades,
        win_rate=(wins / closed_trades) if closed_trades else 0.0,
        profit_factor=profit_factor,
        max_drawdown_pct=max_drawdown_pct,
        average_hold_minutes=round(avg_hold, 2),
        average_mae_r=round(avg_mae_r, 4),
        average_mfe_r=round(avg_mfe_r, 4),
        average_total_fee_eur=round(avg_total_fee_eur, 4),
        average_total_slippage_bps=round(avg_total_slippage_bps, 4),
    )


def _equity_curve_points(events: list[dict[str, Any]], initial_equity: float) -> list[dict[str, Any]]:
    points = [{"label": "start", "value": round(initial_equity, 4)}]
    for event in events:
        if event.get("event_type") not in {"shadow_exit_sent", "shadow_kill_switch_exit"}:
            continue
        payload = event.get("payload", {}) or {}
        ts = payload.get("market_ts") or event.get("ts")
        parsed = _parse_ts(ts)
        label = parsed.strftime("%d.%m %H:%M") if parsed is not None else "n/a"
        points.append(
            {
                "label": label,
                "value": round(float(payload.get("portfolio_equity", initial_equity)), 4),
            }
        )
    return points


def _map_shadow_event(
    event: dict[str, Any],
    *,
    portfolio_name: str,
    initial_equity: float,
    ending_equity: float,
    max_drawdown_pct: float,
) -> tuple[str, dict[str, Any]] | None:
    event_type = str(event.get("event_type", ""))
    payload = event.get("payload", {}) or {}
    mapped_type = {
        "entry_sent": "shadow_entry_sent",
        "entry_rejected": "shadow_entry_rejected",
        "exit_sent": "shadow_exit_sent",
        "kill_switch_exit": "shadow_kill_switch_exit",
    }.get(event_type)
    if mapped_type is None:
        return None
    mapped_payload = dict(payload)
    mapped_payload.update(
        {
            "portfolio_name": portfolio_name,
            "portfolio_initial_equity": initial_equity,
            "portfolio_equity": round(ending_equity, 4),
            "portfolio_max_drawdown_pct": max_drawdown_pct,
        }
    )
    return mapped_type, mapped_payload


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


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
