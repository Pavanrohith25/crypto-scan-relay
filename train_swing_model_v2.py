#!/usr/bin/env python3
"""Selective futures entry model, research only.

Compare three models on validation, freeze winner and threshold, evaluate ONLY
that winner on newest holdout. Calibration uses a purged training tail, never
the validation/test labels. No Telegram or trading actions.
"""
from __future__ import annotations
import gzip
import hashlib
import json
import math
import os
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from train_swing_model_v1 import FEATURES, TARGET_COL, RETURN_COL, TIME_COL

HOUR = 3_600_000
MIN_ALERTS = 100
MIN_PRECISION = .60
PURGE_HOURS = 72
COST_PCT = float(os.getenv("ML_ROUND_TRIP_COST_PCT", ".12"))
FUNDING_RESERVE_PCT = float(os.getenv("ML_FUNDING_RESERVE_PCT", ".06"))
COOLDOWN_HOURS = 12


def load_dataset(path: Path) -> pd.DataFrame:
    # Read only the fixed features plus execution/audit columns, not the large
    # collection of outcome labels. Float32 reduces the broad dataset footprint.
    cols = list(dict.fromkeys(FEATURES + [TIME_COL, "symbol", "direction", "market_regime",
                "entry_eligible", TARGET_COL, RETURN_COL, "exit_ms", "entry_price",
                "mae_first_15m_pct", "mae_first_30m_pct", "mae_first_60m_pct",
                "ambiguous_target5_stop1_5_48h"]))
    chunks, batch = [], []
    with gzip.open(path, "rt") as f:
        for line in f:
            row = json.loads(line)
            missing = set(cols) - row.keys()
            if missing:
                raise ValueError(f"missing columns: {sorted(missing)}")
            batch.append({k: row[k] for k in cols})
            if len(batch) >= 25_000:
                chunks.append(compact_frame(batch))
                batch = []
        if batch:
            chunks.append(compact_frame(batch))
    if not chunks:
        raise ValueError("empty dataset")
    df = pd.concat(chunks, ignore_index=True).sort_values([TIME_COL, "symbol"]).reset_index(drop=True)
    # Ambiguous bars remain as conservative losses, including in evaluation.
    if not df[TARGET_COL].isin([0, 1]).all() or not np.isfinite(df[RETURN_COL]).all():
        raise ValueError("invalid target/return labels")
    if (df["exit_ms"] <= df[TIME_COL]).any() or (df["exit_ms"] > df[TIME_COL] + 48 * HOUR).any():
        raise ValueError("invalid outcome horizon")
    if df.duplicated([TIME_COL, "symbol"]).any():
        raise ValueError("duplicate decision rows")
    return df


def compact_frame(rows):
    df = pd.DataFrame(rows)
    df[FEATURES] = df[FEATURES].apply(pd.to_numeric, errors="raise").astype("float32")
    if not np.isfinite(df[FEATURES].to_numpy()).all():
        raise ValueError("non-finite features")
    return df


def split_by_time(df):
    times = np.sort(df[TIME_COL].unique())
    if len(times) < 100:
        raise ValueError("insufficient chronological history")
    val_start, test_start = int(times[int(len(times) * .6)]), int(times[int(len(times) * .8)])
    gap = PURGE_HOURS * HOUR
    train = df[df[TIME_COL] < val_start - gap].copy()
    val = df[(df[TIME_COL] >= val_start) & (df[TIME_COL] < test_start - gap)].copy()
    test = df[df[TIME_COL] >= test_start].copy()
    # Training tail for sigmoid calibration, with independent label windows.
    tt = np.sort(train[TIME_COL].unique())
    cal_start = int(tt[int(len(tt) * .8)])
    fit = train[train[TIME_COL] < cal_start - gap].copy()
    cal = train[train[TIME_COL] >= cal_start].copy()
    return fit, cal, val, test, {"validation_start_ms": val_start, "test_start_ms": test_start,
                               "calibration_start_ms": cal_start, "purge_hours": PURGE_HOURS,
                               "split": "60/20/20 timestamps; calibration is a purged training tail"}


def logit(probs):
    p = np.clip(probs, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p)).reshape(-1, 1)


def calibrated_probs(model, calibrator, X):
    return calibrator.predict_proba(logit(model.predict_proba(X)[:, 1]))[:, 1]


def alert_indices(df, probs, threshold):
    """Event-time portfolio: one open position per direction, 12h coin cooldown.

    All prospective alerts at a timestamp are ranked before fills. A position
    closes only when the simulation reaches its exit timestamp. Returns never
    influence admission or ranking. This caps highly correlated same-side alts.
    """
    times = df[TIME_COL].to_numpy(np.int64)
    exits = df["exit_ms"].to_numpy(np.int64)
    symbols = df["symbol"].astype(str).to_numpy()
    directions = df["direction"].astype(str).to_numpy()
    eligible = df["entry_eligible"].to_numpy(bool)
    order = np.lexsort((symbols, -probs, times))
    last, active, selected = {}, [], []
    for i in order:
        if not eligible[i] or probs[i] < threshold:
            continue
        ts, symbol, direction = int(times[i]), symbols[i], directions[i]
        active = [(s, d, t) for s, d, t in active if t > ts]
        if symbol in last and ts - last[symbol] < COOLDOWN_HOURS * HOUR:
            continue
        if any(s == symbol or d == direction for s, d, _ in active):
            continue
        selected.append(int(i))
        last[symbol] = ts
        active.append((symbol, direction, int(exits[i])))
    return np.array(selected, dtype=int)


def wilson_interval(wins, n):
    if not n:
        return None
    z, p = 1.96, wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [max(0, center - half), min(1, center + half)]


def metrics_for_indices(df, indices, cost=COST_PCT + FUNDING_RESERVE_PCT):
    if not len(indices):
        return {"alerts": 0, "precision": None, "net_return_sum_pct": 0., "profit_factor": None,
                "avg_net_return_pct": None, "max_closed_trade_drawdown_pct": None,
                "wilson_95_interval": None, "net_return_after_0_50pct_cost": None}
    selected = df.iloc[indices]
    y = selected[TARGET_COL].to_numpy(int)
    r = selected[RETURN_COL].to_numpy(float) - cost
    gains, losses = float(r[r > 0].sum()), float(-r[r < 0].sum())
    # Closed-trade equal-notional P&L, sorted by realization time (not entry).
    # It is explicitly NOT a compounded account or mark-to-market drawdown.
    realized = r[np.argsort(selected["exit_ms"].to_numpy(), kind="stable")]
    equity = np.r_[0., np.cumsum(realized)]
    drawdown = float((np.maximum.accumulate(equity) - equity).max())
    return {"alerts": len(indices), "clean_wins": int(y.sum()), "precision": float(y.mean()),
            "wilson_95_interval": wilson_interval(int(y.sum()), len(y)),
            "profit_factor": gains / losses if losses else (999. if gains else 0.),
            "avg_net_return_pct": float(r.mean()), "net_return_sum_pct": float(r.sum()),
            "median_net_return_pct": float(np.median(r)), "max_closed_trade_drawdown_pct": drawdown,
            "net_return_after_0_50pct_cost": float((selected[RETURN_COL].to_numpy(float) - .50).mean()),
            "ambiguous_losses": int(selected["ambiguous_target5_stop1_5_48h"].sum()),
            "mean_mae_first_15m_pct": float(selected["mae_first_15m_pct"].mean()),
            "mean_mae_first_60m_pct": float(selected["mae_first_60m_pct"].mean()),
            "avg_holding_hours": float(((selected["exit_ms"] - selected[TIME_COL]) / HOUR).mean())}


def qualifies(m):
    return bool(m["alerts"] >= MIN_ALERTS and m["precision"] is not None and m["precision"] >= MIN_PRECISION
                and m["avg_net_return_pct"] is not None and m["avg_net_return_pct"] > 0
                and m["profit_factor"] is not None and m["profit_factor"] > 1)


def choose_threshold(val, probs):
    table = []
    for threshold in np.arange(.35, .951, .01):
        indices = alert_indices(val, probs, float(threshold))
        table.append({"threshold": float(round(threshold, 2)), **metrics_for_indices(val, indices)})
    eligible = [r for r in table if qualifies(r)]
    # Maximize simulated realized profit among precision-qualified choices;
    # do not simply chase the smallest high-hit-rate sample.
    if not eligible:
        return 1.01, table  # explicit NO TRADE, never a diagnostic live threshold
    winner = max(eligible, key=lambda r: (r["net_return_sum_pct"], -r["max_closed_trade_drawdown_pct"], r["precision"]))
    return winner["threshold"], table


def candidate_models():
    from lightgbm import LGBMClassifier
    return {
        "hist_gradient_boosting": HistGradientBoostingClassifier(learning_rate=.05, max_iter=250,
             max_leaf_nodes=15, min_samples_leaf=80, l2_regularization=2., early_stopping=False, random_state=42),
        "lightgbm": LGBMClassifier(n_estimators=300, learning_rate=.04, num_leaves=15,
             min_child_samples=100, reg_lambda=2., n_jobs=2, verbosity=-1, random_state=42,
             deterministic=True, force_col_wise=True),
        "scaled_logistic": make_pipeline(StandardScaler(), LogisticRegression(C=.1, max_iter=1500, random_state=42)),
    }


def main():
    out = Path(os.getenv("ML_ARTIFACT_DIR", "_ml_artifact_v2"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / "historical_futures_dataset_v2.jsonl.gz"
    df = load_dataset(path)
    fit, cal, val, test, split = split_by_time(df)
    # Train on the same fixed entry opportunity distribution used for alerts.
    fit, cal = fit[fit["entry_eligible"] == 1], cal[cal["entry_eligible"] == 1]
    if min(len(fit), len(cal), len(val), len(test)) < 100 or fit[TARGET_COL].nunique() < 2 or cal[TARGET_COL].nunique() < 2:
        raise RuntimeError("insufficient class/history coverage for fitting/calibration")
    candidates, bundles = {}, {}
    for name, model in candidate_models().items():
        print(f"Training {name}: {len(fit)} fit, {len(cal)} calibration rows", flush=True)
        model.fit(fit[FEATURES], fit[TARGET_COL])
        calibrator = LogisticRegression(C=1., max_iter=1000, random_state=42)
        calibrator.fit(logit(model.predict_proba(cal[FEATURES])[:, 1]), cal[TARGET_COL])
        probs = calibrated_probs(model, calibrator, val[FEATURES])
        threshold, table = choose_threshold(val, probs)
        selected = metrics_for_indices(val, alert_indices(val, probs, threshold))
        candidates[name] = {"threshold": threshold, "validation": selected, "threshold_table": table,
                            "validation_brier": float(brier_score_loss(val[TARGET_COL], probs)),
                            "validation_average_precision": float(average_precision_score(val[TARGET_COL], probs))}
        bundles[name] = (model, calibrator)
        print(name, selected, flush=True)
    viable = [n for n, r in candidates.items() if qualifies(r["validation"])]
    winner = max(viable, key=lambda n: (candidates[n]["validation"]["net_return_sum_pct"],
                                      -candidates[n]["validation"]["max_closed_trade_drawdown_pct"])) if viable else None
    report = {"version": "swing-model-v2", "research_only": True, "generated_at_ms": int(time.time() * 1000),
              "prediction_target": "+5% before -1.5% within 48h at next executable open",
              "promotable_to_live_shadow": False, "live_alerts_enabled": False, "selected_model": winner,
              "requirements": {"minimum_validation_alerts": MIN_ALERTS, "minimum_test_alerts": MIN_ALERTS,
                               "minimum_precision": MIN_PRECISION, "positive_net_expected_return": True,
                               "profit_factor_above": 1., "round_trip_cost_pct": COST_PCT,
                               "funding_reserve_pct": FUNDING_RESERVE_PCT},
              "split": {**split, "fit_rows": len(fit), "calibration_rows": len(cal),
                        "validation_rows": len(val), "test_rows": len(test)},
              "candidate_validation_reports": candidates, "test_evaluations": 0,
              "limitations": ["60% is an acceptance target, not a promised success rate",
                              "Wilson interval treats trades as independent; residual dependence can reduce confidence",
                              "Closed-trade drawdown excludes unrealized losses and leverage",
                              "Funding allowance is not actual historical funding", "Forward paper trading required before live alerts",
                              "Candidate models are chosen using validation only; repeated holdout tuning invalidates the test"]}
    if winner:
        model, calibrator = bundles[winner]
        threshold = candidates[winner]["threshold"]
        # This is the first and ONLY model evaluated on the newest test period.
        probs = calibrated_probs(model, calibrator, test[FEATURES])
        indices = alert_indices(test, probs, threshold)
        m = metrics_for_indices(test, indices)
        report.update({"test_evaluations": 1, "test": m, "selected_threshold": threshold,
                       "test_brier": float(brier_score_loss(test[TARGET_COL], probs)),
                       "promotable_to_live_shadow": qualifies(m), "regime_test": {}, "direction_test": {}})
        for name, col in [("regime_test", "market_regime"), ("direction_test", "direction")]:
            values = test[col].astype(str).to_numpy()
            for value in sorted(set(values)):
                report[name][value] = metrics_for_indices(test, indices[values[indices] == value])
        periods = pd.to_datetime(test[TIME_COL], unit="ms", utc=True).dt.strftime("%Y-%m").to_numpy()
        report["monthly_test"] = {month: metrics_for_indices(test, indices[periods[indices] == month])
                                  for month in sorted(set(periods))}
        # Hash all data to prevent silently relabeling/reusing the same holdout.
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1_048_576), b""):
                h.update(block)
        report["dataset_sha256"] = h.hexdigest()
        selected_trades = test.iloc[indices].copy()
        selected_trades["probability"] = probs[indices]
        selected_trades.to_csv(out / "swing_model_v2_test_trades.csv", index=False)
        bundle = {"version": "swing-model-v2", "model": model, "calibrator": calibrator, "features": FEATURES,
                  "threshold": threshold, "primary_target": TARGET_COL, "target_pct": 5., "stop_pct": 1.5,
                  "horizon_hours": 48, "dataset_sha256": h.hexdigest(),
                  "promotable_to_live_shadow": qualifies(m), "live_enabled": False}
        with (out / "swing_model_v2.pkl").open("wb") as f:
            pickle.dump(bundle, f)
    else:
        report["rejection_reason"] = "No model/threshold met validation precision, sample count and net profitability requirements"
        # Remove stale bundles if this output directory is reused.
        (out / "swing_model_v2.pkl").unlink(missing_ok=True)
        (out / "swing_model_v2_test_trades.csv").unlink(missing_ok=True)
    (out / "swing_model_v2_report.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps({k: v for k, v in report.items() if k != "candidate_validation_reports"}, indent=2), flush=True)


if __name__ == "__main__":
    main()
