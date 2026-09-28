#!/usr/bin/env python3
"""
Historical ML Dataset Builder v1

Research-only pipeline for the swing scanner. It reconstructs candidate features
at historical timestamps using only information available at that time, then
adds forward 24h/48h path labels from 5-minute candles.

Important:
- No Telegram.
- No live-trading changes.
- No model training in this file.
- 15m candles are derived from 5m candles to avoid extra API/data cost.
- Same-5m-candle target/stop hits are marked AMBIGUOUS, never guessed.
"""

from __future__ import annotations

import bisect
import gzip
import json
import math
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ARTIFACT_DIR = Path(os.getenv("BACKFILL_ARTIFACT_DIR", "_backfill_artifact"))
OUT_DIR = Path(os.getenv("ML_ARTIFACT_DIR", "_ml_artifact"))

SAMPLE_MINUTES = int(os.getenv("ML_SAMPLE_MINUTES", "15"))
TOP_N = int(os.getenv("ML_TOP_N", "20"))
MIN_24H_QUOTE_VOLUME = float(os.getenv("ML_MIN_24H_QUOTE_VOLUME", "10000000"))
TARGET_PCT = float(os.getenv("ML_TARGET_PCT", "5"))
MAX_HORIZON_H = int(os.getenv("ML_MAX_HORIZON_H", "72"))
MAX_ROWS = int(os.getenv("ML_MAX_ROWS", "0"))

MS_5M = 5 * 60 * 1000
MS_15M = 15 * 60 * 1000
MS_1H = 60 * 60 * 1000
MS_4H = 4 * MS_1H

STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "PYUSD",
}


@dataclass
class Series:
    rows: list[dict]
    close_times: list[int]

    @classmethod
    def from_rows(cls, rows: list[dict]) -> "Series":
        rows = sorted(rows, key=lambda r: int(r["open_time_ms"]))
        return cls(rows=rows, close_times=[int(r["close_time_ms"]) for r in rows])

    def closed(self, asof_ms: int, limit: int | None = None) -> list[dict]:
        # Only candles with close_time < asof are usable.
        idx = bisect.bisect_left(self.close_times, int(asof_ms))
        start = 0 if limit is None else max(0, idx - limit)
        return self.rows[start:idx]

    def future(self, after_ms: int, until_ms: int) -> list[dict]:
        start = bisect.bisect_left(self.close_times, int(after_ms))
        end = bisect.bisect_left(self.close_times, int(until_ms))
        return self.rows[start:end]


def load_jsonl_gz(path: Path) -> list[dict]:
    rows: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: int(r["open_time_ms"]))
    return rows


def aggregate_15m(rows5: list[dict]) -> list[dict]:
    out: list[dict] = []
    bucket: list[dict] = []
    bucket_id = None

    def flush(items: list[dict]):
        if len(items) != 3:
            return
        items = sorted(items, key=lambda r: int(r["open_time_ms"]))
        # Reject gaps. Three contiguous 5m bars are required.
        if any(
            int(items[i + 1]["open_time_ms"]) - int(items[i]["open_time_ms"]) != MS_5M
            for i in range(2)
        ):
            return
        qv = sum(float(x.get("quote_volume", 0.0)) for x in items)
        tbq = sum(float(x.get("taker_buy_quote_volume", 0.0)) for x in items)
        out.append(
            {
                "open_time_ms": int(items[0]["open_time_ms"]),
                "open": float(items[0]["open"]),
                "high": max(float(x["high"]) for x in items),
                "low": min(float(x["low"]) for x in items),
                "close": float(items[-1]["close"]),
                "volume": sum(float(x.get("volume", 0.0)) for x in items),
                "close_time_ms": int(items[-1]["close_time_ms"]),
                "quote_volume": qv,
                "trades": sum(int(x.get("trades", 0)) for x in items),
                "taker_buy_quote_volume": tbq,
            }
        )

    for row in rows5:
        bid = int(row["open_time_ms"]) // MS_15M
        if bucket_id is None:
            bucket_id = bid
        if bid != bucket_id:
            flush(bucket)
            bucket = []
            bucket_id = bid
        bucket.append(row)
    flush(bucket)
    return out


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0 if b else 0.0


def sma(vals: list[float], n: int) -> float:
    if len(vals) < n:
        raise ValueError("insufficient SMA history")
    return sum(vals[-n:]) / n


def atr_pct(rows: list[dict], n: int = 14) -> float:
    if len(rows) < n + 1:
        return 0.0
    trs = []
    for i in range(-n, 0):
        cur = rows[i]
        prev = rows[i - 1]
        h = float(cur["high"])
        l = float(cur["low"])
        pc = float(prev["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    close = float(rows[-1]["close"])
    return safe_div(sum(trs) / len(trs), close) * 100.0


def realized_vol_pct(closes: list[float], n: int = 12) -> float:
    if len(closes) < n + 1:
        return 0.0
    rets = []
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 2:
        return 0.0
    return statistics.pstdev(rets) * math.sqrt(len(rets)) * 100.0


def rolling_24h_snapshot(rows5: list[dict]) -> tuple[float, float, float]:
    # Approximate rolling ticker values using the last 288 completed 5m bars.
    if len(rows5) < 289:
        raise ValueError("need >=289 completed 5m bars")
    window = rows5[-288:]
    qv = sum(float(x.get("quote_volume", 0.0)) for x in window)
    last = float(window[-1]["close"])
    before = float(rows5[-289]["close"])
    chg = pct(last, before)
    buy_q = sum(float(x.get("taker_buy_quote_volume", 0.0)) for x in window)
    buy_ratio = safe_div(buy_q, qv, 0.5)
    return qv, chg, buy_ratio


def activity_score(quote_volume_24h: float, price_change_24h: float) -> float:
    score = min(quote_volume_24h / 25_000_000.0, 20.0) + min(abs(price_change_24h), 6.0) * 2.0
    if abs(price_change_24h) >= 12.0:
        score -= 10.0
    return score


def classify_regime(rows: list[dict]) -> tuple[str, int]:
    if not rows:
        return "MIXED", 0
    changes = [float(r["price_change_24h_pct"]) for r in rows]
    adv = sum(1 for x in changes if x > 0)
    breadth = 100.0 * adv / len(changes)
    med = statistics.median(changes)
    btc = next((float(r["price_change_24h_pct"]) for r in rows if r["symbol"] == "BTCUSDT"), 0.0)
    eth = next((float(r["price_change_24h_pct"]) for r in rows if r["symbol"] == "ETHUSDT"), 0.0)

    points = 0
    points += 1 if breadth >= 55 else (-1 if breadth <= 45 else 0)
    points += 1 if med > 0 else (-1 if med < 0 else 0)
    points += 1 if btc > 0 else (-1 if btc < 0 else 0)
    points += 1 if eth > 0 else (-1 if eth < 0 else 0)
    if points >= 3:
        return "RISK_ON", points
    if points <= -3:
        return "RISK_OFF", points
    return "MIXED", points


def market_context(rows: list[dict]) -> dict:
    changes = [float(r["price_change_24h_pct"]) for r in rows]
    adv = sum(1 for x in changes if x > 0)
    dec = sum(1 for x in changes if x < 0)
    breadth = safe_div(adv, max(1, adv + dec)) * 100.0
    med = statistics.median(changes) if changes else 0.0
    btc = next((float(r["price_change_24h_pct"]) for r in rows if r["symbol"] == "BTCUSDT"), 0.0)
    eth = next((float(r["price_change_24h_pct"]) for r in rows if r["symbol"] == "ETHUSDT"), 0.0)
    regime, regime_score = classify_regime(rows)
    return {
        "regime": regime,
        "regime_score": regime_score,
        "breadth_pct": breadth,
        "median_change_24h_pct": med,
        "btc_change_24h_pct": btc,
        "eth_change_24h_pct": eth,
        "liquid_symbol_count": len(rows),
    }


def directional_features(
    h1: list[dict],
    h4: list[dict],
    m15: list[dict],
    rows5: list[dict],
    universe_row: dict,
    ctx: dict,
) -> dict | None:
    if len(h1) < 30 or len(h4) < 25 or len(m15) < 20 or len(rows5) < 289:
        return None

    h1c = [float(x["close"]) for x in h1]
    h1h = [float(x["high"]) for x in h1]
    h1l = [float(x["low"]) for x in h1]
    h1q = [float(x.get("quote_volume", 0.0)) for x in h1]
    h4c = [float(x["close"]) for x in h4]
    m15c = [float(x["close"]) for x in m15]
    m15q = [float(x.get("quote_volume", 0.0)) for x in m15]

    price = h1c[-1]
    s1_10 = sma(h1c, 10)
    s1_20 = sma(h1c, 20)
    old_s1_10 = sum(h1c[-15:-5]) / 10.0
    s4_10 = sma(h4c, 10)
    s4_20 = sma(h4c, 20)
    old_s4_10 = sum(h4c[-15:-5]) / 10.0

    long_structure = (
        price > s1_10 > s1_20
        and h4c[-1] > s4_10 > s4_20
        and s1_10 > old_s1_10
        and s4_10 > old_s4_10
    )
    short_structure = (
        price < s1_10 < s1_20
        and h4c[-1] < s4_10 < s4_20
        and s1_10 < old_s1_10
        and s4_10 < old_s4_10
    )
    if not long_structure and not short_structure:
        return None
    direction = "LONG" if long_structure else "SHORT"
    sign = 1.0 if direction == "LONG" else -1.0

    recent_12h_high = max(h1h[-12:])
    recent_12h_low = min(h1l[-12:])
    pullback = pct(price, recent_12h_high)
    bounce = pct(price, recent_12h_low)
    dist1 = pct(price, s1_10)
    dist4 = pct(h4c[-1], s4_10)
    m3 = pct(price, h1c[-4])
    m12 = pct(price, h1c[-13])
    m24 = pct(h4c[-1], h4c[-7])

    recent_q = sum(h1q[-3:]) / 3.0
    previous_q = sum(h1q[-15:-3]) / 12.0
    vexp1h = safe_div(recent_q, previous_q)

    m15_sma4 = sma(m15c, 4)
    m15_sma12 = sma(m15c, 12)
    m15_mom15 = pct(m15c[-1], m15c[-2])
    m15_mom45 = pct(m15c[-1], m15c[-4])
    m15_mom180 = pct(m15c[-1], m15c[-13])
    m15_recent_q = sum(m15q[-3:]) / 3.0
    m15_prev_q = sum(m15q[-15:-3]) / 12.0
    vexp15 = safe_div(m15_recent_q, m15_prev_q)

    last_12_5m = rows5[-12:]
    q1h = sum(float(x.get("quote_volume", 0.0)) for x in last_12_5m)
    buy1h = sum(float(x.get("taker_buy_quote_volume", 0.0)) for x in last_12_5m)
    buy_ratio_1h = safe_div(buy1h, q1h, 0.5)

    if direction == "LONG":
        retest_ready = -4.0 <= pullback <= -0.75 and -1.5 <= dist1 <= 1.0
        trigger_like = retest_ready and price > h1c[-2] and price >= s1_10 and vexp1h >= 0.9
        confirmation_15m = m15c[-1] > m15c[-2] and m15c[-1] >= m15_sma4 and vexp15 >= 0.9
    else:
        retest_ready = 0.75 <= bounce <= 4.0 and -1.0 <= dist1 <= 1.5
        trigger_like = retest_ready and price < h1c[-2] and price <= s1_10 and vexp1h >= 0.9
        confirmation_15m = m15c[-1] < m15c[-2] and m15c[-1] <= m15_sma4 and vexp15 >= 0.9

    btc = float(ctx["btc_change_24h_pct"])
    eth = float(ctx["eth_change_24h_pct"])
    chg24 = float(universe_row["price_change_24h_pct"])

    return {
        "direction": direction,
        "direction_sign": sign,
        "entry_price": float(rows5[-1]["close"]),
        "activity_score": float(universe_row["activity_score"]),
        "activity_rank": int(universe_row["activity_rank"]),
        "quote_volume_24h": float(universe_row["quote_volume_24h"]),
        "price_change_24h_pct": chg24,
        "taker_buy_ratio_24h": float(universe_row["taker_buy_ratio_24h"]),
        "taker_buy_ratio_1h": buy_ratio_1h,
        "momentum_3h_pct": m3,
        "momentum_12h_pct": m12,
        "momentum_24h_pct": m24,
        "directional_momentum_3h": sign * m3,
        "directional_momentum_12h": sign * m12,
        "directional_momentum_24h": sign * m24,
        "pullback_from_12h_high_pct": pullback,
        "bounce_from_12h_low_pct": bounce,
        "distance_from_1h_sma10_pct": dist1,
        "directional_distance_from_1h_sma10_pct": sign * dist1,
        "distance_from_4h_sma10_pct": dist4,
        "directional_distance_from_4h_sma10_pct": sign * dist4,
        "sma1h10_slope_pct": pct(s1_10, old_s1_10),
        "sma4h10_slope_pct": pct(s4_10, old_s4_10),
        "volume_expansion_1h": vexp1h,
        "momentum_15m_pct": m15_mom15,
        "momentum_45m_pct": m15_mom45,
        "momentum_180m_pct": m15_mom180,
        "directional_momentum_15m": sign * m15_mom15,
        "directional_momentum_45m": sign * m15_mom45,
        "directional_momentum_180m": sign * m15_mom180,
        "distance_from_15m_sma4_pct": pct(m15c[-1], m15_sma4),
        "distance_from_15m_sma12_pct": pct(m15c[-1], m15_sma12),
        "volume_expansion_15m": vexp15,
        "atr_1h_pct": atr_pct(h1, 14),
        "realized_vol_12h_pct": realized_vol_pct(h1c, 12),
        "relative_strength_vs_btc_24h": sign * (chg24 - btc),
        "relative_strength_vs_eth_24h": sign * (chg24 - eth),
        "retest_ready": int(retest_ready),
        "trigger_like_1h": int(trigger_like),
        "confirmation_15m": int(confirmation_15m),
        "anti_chase_abs_12h_pct": abs(m12),
    }


def _path_label(
    direction: str,
    entry: float,
    future5: list[dict],
    target_pct: float,
    stop_pct: float,
    horizon_h: int,
) -> dict:
    sign = 1.0 if direction == "LONG" else -1.0
    target = entry * (1.0 + sign * target_pct / 100.0)
    stop = entry * (1.0 - sign * stop_pct / 100.0)
    if not future5:
        return {
            "eventual_target": 0,
            "path": "OPEN",
            "target_before_stop": 0,
            "ambiguous": 0,
            "time_to_target_min": None,
            "mae_before_target_pct": None,
            "close_return_pct": 0.0,
            "sim_return_pct": 0.0,
        }

    start_close = int(future5[0]["close_time_ms"])
    horizon_ms = horizon_h * MS_1H
    path = "OPEN"
    eventual = False
    first_target_ms = None
    worst_before_target = 0.0
    last_close = entry

    for bar in future5:
        close_ms = int(bar["close_time_ms"])
        elapsed = close_ms - start_close + MS_5M
        if elapsed > horizon_ms:
            break
        hi = float(bar["high"])
        lo = float(bar["low"])
        last_close = float(bar["close"])
        if direction == "LONG":
            adv = pct(lo, entry)
            hit_target = hi >= target
            hit_stop = lo <= stop
        else:
            adv = -pct(hi, entry)
            hit_target = lo <= target
            hit_stop = hi >= stop

        eventual = eventual or hit_target
        if path == "OPEN":
            worst_before_target = min(worst_before_target, adv)
            if hit_target and hit_stop:
                path = "AMBIGUOUS"
                first_target_ms = close_ms
            elif hit_target:
                path = "TARGET_FIRST"
                first_target_ms = close_ms
            elif hit_stop:
                path = "STOP_FIRST"

    close_ret = sign * pct(last_close, entry)
    if path == "TARGET_FIRST":
        sim_ret = target_pct
    elif path in {"STOP_FIRST", "AMBIGUOUS"}:
        sim_ret = -stop_pct
    else:
        sim_ret = close_ret

    return {
        "eventual_target": int(eventual),
        "path": path,
        "target_before_stop": int(path == "TARGET_FIRST"),
        "ambiguous": int(path == "AMBIGUOUS"),
        "time_to_target_min": (
            (first_target_ms - start_close + MS_5M) / 60000.0
            if first_target_ms is not None else None
        ),
        "mae_before_target_pct": worst_before_target if first_target_ms is not None else None,
        "close_return_pct": close_ret,
        "sim_return_pct": sim_ret,
    }


def forward_labels(direction: str, entry: float, future5: list[dict]) -> dict:
    # Canonical path labels match the locked swing objective:
    # +3 before -1, +5 before -1.5, +10 before -2, at 24/48/72h.
    targets = ((3.0, 1.0), (5.0, 1.5), (10.0, 2.0))
    horizons = (24, 48, 72)
    out: dict[str, Any] = {}

    # MFE/MAE are retained independently from target/stop path labels so that
    # volatile eventual winners can be distinguished from clean winners.
    if future5:
        start_close = int(future5[0]["close_time_ms"])
        for horizon_h in horizons:
            mfe = 0.0
            mae = 0.0
            sign = 1.0 if direction == "LONG" else -1.0
            for bar in future5:
                elapsed = int(bar["close_time_ms"]) - start_close + MS_5M
                if elapsed > horizon_h * MS_1H:
                    break
                hi = float(bar["high"])
                lo = float(bar["low"])
                if direction == "LONG":
                    fav = pct(hi, entry)
                    adv = pct(lo, entry)
                else:
                    fav = -pct(lo, entry)
                    adv = -pct(hi, entry)
                mfe = max(mfe, fav)
                mae = min(mae, adv)
            out[f"mfe_{horizon_h}h_pct"] = mfe
            out[f"mae_{horizon_h}h_pct"] = mae
    else:
        for horizon_h in horizons:
            out[f"mfe_{horizon_h}h_pct"] = 0.0
            out[f"mae_{horizon_h}h_pct"] = 0.0

    for target_pct, stop_pct in targets:
        target_tag = str(int(target_pct))
        stop_tag = str(stop_pct).replace(".", "_")
        for horizon_h in horizons:
            r = _path_label(direction, entry, future5, target_pct, stop_pct, horizon_h)
            prefix = f"target{target_tag}_stop{stop_tag}_{horizon_h}h"
            out[f"eventual_target{target_tag}_{horizon_h}h"] = r["eventual_target"]
            out[f"path_{prefix}"] = r["path"]
            out[f"target{target_tag}_before_stop{stop_tag}_{horizon_h}h"] = r["target_before_stop"]
            out[f"ambiguous_{prefix}"] = r["ambiguous"]
            out[f"time_to_target{target_tag}_{horizon_h}h_min"] = r["time_to_target_min"]
            out[f"mae_before_target{target_tag}_{horizon_h}h_pct"] = r["mae_before_target_pct"]
            out[f"close_return_{horizon_h}h_pct"] = r["close_return_pct"]
            out[f"sim_return_target{target_tag}_stop{stop_tag}_{horizon_h}h_pct"] = r["sim_return_pct"]

    # Convenience clean/volatile labels for the primary +5% / 48h objective.
    out["clean_target5_48h"] = out["target5_before_stop1_5_48h"]
    out["volatile_target5_48h"] = int(
        out["eventual_target5_48h"]
        and not out["target5_before_stop1_5_48h"]
        and out["path_target5_stop1_5_48h"] in {"STOP_FIRST", "AMBIGUOUS"}
    )
    return out


def find_symbol_file(symbol_dir: Path, interval: str, days: int) -> Path | None:
    exact = symbol_dir / f"{interval}_{days}d.jsonl.gz"
    if exact.exists():
        return exact
    matches = sorted(symbol_dir.glob(f"{interval}_*d.jsonl.gz"))
    return matches[-1] if matches else None


def main():
    manifest_path = ARTIFACT_DIR / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    days = int(manifest.get("days") or 0)
    symbols = list(manifest.get("symbols") or [])
    if not symbols:
        raise RuntimeError("manifest symbols missing")

    data: dict[str, dict[str, Series]] = {}
    skipped: dict[str, str] = {}

    for symbol in symbols:
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        if base in STABLE_BASES:
            skipped[symbol] = "stable base"
            continue
        sdir = ARTIFACT_DIR / symbol
        p5 = find_symbol_file(sdir, "5m", days)
        p1 = find_symbol_file(sdir, "1h", days)
        p4 = find_symbol_file(sdir, "4h", days)
        if not p5 or not p1 or not p4:
            skipped[symbol] = "missing interval file"
            continue
        try:
            r5 = load_jsonl_gz(p5)
            r1 = load_jsonl_gz(p1)
            r4 = load_jsonl_gz(p4)
            r15 = aggregate_15m(r5)
            if len(r5) < 600 or len(r1) < 40 or len(r4) < 25 or len(r15) < 100:
                skipped[symbol] = "insufficient history"
                continue
            data[symbol] = {
                "5m": Series.from_rows(r5),
                "15m": Series.from_rows(r15),
                "1h": Series.from_rows(r1),
                "4h": Series.from_rows(r4),
            }
        except Exception as exc:
            skipped[symbol] = f"load error: {exc}"

    if len(data) < 5:
        raise RuntimeError(f"too few usable symbols: {len(data)}")

    start_candidates = []
    end_candidates = []
    for d in data.values():
        start_candidates.append(int(d["5m"].rows[0]["close_time_ms"]) + 30 * MS_4H)
        end_candidates.append(int(d["5m"].rows[-1]["close_time_ms"]) - MAX_HORIZON_H * MS_1H)
    start_ms = max(start_candidates)
    end_ms = min(end_candidates)
    if end_ms <= start_ms:
        raise RuntimeError("no common historical window after warmup/horizon")

    step_ms = SAMPLE_MINUTES * 60 * 1000
    # Align sample timestamps to the next 15m boundary + 1ms so the just-closed bar is usable.
    ts = ((start_ms // step_ms) + 1) * step_ms + 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dataset_path = OUT_DIR / "historical_ml_dataset_v1.jsonl.gz"
    meta_path = OUT_DIR / "historical_ml_dataset_v1_meta.json"

    row_count = 0
    timestamp_count = 0
    candidate_counts = []
    label_positive = 0
    ambiguous_count = 0
    regime_counts = {"RISK_ON": 0, "MIXED": 0, "RISK_OFF": 0}

    with gzip.open(dataset_path, "wt", encoding="utf-8") as out:
        while ts <= end_ms:
            universe: list[dict] = []
            closed_cache: dict[str, dict[str, list[dict]]] = {}

            for symbol, series in data.items():
                r5 = series["5m"].closed(ts, 400)
                if len(r5) < 289:
                    continue
                try:
                    qv24, chg24, buy_ratio24 = rolling_24h_snapshot(r5)
                except ValueError:
                    continue
                if qv24 < MIN_24H_QUOTE_VOLUME:
                    continue
                row = {
                    "symbol": symbol,
                    "quote_volume_24h": qv24,
                    "price_change_24h_pct": chg24,
                    "taker_buy_ratio_24h": buy_ratio24,
                }
                row["activity_score"] = activity_score(qv24, chg24)
                universe.append(row)
                closed_cache[symbol] = {"5m": r5}

            universe.sort(key=lambda x: x["activity_score"], reverse=True)
            for i, row in enumerate(universe, start=1):
                row["activity_rank"] = i
            ctx = market_context(universe)
            regime_counts[ctx["regime"]] = regime_counts.get(ctx["regime"], 0) + 1

            selected = universe[:TOP_N]
            emitted_here = 0
            for u in selected:
                symbol = u["symbol"]
                series = data[symbol]
                r5 = closed_cache[symbol]["5m"]
                h1 = series["1h"].closed(ts, 80)
                h4 = series["4h"].closed(ts, 45)
                m15 = series["15m"].closed(ts, 80)
                feat = directional_features(h1, h4, m15, r5, u, ctx)
                if feat is None:
                    continue

                future_end = ts + MAX_HORIZON_H * MS_1H
                future5 = series["5m"].future(ts, future_end + 1)
                if not future5:
                    continue
                labels = forward_labels(feat["direction"], feat["entry_price"], future5)

                row = {
                    "asof_ms": int(ts),
                    "symbol": symbol,
                    "market_regime": ctx["regime"],
                    "market_regime_score": ctx["regime_score"],
                    "market_breadth_pct": ctx["breadth_pct"],
                    "market_median_change_24h_pct": ctx["median_change_24h_pct"],
                    "btc_change_24h_pct": ctx["btc_change_24h_pct"],
                    "eth_change_24h_pct": ctx["eth_change_24h_pct"],
                    "liquid_symbol_count": ctx["liquid_symbol_count"],
                    **feat,
                    **labels,
                }
                out.write(json.dumps(row, sort_keys=True) + "\n")
                row_count += 1
                emitted_here += 1
                label_positive += int(labels["target5_before_stop1_5_48h"])
                ambiguous_count += int(labels["ambiguous_target5_stop1_5_48h"])

                if MAX_ROWS and row_count >= MAX_ROWS:
                    break

            timestamp_count += 1
            candidate_counts.append(emitted_here)
            if MAX_ROWS and row_count >= MAX_ROWS:
                break
            ts += step_ms

    meta = {
        "version": "historical-ml-dataset-v1",
        "generated_at_ms": int(time.time() * 1000),
        "source_days": days,
        "symbols_requested": symbols,
        "symbols_usable": sorted(data.keys()),
        "symbols_skipped": skipped,
        "sample_minutes": SAMPLE_MINUTES,
        "top_n_activity": TOP_N,
        "min_24h_quote_volume": MIN_24H_QUOTE_VOLUME,
        "target_pct": TARGET_PCT,
        "canonical_path_labels": ["+3 before -1", "+5 before -1.5", "+10 before -2"],
        "max_horizon_h": MAX_HORIZON_H,
        "row_count": row_count,
        "timestamp_count": timestamp_count,
        "positive_clean_target5_before_stop1_5_48h": label_positive,
        "positive_rate": safe_div(label_positive, max(1, row_count)),
        "ambiguous_count": ambiguous_count,
        "mean_directional_candidates_per_timestamp": safe_div(sum(candidate_counts), max(1, len(candidate_counts))),
        "regime_timestamp_counts": regime_counts,
        "anti_leakage": [
            "features use only candles with close_time_ms < asof_ms",
            "forward labels begin at/after asof_ms and never feed feature computation",
            "15m candles are aggregated only from complete contiguous 5m bars",
            "same 5m target+stop hit is AMBIGUOUS and treated conservatively",
            "eventual-target labels are retained separately from target-first path labels",
        ],
    }
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    print(json.dumps(meta, indent=2, sort_keys=True))
    if row_count < 100:
        raise SystemExit("dataset too small; refusing to continue")


if __name__ == "__main__":
    main()
