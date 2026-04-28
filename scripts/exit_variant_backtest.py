#!/usr/bin/env python3
"""Multi-variant EXIT backtest — find the continuous-edge unlock.

Tests N exit-config variants vs the current production baseline against
historical paper signals. Goal: identify whether tweaking trail_lock,
dead_kill_R, or stall_kill produces material PnL uplift.

ENGINE LIMITATIONS (be aware):
The replay engine's ConfigurableExitPolicy models these knobs only:
  - max_age_sec, trail_trigger, trail_lock
  - dead_kill_R (kills when current_r < threshold after grace)
  - stall_kill_R (similar, longer grace)
  - tp_R (hard take-profit at peak_mfe_r threshold)

It does NOT model: no_momentum, exhaustion_wick, exhaustion_shrink,
dead_market. Those guards live in the live _monitor_trade and only
fire on real ticks. So this backtest cannot test hypotheses A or D
from the loss-attribution analysis. Those need forward live-pilot data.

USAGE:
  python3 scripts/exit_variant_backtest.py --days 7

OUTPUT:
  storage/backtest_exit/exit_variants_TS.md
"""
import sys
import datetime as datetime_mod
import pathlib
import logging
from datetime import timezone, timedelta

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

OUT_DIR = ROOT / "storage" / "backtest_exit"
TS = datetime_mod.datetime.utcnow()
OUT_DIR.mkdir(parents=True, exist_ok=True)

CACHED_SYMBOLS = {
    "AVAX/USDT", "BTC/USDT", "DOGE/USDT", "ETH/USDT",
    "LINK/USDT", "SOL/USDT", "XRP/USDT",
}

# 8 variants to test. Each ID encodes the change vs baseline.
# All keep max_age_sec=600 (current production cap) and trail_trigger=0.5R
# (already proven by trail-trigger backtest that 0.3R doesn't help).
VARIANTS = [
    # baseline = current production primary
    {"id": "baseline",            "max_age_sec": 600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R": -0.05, "tp_R": None,
     "_note": "current production"},

    # B variants — tighter trail lock (catch retracements earlier)
    {"id": "B_lock_70",           "max_age_sec": 600, "trail_trigger": 0.5, "trail_lock": 0.70,
     "dead_kill_R": -0.10, "stall_kill_R": -0.05, "tp_R": None,
     "_note": "trail_lock 80%→70%"},
    {"id": "B_lock_60",           "max_age_sec": 600, "trail_trigger": 0.5, "trail_lock": 0.60,
     "dead_kill_R": -0.10, "stall_kill_R": -0.05, "tp_R": None,
     "_note": "trail_lock 80%→60%"},

    # C variants — dead_kill aggressiveness
    {"id": "C_kill_05",           "max_age_sec": 600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.05, "stall_kill_R": -0.05, "tp_R": None,
     "_note": "dead_kill -0.10R→-0.05R (kill faster)"},
    {"id": "C_kill_15",           "max_age_sec": 600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.15, "stall_kill_R": -0.05, "tp_R": None,
     "_note": "dead_kill -0.10R→-0.15R (let it breathe)"},

    # E variants — stall guard
    {"id": "E_no_stall",          "max_age_sec": 600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R":  None, "tp_R": None,
     "_note": "remove stall_kill"},

    # F variants — TP cap
    {"id": "F_tp_1R",             "max_age_sec": 600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R": -0.05, "tp_R": 1.0,
     "_note": "hard TP at +1R"},
    {"id": "F_tp_15",             "max_age_sec": 600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R": -0.05, "tp_R": 1.5,
     "_note": "hard TP at +1.5R"},

    # Combos — if individual variants positive, test composition
    {"id": "combo_lock70_kill05", "max_age_sec": 600, "trail_trigger": 0.5, "trail_lock": 0.70,
     "dead_kill_R": -0.05, "stall_kill_R": -0.05, "tp_R": None,
     "_note": "B(lock 70) + C(kill -0.05)"},
    {"id": "combo_lock70_no_stall", "max_age_sec": 600, "trail_trigger": 0.5, "trail_lock": 0.70,
     "dead_kill_R": -0.10, "stall_kill_R":  None, "tp_R": None,
     "_note": "B(lock 70) + E(no stall)"},
]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    from phase3_historical_sweep import make_configurable_exit_policy
    from backtest.execution_replay.engine import load_signals, run_backtest
    from backtest.execution_replay.fill_model import TakerFillModel
    from backtest.execution_replay.metrics import compute_metrics

    signals_path = ROOT / "storage" / "closed_signals.json"
    since = datetime_mod.datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"Loading paper signals last {args.days}d (since {since.isoformat()})...")
    signals = load_signals(signals_path, since=since)
    raw_n = len(signals)

    def _sym(s):
        if isinstance(s, dict):
            return s.get("symbol", "")
        return getattr(s, "symbol", "")
    signals = [s for s in signals if _sym(s) in CACHED_SYMBOLS]
    print(f"  {raw_n} signals → {len(signals)} after cache-symbol filter")
    if not signals:
        print("No signals — abort")
        return 1

    fill_model = TakerFillModel()
    results = []

    for cfg in VARIANTS:
        print(f"\n=== {cfg['id']} — {cfg.get('_note','')} ===")
        try:
            ep = make_configurable_exit_policy(cfg)
            trades = run_backtest(
                signals=signals,
                fill_model=fill_model,
                default_funding_rate_8h=0.0,
                exit_policy=ep,
            )
            metrics = compute_metrics(trades)

            try:
                pnl_col = trades["sim_net_usd"] if "sim_net_usd" in trades.columns else trades.get("pnl_usd")
                wins = int((pnl_col > 0).sum()) if pnl_col is not None else 0
            except Exception:
                wins = 0
            try:
                ep_col = trades["exit_policy_name"] if "exit_policy_name" in trades.columns else None
                replayed = int((ep_col != "historical").sum()) if ep_col is not None else 0
            except Exception:
                replayed = 0

            n = getattr(metrics, "n_trades", 0) or len(trades)
            wr_pct = float(getattr(metrics, "win_rate", 0) or 0) * 100.0
            if wr_pct > 100.0:
                wr_pct = float(getattr(metrics, "win_rate", 0) or 0)

            try:
                if "replayed_exit_reason" in trades.columns:
                    rc = trades["replayed_exit_reason"].value_counts().to_dict()
                else:
                    rc = {}
            except Exception:
                rc = {}

            results.append({
                "cfg_id": cfg["id"], "note": cfg.get("_note", ""),
                "n": n, "wins": wins, "wr": wr_pct,
                "net": getattr(metrics, "total_pnl_usd", 0),
                "avg": getattr(metrics, "avg_pnl_per_trade", 0),
                "pf": getattr(metrics, "profit_factor", 0),
                "max_dd": getattr(metrics, "max_drawdown_usd", 0),
                "replayed": replayed,
                "trail_n": int(rc.get("trail_profit", 0)),
                "td_n": sum(int(rc.get(k, 0)) for k in rc if str(k).startswith("time_decay")),
                "dead_n": int(rc.get("dead_signal_unified", 0)),
                "sl_n": int(rc.get("sl_hit", 0)),
                "tp_n": sum(int(rc.get(k, 0)) for k in rc if "tp_hit" in str(k)),
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            results.append({
                "cfg_id": cfg["id"], "note": cfg.get("_note", ""),
                "n": 0, "wins": 0, "wr": 0, "net": 0, "avg": 0,
                "pf": 0, "max_dd": 0, "replayed": 0,
                "trail_n": 0, "td_n": 0, "dead_n": 0, "sl_n": 0, "tp_n": 0,
                "error": str(e)[:120],
            })

    # Sort by net desc
    results.sort(key=lambda r: -r["net"])
    baseline = next((r for r in results if r["cfg_id"] == "baseline"), None)

    lines = [
        f"# Multi-Variant Exit Backtest — last {args.days}d",
        f"Generated: {TS.isoformat()}Z",
        "",
        f"**Test**: {len(VARIANTS)} variants × {len(signals)} historical paper signals.",
        "",
        "**Engine limitation note**: replay engine models max_age, trail_trigger, "
        "trail_lock, dead_kill_R, stall_kill_R, tp_R. It does NOT model no_momentum, "
        "exhaustion_wick, exhaustion_shrink, or dead_market — those guards live in the "
        "live `_monitor_trade` and only fire on real tick streams. Hypotheses A "
        "(no_momentum) and D (exhaustion threshold) cannot be backtested here.",
        "",
        "## Leaderboard (sorted by Net PnL desc)",
        "",
        "| Config | Note | n | wins | WR | Net | Avg | PF | MaxDD | trail_n | td_n | dead_n | tp_n | sl_n |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        if r.get("error"):
            lines.append(f"| `{r['cfg_id']}` | {r['note']} | ERR | | | | | | | | | | | {r['error']} |")
            continue
        marker = " 🥇" if r is results[0] else (" (baseline)" if r["cfg_id"] == "baseline" else "")
        lines.append(
            f"| `{r['cfg_id']}`{marker} | {r['note']} | {r['n']} | {r['wins']} | "
            f"{r['wr']:.1f}% | ${r['net']:.2f} | ${r['avg']:.3f} | "
            f"{r['pf']:.2f} | ${r['max_dd']:.2f} | "
            f"{r['trail_n']} | {r['td_n']} | {r['dead_n']} | {r['tp_n']} | {r['sl_n']} |"
        )

    if baseline:
        lines.extend(["", "## Per-variant uplift vs baseline", "",
                      "| Config | Net Δ vs baseline | % uplift | trail_n Δ | dead_n Δ | tp_n Δ |",
                      "|---|---:|---:|---:|---:|---:|"])
        for r in results:
            if r["cfg_id"] == "baseline" or r.get("error"):
                continue
            d_net = r["net"] - baseline["net"]
            pct = (d_net / baseline["net"] * 100) if baseline["net"] != 0 else 0
            d_trail = r["trail_n"] - baseline["trail_n"]
            d_dead = r["dead_n"] - baseline["dead_n"]
            d_tp = r["tp_n"] - baseline["tp_n"]
            flag = " 🟢" if d_net > baseline["net"] * 0.1 else (" 🔴" if d_net < -baseline["net"] * 0.1 else "")
            lines.append(
                f"| `{r['cfg_id']}` | ${d_net:+.2f}{flag} | {pct:+.1f}% | "
                f"{d_trail:+d} | {d_dead:+d} | {d_tp:+d} |"
            )

    lines.extend(["", "## Decision rule", "",
                  "- Ship a variant only if Net uplift ≥ +10% AND PF stays ≥ baseline AND MaxDD stays ≤ baseline.",
                  "- For TP variants: also require tp_n > 0 (otherwise the cap never bound, irrelevant test).",
                  "- For combos: if both ingredients independently +ve → combo SHOULD also be +ve. If not → interaction effect.",
                  "",
                  "Engine limitations mean **no_momentum and exhaustion_wick** cannot be tested here. "
                  "If baseline wins this backtest, the next investigation should be the live "
                  "_monitor_trade guards (no_momentum, exhaustion logic). Forward-test via Phase 2 "
                  "leaderboard with v2_10min_no_kill (no dead_kill, no stall) which IS testable forward.",
                 ])

    out_file = OUT_DIR / f"exit_variants_{TS.strftime('%Y%m%d_%H%M%S')}.md"
    out_file.write_text("\n".join(lines))
    print(f"\nWrote: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
