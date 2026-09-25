import json
import time
from pathlib import Path

# v3.4 SHADOW ONLY
# No Telegram alerts.
# No effect on v3.1 / v3.2 / v3.3 decisions.

SCAN_FILE = Path("new_swing.json")
STATE_FILE = Path("swing_state.json")
EVENTS_FILE = Path("swing_events.jsonl")


def load_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def append_event(event):
    with EVENTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(event, sort_keys=True) + "\n"
        )


def snapshot(candidate):
    keys = [
        "symbol",
        "bias",
        "swing_score",
        "setup_state",
        "retest_reason",
        "price",
        "momentum_3h_pct",
        "momentum_12h_pct",
        "momentum_24h_pct",
        "pullback_from_12h_high_pct",
        "bounce_from_12h_low_pct",
        "distance_from_1h_sma10_pct",
        "volume_expansion_1h",
        "quote_volume_24h",
        "reasons",
        "warnings",
    ]

    return {
        key: candidate.get(key)
        for key in keys
        if key in candidate
    }


def main():
    scan = load_json(SCAN_FILE, {})

    if not scan:
        raise RuntimeError(
            "new_swing.json is missing or invalid"
        )

    now_ms = int(time.time() * 1000)

    state = load_json(
        STATE_FILE,
        {
            "version": "3.4-shadow",
            "pairs": {},
        },
    )

    state.setdefault(
        "version",
        "3.4-shadow",
    )

    state.setdefault(
        "pairs",
        {},
    )

    state["last_processed_at_ms"] = now_ms

    candidates = scan.get("candidates", []) or []

    for candidate in candidates:
        symbol = candidate.get("symbol")

        if not symbol:
            continue

        bias = str(
            candidate.get("bias") or ""
        ).upper()

        setup_state = str(
            candidate.get("setup_state")
            or "TREND_ONLY"
        )

        price = candidate.get("price")
        score = candidate.get("swing_score")

        pair_state = state["pairs"].setdefault(
            symbol,
            {},
        )

        previous_state = pair_state.get(
            "last_setup_state"
        )

        previous_bias = pair_state.get(
            "last_bias"
        )

        first_seen = (
            pair_state.get("first_seen_at_ms")
            is None
        )

        if first_seen:
            pair_state["first_seen_at_ms"] = now_ms

            append_event(
                {
                    "event": "SWING_FIRST_SEEN",
                    "timestamp_ms": now_ms,
                    "symbol": symbol,
                    "bias": bias,
                    "setup_state": setup_state,
                    "price": price,
                    "swing_score": score,
                    "snapshot": snapshot(candidate),
                }
            )

        if (
            previous_state is not None
            and setup_state != previous_state
        ):
            append_event(
                {
                    "event": "SWING_STATE_TRANSITION",
                    "timestamp_ms": now_ms,
                    "symbol": symbol,
                    "bias": bias,
                    "from_state": previous_state,
                    "to_state": setup_state,
                    "price": price,
                    "swing_score": score,
                    "snapshot": snapshot(candidate),
                }
            )

        if (
            previous_bias is not None
            and bias != previous_bias
        ):
            append_event(
                {
                    "event": "SWING_BIAS_CHANGE",
                    "timestamp_ms": now_ms,
                    "symbol": symbol,
                    "from_bias": previous_bias,
                    "to_bias": bias,
                    "price": price,
                    "snapshot": snapshot(candidate),
                }
            )

        # Log a trigger only when the candidate ENTERS
        # CONTINUATION_TRIGGER. Repeated scans do not duplicate it.
        if (
            setup_state == "CONTINUATION_TRIGGER"
            and previous_state != "CONTINUATION_TRIGGER"
        ):
            append_event(
                {
                    "event": "SWING_CONTINUATION_TRIGGER",
                    "timestamp_ms": now_ms,
                    "symbol": symbol,
                    "bias": bias,
                    "trigger_price": price,
                    "swing_score": score,
                    "target_horizon": "24-48h",
                    "research_targets_pct": [
                        3.0,
                        5.0,
                    ],
                    "snapshot": snapshot(candidate),
                }
            )

            pair_state[
                "last_trigger_at_ms"
            ] = now_ms

            pair_state[
                "last_trigger_price"
            ] = price

            pair_state[
                "last_trigger_bias"
            ] = bias

        pair_state["last_seen_at_ms"] = now_ms
        pair_state["last_setup_state"] = setup_state
        pair_state["last_bias"] = bias
        pair_state["last_price"] = price
        pair_state["last_swing_score"] = score

    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    if not EVENTS_FILE.exists():
        EVENTS_FILE.write_text("")

    print(
        f"Processed {len(candidates)} "
        "v3.4 swing candidates."
    )


if __name__ == "__main__":
    main()
