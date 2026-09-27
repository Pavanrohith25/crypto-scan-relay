#!/usr/bin/env python3
"""
Historical Feature Validator v1.1 - DATA ONLY / NO LIVE TRADING EFFECT

Extends v1 by reconstructing historical 24h quote volume from 288 completed
5-minute candles at each checkpoint. Candle-derived v3.4 feature formulas and
live parity checks remain delegated to the proven v1 implementation.
"""

from __future__ import annotations

import json
import time

import historical_feature_validator_v1 as base


def historical_quote_volume_24h(five_minute_closed: list[dict]) -> float:
    if len(five_minute_closed) < 288:
        raise ValueError(
            f"insufficient 5m history for 24h volume: {len(five_minute_closed)}"
        )
    return sum(float(x["quote_volume"]) for x in five_minute_closed[-288:])


def poison_future(rows: list[dict], as_of_ms: int) -> list[dict]:
    poisoned = []
    for row in rows:
        rr = dict(row)
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
        poisoned.append(rr)
    return poisoned


def run_historical_validation():
    manifest_path = base.ARTIFACT_DIR / "manifest.json"
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
        m5_path = base.ARTIFACT_DIR / symbol / f"5m_{manifest['days']}d.jsonl.gz"
        h1_path = base.ARTIFACT_DIR / symbol / f"1h_{manifest['days']}d.jsonl.gz"
        h4_path = base.ARTIFACT_DIR / symbol / f"4h_{manifest['days']}d.jsonl.gz"

        if not m5_path.exists() or not h1_path.exists() or not h4_path.exists():
            errors.append(
                {
                    "symbol": symbol,
                    "code": "HISTORICAL_FILES_MISSING",
                    "m5": str(m5_path),
                    "h1": str(h1_path),
                    "h4": str(h4_path),
                }
            )
            continue

        m5_all = base.load_gzip_jsonl(m5_path)
        h1_all = base.load_gzip_jsonl(h1_path)
        h4_all = base.load_gzip_jsonl(h4_path)

        for as_of_ms in base.deterministic_checkpoints(
            h1_all, h4_all, base.CHECKPOINTS_PER_SYMBOL
        ):
            m5_closed = base.closed_before(m5_all, as_of_ms)
            h1_closed = base.closed_before(h1_all, as_of_ms)
            h4_closed = base.closed_before(h4_all, as_of_ms)

            if len(m5_closed) < 288 or len(h1_closed) < 30 or len(h4_closed) < 25:
                continue

            rolling_quote_volume_24h = historical_quote_volume_24h(m5_closed)
            reconstructed = base.feature_core(
                h1_closed, h4_closed, rolling_quote_volume_24h
            )

            m5_poison_closed = base.closed_before(
                poison_future(m5_all, as_of_ms), as_of_ms
            )
            h1_poison_closed = base.closed_before(
                poison_future(h1_all, as_of_ms), as_of_ms
            )
            h4_poison_closed = base.closed_before(
                poison_future(h4_all, as_of_ms), as_of_ms
            )
            poisoned = base.feature_core(
                h1_poison_closed,
                h4_poison_closed,
                historical_quote_volume_24h(m5_poison_closed),
            )

            checked += 1

            if not base.same_visible_features(reconstructed, poisoned):
                errors.append(
                    {
                        "symbol": symbol,
                        "as_of_ms": as_of_ms,
                        "code": "LOOKAHEAD_LEAK_DETECTED",
                    }
                )

            if int(m5_closed[-1]["close_time_ms"]) >= as_of_ms:
                errors.append(
                    {
                        "symbol": symbol,
                        "as_of_ms": as_of_ms,
                        "code": "5M_FUTURE_CANDLE_USED",
                    }
                )

            if reconstructed["_last_1h_close_time_ms"] >= as_of_ms:
                errors.append(
                    {
                        "symbol": symbol,
                        "as_of_ms": as_of_ms,
                        "code": "1H_FUTURE_CANDLE_USED",
                    }
                )

            if reconstructed["_last_4h_close_time_ms"] >= as_of_ms:
                errors.append(
                    {
                        "symbol": symbol,
                        "as_of_ms": as_of_ms,
                        "code": "4H_FUTURE_CANDLE_USED",
                    }
                )

            if len(samples) < 30:
                sample = base.visible_feature_row(reconstructed)
                sample.update(
                    {
                        "symbol": symbol,
                        "as_of_ms": as_of_ms,
                        "last_5m_close_time_ms": int(m5_closed[-1]["close_time_ms"]),
                        "last_1h_close_time_ms": reconstructed[
                            "_last_1h_close_time_ms"
                        ],
                        "last_4h_close_time_ms": reconstructed[
                            "_last_4h_close_time_ms"
                        ],
                        "quote_volume_24h": rolling_quote_volume_24h,
                        "quote_volume_24h_note": (
                            "rolling 24h quote volume from 288 completed 5m candles; "
                            "no future candles used"
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


def main():
    base.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    historical = run_historical_validation()
    live = base.run_live_parity()

    overall = "PASS"
    if historical["status"] != "PASS" or live["status"] != "PASS":
        overall = "FAIL"

    report = {
        "version": "historical-feature-validator-v1.1",
        "generated_at_ms": int(time.time() * 1000),
        "deployed_logic_reference": "v3.4 swing discovery",
        "status": overall,
        "historical_no_lookahead": {
            k: v for k, v in historical.items() if k != "samples"
        },
        "live_parity": live,
        "important_notes": [
            "Candle-derived features exactly mirror deployed v3.4 formulas.",
            "Historical 24h quote volume uses 288 completed 5m candles at each checkpoint; live parity still uses the backend's actual rolling ticker quote_volume_24h.",
            "No outcome labels or predictive training are performed by this validator.",
        ],
    }

    base.REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    with base.SAMPLES_PATH.open("w", encoding="utf-8") as fh:
        for row in historical["samples"]:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    print(
        f"[Feature Validator v1.1] status={overall} | "
        f"historical_checkpoints={historical['checkpoints_tested']} | "
        f"historical_errors={historical['error_count']} | "
        f"live_symbols={live['symbols_compared']} | "
        f"live_mismatches={live['mismatch_count']}"
    )

    if overall != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
