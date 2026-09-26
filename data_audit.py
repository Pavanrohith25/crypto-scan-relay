#!/usr/bin/env python3
"""
Data Audit v1 - SHADOW ONLY

Checks internal consistency of the live swing dataset/ranking pipeline.
This does NOT change trading logic, Telegram alerts, or any score.

It writes data_audit_report.json and always exits 0 so an audit warning
cannot interrupt the live workflow. Once it has proven stable, it can be
made blocking in a separate data-only workflow.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from swing_ranker import classify_market_regime, score_candidate

NEW_SWING = Path("new_swing.json")
RANKINGS = Path("swing_rankings.json")
DATASET_STATE = Path("dataset_v1_state.json")
RANK_HISTORY_STATE = Path("swing_rank_history_state.json")
REPORT = Path("data_audit_report.json")

BUCKET_MINUTES = 15
BUCKET_MS = BUCKET_MINUTES * 60 * 1000

# Locked trading/research objective.
CANONICAL_OBJECTIVE = {
    "objective_version": "swing-objective-v1",
    "holding_window": "24-72h",
    "target_moves_pct": [3, 5, 10],
    "primary_style": "trend_continuation_swing",
}


def load_json(path: Path, default=None):
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def load_last_jsonl(path: Path):
    if not path.exists():
        return None
    last = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if last is None:
        return None
    try:
        return json.loads(last)
    except Exception:
        return None


def close(a, b, tol=1e-6):
    try:
        return math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)
    except (TypeError, ValueError):
        return False


def add(items, level, code, message, **details):
    row = {"level": level, "code": code, "message": message}
    if details:
        row["details"] = details
    items.append(row)


def audit_market_context(ctx, issues):
    if not isinstance(ctx, dict):
        add(issues, "ERROR", "MARKET_CONTEXT_MISSING", "market_context is missing or invalid")
        return

    universe = ctx.get("liquid_universe_count")
    adv = ctx.get("advancers_24h")
    dec = ctx.get("decliners_24h")
    flat = ctx.get("unchanged_24h")
    breadth = ctx.get("breadth_positive_pct")

    try:
        universe_i = int(universe)
        adv_i = int(adv)
        dec_i = int(dec)
        flat_i = int(flat)
    except (TypeError, ValueError):
        add(issues, "ERROR", "MARKET_COUNTS_INVALID", "market breadth counts are not integers")
        return

    if adv_i + dec_i + flat_i != universe_i:
        add(
            issues,
            "ERROR",
            "MARKET_COUNTS_DONT_SUM",
            "advancers + decliners + unchanged does not equal liquid_universe_count",
            advancers=adv_i,
            decliners=dec_i,
            unchanged=flat_i,
            universe=universe_i,
        )

    if universe_i > 0:
        expected = round(100.0 * adv_i / universe_i, 2)
        if not close(expected, breadth, tol=1e-4):
            add(
                issues,
                "ERROR",
                "BREADTH_MISMATCH",
                "breadth_positive_pct does not match advancers / universe",
                expected=expected,
                actual=breadth,
            )


def audit_analysis_pool(payload, issues):
    pool = payload.get("analysis_pool")
    if not isinstance(pool, list):
        add(issues, "ERROR", "ANALYSIS_POOL_MISSING", "analysis_pool is missing")
        return {}

    declared = payload.get("analysis_pool_count")
    if declared is not None and int(declared) != len(pool):
        add(
            issues,
            "ERROR",
            "ANALYSIS_POOL_COUNT_MISMATCH",
            "analysis_pool_count does not match actual analysis_pool length",
            declared=declared,
            actual=len(pool),
        )

    symbols = []
    source = {}
    ranks = []

    for idx, row in enumerate(pool):
        if not isinstance(row, dict):
            add(issues, "ERROR", "POOL_ROW_INVALID", "analysis_pool contains a non-object row", index=idx)
            continue

        symbol = row.get("symbol")
        if not symbol:
            add(issues, "ERROR", "SYMBOL_MISSING", "analysis_pool row has no symbol", index=idx)
            continue

        symbols.append(symbol)
        source[symbol] = row

        try:
            price = float(row.get("price"))
            if price <= 0:
                raise ValueError
        except (TypeError, ValueError):
            add(issues, "ERROR", "PRICE_INVALID", "candidate price is missing/non-positive", symbol=symbol)

        try:
            volume = float(row.get("quote_volume_24h"))
            if volume < 0:
                raise ValueError
        except (TypeError, ValueError):
            add(issues, "ERROR", "VOLUME_INVALID", "quote_volume_24h is invalid", symbol=symbol)

        rank = row.get("activity_rank")
        try:
            ranks.append(int(rank))
        except (TypeError, ValueError):
            add(issues, "ERROR", "ACTIVITY_RANK_INVALID", "activity_rank is invalid", symbol=symbol)

    if len(symbols) != len(set(symbols)):
        add(issues, "ERROR", "DUPLICATE_SYMBOLS", "analysis_pool contains duplicate symbols")

    if ranks and len(ranks) == len(pool):
        if len(ranks) != len(set(ranks)):
            add(issues, "ERROR", "DUPLICATE_ACTIVITY_RANK", "analysis_pool contains duplicate activity ranks")

    return source


def audit_rankings(source_payload, ranking_payload, source_map, issues):
    rows = ranking_payload.get("rankings")
    if not isinstance(rows, list):
        add(issues, "ERROR", "RANKINGS_MISSING", "swing_rankings.json has no rankings list")
        return

    ctx = source_payload.get("market_context") or {}
    expected_regime, expected_regime_score = classify_market_regime(ctx)

    if ranking_payload.get("market_regime_v1") != expected_regime:
        add(
            issues,
            "ERROR",
            "REGIME_MISMATCH",
            "saved market regime does not match recomputed regime",
            expected=expected_regime,
            actual=ranking_payload.get("market_regime_v1"),
        )

    if ranking_payload.get("market_regime_score_v1") != expected_regime_score:
        add(
            issues,
            "ERROR",
            "REGIME_SCORE_MISMATCH",
            "saved regime score does not match recomputed score",
            expected=expected_regime_score,
            actual=ranking_payload.get("market_regime_score_v1"),
        )

    previous_score = None
    expected_rank = 1

    for saved in rows:
        symbol = saved.get("symbol")
        source = source_map.get(symbol)

        if source is None:
            add(
                issues,
                "ERROR",
                "RANKED_SYMBOL_NOT_IN_POOL",
                "ranked symbol does not exist in current analysis_pool",
                symbol=symbol,
            )
            continue

        if str(source.get("bias") or "").upper() not in {"LONG", "SHORT"}:
            add(
                issues,
                "ERROR",
                "NEUTRAL_SYMBOL_RANKED",
                "a non-directional symbol appears in directional rankings",
                symbol=symbol,
                source_bias=source.get("bias"),
            )

        # Fields that should be copied exactly from the same source snapshot.
        for field in (
            "bias",
            "setup_state",
            "price",
            "swing_score",
            "activity_rank",
            "momentum_3h_pct",
            "momentum_12h_pct",
            "momentum_24h_pct",
            "volume_expansion_1h",
            "quote_volume_24h",
            "price_change_24h_pct",
        ):
            if saved.get(field) != source.get(field):
                add(
                    issues,
                    "ERROR",
                    "RANK_SOURCE_FIELD_MISMATCH",
                    "saved ranking field differs from source analysis_pool",
                    symbol=symbol,
                    field=field,
                    source=source.get(field),
                    saved=saved.get(field),
                )

        recomputed = score_candidate(source, ctx, expected_regime)

        if not close(
            saved.get("swing_quality_score_v1"),
            recomputed.get("swing_quality_score_v1"),
            tol=1e-9,
        ):
            add(
                issues,
                "ERROR",
                "QUALITY_SCORE_MISMATCH",
                "saved swing quality score does not match recomputation",
                symbol=symbol,
                expected=recomputed.get("swing_quality_score_v1"),
                actual=saved.get("swing_quality_score_v1"),
            )

        for field in ("relative_strength_vs_btc_pct", "relative_strength_vs_market_pct"):
            if not close(saved.get(field), recomputed.get(field), tol=1e-9):
                add(
                    issues,
                    "ERROR",
                    "RELATIVE_STRENGTH_MISMATCH",
                    "saved relative-strength value does not match recomputation",
                    symbol=symbol,
                    field=field,
                    expected=recomputed.get(field),
                    actual=saved.get(field),
                )

        rank = saved.get("swing_quality_rank_v1")
        if rank != expected_rank:
            add(
                issues,
                "ERROR",
                "QUALITY_RANK_SEQUENCE_INVALID",
                "quality ranks are not sequential",
                symbol=symbol,
                expected=expected_rank,
                actual=rank,
            )
        expected_rank += 1

        score = float(saved.get("swing_quality_score_v1") or 0)
        if previous_score is not None and score > previous_score:
            add(
                issues,
                "ERROR",
                "QUALITY_SORT_INVALID",
                "rankings are not sorted descending by quality score",
                symbol=symbol,
                previous_score=previous_score,
                current_score=score,
            )
        previous_score = score


def audit_persisted_snapshot(state_path, history_key, issues):
    state = load_json(state_path, {})
    last_file = state.get("last_file")

    if not last_file:
        add(
            issues,
            "WARNING",
            f"{history_key.upper()}_STATE_EMPTY",
            f"{state_path} has no last_file yet",
        )
        return None

    latest = load_last_jsonl(Path(last_file))
    if latest is None:
        add(
            issues,
            "ERROR",
            f"{history_key.upper()}_LATEST_UNREADABLE",
            f"could not read latest JSONL record from {last_file}",
        )
        return None

    if latest.get("bucket_start_ms") != state.get("last_bucket_start_ms"):
        add(
            issues,
            "ERROR",
            f"{history_key.upper()}_STATE_BUCKET_MISMATCH",
            "state bucket does not match latest persisted record",
            state_bucket=state.get("last_bucket_start_ms"),
            record_bucket=latest.get("bucket_start_ms"),
        )

    return latest


def main():
    now_ms = int(time.time() * 1000)
    issues = []

    source = load_json(NEW_SWING, {})
    rankings = load_json(RANKINGS, {})

    if not source:
        add(issues, "ERROR", "NEW_SWING_MISSING", "new_swing.json is missing or invalid")
    if not rankings:
        add(issues, "ERROR", "RANKINGS_FILE_MISSING", "swing_rankings.json is missing or invalid")

    if source:
        audit_market_context(source.get("market_context"), issues)
        source_map = audit_analysis_pool(source, issues)
    else:
        source_map = {}

    if source and rankings:
        # They must come from the same scan.
        if source.get("generated_at_ms") != rankings.get("generated_at_ms"):
            add(
                issues,
                "ERROR",
                "SOURCE_RANK_TIMESTAMP_MISMATCH",
                "new_swing and swing_rankings were not generated from the same scan",
                source_generated_at_ms=source.get("generated_at_ms"),
                rankings_generated_at_ms=rankings.get("generated_at_ms"),
            )
        audit_rankings(source, rankings, source_map, issues)

    latest_dataset = audit_persisted_snapshot(DATASET_STATE, "dataset", issues)
    latest_history = audit_persisted_snapshot(RANK_HISTORY_STATE, "rank_history", issues)

    # Freshness: persisted snapshots should be recent, but because they are 15m
    # bucketed we allow two buckets before warning.
    for name, snap in (("dataset", latest_dataset), ("rank_history", latest_history)):
        if snap and snap.get("generated_at_ms"):
            age_ms = now_ms - int(snap["generated_at_ms"])
            if age_ms > 2 * BUCKET_MS:
                add(
                    issues,
                    "WARNING",
                    f"{name.upper()}_STALE",
                    f"latest {name} snapshot is older than 30 minutes",
                    age_minutes=round(age_ms / 60000, 2),
                )

    # Known metadata guard. Existing Dataset v1 rows may still carry the old
    # 24-48h / 3-5% descriptors even though raw feature values are valid.
    if latest_dataset:
        old_horizon = latest_dataset.get("target_horizon")
        old_move = latest_dataset.get("target_move")
        if old_horizon != CANONICAL_OBJECTIVE["holding_window"] or old_move != "3-10%":
            add(
                issues,
                "WARNING",
                "LEGACY_OBJECTIVE_METADATA",
                "Dataset v1 contains legacy target metadata. Raw market features remain usable; historical/backfill labels must use the locked canonical objective instead.",
                stored_target_horizon=old_horizon,
                stored_target_move=old_move,
                canonical_holding_window=CANONICAL_OBJECTIVE["holding_window"],
                canonical_targets=CANONICAL_OBJECTIVE["target_moves_pct"],
            )

    errors = [x for x in issues if x["level"] == "ERROR"]
    warnings = [x for x in issues if x["level"] == "WARNING"]

    report = {
        "version": "data-audit-v1",
        "generated_at_ms": now_ms,
        "generated_at_utc": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(),
        "status": "FAIL" if errors else ("PASS_WITH_WARNINGS" if warnings else "PASS"),
        "canonical_objective": CANONICAL_OBJECTIVE,
        "checks": {
            "errors": len(errors),
            "warnings": len(warnings),
            "analysis_pool_count": len(source.get("analysis_pool") or []) if source else 0,
            "ranked_count": len(rankings.get("rankings") or []) if rankings else 0,
        },
        "issues": issues,
    }

    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    print(
        f"[Data Audit] status={report['status']} "
        f"errors={len(errors)} warnings={len(warnings)} "
        f"pool={report['checks']['analysis_pool_count']} "
        f"ranked={report['checks']['ranked_count']}"
    )

    # Intentionally non-blocking for v1.
    raise SystemExit(0)


if __name__ == "__main__":
    main()
