#!/usr/bin/env python3
import gzip, hashlib, json, os, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://data-api.binance.vision"
OUT = Path("_backfill_artifact")
MANIFEST = OUT / "manifest.json"

SYMBOLS = [
    s.strip().upper()
    for s in os.getenv(
        "BACKFILL_SYMBOLS",
        "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT"
    ).split(",")
    if s.strip()
]

DAYS = max(1, min(int(os.getenv("BACKFILL_DAYS", "30")), 365))
MIN_COMPLETE_SYMBOLS = max(1, int(os.getenv("BACKFILL_MIN_COMPLETE_SYMBOLS", "45")))
CRITICAL_SYMBOLS = {"BTCUSDT", "ETHUSDT"}

INTERVALS = {
    "5m": 300_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}

LIMIT = 1000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def get_page(symbol, interval, start_ms, end_ms):
    q = urlencode({
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": LIMIT,
    })
    req = Request(
        f"{BASE}/api/v3/klines?{q}",
        headers={
            "User-Agent": "crypto-scan-backfill/1.1",
            "Accept": "application/json",
        },
    )
    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected response: {data}")
    return data


def fetch_all(symbol, interval, step, start_ms, end_exclusive):
    rows = []
    cur = start_ms
    end_inclusive = end_exclusive - 1
    while cur <= end_inclusive:
        page = get_page(symbol, interval, cur, end_inclusive)
        if not page:
            break
        rows.extend(x for x in page if start_ms <= int(x[0]) < end_exclusive)
        nxt = int(page[-1][0]) + step
        if nxt <= cur:
            raise RuntimeError("pagination did not advance")
        cur = nxt
        if len(page) < LIMIT:
            break
        time.sleep(0.08)
    return rows


def compact(x):
    return {
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


def audit(rows, step, expected):
    errors = []
    seen = set()
    prev = None
    for i, r in enumerate(rows):
        t = r["open_time_ms"]
        if t in seen:
            errors.append({"code": "DUPLICATE", "index": i, "open_time_ms": t})
        seen.add(t)
        if prev is not None and t - prev != step:
            errors.append({
                "code": "GAP_OR_BAD_STEP",
                "index": i,
                "prev": prev,
                "current": t,
                "delta": t - prev,
            })
        prev = t

        o, h, l, c = r["open"], r["high"], r["low"], r["close"]
        if min(o, h, l, c) <= 0 or h < max(o, c) or l > min(o, c) or h < l:
            errors.append({"code": "BAD_OHLC", "index": i, "open_time_ms": t})
        if r["volume"] < 0 or r["quote_volume"] < 0:
            errors.append({"code": "NEGATIVE_VOLUME", "index": i, "open_time_ms": t})

    if len(rows) != expected:
        errors.append({"code": "ROW_COUNT", "expected": expected, "actual": len(rows)})

    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors[:100],
        "error_count": len(errors),
        "rows": len(rows),
    }


def write_gz(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":"), sort_keys=True) + "\n")

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1_048_576), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    OUT.mkdir(exist_ok=True)
    now_ms = int(time.time() * 1000)
    end_override = os.getenv("BACKFILL_END_MS", "").strip()
    requested_end = int(end_override) if end_override else now_ms

    report = {
        "version": "historical-raw-pilot-v1.1",
        "source": BASE,
        "market": "spot",
        "generated_at_ms": now_ms,
        "generated_at_utc": iso(now_ms),
        "symbols": SYMBOLS,
        "eligible_symbols": [],
        "excluded_symbols": [],
        "minimum_complete_symbols": MIN_COMPLETE_SYMBOLS,
        "days": DAYS,
        "files": [],
        "status": "PASS",
    }

    total_errors = 0

    for symbol in SYMBOLS:
        symbol_ok = True
        symbol_errors = []

        for interval, step in INTERVALS.items():
            end_excl = (requested_end // step) * step
            start = end_excl - DAYS * 86_400_000
            expected = (end_excl - start) // step
            path = OUT / symbol / f"{interval}_{DAYS}d.jsonl.gz"

            print(f"Fetching {symbol} {interval}: {iso(start)} -> {iso(end_excl)}", flush=True)

            try:
                raw = fetch_all(symbol, interval, step, start, end_excl)
                rows = sorted((compact(x) for x in raw), key=lambda r: r["open_time_ms"])
                check = audit(rows, step, expected)

                if check["status"] != "PASS":
                    symbol_ok = False
                    total_errors += check["error_count"]
                    symbol_errors.extend({"interval": interval, **e} for e in check["errors"])
                    if path.exists():
                        path.unlink()
                    print(
                        f"[Backfill] EXCLUDE {symbol} {interval}: audit failed "
                        f"errors={check['errors'][:5]}",
                        flush=True,
                    )
                    report["files"].append({
                        "symbol": symbol,
                        "interval": interval,
                        "start_ms": start,
                        "end_exclusive_ms": end_excl,
                        "expected_rows": expected,
                        "actual_rows": len(rows),
                        "audit": check,
                    })
                    continue

                sha = write_gz(path, rows)
                report["files"].append({
                    "symbol": symbol,
                    "interval": interval,
                    "path": str(path),
                    "sha256": sha,
                    "start_ms": start,
                    "end_exclusive_ms": end_excl,
                    "expected_rows": expected,
                    "actual_rows": len(rows),
                    "audit": check,
                })

            except Exception as e:
                symbol_ok = False
                total_errors += 1
                err = {"interval": interval, "code": "FETCH_FAILED", "message": str(e)}
                symbol_errors.append(err)
                if path.exists():
                    path.unlink()
                print(f"[Backfill] EXCLUDE {symbol} {interval}: {e}", flush=True)
                report["files"].append({
                    "symbol": symbol,
                    "interval": interval,
                    "audit": {
                        "status": "FAIL",
                        "error_count": 1,
                        "errors": [{"code": "FETCH_FAILED", "message": str(e)}],
                    },
                })

        if symbol_ok:
            report["eligible_symbols"].append(symbol)
        else:
            # Never allow a partly-valid symbol into the backtest. Remove any
            # interval files that may have succeeded before another interval failed.
            symbol_dir = OUT / symbol
            if symbol_dir.exists():
                for p in symbol_dir.glob(f"*_{DAYS}d.jsonl.gz"):
                    p.unlink()
            report["excluded_symbols"].append({"symbol": symbol, "errors": symbol_errors[:20]})

    report["total_errors"] = total_errors
    complete = len(report["eligible_symbols"])
    critical_missing = sorted(CRITICAL_SYMBOLS.intersection(SYMBOLS) - set(report["eligible_symbols"]))
    enough_symbols = complete >= min(MIN_COMPLETE_SYMBOLS, len(SYMBOLS))
    quality_ok = enough_symbols and not critical_missing

    report["complete_symbol_count"] = complete
    report["critical_missing"] = critical_missing
    report["status"] = "PASS" if quality_ok else "FAIL"

    MANIFEST.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    print(
        f"Pilot {report['status']} | requested={len(SYMBOLS)} | complete={complete} | "
        f"excluded={len(report['excluded_symbols'])} | raw_errors={total_errors}",
        flush=True,
    )
    if report["excluded_symbols"]:
        print("Excluded symbols:", json.dumps(report["excluded_symbols"], indent=2), flush=True)

    if not quality_ok:
        if critical_missing:
            print(f"Critical market-context symbols missing: {critical_missing}", flush=True)
        if not enough_symbols:
            print(
                f"Only {complete} complete symbols; require at least "
                f"{min(MIN_COMPLETE_SYMBOLS, len(SYMBOLS))}.",
                flush=True,
            )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
