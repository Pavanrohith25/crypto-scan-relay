#!/usr/bin/env python3
"""Compact integrity audit for the 5% / 24-48h V3.4 objective."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

NEW_SWING = Path("new_swing.json")
RANKINGS = Path("swing_rankings.json")
OUTCOMES = Path("swing_outcomes.json")
GATE = Path("historical_gate_config.json")
REPORT = Path("data_audit_report_v2.json")

OBJECTIVE = {
    "version": "swing-objective-v2-five-percent",
    "target_pct": 5.0,
    "primary_horizon_hours": 24,
    "secondary_horizon_hours": 48,
    "diagnostic_horizons_hours": [4, 12],
    "live_stop_pct": 6.0,
}


def load(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def main():
    now = int(time.time() * 1000)
    scan = load(NEW_SWING)
    rankings = load(RANKINGS)
    outcomes = load(OUTCOMES)
    gate = load(GATE)
    issues = []

    pool = scan.get("analysis_pool") if isinstance(scan, dict) else None
    if not isinstance(pool, list):
        issues.append("analysis_pool missing")
    if not isinstance(scan.get("market_context"), dict):
        issues.append("market_context missing")

    if scan and rankings and scan.get("generated_at_ms") != rankings.get("generated_at_ms"):
        issues.append("rankings are not from the same scan")

    if outcomes:
        obj = outcomes.get("objective") or {}
        if float(obj.get("target_pct") or 0) != 5.0:
            issues.append("outcome tracker target is not +5%")
        if int(obj.get("primary_horizon_hours") or 0) != 24:
            issues.append("outcome tracker primary horizon is not 24h")
        if int(obj.get("secondary_horizon_hours") or 0) != 48:
            issues.append("outcome tracker secondary horizon is not 48h")

    gate_status = "not_built"
    if gate:
        gate_status = "enabled" if gate.get("enabled") else "disabled_or_insufficient_sample"
        obj = gate.get("objective") or {}
        if float(obj.get("target_pct") or 0) != 5.0:
            issues.append("historical gate target is not +5%")
        if int(obj.get("max_horizon_hours") or 0) != 48:
            issues.append("historical gate max horizon is not 48h")

    report = {
        "version": "data-audit-v2-five-percent",
        "generated_at_ms": now,
        "generated_at_utc": datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat(),
        "status": "PASS" if not issues else "FAIL",
        "objective": OBJECTIVE,
        "analysis_pool_count": len(pool) if isinstance(pool, list) else 0,
        "ranked_count": len(rankings.get("rankings") or []) if rankings else 0,
        "tracked_trigger_count": len((outcomes.get("triggers") or {})) if outcomes else 0,
        "historical_gate_status": gate_status,
        "historical_gate_sample_count": gate.get("sample_count") if gate else None,
        "issues": issues,
    }
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"[Data Audit v2] status={report['status']} issues={len(issues)} gate={gate_status}")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
