#!/usr/bin/env python3
"""
Weak-Spot Miner for VN Edge
===========================
Mines storage/closed_signals.json for (scanner × regime × side × session)
combos that have statistically meaningful negative edge. Output ranked
tuning candidates for regime_filter.py.

Use this BEFORE adjusting live filters — only tune combos with n ≥ 50
and WR < cohort_mean - 10pp. Anything smaller is noise.

Usage:
    python scripts/analyze_weakspots.py
    python scripts/analyze_weakspots.py --days 14 --min-n 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


def parse_ts(x):
    if not x:
        return None
    try:
        return datetime.fromisoformat(x.replace('Z', '+00:00'))
    except Exception:
        return None


def utc_to_session(utc_hour: int) -> str:
    """Map UTC hour to broad session band (matches scalp_strategy convention)."""
    # Rough buckets: Asia 0-7, Europe 7-14, US 14-21, Late 21-24
    if 0 <= utc_hour < 7:
        return "asia"
    elif 7 <= utc_hour < 14:
        return "europe"
    elif 14 <= utc_hour < 21:
        return "us"
    else:
        return "late"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    default_path = str(Path(__file__).resolve().parent.parent / "storage" / "closed_signals.json")
    ap.add_argument("--signals", default=default_path)
    ap.add_argument("--days", type=int, default=30, help="Lookback window")
    ap.add_argument("--min-n", type=int, default=30,
                    help="Minimum trades per combo to consider (ignore noise)")
    ap.add_argument("--min-gap-pp", type=float, default=10.0,
                    help="WR must be this many pp below cohort mean to flag")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    path = Path(args.signals)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 2

    with open(path) as fh:
        data = json.load(fh)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    trades = []
    for t in data:
        ts = parse_ts(t.get("exit_time"))
        if ts is None or ts < cutoff:
            continue
        md = t.get("metadata") or {}
        entry_ts = parse_ts(t.get("entry_time")) or ts
        trades.append({
            "symbol":   t.get("symbol", "?"),
            "side":     t.get("side", "?"),
            "scanner":  md.get("setup_type") or md.get("scanner") or "?",
            "regime":   md.get("regime") or "?",
            "session":  md.get("session") or utc_to_session(entry_ts.hour),
            "utc_hour": entry_ts.hour,
            "pnl_pct":  float(t.get("pnl_pct") or 0),
            "exit_r":   float(t.get("exit_r") or 0),
            "exit_reason": t.get("exit_reason", "?"),
            "mfe_r":    float(t.get("mfe_r") or 0),
        })

    if not trades:
        print("No trades in window", file=sys.stderr)
        return 3

    # Overall baseline
    n_total = len(trades)
    wins_total = sum(1 for t in trades if t["pnl_pct"] > 0)
    wr_baseline = wins_total / n_total * 100
    total_r = sum(t["exit_r"] for t in trades)
    print(f"=== Baseline (last {args.days}d) ===")
    print(f"  trades: {n_total:,}")
    print(f"  WR: {wr_baseline:.1f}%  total_R: {total_r:+.2f}R  avg_R: {total_r/n_total:+.3f}R")
    print()

    # --- COHORT 1: scanner × regime × side ---
    combos = defaultdict(list)
    for t in trades:
        key = (t["scanner"], t["regime"], t["side"])
        combos[key].append(t)

    rows = []
    for (scn, reg, side), group in combos.items():
        n = len(group)
        if n < args.min_n:
            continue
        wins = sum(1 for g in group if g["pnl_pct"] > 0)
        wr = wins / n * 100
        total_r = sum(g["exit_r"] for g in group)
        avg_r = total_r / n
        gap = wr - wr_baseline
        rows.append({
            "scanner": scn, "regime": reg, "side": side,
            "n": n, "wr": wr, "gap": gap,
            "total_r": total_r, "avg_r": avg_r,
        })

    rows.sort(key=lambda r: r["gap"])  # worst first

    print(f"=== scanner × regime × side (n >= {args.min_n}) ===")
    print(f"{'scanner':<22} {'regime':<17} {'side':<6} {'n':>5} {'WR%':>6} {'gap':>6} {'avg_R':>8}  verdict")
    print("-" * 85)
    for r in rows:
        verdict = ""
        if r["gap"] <= -args.min_gap_pp:
            verdict = "⚠ WEAKSPOT"
        elif r["gap"] >= args.min_gap_pp:
            verdict = "✓ strength"
        print(f"  {r['scanner']:<20} {r['regime']:<17} {r['side']:<6} {r['n']:>5} "
              f"{r['wr']:>5.1f}% {r['gap']:>+5.1f}  {r['avg_r']:>+.3f}R  {verdict}")

    # --- COHORT 2: scanner × regime × side × session (finer) ---
    combos4 = defaultdict(list)
    for t in trades:
        key = (t["scanner"], t["regime"], t["side"], t["session"])
        combos4[key].append(t)

    rows4 = []
    for (scn, reg, side, sess), group in combos4.items():
        n = len(group)
        if n < args.min_n // 2:  # lower threshold for finer cohort
            continue
        wins = sum(1 for g in group if g["pnl_pct"] > 0)
        wr = wins / n * 100
        total_r = sum(g["exit_r"] for g in group)
        avg_r = total_r / n
        gap = wr - wr_baseline
        rows4.append({
            "scanner": scn, "regime": reg, "side": side, "session": sess,
            "n": n, "wr": wr, "gap": gap, "total_r": total_r, "avg_r": avg_r,
        })

    rows4.sort(key=lambda r: r["gap"])
    print()
    print(f"=== scanner × regime × side × session (n >= {args.min_n // 2}, sorted worst-first) ===")
    print(f"{'scanner':<22} {'regime':<17} {'side':<6} {'session':<8} {'n':>5} {'WR%':>6} {'gap':>6} {'avg_R':>8}")
    print("-" * 95)
    for r in rows4[:15]:
        print(f"  {r['scanner']:<20} {r['regime']:<17} {r['side']:<6} {r['session']:<8} {r['n']:>5} "
              f"{r['wr']:>5.1f}% {r['gap']:>+5.1f}  {r['avg_r']:>+.3f}R")
    print(f"\n  ... showing 15 worst. Top strengths:")
    for r in rows4[-5:]:
        print(f"  {r['scanner']:<20} {r['regime']:<17} {r['side']:<6} {r['session']:<8} {r['n']:>5} "
              f"{r['wr']:>5.1f}% {r['gap']:>+5.1f}  {r['avg_r']:>+.3f}R")

    # --- HYPOTHESIS TEST: sideways + structure_bounce + short + us ---
    print()
    print("=== HYPOTHESIS: sideways + structure_bounce + short + US session ===")
    target = [t for t in trades if t["regime"] == "sideways"
              and t["scanner"] == "structure_bounce"
              and t["side"] == "short"
              and t["session"] == "us"]
    if target:
        wins = sum(1 for t in target if t["pnl_pct"] > 0)
        wr = wins / len(target) * 100
        total_r = sum(t["exit_r"] for t in target)
        gap = wr - wr_baseline
        verdict = "CONFIRMED WEAKSPOT" if gap <= -args.min_gap_pp else "NOT SIGNIFICANT"
        print(f"  n={len(target)}  WR={wr:.1f}%  gap={gap:+.1f}pp vs baseline  total_R={total_r:+.2f}  → {verdict}")
        # Break down by hour
        by_hour = defaultdict(list)
        for t in target:
            by_hour[t["utc_hour"]].append(t)
        print(f"  by UTC hour:")
        for h in sorted(by_hour.keys()):
            items = by_hour[h]
            hwr = sum(1 for x in items if x["pnl_pct"] > 0) / len(items) * 100
            htr = sum(x["exit_r"] for x in items)
            print(f"    {h:02d}:00  n={len(items):>3}  WR={hwr:>5.1f}%  total_R={htr:+.2f}")
    else:
        print("  No data for this combo")

    if args.json:
        print(json.dumps({
            "baseline": {"n": n_total, "wr": wr_baseline, "total_r": total_r},
            "by_scanner_regime_side": rows,
            "by_scanner_regime_side_session": rows4,
        }, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())
