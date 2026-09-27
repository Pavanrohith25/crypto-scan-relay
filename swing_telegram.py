import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

EVENTS_FILE = Path("swing_events.jsonl")
STATE_FILE = Path("swing_telegram_state.json")


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


def format_alert(event):
    snap = event.get("snapshot") or {}
    reasons = snap.get("reasons") or []
    warnings = snap.get("warnings") or []
    reason_text = "\n".join(f"- {x}" for x in reasons[:5]) or "- None listed"
    warning_text = "\n".join(f"- {x}" for x in warnings[:5]) or "- None"
    return (
        "[V3.4 SWING CANDIDATE]\n\n"
        f"Pair: {event.get('symbol')}\n"
        f"Bias: {event.get('bias')}\n"
        f"Trigger price: {event.get('trigger_price')}\n"
        f"Swing score: {event.get('swing_score')}/100\n"
        f"Setup: CONTINUATION_TRIGGER\n"
        f"Research horizon: {event.get('target_horizon', '24-48h')}\n\n"
        f"Why it triggered:\n{reason_text}\n\n"
        f"Warnings:\n{warning_text}\n\n"
        "Research candidate only - manual review required. "
        "This alert is from the directional swing engine, not the funding/OI squeeze alert path."
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
        if not symbol or ts is None:
            continue
        trigger_id = event.get("trigger_id") or f"{symbol}-{int(ts)}"
        if trigger_id in sent:
            continue
        alerts.append((trigger_id, format_alert(event)))

    for trigger_id, message in alerts:
        if send_telegram(message):
            sent.add(trigger_id)
            new_ids.append(trigger_id)
            print(f"Sent swing Telegram alert: {trigger_id}")

    state["sent_trigger_ids"] = sorted(sent)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    if not new_ids:
        print("No new V3.4 continuation trigger for Telegram.")


if __name__ == "__main__":
    main()
