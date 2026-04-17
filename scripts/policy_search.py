#!/usr/bin/env python3
"""
Policy Search — Phase E2 scaffolding
====================================
Proposes filter-parameter variants for per-cohort tuning, backed by
Thompson sampling on historical WR.

Reads the weak-spot map from analyze_weakspots.py and produces
variant proposals like:

    cohort: (sideways, structure_bounce, short, us)
    current: {min_confidence: 70}
    proposed variants:
      - {min_confidence: 75} — expected WR lift +3.2pp (95% CI: -1.1, +7.5)
      - {min_confidence: 80} — expected WR lift +5.8pp (95% CI: +0.2, +11.4)

NOTHING is applied. Output goes to storage/policy_variants/{timestamp}/
as JSON for human review. Tomorrow we wire an opt-in shadow-mode
adapter in scalp_strategy to test one variant at a time.

Phase E2 uses a simple Bayesian beta posterior per cohort+variant.
Phase E3 (future) will use Thompson sampling to allocate real traffic
to variants and track posteriors over time.

Design contract:
  - READ ONLY from storage/closed_signals.json + memory weak-spot map
  - WRITE ONLY to storage/policy_variants/
  - No edits to live filter code
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
VARIANT_DIR = STORAGE / "policy_variants"


# Hand-selected target cohorts based on weak-spot map (2026-04-16)
# Format: (scanner, regime, side, session): {current_params}
TARGET_COHORTS = [
    # Biggest leaks first
    (("structure_bounce", "high_volatility", "long", "*"), {"min_confidence": 75}),
    (("structure_bounce", "high_volatility", "short", "*"), {"min_confidence": 75}),
    (("structure_bounce", "sideways", "short", "us"), {"min_confidence": 70}),
    (("structure_bounce", "sideways", "long", "asia_early"), {"min_confidence": 70}),
    (("structure_bounce", "sideways", "short", "asia_early"), {"min_confidence": 70}),
    (("structure_bounce", "sideways", "long", "india_midday"), {"min_confidence": 70}),
    (("structure_bounce", "high_volatility", "long", "europe"), {"min_confidence": 75}),
    (("structure_bounce", "mean_reversion", "short", "us"), {"min_confidence": 68}),
]


def parse_ts(x):
    if not x: return None
    try: return datetime.fromisoformat(x.replace('Z', '+00:00'))
    except Exception: return None


def utc_to_session(utc_hour: int) -> str:
    if 0 <= utc_hour < 7: return "asia"
    if 7 <= utc_hour < 14: return "europe"
    if 14 <= utc_hour < 21: return "us"
    return "late"


def _cohort_matches(trade: dict, cohort: tuple) -> bool:
    scn, reg, side, sess = cohort
    md = trade.get("metadata") or {}
    t_scn = md.get("setup_type") or md.get("scanner") or ""
    t_reg = md.get("regime") or ""
    t_side = trade.get("side") or ""
    entry_ts = parse_ts(trade.get("entry_time"))
    t_sess = md.get("session") or (utc_to_session(entry_ts.hour) if entry_ts else "")

    if scn != "*" and scn != t_scn: return False
    if reg != "*" and reg != t_reg: return False
    if side != "*" and side != t_side: return False
    if sess != "*" and sess != t_sess: return False
    return True


def _beta_wr_estimate(wins: int, losses: int, alpha_prior: float = 1.0, beta_prior: float = 1.0):
    """Posterior Beta(alpha+wins, beta+losses). Return mean, 95% CI."""
    a = alpha_prior + wins
    b = beta_prior + losses
    # Beta mean = a / (a+b)
    mean = a / (a + b)
    # 95% CI — use scipy if available, else approximate with normal
    try:
        from scipy.stats import beta
        lo, hi = beta.ppf([0.025, 0.975], a, b)
    except Exception:
        import math
        var = (a * b) / ((a + b) ** 2 * (a + b + 1))
        sd = math.sqrt(var)
        lo, hi = max(0, mean - 1.96 * sd), min(1, mean + 1.96 * sd)
    return mean, lo, hi


def _simulate_filter(trades: list, confidence_threshold: int) -> dict:
    """If we had blocked trades where confidence < threshold, what would the
    remaining cohort WR and total R be?
    """
    kept = [t for t in trades if (t.get("confidence") or 0) >= confidence_threshold]
    if not kept:
        return {"n": 0, "wr": 0, "total_r": 0}
    wins = sum(1 for t in kept if float(t.get("pnl_pct") or 0) > 0)
    wr = wins / len(kept) * 100
    total_r = sum(float(t.get("exit_r") or 0) for t in kept)
    return {"n": len(kept), "wr": wr, "total_r": total_r, "kept_pct": len(kept) / len(trades) * 100}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--signals", default=str(STORAGE / "closed_signals.json"))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="Phase E2 is dry-run only — variants written to disk for review")
    args = ap.parse_args(argv)

    path = Path(args.signals)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 2

    with open(path) as fh:
        data = json.load(fh)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    trades = [t for t in data if (p := parse_ts(t.get("exit_time"))) and p > cutoff]

    # Overall baseline
    n = len(trades)
    wins = sum(1 for t in trades if float(t.get("pnl_pct") or 0) > 0)
    base_wr = wins / n * 100 if n else 0
    print(f"=== Policy Search — {args.days}d window, {n} trades, baseline WR {base_wr:.1f}% ===\n")

    # For each target cohort, propose 3-4 variants and score them
    variants_all = []
    for cohort, current_params in TARGET_COHORTS:
        cohort_trades = [t for t in trades if _cohort_matches(t, cohort)]
        if len(cohort_trades) < 20:
            continue  # skip cohorts with insufficient data

        scn, reg, side, sess = cohort
        wins_c = sum(1 for t in cohort_trades if float(t.get("pnl_pct") or 0) > 0)
        losses_c = len(cohort_trades) - wins_c
        cohort_wr_mean, lo, hi = _beta_wr_estimate(wins_c, losses_c)

        cur_conf = current_params.get("min_confidence", 70)
        # Propose variants: current, +5, +10, +15
        variants = []
        for conf in (cur_conf, cur_conf + 5, cur_conf + 10, cur_conf + 15):
            sim = _simulate_filter(cohort_trades, conf)
            if sim["n"] >= 10:
                # Beta posterior on filtered cohort
                post_wins = int(sim["n"] * sim["wr"] / 100)
                post_losses = sim["n"] - post_wins
                mean, lo_v, hi_v = _beta_wr_estimate(post_wins, post_losses)
                lift = sim["wr"] - cohort_wr_mean * 100
                variants.append({
                    "params": {"min_confidence": conf},
                    "n_after_filter": sim["n"],
                    "kept_pct": round(sim["kept_pct"], 1),
                    "wr": round(sim["wr"], 1),
                    "wr_lift_pp": round(lift, 2),
                    "wr_95ci": [round(lo_v * 100, 1), round(hi_v * 100, 1)],
                    "total_r": round(sim["total_r"], 2),
                })

        cohort_rec = {
            "cohort": {"scanner": scn, "regime": reg, "side": side, "session": sess},
            "current": current_params,
            "baseline_n": len(cohort_trades),
            "baseline_wr": round(cohort_wr_mean * 100, 1),
            "baseline_wr_95ci": [round(lo * 100, 1), round(hi * 100, 1)],
            "variants": variants,
        }
        variants_all.append(cohort_rec)

        print(f"cohort: {scn} × {reg} × {side} × {sess}")
        print(f"  baseline: n={len(cohort_trades)}, WR={cohort_wr_mean*100:.1f}% (95%CI {lo*100:.1f}-{hi*100:.1f})")
        for v in variants:
            star = " ←" if v["wr_lift_pp"] > 3.0 else ""
            print(f"  min_conf={v['params']['min_confidence']}: "
                  f"kept {v['n_after_filter']} ({v['kept_pct']}%), "
                  f"WR={v['wr']}% (lift {v['wr_lift_pp']:+.1f}pp, 95%CI {v['wr_95ci'][0]}-{v['wr_95ci'][1]}){star}")
        print()

    # Write variants to disk
    if not args.dry_run:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_dir = VARIANT_DIR / ts
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "proposals.json"
        with open(out_file, "w") as fh:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "window_days": args.days,
                "baseline_wr": base_wr,
                "cohorts": variants_all,
            }, fh, indent=2, default=str)
        print(f"Saved: {out_file}")
    else:
        print("--dry-run: no variant file written.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
