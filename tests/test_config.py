import unittest
from unittest.mock import patch

from daytrading_bot.config import BotConfig, load_config_from_env


class ConfigTests(unittest.TestCase):
    def test_default_pair_universe_prefers_liquid_simple_eur_pairs(self) -> None:
        config = BotConfig()
        symbols = {pair.symbol for pair in config.pairs}

        self.assertTrue({"XBTEUR", "ETHEUR", "SOLEUR"}.issubset(symbols))
        self.assertTrue({"XRPEUR", "LTCEUR", "XDGEUR", "FETEUR"}.issubset(symbols))
        self.assertTrue({"ADAEUR", "LINKEUR", "DOTEUR", "TRXEUR", "ATOMEUR"}.issubset(symbols))

    def test_default_pair_universe_excludes_currently_wider_spread_pairs(self) -> None:
        config = BotConfig()
        symbols = {pair.symbol for pair in config.pairs}

        self.assertNotIn("SNXEUR", symbols)
        self.assertNotIn("AVAXEUR", symbols)

    def test_env_can_override_runtime_pair_subset(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "BOT_PAIRS": "XBTEUR,ETHEUR,SOLEUR",
                "BOT_MODE": "paper",
            },
            clear=False,
        ):
            bot_config, _ = load_config_from_env()

        self.assertEqual(tuple(pair.symbol for pair in bot_config.pairs), ("XBTEUR", "ETHEUR", "SOLEUR"))

    def test_pair_specific_execution_profiles_are_more_conservative_for_high_beta_pairs(self) -> None:
        config = BotConfig()
        xbt = config.pair_by_symbol("XBTEUR")
        doge = config.pair_by_symbol("XDGEUR")
        fet = config.pair_by_symbol("FETEUR")

        self.assertLess(xbt.paper_min_entry_slippage_bps, doge.paper_min_entry_slippage_bps)
        self.assertLess(xbt.paper_min_exit_slippage_bps, fet.paper_min_exit_slippage_bps)
        self.assertGreater(xbt.paper_entry_maker_probability_cap, fet.paper_entry_maker_probability_cap)
        self.assertGreater(xbt.paper_exit_maker_probability_cap, doge.paper_exit_maker_probability_cap)


if __name__ == "__main__":
    unittest.main()
