#!/usr/bin/env python3
"""
ML Calibration Audit + R1 Validation
=====================================
Reads storage/closed_signals.json (production paper trades with embedded
ml_probability + outcome) and produces:

  1. Decile calibration table (predicted vs actual WR)
  2. Top-vs-bottom quartile lift (rank usefulness)
  3. Counterfactual: fits an isotonic calibrator on first 70% of trades
     (time-ordered) and applies to last 30% to simulate what the R1
     CalibratedClassifierCV wrapper produces in training.
  4. Headline metrics: AUC, Brier, mean abs calibration error, before/after.

Run after each ML retrain to verify calibration and OOS edge.

Usage:
    # On VM (live data):
    python -m scripts.audit_ml_calibration

    # On Mac with pulled snapshot:
    SIGNALS_PATH=/path/to/closed_signals.json python -m scripts.audit_ml_calibration
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, roc_auc_score


def parse_ts(x):
    if not x:
        return None
    try:
        return datetime.fromisoformat(x.replace('Z', '+00:00'))
    except Exception:
        return None


def ml_meta(t, k):
    return (t.get('metadata', {}) or {}).get(k)


def calib_table(p, y, label, out=sys.stdout):
    print(f"\n--- {label} ---", file=out)
    print(f"{'decile':<10} {'N':>5} {'pred_avg':>9} {'actual_WR':>10} {'gap':>7}", file=out)
    deciles = defaultdict(list)
    for pp, yy in zip(p, y):
        d = int(min(pp, 0.999) * 10)
        deciles[d].append((pp, yy))
    mae = 0.0
    tot = 0
    for d in sorted(deciles.keys()):
        items = deciles[d]
        n_b = len(items)
        if n_b < 5:
            continue
        pred = sum(x[0] for x in items) / n_b
        actual = sum(x[1] for x in items) / n_b
        gap_pp = (actual - pred) * 100
        rng = f"{d/10:.1f}-{(d+1)/10:.1f}"
        print(f"  {rng:<8} {n_b:>5} {pred:>9.3f} {actual*100:>9.1f}% {gap_pp:>+5.1f}pp", file=out)
        mae += abs(actual - pred) * n_b
        tot += n_b
    mae = mae / tot if tot else 0.0
    if len(np.unique(y)) >= 2:
        try:
            auc = roc_auc_score(y, p)
        except Exception:
            auc = float('nan')
    else:
        auc = float('nan')
    brier = brier_score_loss(y, p)
    print(f"  AUC: {auc:.4f}  Brier: {brier:.4f}  MeanAbsCalErr: {mae*100:.2f}pp", file=out)
    return {'auc': float(auc) if auc == auc else None, 'brier': float(brier), 'mae': float(mae)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--signals', default=os.environ.get(
        'SIGNALS_PATH',
        str(Path(__file__).resolve().parent.parent / 'storage' / 'closed_signals.json'),
    ), help='Path to closed_signals.json')
    ap.add_argument('--days', type=int, default=30, help='Lookback window in days')
    ap.add_argument('--split', type=float, default=0.7, help='Time-ordered train fraction for isotonic fit')
    ap.add_argument('--json', action='store_true', help='Output machine-readable JSON summary at end')
    args = ap.parse_args(argv)

    p_path = Path(args.signals)
    if not p_path.exists():
        print(f"ERROR: {p_path} not found", file=sys.stderr)
        return 2

    with open(p_path) as fh:
        data = json.load(fh)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    records = []
    for t in data:
        p = ml_meta(t, 'ml_probability')
        if p is None:
            continue
        try:
            p = float(p)
        except Exception:
            continue
        if p == 0.0:
            continue  # skip "no model" sentinel
        ts = parse_ts(t.get('exit_time'))
        if ts is None or ts < cutoff:
            continue
        pnl = float(t.get('pnl_pct') or 0)
        records.append((ts, p, 1 if pnl > 0 else 0))

    records.sort(key=lambda r: r[0])
    if len(records) < 200:
        print(f"ERROR: only {len(records)} records, need >=200 for meaningful calibration", file=sys.stderr)
        return 3

    n = len(records)
    split = int(n * args.split)
    probs = np.array([r[1] for r in records])
    labels = np.array([r[2] for r in records])
    p_train, y_train = probs[:split], labels[:split]
    p_test, y_test = probs[split:], labels[split:]

    print(f"=== ML Calibration Audit ===")
    print(f"  signals: {p_path}")
    print(f"  records (last {args.days}d, valid ml_prob): {n:,}")
    print(f"  train (oldest {int(args.split*100)}%): {len(p_train):,}")
    print(f"  test  (newest {int((1-args.split)*100)}%): {len(p_test):,}")

    baseline = calib_table(p_test, y_test, "BASELINE — raw production probs on test set")

    ir = IsotonicRegression(out_of_bounds='clip')
    ir.fit(p_train, y_train)
    p_test_cal = ir.transform(p_test)
    after = calib_table(p_test_cal, y_test, "AFTER R1 — isotonic-calibrated probs on test set")

    # Top vs bottom quartile lift (calibrated rank usefulness)
    order = np.argsort(p_test_cal)
    q = len(order) // 4
    bot_wr = float(y_test[order[:q]].mean()) if q else float('nan')
    top_wr = float(y_test[order[-q:]].mean()) if q else float('nan')
    lift_pp = (top_wr - bot_wr) * 100
    print(f"\n--- TOP vs BOTTOM 25% (test set, by calibrated score) ---")
    print(f"  Bottom 25% WR: {bot_wr*100:.1f}% (n={q})")
    print(f"  Top    25% WR: {top_wr*100:.1f}% (n={q})")
    print(f"  Lift: {lift_pp:+.1f}pp  ({'EDGE' if lift_pp > 3 else 'NO EDGE'})")

    print(f"\n=== HEADLINE ===")
    cal_imp_pp = (baseline['mae'] - after['mae']) * 100
    print(f"  Mean abs calibration error: {baseline['mae']*100:.2f}pp -> {after['mae']*100:.2f}pp ({cal_imp_pp:+.2f}pp)")
    print(f"  Brier: {baseline['brier']:.4f} -> {after['brier']:.4f}")
    print(f"  AUC: {baseline['auc']:.4f} -> {after['auc']:.4f}  (isotonic preserves rank)")
    if (after['auc'] or 0) < 0.53:
        print(f"  ⚠ AUC < 0.53 OOS — model has near-zero discriminative edge. Calibration alone won't help.")
    if lift_pp < 3:
        print(f"  ⚠ Top-quartile lift < 3pp — rank-based gating not justified by this model.")

    if args.json:
        print(json.dumps({
            'records': n,
            'train_n': len(p_train),
            'test_n': len(p_test),
            'baseline': baseline,
            'after_r1': after,
            'top_vs_bottom_lift_pp': lift_pp,
            'top_q_wr': top_wr,
            'bot_q_wr': bot_wr,
        }, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
