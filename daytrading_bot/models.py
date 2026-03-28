from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class OrderBookSnapshot:
    symbol: str
    best_bid: float
    best_ask: float
    bid_volume_top5: float
    ask_volume_top5: float

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_bps(self) -> float:
        if self.mid_price <= 0:
            return 0.0
        return ((self.best_ask - self.best_bid) / self.mid_price) * 10_000

    @property
    def imbalance(self) -> float:
        if self.ask_volume_top5 <= 0:
            return 0.0
        return self.bid_volume_top5 / self.ask_volume_top5


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    candles_1m: Sequence[Candle]
    candles_5m: Sequence[Candle]
    candles_15m: Sequence[Candle]
    order_book: OrderBookSnapshot
    atr_pct_history_15m: Sequence[float]


@dataclass(frozen=True)
class VolatilitySnapshot:
    pair: str
    ts: datetime
    atr_pct_15m: float
    spread_bps: float
    vol_z_5m: float
    adx_15m: float
    ema20_15m: float
    ema50_15m: float
    vwap_dist_bps: float
    imbalance_1m: float


@dataclass(frozen=True)
class DayTradeIntent:
    pair: str
    entry_zone: float
    stop_price: float
    trail_activation_r: float
    max_hold_min: int
    budget_eur: float
    reason_code: str
    score: float
    quality: str
    setup_type: str = "breakout_pullback"
    regime_label: str = "bullish"
    strategy_id: str = "champion_breakout"
    strategy_family: str = "breakout_recovery"
    break_even_trigger_r: float = 1.0
    time_decay_minutes: int = 0
    time_decay_min_r: float = 0.0


@dataclass
class ActiveTrade:
    pair: str
    entry_ts: datetime
    entry_price: float
    initial_stop_price: float
    stop_price: float
    budget_eur: float
    reason_code: str
    max_hold_min: int
    trail_activation_r: float
    setup_type: str = "breakout_pullback"
    regime_label: str = "bullish"
    strategy_id: str = "champion_breakout"
    strategy_family: str = "breakout_recovery"
    quality: str = "B"
    score: float = 0.0
    break_even_trigger_r: float = 1.0
    time_decay_minutes: int = 0
    time_decay_min_r: float = 0.0
    entry_liquidity_role: str = "taker"
    exit_liquidity_role: str = "taker"
    entry_fee_rate: float = 0.0
    expected_exit_fee_rate: float = 0.0
    entry_fee_eur: float = 0.0
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0
    entry_maker_probability: float = 0.0
    exit_maker_probability: float = 0.0
    best_price_seen: float = 0.0
    worst_price_seen: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    replay_points: list[dict[str, float | str]] = field(default_factory=list)
    trailing_enabled: bool = False
    closed: bool = False

    @property
    def risk_per_unit(self) -> float:
        return max(self.entry_price - self.initial_stop_price, 1e-9)

    def r_multiple(self, market_price: float) -> float:
        return (market_price - self.entry_price) / self.risk_per_unit

    def unrealized_pnl(self, market_price: float, fee_rate: float | None = None) -> float:
        gross = self.budget_eur * ((market_price / self.entry_price) - 1.0)
        exit_fee_rate = self.expected_exit_fee_rate if fee_rate is None else fee_rate
        fees = self.entry_fee_eur + (self.budget_eur * exit_fee_rate)
        return gross - fees

    def update_extrema(self, high_price: float, low_price: float) -> None:
        if self.best_price_seen <= 0:
            self.best_price_seen = self.entry_price
        if self.worst_price_seen <= 0:
            self.worst_price_seen = self.entry_price
        self.best_price_seen = max(self.best_price_seen, high_price)
        self.worst_price_seen = min(self.worst_price_seen, low_price)
        self.mfe_r = max(self.mfe_r, self.r_multiple(self.best_price_seen))
        self.mae_r = min(self.mae_r, self.r_multiple(self.worst_price_seen))

    def append_replay_point(self, ts: datetime, market_price: float, realized_pnl_hint: float | None = None) -> None:
        point = {
            "ts": ts.isoformat(),
            "price": round(market_price, 8),
            "r_multiple": round(self.r_multiple(market_price), 6),
            "pnl_eur": round(self.unrealized_pnl(market_price), 6) if realized_pnl_hint is None else round(realized_pnl_hint, 6),
        }
        if self.replay_points and self.replay_points[-1].get("ts") == point["ts"]:
            self.replay_points[-1] = point
            return
        self.replay_points.append(point)


@dataclass
class RiskState:
    equity: float
    hwm: float
    dd_pct: float
    day_loss_pct: float
    consecutive_losses: int
    active_trade: ActiveTrade | None
    lock_state: str
    trades_today: int = 0
    current_day: date | None = None
    read_only_until: datetime | None = None
    wins: int = 0
    losses: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0

    @property
    def total_trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float("inf") if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss
