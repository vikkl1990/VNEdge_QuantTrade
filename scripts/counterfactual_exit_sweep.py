#!/usr/bin/env python3
"""Phase 1 — Counterfactual Exit Configuration Sweep.

Take recent closed delta_shadow trades, replay each with N alternative
exit configurations using the recorded peak_mfe_r + initial_risk + duration,
and report a leaderboard of which config would have produced best PnL.

LIMITATIONS (honest):
  - Uses peak_mfe_r as proxy for "would TP have been hit"
  - Cannot precisely simulate trail-then-SL because we lack post-peak price track
  - Approximates trail exit as lock_pct × peak_mfe_r (matches Phase 5.5-N3 design)
  - kill_threshold approximated by treating actual exit_reason as the closest fit
  - This is a DIRECTIONAL tool, not a precise replay

USAGE:
  python3 scripts/counterfactual_exit_sweep.py [--hours 24]

OUTPUTS:
  storage/exit_sweep/sweep_YYYYMMDD_HHMMSS.md
"""
import sys
import argparse
import datetime
import pathlib
import subprocess
from collections import defaultdict

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
OUT_DIR = ROOT / "storage" / "exit_sweep"
TS = datetime.datetime.utcnow()
OUT_DIR.mkdir(parents=True, exist_ok=True)


def psql(sql):
    cmd = (
        "PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge "
        f"-A -F '|' -t -c \"{sql}\""
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def fetch_trades(hours):
    sql = (
        "SELECT id::text, symbol, side, entry_price::float, exit_price::float, "
        "quantity::float, "
        "COALESCE((metadata::jsonb->>'contract_size')::float, 1.0) as cs, "
        "pnl_usd::float, fees_usd::float, "
        "EXTRACT(EPOCH FROM (closed_at-opened_at))::float as dur_sec, "
        "COALESCE((metadata::jsonb->>'peak_mfe_r')::float, 0) as peak_mfe_r, "
        "COALESCE((metadata::jsonb->>'initial_risk')::float, 0) as initial_risk, "
        "COALESCE((metadata::jsonb->>'stop_loss')::float, 0) as sl, "
        "COALESCE(metadata::jsonb->>'exit_reason', '-') as actual_reason, "
        "COALESCE(metadata::jsonb->>'grade', '-') as grade, "
        "COALESCE(metadata::jsonb->>'regime', '-') as regime "
        f"FROM user_trades WHERE exchange='delta_india' AND trade_type='shadow' "
        f"AND closed_at >= NOW() - INTERVAL '{hours} hours' "
        "AND COALESCE(metadata::jsonb->>'exit_reason','') NOT LIKE 'force_orphan%' "
        "AND COALESCE(metadata::jsonb->>'exit_reason','') NOT LIKE 'auto_responder%' "
        "AND quantity > 0 AND entry_price > 0 AND exit_price > 0"
    )
    rc, out, _ = psql(sql)
    rows = []
    for line in (out.splitlines() if out else []):
        parts = line.split("|")
        if len(parts) < 14:
            continue
        try:
            rows.append({
                "id": parts[0], "symbol": parts[1], "side": parts[2].lower(),
                "entry": float(parts[3]), "actual_exit": float(parts[4]),
                "qty": float(parts[5]), "cs": float(parts[6]),
                "actual_pnl": float(parts[7]), "actual_fees": float(parts[8]),
                "dur_sec": float(parts[9]),
                "peak_mfe_r": float(parts[10]),
                "initial_risk": float(parts[11]),
                "sl_price": float(parts[12]),
                "actual_reason": parts[13],
                "grade": parts[14] if len(parts) > 14 else "-",
                "regime": parts[15] if len(parts) > 15 else "-",
            })
        except (ValueError, IndexError):
            continue
    return rows


# ─── Counterfactual exit simulation ────────────────────────────────
FEE_PCT = 0.00059  # one-side fee


def cf_pnl_at_R(trade, exit_R):
    """Compute PnL if we exited at +X R from entry.

    PnL_$ = entry × qty × cs × (R × initial_risk_pct × side_sign)
    where initial_risk_pct = abs(entry - sl)/entry.
    Then subtract round-trip fees.
    """
    if trade["initial_risk"] <= 0 or trade["entry"] <= 0:
        return None
    # initial_risk is in PRICE units (the SL distance). Convert to PnL$ per R.
    sign = 1.0 if trade["side"] == "long" else -1.0
    pnl_per_R = trade["initial_risk"] * trade["qty"] * trade["cs"] * sign
    gross = exit_R * pnl_per_R
    # Round-trip taker fees (entry + exit)
    notional_in = trade["entry"] * trade["qty"] * trade["cs"]
    notional_out = (trade["entry"] + exit_R * trade["initial_risk"] * sign) * trade["qty"] * trade["cs"]
    fees = (notional_in + notional_out) * FEE_PCT
    return gross - fees


def cf_exit_at_TP(trade, tp_R):
    """If peak_mfe_r >= tp_R, exit at +tp_R. Otherwise hold until actual exit."""
    if trade["peak_mfe_r"] >= tp_R:
        return cf_pnl_at_R(trade, tp_R), "tp_hit"
    return trade["actual_pnl"], "fell_through"


def cf_exit_at_trail(trade, trigger_R, lock_pct):
    """If peak_mfe_r >= trigger_R, exit at lock_pct × peak_mfe_r.
    Otherwise hold until actual exit."""
    if trade["peak_mfe_r"] >= trigger_R:
        locked_R = lock_pct * trade["peak_mfe_r"]
        return cf_pnl_at_R(trade, locked_R), "trail_hit"
    return trade["actual_pnl"], "fell_through"


def cf_exit_at_max_age(trade, max_age_sec):
    """If actual duration <= max_age, no change. Otherwise approximate
    "would have exited earlier" PnL by the actual_pnl × (max_age / actual_duration)
    — assumes linear value drift, very rough.
    """
    if trade["dur_sec"] <= max_age_sec:
        return trade["actual_pnl"], "no_change"
    # Crude approximation: exit at the midpoint between entry & actual exit
    # by assuming linear price interpolation (this overstates noise, ok for sweep)
    frac = max_age_sec / trade["dur_sec"]
    approx_pnl = trade["actual_pnl"] * frac
    return approx_pnl, "force_max_age"


def cf_exit_at_kill_threshold(trade, kill_R):
    """If actual exit was BELOW kill_R (didn't fire), apply the actual PnL.
    If actual fired EARLIER than kill_R, would have NOT killed at the new
    threshold — approximated by holding until either TP or expiry.
    Simplified for first pass.
    """
    # If trade was a winner (peak_mfe_r > 0.10), kill threshold doesn't matter
    if trade["peak_mfe_r"] >= 0.20:
        return trade["actual_pnl"], "winner_unaffected"
    # For losers, crude assumption: looser kill = held to SL (1R loss)
    pnl_at_sl = cf_pnl_at_R(trade, -1.0)
    return pnl_at_sl if pnl_at_sl else trade["actual_pnl"], "held_to_sl"


# ─── Run sweeps ────────────────────────────────────────────────────
def run_sweep(trades, label, configs, sim_func):
    """Run a single-dimension sweep. configs = list of dicts with config params."""
    results = []
    for cfg in configs:
        total_pnl = 0.0
        n_trades = 0
        n_changed = 0
        for t in trades:
            cf_pnl, change_tag = sim_func(t, **cfg)
            if cf_pnl is None:
                cf_pnl = t["actual_pnl"]
            total_pnl += cf_pnl
            n_trades += 1
            if change_tag not in ("no_change", "fell_through", "winner_unaffected"):
                n_changed += 1
        actual_total = sum(t["actual_pnl"] for t in trades)
        delta = total_pnl - actual_total
        results.append({
            "config": cfg,
            "n": n_trades,
            "n_changed": n_changed,
            "actual_total": actual_total,
            "cf_total": total_pnl,
            "delta": delta,
        })
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="lookback window")
    args = ap.parse_args()

    trades = fetch_trades(args.hours)
    if not trades:
        print(f"No usable trades in last {args.hours}h")
        sys.exit(1)

    actual_total = sum(t["actual_pnl"] for t in trades)
    actual_wins = sum(1 for t in trades if t["actual_pnl"] > 0)
    print(f"Loaded {len(trades)} trades | actual net=${actual_total:.2f} | actual WR={actual_wins/len(trades)*100:.1f}%")

    out_lines = [
        f"# Counterfactual Exit Sweep — {args.hours}h lookback",
        f"Generated: {TS.isoformat()}Z",
        f"",
        f"**Trades analyzed:** {len(trades)}",
        f"**Actual net PnL:** ${actual_total:.2f}",
        f"**Actual WR:** {actual_wins/len(trades)*100:.1f}%",
        f"",
        f"## NOTE — Methodology limits",
        f"- Uses recorded `peak_mfe_r` as proxy for max favorable excursion",
        f"- TP exit: assumes if peak ≥ TP_R, would have hit TP cleanly (best case)",
        f"- Trail exit: assumes lock at lock_pct × peak (matches Phase 5.5 design)",
        f"- max_age exit: linear interpolation of PnL (rough, OK for direction)",
        f"- kill_threshold: simplified to held-to-SL for losers, no change for winners",
        f"- This is DIRECTIONAL only. Phase 2 (live shadow-of-shadow) will be precise.",
        f"",
    ]

    # Sweep 1: TP target
    sweep1 = run_sweep(trades, "TP target",
        [{"tp_R": r} for r in [1.0, 1.5, 2.0, 3.0, 5.0]],
        cf_exit_at_TP,
    )
    out_lines.append("## Sweep 1 — TP target (would-have-hit)")
    out_lines.append("")
    out_lines.append("| TP_R | n_changed | cf_total ($) | Δ vs actual |")
    out_lines.append("|---:|---:|---:|---:|")
    for r in sweep1:
        out_lines.append(f"| {r['config']['tp_R']:.1f}R | {r['n_changed']} | {r['cf_total']:+.2f} | {r['delta']:+.2f} |")
    out_lines.append("")

    # Sweep 2: trail trigger × lock
    out_lines.append("## Sweep 2 — Trail trigger × lock_pct")
    out_lines.append("")
    out_lines.append("| trigger_R | lock_pct | n_changed | cf_total ($) | Δ vs actual |")
    out_lines.append("|---:|---:|---:|---:|---:|")
    for trigger in [0.3, 0.5, 0.7, 1.0]:
        for lock in [0.4, 0.6, 0.8]:
            results = run_sweep(trades, "trail",
                [{"trigger_R": trigger, "lock_pct": lock}],
                cf_exit_at_trail,
            )
            r = results[0]
            out_lines.append(f"| {trigger:.1f} | {lock:.0%} | {r['n_changed']} | {r['cf_total']:+.2f} | {r['delta']:+.2f} |")
    out_lines.append("")

    # Sweep 3: max_age
    sweep3 = run_sweep(trades, "max_age",
        [{"max_age_sec": s} for s in [300, 600, 900, 1800, 3600, 99999]],
        cf_exit_at_max_age,
    )
    out_lines.append("## Sweep 3 — max_age cap (force-close)")
    out_lines.append("")
    out_lines.append("| max_age | n_changed | cf_total ($) | Δ vs actual |")
    out_lines.append("|---:|---:|---:|---:|")
    for r in sweep3:
        sec = r['config']['max_age_sec']
        label = f"{sec//60}min" if sec < 99999 else "NEVER"
        out_lines.append(f"| {label} | {r['n_changed']} | {r['cf_total']:+.2f} | {r['delta']:+.2f} |")
    out_lines.append("")

    # Per-symbol PnL distribution
    out_lines.append("## Actual exit-reason breakdown (for reference)")
    out_lines.append("")
    by_reason = defaultdict(lambda: [0.0, 0])
    for t in trades:
        by_reason[t["actual_reason"]][0] += t["actual_pnl"]
        by_reason[t["actual_reason"]][1] += 1
    out_lines.append("| Reason | n | net ($) | avg |")
    out_lines.append("|---|---:|---:|---:|")
    for r, (pnl, n) in sorted(by_reason.items(), key=lambda x: -x[1][1]):
        out_lines.append(f"| {r} | {n} | {pnl:+.2f} | {pnl/n:+.3f} |")
    out_lines.append("")

    # Per-symbol PnL by peak_mfe_r distribution
    out_lines.append("## peak_mfe_r distribution (helps identify TP target)")
    out_lines.append("")
    buckets = [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.5), (0.5, 0.7),
               (0.7, 1.0), (1.0, 2.0), (2.0, 99)]
    for lo, hi in buckets:
        ts = [t for t in trades if lo <= t["peak_mfe_r"] < hi]
        if not ts:
            continue
        wins = sum(1 for t in ts if t["actual_pnl"] > 0)
        net = sum(t["actual_pnl"] for t in ts)
        out_lines.append(f"- peak {lo:.1f}-{hi:.1f}R: n={len(ts)}, wins={wins}, net=${net:+.2f}")

    # Best config recommendations
    best_tp = max(sweep1, key=lambda r: r["cf_total"])
    best_age = max(sweep3, key=lambda r: r["cf_total"])
    out_lines.extend([
        "",
        "## Top picks (per single-dim sweep)",
        f"- Best TP target: **{best_tp['config']['tp_R']:.1f}R** → cf_total=${best_tp['cf_total']:+.2f} (Δ ${best_tp['delta']:+.2f})",
        f"- Best max_age: **{best_age['config']['max_age_sec']//60}min** → cf_total=${best_age['cf_total']:+.2f} (Δ ${best_age['delta']:+.2f})",
        "",
        "## Recommendation",
        "Run Phase 2 (shadow-of-shadow live mode) to validate these picks with",
        "precise exit simulation — Phase 1 numbers are directional approximations.",
    ])

    out_file = OUT_DIR / f"sweep_{TS.strftime('%Y%m%d_%H%M%S')}.md"
    out_file.write_text("\n".join(out_lines))
    print(f"Wrote: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
