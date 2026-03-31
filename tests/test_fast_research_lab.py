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
                        "closed_trades": 4,
                        "win_rate": 0.5,
                        "profit_factor": 1.1,
                        "expectancy_eur": 0.03,
                        "score": 4.2,
                        "eligible_for_promotion": False,
                        "gates": {"promotion_allowed": {"passed": False}},
                    },
                ],
            }

            payload = build_fast_research_lab_payload(strategy_lab, telemetry_path)

        self.assertEqual(payload["summary"]["strategies_seen"], 1)
        self.assertEqual(payload["signals"]["observed"], 2)
        self.assertEqual(payload["signals"]["paper_candidates"], 1)
        self.assertEqual(payload["signals"]["micro_rejections"], 1)
        self.assertEqual(len(payload["micro_signals"]), 2)
        self.assertTrue(payload["beginner_notes"])


if __name__ == "__main__":
    unittest.main()
