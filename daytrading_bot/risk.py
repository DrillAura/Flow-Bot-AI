from __future__ import annotations

from datetime import datetime

from .config import BotConfig
from .models import ActiveTrade, RiskState
from .sessions import localize, next_trade_day_start


class RiskController:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._closed_equity = config.initial_equity_eur
        self._max_dd_pct = 0.0
        self.state = RiskState(
            equity=config.initial_equity_eur,
            hwm=config.initial_equity_eur,
            dd_pct=0.0,
            day_loss_pct=0.0,
            consecutive_losses=0,
            active_trade=None,
            lock_state="active",
        )

    def roll_day(self, moment: datetime) -> None:
        local_day = localize(moment, self.config).date()
        if self.state.current_day == local_day:
            return
        self.state.current_day = local_day
        self.state.trades_today = 0
        self.state.day_loss_pct = 0.0
        self.state.consecutive_losses = 0
        if self.state.lock_state == "read_only":
            self._set_lock_state(moment)

    def mark_to_market(self, unrealized_pnl: float, moment: datetime) -> None:
        self.roll_day(moment)
        self.state.equity = self._closed_equity + unrealized_pnl
        self.state.hwm = max(self.state.hwm, self.state.equity)
        if self.state.hwm > 0:
            self.state.dd_pct = max(0.0, (self.state.hwm - self.state.equity) / self.state.hwm)
            self._max_dd_pct = max(self._max_dd_pct, self.state.dd_pct)
        self._set_lock_state(moment)

    def record_trade_opened(self, trade: ActiveTrade, moment: datetime) -> None:
        self.roll_day(moment)
        self.state.active_trade = trade
        self.state.trades_today += 1

    def record_trade_closed(self, pnl_eur: float, moment: datetime) -> None:
        self.roll_day(moment)
        self._closed_equity += pnl_eur
        self.state.equity = self._closed_equity
        self.state.hwm = max(self.state.hwm, self.state.equity)
        self.state.active_trade = None
        if self.state.hwm > 0:
            self.state.dd_pct = max(0.0, (self.state.hwm - self.state.equity) / self.state.hwm)
            self._max_dd_pct = max(self._max_dd_pct, self.state.dd_pct)
        if pnl_eur > 0:
            self.state.wins += 1
            self.state.gross_profit += pnl_eur
            self.state.consecutive_losses = 0
        elif pnl_eur < 0:
            self.state.losses += 1
            self.state.gross_loss += abs(pnl_eur)
            self.state.consecutive_losses += 1
            if self._closed_equity > 0:
                self.state.day_loss_pct += abs(pnl_eur) / max(self._closed_equity, 1e-9)
        else:
            self.state.consecutive_losses = 0
        self._set_lock_state(moment)

    def current_risk_pct(self) -> float:
        if self.state.lock_state in {"reduced", "read_only"}:
            return self.config.reduced_risk_per_trade_pct
        return self.config.base_risk_per_trade_pct

    def can_open_trade(self, moment: datetime, quality: str) -> tuple[bool, str]:
        self.roll_day(moment)
        self._set_lock_state(moment)
        if self.state.active_trade is not None:
            return False, "active_trade_present"
        if self.state.lock_state == "killed":
            return False, "kill_switch_active"
        if self.state.lock_state == "read_only":
            return False, "read_only"
        if self.state.trades_today >= self.config.max_trades_per_day:
            return False, "max_trades_reached"
        if self.state.consecutive_losses >= self.config.consecutive_losses_limit:
            return False, "consecutive_losses_limit"
        if self.state.day_loss_pct >= self.config.daily_loss_limit_pct:
            return False, "daily_loss_limit"
        if self.state.lock_state in {"warning", "reduced"} and quality != "A":
            return False, "warning_state_requires_a_setup"
        return True, "ok"

    def position_budget(self, entry_price: float, stop_price: float, available_eur: float) -> float:
        if stop_price >= entry_price:
            return 0.0
        stop_pct = (entry_price - stop_price) / entry_price
        risk_budget = self.state.equity * self.current_risk_pct()
        raw_budget = risk_budget / max(stop_pct, 1e-9)
        max_budget = available_eur * self.config.max_position_fraction
        return max(0.0, min(raw_budget, max_budget))

    @property
    def max_drawdown_pct(self) -> float:
        return self._max_dd_pct

    def _set_lock_state(self, moment: datetime) -> None:
        dd = self.state.dd_pct
        if dd >= self.config.max_drawdown_pct:
            self.state.lock_state = "killed"
            return
        if dd >= self.config.read_only_drawdown_pct:
            self.state.lock_state = "read_only"
            self.state.read_only_until = next_trade_day_start(moment, self.config)
            return
        if self.state.read_only_until is not None and moment < self.state.read_only_until:
            self.state.lock_state = "read_only"
            return
        self.state.read_only_until = None
        if dd >= self.config.reduced_drawdown_pct:
            self.state.lock_state = "reduced"
            return
        if dd >= self.config.warning_drawdown_pct:
            self.state.lock_state = "warning"
            return
        self.state.lock_state = "active"
