#!/usr/bin/env python3
"""
reliability_table.py — ML probability calibration read on closed fills.

Answers one question: does ml_probability actually mean P(win) in this bot,
or is it an uncalibrated score being used as if it were one? Builds a
reliability table (equal-mass bins of ml_probability vs. realized win rate
vs. mean net-R) per (setup_type, regime, side) slice, plus Brier score and
ECE for each slice.

This is deliberately separate from scripts/audit_ml_calibration.py, which
does fixed-width-decile isotonic recalibration fitting on a different data
source (storage/closed_signals.json, the rolling 5000-trade file). This
script reads the complete append-only history and is the tool referenced in
docs/SCANNER_CLUSTER_ANALYSIS_TODO_20260915.md and
docs/PIPELINE_ARCHITECTURE_TARGET_20260916.md ("item 6" / the ML-calibration
discussion): no 0.50/0.65 classify_trade() cut should be trusted as a real
probability threshold until this table says the model is calibrated.

Safe: READ-ONLY. Does not touch live bot state, does not hit any exchange.

Usage:
  python3 scripts/reliability_table.py [--archive PATH] [--min-n-per-bin 30]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARCHIVE = PROJECT_ROOT / "storage" / "closed_signals_archive.jsonl"
OUTPUT_FILE = PROJECT_ROOT / "storage" / "research" / "reliability_table.json"

# Same verdict vocabulary as the stamp-once fix in strategies/scalp_strategy.py
# and bot/signal_tracker.py's classify_trade() — a trade tagged with any of
# these did not carry a real model score, regardless of what numeric value
# happens to be stored in ml_probability for it.
NO_REAL_SCORE_VERDICTS_PREFIX = ("ABSTAIN",)
NO_REAL_SCORE_VERDICTS_EXACT = ("STALE_MODEL", "UNREACHABLE", "API_ERROR")


def _has_real_score(meta: dict) -> bool:
    verdict = str(meta.get("ml_verdict") or "")
    if verdict.startswith(NO_REAL_SCORE_VERDICTS_PREFIX):
        return False
    if verdict in NO_REAL_SCORE_VERDICTS_EXACT:
        return False
    p = meta.get("ml_probability")
    return isinstance(p, (int, float))


def load_rows(archive_path: Path) -> list[dict]:
    rows = []
    if not archive_path.exists():
        return rows
    with open(archive_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def dedup_by_trade_id(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in rows:
        tid = r.get("trade_id")
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        out.append(r)
    return out


def slice_key(row: dict) -> tuple[str, str, str]:
    meta = row.get("metadata") or {}
    setup = str(row.get("setup_type") or meta.get("setup_type") or "unknown")
    regime = str(meta.get("regime") or "unknown")
    side = str(row.get("side") or "unknown")
    return (setup, regime, side)


def equal_mass_bins(values: list[float], max_bins: int) -> list[int]:
    """Assign each value to a quantile bin index (0..n_bins-1), stable-sorted."""
    n = len(values)
    if n == 0:
        return []
    n_bins = max(1, min(max_bins, n))
    order = sorted(range(n), key=lambda i: values[i])
    bin_of = [0] * n
    for rank, idx in enumerate(order):
        bin_of[idx] = min(n_bins - 1, (rank * n_bins) // n)
    return bin_of


def compute_slice_table(slice_rows: list[dict], min_n_per_bin: int) -> dict:
    ps = []
    ys = []
    rs = []
    fee_drags = []
    for row in slice_rows:
        meta = row.get("metadata") or {}
        p = float(meta["ml_probability"])
        exit_r = row.get("exit_r")
        if exit_r is None:
            continue
        exit_r = float(exit_r)
        ps.append(p)
        ys.append(1.0 if exit_r > 0 else 0.0)
        rs.append(exit_r)
        fd = meta.get("fee_drag_r")
        fee_drags.append(float(fd) if isinstance(fd, (int, float)) else None)

    n = len(ps)
    if n == 0:
        return {"n": 0, "insufficient_sample": True, "bins": []}

    # Auto-shrink bin count for small samples — never claim more resolution
    # than the sample supports.
    max_bins = max(2, min(10, n // 5)) if n >= 10 else 1
    bin_assignment = equal_mass_bins(ps, max_bins)
    n_bins = max(bin_assignment) + 1 if bin_assignment else 0

    bins = []
    for b in range(n_bins):
        idxs = [i for i in range(n) if bin_assignment[i] == b]
        bn = len(idxs)
        if bn == 0:
            continue
        mean_p = sum(ps[i] for i in idxs) / bn
        win_rate = sum(ys[i] for i in idxs) / bn
        mean_r = sum(rs[i] for i in idxs) / bn
        fd_vals = [fee_drags[i] for i in idxs if fee_drags[i] is not None]
        mean_fee_drag = sum(fd_vals) / len(fd_vals) if fd_vals else None
        bins.append({
            "n": bn,
            "mean_p": round(mean_p, 4),
            "win_rate": round(win_rate, 4),
            "mean_exit_r": round(mean_r, 4),
            "mean_fee_drag_r": round(mean_fee_drag, 4) if mean_fee_drag is not None else None,
            "insufficient_sample": bn < min_n_per_bin,
        })

    # Brier score + skill vs. base-rate baseline
    brier = sum((ps[i] - ys[i]) ** 2 for i in range(n)) / n
    base_rate = sum(ys) / n
    brier_ref = sum((base_rate - ys[i]) ** 2 for i in range(n)) / n
    skill = (1 - brier / brier_ref) if brier_ref > 0 else None

    # ECE: equal-mass weighted mean-abs calibration gap
    ece = sum((b["n"] / n) * abs(b["mean_p"] - b["win_rate"]) for b in bins)

    return {
        "n": n,
        "insufficient_sample": n < min_n_per_bin * 2,  # need at least 2 real bins
        "base_rate": round(base_rate, 4),
        "brier_score": round(brier, 4),
        "brier_skill_vs_base_rate": round(skill, 4) if skill is not None else None,
        "ece": round(ece, 4),
        "bins": bins,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=str, default=str(DEFAULT_ARCHIVE))
    parser.add_argument("--min-n-per-bin", type=int, default=30,
                        help="Bins/slices below this n are labeled insufficient_sample, not hidden.")
    args = parser.parse_args()

    archive_path = Path(args.archive)
    rows = dedup_by_trade_id(load_rows(archive_path))
    scored_rows = [r for r in rows if _has_real_score(r.get("metadata") or {}) and r.get("exit_r") is not None]

    print(f"Reliability table — {archive_path}")
    print(f"Total closed rows: {len(rows)} | with a real ML score + exit_r: {len(scored_rows)} "
          f"(dropped {len(rows) - len(scored_rows)} as ABSTAIN/STALE_MODEL/UNREACHABLE/API_ERROR/missing-exit_r)")
    print()

    slices: dict[tuple, list[dict]] = defaultdict(list)
    for r in scored_rows:
        slices[slice_key(r)].append(r)

    output = {"archive": str(archive_path), "total_rows": len(rows),
              "scored_rows": len(scored_rows), "slices": {}}

    if not slices:
        print("No slices with a real ML score exist yet. Nothing to report — "
              "this is expected until non-shadow, non-abstain, non-stale scored "
              "fills accumulate. See docs/SCANNER_CLUSTER_ANALYSIS_TODO_20260915.md.")
    for key, slice_rows in sorted(slices.items(), key=lambda kv: -len(kv[1])):
        setup, regime, side = key
        table = compute_slice_table(slice_rows, args.min_n_per_bin)
        output["slices"][f"{setup}|{regime}|{side}"] = table

        print(f"=== {setup} | {regime} | {side} — n={table['n']} "
              f"{'(INSUFFICIENT SAMPLE)' if table['insufficient_sample'] else ''}")
        if table["n"] == 0:
            print()
            continue
        print(f"  brier={table['brier_score']}  skill_vs_base_rate={table['brier_skill_vs_base_rate']}  "
              f"ece={table['ece']}  base_rate={table['base_rate']}")
        print(f"  {'n':>4} {'mean_p':>8} {'win_rate':>9} {'mean_exit_r':>12} {'mean_fee_drag_r':>16}  flag")
        for b in table["bins"]:
            flag = "insufficient" if b["insufficient_sample"] else ""
            fd = f"{b['mean_fee_drag_r']:.3f}" if b["mean_fee_drag_r"] is not None else "n/a"
            print(f"  {b['n']:>4} {b['mean_p']:>8.3f} {b['win_rate']:>9.3f} {b['mean_exit_r']:>12.3f} {fd:>16}  {flag}")
        print()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as fh:
        json.dump(output, fh, indent=2, default=str)
    print(f"Wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
