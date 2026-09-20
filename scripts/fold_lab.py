#!/usr/bin/env python3
"""
fold_lab.py — rolling walk-forward folds over an exit-card CSV (G4 of
docs/research/BREW_1H_PREREG_20260917.md).

Folds: judge windows of --judge-days stepped through the sample; a fold is
"fit" on the --fit-days before it (reported for reference — nothing is
tuned, the exits are fixed). Per scanner x exit: avg R per judge fold, the
count of positive folds, pooled avg R, pooled top-5 share.

  python scripts/fold_lab.py --csv storage/research/scanner_lab/duration/BTC_USDT_exit_card_1h500d_trades.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--fit-days", type=int, default=120)
    ap.add_argument("--judge-days", type=int, default=60)
    ap.add_argument("--min-fold-n", type=int, default=8)
    ap.add_argument("--exits", default="old_trail,card_r,old_tp1")
    ap.add_argument("--min-pos-folds", type=int, default=4)
    args = ap.parse_args()

    d = pd.read_csv(args.csv)
    d["t"] = pd.to_datetime(d["entry_time"])
    t0, t1 = d["t"].min(), d["t"].max()
    edges = []
    start = t0 + pd.Timedelta(days=args.fit_days)
    while start + pd.Timedelta(days=args.judge_days) <= t1 + pd.Timedelta(days=1):
        edges.append((start, start + pd.Timedelta(days=args.judge_days)))
        start += pd.Timedelta(days=args.judge_days)
    print(f"{Path(args.csv).name}: {len(d)} trades {t0.date()} -> {t1.date()}; {len(edges)} judge folds of {args.judge_days}d after a {args.fit_days}d fit")
    exits = [e.strip() for e in args.exits.split(",")]
    pd.set_option("display.width", 220, "display.max_rows", 200, "display.float_format", "{:.3f}".format)
    for tf, g in d.groupby("tf"):
        rows = []
        for sc, s in g.groupby("scanner"):
            for ex in exits:
                fold_avgs = []
                for a, b in edges:
                    j = s[(s["t"] >= a) & (s["t"] < b)][ex]
                    fold_avgs.append(j.mean() if len(j) >= args.min_fold_n else np.nan)
                fa = np.array(fold_avgs, dtype=float)
                valid = fa[~np.isnan(fa)]
                allr = s[ex]
                top5 = np.sort(allr.values)[-5:].sum() / allr.sum() if allr.sum() > 0 and len(allr) >= 5 else np.nan
                rows.append({"scanner": sc, "exit": ex, "n": len(s), "folds": len(valid),
                             "pos_folds": int((valid > 0).sum()), "neg_folds": int((valid <= 0).sum()),
                             "fold_avg_median": float(np.median(valid)) if len(valid) else np.nan,
                             "pooled_avg": allr.mean(), "pooled_win%": (allr > 0).mean() * 100, "top5": top5,
                             "folds_str": " ".join("+" if v > 0 else "-" for v in valid)})
        r = pd.DataFrame(rows)
        r["candidate"] = (r["pos_folds"] >= args.min_pos_folds) & (r["pooled_avg"] > 0) & (r["top5"] < 0.6)
        print(f"\n[{tf}] fold-level results (exit x scanner), sorted by positive folds then pooled avg")
        print(r.sort_values(["pos_folds", "pooled_avg"], ascending=[False, False]).to_string(index=False))
        c = r[r["candidate"]]
        print(f"\n[{tf}] CANDIDATES (>= {args.min_pos_folds} positive folds, pooled avg > 0, top5 < 0.6): "
              + (", ".join(f"{x.scanner}/{x.exit}" for x in c.itertuples()) if len(c) else "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
