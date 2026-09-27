import unittest

from swing_entry_drift import directional_drift_pct, drift_bucket, evaluate_entry_drift


class EntryDriftTests(unittest.TestCase):
    def test_long_directional_drift(self):
        self.assertAlmostEqual(directional_drift_pct(100.0, 101.0, "LONG"), 1.0, places=6)

    def test_short_directional_drift(self):
        self.assertAlmostEqual(directional_drift_pct(100.0, 99.0, "SHORT"), 1.010101, places=6)

    def test_research_buckets(self):
        self.assertEqual(drift_bucket(-0.2), "against_trigger")
        self.assertEqual(drift_bucket(0.2), "0_to_0.5")
        self.assertEqual(drift_bucket(0.7), "0.5_to_1")
        self.assertEqual(drift_bucket(1.4), "1_to_2")
        self.assertEqual(drift_bucket(2.4), "2_to_3")
        self.assertEqual(drift_bucket(3.4), "3_to_5")
        self.assertEqual(drift_bucket(5.0), "5_plus")

    def test_blocks_when_target_already_reached(self):
        result = evaluate_entry_drift(100.0, 105.1, "LONG", 5.0, 6.0)
        self.assertTrue(result["blocked"])
        self.assertIn("target already", result["reason"])

    def test_blocks_when_stop_already_reached(self):
        result = evaluate_entry_drift(100.0, 93.9, "LONG", 5.0, 6.0)
        self.assertTrue(result["blocked"])
        self.assertIn("stop already", result["reason"])

    def test_does_not_promote_arbitrary_cutoff(self):
        result = evaluate_entry_drift(100.0, 102.5, "LONG", 5.0, 6.0)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["drift_bucket"], "2_to_3")


if __name__ == "__main__":
    unittest.main()
