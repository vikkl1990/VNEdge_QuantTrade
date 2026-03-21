#!/usr/bin/env python3
"""
Walk-Forward AUC Validation Script
====================================
Loads candle data, builds features + directional labels, then runs 5-fold
walk-forward TimeSeriesSplit validation with a RandomForest identical to
candidate_trainer.  Reports per-symbol, per-fold, and aggregate OOS AUC.

Usage:
    python -m ml_training.validate_auc
    python -m ml_training.validate_auc --symbols BTC/USDT,ETH/USDT
    python -m ml_training.validate_auc --days 60
    python -m ml_training.validate_auc --fetch   # fetch fresh data from exchange
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ml_training.feature_builder import build_features, build_directional_labels, compute_indicators
from ml_training.candle_collector import CandleCollector, STORAGE_DIR as CANDLE_CACHE_DIR

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────
TRAINING_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT",
    "AVAX/USDT", "LINK/USDT", "DOGE/USDT",
]

TIMEFRAME = "5m"
N_SPLITS = 5
WARMUP_ROWS = 200
MAX_ROWS = 20_000
AUC_PASS_THRESHOLD = 0.55

RESULTS_DIR = PROJECT_ROOT / "storage"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_FILE = RESULTS_DIR / "ml_validation_results.json"

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ── Helpers ─────────────────────────────────────────────────────────

def _color_auc(auc: float) -> str:
    """Return colored AUC string."""
    color = GREEN if auc >= AUC_PASS_THRESHOLD else RED
    return f"{color}{auc:.4f}{RESET}"


def _status(auc: float) -> str:
    if auc >= 0.60:
        return f"{GREEN}PASS (strong){RESET}"
    elif auc >= AUC_PASS_THRESHOLD:
        return f"{GREEN}PASS{RESET}"
    else:
        return f"{RED}FAIL{RESET}"


def load_candles_from_cache(symbol: str, timeframe: str = "5m") -> Optional[pd.DataFrame]:
    """Load candle data from the parquet/csv cache."""
    safe = symbol.replace("/", "_").replace(":", "_")
    pq_path = CANDLE_CACHE_DIR / f"{safe}_{timeframe}.parquet"
    csv_path = CANDLE_CACHE_DIR / f"{safe}_{timeframe}.csv"

    if pq_path.exists():
        try:
            df = pd.read_parquet(pq_path)
            if not df.empty:
                return df
        except Exception:
            pass

    if csv_path.exists():
        try:
            df = pd.read_csv(csv_path, parse_dates=["datetime"])
            df.set_index("datetime", inplace=True)
            if not df.empty:
                return df
        except Exception:
            pass

    return None


async def fetch_candles(symbol: str, timeframe: str = "5m", days: int = 90) -> Optional[pd.DataFrame]:
    """Fetch candle data from exchange (requires exchange connectivity)."""
    try:
        from config.loader import load_config as load_config_dict
        from exchange.factory import create_exchange_client

        config = load_config_dict()
        exchange = create_exchange_client()
        await exchange.connect()

        collector = CandleCollector(exchange, [symbol], [timeframe])
        df = await collector.collect_symbol_tf(symbol, timeframe, days=days)

        try:
            await exchange.close()
        except Exception:
            pass

        return df if df is not None and not df.empty else None
    except Exception as e:
        logger.warning("Failed to fetch %s from exchange: %s", symbol, e)
        return None


def validate_symbol(
    symbol: str,
    df: pd.DataFrame,
    side: str,
) -> Dict:
    """Run walk-forward AUC validation for one symbol + side.

    Returns dict with per-fold AUC and aggregate AUC.
    """
    # Build features
    feat_df = build_features(df)

    # Build directional labels (default mode)
    labels = build_directional_labels(df)

    # For short side, invert labels: we want price to go DOWN
    if side == "short":
        labels = (1 - labels).rename("label")

    # Align features and labels
    combined = feat_df.join(labels, how="inner").dropna()

    # Skip warmup rows
    if len(combined) <= WARMUP_ROWS:
        return {"error": f"Not enough data after warmup ({len(combined)} rows)", "auc": 0.5}

    combined = combined.iloc[WARMUP_ROWS:]

    # Cap rows for memory
    if len(combined) > MAX_ROWS:
        combined = combined.iloc[-MAX_ROWS:]

    X = combined.drop(columns=["label"])
    y = combined["label"]

    # Replace inf/nan
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

    feature_names = list(X.columns)
    n_samples = len(X)
    pos_rate = float(y.mean())

    if len(y.unique()) < 2:
        return {"error": "Only one class present", "auc": 0.5, "n_samples": n_samples}

    # Walk-forward TimeSeriesSplit
    tscv = TimeSeriesSplit(n_splits=N_SPLITS)
    fold_results = []

    for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        if len(y_train.unique()) < 2 or len(y_test) < 10:
            fold_results.append({
                "fold": fold_idx,
                "auc": 0.5,
                "train_size": len(X_train),
                "test_size": len(X_test),
                "skipped": True,
            })
            continue

        # Scale features
        scaler = StandardScaler()
        X_train_sc = scaler.fit_transform(X_train)
        X_test_sc = scaler.transform(X_test)

        # Train RF with same params as candidate_trainer
        clf = RandomForestClassifier(
            n_estimators=50,
            max_depth=6,
            class_weight="balanced",
            min_samples_leaf=5,
            n_jobs=1,
            random_state=42 + fold_idx,
        )
        clf.fit(X_train_sc, y_train)

        probs = clf.predict_proba(X_test_sc)[:, 1]

        try:
            auc = roc_auc_score(y_test, probs)
        except ValueError:
            auc = 0.5

        fold_results.append({
            "fold": fold_idx,
            "auc": round(float(auc), 4),
            "train_size": len(X_train),
            "test_size": len(X_test),
            "test_pos_rate": round(float(y_test.mean()), 4),
            "skipped": False,
        })

    # Aggregate AUC (mean of valid folds)
    valid_aucs = [f["auc"] for f in fold_results if not f.get("skipped")]
    agg_auc = float(np.mean(valid_aucs)) if valid_aucs else 0.5

    return {
        "symbol": symbol,
        "side": side,
        "n_samples": n_samples,
        "n_features": len(feature_names),
        "positive_rate": round(pos_rate, 4),
        "folds": fold_results,
        "aggregate_auc": round(agg_auc, 4),
        "pass": agg_auc >= AUC_PASS_THRESHOLD,
    }


def print_report(results: Dict):
    """Print colored validation report to terminal."""
    print()
    print(f"{BOLD}{CYAN}{'='*70}{RESET}")
    print(f"{BOLD}{CYAN}  VN Edge — Walk-Forward AUC Validation Report{RESET}")
    print(f"{BOLD}{CYAN}{'='*70}{RESET}")
    print()

    all_aucs = []
    failing = []

    for symbol, sides in results.get("by_symbol", {}).items():
        print(f"{BOLD}  {symbol}{RESET}")
        for side, data in sides.items():
            if "error" in data:
                print(f"    {side:>5s}: {RED}ERROR — {data['error']}{RESET}")
                continue

            auc = data["aggregate_auc"]
            all_aucs.append(auc)
            status = _status(auc)
            n = data["n_samples"]
            pos = data["positive_rate"]

            print(f"    {side:>5s}:  AUC={_color_auc(auc)}  {status}  "
                  f"(n={n:,}, pos_rate={pos:.1%})")

            # Per-fold detail
            for f in data.get("folds", []):
                if f.get("skipped"):
                    print(f"           fold {f['fold']}: {YELLOW}skipped{RESET}")
                else:
                    fauc = f["auc"]
                    print(f"           fold {f['fold']}: AUC={_color_auc(fauc)}  "
                          f"(train={f['train_size']:,} test={f['test_size']:,})")

            if not data.get("pass"):
                failing.append(f"{symbol} {side}")

        print()

    # Aggregate summary
    print(f"{BOLD}{CYAN}{'─'*70}{RESET}")
    if all_aucs:
        mean_auc = np.mean(all_aucs)
        median_auc = np.median(all_aucs)
        min_auc = np.min(all_aucs)
        max_auc = np.max(all_aucs)

        print(f"  {BOLD}Aggregate AUC:{RESET}  mean={_color_auc(mean_auc)}  "
              f"median={_color_auc(median_auc)}  "
              f"min={_color_auc(min_auc)}  max={_color_auc(max_auc)}")
        print(f"  {BOLD}Models tested:{RESET}  {len(all_aucs)}")
        print(f"  {BOLD}Passing (>={AUC_PASS_THRESHOLD}):{RESET}  "
              f"{sum(1 for a in all_aucs if a >= AUC_PASS_THRESHOLD)}/{len(all_aucs)}")

        if failing:
            print()
            print(f"  {RED}{BOLD}FAILING models (AUC < {AUC_PASS_THRESHOLD}):{RESET}")
            for f in failing:
                print(f"    {RED}  - {f}{RESET}")
        else:
            print(f"\n  {GREEN}{BOLD}All models PASSING.{RESET}")
    else:
        print(f"  {RED}No valid results to report.{RESET}")

    print(f"{BOLD}{CYAN}{'='*70}{RESET}")
    print()


async def run_validation(
    symbols: Optional[List[str]] = None,
    days: int = 90,
    do_fetch: bool = False,
) -> Dict:
    """Run the full validation pipeline."""
    symbols = symbols or TRAINING_SYMBOLS
    start_time = time.time()

    print(f"\n{CYAN}Starting walk-forward AUC validation...{RESET}")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Timeframe: {TIMEFRAME}")
    print(f"  Folds: {N_SPLITS}")
    print(f"  Max rows: {MAX_ROWS:,}")
    print(f"  Pass threshold: AUC >= {AUC_PASS_THRESHOLD}")
    print()

    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "symbols": symbols,
            "timeframe": TIMEFRAME,
            "n_splits": N_SPLITS,
            "warmup_rows": WARMUP_ROWS,
            "max_rows": MAX_ROWS,
            "pass_threshold": AUC_PASS_THRESHOLD,
        },
        "by_symbol": {},
    }

    for symbol in symbols:
        print(f"  {CYAN}Processing {symbol}...{RESET}")

        # Load candle data
        df = load_candles_from_cache(symbol, TIMEFRAME)

        if df is None and do_fetch:
            print(f"    No cache found, fetching from exchange...")
            df = await fetch_candles(symbol, TIMEFRAME, days=days)

        if df is None or df.empty:
            print(f"    {RED}No data available for {symbol}. "
                  f"Run training pipeline first or use --fetch.{RESET}")
            results["by_symbol"][symbol] = {
                "long": {"error": "No candle data available", "auc": 0.5},
                "short": {"error": "No candle data available", "auc": 0.5},
            }
            continue

        print(f"    Loaded {len(df):,} candles")

        # Validate long and short separately
        symbol_results = {}
        for side in ["long", "short"]:
            print(f"    Validating {side}...")
            try:
                res = validate_symbol(symbol, df, side)
                symbol_results[side] = res
                auc = res.get("aggregate_auc", 0.5)
                status = "PASS" if res.get("pass") else "FAIL"
                color = GREEN if res.get("pass") else RED
                print(f"      {color}{status}: AUC = {auc:.4f}{RESET}")
            except Exception as e:
                logger.exception("Error validating %s %s: %s", symbol, side, e)
                symbol_results[side] = {"error": str(e), "auc": 0.5}
                print(f"      {RED}ERROR: {e}{RESET}")

        results["by_symbol"][symbol] = symbol_results

    # Compute aggregate stats
    all_aucs = []
    for sym_data in results["by_symbol"].values():
        for side_data in sym_data.values():
            if "error" not in side_data:
                all_aucs.append(side_data["aggregate_auc"])

    results["aggregate"] = {
        "mean_auc": round(float(np.mean(all_aucs)), 4) if all_aucs else 0.5,
        "median_auc": round(float(np.median(all_aucs)), 4) if all_aucs else 0.5,
        "min_auc": round(float(np.min(all_aucs)), 4) if all_aucs else 0.5,
        "max_auc": round(float(np.max(all_aucs)), 4) if all_aucs else 0.5,
        "n_models": len(all_aucs),
        "n_passing": sum(1 for a in all_aucs if a >= AUC_PASS_THRESHOLD),
        "n_failing": sum(1 for a in all_aucs if a < AUC_PASS_THRESHOLD),
        "all_pass": all(a >= AUC_PASS_THRESHOLD for a in all_aucs) if all_aucs else False,
    }

    elapsed = time.time() - start_time
    results["elapsed_seconds"] = round(elapsed, 1)

    # Save results
    try:
        RESULTS_FILE.write_text(json.dumps(results, indent=2, default=str))
        print(f"\n  Results saved to {RESULTS_FILE}")
    except Exception as e:
        logger.error("Failed to save results: %s", e)

    # Print report
    print_report(results)

    return results


def parse_args():
    parser = argparse.ArgumentParser(description="Walk-forward AUC validation")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated symbols (default: all 6 training symbols)")
    parser.add_argument("--days", type=int, default=90,
                        help="Days of data to fetch if --fetch is used (default: 90)")
    parser.add_argument("--fetch", action="store_true",
                        help="Fetch data from exchange if cache is empty")
    return parser.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    args = parse_args()
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None

    asyncio.run(run_validation(
        symbols=symbols,
        days=args.days,
        do_fetch=args.fetch,
    ))


if __name__ == "__main__":
    main()
