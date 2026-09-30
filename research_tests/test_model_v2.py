import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from historical_futures_backfill_v2 import DTYPE, MS_5M, aggregate, validate_bars
from historical_futures_dataset_v2 import ArraySeries, entry_gate, path_labels
from train_swing_model_v2 import (HOUR, MIN_ALERTS, alert_indices, choose_threshold,
                                 metrics_for_indices, qualifies, split_by_time)


def candles(n=576, price=100., start=0):
    a = np.zeros(n, dtype=DTYPE)
    a["open_time_ms"] = start + np.arange(n) * MS_5M
    a["close_time_ms"] = a["open_time_ms"] + MS_5M - 1
    for k in ["open", "high", "low", "close"]:
        a[k] = price
    a["volume"], a["quote_volume"], a["taker_buy_quote_volume"] = 1., 100., 50.
    return a


class PathTests(unittest.TestCase):
    def test_clean_long_short_and_stop_first(self):
        a = candles()
        a[1]["high"] = 105.
        r = path_labels("LONG", a)
        self.assertEqual(r["target5_before_stop1_5_48h"], 1)
        self.assertAlmostEqual(r["sim_return_target5_stop1_5_48h_pct"], 5.)
        a[0]["low"] = 98.
        r = path_labels("LONG", a)
        self.assertEqual(r["target5_before_stop1_5_48h"], 0)
        self.assertEqual(r["eventual_target5_48h"], 1)
        self.assertAlmostEqual(r["sim_return_target5_stop1_5_48h_pct"], -1.5)
        a = candles()
        a[1]["low"] = 95.
        self.assertEqual(path_labels("SHORT", a)["target5_before_stop1_5_48h"], 1)

    def test_ambiguity_is_loss_and_execution_uses_next_open(self):
        a = candles(price=110.)
        a[1]["high"], a[1]["low"] = 116., 108.
        r = path_labels("LONG", a)
        self.assertEqual(r["entry_price"], 110.)
        self.assertEqual(r["path_target5_stop1_5_48h"], "AMBIGUOUS")
        self.assertLess(r["sim_return_target5_stop1_5_48h_pct"], 0)

    def test_gap_through_stop_uses_executable_open(self):
        a = candles()
        for k in ["open", "high", "low", "close"]:
            a[1][k] = 94.
        self.assertAlmostEqual(path_labels("LONG", a)["sim_return_target5_stop1_5_48h_pct"], -6.)
        a = candles()
        for k in ["open", "high", "low", "close"]:
            a[1][k] = 106.
        self.assertAlmostEqual(path_labels("SHORT", a)["sim_return_target5_stop1_5_48h_pct"], -6.)

    def test_timeout_returns_horizon_close(self):
        a = candles()
        a[-1]["close"], a[-1]["high"] = 102., 102.
        r = path_labels("LONG", a)
        self.assertEqual(r["path_target5_stop1_5_48h"], "TIMEOUT")
        self.assertAlmostEqual(r["sim_return_target5_stop1_5_48h_pct"], 2.)
        self.assertEqual(r["exit_ms"], 48 * HOUR)


class HistoryTests(unittest.TestCase):
    def test_gap_and_partial_bucket_rejected(self):
        a = candles(7)
        self.assertEqual(len(aggregate(a, 15)), 2)
        a = np.delete(a, 1)
        self.assertEqual(len(aggregate(a, 15)), 1)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "5m.npy"
            np.save(p, a)
            series = ArraySeries(p, MS_5M)
            self.assertEqual(series.closed(3 * MS_5M, 3), [])

    def test_future_candles_cannot_change_features(self):
        a = candles(1000)
        ts = 300 * MS_5M
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "5m.npy"
            np.save(p, a)
            s = ArraySeries(p, MS_5M)
            before, snap = s.closed(ts, 289), s.snapshot(ts)
            self.assertEqual(len(before), 289)
            self.assertTrue(all(r["close_time_ms"] < ts for r in before))
            del s
            a[300:]["close"] = 999.
            a[300:]["quote_volume"] = 1e9
            np.save(p, a)
            s = ArraySeries(p, MS_5M)
            self.assertEqual(before, s.closed(ts, 289))
            self.assertEqual(snap, s.snapshot(ts))
            self.assertEqual(s.forward(ts)[0]["open_time_ms"], ts)

    def test_bad_values_and_duplicate_times_fail(self):
        a = candles(10)
        validate_bars(a)
        a[1]["quote_volume"] = float("nan")
        with self.assertRaises(ValueError):
            validate_bars(a)
        a = candles(10)
        a[1]["open_time_ms"] = 0
        with self.assertRaises(ValueError):
            validate_bars(a)


class SelectionTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame([
            {"asof_ms": 0, "exit_ms": 2 * HOUR, "symbol": "DOGEUSDT", "direction": "SHORT", "entry_eligible": 1},
            {"asof_ms": 0, "exit_ms": HOUR, "symbol": "BCHUSDT", "direction": "SHORT", "entry_eligible": 1},
            {"asof_ms": HOUR, "exit_ms": 3 * HOUR, "symbol": "FILUSDT", "direction": "SHORT", "entry_eligible": 1},
            {"asof_ms": 2 * HOUR, "exit_ms": 4 * HOUR, "symbol": "DOGEUSDT", "direction": "SHORT", "entry_eligible": 1},
        ])

    def test_select_highest_probability_cap_and_cooldown(self):
        df = self.frame()
        idx = alert_indices(df, np.array([.7, .9, .8, .9]), .6)
        self.assertEqual(idx.tolist(), [1, 2])

    def test_regime_gate_and_chase(self):
        r = {"direction": "SHORT", "market_regime": "RISK_ON", "retest_ready": 1,
             "confirmation_15m": 1, "anti_chase_abs_12h_pct": 2., "distance_from_15m_sma4_pct": .1}
        self.assertFalse(entry_gate(r))
        r["market_regime"] = "RISK_OFF"
        self.assertTrue(entry_gate(r))
        r["anti_chase_abs_12h_pct"] = 9.
        self.assertFalse(entry_gate(r))

    def test_purged_splits_and_time_group_integrity(self):
        times = np.repeat(np.arange(1000) * 12 * HOUR, 3)
        df = pd.DataFrame({"asof_ms": times, "symbol": ["a", "b", "c"] * 1000})
        fit, cal, val, test, _ = split_by_time(df)
        for a, b in [(fit, cal), (cal, val), (val, test)]:
            self.assertLess(a["asof_ms"].max() + 72 * HOUR, b["asof_ms"].min())
            self.assertFalse(set(a["asof_ms"]) & set(b["asof_ms"]))

    def test_no_validation_edge_means_no_trade(self):
        df = self.frame()
        for c, v in {"target5_before_stop1_5_48h": 0, "sim_return_target5_stop1_5_48h_pct": -1.5,
                     "ambiguous_target5_stop1_5_48h": 0, "mae_first_15m_pct": -1., "mae_first_60m_pct": -1.}.items():
            df[c] = v
        threshold, _ = choose_threshold(df, np.full(len(df), .9))
        self.assertGreater(threshold, 1.)
        self.assertEqual(len(alert_indices(df, np.full(len(df), .9), threshold)), 0)

    def test_60pct_sample_and_profit_gate(self):
        row = {"alerts": 100, "precision": .6, "avg_net_return_pct": .1, "profit_factor": 1.1}
        self.assertTrue(qualifies(row))
        for field, value in [("alerts", 99), ("precision", .599), ("avg_net_return_pct", 0), ("profit_factor", 1.)]:
            self.assertFalse(qualifies({**row, field: value}))


if __name__ == "__main__":
    unittest.main()
