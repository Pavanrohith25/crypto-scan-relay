import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

FRESH_MAX_SECONDS = 240
CACHE_MAX_SECONDS = 130
CONFIRM_STREAK = 2
IMMEDIATE_SCORE = 90.0
LOW_OI_MIN_SCORE = 82.0
COOLDOWN_SECONDS = 45 * 60
MAX_ACTIVE_SECONDS = 6 * 60 * 60
DETERIORATION_SCORE_DROP = 10.0
DETERIORATION_FLOOR_DELTA = 8.0

NEW_SCAN = Path("new_scan.json")
LATEST_SCAN = Path("latest_scan.json")
STATE_FILE = Path("scanner_state.json")
EVENTS_FILE = Path("signal_events.jsonl")


def f(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
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
    by_pair = {}
    for section in ("watchlist", "trade_candidates"):
        for candidate in scan.get(section, []) or []:
            pair = candidate.get("pair")
            if pair:
                by_pair[pair] = candidate
    return by_pair


def snapshot(c):
    keys = [
        "pair", "bias", "score", "current_price", "strength", "action",
        "funding_rate_pct", "cross_exchange_median_funding_pct",
        "binance_funding_minus_median_pct", "basis_pct",
        "oi_change_5m_pct", "oi_change_15m_pct", "oi_change_1h_pct",
        "oi_change_4h_pct", "oi_change_24h_pct", "open_interest_usd",
        "spot_buy_sell_ratio_1h", "spot_net_flow_1h_usd",
        "spot_volume_1h_usd", "price_change_1h_spot_pct",
        "price_change_4h_spot_pct", "price_change_24h_futures_pct",
        "futures_volume_change_24h_pct", "invalidation", "reasons",
        "warnings", "blockers",
    ]
    return {k: c.get(k) for k in keys if k in c}


def entry_gate(c, threshold):
    score = f(c.get("score"), 0.0)
    bias = str(c.get("bias") or "").upper()
    flow = f(c.get("spot_net_flow_1h_usd"))
    bs = f(c.get("spot_buy_sell_ratio_1h"))
    oi15 = f(c.get("oi_change_15m_pct"))
    warnings = [str(x).lower() for x in (c.get("warnings") or [])]
    blockers = c.get("blockers") or []

    required_score = LOW_OI_MIN_SCORE if any("low open interest" in w for w in warnings) else threshold
    reasons = []

    if score < required_score:
        reasons.append(f"score {score:.1f} below required {required_score:.1f}")
    if blockers:
        reasons.append("candidate has blocker(s)")
    if flow is None or bs is None:
        reasons.append("missing Binance spot confirmation")
    elif bias == "LONG" and not (flow > 0 and bs >= 1.05):
        reasons.append(f"long spot confirmation weak (B/S {bs:.3f}, flow {flow:,.0f})")
    elif bias == "SHORT" and not (flow < 0 and bs <= 0.95):
        reasons.append(f"short spot confirmation weak (B/S {bs:.3f}, flow {flow:,.0f})")
    if oi15 is None or oi15 <= 0:
        reasons.append("15m OI is not expanding")

    return len(reasons) == 0, required_score, reasons


def formal_invalidation(c, bias):
    oi15 = f(c.get("oi_change_15m_pct"))
    flow = f(c.get("spot_net_flow_1h_usd"))
    bs = f(c.get("spot_buy_sell_ratio_1h"))
    if oi15 is None or flow is None or bs is None:
        return False
    if bias == "LONG":
        return oi15 <= -0.25 and flow < 0 and bs < 0.95
    if bias == "SHORT":
        return oi15 <= -0.25 and flow > 0 and bs > 1.05
    return False


def spot_reversal(c, bias):
    flow = f(c.get("spot_net_flow_1h_usd"))
    bs = f(c.get("spot_buy_sell_ratio_1h"))
    if flow is None or bs is None:
        return False
    if bias == "LONG":
        return flow < 0 and bs < 0.95
    if bias == "SHORT":
        return flow > 0 and bs > 1.05
    return False


def update_excursions(st, c, now_ms):
    entry = f(st.get("confirmed_price"))
    price = f(c.get("current_price"))
    bias = st.get("confirmed_bias")
    if not entry or not price or entry <= 0:
        return

    if bias == "SHORT":
        ret = (entry / price - 1.0) * 100.0
    else:
        ret = (price / entry - 1.0) * 100.0

    st["last_return_pct"] = round(ret, 6)
    st["mfe_pct"] = round(max(f(st.get("mfe_pct"), 0.0), ret), 6)
    st["mae_pct"] = round(min(f(st.get("mae_pct"), 0.0), ret), 6)

    for label, level in (("tp2", 2.0), ("tp3", 3.0), ("tp4", 4.0)):
        key = f"{label}_hit_at_ms"
        if ret >= level and not st.get(key):
            st[key] = now_ms
    for label, level in (("sl1", -1.0), ("sl15", -1.5), ("sl2", -2.0)):
        key = f"{label}_hit_at_ms"
        if ret <= level and not st.get(key):
            st[key] = now_ms


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets missing; alert not sent.")
        return False

    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Telegram returned HTTP {response.status}")
    return True


def candidate_alert(c, streak, immediate):
    reasons = c.get("reasons") or []
    warnings = c.get("warnings") or []
    reason_text = "\n".join(f"- {x}" for x in reasons[:5]) or "- None listed"
    warning_text = "\n".join(f"- {x}" for x in warnings[:5]) or "- None"
    confirmation = "90+ immediate confirmation" if immediate else f"{streak}-scan confirmation"
    return (
        "[ALERT] CONFIRMED CRYPTO SETUP\n\n"
        f"Pair: {c.get('pair')}\n"
        f"Bias: {c.get('bias')}\n"
        f"Score: {c.get('score')}/100\n"
        f"Current price: {c.get('current_price')}\n"
        f"Confirmation: {confirmation}\n\n"
        f"Why it qualified:\n{reason_text}\n\n"
        f"Invalidation:\n{c.get('invalidation', 'Not provided')}\n\n"
        f"Warnings:\n{warning_text}\n\n"
        "Manual review required - this is not an automatic entry."
    )


def deterioration_alert(c, st, flags):
    current_score = f(c.get("score"), 0.0)
    return (
        "[WARNING] SIGNAL DETERIORATION\n\n"
        f"Pair: {c.get('pair')}\n"
        f"Original bias: {st.get('confirmed_bias')}\n"
        f"Current bias: {c.get('bias')}\n"
        f"Entry/confirmation score: {st.get('confirmed_score')}\n"
        f"Current score: {current_score:.1f}\n"
        f"Current price: {c.get('current_price')}\n"
        f"MFE since confirmation: {f(st.get('mfe_pct'), 0.0):.2f}%\n"
        f"MAE since confirmation: {f(st.get('mae_pct'), 0.0):.2f}%\n\n"
        "Material changes:\n" + "\n".join(f"- {x}" for x in flags) + "\n\n"
        f"Current invalidation:\n{c.get('invalidation', 'Not provided')}\n\n"
        "Protect the position / review the thesis. This is not an automatic exit order."
    )


def main():
    scan = load_json(NEW_SCAN, {})
    if not scan:
        raise RuntimeError("new_scan.json is missing or invalid")

    now_ms = int(time.time() * 1000)
    generated_ms = int(scan.get("generated_at_ms") or 0)
    age_seconds = (now_ms - generated_ms) / 1000.0 if generated_ms else 999999
    cache_age = f(scan.get("cache_age_seconds"), 999999)

    if generated_ms <= 0 or age_seconds < -60 or age_seconds > FRESH_MAX_SECONDS or cache_age > CACHE_MAX_SECONDS:
        print(f"Scan not fresh enough (age={age_seconds:.1f}s, cache={cache_age}s). Skipping publish and alerts.")
        return

    threshold = f(scan.get("thresholds", {}).get("minimum_candidate_score"), 78.0)
    current = combined_candidates(scan)
    state = load_json(STATE_FILE, {"version": 2, "pairs": {}})
    state.setdefault("version", 2)
    state.setdefault("pairs", {})
    state["last_processed_generated_at_ms"] = generated_ms
    state["last_processed_at_ms"] = now_ms
    state["minimum_candidate_score"] = threshold

    alerts = []

    for pair, c in current.items():
        st = state["pairs"].setdefault(pair, {})
        score = f(c.get("score"), 0.0)
        bias = str(c.get("bias") or "").upper()
        st["last_seen_at_ms"] = now_ms
        st["last_score"] = score
        st["last_bias"] = bias
        st["last_price"] = c.get("current_price")

        if st.get("active"):
            update_excursions(st, c, now_ms)
            confirmed_bias = st.get("confirmed_bias")
            confirmed_score = f(st.get("confirmed_score"), score)
            previous_score = f(st.get("previous_score"), confirmed_score)
            flags = []

            if bias and confirmed_bias and bias != confirmed_bias:
                flags.append(f"bias flipped {confirmed_bias} -> {bias}")
            if formal_invalidation(c, confirmed_bias):
                flags.append("formal OI + spot invalidation is now true")
            if spot_reversal(c, confirmed_bias):
                flags.append("spot confirmation reversed against the original thesis")

            below_floor = score <= threshold - DETERIORATION_FLOOR_DELTA
            fast_drop = previous_score - score >= DETERIORATION_SCORE_DROP and score < threshold
            if below_floor or fast_drop:
                flags.append(f"score deteriorated to {score:.1f} from {previous_score:.1f}")

            active_age = (now_ms - int(st.get("confirmed_at_ms") or now_ms)) / 1000.0
            if active_age >= MAX_ACTIVE_SECONDS:
                flags.append("signal reached the 6-hour research horizon")

            if flags:
                alerts.append(deterioration_alert(c, st, flags))
                event_type = "SIGNAL_EXPIRED" if flags == ["signal reached the 6-hour research horizon"] else "SIGNAL_DETERIORATED"
                append_event({
                    "event": event_type,
                    "timestamp_ms": now_ms,
                    "generated_at_ms": generated_ms,
                    "pair": pair,
                    "flags": flags,
                    "entry": st.get("entry_snapshot"),
                    "exit_snapshot": snapshot(c),
                    "confirmed_price": st.get("confirmed_price"),
                    "confirmed_score": st.get("confirmed_score"),
                    "mfe_pct": st.get("mfe_pct"),
                    "mae_pct": st.get("mae_pct"),
                    "tp2_hit_at_ms": st.get("tp2_hit_at_ms"),
                    "tp3_hit_at_ms": st.get("tp3_hit_at_ms"),
                    "tp4_hit_at_ms": st.get("tp4_hit_at_ms"),
                    "sl1_hit_at_ms": st.get("sl1_hit_at_ms"),
                    "sl15_hit_at_ms": st.get("sl15_hit_at_ms"),
                    "sl2_hit_at_ms": st.get("sl2_hit_at_ms"),
                })
                st["active"] = False
                st["deteriorated_at_ms"] = now_ms
                st["cooldown_until_ms"] = now_ms + COOLDOWN_SECONDS * 1000
                st["qualifying_streak"] = 0

            st["previous_score"] = score
            continue

        cooldown_until = int(st.get("cooldown_until_ms") or 0)
        gate_ok, required_score, gate_reasons = entry_gate(c, threshold)
        qualifying = score >= required_score and gate_ok

        if now_ms < cooldown_until:
            st["qualifying_streak"] = 0
            st["gate_reasons"] = ["pair cooldown active after deterioration"]
            continue

        if qualifying:
            st["qualifying_streak"] = int(st.get("qualifying_streak") or 0) + 1
        else:
            st["qualifying_streak"] = 0
        st["gate_reasons"] = gate_reasons
        st["required_score"] = required_score

        immediate = qualifying and score >= IMMEDIATE_SCORE
        confirmed = immediate or (qualifying and st["qualifying_streak"] >= CONFIRM_STREAK)

        if confirmed:
            st.update({
                "active": True,
                "confirmed_at_ms": now_ms,
                "confirmed_generated_at_ms": generated_ms,
                "confirmed_bias": bias,
                "confirmed_score": score,
                "confirmed_price": c.get("current_price"),
                "previous_score": score,
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "entry_snapshot": snapshot(c),
                "cooldown_until_ms": 0,
            })
            for key in ("tp2_hit_at_ms", "tp3_hit_at_ms", "tp4_hit_at_ms", "sl1_hit_at_ms", "sl15_hit_at_ms", "sl2_hit_at_ms"):
                st.pop(key, None)

            alerts.append(candidate_alert(c, st["qualifying_streak"], immediate))
            append_event({
                "event": "SIGNAL_CONFIRMED",
                "timestamp_ms": now_ms,
                "generated_at_ms": generated_ms,
                "pair": pair,
                "confirmation": "immediate_90_plus" if immediate else "two_scan",
                "snapshot": snapshot(c),
            })

    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    LATEST_SCAN.write_text(json.dumps(scan, indent=2, sort_keys=True) + "\n")

    if not EVENTS_FILE.exists():
        EVENTS_FILE.write_text("")

    if not alerts:
        print("No confirmed new setup or material deterioration alert.")
        return

    for text in alerts:
        sent = send_telegram(text)
        print("Telegram alert sent." if sent else "Telegram alert skipped.")


if __name__ == "__main__":
    main()
