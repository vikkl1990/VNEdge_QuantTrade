#!/usr/bin/env python3
"""Phase 3 — Historical Backtest Sweep.

Leverages the existing backtest/execution_replay/ engine to replay paper
signals from the last N days through ANY exit configuration. Wraps the
Phase 2 EXIT_CONFIGS list so the same configs can be tested against
HISTORICAL data for fast iteration (no need to wait 24-48h for forward).

Phase 1 was approximate (peak_mfe_r proxy).
Phase 2 is precise but slow (live forward-test).
Phase 3 is precise AND fast (historical replay over 1m candle history).

The 3 phases triangulate. If all three agree on a winning config, ship it.

USAGE:
  python3 scripts/phase3_historical_sweep.py [--days 7]

OUTPUTS:
  storage/phase3/historical_sweep_TS.md
"""
import sys
import argparse
import datetime
import pathlib
import logging
from datetime import timezone, timedelta

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "storage" / "phase3"
TS = datetime.datetime.utcnow()
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Symbols with full 1m parquet cache locally (47MB cache covers these).
# Phase 3 replays only these — sidesteps the broken REST candle fetch
# (DeltaRestClient.request signature changed in 5.3, no longer accepts
# `params` kwarg in candle_cache.fetch_candles_rest at line 108).
# These also happen to be the symbols the bot trades the most, so the
# sweep stays representative.
CACHED_SYMBOLS = {
    "AVAX/USDT", "BTC/USDT", "DOGE/USDT", "ETH/USDT",
    "LINK/USDT", "SOL/USDT", "XRP/USDT",
}

# Reuse Phase 2 configs verbatim — same exit profiles, historical replay
PHASE2_EXIT_CONFIGS = [
    {"id": "primary",          "max_age_sec":  600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R": -0.05, "tp_R": None},
    {"id": "v1_5min_tight",    "max_age_sec":  300, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R":  None, "tp_R": None},
    {"id": "v2_10min_no_kill", "max_age_sec":  600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R":  None, "stall_kill_R":  None, "tp_R": None},
    {"id": "v3_30min_paper",   "max_age_sec": 1800, "trail_trigger": 0.3, "trail_lock": 0.50,
     "dead_kill_R":  None, "stall_kill_R":  None, "tp_R": None},
    {"id": "v4_60min_unrest",  "max_age_sec": 3600, "trail_trigger": 0.7, "trail_lock": 0.80,
     "dead_kill_R":  None, "stall_kill_R":  None, "tp_R": 2.0},
    # 2026-04-28 Path 2 — v6_tp_15R from exit_variant_backtest (5.2% Pareto-better).
    {"id": "v6_tp_15R",        "max_age_sec":  600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill_R": -0.10, "stall_kill_R": -0.05, "tp_R": 1.5},
]


def make_configurable_exit_policy(cfg):
    """Build a callable ExitPolicy object from a Phase 2 config dict.

    Mirrors the Phase 2 _monitor_trade logic:
      - hard time decay at cfg.max_age_sec
      - trail engages at peak_mfe_r >= cfg.trail_trigger, locks at cfg.trail_lock × peak
      - dead_kill_R: if not None, kills when current_r < cfg.dead_kill_R after grace
      - tp_R: if not None, exits at peak >= cfg.tp_R
    """
    # Late import — engine loads heavy deps (pandas) only when sweep actually runs
    from backtest.execution_replay.exit_policy import TradeState

    class ConfigurableExitPolicy:
        def __init__(self):
            self.cfg = cfg
            self.name = cfg["id"]

        def should_exit(self, state, candle):
            """Check if trade should exit at this candle. Returns reason string or None."""
            # 2026-04-27 fix — TradeState exposes age_sec (engine-updated each
            # tick), not entry_time_unix. peak_mfe_r is also engine-managed.
            age_sec = float(getattr(state, "age_sec", 0) or 0)
            high = float(candle.get("high", 0))
            low = float(candle.get("low", 0))
            close = float(candle.get("close", 0))
            risk = state.initial_risk if state.initial_risk > 0 else abs(state.entry_price - state.stop_loss)
            if risk <= 0:
                return None

            # Compute trough this bar for trail-retest. Engine already
            # advances state.peak_mfe_r each tick — we just read it.
            if state.side == "long":
                bar_trough = (low - state.entry_price) / risk
            else:
                bar_trough = (state.entry_price - high) / risk

            # 1. SL hit (uses bar low/high — same convention as engine)
            if state.side == "long" and low <= state.stop_loss:
                return "sl_hit"
            if state.side == "short" and high >= state.stop_loss:
                return "sl_hit"

            # 2. TP target (Phase 2 v4 only)
            if self.cfg.get("tp_R") is not None and state.peak_mfe_r >= self.cfg["tp_R"]:
                return f"phase2_tp_hit_{self.cfg['tp_R']}R"

            # 3. Trail engaged → check if current_r dropped below lock
            if state.peak_mfe_r >= self.cfg["trail_trigger"]:
                lock_r = state.peak_mfe_r * self.cfg["trail_lock"]
                cur_r = bar_trough  # use bar low (long) / bar high (short) for retest
                if cur_r <= lock_r:
                    # Compute exit price at locked SL level
                    if state.side == "long":
                        new_sl = state.entry_price + lock_r * risk
                    else:
                        new_sl = state.entry_price - lock_r * risk
                    state.stop_loss = new_sl  # mutates so engine prices the exit at new_sl
                    return "trail_profit"

            # 4. Dead-signal kill (Phase 2 primary, v1 only)
            if self.cfg.get("dead_kill_R") is not None:
                # Crude: check if past 60s grace AND current_r below kill threshold
                if age_sec > 60:
                    cur_r = (close - state.entry_price) / risk if state.side == "long" else (state.entry_price - close) / risk
                    if cur_r < self.cfg["dead_kill_R"] and state.peak_mfe_r < 0.15:
                        return "dead_signal_unified"

            # 5. Max age
            if age_sec >= self.cfg["max_age_sec"]:
                return f"time_decay_{int(age_sec // 60)}m"

            return None

    return ConfigurableExitPolicy()


def run_sweep(days):
    from backtest.execution_replay.engine import load_signals, run_backtest
    from backtest.execution_replay.fill_model import TakerFillModel
    from backtest.execution_replay.metrics import compute_metrics

    signals_path = ROOT / "storage" / "closed_signals.json"
    since = datetime.datetime.now(timezone.utc) - timedelta(days=days)
    print(f"Loading paper signals from last {days}d (since {since.isoformat()})...")
    signals = load_signals(signals_path, since=since)
    raw_n = len(signals)
    # Filter to symbols with local 1m parquet cache so the candle-walk
    # exit_policy can actually fire (no REST roundtrip required).
    def _sym(s):
        # signal dict shape: {"symbol": "BTC/USDT", ...}
        if isinstance(s, dict):
            return s.get("symbol", "")
        return getattr(s, "symbol", "")
    signals = [s for s in signals if _sym(s) in CACHED_SYMBOLS]
    print(f"  Loaded {raw_n} signals → {len(signals)} after cache-symbol filter")
    print(f"  Cached symbols: {sorted(CACHED_SYMBOLS)}")
    if not signals:
        print("No signals after filter — nothing to sweep")
        return []

    fill_model = TakerFillModel()
    results = []

    for cfg in PHASE2_EXIT_CONFIGS:
        print(f"\n=== Running config: {cfg['id']} ===")
        try:
            exit_policy = make_configurable_exit_policy(cfg)
            trades = run_backtest(
                signals=signals,
                fill_model=fill_model,
                default_funding_rate_8h=0.0,
                exit_policy=exit_policy,
            )
            metrics = compute_metrics(trades)
            # 2026-04-27 fix — run_backtest returns pd.DataFrame, not list.
            # Iterating it yields column NAMES (strings), so getattr always
            # falls to the default → wins=0 / replayed=0 for every config
            # even though aggregates were correct. Use the column directly.
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
            # win_rate is already a fraction (0.0-1.0); pct = ×100. Earlier bug
            # double-counted because wins=0 made the displayed WR wrap to (wins/n)
            # over a tiny denominator after metrics.win_rate × 100.
            wr_pct = float(getattr(metrics, "win_rate", 0) or 0) * 100.0
            # Sanity guard: if metrics.win_rate looks already-pct (>1.0), don't ×100.
            if wr_pct > 100.0:
                wr_pct = float(getattr(metrics, "win_rate", 0) or 0)
            results.append({
                "cfg_id": cfg["id"],
                "n": n,
                "wins": wins,
                "wr": wr_pct,
                "net": getattr(metrics, "total_pnl_usd", 0),
                "avg": getattr(metrics, "avg_pnl_per_trade", 0),
                "pf": getattr(metrics, "profit_factor", 0),
                "max_dd": getattr(metrics, "max_drawdown_usd", 0),
                "replayed": replayed,
            })
        except Exception as e:
            print(f"  ERROR for {cfg['id']}: {e}")
            results.append({
                "cfg_id": cfg["id"], "n": 0, "wins": 0, "wr": 0,
                "net": 0, "avg": 0, "pf": 0, "max_dd": 0,
                "error": str(e)[:100],
            })

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    results = run_sweep(args.days)
    if not results:
        print("No results.")
        sys.exit(1)

    # Sort by net PnL desc
    results.sort(key=lambda r: -r["net"])

    lines = [
        f"# Phase 3 Historical Backtest Sweep — last {args.days}d",
        f"Generated: {TS.isoformat()}Z",
        "",
        f"**Configs tested:** {len(results)}",
        "",
        "## Leaderboard (sorted by net PnL desc)",
        "",
        "**`replayed`** = trades where the candle-walk exit_policy actually fired",
        "(not just historical fallback). If 0, candles weren't loaded for any signal —",
        "results identical across configs because only historical exit was used.",
        "",
        "| Config | n | replayed | wins | WR | Net | Avg | PF | MaxDD | Error |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        err = r.get("error", "")
        lines.append(
            f"| `{r['cfg_id']}` | {r['n']} | {r.get('replayed', 0)} | {r['wins']} | "
            f"{r.get('wr', 0):.1f}% | "
            f"${r['net']:.2f} | ${r['avg']:.3f} | "
            f"{r['pf']:.2f} | ${r['max_dd']:.2f} | {err} |"
        )

    if results:
        best = results[0]
        lines.extend([
            "",
            f"## 🏆 Best historical config: **{best['cfg_id']}**",
            f"- Net: ${best['net']:.2f}",
            f"- PF: {best['pf']:.2f}",
            f"- WR: {best.get('wr', 0):.1f}%",
            f"- n: {best['n']} trades",
            "",
            "## Cross-check with Phase 2 forward test",
            "Run `python3 scripts/phase2_leaderboard.py --hours 24` after 24h",
            "of forward-test data. If both phases agree on the same winner,",
            "promote that config to PRIMARY in user_real_manager.py.",
        ])

    out_file = OUT_DIR / f"historical_sweep_{TS.strftime('%Y%m%d_%H%M%S')}.md"
    out_file.write_text("\n".join(lines))
    print(f"\nWrote: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
