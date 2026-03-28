from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib import request

from .config import BotConfig, ThreeCommasConfig
from .models import ActiveTrade, DayTradeIntent
from .sessions import is_trade_window


class ThreeCommasSignalClient:
    def __init__(self, bot_config: BotConfig, execution_config: ThreeCommasConfig) -> None:
        self.bot_config = bot_config
        self.execution_config = execution_config

    def build_entry_payload(self, intent: DayTradeIntent) -> dict[str, Any]:
        pair = self.bot_config.pair_by_symbol(intent.pair)
        payload: dict[str, Any] = {
            "secret": self.execution_config.secret,
            "max_lag": str(self.execution_config.max_lag_seconds),
            "timestamp": self._timestamp(),
            "trigger_price": f"{intent.entry_zone:.8f}",
            "tv_exchange": pair.tv_exchange,
            "tv_instrument": pair.tv_instrument,
            "action": "enter_long",
            "bot_uuid": self.execution_config.bot_uuid,
            "order": {
                "amount": f"{intent.budget_eur:.2f}",
                "currency_type": self.execution_config.order_currency_type,
                "order_type": self.execution_config.entry_order_type,
            },
        }
        if self.execution_config.entry_order_type == "limit":
            payload["order"]["price"] = f"{intent.entry_zone:.8f}"
        return payload

    def build_exit_payload(self, trade: ActiveTrade, trigger_price: float) -> dict[str, Any]:
        pair = self.bot_config.pair_by_symbol(trade.pair)
        return {
            "secret": self.execution_config.secret,
            "max_lag": str(self.execution_config.max_lag_seconds),
            "timestamp": self._timestamp(),
            "trigger_price": f"{trigger_price:.8f}",
            "tv_exchange": pair.tv_exchange,
            "tv_instrument": pair.tv_instrument,
            "action": "exit_long",
            "bot_uuid": self.execution_config.bot_uuid,
            "order": {
                "amount": "100",
                "currency_type": "position_percent",
            },
        }

    def build_disable_payload(self, market_close: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "secret": self.execution_config.secret,
            "max_lag": str(self.execution_config.max_lag_seconds),
            "timestamp": self._timestamp(),
            "action": "disable",
            "bot_uuid": self.execution_config.bot_uuid,
        }
        if market_close:
            payload["positions_sub_action"] = "market_close"
        return payload

    def validate_entry_intent(self, intent: DayTradeIntent) -> tuple[bool, str]:
        if self.execution_config.mode != "live":
            return True, "ok"
        if (
            intent.setup_type == "recovery_reclaim"
            and not self.bot_config.meets_quality(intent.quality, self.bot_config.live_recovery_min_quality)
        ):
            return False, f"live_requires_{self.bot_config.live_recovery_min_quality.lower()}_recovery_quality"
        return True, "ok"

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.execution_config.dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "mode": self.execution_config.mode,
                "payload": payload,
            }

        self._validate_live_configuration()
        self._validate_payload(payload)

        raw = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.execution_config.webhook_url,
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8")
            return {"ok": True, "mode": self.execution_config.mode, "status": response.status, "body": body}

    def _validate_live_configuration(self) -> None:
        if not self.execution_config.allow_live:
            raise RuntimeError("Live mode blocked. Set BOT_ALLOW_LIVE=true to arm real 3Commas webhooks.")
        if not self.execution_config.secret:
            raise RuntimeError("Live mode requires THREE_COMMAS_SECRET.")
        if not self.execution_config.bot_uuid:
            raise RuntimeError("Live mode requires THREE_COMMAS_BOT_UUID.")

    def _validate_payload(self, payload: dict[str, Any]) -> None:
        required_fields = {"secret", "timestamp", "action"}
        missing = sorted(field for field in required_fields if not payload.get(field))
        if missing:
            raise RuntimeError(f"Invalid 3Commas payload: missing {', '.join(missing)}")
        if payload["action"] in {"enter_long", "exit_long", "disable"} and not payload.get("bot_uuid"):
            raise RuntimeError("Invalid 3Commas payload: missing bot_uuid")
        if payload["action"] in {"enter_long", "exit_long"} and not payload.get("trigger_price"):
            raise RuntimeError("Invalid 3Commas payload: missing trigger_price")
        if payload["action"] == "enter_long" and not isinstance(payload.get("order"), dict):
            raise RuntimeError("Invalid 3Commas payload: missing order")
        if payload["action"] == "exit_long" and payload.get("order", {}).get("currency_type") != "position_percent":
            raise RuntimeError("Invalid 3Commas payload: invalid exit order")

    def live_preflight(self) -> dict[str, Any]:
        issues: list[str] = []
        now_local = datetime.now(self.bot_config.timezone)
        if self.execution_config.mode == "live":
            if not self.execution_config.allow_live:
                issues.append("BOT_ALLOW_LIVE is false")
            if not self.execution_config.secret:
                issues.append("THREE_COMMAS_SECRET is missing")
            if not self.execution_config.bot_uuid:
                issues.append("THREE_COMMAS_BOT_UUID is missing")
        return {
            "mode": self.execution_config.mode,
            "armed": self.execution_config.mode != "live" or len(issues) == 0,
            "session_open": is_trade_window(now_local, self.bot_config),
            "local_time": now_local.isoformat(timespec="seconds"),
            "issues": issues,
        }

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
