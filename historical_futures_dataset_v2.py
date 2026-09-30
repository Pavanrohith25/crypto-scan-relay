#!/usr/bin/env python3
"""Point-in-time futures features and executable, path-aware entry labels.

Numeric mmap candles bound memory; 24h snapshots use prefix sums. Each coin has
its own history window. Gaps invalidate affected features/outcomes, not other
coins' entire histories. Only the next bar OPEN is used as simulated entry.
"""
from __future__ import annotations
import gzip
import json
import os
from pathlib import Path

import numpy as np

from historical_ml_dataset_v1 import directional_features, activity_score, market_context
from historical_futures_backfill_v2 import MS_5M

HOUR = 3_600_000


class ArraySeries:
    def __init__(self, path: Path, step: int):
        self.a = np.load(path, mmap_mode="r", allow_pickle=False)
        self.step = step
        self.names = self.a.dtype.names
        self.qsum = np.r_[0., np.cumsum(self.a["quote_volume"])]
        self.bsum = np.r_[0., np.cumsum(self.a["taker_buy_quote_volume"])]

    def index(self, ts):
        return int(np.searchsorted(self.a["close_time_ms"], ts, side="left"))

    def contiguous(self, lo, hi):
        return hi > lo and int(self.a[hi - 1]["open_time_ms"] - self.a[lo]["open_time_ms"]) == (hi - lo - 1) * self.step

    def closed(self, ts: int, limit: int) -> list[dict]:
        hi = self.index(ts)
        lo = max(0, hi - limit)
        if not self.contiguous(lo, hi) or hi == 0 or ts - int(self.a[hi - 1]["close_time_ms"]) > self.step:
            return []
        return [dict(zip(self.names, row.tolist())) for row in self.a[lo:hi]]

    def snapshot(self, ts):
        hi = self.index(ts)
        if hi < 289 or not self.contiguous(hi - 289, hi) or ts - int(self.a[hi - 1]["close_time_ms"]) > MS_5M:
            return None
        q = float(self.qsum[hi] - self.qsum[hi - 288])
        buy = float(self.bsum[hi] - self.bsum[hi - 288])
        change = (float(self.a[hi - 1]["close"]) / float(self.a[hi - 289]["close"]) - 1) * 100
        return q, change, buy / q if q else .5

    def forward(self, ts, hours=48):
        lo = int(np.searchsorted(self.a["open_time_ms"], ts, side="left"))
        n = hours * 12
        hi = lo + n
        if hi > len(self.a) or not self.contiguous(lo, hi):
            return None
        if int(self.a[lo]["open_time_ms"]) != ts:
            return None
        return self.a[lo:hi]


def path_labels(direction: str, bars: np.ndarray) -> dict:
    """Label +5% before stop at 48h, retaining ambiguity as a loss.

    The entry is next executable open. Gaps at entry and future bars are never
    disguised as fills at the prior close or at a skipped stop price.
    """
    entry = float(bars[0]["open"])
    sign = 1 if direction == "LONG" else -1
    fav = sign * ((bars["high"] if sign == 1 else bars["low"]) / entry - 1) * 100
    adv = sign * ((bars["low"] if sign == 1 else bars["high"]) / entry - 1) * 100
    target_indices = np.flatnonzero(fav >= 5 - 1e-10)
    target_i = int(target_indices[0]) if len(target_indices) else len(bars)
    close_ret = sign * (float(bars[-1]["close"]) / entry - 1) * 100
    out = {"entry_price": entry, "entry_ms": int(bars[0]["open_time_ms"]),
           "mfe_48h_pct": float(max(0, fav.max())), "mae_48h_pct": float(min(0, adv.min())),
           "eventual_target5_48h": int(target_i < len(bars))}
    for mins in [15, 30, 60]:
        out[f"mae_first_{mins}m_pct"] = float(min(0, adv[:mins // 5].min()))
    for stop, tag in [(1., "1_0"), (1.5, "1_5"), (2.5, "2_5"), (3., "3_0")]:
        stop_indices = np.flatnonzero(adv <= -stop + 1e-10)
        stop_i = int(stop_indices[0]) if len(stop_indices) else len(bars)
        if target_i == stop_i and target_i < len(bars):
            path, ret, exit_i = "AMBIGUOUS", -stop, stop_i
        elif target_i < stop_i:
            path, ret, exit_i = "TARGET_FIRST", 5., target_i
        elif stop_i < len(bars):
            path, ret, exit_i = "STOP_FIRST", -stop, stop_i
        else:
            path, ret, exit_i = "TIMEOUT", close_ret, len(bars) - 1
        # If a bar opens through the stop, loss is the worse executable open.
        # For target gaps use only the target price (conservative).
        if path in {"STOP_FIRST", "AMBIGUOUS"}:
            ret = min(ret, sign * (float(bars[exit_i]["open"]) / entry - 1) * 100)
        out[f"target5_before_stop{tag}_48h"] = int(path == "TARGET_FIRST")
        if tag == "1_5":
            out.update({"path_target5_stop1_5_48h": path,
                        "ambiguous_target5_stop1_5_48h": int(path == "AMBIGUOUS"),
                        "sim_return_target5_stop1_5_48h_pct": float(ret),
                        "exit_ms": int(bars[exit_i]["close_time_ms"]) + 1,
                        "mae_before_exit_pct": float(min(0, adv[:exit_i + 1].min()))})
    return out


def entry_gate(row: dict) -> bool:
    """Fixed before model fitting; same eligibility for train/validation/test."""
    compatible = not ((row["direction"] == "SHORT" and row["market_regime"] == "RISK_ON") or
                      (row["direction"] == "LONG" and row["market_regime"] == "RISK_OFF"))
    return bool(compatible and row["retest_ready"] and row["confirmation_15m"]
                and row["anti_chase_abs_12h_pct"] <= 8
                and abs(row["distance_from_15m_sma4_pct"]) <= .75)


def main():
    raw = Path(os.getenv("ML_RAW_DIR", "_futures_raw_v2"))
    out = Path(os.getenv("ML_ARTIFACT_DIR", "_ml_artifact_v2"))
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((raw / "manifest.json").read_text())
    if manifest["status"] != "PASS":
        raise RuntimeError("raw coverage audit failed")
    data = {symbol: {interval: ArraySeries(raw / symbol / (interval + ".npy"), step)
                     for interval, step in [("5m", MS_5M), ("15m", 900_000), ("1h", HOUR), ("4h", 4 * HOUR)]}
            for symbol in manifest["eligible_symbols"]}
    start = int(manifest["start_ms"]) + 120 * HOUR
    end = int(manifest["end_exclusive_ms"]) - 48 * HOUR
    step = int(os.getenv("ML_SAMPLE_MINUTES", "15")) * 60_000
    if step != 900_000:
        raise ValueError("v2 requires a 15-minute decision cadence")
    start = (start + step - 1) // step * step
    top_n = int(os.getenv("ML_TOP_N", "30"))
    min_vol = float(os.getenv("ML_MIN_24H_QUOTE_VOLUME", "10000000"))
    count, positive, eligible = 0, 0, 0
    regimes, symbols_emitted = {}, set()
    with gzip.open(out / "historical_futures_dataset_v2.jsonl.gz", "wt", compresslevel=3) as f:
        for num, ts in enumerate(range(start, end, step)):
            universe, context_rows = [], []
            for symbol, series in data.items():
                snap = series["5m"].snapshot(ts)
                if snap is None:
                    continue
                qv, chg, buy = snap
                row = {"symbol": symbol, "quote_volume_24h": qv,
                       "price_change_24h_pct": chg, "taker_buy_ratio_24h": buy,
                       "activity_score": activity_score(qv, chg)}
                if qv >= min_vol:
                    universe.append(row)
                    context_rows.append(row)
                elif symbol in {"BTCUSDT", "ETHUSDT"}:
                    context_rows.append(row)
            if not {"BTCUSDT", "ETHUSDT"}.issubset({r["symbol"] for r in context_rows}):
                continue
            ctx = market_context(context_rows)
            universe.sort(key=lambda r: (-r["activity_score"], r["symbol"]))
            for rank, u in enumerate(universe[:top_n], 1):
                u["activity_rank"] = rank
                series = data[u["symbol"]]
                # Load only the selected candidates' small feature windows.
                feat = directional_features(series["1h"].closed(ts, 80), series["4h"].closed(ts, 45),
                                            series["15m"].closed(ts, 80), series["5m"].closed(ts, 289), u, ctx)
                if feat is None:
                    continue
                future = series["5m"].forward(ts)
                if future is None:
                    continue
                labels = path_labels(feat["direction"], future)
                # Prior close is detection price; next OPEN is execution price.
                row = {"asof_ms": ts, "symbol": u["symbol"], "market_regime": ctx["regime"],
                       "market_regime_score": ctx["regime_score"], "market_breadth_pct": ctx["breadth_pct"],
                       "market_median_change_24h_pct": ctx["median_change_24h_pct"],
                       "btc_change_24h_pct": ctx["btc_change_24h_pct"], "eth_change_24h_pct": ctx["eth_change_24h_pct"],
                       "liquid_symbol_count": len(universe), **feat,
                       "detection_price": feat["entry_price"], **labels}
                row["entry_eligible"] = int(entry_gate(row))
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
                count += 1
                positive += row["target5_before_stop1_5_48h"]
                eligible += row["entry_eligible"]
                regimes[ctx["regime"]] = regimes.get(ctx["regime"], 0) + 1
                symbols_emitted.add(u["symbol"])
            if num % 192 == 0:
                print(f"Dataset {num // 96} days: {count} rows, {eligible} entry-eligible", flush=True)
    meta = {"version": "futures-dataset-v2", "source_days": manifest["days"], "market": manifest["market"],
            "symbols_usable": manifest["eligible_symbols"], "symbols_with_candidates": sorted(symbols_emitted),
            "row_count": count, "entry_eligible_rows": eligible, "clean_winner_rows": positive,
            "regime_counts": regimes, "start_ms": start, "end_ms": end, "top_n": top_n,
            "universe": manifest["discovery"], "primary_label": "+5% before -1.5% within 48h",
            "feature_columns_exclude": ["entry_price", "detection_price", "exit_ms", "all future label/path columns"],
            "execution": "next 5m OPEN after decision; barrier ambiguity loses; gaps excluded; gap-through-stop priced at worse open",
            "limitations": ["Fixed sample of archived contracts, not entire historical exchange universe",
                            "No historical OI/liquidations/on-chain observations are fabricated",
                            "Funding is reserved as a cost, not reconstructed actual funding",
                            "This fixed 15m confirmation gate is not a validated live two-scan retest state machine"]}
    (out / "historical_futures_dataset_v2_meta.json").write_text(json.dumps(meta, indent=2))
    if eligible < 100:
        raise RuntimeError("too few eligible rows")


if __name__ == "__main__":
    main()
