import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# v3.6 FIVE-PERCENT OUTCOME TRACKER - SHADOW ONLY
# Objective: +5% from trigger, with 24h primary and 48h secondary horizon.
# 4h / 12h are diagnostics for speed. Stop grid is research-only and is used
# to learn how much adverse movement successful setups typically need.

SWING_EVENTS_FILE = Path("swing_events.jsonl")
OUTCOMES_FILE = Path("swing_outcomes.json")
OUTCOME_EVENTS_FILE = Path("swing_outcome_events.jsonl")

BINANCE_DATA_BASE = "https://data-api.binance.vision"
INTERVAL = "5m"
BAR_MS = 5 * 60 * 1000
TARGET_PCT = 5.0
STOP_LEVELS_PCT = (3.0, 4.0, 5.0, 6.0, 8.0)
HORIZONS = (
    (4, "outcome_4h", "SWING_OUTCOME_4H"),
    (12, "outcome_12h", "SWING_OUTCOME_12H"),
    (24, "outcome_24h", "SWING_OUTCOME_24H"),
    (48, "outcome_48h", "SWING_OUTCOME_48H"),
)


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def append_event(event):
    with OUTCOME_EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def load_trigger_events():
    triggers = {}
    if not SWING_EVENTS_FILE.exists():
        return triggers

    for raw_line in SWING_EVENTS_FILE.read_text().splitlines():
        try:
            event = json.loads(raw_line)
        except Exception:
            continue
        if event.get("event") != "SWING_CONTINUATION_TRIGGER":
            continue

        symbol = event.get("symbol")
        bias = str(event.get("bias") or "").upper()
        trigger_at_ms = event.get("timestamp_ms")
        trigger_price = event.get("trigger_price")
        if not symbol or bias not in {"LONG", "SHORT"} or trigger_at_ms is None or trigger_price is None:
            continue

        trigger_id = event.get("trigger_id") or f"{symbol}-{int(trigger_at_ms)}"
        triggers[trigger_id] = {
            "trigger_id": trigger_id,
            "symbol": symbol,
            "bias": bias,
            "trigger_at_ms": int(trigger_at_ms),
            "trigger_price": float(trigger_price),
            "swing_score": event.get("swing_score"),
            "snapshot": event.get("snapshot") or {},
        }
    return triggers


def ceil_to_next_bar(timestamp_ms):
    ts = int(timestamp_ms)
    return ((ts + BAR_MS - 1) // BAR_MS) * BAR_MS


def fetch_klines(symbol, start_ms, end_ms):
    params = urlencode({
        "symbol": symbol,
        "interval": INTERVAL,
        "startTime": int(start_ms),
        "endTime": int(end_ms),
        "limit": 1000,
    })
    request = Request(
        f"{BINANCE_DATA_BASE}/api/v3/klines?{params}",
        headers={"User-Agent": "crypto-scan-five-percent-outcomes/1.0", "Accept": "application/json"},
    )
    with urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Binance response for {symbol}: {data}")
    return data


def rounded(value, digits=4):
    if value is None:
        return None
    return round(float(value), digits)


def favorable_and_adverse(entry, bias, high, low):
    if bias == "SHORT":
        favorable = (1.0 - low / entry) * 100.0
        adverse = (1.0 - high / entry) * 100.0
    else:
        favorable = (high / entry - 1.0) * 100.0
        adverse = (low / entry - 1.0) * 100.0
    return favorable, adverse


def evaluate_stop_path(trigger, klines, stop_pct):
    entry = float(trigger["trigger_price"])
    bias = trigger["bias"]
    trigger_ms = int(trigger["trigger_at_ms"])
    result = "OPEN"
    resolution_bar_ms = None
    first_target_ms = None
    first_stop_ms = None

    for kline in klines:
        bar_ms = int(kline[0])
        high = float(kline[2])
        low = float(kline[3])
        favorable, adverse = favorable_and_adverse(entry, bias, high, low)
        hit_target = favorable >= TARGET_PCT
        hit_stop = adverse <= -stop_pct

        if first_target_ms is None and hit_target:
            first_target_ms = bar_ms
        if first_stop_ms is None and hit_stop:
            first_stop_ms = bar_ms

        if result == "OPEN":
            if hit_target and hit_stop:
                result = "AMBIGUOUS"
                resolution_bar_ms = bar_ms
            elif hit_target:
                result = "TARGET_FIRST"
                resolution_bar_ms = bar_ms
            elif hit_stop:
                result = "STOP_FIRST"
                resolution_bar_ms = bar_ms

    return {
        "target_pct": TARGET_PCT,
        "stop_pct": stop_pct,
        "result": result,
        "target_first": result == "TARGET_FIRST",
        "stop_first": result == "STOP_FIRST",
        "ambiguous": result == "AMBIGUOUS",
        "target_reached": first_target_ms is not None,
        "stop_reached": first_stop_ms is not None,
        "first_target_bar_ms": first_target_ms,
        "first_stop_bar_ms": first_stop_ms,
        "resolution_bar_ms": resolution_bar_ms,
        "time_to_target_minutes": None if first_target_ms is None else rounded(max(0, first_target_ms - trigger_ms) / 60000.0, 2),
        "time_to_stop_minutes": None if first_stop_ms is None else rounded(max(0, first_stop_ms - trigger_ms) / 60000.0, 2),
    }


def calculate_outcome(trigger, klines, horizon_hours):
    if not klines:
        raise RuntimeError("No kline data returned")

    entry = float(trigger["trigger_price"])
    bias = trigger["bias"]
    if entry <= 0:
        raise RuntimeError("Invalid trigger price")

    mfe = 0.0
    mae = 0.0
    first_target_ms = None
    mae_before_target = 0.0

    for kline in klines:
        bar_ms = int(kline[0])
        high = float(kline[2])
        low = float(kline[3])
        favorable, adverse = favorable_and_adverse(entry, bias, high, low)
        mfe = max(mfe, favorable)
        mae = min(mae, adverse)
        if first_target_ms is None:
            mae_before_target = min(mae_before_target, adverse)
            if favorable >= TARGET_PCT:
                first_target_ms = bar_ms

    last_close = float(klines[-1][4])
    if bias == "SHORT":
        close_return = (1.0 - last_close / entry) * 100.0
    else:
        close_return = (last_close / entry - 1.0) * 100.0

    stop_grid = {
        f"stop_{str(stop).replace('.', '_')}pct": evaluate_stop_path(trigger, klines, stop)
        for stop in STOP_LEVELS_PCT
    }

    return {
        "objective": "+5pct",
        "horizon_hours": horizon_hours,
        "sampling_interval": INTERVAL,
        "bars_used": len(klines),
        "target_5_reached": first_target_ms is not None,
        "first_5pct_bar_ms": first_target_ms,
        "time_to_5pct_minutes": None if first_target_ms is None else rounded(max(0, first_target_ms - int(trigger["trigger_at_ms"])) / 60000.0, 2),
        "mae_before_5pct_pct": None if first_target_ms is None else rounded(mae_before_target),
        "mfe_pct": rounded(mfe),
        "mae_pct": rounded(mae),
        "close_return_pct": rounded(close_return),
        "stop_grid": stop_grid,
    }


def evaluate_window(trigger, horizon_hours):
    trigger_ms = trigger["trigger_at_ms"]
    start_ms = ceil_to_next_bar(trigger_ms)
    end_ms = trigger_ms + horizon_hours * 60 * 60 * 1000
    klines = fetch_klines(trigger["symbol"], start_ms, end_ms)
    outcome = calculate_outcome(trigger, klines, horizon_hours)
    outcome["window_start_ms"] = start_ms
    outcome["window_end_ms"] = end_ms
    outcome["sampling_note"] = (
        "Uses 5m candles beginning with the first full bar at/after the trigger. "
        "If TP and SL are both touched inside the same 5m candle before either resolves, the stop-path result is AMBIGUOUS."
    )
    return outcome


def needs_refresh(existing):
    return not isinstance(existing, dict) or existing.get("objective") != "+5pct" or "stop_grid" not in existing


def main():
    now_ms = int(time.time() * 1000)
    triggers = load_trigger_events()
    outcomes = load_json(OUTCOMES_FILE, {"version": "3.6-five-percent", "triggers": {}})
    outcomes["version"] = "3.6-five-percent"
    outcomes["objective"] = {
        "target_pct": TARGET_PCT,
        "primary_horizon_hours": 24,
        "secondary_horizon_hours": 48,
        "diagnostic_horizons_hours": [4, 12],
        "stop_grid_pct": list(STOP_LEVELS_PCT),
    }
    outcomes.setdefault("triggers", {})

    for trigger_id, trigger in triggers.items():
        record = outcomes["triggers"].setdefault(trigger_id, {**trigger})
        record.update({
            "symbol": trigger["symbol"],
            "bias": trigger["bias"],
            "trigger_at_ms": trigger["trigger_at_ms"],
            "trigger_price": trigger["trigger_price"],
            "swing_score": trigger.get("swing_score"),
            "snapshot": trigger.get("snapshot") or {},
        })

        age_ms = now_ms - int(trigger["trigger_at_ms"])
        for horizon, key, event_name in HORIZONS:
            record.setdefault(key, None)
            if age_ms < horizon * 60 * 60 * 1000 or not needs_refresh(record.get(key)):
                continue
            try:
                result = evaluate_window(trigger, horizon)
            except Exception as exc:
                print(f"{trigger_id}: {horizon}h evaluation failed: {exc}")
                continue
            record[key] = result
            record[f"evaluated_{horizon}h_at_ms"] = now_ms
            append_event({
                "event": event_name,
                "timestamp_ms": now_ms,
                "trigger_id": trigger_id,
                "symbol": trigger["symbol"],
                "bias": trigger["bias"],
                "trigger_at_ms": trigger["trigger_at_ms"],
                "trigger_price": trigger["trigger_price"],
                "swing_score": trigger.get("swing_score"),
                "outcome": result,
            })

    outcomes["last_processed_at_ms"] = now_ms
    OUTCOMES_FILE.write_text(json.dumps(outcomes, indent=2, sort_keys=True) + "\n")
    if not OUTCOME_EVENTS_FILE.exists():
        OUTCOME_EVENTS_FILE.write_text("")

    pending = {h: 0 for h, _, _ in HORIZONS}
    for record in outcomes["triggers"].values():
        age_ms = now_ms - int(record["trigger_at_ms"])
        for horizon, key, _ in HORIZONS:
            if age_ms >= horizon * 60 * 60 * 1000 and needs_refresh(record.get(key)):
                pending[horizon] += 1

    print(
        f"Tracked {len(outcomes['triggers'])} trigger(s). "
        f"Pending: 4h={pending[4]}, 12h={pending[12]}, 24h={pending[24]}, 48h={pending[48]}."
    )


if __name__ == "__main__":
    main()
