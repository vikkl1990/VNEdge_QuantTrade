#!/usr/bin/env python3
"""
Cohort Health Monitor — Phase E3 scaffolding
============================================
Continuously (on invocation) audits per-cohort WR and alerts when any
cohort's rolling-50 WR drops significantly below its historical mean.

Catches:
  - A scanner-regime combo degrading after market structure shift
  - ML model decay on a specific pair
  - Session-time-of-day weakness emerging

Output:
  - Prints a health table of cohorts with N >= 30
  - Flags cohorts where (rolling_WR - historical_WR) < -8pp
  - Writes storage/cohort_health/{timestamp}.json for dashboard

Design contract:
  - READ ONLY from storage/closed_signals.json
  - WRITE ONLY to storage/cohort_health/
  - Alert channel: append to storage/alerts.jsonl (existing format)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "storage"
HEALTH_DIR = STORAGE / "cohort_health"
ALERTS_FILE = STORAGE / "alerts.jsonl"


def parse_ts(x):
    if not x: return None
    try: return datetime.fromisoformat(x.replace('Z', '+00:00'))
    except Exception: return None


def utc_to_session(h):
    if 0 <= h < 7: return "asia"
    if 7 <= h < 14: return "europe"
    if 14 <= h < 21: return "us"
    return "late"


def _cohort_key(t: dict) -> tuple:
    md = t.get("metadata") or {}
    scn = md.get("setup_type") or "?"
    reg = md.get("regime") or "?"
    side = t.get("side") or "?"
    return (scn, reg, side)


def _wr(trades: list) -> tuple:
    if not trades: return 0.0, 0, 0
    wins = sum(1 for t in trades if float(t.get("pnl_pct") or 0) > 0)
    return wins / len(trades) * 100, wins, len(trades) - wins


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--signals", default=str(STORAGE / "closed_signals.json"))
    ap.add_argument("--historical-days", type=int, default=21,
                    help="Historical baseline window")
    ap.add_argument("--rolling-n", type=int, default=50,
                    help="Rolling trade count for recent WR")
    ap.add_argument("--alert-pp", type=float, default=8.0,
                    help="pp drop from historical to trigger alert")
    ap.add_argument("--min-historical-n", type=int, default=30,
                    help="Minimum historical trades to be auditable")
    args = ap.parse_args(argv)

    path = Path(args.signals)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 2

    with open(path) as fh:
        data = json.load(fh)

    now = datetime.now(timezone.utc)
    hist_cutoff = now - timedelta(days=args.historical_days)
    historical = [t for t in data if (p := parse_ts(t.get("exit_time"))) and p > hist_cutoff]

    # Group by cohort
    by_cohort = defaultdict(list)
    for t in historical:
        by_cohort[_cohort_key(t)].append(t)

    rows = []
    alerts = []

    print(f"=== Cohort Health — {args.historical_days}d window, {len(historical)} trades ===\n")
    print(f"{'cohort':<60} {'N':>5} {'WR_hist':>8} {'WR_recent':>10} {'delta':>7} {'verdict':<12}")
    print("-" * 105)

    for cohort, trades in sorted(by_cohort.items(), key=lambda kv: -len(kv[1])):
        n = len(trades)
        if n < args.min_historical_n:
            continue

        # Sort by exit time ascending to get "recent N"
        trades_sorted = sorted(trades, key=lambda t: parse_ts(t.get("exit_time")) or now)
        recent = trades_sorted[-args.rolling_n:]
        hist_wr, _, _ = _wr(trades_sorted)
        rec_wr, _, _ = _wr(recent)
        delta = rec_wr - hist_wr

        verdict = "HEALTHY"
        if delta <= -args.alert_pp:
            verdict = "⚠ DEGRADED"
            alerts.append({
                "type": "cohort_degradation",
                "cohort": list(cohort),
                "historical_wr": round(hist_wr, 1),
                "recent_wr": round(rec_wr, 1),
                "delta_pp": round(delta, 1),
                "n_historical": n,
                "n_recent": len(recent),
            })
        elif delta >= args.alert_pp:
            verdict = "✓ IMPROVED"

        label = f"{cohort[0]} × {cohort[1]} × {cohort[2]}"
        rows.append({
            "cohort": label,
            "historical_wr": round(hist_wr, 1),
            "recent_wr": round(rec_wr, 1),
            "delta_pp": round(delta, 1),
            "n_historical": n,
            "n_recent": len(recent),
            "verdict": verdict,
        })
        print(f"{label:<60} {n:>5} {hist_wr:>7.1f}% {rec_wr:>9.1f}% {delta:>+6.1f} {verdict:<12}")

    # Emit alerts
    print()
    if alerts:
        print(f"=== {len(alerts)} DEGRADATION ALERTS ===")
        for a in alerts:
            print(f"  {a['cohort'][0]} × {a['cohort'][1]} × {a['cohort'][2]}: "
                  f"{a['historical_wr']}% → {a['recent_wr']}% ({a['delta_pp']:+.1f}pp on last {a['n_recent']})")

        ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERTS_FILE, "a") as fh:
            for a in alerts:
                a["ts"] = now.isoformat()
                fh.write(json.dumps(a, default=str) + "\n")
        print(f"  Alerts appended to: {ALERTS_FILE}")
    else:
        print("✓ All cohorts healthy — no degradations detected.")

    # Write health snapshot
    ts_str = now.strftime("%Y%m%d_%H%M%S")
    out_dir = HEALTH_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"cohort_health_{ts_str}.json"
    with open(out_file, "w") as fh:
        json.dump({
            "timestamp": now.isoformat(),
            "window_days": args.historical_days,
            "rolling_n": args.rolling_n,
            "alert_threshold_pp": args.alert_pp,
            "cohorts": rows,
            "alerts": alerts,
        }, fh, indent=2, default=str)
    print(f"\nSnapshot saved: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
