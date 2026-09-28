#!/usr/bin/env python3
"""
Train Swing Model v1 - research only.

Consumes _ml_artifact/historical_ml_dataset_v1.jsonl.gz, performs a strictly
chronological 60/20/20 train/validation/test split, trains a gradient-boosted
classifier, selects an alert threshold only on validation data, and evaluates
that frozen threshold on the untouched newest test segment.

The model predicts: +5% target before -3% stop within 48h.
It is NOT wired into Telegram by this script.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_recall_curve,
    roc_auc_score,
)

ARTIFACT_DIR = Path(os.getenv("ML_ARTIFACT_DIR", "_ml_artifact"))
DATASET_PATH = ARTIFACT_DIR / "historical_ml_dataset_v1.jsonl.gz"
MODEL_PATH = ARTIFACT_DIR / "swing_model_v1.pkl"
REPORT_PATH = ARTIFACT_DIR / "swing_model_v1_report.json"

TRAIN_FRAC = float(os.getenv("ML_TRAIN_FRAC", "0.60"))
VAL_FRAC = float(os.getenv("ML_VAL_FRAC", "0.20"))
MIN_VAL_ALERTS = int(os.getenv("ML_MIN_VAL_ALERTS", "20"))
MIN_TEST_ALERTS = int(os.getenv("ML_MIN_TEST_ALERTS", "20"))
MIN_TEST_PRECISION = float(os.getenv("ML_MIN_TEST_PRECISION", "0.50"))
MIN_TEST_AVG_RETURN = float(os.getenv("ML_MIN_TEST_AVG_RETURN", "0.20"))
ROUND_TRIP_COST_PCT = float(os.getenv("ML_ROUND_TRIP_COST_PCT", "0.12"))
PURGE_GAP_HOURS = int(os.getenv("ML_PURGE_GAP_HOURS", "72"))
ALERT_COOLDOWN_HOURS = int(os.getenv("ML_ALERT_COOLDOWN_HOURS", "12"))

TARGET_COL = "target5_before_stop1_5_48h"
RETURN_COL = "sim_return_target5_stop1_5_48h_pct"
TIME_COL = "asof_ms"

FEATURES = [
    "direction_sign",
    "activity_score",
    "activity_rank",
    "quote_volume_24h",
    "price_change_24h_pct",
    "taker_buy_ratio_24h",
    "taker_buy_ratio_1h",
    "market_regime_score",
    "market_breadth_pct",
    "market_median_change_24h_pct",
    "btc_change_24h_pct",
    "eth_change_24h_pct",
    "liquid_symbol_count",
    "directional_momentum_3h",
    "directional_momentum_12h",
    "directional_momentum_24h",
    "directional_distance_from_1h_sma10_pct",
    "directional_distance_from_4h_sma10_pct",
    "sma1h10_slope_pct",
    "sma4h10_slope_pct",
    "volume_expansion_1h",
    "directional_momentum_15m",
    "directional_momentum_45m",
    "directional_momentum_180m",
    "distance_from_15m_sma4_pct",
    "distance_from_15m_sma12_pct",
    "volume_expansion_15m",
    "atr_1h_pct",
    "realized_vol_12h_pct",
    "relative_strength_vs_btc_24h",
    "relative_strength_vs_eth_24h",
    "retest_ready",
    "trigger_like_1h",
    "confirmation_15m",
    "anti_chase_abs_12h_pct",
]


def load_dataset() -> pd.DataFrame:
    rows = []
    with gzip.open(DATASET_PATH, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        raise RuntimeError("dataset is empty")
    df = pd.DataFrame(rows)
    missing = [c for c in FEATURES + [TARGET_COL, RETURN_COL, TIME_COL] if c not in df.columns]
    if missing:
        raise RuntimeError(f"missing required columns: {missing}")
    df = df.sort_values([TIME_COL, "symbol"]).reset_index(drop=True)
    # Conservative: ambiguous path rows are excluded from classifier training.
    if "ambiguous_target5_stop1_5_48h" in df.columns:
        df = df[df["ambiguous_target5_stop1_5_48h"].fillna(0).astype(int) == 0].copy()
    for col in FEATURES:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df[FEATURES] = df[FEATURES].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    df[TARGET_COL] = pd.to_numeric(df[TARGET_COL], errors="coerce").fillna(0).astype(int)
    df[RETURN_COL] = pd.to_numeric(df[RETURN_COL], errors="coerce").fillna(0.0)
    return df


def split_by_time(df: pd.DataFrame):
    # Purged chronological split: no training label window is allowed to overlap
    # validation, and no validation label window may overlap test.
    times = np.array(sorted(df[TIME_COL].unique()))
    if len(times) < 20:
        raise RuntimeError("too few unique timestamps for chronological split")
    train_i = max(1, int(len(times) * TRAIN_FRAC))
    val_i = max(train_i + 1, int(len(times) * (TRAIN_FRAC + VAL_FRAC)))
    val_i = min(val_i, len(times) - 1)
    val_start = int(times[train_i])
    test_start = int(times[val_i])
    gap_ms = PURGE_GAP_HOURS * 60 * 60 * 1000
    train_end = val_start - gap_ms
    val_end = test_start - gap_ms

    train = df[df[TIME_COL] <= train_end].copy()
    val = df[(df[TIME_COL] >= val_start) & (df[TIME_COL] <= val_end)].copy()
    test = df[df[TIME_COL] >= test_start].copy()
    if min(len(train), len(val), len(test)) < 20:
        raise RuntimeError(
            f"purged split too small train={len(train)} val={len(val)} test={len(test)} "
            f"gap_h={PURGE_GAP_HOURS}"
        )
    return train, val, test, int(train_end), int(val_end), int(val_start), int(test_start)


def safe_auc(y, p):
    try:
        return float(roc_auc_score(y, p))
    except Exception:
        return None


def safe_ap(y, p):
    try:
        return float(average_precision_score(y, p))
    except Exception:
        return None


def alert_mask_with_cooldown(df: pd.DataFrame, probs: np.ndarray, threshold: float) -> np.ndarray:
    raw = probs >= threshold
    if not raw.any():
        return raw
    order = np.argsort(df[TIME_COL].to_numpy())
    last_by_symbol: dict[str, int] = {}
    cooldown_ms = ALERT_COOLDOWN_HOURS * 60 * 60 * 1000
    keep = np.zeros(len(df), dtype=bool)
    symbols = df["symbol"].astype(str).to_numpy()
    times = df[TIME_COL].to_numpy(dtype=np.int64)
    for idx in order:
        if not raw[idx]:
            continue
        symbol = symbols[idx]
        ts = int(times[idx])
        prev = last_by_symbol.get(symbol)
        if prev is not None and ts - prev < cooldown_ms:
            continue
        keep[idx] = True
        last_by_symbol[symbol] = ts
    return keep


def subset_metrics(df: pd.DataFrame, probs: np.ndarray, threshold: float) -> dict:
    mask = alert_mask_with_cooldown(df, probs, threshold)
    n = int(mask.sum())
    base_rate = float(df[TARGET_COL].mean()) if len(df) else 0.0
    if n == 0:
        return {
            "threshold": float(threshold),
            "alerts": 0,
            "coverage": 0.0,
            "precision": None,
            "lift_vs_base": None,
            "avg_simulated_return_pct_after_cost": None,
            "median_simulated_return_pct_after_cost": None,
        }
    y = df.loc[mask, TARGET_COL].to_numpy()
    rets = df.loc[mask, RETURN_COL].to_numpy(dtype=float) - ROUND_TRIP_COST_PCT
    precision = float(y.mean())
    return {
        "threshold": float(threshold),
        "alerts": n,
        "coverage": float(n / len(df)),
        "precision": precision,
        "lift_vs_base": float(precision / base_rate) if base_rate > 0 else None,
        "avg_simulated_return_pct_after_cost": float(np.mean(rets)),
        "median_simulated_return_pct_after_cost": float(np.median(rets)),
    }


def choose_threshold(val: pd.DataFrame, probs: np.ndarray) -> tuple[float, list[dict]]:
    # Threshold selection is performed ONLY on validation data.
    candidates = sorted(set([round(x, 3) for x in np.linspace(0.35, 0.90, 56)]))
    rows = [subset_metrics(val, probs, t) for t in candidates]
    eligible = [
        r for r in rows
        if r["alerts"] >= MIN_VAL_ALERTS
        and r["avg_simulated_return_pct_after_cost"] is not None
        and r["avg_simulated_return_pct_after_cost"] > 0
    ]
    if eligible:
        # Prefer precision first, then expected return, then lower coverage.
        best = max(
            eligible,
            key=lambda r: (
                r["precision"] if r["precision"] is not None else -1,
                r["avg_simulated_return_pct_after_cost"],
                -r["coverage"],
            ),
        )
        return float(best["threshold"]), rows

    # If no positive-EV threshold exists, select a conservative top-tail threshold
    # for diagnosis but mark the model non-promotable later.
    q = float(np.quantile(probs, 0.90)) if len(probs) else 1.0
    return max(0.5, min(0.95, q)), rows


def slice_report(df: pd.DataFrame, probs: np.ndarray, threshold: float) -> dict:
    y = df[TARGET_COL].to_numpy()
    report = {
        "samples": int(len(df)),
        "positive_rate": float(np.mean(y)) if len(y) else 0.0,
        "roc_auc": safe_auc(y, probs),
        "average_precision": safe_ap(y, probs),
        "brier": float(brier_score_loss(y, probs)) if len(set(y)) > 1 else None,
        "selected_threshold": subset_metrics(df, probs, threshold),
        "top_deciles": {},
    }
    for q in (0.90, 0.95, 0.98):
        t = float(np.quantile(probs, q)) if len(probs) else 1.0
        report["top_deciles"][f"top_{int((1-q)*100)}pct"] = subset_metrics(df, probs, t)
    return report


def feature_importance(model, X: pd.DataFrame, y: pd.Series) -> list[dict]:
    # Permutation importance on a capped recent sample keeps runtime reasonable.
    if len(X) > 5000:
        X = X.iloc[-5000:]
        y = y.iloc[-5000:]
    try:
        pi = permutation_importance(
            model, X, y, n_repeats=3, random_state=42, scoring="average_precision"
        )
        pairs = sorted(zip(FEATURES, pi.importances_mean), key=lambda x: x[1], reverse=True)
        return [{"feature": f, "importance": float(v)} for f, v in pairs[:20]]
    except Exception:
        return []


def main():
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    df = load_dataset()
    train, val, test, train_end, val_end, val_start, test_start = split_by_time(df)

    X_train = train[FEATURES]
    y_train = train[TARGET_COL]
    X_val = val[FEATURES]
    y_val = val[TARGET_COL]
    X_test = test[FEATURES]
    y_test = test[TARGET_COL]

    model = HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=300,
        max_leaf_nodes=15,
        min_samples_leaf=40,
        l2_regularization=1.0,
        random_state=42,
    )
    model.fit(X_train, y_train)

    p_train = model.predict_proba(X_train)[:, 1]
    p_val = model.predict_proba(X_val)[:, 1]
    p_test = model.predict_proba(X_test)[:, 1]

    threshold, threshold_table = choose_threshold(val, p_val)
    train_report = slice_report(train, p_train, threshold)
    val_report = slice_report(val, p_val, threshold)
    test_report = slice_report(test, p_test, threshold)

    test_sel = test_report["selected_threshold"]
    promotable = bool(
        test_sel["alerts"] >= MIN_TEST_ALERTS
        and test_sel["precision"] is not None
        and test_sel["precision"] >= MIN_TEST_PRECISION
        and test_sel["avg_simulated_return_pct_after_cost"] is not None
        and test_sel["avg_simulated_return_pct_after_cost"] >= MIN_TEST_AVG_RETURN
    )

    report = {
        "version": "swing-model-v1",
        "generated_at_ms": int(time.time() * 1000),
        "research_only": True,
        "prediction_target": "+5% before -1.5% within 48h",
        "promotable_to_live_shadow": promotable,
        "promotion_requirements": {
            "minimum_test_alerts": MIN_TEST_ALERTS,
            "minimum_test_precision": MIN_TEST_PRECISION,
            "minimum_test_avg_return_pct_after_cost": MIN_TEST_AVG_RETURN,
            "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
            "alert_cooldown_hours": ALERT_COOLDOWN_HOURS,
        },
        "chronological_split": {
            "train_samples": int(len(train)),
            "validation_samples": int(len(val)),
            "test_samples": int(len(test)),
            "train_end_asof_ms": train_end,
            "validation_start_asof_ms": val_start,
            "validation_end_asof_ms": val_end,
            "test_start_asof_ms": test_start,
            "purge_gap_hours": PURGE_GAP_HOURS,
            "test_is_newest_untouched_segment": True,
        },
        "selected_probability_threshold_from_validation_only": threshold,
        "train": train_report,
        "validation": val_report,
        "test": test_report,
        "top_features_by_validation_permutation_importance": feature_importance(model, X_val, y_val),
        "validation_threshold_table": threshold_table,
        "notes": [
            "No random train/test split is used.",
            "A 72h purge/embargo gap prevents outcome-window overlap across the canonical label horizon.",
            "Alert metrics apply a per-symbol cooldown so repeated 15m snapshots do not count as separate calls.",
            "The threshold is chosen only on validation data and frozen before test evaluation.",
            "A positive test result does not guarantee future profitability.",
            "Underlying price-move returns are evaluated before leverage; leverage does not create edge.",
            "This model is not connected to Telegram or order execution.",
        ],
    }

    bundle = {
        "model": model,
        "features": FEATURES,
        "threshold": threshold,
        "version": "swing-model-v1",
        "prediction_target": "+5% before -1.5% within 48h",
    }
    with MODEL_PATH.open("wb") as fh:
        pickle.dump(bundle, fh)
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    print(json.dumps({
        "promotable_to_live_shadow": promotable,
        "selected_threshold": threshold,
        "train": train_report,
        "validation": val_report,
        "test": test_report,
        "top_features": report["top_features_by_validation_permutation_importance"][:10],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
