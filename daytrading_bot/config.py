from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, replace
from datetime import time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .runtime_layout import build_runtime_paths, ensure_runtime_dirs


def load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=1), name=name)


@dataclass(frozen=True)
class SessionWindow:
    start: time
    end: time


@dataclass(frozen=True)
class PairConfig:
    symbol: str
    tv_exchange: str
    tv_instrument: str
    min_notional_eur: float = 10.0
    paper_entry_maker_probability_cap: float = 0.35
    paper_exit_maker_probability_cap: float = 0.10
    paper_entry_slippage_spread_weight: float = 0.35
    paper_exit_slippage_spread_weight: float = 0.45
    paper_min_entry_slippage_bps: float = 0.35
    paper_min_exit_slippage_bps: float = 0.55


DEFAULT_PAIRS: tuple[PairConfig, ...] = (
    PairConfig(
        symbol="XBTEUR",
        tv_exchange="KRAKEN",
        tv_instrument="XBTEUR",
        paper_entry_maker_probability_cap=0.55,
        paper_exit_maker_probability_cap=0.20,
        paper_entry_slippage_spread_weight=0.16,
        paper_exit_slippage_spread_weight=0.24,
        paper_min_entry_slippage_bps=0.12,
        paper_min_exit_slippage_bps=0.22,
    ),
    PairConfig(
        symbol="ETHEUR",
        tv_exchange="KRAKEN",
        tv_instrument="ETHEUR",
        paper_entry_maker_probability_cap=0.52,
        paper_exit_maker_probability_cap=0.18,
        paper_entry_slippage_spread_weight=0.18,
        paper_exit_slippage_spread_weight=0.26,
        paper_min_entry_slippage_bps=0.14,
        paper_min_exit_slippage_bps=0.24,
    ),
    PairConfig(
        symbol="SOLEUR",
        tv_exchange="KRAKEN",
        tv_instrument="SOLEUR",
        paper_entry_maker_probability_cap=0.40,
        paper_exit_maker_probability_cap=0.12,
        paper_entry_slippage_spread_weight=0.26,
        paper_exit_slippage_spread_weight=0.34,
        paper_min_entry_slippage_bps=0.24,
        paper_min_exit_slippage_bps=0.38,
    ),
    PairConfig(
        symbol="XRPEUR",
        tv_exchange="KRAKEN",
        tv_instrument="XRPEUR",
        paper_entry_maker_probability_cap=0.30,
        paper_exit_maker_probability_cap=0.08,
        paper_entry_slippage_spread_weight=0.42,
        paper_exit_slippage_spread_weight=0.52,
        paper_min_entry_slippage_bps=0.48,
        paper_min_exit_slippage_bps=0.70,
    ),
    PairConfig(
        symbol="LTCEUR",
        tv_exchange="KRAKEN",
        tv_instrument="LTCEUR",
        paper_entry_maker_probability_cap=0.38,
        paper_exit_maker_probability_cap=0.12,
        paper_entry_slippage_spread_weight=0.28,
        paper_exit_slippage_spread_weight=0.36,
        paper_min_entry_slippage_bps=0.24,
        paper_min_exit_slippage_bps=0.40,
    ),
    PairConfig(
        symbol="XDGEUR",
        tv_exchange="KRAKEN",
        tv_instrument="XDGEUR",
        paper_entry_maker_probability_cap=0.24,
        paper_exit_maker_probability_cap=0.05,
        paper_entry_slippage_spread_weight=0.55,
        paper_exit_slippage_spread_weight=0.72,
        paper_min_entry_slippage_bps=0.65,
        paper_min_exit_slippage_bps=0.95,
    ),
    PairConfig(
        symbol="ADAEUR",
        tv_exchange="KRAKEN",
        tv_instrument="ADAEUR",
        paper_entry_maker_probability_cap=0.28,
        paper_exit_maker_probability_cap=0.08,
        paper_entry_slippage_spread_weight=0.45,
        paper_exit_slippage_spread_weight=0.58,
        paper_min_entry_slippage_bps=0.50,
        paper_min_exit_slippage_bps=0.78,
    ),
    PairConfig(
        symbol="LINKEUR",
        tv_exchange="KRAKEN",
        tv_instrument="LINKEUR",
        paper_entry_maker_probability_cap=0.32,
        paper_exit_maker_probability_cap=0.09,
        paper_entry_slippage_spread_weight=0.38,
        paper_exit_slippage_spread_weight=0.50,
        paper_min_entry_slippage_bps=0.42,
        paper_min_exit_slippage_bps=0.68,
    ),
    PairConfig(
        symbol="DOTEUR",
        tv_exchange="KRAKEN",
        tv_instrument="DOTEUR",
        paper_entry_maker_probability_cap=0.30,
        paper_exit_maker_probability_cap=0.08,
        paper_entry_slippage_spread_weight=0.42,
        paper_exit_slippage_spread_weight=0.54,
        paper_min_entry_slippage_bps=0.48,
        paper_min_exit_slippage_bps=0.72,
    ),
    PairConfig(
        symbol="TRXEUR",
        tv_exchange="KRAKEN",
        tv_instrument="TRXEUR",
        paper_entry_maker_probability_cap=0.28,
        paper_exit_maker_probability_cap=0.08,
        paper_entry_slippage_spread_weight=0.40,
        paper_exit_slippage_spread_weight=0.52,
        paper_min_entry_slippage_bps=0.46,
        paper_min_exit_slippage_bps=0.70,
    ),
    PairConfig(
        symbol="ATOMEUR",
        tv_exchange="KRAKEN",
        tv_instrument="ATOMEUR",
        paper_entry_maker_probability_cap=0.30,
        paper_exit_maker_probability_cap=0.08,
        paper_entry_slippage_spread_weight=0.38,
        paper_exit_slippage_spread_weight=0.50,
        paper_min_entry_slippage_bps=0.44,
        paper_min_exit_slippage_bps=0.68,
    ),
    PairConfig(
        symbol="FETEUR",
        tv_exchange="KRAKEN",
        tv_instrument="FETEUR",
        paper_entry_maker_probability_cap=0.22,
        paper_exit_maker_probability_cap=0.05,
        paper_entry_slippage_spread_weight=0.58,
        paper_exit_slippage_spread_weight=0.78,
        paper_min_entry_slippage_bps=0.72,
        paper_min_exit_slippage_bps=1.10,
    ),
)


@dataclass(frozen=True)
class ThreeCommasConfig:
    secret: str = ""
    bot_uuid: str = ""
    webhook_url: str = "https://api.3commas.io/signal_bots/webhooks"
    max_lag_seconds: int = 30
    order_currency_type: str = "quote"
    entry_order_type: str = "market"
    mode: str = "paper"
    allow_live: bool = False

    @property
    def dry_run(self) -> bool:
        return self.mode != "live"

    def with_mode(self, mode: str) -> "ThreeCommasConfig":
        normalized = mode.lower()
        if normalized not in {"paper", "live"}:
            normalized = "paper"
        return replace(self, mode=normalized)


@dataclass(frozen=True)
class BotConfig:
    timezone_name: str = "Europe/Berlin"
    initial_equity_eur: float = 100.0
    max_position_fraction: float = 0.80
    base_risk_per_trade_pct: float = 0.009
    reduced_risk_per_trade_pct: float = 0.0045
    daily_loss_limit_pct: float = 0.018
    max_drawdown_pct: float = 0.05
    warning_drawdown_pct: float = 0.025
    reduced_drawdown_pct: float = 0.035
    read_only_drawdown_pct: float = 0.042
    max_trades_per_day: int = 3
    consecutive_losses_limit: int = 2
    min_win_rate_gate: float = 0.55
    min_profit_factor_gate: float = 1.30
    hard_flat_time: time = time(hour=21, minute=30)
    trade_windows: tuple[SessionWindow, ...] = (
        SessionWindow(time(hour=8, minute=0), time(hour=11, minute=30)),
        SessionWindow(time(hour=14, minute=30), time(hour=18, minute=30)),
    )
    min_spread_bps: float = 0.0
    max_spread_bps: float = 12.0
    min_volume_zscore: float = 1.5
    breakout_volume_zscore: float = 2.0
    opening_range_bars_5m: int = 6
    opening_range_volume_zscore: float = 1.8
    opening_range_max_hold_minutes: int = 110
    min_adx_15m: float = 20.0
    trend_pullback_min_adx_15m: float = 22.0
    trend_pullback_volume_zscore: float = 1.2
    trend_pullback_max_hold_minutes: int = 105
    recovery_min_adx_15m: float = 16.0
    recovery_max_ema_gap_pct: float = 0.008
    recovery_atr_percentile_min: float = 35.0
    recovery_atr_percentile_max: float = 95.0
    recovery_compression_atr_multiple: float = 2.8
    recovery_ema_reclaim_buffer_atr: float = 0.25
    recovery_price_reclaim_buffer_atr: float = 0.10
    recovery_min_score: float = 55.0
    recovery_stop_atr_multiple: float = 0.9
    recovery_max_stop_pct: float = 0.010
    recovery_max_hold_minutes: int = 75
    recovery_break_even_trigger_r: float = 0.60
    recovery_trail_activation_r: float = 0.90
    recovery_time_decay_minutes: int = 30
    recovery_time_decay_min_r: float = 0.15
    quality_a_min_score: float = 72.0
    quality_b_min_score: float = 58.0
    live_recovery_min_quality: str = "A"
    max_stop_pct: float = 0.0125
    stop_atr_multiple: float = 1.1
    trail_activation_r: float = 1.4
    break_even_trigger_r: float = 1.0
    break_even_fee_buffer_pct: float = 0.0010
    trail_atr_multiple: float = 0.8
    max_hold_minutes: int = 120
    paper_maker_fee_rate: float = 0.0025
    paper_taker_fee_rate: float = 0.0040
    paper_entry_maker_probability_cap: float = 0.35
    paper_exit_maker_probability_cap: float = 0.10
    paper_entry_slippage_spread_weight: float = 0.35
    paper_exit_slippage_spread_weight: float = 0.45
    paper_min_entry_slippage_bps: float = 0.35
    paper_min_exit_slippage_bps: float = 0.55
    atr_percentile_min: float = 55.0
    atr_percentile_max: float = 90.0
    shock_candle_atr_multiple: float = 1.8
    quote_fee_rate: float = 0.0040
    pairs: tuple[PairConfig, ...] = DEFAULT_PAIRS
    shadow_portfolio_sizes_eur: tuple[float, ...] = (50.0, 100.0, 250.0, 500.0, 1000.0)
    telemetry_path: str = "logs/trading_events.jsonl"
    strategy_lab_state_path: str = "logs/strategy_lab_state.json"
    active_strategy_id: str = "champion_breakout"
    strategy_lab_min_closed_trades: int = 6
    strategy_lab_min_profit_factor: float = 1.10
    strategy_lab_min_win_rate: float = 0.48
    strategy_lab_min_expectancy_eur: float = 0.0
    strategy_lab_max_drawdown_pct: float = 0.035
    strategy_lab_min_distinct_regimes: int = 2
    strategy_lab_min_trades_per_regime: int = 2
    strategy_lab_max_regime_concentration: float = 0.75
    strategy_lab_min_distinct_assets: int = 2
    strategy_lab_min_trades_per_asset: int = 2
    strategy_lab_max_asset_concentration: float = 0.75
    strategy_lab_promotion_score_margin: float = 0.15
    strategy_lab_promotion_cooldown_hours: int = 24
    strategy_lab_pinned_paper_strategy_id: str = ""
    strategy_lab_rollback_min_closed_trades: int = 4
    strategy_lab_rollback_min_profit_factor: float = 0.95
    strategy_lab_rollback_max_drawdown_pct: float = 0.045
    allow_live_strategy_promotion: bool = False
    forward_test_min_trades: int = 30
    calibration_min_trades: int = 3

    @property
    def timezone(self) -> tzinfo:
        return load_timezone(self.timezone_name)

    def pair_by_symbol(self, symbol: str) -> PairConfig:
        for pair in self.pairs:
            if pair.symbol == symbol:
                return pair
        raise KeyError(symbol)

    def classify_quality(self, score: float) -> str:
        if score >= self.quality_a_min_score:
            return "A"
        if score >= self.quality_b_min_score:
            return "B"
        return "C"

    @staticmethod
    def quality_rank(quality: str) -> int:
        return {"A": 3, "B": 2, "C": 1}.get(quality.upper(), 0)

    def meets_quality(self, actual: str, minimum: str) -> bool:
        return self.quality_rank(actual) >= self.quality_rank(minimum)


def load_config_from_env(
    project_root: str | Path | None = None,
    device_id: str | None = None,
) -> tuple[BotConfig, ThreeCommasConfig]:
    runtime_paths = ensure_runtime_dirs(build_runtime_paths(project_root=project_root, device_id=device_id))
    bot_config = BotConfig(
        pairs=_resolve_pairs_from_env(os.getenv("BOT_PAIRS", "")),
        active_strategy_id=os.getenv("BOT_ACTIVE_STRATEGY", "champion_breakout").strip() or "champion_breakout",
        allow_live_strategy_promotion=os.getenv("BOT_ALLOW_LIVE_STRATEGY_PROMOTION", "false").lower() == "true",
        strategy_lab_pinned_paper_strategy_id=os.getenv("BOT_PINNED_PAPER_STRATEGY", "").strip(),
        telemetry_path=os.getenv("BOT_TELEMETRY_PATH", "").strip() or runtime_paths.telemetry_path,
        strategy_lab_state_path=os.getenv("BOT_STRATEGY_LAB_STATE_PATH", "").strip() or runtime_paths.strategy_lab_state_path,
    )
    mode = os.getenv("BOT_MODE", "paper").lower()
    if mode not in {"paper", "live"}:
        mode = "paper"
    execution_config = ThreeCommasConfig(
        secret=os.getenv("THREE_COMMAS_SECRET", ""),
        bot_uuid=os.getenv("THREE_COMMAS_BOT_UUID", ""),
        webhook_url=os.getenv(
            "THREE_COMMAS_WEBHOOK_URL",
            "https://api.3commas.io/signal_bots/webhooks",
        ),
        mode=mode,
        allow_live=os.getenv("BOT_ALLOW_LIVE", "false").lower() == "true",
    )
    return bot_config, execution_config


def _resolve_pairs_from_env(value: str) -> tuple[PairConfig, ...]:
    requested = [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]
    if not requested:
        return DEFAULT_PAIRS

    by_symbol = {pair.symbol: pair for pair in DEFAULT_PAIRS}
    selected = tuple(by_symbol[symbol] for symbol in requested if symbol in by_symbol)
    return selected or DEFAULT_PAIRS
