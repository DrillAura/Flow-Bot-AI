import json
import tempfile
import unittest
from pathlib import Path

from daytrading_bot.config import BotConfig
from daytrading_bot.shadow_portfolios import build_shadow_portfolio_specs, run_shadow_portfolio_report


class ShadowPortfolioTests(unittest.TestCase):
    def test_build_shadow_portfolio_specs_uses_config_sizes(self) -> None:
        specs = build_shadow_portfolio_specs(BotConfig(shadow_portfolio_sizes_eur=(50.0, 250.0)))
        self.assertEqual([spec.name for spec in specs], ["shadow_0050", "shadow_0250"])

    def test_run_shadow_portfolio_report_summarizes_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "shadows.jsonl"
            rows = [
                {
                    "ts": "2026-03-27T10:00:00Z",
                    "event_type": "shadow_exit_sent",
                    "payload": {
                        "portfolio_name": "shadow_0100",
                        "portfolio_initial_equity": 100.0,
                        "portfolio_equity": 101.5,
                        "portfolio_max_drawdown_pct": 0.012,
                        "pnl_eur": 1.5,
                        "hold_minutes": 35.0,
                        "regime_label": "bullish",
                        "setup_type": "breakout_pullback",
                        "market_ts": "2026-03-27T10:00:00Z",
                        "mae_r": -0.35,
                        "mfe_r": 1.2,
                        "total_fee_eur": 0.42,
                        "entry_slippage_bps": 0.6,
                        "exit_slippage_bps": 0.9,
                    },
                },
                {
                    "ts": "2026-03-27T11:00:00Z",
                    "event_type": "shadow_exit_sent",
                    "payload": {
                        "portfolio_name": "shadow_0100",
                        "portfolio_initial_equity": 100.0,
                        "portfolio_equity": 100.5,
                        "portfolio_max_drawdown_pct": 0.018,
                        "pnl_eur": -1.0,
                        "hold_minutes": 20.0,
                        "regime_label": "recovery",
                        "setup_type": "recovery_reclaim",
                        "market_ts": "2026-03-27T11:00:00Z",
                        "mae_r": -0.65,
                        "mfe_r": 0.4,
                        "total_fee_eur": 0.38,
                        "entry_slippage_bps": 0.5,
                        "exit_slippage_bps": 1.1,
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            report = run_shadow_portfolio_report(path, BotConfig(shadow_portfolio_sizes_eur=(100.0, 250.0)))

        self.assertTrue(report.source_exists)
        self.assertEqual(len(report.portfolios), 2)
        summary = next(row for row in report.portfolios if row.name == "shadow_0100")
        self.assertEqual(summary.closed_trades, 2)
        self.assertAlmostEqual(summary.ending_equity, 100.5)
        self.assertAlmostEqual(summary.net_pnl_eur, 0.5)
        self.assertGreaterEqual(summary.max_drawdown_pct, 0.018)
        self.assertAlmostEqual(summary.average_mae_r, -0.5)
        self.assertAlmostEqual(summary.average_mfe_r, 0.8)
        self.assertAlmostEqual(summary.average_total_fee_eur, 0.4)
        self.assertAlmostEqual(summary.average_total_slippage_bps, 1.55)
        self.assertEqual(len(report.equity_curves), 2)
        self.assertIn("filter_options", report.__dict__)
        self.assertEqual(report.filter_options["portfolios"], ["shadow_0100", "shadow_0250"])
        self.assertIn("bullish", report.filter_options["regimes"])
        regimes = {(row["portfolio"], row["regime_label"]) for row in report.regime_comparison}
        self.assertIn(("shadow_0100", "bullish"), regimes)
        setups = {(row["portfolio"], row["setup_type"]) for row in report.setup_comparison}
        self.assertIn(("shadow_0100", "recovery_reclaim"), setups)


if __name__ == "__main__":
    unittest.main()
