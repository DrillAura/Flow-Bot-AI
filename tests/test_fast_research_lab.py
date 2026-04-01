import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from daytrading_bot.fast_research_lab import build_fast_research_lab_payload


class FastResearchLabTests(unittest.TestCase):
    def test_build_fast_research_lab_payload_filters_to_fast_family_and_counts_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            telemetry_path = Path(tempdir) / "telemetry.jsonl"
            events = [
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event_type": "signal_observed",
                    "payload": {
                        "strategy_id": "fast_imbalance_scalp",
                        "strategy_family": "fast_trading",
                        "pair": "XBTEUR",
                        "regime_label": "fast_trading",
                        "setup_type": "fast_micro_scalp",
                        "tradable": True,
                        "analysis_windows": {
                            "1S": {"change_pct": 0.018, "available": True},
                            "5S": {"change_pct": 0.041, "available": True},
                        },
                        "snapshot": {"spread_bps": 4.2, "imbalance_1m": 1.18},
                        "rejection_reasons": [],
                    },
                },
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event_type": "signal_observed",
                    "payload": {
                        "strategy_id": "fast_imbalance_scalp_tight",
                        "strategy_family": "fast_trading",
                        "pair": "ETHEUR",
                        "regime_label": "fast_trading",
                        "setup_type": "fast_micro_scalp",
                        "tradable": False,
                        "analysis_windows": {
                            "1S": {"change_pct": 0.012, "available": True},
                            "5S": {"change_pct": 0.025, "available": True},
                        },
                        "snapshot": {"spread_bps": 5.0, "imbalance_1m": 1.05},
                        "rejection_reasons": ["fast_imbalance_too_low"],
                    },
                },
            ]
            telemetry_path.write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")

            strategy_lab = {
                "current_paper_strategy_id": "champion_breakout",
                "current_live_strategy_id": "champion_breakout",
                "strategies": [
                    {
                        "strategy_id": "champion_breakout",
                        "label": "Champion Breakout",
                        "family": "breakout_recovery",
                        "strategy_type": "breakout_recovery",
                        "closed_trades": 12,
                        "win_rate": 0.6,
                        "profit_factor": 1.3,
                        "expectancy_eur": 0.09,
                        "score": 8.0,
                        "eligible_for_promotion": True,
                        "gates": {"promotion_allowed": {"passed": True}},
                    },
                    {
                        "strategy_id": "fast_imbalance_scalp",
                        "label": "Fast Imbalance Scalp",
                        "family": "fast_trading",
                        "strategy_type": "fast_micro_scalp",
                        "closed_trades": 4,
                        "win_rate": 0.5,
                        "profit_factor": 1.1,
                        "expectancy_eur": 0.03,
                        "score": 4.2,
                        "eligible_for_promotion": False,
                        "gates": {"promotion_allowed": {"passed": False}},
                    },
                    {
                        "strategy_id": "fast_liquidity_sweep_reclaim",
                        "label": "Fast Liquidity Sweep Reclaim",
                        "family": "fast_trading",
                        "strategy_type": "fast_liquidity_sweep_reclaim",
                        "closed_trades": 6,
                        "win_rate": 0.67,
                        "profit_factor": 1.35,
                        "expectancy_eur": 0.05,
                        "score": 8.8,
                        "eligible_for_promotion": False,
                        "gates": {"promotion_allowed": {"passed": False}},
                    },
                    {
                        "strategy_id": "fast_vwap_reclaim_scalp",
                        "label": "Fast VWAP Reclaim Scalp",
                        "family": "fast_trading",
                        "strategy_type": "fast_vwap_reclaim_scalp",
                        "closed_trades": 5,
                        "win_rate": 0.4,
                        "profit_factor": 1.05,
                        "expectancy_eur": 0.01,
                        "score": 5.1,
                        "eligible_for_promotion": False,
                        "gates": {"promotion_allowed": {"passed": False}},
                    },
                ],
            }

            payload = build_fast_research_lab_payload(strategy_lab, telemetry_path)

        self.assertEqual(payload["summary"]["strategies_seen"], 3)
        self.assertEqual(payload["signals"]["observed"], 2)
        self.assertEqual(payload["signals"]["paper_candidates"], 1)
        self.assertEqual(payload["signals"]["micro_rejections"], 1)
        self.assertEqual(len(payload["micro_signals"]), 2)
        self.assertTrue(payload["beginner_notes"])
        self.assertIn("compare", payload)
        self.assertIn("drilldown", payload)
        self.assertEqual(payload["compare"]["summary"]["observed_pairs"], 2)
        self.assertEqual(payload["compare"]["summary"]["observed_setup_types"], 1)
        self.assertEqual(payload["compare"]["summary"]["strategy_setup_types"], 3)
        self.assertEqual(payload["compare"]["summary"]["top_family"], "Fast Liquidity Sweep Reclaim")
        self.assertEqual(payload["compare"]["summary"]["top_pair"], "XBTEUR")
        self.assertEqual(payload["compare"]["summary"]["top_rejection"], "fast_imbalance_too_low")
        self.assertEqual(len(payload["compare"]["family_rows"]), 3)
        self.assertEqual(len(payload["compare"]["pair_rows"]), 2)
        self.assertEqual(payload["compare"]["family_rows"][0]["strategy_type"], "fast_liquidity_sweep_reclaim")
        self.assertEqual(payload["compare"]["family_rows"][0]["observed_signals"], 0)
        self.assertTrue(
            any(
                row["strategy_type"] == "fast_micro_scalp" and row["observed_signals"] == 2
                for row in payload["compare"]["family_rows"]
            )
        )
        self.assertEqual(payload["compare"]["pair_rows"][0]["pair"], "XBTEUR")
        self.assertEqual(payload["compare"]["pair_rows"][0]["observed_signals"], 1)
        self.assertGreaterEqual(payload["compare"]["rejection_leaderboard"][0]["count"], 1)
        self.assertIn("summary_cards", payload["compare"])
        self.assertEqual(len(payload["drilldown_summary"]), 4)
        self.assertEqual(payload["compare"]["strategy_rows"][0]["strategy_type"], "fast_liquidity_sweep_reclaim")
        self.assertIn("observed_signals", payload["compare"]["strategy_rows"][0])

    def test_build_fast_research_lab_payload_includes_setup_and_asset_compare_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            telemetry_path = Path(tempdir) / "telemetry.jsonl"
            events = [
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event_type": "signal_observed",
                    "payload": {
                        "strategy_id": "fast_failed_breakout_reclaim_micro",
                        "strategy_family": "fast_trading",
                        "pair": "SOL",
                        "regime_label": "fast_trading",
                        "setup_type": "fast_failed_breakout_reclaim_micro",
                        "tradable": True,
                        "analysis_windows": {
                            "1S": {"change_pct": 0.022, "available": True},
                            "5S": {"change_pct": 0.044, "available": True},
                        },
                        "snapshot": {"spread_bps": 3.4, "imbalance_1m": 1.22},
                        "rejection_reasons": [],
                    },
                },
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event_type": "signal_observed",
                    "payload": {
                        "strategy_id": "fast_vwap_reclaim_scalp",
                        "strategy_family": "fast_trading",
                        "pair": "DOGE",
                        "regime_label": "fast_trading",
                        "setup_type": "fast_vwap_reclaim_scalp",
                        "tradable": False,
                        "analysis_windows": {
                            "1S": {"change_pct": 0.011, "available": True},
                            "5S": {"change_pct": 0.021, "available": True},
                        },
                        "snapshot": {"spread_bps": 6.0, "imbalance_1m": 1.04},
                        "rejection_reasons": ["fast_vwap_volume_too_low", "fast_imbalance_too_low"],
                    },
                },
            ]
            telemetry_path.write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")

            strategy_lab = {
                "current_paper_strategy_id": "fast_failed_breakout_reclaim_micro",
                "current_live_strategy_id": "champion_breakout",
                "strategies": [
                    {
                        "strategy_id": "fast_failed_breakout_reclaim_micro",
                        "label": "Fast Failed Breakout Reclaim",
                        "family": "fast_trading",
                        "strategy_type": "fast_failed_breakout_reclaim_micro",
                        "closed_trades": 8,
                        "win_rate": 0.62,
                        "profit_factor": 1.42,
                        "expectancy_eur": 0.06,
                        "score": 8.7,
                        "eligible_for_promotion": False,
                        "gates": {"promotion_allowed": {"passed": False}},
                    },
                    {
                        "strategy_id": "fast_vwap_reclaim_scalp",
                        "label": "Fast VWAP Reclaim Scalp",
                        "family": "fast_trading",
                        "strategy_type": "fast_vwap_reclaim_scalp",
                        "closed_trades": 5,
                        "win_rate": 0.4,
                        "profit_factor": 1.05,
                        "expectancy_eur": 0.01,
                        "score": 5.1,
                        "eligible_for_promotion": False,
                        "gates": {"promotion_allowed": {"passed": False}},
                    },
                ],
            }

            payload = build_fast_research_lab_payload(strategy_lab, telemetry_path)

        self.assertEqual(payload["compare"]["summary"]["observed_setup_types"], 2)
        self.assertEqual(payload["compare"]["summary"]["strategy_setup_types"], 2)
        self.assertEqual(payload["compare"]["summary"]["top_pair"], "SOL")
        self.assertEqual(payload["compare"]["summary"]["top_rejection"], "fast_vwap_volume_too_low")
        self.assertIn("setup_types", payload["compare"]["pair_rows"][0])
        self.assertIn("rejection_share", payload["compare"]["pair_rows"][0])
        self.assertIn("avg_change_1s_bps", payload["compare"]["family_rows"][0])
        self.assertIn("avg_spread_bps", payload["compare"]["family_rows"][0])
        self.assertEqual(payload["drilldown"]["families"][0]["strategy_id"], "fast_failed_breakout_reclaim_micro")
        self.assertTrue(payload["drilldown"]["summary_cards"])


if __name__ == "__main__":
    unittest.main()
