import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from swing_ranker import classify_market_regime, score_candidate

EVENTS_FILE = Path("swing_events.jsonl")
STATE_FILE = Path("swing_telegram_state.json")
FILTER_EVENTS_FILE = Path("swing_telegram_filter_events.jsonl")
SWING_SCAN_FILE = Path("new_swing.json")
HISTORICAL_GATE_FILE = Path("historical_gate_config.json")

BINANCE_DATA_BASE = "https://data-api.binance.vision"
DEFAULT_TP_PCT = 5.0
DEFAULT_SL_PCT = 6.0
HOLD_WINDOW = "24-48h"
DAILY_LOOKBACK = 80


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def append_filter_event(payload):
    with FILTER_EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def quality_bucket(score):
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    if score < 50:
        return "0-49"
    if score < 60:
        return "50-59"
    if score < 70:
        return "60-69"
    if score < 80:
        return "70-79"
    return "80-100"


def current_hidden_context(symbol):
    payload = load_json(SWING_SCAN_FILE, {})
    pool = payload.get("analysis_pool") if isinstance(payload, dict) else None
    ctx = payload.get("market_context") if isinstance(payload, dict) else None
    if not isinstance(pool, list) or not isinstance(ctx, dict):
        return None

    row = next((x for x in pool if isinstance(x, dict) and x.get("symbol") == symbol), None)
    if row is None:
        return None

    regime, _ = classify_market_regime(ctx)
    quality = score_candidate(row, ctx, regime)
    score = quality.get("swing_quality_score_v1")
    return {
        "market_regime": regime,
        "quality_bucket": quality_bucket(score),
        "quality_score": score,
        "relative_strength_vs_btc_pct": quality.get("relative_strength_vs_btc_pct"),
        "relative_strength_vs_market_pct": quality.get("relative_strength_vs_market_pct"),
        "momentum_24h_pct": row.get("momentum_24h_pct"),
        "volume_expansion_1h": row.get("volume_expansion_1h"),
        "swing_score": row.get("swing_score"),
    }


def historical_gate_allows(symbol):
    gate = load_json(HISTORICAL_GATE_FILE, {})
    if not isinstance(gate, dict) or not gate.get("enabled"):
        return True, "historical gate not enabled"

    hidden = current_hidden_context(symbol)
    if hidden is None:
        return False, "historical gate enabled but live macro/quality context is unavailable"

    allowed = gate.get("allowed_combinations") or []
    for item in allowed:
        if (
            item.get("market_regime") == hidden["market_regime"]
            and item.get("quality_bucket") == hidden["quality_bucket"]
        ):
            return True, f"historically supported {hidden['market_regime']} / {hidden['quality_bucket']}"

    return False, f"historically unsupported {hidden['market_regime']} / {hidden['quality_bucket']}"


def trade_parameters():
    gate = load_json(HISTORICAL_GATE_FILE, {})
    objective = gate.get("objective") if isinstance(gate, dict) else None
    if not isinstance(objective, dict):
        objective = {}
    try:
        tp = float(objective.get("target_pct", DEFAULT_TP_PCT))
    except (TypeError, ValueError):
        tp = DEFAULT_TP_PCT
    try:
        sl = float(objective.get("live_stop_pct", DEFAULT_SL_PCT))
    except (TypeError, ValueError):
        sl = DEFAULT_SL_PCT
    return tp, sl


def fetch_daily_closes(symbol):
    query = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": "1d",
        "limit": DAILY_LOOKBACK,
    })
    req = urllib.request.Request(
        f"{BINANCE_DATA_BASE}/api/v3/klines?{query}",
        headers={"User-Agent": "crypto-scan-daily-exhaustion/1.0", "Accept": "application/json"},
    )

    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected daily kline response for {symbol}: {data}")

            now_ms = int(time.time() * 1000)
            completed = [row for row in data if len(row) > 6 and int(row[6]) < now_ms]
            closes = [float(row[4]) for row in completed]
            if len(closes) < 55:
                raise RuntimeError(f"Only {len(closes)} completed daily candles available")
            return closes
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"Daily context unavailable for {symbol}: {last_error}")


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = alpha * x + (1.0 - alpha) * value
    return value


def rsi_series(closes, period=14):
    if len(closes) <= period:
        return []

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result = []

    def one_rsi(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    result.append(one_rsi(avg_gain, avg_loss))
    for i in range(period, len(changes)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        result.append(one_rsi(avg_gain, avg_loss))
    return result


def daily_context_from_closes(closes):
    if len(closes) < 55:
        raise ValueError("At least 55 completed daily closes are required")

    price = float(closes[-1])
    ema20_value = ema(closes, 20)
    ema50_value = ema(closes, 50)
    rsi_values = rsi_series(closes, 14)
    if len(rsi_values) < 14:
        raise ValueError("Insufficient RSI history")

    rsi14 = float(rsi_values[-1])
    recent_rsi = rsi_values[-14:]
    lo = min(recent_rsi)
    hi = max(recent_rsi)
    stoch_rsi = 50.0 if hi == lo else (rsi14 - lo) / (hi - lo) * 100.0
    seven_day_change = (price / float(closes[-8]) - 1.0) * 100.0

    return {
        "daily_close": round(price, 10),
        "daily_rsi14": round(rsi14, 2),
        "daily_stoch_rsi14": round(stoch_rsi, 2),
        "daily_change_7d_pct": round(seven_day_change, 2),
        "daily_ema20": round(float(ema20_value), 10),
        "daily_ema50": round(float(ema50_value), 10),
        "distance_from_daily_ema20_pct": round((price / float(ema20_value) - 1.0) * 100.0, 2),
        "distance_from_daily_ema50_pct": round((price / float(ema50_value) - 1.0) * 100.0, 2),
        "daily_candles_used": len(closes),
    }


def evaluate_late_entry_risk(bias, daily, hidden=None):
    """Conservative higher-timeframe exhaustion gate.

    This is deliberately a multi-factor blocker: no single oscillator can veto
    a setup by itself unless price is also materially stretched. The same logic
    is mirrored for LONG and SHORT continuation entries.
    """
    bias = str(bias or "").upper()
    hidden = hidden or {}
    points = 0
    reasons = []

    rsi = float(daily["daily_rsi14"])
    stoch = float(daily["daily_stoch_rsi14"])
    change7 = float(daily["daily_change_7d_pct"])
    ema20_dist = float(daily["distance_from_daily_ema20_pct"])
    ema50_dist = float(daily["distance_from_daily_ema50_pct"])

    rs_btc = hidden.get("relative_strength_vs_btc_pct")
    rs_market = hidden.get("relative_strength_vs_market_pct")
    try:
        rs_btc = float(rs_btc) if rs_btc is not None else None
    except (TypeError, ValueError):
        rs_btc = None
    try:
        rs_market = float(rs_market) if rs_market is not None else None
    except (TypeError, ValueError):
        rs_market = None

    if bias == "LONG":
        if rsi >= 72:
            points += 2
            reasons.append(f"daily RSI {rsi:.1f} >= 72")
        elif rsi >= 65:
            points += 1
            reasons.append(f"daily RSI elevated at {rsi:.1f}")

        if stoch >= 90:
            points += 1
            reasons.append(f"daily StochRSI {stoch:.1f} >= 90")

        if change7 >= 20:
            points += 2
            reasons.append(f"7d expansion +{change7:.1f}%")
        elif change7 >= 12:
            points += 1
            reasons.append(f"7d expansion +{change7:.1f}%")

        if ema20_dist >= 10:
            points += 2
            reasons.append(f"{ema20_dist:.1f}% above daily EMA20")
        elif ema20_dist >= 6:
            points += 1
            reasons.append(f"{ema20_dist:.1f}% above daily EMA20")

        if ema50_dist >= 18:
            points += 1
            reasons.append(f"{ema50_dist:.1f}% above daily EMA50")

        relative_values = [x for x in [rs_btc, rs_market] if x is not None]
        weak_relative = min(relative_values) if relative_values else None
        if weak_relative is not None and weak_relative <= -2:
            points += 1
            reasons.append(f"weak live relative strength {weak_relative:.1f}%")

        materially_extended = change7 >= 12 or ema20_dist >= 6
        hard_exhaustion = rsi >= 78 and ema20_dist >= 8
        blocked = (points >= 4 and materially_extended) or hard_exhaustion

    elif bias == "SHORT":
        if rsi <= 28:
            points += 2
            reasons.append(f"daily RSI {rsi:.1f} <= 28")
        elif rsi <= 35:
            points += 1
            reasons.append(f"daily RSI depressed at {rsi:.1f}")

        if stoch <= 10:
            points += 1
            reasons.append(f"daily StochRSI {stoch:.1f} <= 10")

        if change7 <= -20:
            points += 2
            reasons.append(f"7d decline {change7:.1f}%")
        elif change7 <= -12:
            points += 1
            reasons.append(f"7d decline {change7:.1f}%")

        if ema20_dist <= -10:
            points += 2
            reasons.append(f"{abs(ema20_dist):.1f}% below daily EMA20")
        elif ema20_dist <= -6:
            points += 1
            reasons.append(f"{abs(ema20_dist):.1f}% below daily EMA20")

        if ema50_dist <= -18:
            points += 1
            reasons.append(f"{abs(ema50_dist):.1f}% below daily EMA50")

        relative_values = [x for x in [rs_btc, rs_market] if x is not None]
        strong_relative = max(relative_values) if relative_values else None
        if strong_relative is not None and strong_relative >= 2:
            points += 1
            reasons.append(f"asset still relatively strong by {strong_relative:.1f}%")

        materially_extended = change7 <= -12 or ema20_dist <= -6
        hard_exhaustion = rsi <= 22 and ema20_dist <= -8
        blocked = (points >= 4 and materially_extended) or hard_exhaustion

    else:
        return {
            "blocked": True,
            "risk_points": 99,
            "reasons": ["invalid trade direction"],
        }

    return {
        "blocked": bool(blocked),
        "risk_points": int(points),
        "reasons": reasons,
    }


def late_entry_gate(symbol, bias):
    hidden = current_hidden_context(symbol)
    try:
        closes = fetch_daily_closes(symbol)
        daily = daily_context_from_closes(closes)
    except Exception as exc:
        return False, f"daily anti-chase context unavailable: {exc}", None, hidden, None

    assessment = evaluate_late_entry_risk(bias, daily, hidden)
    if assessment["blocked"]:
        reason = "daily late-entry/exhaustion risk: " + "; ".join(assessment["reasons"])
        return False, reason, daily, hidden, assessment

    return True, "daily anti-chase check passed", daily, hidden, assessment


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram secrets missing; swing alert not sent.")
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


def trade_levels(entry, bias, tp_pct, sl_pct):
    entry = float(entry)
    bias = str(bias or "").upper()
    if bias == "SHORT":
        tp = entry * (1.0 - tp_pct / 100.0)
        sl = entry * (1.0 + sl_pct / 100.0)
    else:
        tp = entry * (1.0 + tp_pct / 100.0)
        sl = entry * (1.0 - sl_pct / 100.0)
    return tp, sl


def fmt_price(value):
    value = float(value)
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    if value >= 0.01:
        return f"{value:.6f}"
    return f"{value:.8f}"


def format_alert(event, tp_pct, sl_pct):
    symbol = event.get("symbol")
    bias = str(event.get("bias") or "").upper()
    entry = event.get("trigger_price")
    tp, sl = trade_levels(entry, bias, tp_pct, sl_pct)

    return (
        "[V3.4 TRADE SETUP]\n\n"
        f"Pair: {symbol}\n"
        f"Direction: {bias}\n"
        f"Entry: {fmt_price(entry)}\n"
        f"TP (+{tp_pct:g}%): {fmt_price(tp)}\n"
        f"SL (-{sl_pct:g}%): {fmt_price(sl)}\n"
        f"Target window: {HOLD_WINDOW}\n\n"
        "Plan: target the 5% move from entry. If TP is not reached within 48h, "
        "review the position rather than assuming a later recovery.\n\n"
        "Manual trade decision required."
    )


def main():
    if not EVENTS_FILE.exists():
        print("No swing event file.")
        return

    state = load_json(STATE_FILE, {"sent_trigger_ids": [], "processed_trigger_ids": []})
    sent = set(state.get("sent_trigger_ids") or [])
    processed = set(state.get("processed_trigger_ids") or []) | sent
    tp_pct, sl_pct = trade_parameters()
    new_sent = []
    blocked = []

    for raw in EVENTS_FILE.read_text().splitlines():
        try:
            event = json.loads(raw)
        except Exception:
            continue

        if event.get("event") != "SWING_CONTINUATION_TRIGGER":
            continue

        symbol = event.get("symbol")
        ts = event.get("timestamp_ms")
        entry = event.get("trigger_price")
        bias = str(event.get("bias") or "").upper()
        if not symbol or ts is None or entry is None or bias not in {"LONG", "SHORT"}:
            continue

        trigger_id = event.get("trigger_id") or f"{symbol}-{int(ts)}"
        if trigger_id in processed:
            continue

        processed.add(trigger_id)

        historical_allowed, historical_reason = historical_gate_allows(symbol)
        if not historical_allowed:
            record = {
                "timestamp_ms": int(time.time() * 1000),
                "trigger_id": trigger_id,
                "symbol": symbol,
                "bias": bias,
                "decision": "BLOCKED",
                "stage": "historical_gate",
                "reason": historical_reason,
            }
            blocked.append(record)
            append_filter_event(record)
            print(f"Blocked Telegram setup {trigger_id}: {historical_reason}")
            continue

        daily_allowed, daily_reason, daily, hidden, assessment = late_entry_gate(symbol, bias)
        decision_record = {
            "timestamp_ms": int(time.time() * 1000),
            "trigger_id": trigger_id,
            "symbol": symbol,
            "bias": bias,
            "trigger_price": float(entry),
            "decision": "ALLOWED" if daily_allowed else "BLOCKED",
            "stage": "daily_anti_chase",
            "reason": daily_reason,
            "historical_gate_reason": historical_reason,
            "daily_context": daily,
            "hidden_context": hidden,
            "late_entry_assessment": assessment,
        }
        append_filter_event(decision_record)

        if not daily_allowed:
            blocked.append(decision_record)
            print(f"Blocked Telegram setup {trigger_id}: {daily_reason}")
            continue

        message = format_alert(event, tp_pct, sl_pct)
        if send_telegram(message):
            sent.add(trigger_id)
            new_sent.append(trigger_id)
            print(f"Sent V3.4 trade setup: {trigger_id} ({historical_reason}; {daily_reason})")

    state["sent_trigger_ids"] = sorted(sent)
    state["processed_trigger_ids"] = sorted(processed)
    state["tp_pct"] = tp_pct
    state["sl_pct"] = sl_pct
    state["target_window"] = HOLD_WINDOW
    state["historical_gate_enabled"] = bool(load_json(HISTORICAL_GATE_FILE, {}).get("enabled"))
    state["daily_anti_chase_enabled"] = True
    state["daily_anti_chase_policy"] = {
        "daily_candles": DAILY_LOOKBACK,
        "fail_closed_when_daily_context_unavailable": True,
        "block_rule": "risk_points>=4 with material daily extension, or hard RSI+EMA20 exhaustion",
    }
    state["last_blocked"] = blocked[-20:]
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    if not new_sent:
        print("No new eligible V3.4 trade setup for Telegram.")


if __name__ == "__main__":
    main()
