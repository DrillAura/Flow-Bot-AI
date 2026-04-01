import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daytrading_bot.config import BotConfig, ThreeCommasConfig
from daytrading_bot.strategy_lab import (
    StrategyRuntimeSelector,
    build_strategy_specs,
    review_strategy_lab,
)


class StrategyLabTests(unittest.TestCase):
    def test_build_strategy_specs_exposes_multiple_candidates(self) -> None:
        specs = build_strategy_specs()
        ids = {spec.strategy_id for spec in specs}

        self.assertGreaterEqual(len(specs), 13)
        self.assertIn("champion_breakout", ids)
        self.assertIn("mean_reversion_vwap", ids)
        self.assertIn("opening_range_breakout", ids)
        self.assertIn("trend_continuation_pullback", ids)
        self.assertIn("fast_imbalance_scalp", ids)
        self.assertIn("fast_imbalance_scalp_tight", ids)
        self.assertIn("fast_liquidity_sweep_reclaim", ids)
        self.assertIn("fast_vwap_reclaim_scalp", ids)
        self.assertIn("fast_failed_breakout_reclaim_micro", ids)
        self.assertIn("fast_liquidity_sweep_reversal", ids)
        fast_specs = [spec for spec in specs if spec.family == "fast_trading"]
        self.assertTrue(all(not spec.promotion_allowed for spec in fast_specs))

    def test_runtime_selector_refreshes_paper_strategy_from_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "strategy_lab_state.json"
            bot_config = BotConfig(strategy_lab_state_path=str(state_path), active_strategy_id="champion_breakout")
            selector = StrategyRuntimeSelector(bot_config, ThreeCommasConfig(mode="paper"))

            self.assertEqual(selector.active_strategy_id, "champion_breakout")

            state_path.write_text(
                json.dumps(
                    {
                        "current_paper_strategy_id": "mean_reversion_vwap",
                        "current_live_strategy_id": "champion_breakout",
                    }
                ),
                encoding="utf-8",
            )
            selector.maybe_refresh(active_trade_present=False)

            self.assertEqual(selector.active_strategy_id, "mean_reversion_vwap")
            self.assertEqual(getattr(selector.strategy, "strategy_id", ""), "mean_reversion_vwap")

            state_path.write_text(
                json.dumps(
                    {
                        "current_paper_strategy_id": "breakout_conservative",
                        "current_live_strategy_id": "champion_breakout",
                    }
                ),
                encoding="utf-8",
            )
            selector.maybe_refresh(active_trade_present=True)

            self.assertEqual(selector.active_strategy_id, "mean_reversion_vwap")

    def test_review_strategy_lab_promotes_best_eligible_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            telemetry_path = Path(tempdir) / "telemetry.jsonl"
            state_path = Path(tempdir) / "strategy_lab_state.json"
            events = []
            now = datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)
            for index, pnl in enumerate((0.8, -0.4, 0.7, -0.2, 0.5, -0.1), start=1):
                regime = "breakout" if index % 2 else "recovery"
                pair = "XBTEUR" if index % 2 else "ETHEUR"
                events.append(
                    self._lab_exit_event(
                        "champion_breakout",
                        "Champion Breakout",
                        "breakout_recovery",
                        "breakout_recovery",
                        pnl,
                        now + timedelta(minutes=index),
                        regime_label=regime,
                        pair=pair,
                    )
                )
            for index, pnl in enumerate((0.9, 0.8, 0.7, 0.6, 0.5, 0.4), start=10):
                regime = "mean_reversion" if index % 2 else "trend_pullback"
                pair = "SOLEUR" if index % 2 else "ETHEUR"
                events.append(
                    self._lab_exit_event(
                        "mean_reversion_vwap",
                        "VWAP Mean Reversion",
                        "mean_reversion",
                        "mean_reversion_vwap",
                        pnl,
                        now + timedelta(minutes=index),
                        max_drawdown=0.01,
                        regime_label=regime,
                        pair=pair,
                    )
                )
            telemetry_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            bot_config = BotConfig(
                strategy_lab_state_path=str(state_path),
                active_strategy_id="champion_breakout",
                strategy_lab_min_closed_trades=4,
                strategy_lab_min_profit_factor=1.05,
                strategy_lab_min_win_rate=0.5,
                strategy_lab_min_expectancy_eur=0.0,
                strategy_lab_max_drawdown_pct=0.035,
                strategy_lab_promotion_score_margin=0.05,
            )

            review = review_strategy_lab(telemetry_path, bot_config)

            self.assertTrue(review.source_exists)
            self.assertTrue(review.paper_promotion_applied)
            self.assertFalse(review.live_promotion_applied)
            self.assertEqual(review.current_paper_strategy_id, "mean_reversion_vwap")
            self.assertEqual(review.recommended_paper_strategy_id, "mean_reversion_vwap")
            self.assertIn("mean_reversion_vwap", review.promotion_reason)
            self.assertTrue(state_path.exists())

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["current_paper_strategy_id"], "mean_reversion_vwap")
            self.assertEqual(saved["current_live_strategy_id"], "champion_breakout")
            promoted = next(row for row in saved["strategies"] if row["strategy_id"] == "mean_reversion_vwap")
            self.assertTrue(promoted["gates"]["distinct_regimes"]["passed"])
            self.assertTrue(promoted["gates"]["regime_trade_depth"]["passed"])
            self.assertTrue(promoted["gates"]["regime_concentration"]["passed"])

    def test_review_strategy_lab_blocks_single_regime_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            telemetry_path = Path(tempdir) / "telemetry.jsonl"
            state_path = Path(tempdir) / "strategy_lab_state.json"
            now = datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)
            events = [
                self._lab_exit_event(
                    "mean_reversion_vwap",
                    "VWAP Mean Reversion",
                    "mean_reversion",
                    "mean_reversion_vwap",
                    pnl,
                    now + timedelta(minutes=index),
                    regime_label="mean_reversion",
                    pair="SOLEUR" if index % 2 else "ETHEUR",
                )
                for index, pnl in enumerate((0.9, 0.8, 0.7, 0.6, 0.5, 0.4), start=1)
            ]
            telemetry_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            review = review_strategy_lab(
                telemetry_path,
                BotConfig(
                    strategy_lab_state_path=str(state_path),
                    active_strategy_id="champion_breakout",
                    strategy_lab_min_closed_trades=4,
                    strategy_lab_min_profit_factor=1.05,
                    strategy_lab_min_win_rate=0.5,
                    strategy_lab_min_expectancy_eur=0.0,
                    strategy_lab_max_drawdown_pct=0.035,
                    strategy_lab_min_distinct_regimes=2,
                    strategy_lab_min_trades_per_regime=2,
                    strategy_lab_max_regime_concentration=0.75,
                ),
            )

            self.assertFalse(review.paper_promotion_applied)
            self.assertEqual(review.promotion_reason, "no_eligible_challenger")
            blocked = next(row for row in review.strategies if row.strategy_id == "mean_reversion_vwap")
            self.assertFalse(blocked.eligible_for_promotion)
            self.assertFalse(blocked.gates["distinct_regimes"].passed)
            self.assertFalse(blocked.gates["regime_concentration"].passed)

    def test_review_strategy_lab_respects_promotion_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            telemetry_path = Path(tempdir) / "telemetry.jsonl"
            state_path = Path(tempdir) / "strategy_lab_state.json"
            now = datetime.now(timezone.utc)
            state_path.write_text(
                json.dumps(
                    {
                        "current_paper_strategy_id": "champion_breakout",
                        "current_live_strategy_id": "champion_breakout",
                        "current_paper_promoted_at": now.isoformat(),
                        "paper_promotion_cooldown_until": (now + timedelta(hours=12)).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            events = [
                self._lab_exit_event("champion_breakout", "Champion Breakout", "breakout_recovery", "breakout_recovery", pnl, now + timedelta(minutes=index), regime_label="breakout" if index % 2 else "recovery", pair="XBTEUR" if index % 2 else "ETHEUR")
                for index, pnl in enumerate((0.2, 0.1, 0.2, 0.1), start=1)
            ] + [
                self._lab_exit_event("mean_reversion_vwap", "VWAP Mean Reversion", "mean_reversion", "mean_reversion_vwap", pnl, now + timedelta(minutes=10 + index), regime_label="mean_reversion" if index % 2 else "trend_pullback", pair="SOLEUR" if index % 2 else "ETHEUR")
                for index, pnl in enumerate((0.9, 0.8, 0.7, 0.6), start=1)
            ]
            telemetry_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            review = review_strategy_lab(
                telemetry_path,
                BotConfig(
                    strategy_lab_state_path=str(state_path),
                    active_strategy_id="champion_breakout",
                    strategy_lab_min_closed_trades=4,
                    strategy_lab_min_profit_factor=1.05,
                    strategy_lab_min_win_rate=0.5,
                    strategy_lab_min_expectancy_eur=0.0,
                    strategy_lab_max_drawdown_pct=0.035,
                    strategy_lab_promotion_score_margin=0.05,
                ),
            )

            self.assertFalse(review.paper_promotion_applied)
            self.assertEqual(review.current_paper_strategy_id, "champion_breakout")
            self.assertEqual(review.promotion_reason, "promotion_cooldown_active")

    def test_review_strategy_lab_rolls_back_underperforming_recent_champion(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            telemetry_path = Path(tempdir) / "telemetry.jsonl"
            state_path = Path(tempdir) / "strategy_lab_state.json"
            now = datetime.now(timezone.utc)
            state_path.write_text(
                json.dumps(
                    {
                        "current_paper_strategy_id": "mean_reversion_vwap",
                        "current_live_strategy_id": "champion_breakout",
                        "previous_paper_strategy_id": "champion_breakout",
                        "current_paper_promoted_at": (now - timedelta(hours=1)).isoformat(),
                        "paper_promotion_cooldown_until": (now + timedelta(hours=12)).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            events = [
                self._lab_exit_event("mean_reversion_vwap", "VWAP Mean Reversion", "mean_reversion", "mean_reversion_vwap", pnl, now + timedelta(minutes=index), regime_label="mean_reversion" if index % 2 else "trend_pullback", max_drawdown=0.05, pair="SOLEUR" if index % 2 else "ETHEUR")
                for index, pnl in enumerate((-0.8, -0.7, -0.5, -0.4), start=1)
            ]
            telemetry_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            review = review_strategy_lab(
                telemetry_path,
                BotConfig(
                    strategy_lab_state_path=str(state_path),
                    active_strategy_id="champion_breakout",
                    strategy_lab_min_closed_trades=4,
                    strategy_lab_min_profit_factor=1.05,
                    strategy_lab_min_win_rate=0.5,
                    strategy_lab_min_expectancy_eur=0.0,
                    strategy_lab_max_drawdown_pct=0.035,
                    strategy_lab_rollback_min_closed_trades=4,
                    strategy_lab_rollback_min_profit_factor=0.95,
                    strategy_lab_rollback_max_drawdown_pct=0.045,
                ),
            )

            self.assertTrue(review.paper_promotion_applied)
            self.assertTrue(review.rollback_applied)
            self.assertEqual(review.current_paper_strategy_id, "champion_breakout")
            self.assertEqual(review.promotion_reason, "paper_rollback_to_champion_breakout")

    def test_review_strategy_lab_blocks_asset_concentrated_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            telemetry_path = Path(tempdir) / "telemetry.jsonl"
            state_path = Path(tempdir) / "strategy_lab_state.json"
            now = datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)
            events = [
                self._lab_exit_event(
                    "mean_reversion_vwap",
                    "VWAP Mean Reversion",
                    "mean_reversion",
                    "mean_reversion_vwap",
                    pnl,
                    now + timedelta(minutes=index),
                    regime_label="mean_reversion" if index % 2 else "trend_pullback",
                    pair="SOLEUR",
                )
                for index, pnl in enumerate((0.9, 0.8, 0.7, 0.6, 0.5, 0.4), start=1)
            ]
            telemetry_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            review = review_strategy_lab(
                telemetry_path,
                BotConfig(
                    strategy_lab_state_path=str(state_path),
                    active_strategy_id="champion_breakout",
                    strategy_lab_min_closed_trades=4,
                    strategy_lab_min_profit_factor=1.05,
                    strategy_lab_min_win_rate=0.5,
                    strategy_lab_min_expectancy_eur=0.0,
                    strategy_lab_max_drawdown_pct=0.035,
                    strategy_lab_min_distinct_regimes=2,
                    strategy_lab_min_trades_per_regime=2,
                    strategy_lab_max_regime_concentration=0.75,
                    strategy_lab_min_distinct_assets=2,
                    strategy_lab_min_trades_per_asset=2,
                    strategy_lab_max_asset_concentration=0.75,
                ),
            )

            blocked = next(row for row in review.strategies if row.strategy_id == "mean_reversion_vwap")
            self.assertFalse(blocked.eligible_for_promotion)
            self.assertFalse(blocked.gates["distinct_assets"].passed)
            self.assertFalse(blocked.gates["asset_concentration"].passed)

    @staticmethod
    def _lab_exit_event(
        strategy_id: str,
        label: str,
        family: str,
        strategy_type: str,
        pnl: float,
        ts: datetime,
        *,
        max_drawdown: float = 0.02,
        regime_label: str | None = None,
        pair: str = "XBTEUR",
    ) -> dict:
        return {
            "ts": ts.isoformat().replace("+00:00", "Z"),
            "event_type": "strategy_lab_exit_sent",
            "payload": {
                "strategy_id": strategy_id,
                "strategy_label": label,
                "strategy_family": family,
                "strategy_type": strategy_type,
                "pnl_eur": pnl,
                "hold_minutes": 24.0,
                "strategy_max_drawdown_pct": max_drawdown,
                "pair": pair,
                "regime_label": regime_label or family,
                "setup_type": strategy_type,
            },
        }


if __name__ == "__main__":
    unittest.main()
