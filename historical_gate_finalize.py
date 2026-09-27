#!/usr/bin/env python3
"""Build a simple historical eligibility gate from V3.4 backtest triggers.

The live Telegram message never displays rankings/scores. This file only uses
historical regime + hidden quality buckets to decide whether a future V3.4
continuation trigger resembles groups that reached +5% often enough in the
historical sample.
"""

import json
import os
import time
from collections import defaultdict
from pathlib import Path

TRIGGERS = Path(os.getenv("V34_BACKTEST_TRIGGERS", "historical_v34_5pct_triggers.jsonl"))
REPORT = Path(os.getenv("V34_BACKTEST_REPORT", "historical_v34_5pct_report.json"))
OUTPUT = Path(os.getenv("V34_GATE_CONFIG", "historical_gate_config.json"))

MIN_GROUP_SAMPLES = int(os.getenv("V34_MIN_GATE_SAMPLE", "20"))
MIN_TOTAL_SAMPLES = int(os.getenv("V34_MIN_TOTAL_SAMPLE", "100"))
MIN_HIT_RATE = float(os.getenv("V34_MIN_GATE_HIT_RATE", "0.60"))
LIVE_TP_PCT = 5.0
LIVE_SL_PCT = 6.0
MAX_HOLD_HOURS = 48


def main():
    report = json.loads(REPORT.read_text())
    groups = defaultdict(lambda: {"samples": 0, "wins": 0})

    for raw in TRIGGERS.read_text().splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        regime = str(row.get("market_regime") or "MIXED")
        bucket = str(row.get("quality_bucket") or "other")
        outcome = (row.get("outcomes") or {}).get("48") or {}
        key = (regime, bucket)
        groups[key]["samples"] += 1
        if outcome.get("target_5_reached"):
            groups[key]["wins"] += 1

    all_groups = []
    allowed = []
    for (regime, bucket), stats in sorted(groups.items()):
        n = stats["samples"]
        wins = stats["wins"]
        rate = wins / n if n else 0.0
        item = {
            "market_regime": regime,
            "quality_bucket": bucket,
            "samples": n,
            "target_5_hits_48h": wins,
            "target_5_hit_rate_48h": round(rate, 4),
        }
        all_groups.append(item)
        if n >= MIN_GROUP_SAMPLES and rate >= MIN_HIT_RATE:
            allowed.append(item)

    total = int(report.get("total_triggers") or 0)
    ready = total >= MIN_TOTAL_SAMPLES and bool(allowed)

    config = {
        "version": "historical-gate-v2-regime-quality",
        "generated_at_ms": int(time.time() * 1000),
        "enabled": ready,
        "objective": {
            "target_pct": LIVE_TP_PCT,
            "primary_horizon_hours": 24,
            "max_horizon_hours": MAX_HOLD_HOURS,
            "live_stop_pct": LIVE_SL_PCT,
        },
        "requirements": {
            "minimum_total_samples": MIN_TOTAL_SAMPLES,
            "minimum_group_samples": MIN_GROUP_SAMPLES,
            "minimum_48h_5pct_hit_rate": MIN_HIT_RATE,
        },
        "sample_count": total,
        "allowed_combinations": allowed,
        "all_combinations": all_groups,
        "research_best_stop_pct": report.get("research_best_stop_pct_by_average_simulated_return"),
        "note": (
            "When enabled, Telegram requires the live market-regime/hidden-quality combination "
            "to match an allowed historical group. TP remains +5%; live SL remains -6% until "
            "the stop study is reviewed separately. Historical performance is not a guarantee."
        ),
    }
    OUTPUT.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    print(
        f"[Gate] enabled={ready} samples={total} allowed_groups={len(allowed)} "
        f"threshold={MIN_HIT_RATE:.0%}"
    )


if __name__ == "__main__":
    main()
