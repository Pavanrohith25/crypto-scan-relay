import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# v3.5 PATH-DEPENDENT OUTCOME TRACKER - SHADOW ONLY
# Evaluates continuation triggers after 24h / 48h / 72h.
# Primary labels:
#   +3% before -1%
#   +5% before -1.5%
#   +10% before -2%
# If target and stop are both touched inside the same 5m candle before either
# was previously resolved, the path result is AMBIGUOUS.

SWING_EVENTS_FILE = Path("swing_events.jsonl")
OUTCOMES_FILE = Path("swing_outcomes.json")
OUTCOME_EVENTS_FILE = Path("swing_outcome_events.jsonl")

BINANCE_DATA_BASE = "https://data-api.binance.vision"
INTERVAL = "5m"
BAR_MS = 5 * 60 * 1000

PATH_RULES = {
    "plus3_before_minus1": {"target_pct": 3.0, "stop_pct": 1.0},
    "plus5_before_minus1_5": {"target_pct": 5.0, "stop_pct": 1.5},
    "plus10_before_minus2": {"target_pct": 10.0, "stop_pct": 2.0},
}


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
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("event") != "SWING_CONTINUATION_TRIGGER":
            continue

        symbol = event.get("symbol")
        bias = str(event.get("bias") or "").upper()
        trigger_at_ms = event.get("timestamp_ms")
        trigger_price = event.get("trigger_price")
        if (
            not symbol
            or bias not in {"LONG", "SHORT"}
            or trigger_at_ms is None
            or trigger_price is None
        ):
            continue

        trigger_id = event.get("trigger_id") or f"{symbol}-{int(trigger_at_ms)}"
        triggers[trigger_id] = {
            "trigger_id": trigger_id,
            "symbol": symbol,
            "bias": bias,
            "trigger_at_ms": int(trigger_at_ms),
            "trigger_price": float(trigger_price),
            "swing_score": event.get("swing_score"),
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
    url = f"{BINANCE_DATA_BASE}/api/v3/klines?{params}"
    request = Request(url, headers={
        "User-Agent": "crypto-scan-relay-path-outcomes/2.0",
        "Accept": "application/json",
    })
    with urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Binance response for {symbol}: {data}")
    return data


def rounded(value, digits=4):
    if value is None:
        return None
    return round(float(value), digits)


def evaluate_path_rule(trigger, klines, target_pct, stop_pct):
    entry = float(trigger["trigger_price"])
    bias = trigger["bias"]
    trigger_ms = int(trigger["trigger_at_ms"])

    resolution = "OPEN"
    resolution_bar_ms = None
    first_target_bar_ms = None
    first_stop_bar_ms = None
    running_worst_adverse_pct = 0.0
    mae_before_target_pct = None

    for kline in klines:
        bar_open_ms = int(kline[0])
        high = float(kline[2])
        low = float(kline[3])

        if bias == "LONG":
            adverse_this_bar = (low / entry - 1.0) * 100.0
            hit_target = high >= entry * (1.0 + target_pct / 100.0)
            hit_stop = low <= entry * (1.0 - stop_pct / 100.0)
        else:
            adverse_this_bar = (1.0 - high / entry) * 100.0
            hit_target = low <= entry * (1.0 - target_pct / 100.0)
            hit_stop = high >= entry * (1.0 + stop_pct / 100.0)

        running_worst_adverse_pct = min(running_worst_adverse_pct, adverse_this_bar)

        if first_target_bar_ms is None and hit_target:
            first_target_bar_ms = bar_open_ms
            mae_before_target_pct = running_worst_adverse_pct

        if first_stop_bar_ms is None and hit_stop:
            first_stop_bar_ms = bar_open_ms

        if resolution == "OPEN":
            if hit_target and hit_stop:
                resolution = "AMBIGUOUS"
                resolution_bar_ms = bar_open_ms
            elif hit_target:
                resolution = "TARGET_FIRST"
                resolution_bar_ms = bar_open_ms
            elif hit_stop:
                resolution = "STOP_FIRST"
                resolution_bar_ms = bar_open_ms

    time_to_target_minutes = None
    if first_target_bar_ms is not None:
        time_to_target_minutes = max(0.0, (first_target_bar_ms - trigger_ms) / 60000.0)

    time_to_stop_minutes = None
    if first_stop_bar_ms is not None:
        time_to_stop_minutes = max(0.0, (first_stop_bar_ms - trigger_ms) / 60000.0)

    return {
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "result": resolution,
        "target_first": resolution == "TARGET_FIRST",
        "stop_first": resolution == "STOP_FIRST",
        "ambiguous": resolution == "AMBIGUOUS",
        "target_reached": first_target_bar_ms is not None,
        "stop_reached": first_stop_bar_ms is not None,
        "first_target_bar_ms": first_target_bar_ms,
        "first_stop_bar_ms": first_stop_bar_ms,
        "resolution_bar_ms": resolution_bar_ms,
        "time_to_target_minutes": rounded(time_to_target_minutes, 2),
        "time_to_stop_minutes": rounded(time_to_stop_minutes, 2),
        "mae_before_target_pct": rounded(mae_before_target_pct),
    }


def calculate_outcome(trigger, klines, horizon_hours):
    if not klines:
        raise RuntimeError("No kline data returned")

    entry = float(trigger["trigger_price"])
    bias = trigger["bias"]
    if entry <= 0:
        raise RuntimeError("Invalid trigger price")

    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    last_close = float(klines[-1][4])
    highest = max(highs)
    lowest = min(lows)

    if bias == "LONG":
        mfe_pct = (highest / entry - 1.0) * 100.0
        mae_pct = (lowest / entry - 1.0) * 100.0
        close_return_pct = (last_close / entry - 1.0) * 100.0
        favorable_price = highest
        adverse_price = lowest
    else:
        mfe_pct = (1.0 - lowest / entry) * 100.0
        mae_pct = (1.0 - highest / entry) * 100.0
        close_return_pct = (1.0 - last_close / entry) * 100.0
        favorable_price = lowest
        adverse_price = highest

    first_target_bars = {3: None, 5: None, 10: None}
    for kline in klines:
        bar_open_ms = int(kline[0])
        high = float(kline[2])
        low = float(kline[3])
        for target_pct in first_target_bars:
            if first_target_bars[target_pct] is not None:
                continue
            if bias == "LONG":
                hit = high >= entry * (1.0 + target_pct / 100.0)
            else:
                hit = low <= entry * (1.0 - target_pct / 100.0)
            if hit:
                first_target_bars[target_pct] = bar_open_ms

    path_labels = {
        name: evaluate_path_rule(
            trigger,
            klines,
            rule["target_pct"],
            rule["stop_pct"],
        )
        for name, rule in PATH_RULES.items()
    }

    return {
        "horizon_hours": horizon_hours,
        "sampling_interval": INTERVAL,
        "bars_used": len(klines),
        "mfe_pct": rounded(mfe_pct),
        "mae_pct": rounded(mae_pct),
        "close_return_pct": rounded(close_return_pct),
        "max_favorable_price": rounded(favorable_price, 12),
        "max_adverse_price": rounded(adverse_price, 12),
        "target_3_reached": first_target_bars[3] is not None,
        "target_5_reached": first_target_bars[5] is not None,
        "target_10_reached": first_target_bars[10] is not None,
        "first_3pct_bar_ms": first_target_bars[3],
        "first_5pct_bar_ms": first_target_bars[5],
        "first_10pct_bar_ms": first_target_bars[10],
        "path_labels": path_labels,
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
        "Up to the first 5 minutes after the trigger may be omitted. If target "
        "and stop occur in the same 5m candle before either resolves, result is AMBIGUOUS."
    )
    return outcome


def needs_refresh(existing):
    if not isinstance(existing, dict):
        return True
    return "path_labels" not in existing or "target_10_reached" not in existing


def main():
    now_ms = int(time.time() * 1000)
    triggers = load_trigger_events()
    outcomes = load_json(OUTCOMES_FILE, {"version": "3.5-path-labels", "triggers": {}})
    outcomes["version"] = "3.5-path-labels"
    outcomes.setdefault("triggers", {})

    horizons = (
        (24, "outcome_24h", "SWING_OUTCOME_24H"),
        (48, "outcome_48h", "SWING_OUTCOME_48H"),
        (72, "outcome_72h", "SWING_OUTCOME_72H"),
    )

    for trigger_id, trigger in triggers.items():
        record = outcomes["triggers"].setdefault(trigger_id, {**trigger})
        record.setdefault("outcome_24h", None)
        record.setdefault("outcome_48h", None)
        record.setdefault("outcome_72h", None)
        record.update({
            "symbol": trigger["symbol"],
            "bias": trigger["bias"],
            "trigger_at_ms": trigger["trigger_at_ms"],
            "trigger_price": trigger["trigger_price"],
            "swing_score": trigger.get("swing_score"),
        })

        age_ms = now_ms - int(trigger["trigger_at_ms"])
        for horizon, key, event_name in horizons:
            if age_ms < horizon * 60 * 60 * 1000:
                continue
            if not needs_refresh(record.get(key)):
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

    pending = {24: 0, 48: 0, 72: 0}
    for record in outcomes["triggers"].values():
        age_ms = now_ms - int(record["trigger_at_ms"])
        for horizon, key, _ in horizons:
            if age_ms >= horizon * 60 * 60 * 1000 and needs_refresh(record.get(key)):
                pending[horizon] += 1

    print(
        f"Tracked {len(outcomes['triggers'])} trigger(s). "
        f"Pending matured evaluations: 24h={pending[24]}, "
        f"48h={pending[48]}, 72h={pending[72]}."
    )


if __name__ == "__main__":
    main()
