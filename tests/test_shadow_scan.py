import unittest
from shadow_scan import process_scan

def scan_of(*cands, ts=1_000_000):
    return {
        "generated_at_ms": ts,
        "cache_age_seconds": 0,
        "thresholds": {"minimum_candidate_score": 78},
        "trade_candidates": list(cands),
        "watchlist": [],
    }

def base(pair, price, score, **kw):
    c = {
        "pair": pair,
        "bias": "LONG",
        "current_price": price,
        "score": score,
        "blockers": [],
        "warnings": [],
        "spot_buy_sell_ratio_1h": 1.166,
        "spot_net_flow_1h_usd": 126437.98,
        "spot_volume_1h_usd": 1650545.39,
        "oi_change_15m_pct": 0.68,
        "oi_change_1h_pct": 5.48,
        "oi_change_4h_pct": 12.16,
        "oi_change_24h_pct": 18.62,
        "price_change_1h_spot_pct": 1.51,
        "price_change_4h_spot_pct": 6.66,
        "price_change_24h_futures_pct": 20.2,
    }
    c.update(kw)
    return c

class ShadowTests(unittest.TestCase):
    def test_lsk_arms_survives_score_dip_then_confirms(self):
        state = {"version":"3.2-shadow","pairs":{}}
        l1 = base("LSKUSDT", 0.37144, 82)
        state, e1 = process_scan(scan_of(l1), state, now_ms=1_000_000, emit_events=False)
        self.assertTrue(state["pairs"]["LSKUSDT"]["armed"])
        self.assertEqual(e1[0]["event"], "SHADOW_ARMED")

        l2 = base("LSKUSDT", 0.36982, 76,
                  oi_change_15m_pct=1.28, oi_change_1h_pct=4.57,
                  oi_change_4h_pct=12.97, oi_change_24h_pct=17.53,
                  price_change_24h_futures_pct=19.53)
        state, e2 = process_scan(scan_of(l2, ts=1_300_000), state, now_ms=1_300_000, emit_events=False)
        self.assertTrue(state["pairs"]["LSKUSDT"]["armed"])
        self.assertFalse(state["pairs"]["LSKUSDT"].get("active", False))

        l3 = base("LSKUSDT", 0.37765, 82,
                  oi_change_15m_pct=0.87, oi_change_1h_pct=9.69,
                  oi_change_4h_pct=13.85, oi_change_24h_pct=21.72,
                  price_change_24h_futures_pct=23.71)
        state, e3 = process_scan(scan_of(l3, ts=1_600_000), state, now_ms=1_600_000, emit_events=False)
        self.assertTrue(state["pairs"]["LSKUSDT"]["active"])
        self.assertTrue(any(x["event"] == "SHADOW_CONFIRMED" for x in e3))

    def test_one_90_is_blocked_as_deep_countertrend(self):
        one = base(
            "ONEUSDT", 0.0023828, 90,
            spot_buy_sell_ratio_1h=1.214,
            spot_net_flow_1h_usd=37374.23,
            spot_volume_1h_usd=387408.42,
            oi_change_15m_pct=3.01,
            oi_change_1h_pct=4.9,
            oi_change_4h_pct=-1.43,
            oi_change_24h_pct=-22.58,
            price_change_1h_spot_pct=0.41,
            price_change_4h_spot_pct=-5.98,
            price_change_24h_futures_pct=-21.8,
        )
        state, events = process_scan(scan_of(one), {"version":"3.2-shadow","pairs":{}},
                                     now_ms=1_000_000, emit_events=False)
        st = state["pairs"]["ONEUSDT"]
        self.assertEqual(st["last_decision"], "BLOCKED_DEEP_COUNTERTREND")
        self.assertFalse(st.get("armed", False))
        self.assertFalse(st.get("active", False))

    def test_steem_low_liquidity_does_not_arm(self):
        steem = base(
            "STEEMUSDT", 0.06269, 85,
            warnings=["low open interest"],
            spot_buy_sell_ratio_1h=1.083,
            spot_net_flow_1h_usd=2890.91,
            spot_volume_1h_usd=72352.63,
            oi_change_15m_pct=1.68,
            oi_change_1h_pct=-2.31,
            oi_change_4h_pct=11.76,
            oi_change_24h_pct=24.96,
            price_change_1h_spot_pct=-1.07,
            price_change_4h_spot_pct=0.42,
            price_change_24h_futures_pct=4.08,
        )
        state, events = process_scan(scan_of(steem), {"version":"3.2-shadow","pairs":{}},
                                     now_ms=1_000_000, emit_events=False)
        st = state["pairs"]["STEEMUSDT"]
        self.assertEqual(st["last_decision"], "WAIT")
        self.assertFalse(st.get("armed", False))
        self.assertTrue(any("spot volume" in r for r in st["gate_reasons"]))

if __name__ == "__main__":
    unittest.main()
