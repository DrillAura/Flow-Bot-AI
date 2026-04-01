from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import BotConfig, ThreeCommasConfig
from .telemetry import InMemoryTelemetry, JsonlTelemetry


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    label: str
    family: str
    strategy_type: str
    description: str
    config_overrides: dict[str, float | int | str]
    promotion_allowed: bool = True


@dataclass(frozen=True)
class StrategyGateResult:
    name: str
    passed: bool
    actual: float | int | str
    threshold: str


@dataclass(frozen=True)
class StrategyPerformanceSummary:
    strategy_id: str
    label: str
    family: str
    strategy_type: str
    closed_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    expectancy_eur: float
    net_pnl_eur: float
    max_drawdown_pct: float
    average_hold_minutes: float
    distinct_regimes: int
    dominant_regime_share: float
    distinct_assets: int
    dominant_asset_share: float
    score: float
    gates: dict[str, StrategyGateResult]
    eligible_for_promotion: bool
    latest_activity_ts: str | None
    regime_breakdown: list[dict[str, Any]]
    asset_breakdown: list[dict[str, Any]]
    setup_breakdown: list[dict[str, Any]]


@dataclass(frozen=True)
class StrategyPromotionReview:
    source_exists: bool
    generated_at: str
    current_paper_strategy_id: str
    current_live_strategy_id: str
    recommended_paper_strategy_id: str
    recommended_live_strategy_id: str
    paper_promotion_applied: bool
    live_promotion_applied: bool
    promotion_reason: str
    previous_paper_strategy_id: str | None
    current_paper_promoted_at: str | None
    paper_promotion_cooldown_until: str | None
    rollback_applied: bool
    pinned_paper_strategy_id: str | None
    strategies: list[StrategyPerformanceSummary]


def build_strategy_specs() -> list[StrategySpec]:
    return [
        StrategySpec(
            strategy_id="champion_breakout",
            label="Champion Breakout",
            family="breakout_recovery",
            strategy_type="breakout_recovery",
            description="Current champion using breakout/pullback and recovery reclaim rules.",
            config_overrides={},
        ),
        StrategySpec(
            strategy_id="breakout_conservative",
            label="Breakout Conservative",
            family="breakout_recovery",
            strategy_type="breakout_recovery",
            description="Tighter spread and stronger breakout confirmation before entry.",
            config_overrides={
                "max_spread_bps": 8.0,
                "breakout_volume_zscore": 2.4,
                "atr_percentile_min": 60.0,
                "trail_activation_r": 1.6,
            },
        ),
        StrategySpec(
            strategy_id="trend_follow_throttle",
            label="Trend Follow",
            family="breakout_recovery",
            strategy_type="breakout_recovery",
            description="More trend-selective breakout lane with slightly longer hold time.",
            config_overrides={
                "min_adx_15m": 24.0,
                "breakout_volume_zscore": 2.2,
                "max_hold_minutes": 150,
                "trail_activation_r": 1.2,
            },
        ),
        StrategySpec(
            strategy_id="recovery_hunter",
            label="Recovery Hunter",
            family="breakout_recovery",
            strategy_type="breakout_recovery",
            description="Looser recovery reclaim lane that hunts compressed reclaim setups.",
            config_overrides={
                "recovery_min_score": 48.0,
                "recovery_break_even_trigger_r": 0.5,
                "recovery_trail_activation_r": 0.8,
                "recovery_time_decay_minutes": 25,
            },
        ),
        StrategySpec(
            strategy_id="mean_reversion_vwap",
            label="VWAP Mean Reversion",
            family="mean_reversion",
            strategy_type="mean_reversion_vwap",
            description="Fade-and-reclaim strategy that buys a clean VWAP recovery after short dislocation.",
            config_overrides={},
        ),
        StrategySpec(
            strategy_id="opening_range_breakout",
            label="Opening Range Breakout",
            family="opening_range",
            strategy_type="opening_range_breakout",
            description="Session-aware challenger that trades a clean breakout through the opening range high.",
            config_overrides={},
        ),
        StrategySpec(
            strategy_id="trend_continuation_pullback",
            label="Trend Continuation Pullback",
            family="trend_continuation",
            strategy_type="trend_continuation_pullback",
            description="Continuation challenger that buys pullbacks back into trend support before the next expansion leg.",
            config_overrides={},
        ),
        StrategySpec(
            strategy_id="fast_imbalance_scalp",
            label="Fast Imbalance Scalp",
            family="fast_trading",
            strategy_type="fast_micro_scalp",
            description="Research-only micro scalp lane using 1S/5S thrust, spread compression and orderbook imbalance.",
            config_overrides={},
            promotion_allowed=False,
        ),
        StrategySpec(
            strategy_id="fast_imbalance_scalp_tight",
            label="Fast Imbalance Scalp Tight",
            family="fast_trading",
            strategy_type="fast_micro_scalp",
            description="Tighter fast lane variant with stronger micro thrust and faster time decay.",
            config_overrides={
                "fast_min_change_1s_bps": 2.2,
                "fast_min_change_5s_bps": 4.2,
                "fast_max_hold_minutes": 8,
                "fast_time_decay_minutes": 4,
                "fast_time_decay_min_r": 0.05,
            },
            promotion_allowed=False,
        ),
        StrategySpec(
            strategy_id="fast_liquidity_sweep_reclaim",
            label="Fast Liquidity Sweep Reclaim",
            family="fast_trading",
            strategy_type="fast_liquidity_sweep_reclaim",
            description="Research-only micro lane that buys a fast reclaim after a short liquidity sweep below local 1m lows.",
            config_overrides={},
            promotion_allowed=False,
        ),
        StrategySpec(
            strategy_id="fast_vwap_reclaim_scalp",
            label="Fast VWAP Reclaim Scalp",
            family="fast_trading",
            strategy_type="fast_vwap_reclaim_scalp",
            description="Research-only micro lane that buys a clean 1m VWAP reclaim with short-term thrust and supportive imbalance.",
            config_overrides={},
            promotion_allowed=False,
        ),
        StrategySpec(
            strategy_id="fast_failed_breakout_reclaim_micro",
            label="Fast Failed Breakout Reclaim",
            family="fast_trading",
            strategy_type="fast_failed_breakout_reclaim_micro",
            description="Research-only micro lane that waits for a failed 1m breakout and then buys the reclaim with micro thrust.",
            config_overrides={},
            promotion_allowed=False,
        ),
        StrategySpec(
            strategy_id="fast_liquidity_sweep_reversal",
            label="Fast Liquidity Sweep Reversal",
            family="fast_trading",
            strategy_type="fast_liquidity_sweep_reversal",
            description="Research-only micro lane that demands a stronger close after a short sweep below local lows.",
            config_overrides={},
            promotion_allowed=False,
        ),
    ]


def build_strategy(strategy_spec: StrategySpec, bot_config: BotConfig):
    from .strategy import (
        BreakoutPullbackStrategy,
        FastFailedBreakoutReclaimMicroStrategy,
        FastLiquiditySweepReclaimStrategy,
        FastLiquiditySweepReversalStrategy,
        FastMicroScalpStrategy,
        FastVwapReclaimScalpStrategy,
        MeanReversionVwapStrategy,
        OpeningRangeBreakoutStrategy,
        TrendContinuationPullbackStrategy,
    )

    if strategy_spec.strategy_type == "mean_reversion_vwap":
        return MeanReversionVwapStrategy(
            bot_config,
            strategy_id=strategy_spec.strategy_id,
            strategy_family=strategy_spec.family,
        )
    if strategy_spec.strategy_type == "opening_range_breakout":
        return OpeningRangeBreakoutStrategy(
            bot_config,
            strategy_id=strategy_spec.strategy_id,
            strategy_family=strategy_spec.family,
        )
    if strategy_spec.strategy_type == "trend_continuation_pullback":
        return TrendContinuationPullbackStrategy(
            bot_config,
            strategy_id=strategy_spec.strategy_id,
            strategy_family=strategy_spec.family,
        )
    if strategy_spec.strategy_type == "fast_micro_scalp":
        return FastMicroScalpStrategy(
            bot_config,
            strategy_id=strategy_spec.strategy_id,
            strategy_family=strategy_spec.family,
        )
    if strategy_spec.strategy_type == "fast_liquidity_sweep_reclaim":
        return FastLiquiditySweepReclaimStrategy(
            bot_config,
            strategy_id=strategy_spec.strategy_id,
            strategy_family=strategy_spec.family,
        )
    if strategy_spec.strategy_type == "fast_vwap_reclaim_scalp":
        return FastVwapReclaimScalpStrategy(
            bot_config,
            strategy_id=strategy_spec.strategy_id,
            strategy_family=strategy_spec.family,
        )
    if strategy_spec.strategy_type == "fast_failed_breakout_reclaim_micro":
        return FastFailedBreakoutReclaimMicroStrategy(
            bot_config,
            strategy_id=strategy_spec.strategy_id,
            strategy_family=strategy_spec.family,
        )
    if strategy_spec.strategy_type == "fast_liquidity_sweep_reversal":
        return FastLiquiditySweepReversalStrategy(
            bot_config,
            strategy_id=strategy_spec.strategy_id,
            strategy_family=strategy_spec.family,
        )
    return BreakoutPullbackStrategy(
        bot_config,
        strategy_id=strategy_spec.strategy_id,
        strategy_family=strategy_spec.family,
    )


def resolve_strategy_spec(strategy_id: str) -> StrategySpec | None:
    for spec in build_strategy_specs():
        if spec.strategy_id == strategy_id:
            return spec
    return None


class StrategyRuntimeSelector:
    def __init__(self, bot_config: BotConfig, execution_config: ThreeCommasConfig) -> None:
        self.base_config = bot_config
        self.execution_config = execution_config
        self.state_path = Path(bot_config.strategy_lab_state_path)
        self.active_strategy_id = bot_config.active_strategy_id
        self._last_state_mtime: float | None = None
        self.strategy = self._build_strategy_from_id(self.active_strategy_id)

    def maybe_refresh(self, active_trade_present: bool) -> None:
        if active_trade_present or not self.state_path.exists():
            return
        stat = self.state_path.stat()
        if self._last_state_mtime is not None and stat.st_mtime <= self._last_state_mtime:
            return
        self._last_state_mtime = stat.st_mtime
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return

        if self.execution_config.mode == "live":
            target_strategy_id = str(payload.get("current_live_strategy_id") or self.active_strategy_id)
        else:
            target_strategy_id = str(payload.get("current_paper_strategy_id") or self.active_strategy_id)

        if target_strategy_id and target_strategy_id != self.active_strategy_id:
            self.active_strategy_id = target_strategy_id
            self.strategy = self._build_strategy_from_id(target_strategy_id)

    def _build_strategy_from_id(self, strategy_id: str):
        strategy_spec = resolve_strategy_spec(strategy_id) or build_strategy_specs()[0]
        strategy_config = replace(self.base_config, **strategy_spec.config_overrides)
        return build_strategy(strategy_spec, strategy_config)


class StrategyPaperLab:
    def __init__(self, bot_config: BotConfig, telemetry: JsonlTelemetry) -> None:
        self.bot_config = bot_config
        self.telemetry = telemetry
        self.runners = [self._build_runner(spec) for spec in build_strategy_specs()]

    def process_market(self, contexts, moment: datetime) -> None:
        for runner in self.runners:
            before = len(runner["telemetry"].events)
            engine = runner["engine"]
            engine.process_market(contexts, available_eur=engine.risk.state.equity, moment=moment)
            for event in runner["telemetry"].events[before:]:
                mapped = _map_strategy_lab_event(
                    event,
                    strategy_spec=runner["spec"],
                    ending_equity=engine.risk.state.equity,
                    max_drawdown_pct=engine.risk.max_drawdown_pct,
                )
                if mapped is None:
                    continue
                event_type, payload = mapped
                self.telemetry.log(event_type, payload, event_ts=moment)

    def _build_runner(self, spec: StrategySpec) -> dict[str, Any]:
        from .engine import BotEngine

        strategy_config = replace(self.bot_config, **spec.config_overrides)
        strategy_telemetry = InMemoryTelemetry()
        engine = BotEngine(
            strategy_config,
            ThreeCommasConfig(mode="paper", allow_live=False),
            telemetry=strategy_telemetry,
            enable_research=False,
            strategy=build_strategy(spec, strategy_config),
        )
        return {
            "spec": spec,
            "telemetry": strategy_telemetry,
            "engine": engine,
        }


def review_strategy_lab(telemetry_path: Path, bot_config: BotConfig) -> StrategyPromotionReview:
    specs = {spec.strategy_id: spec for spec in build_strategy_specs()}
    events = _load_events(telemetry_path)
    strategy_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        event_type = str(event.get("event_type", ""))
        if not event_type.startswith("strategy_lab_"):
            continue
        payload = event.get("payload", {}) or {}
        strategy_id = str(payload.get("strategy_id", "unknown"))
        strategy_events[strategy_id].append(event)

    existing_state = _load_state(Path(bot_config.strategy_lab_state_path))
    current_paper = str(existing_state.get("current_paper_strategy_id") or bot_config.active_strategy_id)
    current_live = str(existing_state.get("current_live_strategy_id") or bot_config.active_strategy_id)
    previous_paper = existing_state.get("previous_paper_strategy_id")
    current_paper_promoted_at = _parse_iso(existing_state.get("current_paper_promoted_at"))
    cooldown_until = _parse_iso(existing_state.get("paper_promotion_cooldown_until"))
    now = datetime.now(timezone.utc)
    pinned_paper = str(bot_config.strategy_lab_pinned_paper_strategy_id or "").strip() or None

    summaries: list[StrategyPerformanceSummary] = []
    for strategy_id, spec in specs.items():
        summaries.append(_summarize_strategy(spec, strategy_events.get(strategy_id, []), bot_config))
    summaries.sort(key=lambda item: item.score, reverse=True)

    eligible = [summary for summary in summaries if summary.eligible_for_promotion]
    current_summary = next((summary for summary in summaries if summary.strategy_id == current_paper), None)
    current_score = current_summary.score if current_summary is not None else float("-inf")

    recommended_paper = current_paper
    recommended_live = current_live
    paper_promoted = False
    live_promoted = False
    rollback_applied = False
    promotion_reason = "no_eligible_challenger"

    if pinned_paper:
        recommended_paper = pinned_paper
        promotion_reason = f"paper_pinned_to_{pinned_paper}"
    elif _should_rollback_current(current_summary, bot_config) and previous_paper:
        recommended_paper = str(previous_paper)
        paper_promoted = recommended_paper != current_paper
        rollback_applied = paper_promoted
        promotion_reason = f"paper_rollback_to_{recommended_paper}"
    elif cooldown_until is not None and now < cooldown_until:
        promotion_reason = "promotion_cooldown_active"
    elif eligible:
        best = eligible[0]
        score_margin = best.score - current_score
        if best.strategy_id != current_paper and score_margin >= bot_config.strategy_lab_promotion_score_margin:
            recommended_paper = best.strategy_id
            paper_promoted = True
            promotion_reason = f"paper_promoted_to_{best.strategy_id}"
        elif current_summary is not None and current_summary.eligible_for_promotion:
            promotion_reason = "current_champion_retained"
        else:
            recommended_paper = best.strategy_id
            promotion_reason = f"paper_recommended_{best.strategy_id}"

        if bot_config.allow_live_strategy_promotion and recommended_paper != current_live:
            recommended_live = recommended_paper
            live_promoted = True
            promotion_reason = f"{promotion_reason}_and_live_synced"

    if paper_promoted:
        current_paper_promoted_at = now
        cooldown_until = now + timedelta(hours=bot_config.strategy_lab_promotion_cooldown_hours)
        previous_paper = current_paper
    elif current_paper_promoted_at is None:
        current_paper_promoted_at = now
        cooldown_until = now + timedelta(hours=bot_config.strategy_lab_promotion_cooldown_hours)

    review = StrategyPromotionReview(
        source_exists=telemetry_path.exists(),
        generated_at=now.isoformat(timespec="seconds"),
        current_paper_strategy_id=recommended_paper,
        current_live_strategy_id=recommended_live,
        recommended_paper_strategy_id=recommended_paper,
        recommended_live_strategy_id=recommended_live,
        paper_promotion_applied=paper_promoted,
        live_promotion_applied=live_promoted,
        promotion_reason=promotion_reason,
        previous_paper_strategy_id=str(previous_paper) if previous_paper else None,
        current_paper_promoted_at=current_paper_promoted_at.isoformat(timespec="seconds") if current_paper_promoted_at else None,
        paper_promotion_cooldown_until=cooldown_until.isoformat(timespec="seconds") if cooldown_until else None,
        rollback_applied=rollback_applied,
        pinned_paper_strategy_id=pinned_paper,
        strategies=summaries,
    )
    _write_state(Path(bot_config.strategy_lab_state_path), review)
    return review


def _summarize_strategy(spec: StrategySpec, events: list[dict[str, Any]], bot_config: BotConfig) -> StrategyPerformanceSummary:
    exits = [event for event in events if event.get("event_type") in {"strategy_lab_exit_sent", "strategy_lab_kill_switch_exit"}]
    closed_trades = len(exits)
    wins = sum(1 for event in exits if float(event.get("payload", {}).get("pnl_eur", 0.0)) > 0.0)
    losses = sum(1 for event in exits if float(event.get("payload", {}).get("pnl_eur", 0.0)) < 0.0)
    gross_profit = sum(float(event.get("payload", {}).get("pnl_eur", 0.0)) for event in exits if float(event.get("payload", {}).get("pnl_eur", 0.0)) > 0.0)
    gross_loss = sum(abs(float(event.get("payload", {}).get("pnl_eur", 0.0))) for event in exits if float(event.get("payload", {}).get("pnl_eur", 0.0)) < 0.0)
    net_pnl = gross_profit - gross_loss
    expectancy = (net_pnl / closed_trades) if closed_trades else 0.0
    avg_hold = (
        sum(float(event.get("payload", {}).get("hold_minutes", 0.0)) for event in exits) / closed_trades
        if closed_trades
        else 0.0
    )
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    max_drawdown_pct = max((float(event.get("payload", {}).get("strategy_max_drawdown_pct", 0.0)) for event in exits), default=0.0)
    latest_activity_ts = max((event.get("ts") for event in events), default=None)

    regime_counter: Counter[str] = Counter()
    asset_counter: Counter[str] = Counter()
    setup_counter: Counter[str] = Counter()
    for event in exits:
        payload = event.get("payload", {}) or {}
        regime_counter[str(payload.get("regime_label", "unknown"))] += 1
        asset_counter[str(payload.get("pair", "unknown"))] += 1
        setup_counter[str(payload.get("setup_type", "unknown"))] += 1

    known_regime_counts = [count for label, count in regime_counter.items() if label and label != "unknown"]
    distinct_regimes = len(known_regime_counts)
    min_regime_trades = min(known_regime_counts, default=0)
    dominant_regime_share = (max(known_regime_counts) / closed_trades) if known_regime_counts and closed_trades else 0.0
    known_asset_counts = [count for label, count in asset_counter.items() if label and label != "unknown"]
    distinct_assets = len(known_asset_counts)
    min_asset_trades = min(known_asset_counts, default=0)
    dominant_asset_share = (max(known_asset_counts) / closed_trades) if known_asset_counts and closed_trades else 0.0

    gates = {
        "closed_trades": StrategyGateResult(
            name="closed_trades",
            passed=closed_trades >= bot_config.strategy_lab_min_closed_trades,
            actual=closed_trades,
            threshold=f">= {bot_config.strategy_lab_min_closed_trades}",
        ),
        "profit_factor": StrategyGateResult(
            name="profit_factor",
            passed=profit_factor >= bot_config.strategy_lab_min_profit_factor,
            actual=round(profit_factor, 4),
            threshold=f">= {bot_config.strategy_lab_min_profit_factor:.2f}",
        ),
        "win_rate": StrategyGateResult(
            name="win_rate",
            passed=((wins / closed_trades) if closed_trades else 0.0) >= bot_config.strategy_lab_min_win_rate,
            actual=round((wins / closed_trades) if closed_trades else 0.0, 4),
            threshold=f">= {bot_config.strategy_lab_min_win_rate:.2f}",
        ),
        "expectancy_eur": StrategyGateResult(
            name="expectancy_eur",
            passed=expectancy >= bot_config.strategy_lab_min_expectancy_eur,
            actual=round(expectancy, 4),
            threshold=f">= {bot_config.strategy_lab_min_expectancy_eur:.2f}",
        ),
        "max_drawdown_pct": StrategyGateResult(
            name="max_drawdown_pct",
            passed=max_drawdown_pct <= bot_config.strategy_lab_max_drawdown_pct,
            actual=round(max_drawdown_pct, 4),
            threshold=f"<= {bot_config.strategy_lab_max_drawdown_pct:.4f}",
        ),
        "distinct_regimes": StrategyGateResult(
            name="distinct_regimes",
            passed=distinct_regimes >= bot_config.strategy_lab_min_distinct_regimes,
            actual=distinct_regimes,
            threshold=f">= {bot_config.strategy_lab_min_distinct_regimes}",
        ),
        "regime_trade_depth": StrategyGateResult(
            name="regime_trade_depth",
            passed=min_regime_trades >= bot_config.strategy_lab_min_trades_per_regime,
            actual=min_regime_trades,
            threshold=f">= {bot_config.strategy_lab_min_trades_per_regime}",
        ),
        "regime_concentration": StrategyGateResult(
            name="regime_concentration",
            passed=dominant_regime_share <= bot_config.strategy_lab_max_regime_concentration,
            actual=round(dominant_regime_share, 4),
            threshold=f"<= {bot_config.strategy_lab_max_regime_concentration:.2f}",
        ),
        "distinct_assets": StrategyGateResult(
            name="distinct_assets",
            passed=distinct_assets >= bot_config.strategy_lab_min_distinct_assets,
            actual=distinct_assets,
            threshold=f">= {bot_config.strategy_lab_min_distinct_assets}",
        ),
        "asset_trade_depth": StrategyGateResult(
            name="asset_trade_depth",
            passed=min_asset_trades >= bot_config.strategy_lab_min_trades_per_asset,
            actual=min_asset_trades,
            threshold=f">= {bot_config.strategy_lab_min_trades_per_asset}",
        ),
        "asset_concentration": StrategyGateResult(
            name="asset_concentration",
            passed=dominant_asset_share <= bot_config.strategy_lab_max_asset_concentration,
            actual=round(dominant_asset_share, 4),
            threshold=f"<= {bot_config.strategy_lab_max_asset_concentration:.2f}",
        ),
        "promotion_allowed": StrategyGateResult(
            name="promotion_allowed",
            passed=spec.promotion_allowed,
            actual=str(spec.promotion_allowed).lower(),
            threshold="true",
        ),
    }
    eligible = all(gate.passed for gate in gates.values())
    score = (
        expectancy * 120.0
        + min(profit_factor, 4.0) * 20.0
        + ((wins / closed_trades) if closed_trades else 0.0) * 15.0
        + net_pnl
        + min(distinct_regimes, 3) * 4.0
        + min(distinct_assets, 3) * 3.5
        + max(0.0, (1.0 - dominant_regime_share)) * 6.0
        + max(0.0, (1.0 - dominant_asset_share)) * 5.0
        - (max_drawdown_pct * 120.0)
        - max(bot_config.strategy_lab_min_closed_trades - closed_trades, 0) * 1.5
        - max(bot_config.strategy_lab_min_distinct_regimes - distinct_regimes, 0) * 3.0
        - max(bot_config.strategy_lab_min_trades_per_regime - min_regime_trades, 0) * 1.5
        - max(dominant_regime_share - bot_config.strategy_lab_max_regime_concentration, 0.0) * 18.0
        - max(bot_config.strategy_lab_min_distinct_assets - distinct_assets, 0) * 2.5
        - max(bot_config.strategy_lab_min_trades_per_asset - min_asset_trades, 0) * 1.25
        - max(dominant_asset_share - bot_config.strategy_lab_max_asset_concentration, 0.0) * 14.0
    )

    return StrategyPerformanceSummary(
        strategy_id=spec.strategy_id,
        label=spec.label,
        family=spec.family,
        strategy_type=spec.strategy_type,
        closed_trades=closed_trades,
        wins=wins,
        losses=losses,
        win_rate=(wins / closed_trades) if closed_trades else 0.0,
        profit_factor=profit_factor,
        expectancy_eur=expectancy,
        net_pnl_eur=net_pnl,
        max_drawdown_pct=max_drawdown_pct,
        average_hold_minutes=avg_hold,
        distinct_regimes=distinct_regimes,
        dominant_regime_share=dominant_regime_share,
        distinct_assets=distinct_assets,
        dominant_asset_share=dominant_asset_share,
        score=score,
        gates=gates,
        eligible_for_promotion=eligible,
        latest_activity_ts=latest_activity_ts,
        regime_breakdown=_counter_rows(regime_counter),
        asset_breakdown=_counter_rows(asset_counter),
        setup_breakdown=_counter_rows(setup_counter),
    )


def _map_strategy_lab_event(
    event: dict[str, Any],
    *,
    strategy_spec: StrategySpec,
    ending_equity: float,
    max_drawdown_pct: float,
) -> tuple[str, dict[str, Any]] | None:
    event_type = str(event.get("event_type", ""))
    payload = event.get("payload", {}) or {}
    mapped_type = {
        "entry_sent": "strategy_lab_entry_sent",
        "entry_rejected": "strategy_lab_entry_rejected",
        "exit_sent": "strategy_lab_exit_sent",
        "kill_switch_exit": "strategy_lab_kill_switch_exit",
    }.get(event_type)
    if mapped_type is None:
        return None
    mapped_payload = dict(payload)
    mapped_payload.update(
        {
            "strategy_id": strategy_spec.strategy_id,
            "strategy_label": strategy_spec.label,
            "strategy_family": strategy_spec.family,
            "strategy_type": strategy_spec.strategy_type,
            "strategy_equity": round(ending_equity, 4),
            "strategy_max_drawdown_pct": round(max_drawdown_pct, 6),
        }
    )
    return mapped_type, mapped_payload


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


def _counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    total = sum(counter.values())
    rows: list[dict[str, Any]] = []
    for label, value in counter.most_common():
        rows.append({"label": label, "value": value, "share": (value / total) if total else 0.0})
    return rows


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(path: Path, review: StrategyPromotionReview) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(review)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _parse_iso(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _should_rollback_current(summary: StrategyPerformanceSummary | None, bot_config: BotConfig) -> bool:
    if summary is None:
        return False
    if summary.closed_trades < bot_config.strategy_lab_rollback_min_closed_trades:
        return False
    if summary.max_drawdown_pct > bot_config.strategy_lab_rollback_max_drawdown_pct:
        return True
    if summary.profit_factor < bot_config.strategy_lab_rollback_min_profit_factor:
        return True
    if summary.expectancy_eur < bot_config.strategy_lab_min_expectancy_eur:
        return True
    return False
