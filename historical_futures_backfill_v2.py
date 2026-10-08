#!/usr/bin/env python3
"""Checksum-verified USD-M perpetual archive backfill. No exchange credentials.

Discover archived contracts (including delisted ones), freeze a deterministic
200-contract universe from archives present before the evaluation period, then
retain each contract's actual listing/gap coverage. Missing archives are logged,
never filled. Store numeric arrays for a memory-bounded dataset builder.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import numpy as np

ARCHIVE = "https://data.binance.vision"
BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
PREFIX = "data/futures/um/monthly/klines/"
DTYPE = np.dtype([(k, "i8" if k in {"open_time_ms", "close_time_ms", "trades"} else "f8") for k in (
    "open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms",
    "quote_volume", "trades", "taker_buy_base_volume", "taker_buy_quote_volume")])
MS_5M = 300_000
STABLES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE", "USD1", "PYUSD"}


def fetch(url: str, missing_ok=False) -> bytes | None:
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={"User-Agent": "swing-research-v2"}), timeout=45) as r:
                return r.read()
        except HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return None
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            time.sleep(min(2 ** attempt, 8))
        except (TimeoutError, OSError):
            if attempt == 3:
                raise
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("retry exhausted")


def list_bucket(prefix: str, delimiter: str = "") -> list[str]:
    result, marker = [], ""
    ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    while True:
        q = {"prefix": prefix, "max-keys": 1000}
        if delimiter:
            q["delimiter"] = delimiter
        if marker:
            q["marker"] = marker
        root = ET.fromstring(fetch(BUCKET + "?" + urlencode(q)))
        keys = [x.text for x in root.findall("s:Contents/s:Key", ns)]
        prefixes = [x.text for x in root.findall("s:CommonPrefixes/s:Prefix", ns)]
        result.extend(prefixes if delimiter else keys)
        if root.findtext("s:IsTruncated", default="false", namespaces=ns) != "true":
            break
        marker = root.findtext("s:NextMarker", namespaces=ns) or (keys + prefixes)[-1]
    return result


def discover_universe(cutoff: datetime, count: int) -> tuple[list[str], dict]:
    if count < 2:
        raise ValueError("universe requires BTC and ETH")
    candidates = sorted({p.rstrip("/").split("/")[-1] for p in list_bucket(PREFIX, "/")})
    candidates = [s for s in candidates if s.endswith("USDT") and s[:-4] not in STABLES
                  and "_" not in s]  # dated contracts are excluded
    cutoff_tag = cutoff.strftime("%Y-%m")

    def existed_before(symbol):
        keys = list_bucket(PREFIX + symbol + "/5m/")
        months = [k.rsplit("-", 2)[-2] + "-" + k.rsplit("-", 1)[-1][:2]
                  for k in keys if k.endswith(".zip")]
        return symbol, any(m < cutoff_tag for m in months)

    with ThreadPoolExecutor(max_workers=4) as pool:
        eligible = [s for s, ok in pool.map(existed_before, candidates) if ok]
    if not {"BTCUSDT", "ETHUSDT"}.issubset(eligible):
        raise RuntimeError("BTC/ETH context archives unavailable before cutoff")
    # Fixed seeded sampling, independent of current price, volume and future
    # returns. Liquidity/activity are selected point-in-time by the builder.
    ranked = sorted(set(eligible) - {"BTCUSDT", "ETHUSDT"},
                    key=lambda s: hashlib.sha256(("swing-v2-seed-42:" + s).encode()).hexdigest())
    selected = ["BTCUSDT", "ETHUSDT"] + ranked[:count - 2]
    return selected, {"discovered_count": len(candidates), "eligible_before_cutoff": len(eligible),
                      "universe_cutoff_utc": cutoff.isoformat(), "selection_seed": "42",
                      "selection": "deterministic archive sample; no current ticker filter",
                      "survivorship_note": "Includes retained delisted archives; not proof of complete historical listing coverage"}


def read_archive(key: str) -> tuple[np.ndarray | None, dict]:
    raw = fetch(ARCHIVE + "/" + key, missing_ok=True)
    if raw is None:
        return None, {"key": key, "status": "MISSING"}
    checksum = fetch(ARCHIVE + "/" + key + ".CHECKSUM")
    expected = checksum.decode().split()[0].lower()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise RuntimeError(f"checksum mismatch: {key}")
    rows = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        files = [n for n in z.namelist() if n.endswith(".csv")]
        if len(files) != 1:
            raise RuntimeError(f"unexpected archive members: {key}")
        with z.open(files[0]) as f:
            for fields in csv.reader(io.TextIOWrapper(f, encoding="utf-8")):
                if not fields or not fields[0].isdigit():
                    continue
                t, ct = int(fields[0]), int(fields[6])
                if t > 10**14:  # robust to microsecond archives
                    t, ct = t // 1000, ct // 1000
                rows.append((t, *map(float, fields[1:6]), ct, float(fields[7]),
                             int(fields[8]), float(fields[9]), float(fields[10])))
    return np.array(rows, dtype=DTYPE), {"key": key, "status": "VERIFIED", "sha256": actual}


def validate_bars(a: np.ndarray):
    if not len(a):
        raise ValueError("empty candle set")
    numeric = [k for k in a.dtype.names if k not in {"open_time_ms", "close_time_ms", "trades"}]
    if any(not np.isfinite(a[k]).all() for k in numeric):
        raise ValueError("non-finite candle values")
    if (np.diff(a["open_time_ms"]) <= 0).any():
        raise ValueError("duplicate/unordered timestamps")
    if ((a["open_time_ms"] % MS_5M != 0) |
        (a["close_time_ms"] != a["open_time_ms"] + MS_5M - 1)).any():
        raise ValueError("invalid candle times")
    if any((a[k] <= 0).any() for k in ["open", "high", "low", "close"]):
        raise ValueError("nonpositive price")
    if ((a["high"] < np.maximum(a["open"], a["close"])) |
        (a["low"] > np.minimum(a["open"], a["close"])) | (a["high"] < a["low"])).any():
        raise ValueError("invalid OHLC")
    if any((a[k] < 0).any() for k in ["volume", "quote_volume", "taker_buy_quote_volume", "trades"]):
        raise ValueError("negative volume/trades")


def aggregate(a: np.ndarray, minutes: int) -> np.ndarray:
    n = minutes // 5
    step = minutes * 60_000
    # Group by calendar bucket; exclude incomplete/gappy groups.
    ids = a["open_time_ms"] // step
    starts = np.r_[0, np.flatnonzero(np.diff(ids)) + 1]
    ends = np.r_[starts[1:], len(a)]
    valid = (ends - starts == n) & (a["open_time_ms"][starts] % step == 0)
    starts, ends = starts[valid], ends[valid]
    valid = a["open_time_ms"][ends - 1] - a["open_time_ms"][starts] == (n - 1) * MS_5M
    starts = starts[valid]
    out = np.empty(len(starts), dtype=DTYPE)
    for j, start in enumerate(starts):
        bars = a[start:start + n]
        out[j] = (bars[0]["open_time_ms"], bars[0]["open"], bars["high"].max(),
                  bars["low"].min(), bars[-1]["close"], bars["volume"].sum(),
                  bars[-1]["close_time_ms"], bars["quote_volume"].sum(), bars["trades"].sum(),
                  bars["taker_buy_base_volume"].sum(), bars["taker_buy_quote_volume"].sum())
    return out


def month_start(d):
    return d.replace(day=1)


def next_month(d):
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def backfill_symbol(symbol: str, start: datetime, end: datetime, out: Path) -> dict:
    source, chunks = [], []
    cursor = month_start(start)
    while cursor < end:
        nxt = next_month(cursor)
        tag = cursor.strftime("%Y-%m")
        # Completed months only; current month is collected by daily archive.
        monthly = nxt <= end
        if monthly:
            key = PREFIX + f"{symbol}/5m/{symbol}-5m-{tag}.zip"
            a, record = read_archive(key)
            source.append(record)
            if a is not None:
                chunks.append(a)
        else:
            day = max(cursor, start)
            while day < end:
                date_tag = day.strftime("%Y-%m-%d")
                key = f"data/futures/um/daily/klines/{symbol}/5m/{symbol}-5m-{date_tag}.zip"
                a, record = read_archive(key)
                source.append(record)
                if a is not None:
                    chunks.append(a)
                day += timedelta(days=1)
        cursor = nxt
    if not chunks:
        return {"symbol": symbol, "status": "NO_HISTORY", "archives": source}
    a = np.concatenate(chunks)
    a = a[np.argsort(a["open_time_ms"])]
    lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    a = a[(a["open_time_ms"] >= lo) & (a["close_time_ms"] < hi)]
    validate_bars(a)
    if len(a) < 30 * 288:
        return {"symbol": symbol, "status": "INSUFFICIENT_HISTORY", "rows": len(a), "archives": source}
    sdir = out / symbol
    sdir.mkdir(parents=True, exist_ok=True)
    for interval, minutes in [("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240)]:
        np.save(sdir / (interval + ".npy"), a if minutes == 5 else aggregate(a, minutes))
    return {"symbol": symbol, "status": "PASS", "rows": len(a),
            "first_open_ms": int(a[0]["open_time_ms"]), "last_close_ms": int(a[-1]["close_time_ms"]),
            "gap_count": int((np.diff(a["open_time_ms"]) != MS_5M).sum()), "archives": source}


def main():
    days = int(os.getenv("ML_HISTORY_DAYS", "365"))
    count = int(os.getenv("ML_UNIVERSE_SIZE", "200"))
    if not 30 <= days <= 1095 or not 2 <= count <= 1000:
        raise ValueError("invalid history/universe limits")
    # Two-day publication buffer. End is exclusive and frozen in the manifest.
    end_date = os.getenv("ML_END_DATE")
    end = (datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if end_date
           else datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
    start = end - timedelta(days=days)
    # Universe membership is frozen before the first validation period.
    cutoff = start + timedelta(days=int(days * .60))
    out = Path(os.getenv("ML_RAW_DIR", "_futures_raw_v2"))
    out.mkdir(parents=True, exist_ok=True)
    symbols, discovery = discover_universe(cutoff, count)
    manifest = {"version": "futures-archive-v2", "market": "USD-M perpetual", "days": days,
                "start_ms": int(start.timestamp() * 1000), "end_exclusive_ms": int(end.timestamp() * 1000),
                "symbols": symbols, "discovery": discovery, "files": [], "status": "RUNNING"}
    mp = out / "manifest.json"
    mp.write_text(json.dumps(manifest, indent=2))
    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {symbol}", flush=True)
        try:
            result = backfill_symbol(symbol, start, end, out)
        except Exception as exc:
            result = {"symbol": symbol, "status": "FAIL", "error": str(exc)}
        manifest["files"].append(result)
        mp.write_text(json.dumps(manifest, indent=2))
    passed = [r["symbol"] for r in manifest["files"] if r["status"] == "PASS"]
    manifest["eligible_symbols"] = passed
    required = int(os.getenv("ML_MIN_USABLE_SYMBOLS", str(min(150, count))))
    manifest["status"] = "PASS" if len(passed) >= required and {"BTCUSDT", "ETHUSDT"}.issubset(passed) else "FAIL"
    mp.write_text(json.dumps(manifest, indent=2))
    print(f"Archive {manifest['status']}: {len(passed)}/{len(symbols)} usable", flush=True)
    if manifest["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
