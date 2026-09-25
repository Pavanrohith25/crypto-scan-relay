import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# v3.4 OUTCOME TRACKER - SHADOW ONLY
# Reads SWING_CONTINUATION_TRIGGER events and evaluates them
# after 24h / 48h using Binance 5-minute historical candles.
# No Telegram alerts and no effect on live trading logic.

SWING_EVENTS_FILE = Path("swing_events.jsonl")
OUTCOMES_FILE = Path("swing_outcomes.json")
OUTCOME_EVENTS_FILE = Path("swing_outcome_events.jsonl")

BINANCE_DATA_BASE = "https://data-api.binance.vision"
INTERVAL = "5m"
BAR_MS = 5 * 60 * 1000


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

        trigger_id = (
            event.get("trigger_id")
            or f"{symbol}-{int(trigger_at_ms)}"
        )

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
    params = urlencode(
        {
            "symbol": symbol,
            "interval": INTERVAL,
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "limit": 1000,
        }
    )

    url = f"{BINANCE_DATA_BASE}/api/v3/klines?{params}"

    request = Request(
        url,
        headers={
            "User-Agent": "crypto-scan-relay-v34-outcomes/1.0",
            "Accept": "application/json",
        },
    )

    with urlopen(request, timeout=20) as response:
        data = json.loads(
            response.read().decode("utf-8")
        )

    if not isinstance(data, list):
        raise RuntimeError(
            f"Unexpected Binance response for {symbol}: {data}"
        )

    return data


def rounded(value, digits=4):
    return round(float(value), digits)


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
        close_return_pct = (
            last_close / entry - 1.0
        ) * 100.0
        favorable_price = highest
        adverse_price = lowest

    else:
        mfe_pct = (
            1.0 - lowest / entry
        ) * 100.0
        mae_pct = (
            1.0 - highest / entry
        ) * 100.0
        close_return_pct = (
            1.0 - last_close / entry
        ) * 100.0
        favorable_price = lowest
        adverse_price = highest

    first_3pct_bar_ms = None
    first_5pct_bar_ms = None

    for kline in klines:
        bar_open_ms = int(kline[0])
        high = float(kline[2])
        low = float(kline[3])

        if bias == "LONG":
            hit_3 = high >= entry * 1.03
            hit_5 = high >= entry * 1.05
        else:
            hit_3 = low <= entry * 0.97
            hit_5 = low <= entry * 0.95

        if first_3pct_bar_ms is None and hit_3:
            first_3pct_bar_ms = bar_open_ms

        if first_5pct_bar_ms is None and hit_5:
            first_5pct_bar_ms = bar_open_ms

    return {
        "horizon_hours": horizon_hours,
        "sampling_interval": INTERVAL,
        "bars_used": len(klines),
        "mfe_pct": rounded(mfe_pct),
        "mae_pct": rounded(mae_pct),
        "close_return_pct": rounded(
            close_return_pct
        ),
        "max_favorable_price": rounded(
            favorable_price, 12
        ),
        "max_adverse_price": rounded(
            adverse_price, 12
        ),
        "target_3_reached": (
            first_3pct_bar_ms is not None
        ),
        "target_5_reached": (
            first_5pct_bar_ms is not None
        ),
        "first_3pct_bar_ms": first_3pct_bar_ms,
        "first_5pct_bar_ms": first_5pct_bar_ms,
    }


def evaluate_window(trigger, horizon_hours):
    trigger_ms = trigger["trigger_at_ms"]

    start_ms = ceil_to_next_bar(trigger_ms)
    end_ms = (
        trigger_ms
        + horizon_hours * 60 * 60 * 1000
    )

    klines = fetch_klines(
        symbol=trigger["symbol"],
        start_ms=start_ms,
        end_ms=end_ms,
    )

    outcome = calculate_outcome(
        trigger=trigger,
        klines=klines,
        horizon_hours=horizon_hours,
    )

    outcome["window_start_ms"] = start_ms
    outcome["window_end_ms"] = end_ms
    outcome["sampling_note"] = (
        "Uses 5m candles beginning with the first full "
        "bar at/after the trigger. This avoids including "
        "pre-trigger price action; up to the first 5 "
        "minutes after the trigger may be omitted."
    )

    return outcome


def main():
    now_ms = int(time.time() * 1000)

    triggers = load_trigger_events()

    outcomes = load_json(
        OUTCOMES_FILE,
        {
            "version": "3.4-shadow",
            "triggers": {},
        },
    )

    outcomes.setdefault(
        "version",
        "3.4-shadow",
    )

    outcomes.setdefault(
        "triggers",
        {},
    )

    for trigger_id, trigger in triggers.items():
        record = outcomes["triggers"].setdefault(
            trigger_id,
            {
                **trigger,
                "outcome_24h": None,
                "outcome_48h": None,
            },
        )

        record.update(
            {
                "symbol": trigger["symbol"],
                "bias": trigger["bias"],
                "trigger_at_ms": trigger[
                    "trigger_at_ms"
                ],
                "trigger_price": trigger[
                    "trigger_price"
                ],
                "swing_score": trigger.get(
                    "swing_score"
                ),
            }
        )

        age_ms = (
            now_ms
            - int(trigger["trigger_at_ms"])
        )

        for horizon, key, event_name in (
            (
                24,
                "outcome_24h",
                "SWING_OUTCOME_24H",
            ),
            (
                48,
                "outcome_48h",
                "SWING_OUTCOME_48H",
            ),
        ):
            minimum_age = (
                horizon * 60 * 60 * 1000
            )

            if age_ms < minimum_age:
                continue

            if record.get(key) is not None:
                continue

            try:
                result = evaluate_window(
                    trigger=trigger,
                    horizon_hours=horizon,
                )

            except Exception as exc:
                print(
                    f"{trigger_id}: "
                    f"{horizon}h evaluation failed: "
                    f"{exc}"
                )
                continue

            record[key] = result
            record[
                f"evaluated_{horizon}h_at_ms"
            ] = now_ms

            append_event(
                {
                    "event": event_name,
                    "timestamp_ms": now_ms,
                    "trigger_id": trigger_id,
                    "symbol": trigger["symbol"],
                    "bias": trigger["bias"],
                    "trigger_at_ms": trigger[
                        "trigger_at_ms"
                    ],
                    "trigger_price": trigger[
                        "trigger_price"
                    ],
                    "swing_score": trigger.get(
                        "swing_score"
                    ),
                    "outcome": result,
                }
            )

    outcomes["last_processed_at_ms"] = now_ms

    OUTCOMES_FILE.write_text(
        json.dumps(
            outcomes,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    if not OUTCOME_EVENTS_FILE.exists():
        OUTCOME_EVENTS_FILE.write_text("")

    pending_24h = 0
    pending_48h = 0

    for record in outcomes["triggers"].values():
        age_ms = (
            now_ms
            - int(record["trigger_at_ms"])
        )

        if (
            age_ms >= 24 * 60 * 60 * 1000
            and record.get("outcome_24h")
            is None
        ):
            pending_24h += 1

        if (
            age_ms >= 48 * 60 * 60 * 1000
            and record.get("outcome_48h")
            is None
        ):
            pending_48h += 1

    print(
        f"Tracked {len(outcomes['triggers'])} "
        f"v3.4 trigger(s). "
        f"Pending matured evaluations: "
        f"24h={pending_24h}, "
        f"48h={pending_48h}."
    )


if __name__ == "__main__":
    main()
