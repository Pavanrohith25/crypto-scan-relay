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

DAYS = max(
    1,
    min(int(os.getenv("BACKFILL_DAYS", "30")), 365)
)

INTERVALS = {
    "5m": 300_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}

LIMIT = 1000


def iso(ms):
    return datetime.fromtimestamp(
        ms / 1000,
        tz=timezone.utc
    ).isoformat()


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
            "User-Agent": "crypto-scan-backfill/1.0",
            "Accept": "application/json",
        },
    )

    with urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())

    if not isinstance(data, list):
        raise RuntimeError(
            f"Unexpected response: {data}"
        )

    return data


def fetch_all(
    symbol,
    interval,
    step,
    start_ms,
    end_exclusive,
):
    rows = []
    cur = start_ms
    end_inclusive = end_exclusive - 1

    while cur <= end_inclusive:
        page = get_page(
            symbol,
            interval,
            cur,
            end_inclusive,
        )

        if not page:
            break

        rows.extend(
            x for x in page
            if start_ms <= int(x[0]) < end_exclusive
        )

        nxt = int(page[-1][0]) + step

        if nxt <= cur:
            raise RuntimeError(
                "pagination did not advance"
            )

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
            errors.append({
                "code": "DUPLICATE",
                "index": i,
                "open_time_ms": t,
            })

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

        o = r["open"]
        h = r["high"]
        l = r["low"]
        c = r["close"]

        if (
            min(o, h, l, c) <= 0
            or h < max(o, c)
            or l > min(o, c)
            or h < l
        ):
            errors.append({
                "code": "BAD_OHLC",
                "index": i,
                "open_time_ms": t,
            })

        if (
            r["volume"] < 0
            or r["quote_volume"] < 0
        ):
            errors.append({
                "code": "NEGATIVE_VOLUME",
                "index": i,
                "open_time_ms": t,
            })

    if len(rows) != expected:
        errors.append({
            "code": "ROW_COUNT",
            "expected": expected,
            "actual": len(rows),
        })

    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors[:100],
        "error_count": len(errors),
        "rows": len(rows),
    }


def write_gz(path, rows):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with gzip.open(
        path,
        "wt",
        encoding="utf-8",
    ) as f:
        for r in rows:
            f.write(
                json.dumps(
                    r,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )

    h = hashlib.sha256()

    with open(path, "rb") as f:
        for chunk in iter(
            lambda: f.read(1_048_576),
            b"",
        ):
            h.update(chunk)

    return h.hexdigest()


def main():
    OUT.mkdir(exist_ok=True)

    now_ms = int(time.time() * 1000)

    end_override = os.getenv(
        "BACKFILL_END_MS",
        "",
    ).strip()

    requested_end = (
        int(end_override)
        if end_override
        else now_ms
    )

    report = {
        "version": "historical-raw-pilot-v1",
        "source": BASE,
        "market": "spot",
        "generated_at_ms": now_ms,
        "generated_at_utc": iso(now_ms),
        "symbols": SYMBOLS,
        "days": DAYS,
        "files": [],
        "status": "PASS",
    }

    total_errors = 0

    for symbol in SYMBOLS:
        for interval, step in INTERVALS.items():

            end_excl = (
                requested_end // step
            ) * step

            start = (
                end_excl
                - DAYS * 86_400_000
            )

            expected = (
                end_excl - start
            ) // step

            print(
                f"Fetching {symbol} {interval}: "
                f"{iso(start)} -> "
                f"{iso(end_excl)}"
            )

            try:
                raw = fetch_all(
                    symbol,
                    interval,
                    step,
                    start,
                    end_excl,
                )

                rows = sorted(
                    (compact(x) for x in raw),
                    key=lambda r: r[
                        "open_time_ms"
                    ],
                )

                check = audit(
                    rows,
                    step,
                    expected,
                )

                path = (
                    OUT
                    / symbol
                    / f"{interval}_{DAYS}d.jsonl.gz"
                )

                sha = write_gz(
                    path,
                    rows,
                )

                total_errors += (
                    check["error_count"]
                )

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
                total_errors += 1

                report["files"].append({
                    "symbol": symbol,
                    "interval": interval,
                    "audit": {
                        "status": "FAIL",
                        "error_count": 1,
                        "errors": [{
                            "code": "FETCH_FAILED",
                            "message": str(e),
                        }],
                    },
                })

    report["total_errors"] = (
        total_errors
    )

    report["status"] = (
        "PASS"
        if total_errors == 0
        else "FAIL"
    )

    MANIFEST.write_text(
        json.dumps(
            report,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    print(
        f"Pilot {report['status']} | "
        f"files={len(report['files'])} | "
        f"errors={total_errors}"
    )

    if total_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
