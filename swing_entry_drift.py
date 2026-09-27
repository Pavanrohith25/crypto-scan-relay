import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

EVENTS_FILE = Path("swing_events.jsonl")
STATE_FILE = Path("swing_telegram_state.json")
DRIFT_EVENTS_FILE = Path("swing_entry_drift_events.jsonl")
FILTER_EVENTS_FILE = Path("swing_telegram_filter_events.jsonl")
HISTORICAL_GATE_FILE = Path("historical_gate_config.json")

BINANCE_DATA_BASE = "https://data-api.binance.vision"
DEFAULT_TP_PCT = 5.0
DEFAULT_SL_PCT = 6.0


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def append_jsonl(path, payload):
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


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


def fetch_live_price(symbol):
    query = urllib.parse.urlencode({"symbol": symbol})
    req = urllib.request.Request(
        f"{BINANCE_DATA_BASE}/api/v3/ticker/price?{query}",
        headers={
            "User-Agent": "crypto-scan-entry-drift/1.0",
            "Accept": "application/json",
        },
    )

    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            price = float(payload["price"])
            if price <= 0:
                raise RuntimeError(f"non-positive live price for {symbol}: {price}")
            return price
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))

    raise RuntimeError(f"live price unavailable for {symbol}: {last_error}")


def directional_drift_pct(trigger_price, live_price, bias):
    trigger_price = float(trigger_price)
    live_price = float(live_price)
    bias = str(bias or "").upper()
    if trigger_price <= 0 or live_price <= 0:
        raise ValueError("prices must be positive")
    if bias == "LONG":
        return (live_price / trigger_price - 1.0) * 100.0
    if bias == "SHORT":
        return (trigger_price / live_price - 1.0) * 100.0
    raise ValueError(f"invalid bias: {bias}")


def drift_bucket(drift_pct):
    value = float(drift_pct)
    if value < 0:
        return "against_trigger"
    if value < 0.5:
        return "0_to_0.5"
    if value < 1.0:
        return "0.5_to_1"
    if value < 2.0:
        return "1_to_2"
    if value < 3.0:
        return "2_to_3"
    if value < 5.0:
        return "3_to_5"
    return "5_plus"


def evaluate_entry_drift(trigger_price, live_price, bias, tp_pct, sl_pct):
    drift = directional_drift_pct(trigger_price, live_price, bias)
    if drift >= float(tp_pct):
        return {
            "blocked": True,
            "reason": "target already reached/passed before Telegram alert",
            "directional_drift_pct": round(drift, 4),
            "drift_bucket": drift_bucket(drift),
        }
    if drift <= -float(sl_pct):
        return {
            "blocked": True,
            "reason": "stop already reached/passed before Telegram alert",
            "directional_drift_pct": round(drift, 4),
            "drift_bucket": drift_bucket(drift),
        }
    return {
        "blocked": False,
        "reason": "entry drift recorded for research; no empirical late-entry cutoff promoted yet",
        "directional_drift_pct": round(drift, 4),
        "drift_bucket": drift_bucket(drift),
    }


def pending_triggers(processed):
    if not EVENTS_FILE.exists():
        return []

    found = []
    seen = set()
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
        if trigger_id in processed or trigger_id in seen:
            continue
        seen.add(trigger_id)
        found.append((trigger_id, event))
    return found


def main():
    state = load_json(STATE_FILE, {"sent_trigger_ids": [], "processed_trigger_ids": []})
    sent = set(state.get("sent_trigger_ids") or [])
    processed = set(state.get("processed_trigger_ids") or []) | sent
    tp_pct, sl_pct = trade_parameters()
    now_ms = int(time.time() * 1000)
    recent = []

    for trigger_id, event in pending_triggers(processed):
        symbol = event["symbol"]
        bias = str(event["bias"]).upper()
        trigger_price = float(event["trigger_price"])
        trigger_ms = int(event["timestamp_ms"])

        base = {
            "timestamp_ms": int(time.time() * 1000),
            "trigger_id": trigger_id,
            "symbol": symbol,
            "bias": bias,
            "trigger_price": trigger_price,
            "trigger_at_ms": trigger_ms,
            "alert_check_age_seconds": round(max(0.0, (now_ms - trigger_ms) / 1000.0), 2),
            "target_pct": tp_pct,
            "stop_pct": sl_pct,
        }

        try:
            live_price = fetch_live_price(symbol)
            assessment = evaluate_entry_drift(trigger_price, live_price, bias, tp_pct, sl_pct)
            record = {
                **base,
                "live_price": live_price,
                **assessment,
            }
        except Exception as exc:
            record = {
                **base,
                "live_price": None,
                "blocked": False,
                "reason": f"live entry drift unavailable: {exc}",
                "directional_drift_pct": None,
                "drift_bucket": "unavailable",
            }

        append_jsonl(DRIFT_EVENTS_FILE, record)
        recent.append(record)

        if record["blocked"]:
            processed.add(trigger_id)
            filter_record = {
                **record,
                "decision": "BLOCKED",
                "stage": "entry_drift_objective_stale_guard",
            }
            append_jsonl(FILTER_EVENTS_FILE, filter_record)
            print(f"Blocked stale Telegram setup {trigger_id}: {record['reason']}")
        else:
            drift = record.get("directional_drift_pct")
            if drift is None:
                print(f"Entry drift unavailable for {trigger_id}; leaving decision to existing Telegram gates")
            else:
                print(f"Recorded entry drift {trigger_id}: {drift:+.3f}% ({record['drift_bucket']})")

    state["processed_trigger_ids"] = sorted(processed)
    state["entry_drift_monitor_enabled"] = True
    state["entry_drift_policy"] = {
        "mode": "research_with_objective_stale_guard",
        "research_buckets_pct": [0.0, 0.5, 1.0, 2.0, 3.0, 5.0],
        "block_if_target_already_reached": True,
        "block_if_stop_already_reached": True,
        "empirical_late_entry_cutoff_enabled": False,
    }
    state["last_entry_drift_checks"] = recent[-20:]
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    if not recent:
        print("No fresh V3.4 continuation trigger needs entry-drift measurement.")


if __name__ == "__main__":
    main()
