import unittest

from swing_telegram import evaluate_late_entry_risk


class DailyAntiChaseTests(unittest.TestCase):
    def test_blocks_late_long_with_daily_extension_and_weak_relative_strength(self):
        daily = {
            "daily_rsi14": 68.0,
            "daily_stoch_rsi14": 100.0,
            "daily_change_7d_pct": 28.0,
            "distance_from_daily_ema20_pct": 11.5,
            "distance_from_daily_ema50_pct": 20.0,
        }
        hidden = {
            "relative_strength_vs_btc_pct": -3.7,
            "relative_strength_vs_market_pct": -2.2,
        }
        result = evaluate_late_entry_risk("LONG", daily, hidden)
        self.assertTrue(result["blocked"])
        self.assertGreaterEqual(result["risk_points"], 4)

    def test_allows_healthy_long_after_reset(self):
        daily = {
            "daily_rsi14": 57.0,
            "daily_stoch_rsi14": 63.0,
            "daily_change_7d_pct": 5.5,
            "distance_from_daily_ema20_pct": 2.8,
            "distance_from_daily_ema50_pct": 5.0,
        }
        hidden = {
            "relative_strength_vs_btc_pct": 2.0,
            "relative_strength_vs_market_pct": 3.1,
        }
        result = evaluate_late_entry_risk("LONG", daily, hidden)
        self.assertFalse(result["blocked"])

    def test_blocks_chasing_oversold_short(self):
        daily = {
            "daily_rsi14": 27.0,
            "daily_stoch_rsi14": 4.0,
            "daily_change_7d_pct": -24.0,
            "distance_from_daily_ema20_pct": -12.0,
            "distance_from_daily_ema50_pct": -20.0,
        }
        hidden = {
            "relative_strength_vs_btc_pct": 2.5,
            "relative_strength_vs_market_pct": 3.0,
        }
        result = evaluate_late_entry_risk("SHORT", daily, hidden)
        self.assertTrue(result["blocked"])
        self.assertGreaterEqual(result["risk_points"], 4)

    def test_single_hot_oscillator_does_not_block_without_extension(self):
        daily = {
            "daily_rsi14": 66.0,
            "daily_stoch_rsi14": 95.0,
            "daily_change_7d_pct": 4.0,
            "distance_from_daily_ema20_pct": 2.0,
            "distance_from_daily_ema50_pct": 3.0,
        }
        result = evaluate_late_entry_risk("LONG", daily, {})
        self.assertFalse(result["blocked"])


if __name__ == "__main__":
    unittest.main()
