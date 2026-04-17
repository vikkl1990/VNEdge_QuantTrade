#!/usr/bin/env python3
"""
diagnose_scanners.py — Offline scanner replay for the Research Lab.

Goal: Count theoretical scanner trigger rates by replaying each `_scan_*`
function on historical candles in storage/candle_cache/. Compares against
the production trigger counts from scanner_funnel.jsonl to pinpoint where
signals die (scanner code too strict vs downstream filter stack too strict).

Output: storage/research/scanner_offline_replay.json consumed by
Research Center's scanner_offline_diagnosis() function and exposed to the
/research UI.

Safe: READ-ONLY. Does not hit any exchange, does not touch live bot state,
does not modify any signal_tracker or scalp_strategy hot-path.

Runs from cron on the ML VM (daily) or manually:
  python3 scripts/diagnose_scanners.py [--days 30] [--symbols BTC/USDT,ETH/USDT]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("diagnose_scanners")

STORAGE = PROJECT_ROOT / "storage"
CANDLE_CACHE = STORAGE / "candle_cache"
RESEARCH_DIR = STORAGE / "research"
OUTPUT_FILE = RESEARCH_DIR / "scanner_offline_replay.json"
FUNNEL_FILE = RESEARCH_DIR / "scanner_funnel.jsonl"


def load_candles_for(symbol: str, tf: str = "5m") -> "pd.DataFrame | None":
    """Load cached candles for a symbol. Returns None if not found."""
    import pandas as pd
    # Try a few common path conventions the bot uses for candle caching
    safe = symbol.replace("/", "_")
    candidates = [
        CANDLE_CACHE / f"{safe}_{tf}.json",
        CANDLE_CACHE / f"{safe}_{tf}.csv",
        CANDLE_CACHE / f"{safe.lower()}_{tf}.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            if p.suffix == ".csv":
                df = pd.read_csv(p)
            else:
                with open(p) as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    df = pd.DataFrame(data)
                else:
                    df = pd.DataFrame(data.get("candles", []))
            if df.empty:
                continue
            # Normalise columns
            col_map = {c.lower(): c for c in df.columns}
            if "timestamp" in col_map:
                df["ts"] = pd.to_datetime(df[col_map["timestamp"]], unit="ms", errors="coerce")
            elif "time" in col_map:
                df["ts"] = pd.to_datetime(df[col_map["time"]], errors="coerce")
            return df
        except Exception as e:
            logger.warning("Failed to load %s: %s", p, e)
    return None


def run_one_scanner(strategy, scanner_method, df, htf_bias: int, confirm_bias: int) -> int:
    """Replay a single scanner across the df, counting trigger hits.

    CRITICAL: scanners depend on pre-computed indicators (RSI, VWAP, EMA,
    BB, ATR, supertrend, etc.) as df columns. The strategy's
    _compute_indicators() attaches them. We enrich the full df once, then
    walk-forward on slices — much faster than recomputing per slice.

    Walks forward from bar 60 to end, calling scanner on progressively-
    larger slices so the scanner sees the same context it would live."""
    hits = 0
    # For performance, step by 5 bars on 5m frame (= every 25 min)
    step = 5
    start = 60  # warmup
    symbol = "DIAG/USDT"  # scanners use symbol only for logging
    for i in range(start, len(df), step):
        slice_df = df.iloc[:i].copy()
        try:
            result = scanner_method(symbol, slice_df, htf_bias, confirm_bias)
        except Exception:
            continue
        if result is not None:
            hits += 1
    return hits


def enrich_with_indicators(strat, df):
    """Ensure the df has all indicator columns the scanners expect.
    Uses the strategy's own _compute_indicators() for fidelity — SAME code
    the live bot uses."""
    try:
        enriched = strat._compute_indicators(df)
        return enriched if enriched is not None else df
    except Exception as e:
        logger.warning("_compute_indicators failed: %s — running without indicators", e)
        return df


def aggregate_funnel_production(days: int = 7) -> dict:
    """Re-count production triggers from scanner_funnel.jsonl for cross-reference."""
    if not FUNNEL_FILE.exists():
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    attempts = Counter()
    triggered = Counter()
    try:
        with open(FUNNEL_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts_str = rec.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                for r in rec.get("results") or []:
                    scn = r.get("scanner", "?")
                    attempts[scn] += 1
                    if r.get("triggered"):
                        triggered[scn] += 1
    except Exception:
        pass
    return {s: {"attempts": attempts[s], "triggered": triggered[s]} for s in attempts}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Candle history window")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated (default: BTC/USDT,ETH/USDT,SOL/USDT)")
    parser.add_argument("--tf", type=str, default="5m", help="Timeframe to replay on")
    args = parser.parse_args()

    symbols = [s.strip() for s in (args.symbols or "BTC/USDT,ETH/USDT,SOL/USDT").split(",") if s.strip()]
    logger.info("Starting offline replay: symbols=%s tf=%s days=%d", symbols, args.tf, args.days)

    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

    # Import strategy — may require config + deps. We instantiate with minimal
    # stub if possible; else skip with a clear note.
    try:
        from strategies.scalp_strategy import ScalpStrategy
    except Exception as e:
        logger.error("Failed to import ScalpStrategy: %s", e)
        _write_error("import_failure", str(e))
        return 1

    # Build a stub strategy instance. ScalpStrategy.__init__ might require config.
    try:
        # Try with empty config dict first
        strat = ScalpStrategy({})
    except Exception:
        try:
            # Try default constructor
            strat = ScalpStrategy.__new__(ScalpStrategy)
            # Set minimum attributes it might poke
            import pandas as pd
            strat._atr_ratio = 1.0
            strat._scanner_cofire_log = []
            strat._funnel = Counter()
            strat._weight_manager = None
            strat.logger = logger
        except Exception as e:
            logger.error("Failed to construct ScalpStrategy stub: %s", e)
            _write_error("construct_failure", str(e))
            return 1

    # Discover scanner methods on the strategy
    scanner_methods = {}
    for attr in dir(strat):
        if attr.startswith("_scan_"):
            m = getattr(strat, attr, None)
            if callable(m):
                scanner_methods[attr.replace("_scan_", "")] = m
    logger.info("Found %d scanner methods", len(scanner_methods))

    # Production counts for cross-reference
    production = aggregate_funnel_production(days=7)

    # Replay per symbol
    by_scanner_total = defaultdict(int)
    by_scanner_symbol = defaultdict(dict)
    bars_replayed = 0
    symbols_loaded = 0

    for symbol in symbols:
        df = load_candles_for(symbol, args.tf)
        if df is None or len(df) < 100:
            logger.warning("No cached candles for %s %s (or too few) — skip", symbol, args.tf)
            continue
        symbols_loaded += 1
        # Trim to window (normalise tz for comparison)
        if "ts" in df.columns:
            import pandas as pd
            cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
            try:
                col = pd.to_datetime(df["ts"], errors="coerce", utc=True)
                cutoff_aware = pd.Timestamp(cutoff).tz_convert("UTC") if pd.Timestamp(cutoff).tz is not None else pd.Timestamp(cutoff).tz_localize("UTC")
                mask = col >= cutoff_aware
                df = df[mask.fillna(False)].reset_index(drop=True)
            except Exception as e:
                logger.warning("%s: ts-filter failed (%s) — using full df", symbol, e)
        if len(df) < 100:
            logger.warning("%s: too few candles in window", symbol)
            continue

        # Enrich with indicators once (same code as live bot)
        df = enrich_with_indicators(strat, df)
        bars_replayed += len(df)
        logger.info("%s: replaying %d bars (indicator cols=%d)", symbol, len(df), len(df.columns))

        for name, m in scanner_methods.items():
            try:
                hits = run_one_scanner(strat, m, df, htf_bias=0, confirm_bias=0)
            except Exception as e:
                logger.warning("%s/%s failed: %s", symbol, name, e)
                continue
            by_scanner_total[name] += hits
            by_scanner_symbol[name][symbol] = hits

    # Build output
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.days,
        "timeframe": args.tf,
        "symbols_loaded": symbols_loaded,
        "bars_replayed": bars_replayed,
        "scanners": {},
        "summary": {},
    }

    for name in scanner_methods:
        prod = production.get(name, {"attempts": 0, "triggered": 0})
        theoretical_hits = by_scanner_total.get(name, 0)
        prod_triggers = prod["triggered"]

        # Interpretation
        if theoretical_hits == 0 and prod_triggers == 0:
            interpretation = "SILENT_EVERYWHERE — scanner code never fires on historical candles; logic likely too tight"
            mode_hint = "MODE_1"
        elif theoretical_hits > 0 and prod_triggers == 0:
            interpretation = f"OFFLINE_FIRES_PROD_DEAD — {theoretical_hits} offline hits vs 0 production triggers; downstream filters (regime/VWAP/ATR/session) killing everything"
            mode_hint = "MODE_2"
        elif theoretical_hits > 0 and prod_triggers > 0:
            ratio = prod_triggers / theoretical_hits if theoretical_hits else 0
            interpretation = f"HEALTHY — {prod_triggers}/{theoretical_hits} = {ratio:.1%} of offline hits make it through filters"
            mode_hint = "HEALTHY"
        else:
            interpretation = f"UNCLEAR — offline={theoretical_hits}, production_attempts={prod['attempts']}, triggered={prod_triggers}"
            mode_hint = "UNKNOWN"

        out["scanners"][name] = {
            "theoretical_hits": theoretical_hits,
            "by_symbol": by_scanner_symbol.get(name, {}),
            "production_attempts_7d": prod["attempts"],
            "production_triggered_7d": prod_triggers,
            "interpretation": interpretation,
            "mode_hint": mode_hint,
        }

    out["summary"] = {
        "total_scanners": len(scanner_methods),
        "mode_counts": dict(Counter(s["mode_hint"] for s in out["scanners"].values())),
    }

    # Atomic write
    tmp = OUTPUT_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    tmp.replace(OUTPUT_FILE)
    logger.info("Wrote %s (%d scanners analysed)", OUTPUT_FILE, len(scanner_methods))

    # Print a readable summary
    print(f"\n=== OFFLINE SCANNER REPLAY ({symbols_loaded} symbols, {bars_replayed} bars) ===\n")
    for name, s in sorted(out["scanners"].items(), key=lambda kv: -kv[1]["theoretical_hits"]):
        print(f"  {name:22s}  offline={s['theoretical_hits']:4d}  "
              f"prod_attempts={s['production_attempts_7d']:4d}  "
              f"prod_triggers={s['production_triggered_7d']:3d}  "
              f"[{s['mode_hint']}]")
    print()
    return 0


def _write_error(kind: str, msg: str):
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as fh:
        json.dump({
            "error": kind,
            "message": msg,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scanners": {},
            "summary": {},
        }, fh, indent=2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        _write_error("unhandled_exception", traceback.format_exc())
        sys.exit(1)
