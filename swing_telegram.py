import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

EVENTS_FILE = Path("swing_events.jsonl")
STATE_FILE = Path("swing_telegram_state.json")

TP_PCT = 5.0
SL_PCT = 6.0
HOLD_WINDOW = "24-48h"


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


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


def trade_levels(entry, bias):
    entry = float(entry)
    bias = str(bias or "").upper()
    if bias == "SHORT":
        tp = entry * (1.0 - TP_PCT / 100.0)
        sl = entry * (1.0 + SL_PCT / 100.0)
    else:
        tp = entry * (1.0 + TP_PCT / 100.0)
        sl = entry * (1.0 - SL_PCT / 100.0)
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


def format_alert(event):
    symbol = event.get("symbol")
    bias = str(event.get("bias") or "").upper()
    entry = event.get("trigger_price")
    tp, sl = trade_levels(entry, bias)

    return (
        "[V3.4 TRADE SETUP]\n\n"
        f"Pair: {symbol}\n"
        f"Direction: {bias}\n"
        f"Entry: {fmt_price(entry)}\n"
        f"TP (+{TP_PCT:.0f}%): {fmt_price(tp)}\n"
        f"SL (-{SL_PCT:.0f}%): {fmt_price(sl)}\n"
        f"Target window: {HOLD_WINDOW}\n\n"
        "Plan: target a 5% move from entry. The hard stop is deliberately wider than 5% "
        "so the setup has room for temporary drawdown. If TP is not reached within 48h, "
        "review the position rather than assuming it will recover later.\n\n"
        "Manual trade decision required."
    )


def main():
    if not EVENTS_FILE.exists():
        print("No swing event file.")
        return

    state = load_json(STATE_FILE, {"sent_trigger_ids": []})
    sent = set(state.get("sent_trigger_ids") or [])
    new_ids = []
    alerts = []

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
        if trigger_id in sent:
            continue

        alerts.append((trigger_id, format_alert(event)))

    for trigger_id, message in alerts:
        if send_telegram(message):
            sent.add(trigger_id)
            new_ids.append(trigger_id)
            print(f"Sent V3.4 trade setup: {trigger_id}")

    state["sent_trigger_ids"] = sorted(sent)
    state["tp_pct"] = TP_PCT
    state["sl_pct"] = SL_PCT
    state["target_window"] = HOLD_WINDOW
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    if not new_ids:
        print("No new V3.4 trade setup for Telegram.")


if __name__ == "__main__":
    main()
