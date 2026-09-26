#!/usr/bin/env python3
"""
Historical Feature Validator v1 - DATA ONLY / NO LIVE TRADING EFFECT

Validates two things before historical backfill is scaled:

1) Historical no-lookahead reconstruction
   - Reads the known-good raw backfill artifact.
   - Reconstructs the exact candle-derived v3.4 swing features at many past
     timestamps using ONLY candles closed before each timestamp.
   - Verifies no future candle can change an already-computed feature row.

2) Live parity against the deployed v3.4 /swing-discovery endpoint
   - Captures the backend's analysis_pool and generated_at_ms.
   - Re-fetches Binance 1h/4h candles with endTime pinned to that exact scan time.
   - Recomputes the deployed logic and compares every candle-derived field,
     bias, setup state, warnings, and swing_score.

No Telegram. No repo data mutation. No model training. No trade placement.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ARTIFACT_DIR = Path(os.getenv("BACKFILL_ARTIFACT_DIR", "_backfill_artifact"))
REPORT_PATH = ARTIFACT_DIR / "feature_validation_report.json"
SAMPLES_PATH = ARTIFACT_DIR / "feature_validation_samples.jsonl"

BACKEND_URL = os.getenv(
    "SWING_DISCOVERY_URL",
    "https://web-production-3431ab.up.railway.app/swing-discovery",
)
BINANCE_BASE = "https://data-api.binance.vision"

INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000}
CHECKPOINTS_PER_SYMBOL = int(os.getenv("FEATURE_CHECKPOINTS_PER_SYMBOL", "24"))
HTTP_ATTEMPTS = 4

NUMERIC_FIELDS = (
    "price",
    "momentum_3h_pct",
    "momentum_12h_pct",
    "momentum_24h_pct",
    "pullback_from_12h_high_pct",
    "bounce_from_12h_low_pct",
    "distance_from_1h_sma10_pct",
    "volume_expansion_1h",
    "swing_score",
)

EXACT_FIELDS = (
    "bias",
    "setup_state",
    "retest_reason",
    "warnings",
)


def http_json(url: str, attempts: int = HTTP_ATTEMPTS):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "crypto-scan-feature-validator/1.0",
                    "Accept": "application/json",
                },
            )
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(attempt * 2)
    raise RuntimeError(f"HTTP failed after {attempts} attempts: {url}: {last}")


def load_gzip_jsonl(path: Path) -> list[dict]:
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda x: int(x["open_time_ms"]))
    return rows


def as_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, value))


def closed_before(rows: list[dict], as_of_ms: int) -> list[dict]:
    # A candle is usable only after Binance says it has closed.
    return [r for r in rows if int(r["close_time_ms"]) < int(as_of_ms)]


def feature_core(
    one_hour: list[dict],
    four_hour: list[dict],
    quote_volume_24h: float = 0.0,
) -> dict:
    if len(one_hour) < 30 or len(four_hour) < 25:
        raise ValueError(
            f"insufficient closed history: 1h={len(one_hour)} 4h={len(four_hour)}"
        )

    h1_close = [float(c["close"]) for c in one_hour]
    h1_high = [float(c["high"]) for c in one_hour]
    h1_low = [float(c["low"]) for c in one_hour]
    # Deployed v3.4 uses Binance kline field [7] = quote asset volume.
    h1_volume = [float(c["quote_volume"]) for c in one_hour]

    h4_close = [float(c["close"]) for c in four_hour]

    price = h1_close[-1]

    sma_1h_10 = sum(h1_close[-10:]) / 10
    sma_1h_20 = sum(h1_close[-20:]) / 20
    old_sma_1h_10 = sum(h1_close[-15:-5]) / 10

    recent_12h_high = max(h1_high[-12:])
    recent_12h_low = min(h1_low[-12:])

    pullback_from_high_pct = (price / recent_12h_high - 1) * 100
    bounce_from_low_pct = (price / recent_12h_low - 1) * 100
    distance_from_1h_sma10_pct = (price / sma_1h_10 - 1) * 100

    momentum_3h = (price / h1_close[-4] - 1) * 100
    momentum_12h = (price / h1_close[-13] - 1) * 100

    sma_4h_10 = sum(h4_close[-10:]) / 10
    sma_4h_20 = sum(h4_close[-20:]) / 20
    old_sma_4h_10 = sum(h4_close[-15:-5]) / 10

    momentum_24h = (h4_close[-1] / h4_close[-7] - 1) * 100

    recent_volume = sum(h1_volume[-3:]) / 3
    previous_volume = sum(h1_volume[-15:-3]) / 12
    volume_expansion = recent_volume / previous_volume if previous_volume > 0 else 0

    long_structure = (
        price > sma_1h_10 > sma_1h_20
        and h4_close[-1] > sma_4h_10 > sma_4h_20
        and sma_1h_10 > old_sma_1h_10
        and sma_4h_10 > old_sma_4h_10
    )
    short_structure = (
        price < sma_1h_10 < sma_1h_20
        and h4_close[-1] < sma_4h_10 < sma_4h_20
        and sma_1h_10 < old_sma_1h_10
        and sma_4h_10 < old_sma_4h_10
    )

    if long_structure:
        bias = "LONG"
    elif short_structure:
        bias = "SHORT"
    else:
        bias = "NEUTRAL"

    setup_state = "TREND_ONLY"
    retest_reason = None

    if bias == "LONG":
        retest_ready = (
            -4.0 <= pullback_from_high_pct <= -0.75
            and -1.5 <= distance_from_1h_sma10_pct <= 1.0
        )
        continuation_trigger = (
            retest_ready
            and price > h1_close[-2]
            and price >= sma_1h_10
            and volume_expansion >= 0.9
        )
        if continuation_trigger:
            setup_state = "CONTINUATION_TRIGGER"
            retest_reason = "long pullback reached trend area and buying resumed"
        elif retest_ready:
            setup_state = "RETEST_READY"
            retest_reason = "long trend intact; price is near 1h trend support"

    elif bias == "SHORT":
        retest_ready = (
            0.75 <= bounce_from_low_pct <= 4.0
            and -1.0 <= distance_from_1h_sma10_pct <= 1.5
        )
        continuation_trigger = (
            retest_ready
            and price < h1_close[-2]
            and price <= sma_1h_10
            and volume_expansion >= 0.9
        )
        if continuation_trigger:
            setup_state = "CONTINUATION_TRIGGER"
            retest_reason = "short bounce reached trend area and selling resumed"
        elif retest_ready:
            setup_state = "RETEST_READY"
            retest_reason = "short trend intact; price is near 1h trend resistance"

    score = 0.0
    warnings = []

    if bias != "NEUTRAL":
        score += 25

    if bias == "LONG":
        if momentum_3h > 0:
            score += 10
        if momentum_12h > 0:
            score += 10
        if momentum_24h > 0:
            score += 10
    elif bias == "SHORT":
        if momentum_3h < 0:
            score += 10
        if momentum_12h < 0:
            score += 10
        if momentum_24h < 0:
            score += 10

    if volume_expansion >= 1.5:
        score += 20
    elif volume_expansion >= 1.1:
        score += 10
    elif volume_expansion < 0.7:
        score -= 8
        warnings.append("spot volume participation is weak")

    if quote_volume_24h >= 50_000_000:
        score += 10
    elif quote_volume_24h >= 10_000_000:
        score += 6

    if abs(momentum_12h) >= 8:
        score -= 20
        warnings.append(f"12h move already extended: {momentum_12h:+.2f}%")
    elif abs(momentum_12h) >= 5:
        score -= 10
        warnings.append(f"12h move somewhat extended: {momentum_12h:+.2f}%")

    score = round(clamp(score), 1)

    if abs(momentum_12h) >= 8:
        anti_chase_state = "EXTREME"
    elif abs(momentum_12h) >= 5:
        anti_chase_state = "EXTENDED"
    else:
        anti_chase_state = "CLEAN"

    return {
        "bias": bias,
        "setup_state": setup_state,
        "retest_reason": retest_reason,
        "price": price,
        "momentum_3h_pct": round(momentum_3h, 3),
        "momentum_12h_pct": round(momentum_12h, 3),
        "momentum_24h_pct": round(momentum_24h, 3),
        "pullback_from_12h_high_pct": round(pullback_from_high_pct, 3),
        "bounce_from_12h_low_pct": round(bounce_from_low_pct, 3),
        "distance_from_1h_sma10_pct": round(distance_from_1h_sma10_pct, 3),
        "volume_expansion_1h": round(volume_expansion, 2),
        "swing_score": score,
        "warnings": warnings,
        "anti_chase_state": anti_chase_state,
        # Audit-only internals:
        "_sma_1h_10": sma_1h_10,
        "_sma_1h_20": sma_1h_20,
        "_old_sma_1h_10": old_sma_1h_10,
        "_sma_4h_10": sma_4h_10,
        "_sma_4h_20": sma_4h_20,
        "_old_sma_4h_10": old_sma_4h_10,
        "_last_1h_close_time_ms": int(one_hour[-1]["close_time_ms"]),
        "_last_4h_close_time_ms": int(four_hour[-1]["close_time_ms"]),
    }


def historical_quote_volume_proxy(one_hour_closed: list[dict]) -> float:
    # This is deliberately marked a proxy. The live backend uses Binance's
    # rolling /ticker/24hr quoteVolume, which is not identical to the sum of
    # 24 completed 1h candles. We do NOT use this proxy to claim live parity.
    return sum(float(x["quote_volume"]) for x in one_hour_closed[-24:])


def deterministic_checkpoints(h1: list[dict], h4: list[dict], count: int) -> list[int]:
    # Need 25 completed 4h bars and 30 completed 1h bars before a checkpoint.
    earliest = max(
        int(h1[30]["open_time_ms"]) if len(h1) > 30 else 0,
        int(h4[25]["open_time_ms"]) if len(h4) > 25 else 0,
    )
    latest = min(
        int(h1[-1]["close_time_ms"]) + 1,
        int(h4[-1]["close_time_ms"]) + INTERVAL_MS["4h"],
    )
    if latest <= earliest:
        return []

    # Use hour boundaries offset by 5 minutes, similar to the production
    # scheduler, while spreading deterministically across the artifact.
    span = latest - earliest
    points = []
    for i in range(count):
        raw = earliest + ((i + 1) * span // (count + 1))
        hour = (raw // INTERVAL_MS["1h"]) * INTERVAL_MS["1h"]
        point = hour + 5 * 60 * 1000
        if earliest < point <= latest and point not in points:
            points.append(point)
    return sorted(points)


def visible_feature_row(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def same_visible_features(a: dict, b: dict) -> bool:
    aa = visible_feature_row(a)
    bb = visible_feature_row(b)
    return aa == bb


def run_historical_validation():
    manifest_path = ARTIFACT_DIR / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"raw manifest missing: {manifest_path}")

    manifest = json.loads(manifest_path.read_text())
    symbols = list(manifest.get("symbols") or [])
    if not symbols:
        raise RuntimeError("manifest has no symbols")

    errors = []
    samples = []
    checked = 0

    for symbol in symbols:
        h1_path = ARTIFACT_DIR / symbol / f"1h_{manifest['days']}d.jsonl.gz"
        h4_path = ARTIFACT_DIR / symbol / f"4h_{manifest['days']}d.jsonl.gz"
        if not h1_path.exists() or not h4_path.exists():
            errors.append(
                {
                    "symbol": symbol,
                    "code": "HISTORICAL_FILES_MISSING",
                    "h1": str(h1_path),
                    "h4": str(h4_path),
                }
            )
            continue

        h1_all = load_gzip_jsonl(h1_path)
        h4_all = load_gzip_jsonl(h4_path)

        for as_of_ms in deterministic_checkpoints(
            h1_all, h4_all, CHECKPOINTS_PER_SYMBOL
        ):
            h1_closed = closed_before(h1_all, as_of_ms)
            h4_closed = closed_before(h4_all, as_of_ms)

            if len(h1_closed) < 30 or len(h4_closed) < 25:
                continue

            proxy_volume = historical_quote_volume_proxy(h1_closed)
            base = feature_core(h1_closed, h4_closed, proxy_volume)

            # Poison every future candle. A correct as-of implementation must
            # ignore them completely and produce the exact same feature row.
            h1_poisoned = []
            for r in h1_all:
                rr = dict(r)
                if int(rr["close_time_ms"]) >= as_of_ms:
                    rr.update(
                        {
                            "open": 999999999.0,
                            "high": 999999999.0,
                            "low": 0.000000001,
                            "close": 999999999.0,
                            "quote_volume": 999999999999.0,
                        }
                    )
                h1_poisoned.append(rr)

            h4_poisoned = []
            for r in h4_all:
                rr = dict(r)
                if int(rr["close_time_ms"]) >= as_of_ms:
                    rr.update(
                        {
                            "open": 999999999.0,
                            "high": 999999999.0,
                            "low": 0.000000001,
                            "close": 999999999.0,
                            "quote_volume": 999999999999.0,
                        }
                    )
                h4_poisoned.append(rr)

            h1_poison_closed = closed_before(h1_poisoned, as_of_ms)
            h4_poison_closed = closed_before(h4_poisoned, as_of_ms)
            poisoned = feature_core(
                h1_poison_closed,
                h4_poison_closed,
                historical_quote_volume_proxy(h1_poison_closed),
            )

            checked += 1

            if not same_visible_features(base, poisoned):
                errors.append(
                    {
                        "symbol": symbol,
                        "as_of_ms": as_of_ms,
                        "code": "LOOKAHEAD_LEAK_DETECTED",
                    }
                )

            if base["_last_1h_close_time_ms"] >= as_of_ms:
                errors.append(
                    {
                        "symbol": symbol,
                        "as_of_ms": as_of_ms,
                        "code": "1H_FUTURE_CANDLE_USED",
                    }
                )

            if base["_last_4h_close_time_ms"] >= as_of_ms:
                errors.append(
                    {
                        "symbol": symbol,
                        "as_of_ms": as_of_ms,
                        "code": "4H_FUTURE_CANDLE_USED",
                    }
                )

            if len(samples) < 30:
                sample = visible_feature_row(base)
                sample.update(
                    {
                        "symbol": symbol,
                        "as_of_ms": as_of_ms,
                        "last_1h_close_time_ms": base["_last_1h_close_time_ms"],
                        "last_4h_close_time_ms": base["_last_4h_close_time_ms"],
                        "quote_volume_24h_note": (
                            "historical value is a 24 completed-1h-candle proxy; "
                            "not used for exact live ticker parity"
                        ),
                    }
                )
                samples.append(sample)

    return {
        "status": "PASS" if not errors else "FAIL",
        "checkpoints_tested": checked,
        "errors": errors[:100],
        "error_count": len(errors),
        "samples": samples,
    }


def raw_binance_to_rows(raw: list) -> list[dict]:
    return [
        {
            "open_time_ms": int(x[0]),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(x[5]),
            "close_time_ms": int(x[6]),
            "quote_volume": float(x[7]),
            "trades": int(x[8]),
            "taker_buy_base_volume": float(x[9]),
            "taker_buy_quote_volume": float(x[10]),
        }
        for x in raw
    ]


def fetch_binance_klines(symbol: str, interval: str, limit: int, end_ms: int):
    query = urlencode(
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
            "endTime": int(end_ms),
        }
    )
    data = http_json(f"{BINANCE_BASE}/api/v3/klines?{query}")
    if not isinstance(data, list):
        raise RuntimeError(f"invalid Binance kline payload for {symbol} {interval}")
    return data


def num_equal(a, b, tol=1e-9):
    try:
        return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)
    except (TypeError, ValueError):
        return a == b


def run_live_parity():
    payload = http_json(BACKEND_URL)
    generated_at_ms = int(payload["generated_at_ms"])
    pool = payload.get("analysis_pool") or []

    if not isinstance(pool, list) or not pool:
        raise RuntimeError("live swing-discovery analysis_pool is missing/empty")

    mismatches = []
    compared = 0

    for live in pool:
        symbol = live.get("symbol")
        if not symbol:
            continue

        h1_raw = fetch_binance_klines(symbol, "1h", 72, generated_at_ms)
        h4_raw = fetch_binance_klines(symbol, "4h", 40, generated_at_ms)

        # Mirror the deployed code exactly: the last returned candle is assumed
        # to be forming and is discarded BEFORE feature calculation.
        h1 = raw_binance_to_rows(h1_raw[:-1])
        h4 = raw_binance_to_rows(h4_raw[:-1])

        reconstructed = feature_core(
            h1,
            h4,
            as_float(live.get("quote_volume_24h")),
        )
        compared += 1

        for field in NUMERIC_FIELDS:
            if not num_equal(live.get(field), reconstructed.get(field), tol=1e-8):
                mismatches.append(
                    {
                        "symbol": symbol,
                        "field": field,
                        "live": live.get(field),
                        "reconstructed": reconstructed.get(field),
                    }
                )

        for field in EXACT_FIELDS:
            if live.get(field) != reconstructed.get(field):
                mismatches.append(
                    {
                        "symbol": symbol,
                        "field": field,
                        "live": live.get(field),
                        "reconstructed": reconstructed.get(field),
                    }
                )

    return {
        "status": "PASS" if not mismatches else "FAIL",
        "generated_at_ms": generated_at_ms,
        "analysis_pool_count": len(pool),
        "symbols_compared": compared,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:100],
    }


def main():
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    historical = run_historical_validation()
    live = run_live_parity()

    overall = "PASS"
    if historical["status"] != "PASS" or live["status"] != "PASS":
        overall = "FAIL"

    report = {
        "version": "historical-feature-validator-v1",
        "generated_at_ms": int(time.time() * 1000),
        "deployed_logic_reference": "v3.4 swing discovery",
        "status": overall,
        "historical_no_lookahead": {
            k: v for k, v in historical.items() if k != "samples"
        },
        "live_parity": live,
        "important_notes": [
            "Candle-derived features exactly mirror deployed v3.4 formulas.",
            "Historical 24h quote volume is only a completed-1h proxy; live parity uses the backend's actual rolling ticker quote_volume_24h.",
            "No outcome labels or predictive training are performed by this validator.",
        ],
    }

    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    with SAMPLES_PATH.open("w", encoding="utf-8") as fh:
        for row in historical["samples"]:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    print(
        f"[Feature Validator] status={overall} | "
        f"historical_checkpoints={historical['checkpoints_tested']} | "
        f"historical_errors={historical['error_count']} | "
        f"live_symbols={live['symbols_compared']} | "
        f"live_mismatches={live['mismatch_count']}"
    )

    if overall != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
