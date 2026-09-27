#!/usr/bin/env python3
"""Historical V3.4 backtest for the user's actual trading objective.

Research only. Replays the candle-derived V3.4 continuation logic using only
information available at each historical timestamp, then measures whether the
setup reached +5% within 24h and 48h. It also records 4h/12h diagnostics,
market regime, relative-strength quality, time-to-target, MAE before target,
and a stop grid (-3/-4/-5/-6/-8%).

The script does NOT place trades and does NOT modify Telegram behavior.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import statistics
import time
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path

from historical_feature_validator_v1 import feature_core
from swing_ranker import classify_market_regime, score_candidate

ARTIFACT_DIR = Path(os.getenv("BACKFILL_ARTIFACT_DIR", "_backfill_artifact"))
REPORT_PATH = Path(os.getenv("V34_BACKTEST_REPORT", "historical_v34_5pct_report.json"))
TRIGGERS_PATH = Path(os.getenv("V34_BACKTEST_TRIGGERS", "historical_v34_5pct_triggers.jsonl"))
GATE_PATH = Path(os.getenv("V34_GATE_CONFIG", "historical_gate_config.json"))

TARGET_PCT = 5.0
PRIMARY_HORIZON_H = 24
SECONDARY_HORIZON_H = 48
DIAGNOSTIC_HORIZONS_H = (4, 12)
STOP_GRID_PCT = (3.0, 4.0, 5.0, 6.0, 8.0)
QUALITY_BUCKETS = ((0, 49), (50, 59), (60, 69), (70, 79), (80, 100))
MIN_GATE_SAMPLE = int(os.getenv("V34_MIN_GATE_SAMPLE", "20"))
MIN_TOTAL_SAMPLE = int(os.getenv("V34_MIN_TOTAL_SAMPLE", "100"))
MIN_GATE_HIT_RATE = float(os.getenv("V34_MIN_GATE_HIT_RATE", "0.55"))


def load_gzip_jsonl(path: Path) -> list[dict]:
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda x: int(x["open_time_ms"]))
    return rows


def pct_change(new: float, old: float) -> float:
    if old <= 0:
        return 0.0
    return (new / old - 1.0) * 100.0


def quantile(values: list[float], q: float):
    if not values:
        return None
    vals = sorted(float(x) for x in values)
    if len(vals) == 1:
        return round(vals[0], 4)
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return round(vals[lo], 4)
    frac = pos - lo
    return round(vals[lo] * (1 - frac) + vals[hi] * frac, 4)


def quality_bucket(score: float) -> str:
    score = float(score or 0)
    for lo, hi in QUALITY_BUCKETS:
        if lo <= score <= hi:
            return f"{lo}-{hi}"
    return "other"


def load_series(manifest: dict) -> dict[str, dict]:
    days = int(manifest["days"])
    series = {}
    for symbol in manifest.get("symbols") or []:
        base = ARTIFACT_DIR / symbol
        paths = {
            "5m": base / f"5m_{days}d.jsonl.gz",
            "1h": base / f"1h_{days}d.jsonl.gz",
            "4h": base / f"4h_{days}d.jsonl.gz",
        }
        if not all(p.exists() for p in paths.values()):
            print(f"[Backtest] skipping {symbol}: missing one or more interval files")
            continue
        try:
            h5 = load_gzip_jsonl(paths["5m"])
            h1 = load_gzip_jsonl(paths["1h"])
            h4 = load_gzip_jsonl(paths["4h"])
        except Exception as exc:
            print(f"[Backtest] skipping {symbol}: {exc}")
            continue
        if len(h1) < 60 or len(h4) < 30 or len(h5) < 1000:
            print(f"[Backtest] skipping {symbol}: insufficient history")
            continue
        series[symbol] = {
            "5m": h5,
            "1h": h1,
            "4h": h4,
            "h1_close_times": [int(r["close_time_ms"]) for r in h1],
            "h4_close_times": [int(r["close_time_ms"]) for r in h4],
            "h5_open_times": [int(r["open_time_ms"]) for r in h5],
        }
    return series


def closed_rows(rows: list[dict], close_times: list[int], as_of_ms: int) -> list[dict]:
    idx = bisect_left(close_times, int(as_of_ms))
    return rows[:idx]


def market_context_at(series: dict[str, dict], as_of_ms: int) -> dict:
    changes = []
    by_symbol = {}
    for symbol, data in series.items():
        h1 = data["1h"]
        idx = bisect_left(data["h1_close_times"], int(as_of_ms))
        if idx < 25:
            continue
        last = float(h1[idx - 1]["close"])
        old = float(h1[idx - 25]["close"])
        change = pct_change(last, old)
        changes.append(change)
        by_symbol[symbol] = change

    if not changes:
        return {
            "liquid_universe_count": 0,
            "advancers_24h": 0,
            "decliners_24h": 0,
            "unchanged_24h": 0,
            "breadth_positive_pct": 50.0,
            "median_24h_change_pct": 0.0,
            "btc_24h_change_pct": 0.0,
            "eth_24h_change_pct": 0.0,
        }

    adv = sum(1 for x in changes if x > 0)
    dec = sum(1 for x in changes if x < 0)
    flat = len(changes) - adv - dec
    return {
        "liquid_universe_count": len(changes),
        "advancers_24h": adv,
        "decliners_24h": dec,
        "unchanged_24h": flat,
        "breadth_positive_pct": round(100.0 * adv / len(changes), 2),
        "median_24h_change_pct": round(float(statistics.median(changes)), 3),
        "btc_24h_change_pct": round(float(by_symbol.get("BTCUSDT", 0.0)), 3),
        "eth_24h_change_pct": round(float(by_symbol.get("ETHUSDT", 0.0)), 3),
    }


def price_change_24h(h1_closed: list[dict]) -> float:
    if len(h1_closed) < 25:
        return 0.0
    return round(pct_change(float(h1_closed[-1]["close"]), float(h1_closed[-25]["close"])), 3)


def favorable_adverse(entry: float, bias: str, high: float, low: float):
    if bias == "SHORT":
        return (1.0 - low / entry) * 100.0, (1.0 - high / entry) * 100.0
    return (high / entry - 1.0) * 100.0, (low / entry - 1.0) * 100.0


def evaluate_horizon(data: dict, as_of_ms: int, entry: float, bias: str, horizon_h: int) -> dict | None:
    h5 = data["5m"]
    times = data["h5_open_times"]
    start = bisect_left(times, int(as_of_ms))
    end_ms = int(as_of_ms + horizon_h * 60 * 60 * 1000)
    end = bisect_left(times, end_ms)
    bars = h5[start:end]

    expected = int(horizon_h * 12)
    if len(bars) < max(1, expected - 2):
        return None

    mfe = 0.0
    mae = 0.0
    first_target_ms = None
    mae_before_target = 0.0
    stop_first = {s: None for s in STOP_GRID_PCT}
    resolutions = {s: "OPEN" for s in STOP_GRID_PCT}

    for bar in bars:
        t = int(bar["open_time_ms"])
        high = float(bar["high"])
        low = float(bar["low"])
        favorable, adverse = favorable_adverse(entry, bias, high, low)
        mfe = max(mfe, favorable)
        mae = min(mae, adverse)

        if first_target_ms is None:
            mae_before_target = min(mae_before_target, adverse)

        hit_target = favorable >= TARGET_PCT
        if hit_target and first_target_ms is None:
            first_target_ms = t

        for stop in STOP_GRID_PCT:
            if resolutions[stop] != "OPEN":
                continue
            hit_stop = adverse <= -stop
            if hit_target and hit_stop:
                resolutions[stop] = "AMBIGUOUS"
                stop_first[stop] = t
            elif hit_target:
                resolutions[stop] = "TARGET_FIRST"
            elif hit_stop:
                resolutions[stop] = "STOP_FIRST"
                stop_first[stop] = t

    last_close = float(bars[-1]["close"])
    close_return = (1.0 - last_close / entry) * 100.0 if bias == "SHORT" else (last_close / entry - 1.0) * 100.0

    stop_grid = {}
    for stop in STOP_GRID_PCT:
        result = resolutions[stop]
        if result == "TARGET_FIRST":
            simulated_return = TARGET_PCT
        elif result in {"STOP_FIRST", "AMBIGUOUS"}:
            simulated_return = -stop
        else:
            simulated_return = max(-stop, min(TARGET_PCT, close_return))
        stop_grid[str(stop)] = {
            "result": result,
            "simulated_48h_or_horizon_return_pct": round(simulated_return, 4),
        }

    return {
        "horizon_hours": horizon_h,
        "target_5_reached": first_target_ms is not None,
        "time_to_5pct_minutes": None if first_target_ms is None else round((first_target_ms - as_of_ms) / 60000.0, 2),
        "mae_before_5pct_pct": None if first_target_ms is None else round(mae_before_target, 4),
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
        "close_return_pct": round(close_return, 4),
        "stop_grid": stop_grid,
    }


def build_triggers(series: dict[str, dict]) -> list[dict]:
    records = []
    context_cache = {}

    for symbol, data in series.items():
        h1 = data["1h"]
        previous_state = None

        for idx in range(30, len(h1)):
            as_of_ms = int(h1[idx]["close_time_ms"]) + 1
            h1_closed = closed_rows(h1, data["h1_close_times"], as_of_ms)
            h4_closed = closed_rows(data["4h"], data["h4_close_times"], as_of_ms)
            if len(h1_closed) < 30 or len(h4_closed) < 25:
                continue

            quote_volume_proxy = sum(float(x["quote_volume"]) for x in h1_closed[-24:])
            try:
                features = feature_core(h1_closed, h4_closed, quote_volume_proxy)
            except Exception:
                continue

            state = str(features.get("setup_state") or "TREND_ONLY")
            bias = str(features.get("bias") or "NEUTRAL").upper()

            if state == "CONTINUATION_TRIGGER" and previous_state != "CONTINUATION_TRIGGER" and bias in {"LONG", "SHORT"}:
                if as_of_ms not in context_cache:
                    context_cache[as_of_ms] = market_context_at(series, as_of_ms)
                market_ctx = context_cache[as_of_ms]
                regime, regime_score = classify_market_regime(market_ctx)

                candidate = dict(features)
                candidate["symbol"] = symbol
                candidate["price_change_24h_pct"] = price_change_24h(h1_closed)
                candidate["quote_volume_24h"] = round(quote_volume_proxy, 2)
                quality = score_candidate(candidate, market_ctx, regime)

                entry = float(features["price"])
                outcomes = {}
                complete = True
                for horizon in (*DIAGNOSTIC_HORIZONS_H, PRIMARY_HORIZON_H, SECONDARY_HORIZON_H):
                    result = evaluate_horizon(data, as_of_ms, entry, bias, horizon)
                    if result is None:
                        complete = False
                        break
                    outcomes[str(horizon)] = result

                if complete:
                    records.append({
                        "symbol": symbol,
                        "bias": bias,
                        "trigger_at_ms": as_of_ms,
                        "trigger_at_utc": datetime.fromtimestamp(as_of_ms / 1000, tz=timezone.utc).isoformat(),
                        "entry_price": entry,
                        "setup_state": state,
                        "swing_score": features.get("swing_score"),
                        "swing_quality_score_v1": quality.get("swing_quality_score_v1"),
                        "quality_bucket": quality_bucket(quality.get("swing_quality_score_v1", 0)),
                        "relative_strength_vs_btc_pct": quality.get("relative_strength_vs_btc_pct"),
                        "relative_strength_vs_market_pct": quality.get("relative_strength_vs_market_pct"),
                        "market_regime": regime,
                        "market_regime_score": regime_score,
                        "market_context": market_ctx,
                        "features": {
                            k: features.get(k)
                            for k in (
                                "momentum_3h_pct", "momentum_12h_pct", "momentum_24h_pct",
                                "pullback_from_12h_high_pct", "bounce_from_12h_low_pct",
                                "distance_from_1h_sma10_pct", "volume_expansion_1h", "warnings"
                            )
                        },
                        "outcomes": outcomes,
                    })

            previous_state = state

        print(f"[Backtest] {symbol}: cumulative triggers={len(records)}")

    return records


def rate_summary(records: list[dict], horizon: str) -> dict:
    n = len(records)
    wins = sum(1 for r in records if r["outcomes"][horizon]["target_5_reached"])
    return {
        "samples": n,
        "target_5_hits": wins,
        "target_5_hit_rate": round(wins / n, 4) if n else None,
    }


def grouped_summary(records: list[dict], key: str) -> dict:
    groups = {}
    for r in records:
        groups.setdefault(str(r.get(key)), []).append(r)
    output = {}
    for name, rows in sorted(groups.items()):
        output[name] = {
            "24h": rate_summary(rows, "24"),
            "48h": rate_summary(rows, "48"),
        }
    return output


def stop_summary(records: list[dict]) -> dict:
    output = {}
    for stop in STOP_GRID_PCT:
        key = str(stop)
        results = [r["outcomes"]["48"]["stop_grid"][key] for r in records]
        counts = {x: sum(1 for row in results if row["result"] == x) for x in ("TARGET_FIRST", "STOP_FIRST", "AMBIGUOUS", "OPEN")}
        returns = [float(row["simulated_48h_or_horizon_return_pct"]) for row in results]
        output[key] = {
            "samples": len(results),
            "target_first": counts["TARGET_FIRST"],
            "stop_first": counts["STOP_FIRST"],
            "ambiguous": counts["AMBIGUOUS"],
            "open_at_48h": counts["OPEN"],
            "target_first_rate": round(counts["TARGET_FIRST"] / len(results), 4) if results else None,
            "average_simulated_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
            "median_simulated_return_pct": round(float(statistics.median(returns)), 4) if returns else None,
        }
    return output


def build_report(manifest: dict, records: list[dict]) -> dict:
    winners_48 = [r for r in records if r["outcomes"]["48"]["target_5_reached"]]
    mae_before = [abs(float(r["outcomes"]["48"]["mae_before_5pct_pct"])) for r in winners_48 if r["outcomes"]["48"]["mae_before_5pct_pct"] is not None]
    times = [float(r["outcomes"]["48"]["time_to_5pct_minutes"]) for r in winners_48 if r["outcomes"]["48"]["time_to_5pct_minutes"] is not None]
    stops = stop_summary(records)

    best_stop = None
    if stops:
        best_stop = max(
            stops.items(),
            key=lambda kv: (kv[1]["average_simulated_return_pct"] if kv[1]["average_simulated_return_pct"] is not None else -999),
        )[0]

    return {
        "version": "v34-five-percent-backtest-v1",
        "generated_at_ms": int(time.time() * 1000),
        "objective": {
            "target_pct": TARGET_PCT,
            "primary_horizon_hours": PRIMARY_HORIZON_H,
            "secondary_horizon_hours": SECONDARY_HORIZON_H,
            "diagnostic_horizons_hours": list(DIAGNOSTIC_HORIZONS_H),
            "stop_grid_pct": list(STOP_GRID_PCT),
        },
        "source": {
            "days": manifest.get("days"),
            "symbols_requested": manifest.get("symbols"),
            "market": manifest.get("market"),
            "historical_quote_volume_note": "Uses sum of completed 1h quote-volume candles as a historical proxy for rolling 24h quote volume.",
            "historical_price_change_note": "Uses completed 1h closes for historical 24h price change.",
        },
        "total_triggers": len(records),
        "overall": {
            "4h": rate_summary(records, "4"),
            "12h": rate_summary(records, "12"),
            "24h": rate_summary(records, "24"),
            "48h": rate_summary(records, "48"),
        },
        "by_market_regime": grouped_summary(records, "market_regime"),
        "by_quality_bucket": grouped_summary(records, "quality_bucket"),
        "winner_48h_drawdown_before_5pct": {
            "samples": len(mae_before),
            "median_abs_mae_pct": quantile(mae_before, 0.50),
            "p75_abs_mae_pct": quantile(mae_before, 0.75),
            "p90_abs_mae_pct": quantile(mae_before, 0.90),
            "p95_abs_mae_pct": quantile(mae_before, 0.95),
        },
        "winner_48h_time_to_5pct_minutes": {
            "samples": len(times),
            "median": quantile(times, 0.50),
            "p75": quantile(times, 0.75),
            "p90": quantile(times, 0.90),
        },
        "stop_grid_48h": stops,
        "research_best_stop_pct_by_average_simulated_return": None if best_stop is None else float(best_stop),
        "warnings": [
            "Historical backtests can overfit and do not guarantee future results.",
            "The historical macro/volume reconstruction uses completed-candle proxies rather than exchange rolling-ticker snapshots.",
        ],
    }


def build_gate_config(report: dict) -> dict:
    total = int(report.get("total_triggers") or 0)
    regimes = report.get("by_market_regime") or {}
    buckets = report.get("by_quality_bucket") or {}

    allowed_regimes = []
    for name, stats in regimes.items():
        s = stats.get("48h") or {}
        if int(s.get("samples") or 0) >= MIN_GATE_SAMPLE and float(s.get("target_5_hit_rate") or 0) >= MIN_GATE_HIT_RATE:
            allowed_regimes.append(name)

    allowed_buckets = []
    for name, stats in buckets.items():
        s = stats.get("48h") or {}
        if int(s.get("samples") or 0) >= MIN_GATE_SAMPLE and float(s.get("target_5_hit_rate") or 0) >= MIN_GATE_HIT_RATE:
            allowed_buckets.append(name)

    enough = total >= MIN_TOTAL_SAMPLE and bool(allowed_regimes) and bool(allowed_buckets)
    return {
        "version": "historical-gate-v1",
        "generated_at_ms": report.get("generated_at_ms"),
        "objective": report.get("objective"),
        "sample_count": total,
        "ready_for_review": enough,
        "enabled": False,
        "activation_note": "Gate stays disabled until the backtest report is reviewed. When enabled, Telegram can use only historically supported regime/quality buckets.",
        "minimum_bucket_samples": MIN_GATE_SAMPLE,
        "minimum_48h_5pct_hit_rate": MIN_GATE_HIT_RATE,
        "allowed_market_regimes_research": allowed_regimes,
        "allowed_quality_buckets_research": allowed_buckets,
        "research_best_stop_pct": report.get("research_best_stop_pct_by_average_simulated_return"),
    }


def main():
    manifest_path = ARTIFACT_DIR / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    series = load_series(manifest)
    if len(series) < 5:
        raise SystemExit(f"only {len(series)} usable symbols; refusing to produce a broad-market backtest")

    records = build_triggers(series)
    report = build_report(manifest, records)
    gate = build_gate_config(report)

    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    GATE_PATH.write_text(json.dumps(gate, indent=2, sort_keys=True) + "\n")
    with TRIGGERS_PATH.open("w", encoding="utf-8") as fh:
        for row in records:
            fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")

    print(
        f"[Backtest] triggers={len(records)} | "
        f"24h_hit={report['overall']['24h']['target_5_hit_rate']} | "
        f"48h_hit={report['overall']['48h']['target_5_hit_rate']} | "
        f"research_best_stop={report['research_best_stop_pct_by_average_simulated_return']}"
    )


if __name__ == "__main__":
    main()
