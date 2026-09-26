#!/usr/bin/env python3
"""Dataset v1 logger for the crypto scanner.

Reads new_swing.json produced by the workflow and stores one compact market
snapshot per 15-minute bucket. The Railway trigger may run every 5 minutes;
this logger intentionally de-duplicates those runs so the training dataset
stays useful without growing the repository unnecessarily.

Shadow/data collection only: this file does not change live signal logic,
Telegram alerts, or trading decisions.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

INPUT_PATH = Path("new_swing.json")
STATE_PATH = Path("dataset_v1_state.json")
DATA_DIR = Path("dataset_v1")
BUCKET_MINUTES = 15
BUCKET_MS = BUCKET_MINUTES * 60 * 1000
OBJECTIVE_VERSION = "swing-objective-v1"
CANONICAL_HOLDING_WINDOW = "24-72h"
CANONICAL_TARGET_MOVES_PCT = [3, 5, 10]


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def clean_candidate(row: dict) -> dict:
    """Keep compact, model-relevant fields from each analyzed symbol."""
    return {
        "symbol": row.get("symbol"),
        "bias": row.get("bias"),
        "setup_state": row.get("setup_state"),
        "swing_score": row.get("swing_score"),
        "price": row.get("price"),
        "momentum_3h_pct": row.get("momentum_3h_pct"),
        "momentum_12h_pct": row.get("momentum_12h_pct"),
        "momentum_24h_pct": row.get("momentum_24h_pct"),
        "pullback_from_12h_high_pct": row.get("pullback_from_12h_high_pct"),
        "bounce_from_12h_low_pct": row.get("bounce_from_12h_low_pct"),
        "distance_from_1h_sma10_pct": row.get("distance_from_1h_sma10_pct"),
        "volume_expansion_1h": row.get("volume_expansion_1h"),
        "quote_volume_24h": row.get("quote_volume_24h"),
        "price_change_24h_pct": row.get("price_change_24h_pct"),
        "activity_score": row.get("activity_score"),
        "activity_rank": row.get("activity_rank"),
        "retest_reason": row.get("retest_reason"),
        "warnings": row.get("warnings") or [],
    }


def main() -> None:
    if not INPUT_PATH.exists():
        print("[Dataset v1] new_swing.json not found; nothing to log.")
        return

    payload = load_json(INPUT_PATH, {})
    if not isinstance(payload, dict):
        print("[Dataset v1] invalid new_swing.json payload; skipping.")
        return

    pool = payload.get("analysis_pool")
    if not isinstance(pool, list):
        print(
            "[Dataset v1] analysis_pool missing. "
            "Backend Dataset v1 upgrade may not be deployed yet; skipping."
        )
        return

    generated_at_ms = payload.get("generated_at_ms")
    try:
        generated_at_ms = int(generated_at_ms)
    except (TypeError, ValueError):
        generated_at_ms = int(time.time() * 1000)

    bucket_start_ms = (generated_at_ms // BUCKET_MS) * BUCKET_MS

    state = load_json(
        STATE_PATH,
        {
            "version": "dataset-v1",
            "bucket_minutes": BUCKET_MINUTES,
            "last_bucket_start_ms": 0,
            "snapshots_written": 0,
        },
    )

    try:
        last_bucket = int(state.get("last_bucket_start_ms") or 0)
    except (TypeError, ValueError):
        last_bucket = 0

    if bucket_start_ms <= last_bucket:
        print(
            f"[Dataset v1] bucket {bucket_start_ms} already recorded; skipping duplicate."
        )
        return

    rows = [clean_candidate(row) for row in pool if isinstance(row, dict)]
    market_context = payload.get("market_context")
    if not isinstance(market_context, dict):
        market_context = {}

    snapshot = {
        "dataset_version": "1.1",
        "objective_version": OBJECTIVE_VERSION,
        "generated_at_ms": generated_at_ms,
        "bucket_start_ms": bucket_start_ms,
        "scanner_version": payload.get("version"),
        "strategy": payload.get("strategy"),
        "target_horizon": CANONICAL_HOLDING_WINDOW,
        "target_move": "3-10%",
        "target_moves_pct": CANONICAL_TARGET_MOVES_PCT,
        "source_target_horizon": payload.get("target_horizon"),
        "source_target_move": payload.get("target_move"),
        "universe_size": payload.get("universe_size"),
        "deep_checked": payload.get("deep_checked"),
        "analysis_pool_count": len(rows),
        "market_context": market_context,
        "analysis_pool": rows,
    }

    day = datetime.fromtimestamp(
        bucket_start_ms / 1000,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{day}.jsonl"
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, separators=(",", ":"), sort_keys=True))
        handle.write("\n")

    state.update(
        {
            "version": "dataset-v1",
            "bucket_minutes": BUCKET_MINUTES,
            "last_bucket_start_ms": bucket_start_ms,
            "last_generated_at_ms": generated_at_ms,
            "last_file": str(out_path),
            "last_analysis_pool_count": len(rows),
            "snapshots_written": int(state.get("snapshots_written") or 0) + 1,
        }
    )

    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATE_PATH)

    print(
        f"[Dataset v1] wrote {len(rows)} analyzed symbols to {out_path} "
        f"for 15m bucket {bucket_start_ms}."
    )


if __name__ == "__main__":
    main()
