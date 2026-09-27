import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from swing_ranker import classify_market_regime, score_candidate

EVENTS_FILE = Path("swing_events.jsonl")
STATE_FILE = Path("swing_telegram_state.json")
SWING_SCAN_FILE = Path("new_swing.json")
HISTORICAL_GATE_FILE = Path("historical_gate_config.json")

DEFAULT_TP_PCT = 5.0
DEFAULT_SL_PCT = 6.0
HOLD_WINDOW = "24-48h"


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


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

        allowed, reason = historical_gate_allows(symbol)
        processed.add(trigger_id)
        if not allowed:
            blocked.append({"trigger_id": trigger_id, "symbol": symbol, "reason": reason})
            print(f"Blocked Telegram setup {trigger_id}: {reason}")
            continue

        message = format_alert(event, tp_pct, sl_pct)
        if send_telegram(message):
            sent.add(trigger_id)
            new_sent.append(trigger_id)
            print(f"Sent V3.4 trade setup: {trigger_id} ({reason})")

    state["sent_trigger_ids"] = sorted(sent)
    state["processed_trigger_ids"] = sorted(processed)
    state["tp_pct"] = tp_pct
    state["sl_pct"] = sl_pct
    state["target_window"] = HOLD_WINDOW
    state["historical_gate_enabled"] = bool(load_json(HISTORICAL_GATE_FILE, {}).get("enabled"))
    state["last_blocked"] = blocked[-20:]
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    if not new_sent:
        print("No new eligible V3.4 trade setup for Telegram.")


if __name__ == "__main__":
    main()
