import json
import time
from datetime import datetime, timezone
from pathlib import Path

# Swing Rank History v1 - SHADOW ONLY
# Persists one ranking snapshot per 15-minute bucket.
# Reads swing_rankings.json and writes daily JSONL history files.
# No Telegram alerts and no effect on live trading logic.

INPUT_FILE = Path("swing_rankings.json")
STATE_FILE = Path("swing_rank_history_state.json")
HISTORY_DIR = Path("swing_rank_history")

BUCKET_MINUTES = 15
BUCKET_MS = BUCKET_MINUTES * 60 * 1000


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def atomic_write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def bucket_start_ms(timestamp_ms):
    ts = int(timestamp_ms)
    return (ts // BUCKET_MS) * BUCKET_MS


def utc_date_from_ms(timestamp_ms):
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def compact_ranking(row):
    return {
        "symbol": row.get("symbol"),
        "bias": row.get("bias"),
        "setup_state": row.get("setup_state"),
        "price": row.get("price"),
        "swing_quality_score_v1": row.get("swing_quality_score_v1"),
        "swing_quality_rank_v1": row.get("swing_quality_rank_v1"),
        "swing_score": row.get("swing_score"),
        "activity_rank": row.get("activity_rank"),
        "relative_strength_vs_btc_pct": row.get("relative_strength_vs_btc_pct"),
        "relative_strength_vs_market_pct": row.get("relative_strength_vs_market_pct"),
        "momentum_3h_pct": row.get("momentum_3h_pct"),
        "momentum_12h_pct": row.get("momentum_12h_pct"),
        "momentum_24h_pct": row.get("momentum_24h_pct"),
        "volume_expansion_1h": row.get("volume_expansion_1h"),
        "quote_volume_24h": row.get("quote_volume_24h"),
        "price_change_24h_pct": row.get("price_change_24h_pct"),
        "quality_components": row.get("quality_components"),
    }


def main():
    now_ms = int(time.time() * 1000)

    payload = load_json(INPUT_FILE, {})
    if not isinstance(payload, dict):
        raise SystemExit("[Swing Rank History] invalid swing_rankings.json")

    rankings = payload.get("rankings")
    if not isinstance(rankings, list):
        raise SystemExit("[Swing Rank History] rankings missing")

    generated_at_ms = payload.get("generated_at_ms")
    if generated_at_ms is None:
        generated_at_ms = now_ms

    generated_at_ms = int(generated_at_ms)
    current_bucket = bucket_start_ms(generated_at_ms)

    state = load_json(
        STATE_FILE,
        {
            "version": "swing-rank-history-v1",
            "bucket_minutes": BUCKET_MINUTES,
            "last_bucket_start_ms": None,
            "snapshots_written": 0,
        },
    )

    if state.get("last_bucket_start_ms") == current_bucket:
        print(
            f"[Swing Rank History] bucket {current_bucket} already logged; skipping."
        )
        return

    record = {
        "version": "swing-rank-history-v1",
        "generated_at_ms": generated_at_ms,
        "bucket_start_ms": current_bucket,
        "market_regime_v1": payload.get("market_regime_v1"),
        "market_regime_score_v1": payload.get("market_regime_score_v1"),
        "market_context": payload.get("market_context"),
        "scanner_version": payload.get("scanner_version"),
        "strategy": payload.get("strategy"),
        "analysis_pool_count": payload.get("analysis_pool_count"),
        "directional_ranked_count": payload.get("directional_ranked_count"),
        "rankings": [
            compact_ranking(row)
            for row in rankings
            if isinstance(row, dict)
        ],
    }

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    date_key = utc_date_from_ms(generated_at_ms)
    history_file = HISTORY_DIR / f"{date_key}.jsonl"

    with history_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")

    new_state = {
        "version": "swing-rank-history-v1",
        "bucket_minutes": BUCKET_MINUTES,
        "last_bucket_start_ms": current_bucket,
        "last_generated_at_ms": generated_at_ms,
        "last_file": str(history_file),
        "last_market_regime_v1": payload.get("market_regime_v1"),
        "last_directional_ranked_count": payload.get("directional_ranked_count"),
        "snapshots_written": int(state.get("snapshots_written") or 0) + 1,
    }

    atomic_write_json(STATE_FILE, new_state)

    print(
        f"[Swing Rank History] wrote 1 snapshot to {history_file}; "
        f"regime={payload.get('market_regime_v1')}; "
        f"ranked={len(record['rankings'])}; "
        f"total={new_state['snapshots_written']}"
    )


if __name__ == "__main__":
    main()
