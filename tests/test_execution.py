import unittest
from datetime import datetime, timezone

from daytrading_bot.config import BotConfig, ThreeCommasConfig
from daytrading_bot.execution import ThreeCommasSignalClient
from daytrading_bot.models import ActiveTrade, DayTradeIntent


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot_config = BotConfig()
        self.exec_config = ThreeCommasConfig(secret="secret-token", bot_uuid="bot-uuid", mode="paper")
        self.client = ThreeCommasSignalClient(self.bot_config, self.exec_config)

    def test_build_entry_payload(self) -> None:
        intent = DayTradeIntent(
            pair="XBTEUR",
            entry_zone=35000.0,
            stop_price=34600.0,
            trail_activation_r=1.4,
            max_hold_min=120,
            budget_eur=80.0,
            reason_code="breakout_pullback",
            score=85.0,
            quality="A",
        )
        payload = self.client.build_entry_payload(intent)
        self.assertEqual(payload["action"], "enter_long")
        self.assertEqual(payload["bot_uuid"], "bot-uuid")
        self.assertEqual(payload["tv_exchange"], "KRAKEN")
        self.assertEqual(payload["tv_instrument"], "XBTEUR")
        self.assertEqual(payload["order"]["amount"], "80.00")
        self.assertEqual(payload["order"]["currency_type"], "quote")

    def test_build_exit_payload(self) -> None:
        trade = ActiveTrade(
            pair="XBTEUR",
            entry_ts=datetime.now(timezone.utc),
            entry_price=35000.0,
            initial_stop_price=34600.0,
            stop_price=34600.0,
            budget_eur=80.0,
            reason_code="breakout_pullback",
            max_hold_min=120,
            trail_activation_r=1.4,
        )
        payload = self.client.build_exit_payload(trade, trigger_price=35500.0)
        self.assertEqual(payload["action"], "exit_long")
        self.assertEqual(payload["order"]["amount"], "100")
        self.assertEqual(payload["order"]["currency_type"], "position_percent")

    def test_build_disable_payload_includes_bot_uuid(self) -> None:
        payload = self.client.build_disable_payload()
        self.assertEqual(payload["action"], "disable")
        self.assertEqual(payload["bot_uuid"], "bot-uuid")

    def test_live_mode_requires_explicit_arm(self) -> None:
        client = ThreeCommasSignalClient(
            self.bot_config,
            ThreeCommasConfig(secret="secret-token", bot_uuid="bot-uuid", mode="live", allow_live=False),
        )
        with self.assertRaises(RuntimeError):
            client.send({"action": "enter_long"})

    def test_live_preflight_exposes_missing_arm(self) -> None:
        client = ThreeCommasSignalClient(
            self.bot_config,
            ThreeCommasConfig(secret="secret-token", bot_uuid="bot-uuid", mode="live", allow_live=False),
        )
        preflight = client.live_preflight()
        self.assertEqual(preflight["mode"], "live")
        self.assertFalse(preflight["armed"])
        self.assertIn("BOT_ALLOW_LIVE is false", preflight["issues"])

    def test_paper_preflight_is_not_blocked_by_missing_live_credentials(self) -> None:
        client = ThreeCommasSignalClient(
            self.bot_config,
            ThreeCommasConfig(mode="paper", allow_live=False),
        )
        preflight = client.live_preflight()
        self.assertTrue(preflight["armed"])
        self.assertEqual(preflight["issues"], [])

    def test_live_send_rejects_malformed_payload(self) -> None:
        client = ThreeCommasSignalClient(
            self.bot_config,
            ThreeCommasConfig(secret="secret-token", bot_uuid="bot-uuid", mode="live", allow_live=True),
        )
        with self.assertRaises(RuntimeError):
            client.send({"action": "enter_long", "secret": "secret-token", "timestamp": "2026-03-23T00:00:00Z"})

    def test_live_recovery_entry_requires_a_quality(self) -> None:
        client = ThreeCommasSignalClient(
            self.bot_config,
            ThreeCommasConfig(secret="secret-token", bot_uuid="bot-uuid", mode="live", allow_live=True),
        )
        intent = DayTradeIntent(
            pair="XBTEUR",
            entry_zone=35000.0,
            stop_price=34600.0,
            trail_activation_r=0.9,
            max_hold_min=75,
            budget_eur=80.0,
            reason_code="recovery_reclaim:34990.0",
            score=65.0,
            quality="B",
            setup_type="recovery_reclaim",
            regime_label="recovery",
            break_even_trigger_r=0.6,
            time_decay_minutes=30,
            time_decay_min_r=0.15,
        )
        allowed, reason = client.validate_entry_intent(intent)
        self.assertFalse(allowed)
        self.assertEqual(reason, "live_requires_a_recovery_quality")


if __name__ == "__main__":
    unittest.main()
