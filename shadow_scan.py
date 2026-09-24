import json
import math
import time
from pathlib import Path

# v3.2 SHADOW ONLY
# This file does NOT send Telegram alerts and does NOT alter v3.1 decisions.
# It records what the next-generation entry + position manager would have done.

SCAN_FILE = Path("new_scan.json")
STATE_FILE = Path("shadow_state.json")
EVENTS_FILE = Path("shadow_events.jsonl")

BASE_SCORE = 78.0
LOW_OI_SCORE = 82.0
PERSISTENCE_FLOOR_DROP = 6.0
FALSE_START_SCORE_DROP = 10.0

ARM_TTL_SECONDS = 30 * 60
CONTINUATION_MIN_OBS = 2
COUNTERTREND_MIN_OBS = 3

PRICE_TRIGGER_CONTINUATION_PCT = 0.50
PRICE_TRIGGER_COUNTERTREND_PCT = 0.75
ANTI_CHASE_1H_PCT = 4.0

MIN_SPOT_VOLUME_USD = 50_000.0
LOW_OI_MIN_SPOT_VOLUME_USD = 100_000.0

DEEP_COUNTERTREND_24H_PCT = 8.0
DEEP_COUNTERTREND_OI24_PCT = -8.0


def f(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def append_event(event):
    with EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def combined_candidates(scan):
    out = {}
    for section in ("watchlist", "trade_candidates"):
        for c in scan.get(section, []) or []:
            pair = c.get("pair")
            if pair:
                out[pair] = c
    return out


def warnings_lower(c):
    return [str(x).lower() for x in (c.get("warnings") or [])]


def low_oi(c):
    return any("low open interest" in w for w in warnings_lower(c))


def required_score(c, base_threshold):
    return LOW_OI_SCORE if low_oi(c) else base_threshold


def directed(value, bias):
    x = f(value)
    if x is None:
        return None
    return x if bias == "LONG" else -x


def evidence_fingerprint(c):
    # Exact duplicates should not count as independent confirmations.
    vals = (
        c.get("current_price"),
        c.get("spot_buy_sell_ratio_1h"),
        c.get("spot_net_flow_1h_usd"),
        c.get("spot_volume_1h_usd"),
        c.get("oi_change_15m_pct"),
        c.get("oi_change_1h_pct"),
        c.get("price_change_1h_spot_pct"),
    )
    return "|".join("" if v is None else str(v) for v in vals)


def liquidity_ok(c):
    vol = f(c.get("spot_volume_1h_usd"))
    if vol is None:
        return False, "missing spot volume"
    minimum = LOW_OI_MIN_SPOT_VOLUME_USD if low_oi(c) else MIN_SPOT_VOLUME_USD
    if vol < minimum:
        return False, f"spot volume {vol:,.0f} below required {minimum:,.0f}"
    return True, None


def spot_ok(c, bias):
    flow = f(c.get("spot_net_flow_1h_usd"))
    bs = f(c.get("spot_buy_sell_ratio_1h"))
    if flow is None or bs is None:
        return False, "missing spot confirmation"
    if bias == "LONG":
        if flow <= 0 or bs < 1.05:
            return False, f"long spot weak (B/S {bs:.3f}, flow {flow:,.0f})"
    else:
        if flow >= 0 or bs > 0.95:
            return False, f"short spot weak (B/S {bs:.3f}, flow {flow:,.0f})"
    return True, None


def oi15_ok(c):
    oi15 = f(c.get("oi_change_15m_pct"))
    if oi15 is None or oi15 <= 0:
        return False, "15m OI not expanding"
    return True, None


def anti_chase(c, bias):
    p1 = directed(c.get("price_change_1h_spot_pct"), bias)
    return p1 is not None and p1 >= ANTI_CHASE_1H_PCT


def classify_regime(c, bias):
    p4 = directed(c.get("price_change_4h_spot_pct"), bias)
    p24 = directed(c.get("price_change_24h_futures_pct"), bias)
    oi4 = f(c.get("oi_change_4h_pct"))
    oi24 = f(c.get("oi_change_24h_pct"))

    # Severe opposite 24h move or broad OI destruction = deep countertrend.
    if p24 is not None and p24 <= -DEEP_COUNTERTREND_24H_PCT:
        return "COUNTERTREND_DEEP"
    if oi24 is not None and oi24 <= DEEP_COUNTERTREND_OI24_PCT and (p4 is None or p4 < 1.0):
        return "COUNTERTREND_DEEP"

    continuation_votes = 0
    total_votes = 0
    for val, predicate in (
        (p4, lambda x: x >= 0.5),
        (p24, lambda x: x >= -2.0),
        (oi4, lambda x: x >= 0.0),
        (oi24, lambda x: x >= 0.0),
    ):
        if val is not None:
            total_votes += 1
            continuation_votes += int(predicate(val))

    if total_votes >= 3 and continuation_votes >= 3:
        return "TREND_CONTINUATION"
    return "COUNTERTREND"


def base_quality(c, bias, threshold):
    reasons = []
    score = f(c.get("score"), 0.0)
    req = required_score(c, threshold)

    if score < req:
        reasons.append(f"score {score:.1f} below required {req:.1f}")
    if c.get("blockers"):
        reasons.append("candidate has blocker(s)")

    ok, why = spot_ok(c, bias)
    if not ok:
        reasons.append(why)

    ok, why = oi15_ok(c)
    if not ok:
        reasons.append(why)

    ok, why = liquidity_ok(c)
    if not ok:
        reasons.append(why)

    return not reasons, req, reasons


def structural_health(c, bias, req):
    reasons = []
    score = f(c.get("score"), 0.0)

    if score < req - PERSISTENCE_FLOOR_DROP:
        reasons.append(f"score {score:.1f} below persistence floor {req - PERSISTENCE_FLOOR_DROP:.1f}")
    if c.get("blockers"):
        reasons.append("candidate has blocker(s)")

    ok, why = spot_ok(c, bias)
    if not ok:
        reasons.append(why)

    oi15 = f(c.get("oi_change_15m_pct"))
    if oi15 is None or oi15 <= -0.25:
        reasons.append("15m OI materially negative")

    ok, why = liquidity_ok(c)
    if not ok:
        reasons.append(why)

    return not reasons, reasons


def price_move_from_anchor(anchor, price, bias):
    a, p = f(anchor), f(price)
    if not a or not p or a <= 0:
        return None
    raw = (p / a - 1.0) * 100.0
    return raw if bias == "LONG" else -raw


def snapshot(c):
    keys = [
        "pair", "bias", "score", "current_price", "funding_rate_pct",
        "cross_exchange_median_funding_pct", "basis_pct",
        "oi_change_5m_pct", "oi_change_15m_pct", "oi_change_1h_pct",
        "oi_change_4h_pct", "oi_change_24h_pct", "open_interest_usd",
        "spot_buy_sell_ratio_1h", "spot_net_flow_1h_usd",
        "spot_volume_1h_usd", "price_change_1h_spot_pct",
        "price_change_4h_spot_pct", "price_change_24h_futures_pct",
        "warnings", "blockers", "reasons",
    ]
    return {k: c.get(k) for k in keys if k in c}


def reset_arm(st):
    for key in (
        "armed", "armed_at_ms", "anchor_price", "anchor_score", "anchor_regime",
        "evidence_count", "last_fingerprint", "required_score", "high_priority"
    ):
        st.pop(key, None)


def position_state(c, bias, entry_price):
    """
    Bigger-picture position manager.
    It intentionally ignores tiny 1m/5m price noise and relies on spot + 4h/24h context.
    """
    regime = classify_regime(c, bias)
    spot_good, _ = spot_ok(c, bias)
    oi4 = f(c.get("oi_change_4h_pct"))
    oi24 = f(c.get("oi_change_24h_pct"))
    p4 = directed(c.get("price_change_4h_spot_pct"), bias)
    p24 = directed(c.get("price_change_24h_futures_pct"), bias)
    move = price_move_from_anchor(entry_price, c.get("current_price"), bias)

    bad = 0
    severe = 0

    if not spot_good:
        bad += 1
    if oi4 is not None and oi4 < 0:
        bad += 1
    if p4 is not None and p4 < 0:
        bad += 1
    if oi24 is not None and oi24 < -8:
        severe += 1
    if p24 is not None and p24 < -8:
        severe += 1
    if regime == "COUNTERTREND_DEEP":
        severe += 1

    if severe >= 2 and not spot_good:
        return "EXIT_THESIS_BROKEN"
    if bad >= 3 or (severe >= 1 and bad >= 2):
        return "REDUCE"
    if bad >= 1 or regime != "TREND_CONTINUATION":
        return "HOLD_PROTECT"
    if move is not None and move >= 0:
        return "HOLD_STRONG"
    return "HOLD"


def process_scan(scan, state=None, now_ms=None, emit_events=True):
    now_ms = now_ms or int(time.time() * 1000)
    threshold = f(scan.get("thresholds", {}).get("minimum_candidate_score"), BASE_SCORE)
    state = state or {"version": "3.2-shadow", "pairs": {}}
    state.setdefault("version", "3.2-shadow")
    state.setdefault("pairs", {})
    state["last_processed_generated_at_ms"] = scan.get("generated_at_ms")
    state["last_processed_at_ms"] = now_ms

    emitted = []

    def emit(payload):
        emitted.append(payload)
        if emit_events:
            append_event(payload)

    for pair, c in combined_candidates(scan).items():
        bias = str(c.get("bias") or "").upper()
        if bias not in ("LONG", "SHORT"):
            continue

        st = state["pairs"].setdefault(pair, {})
        st["last_seen_at_ms"] = now_ms
        st["last_score"] = f(c.get("score"), 0.0)
        st["last_price"] = c.get("current_price")
        st["last_bias"] = bias
        st["last_regime"] = classify_regime(c, bias)

        # Shadow position manager after a shadow confirmation.
        if st.get("active"):
            new_pos_state = position_state(c, st["confirmed_bias"], st["confirmed_price"])
            old_pos_state = st.get("position_state")
            st["position_state"] = new_pos_state
            st["last_position_check_ms"] = now_ms
            if new_pos_state != old_pos_state:
                emit({
                    "event": "SHADOW_POSITION_STATE",
                    "timestamp_ms": now_ms,
                    "pair": pair,
                    "state": new_pos_state,
                    "previous_state": old_pos_state,
                    "snapshot": snapshot(c),
                })
            if new_pos_state == "EXIT_THESIS_BROKEN":
                st["active"] = False
                st["closed_at_ms"] = now_ms
            continue

        ok, req, gate_reasons = base_quality(c, bias, threshold)
        regime = classify_regime(c, bias)
        score = f(c.get("score"), 0.0)
        fp = evidence_fingerprint(c)

        # Deep countertrend setups are observations only, never entries in shadow v3.2.
        if regime == "COUNTERTREND_DEEP":
            st["last_decision"] = "BLOCKED_DEEP_COUNTERTREND"
            st["gate_reasons"] = [
                "deep countertrend: 24h structure/OI conflicts with the setup"
            ] + gate_reasons
            reset_arm(st)
            continue

        if not st.get("armed"):
            if ok:
                st.update({
                    "armed": True,
                    "armed_at_ms": now_ms,
                    "anchor_price": c.get("current_price"),
                    "anchor_score": score,
                    "anchor_regime": regime,
                    "evidence_count": 1,
                    "last_fingerprint": fp,
                    "required_score": req,
                    "high_priority": score >= 90.0,
                    "last_decision": "ARMED_HIGH_PRIORITY" if score >= 90.0 else "ARMED",
                    "gate_reasons": [],
                })
                emit({
                    "event": "SHADOW_ARMED",
                    "timestamp_ms": now_ms,
                    "pair": pair,
                    "regime": regime,
                    "high_priority": score >= 90.0,
                    "snapshot": snapshot(c),
                })
            else:
                st["last_decision"] = "WAIT"
                st["gate_reasons"] = gate_reasons
            continue

        # Existing armed setup.
        age_s = (now_ms - int(st.get("armed_at_ms") or now_ms)) / 1000.0
        req = f(st.get("required_score"), req)
        if age_s > ARM_TTL_SECONDS:
            emit({
                "event": "SHADOW_ARM_EXPIRED",
                "timestamp_ms": now_ms,
                "pair": pair,
                "age_seconds": round(age_s, 1),
            })
            reset_arm(st)
            st["last_decision"] = "EXPIRED"
            continue

        healthy, health_reasons = structural_health(c, bias, req)
        anchor_score = f(st.get("anchor_score"), score)
        if (not healthy) or (anchor_score - score >= FALSE_START_SCORE_DROP and score < req):
            emit({
                "event": "SHADOW_FALSE_START",
                "timestamp_ms": now_ms,
                "pair": pair,
                "reasons": health_reasons + (
                    [f"score fell {anchor_score - score:.1f} from armed level"] if anchor_score - score >= FALSE_START_SCORE_DROP else []
                ),
                "snapshot": snapshot(c),
            })
            reset_arm(st)
            st["last_decision"] = "FALSE_START"
            st["gate_reasons"] = health_reasons
            continue

        if fp != st.get("last_fingerprint"):
            st["evidence_count"] = int(st.get("evidence_count") or 0) + 1
            st["last_fingerprint"] = fp

        regime = classify_regime(c, bias)
        st["last_regime"] = regime
        move = price_move_from_anchor(st.get("anchor_price"), c.get("current_price"), bias)
        chased = anti_chase(c, bias)

        if regime == "TREND_CONTINUATION":
            need_obs = CONTINUATION_MIN_OBS
            need_move = PRICE_TRIGGER_CONTINUATION_PCT
            regime_ok = True
        else:
            need_obs = COUNTERTREND_MIN_OBS
            need_move = PRICE_TRIGGER_COUNTERTREND_PCT
            # Moderate countertrend must show an actual 4h reclaim and no broad OI destruction.
            p4 = directed(c.get("price_change_4h_spot_pct"), bias)
            oi4 = f(c.get("oi_change_4h_pct"))
            oi24 = f(c.get("oi_change_24h_pct"))
            regime_ok = (
                p4 is not None and p4 >= 0.5 and
                oi4 is not None and oi4 >= 0 and
                (oi24 is None or oi24 > DEEP_COUNTERTREND_OI24_PCT)
            )

        can_confirm = (
            int(st.get("evidence_count") or 0) >= need_obs and
            move is not None and move >= need_move and
            score >= req - PERSISTENCE_FLOOR_DROP and
            regime_ok and
            not chased
        )

        if can_confirm:
            st.update({
                "active": True,
                "confirmed_at_ms": now_ms,
                "confirmed_price": c.get("current_price"),
                "confirmed_score": score,
                "confirmed_bias": bias,
                "confirmed_regime": regime,
                "position_state": "HOLD",
                "last_decision": "SHADOW_CONFIRMED",
            })
            emit({
                "event": "SHADOW_CONFIRMED",
                "timestamp_ms": now_ms,
                "pair": pair,
                "regime": regime,
                "evidence_count": st.get("evidence_count"),
                "price_trigger_pct": round(move, 4),
                "snapshot": snapshot(c),
            })
            reset_arm(st)
        else:
            st["last_decision"] = "ARMED_WAITING_TRIGGER"
            st["gate_reasons"] = []
            if chased:
                st["gate_reasons"].append(
                    f"anti-chase: 1h move already >= {ANTI_CHASE_1H_PCT:.1f}% in trade direction"
                )
            if move is None or move < need_move:
                st["gate_reasons"].append(
                    f"price follow-through {move if move is not None else 'n/a'} below {need_move:.2f}%"
                )
            if not regime_ok:
                st["gate_reasons"].append("countertrend reclaim not strong enough")

    return state, emitted


def main():
    scan = load_json(SCAN_FILE, {})
    if not scan:
        raise RuntimeError("new_scan.json missing or invalid")

    now_ms = int(time.time() * 1000)
    generated_ms = int(scan.get("generated_at_ms") or 0)
    cache_age = f(scan.get("cache_age_seconds"), 999999)
    age_s = (now_ms - generated_ms) / 1000.0 if generated_ms else 999999

    # Same freshness philosophy as v3.1.
    if generated_ms <= 0 or age_s < -60 or age_s > 240 or cache_age > 130:
        print(f"Shadow scan skipped: stale data (age={age_s:.1f}s, cache={cache_age}s)")
        return

    state = load_json(STATE_FILE, {"version": "3.2-shadow", "pairs": {}})
    state, events = process_scan(scan, state=state, now_ms=now_ms, emit_events=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    if not EVENTS_FILE.exists():
        EVENTS_FILE.write_text("")

    print(f"v3.2 shadow processed. events={len(events)}")
    for e in events:
        print(e["event"], e.get("pair"), e.get("state", ""))


if __name__ == "__main__":
    main()
