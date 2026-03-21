#!/usr/bin/env python3
"""
Latency Measurement Tool -- Binance vs Delta India
===================================================
Measures price dislocation between Binance (leader) and Delta India (follower).
Run this FIRST to prove the edge exists before enabling live trading.

Usage:
    python scripts/measure_latency.py --duration 30 --symbols BTC/USDT,ETH/USDT

Output:
    - Live stats every 30 seconds to stdout
    - Full report at the end
    - Raw data saved to storage/latency_measurements.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so imports work when running
# directly as ``python scripts/measure_latency.py``
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from strategies.latency_arb import LatencyArbEngine, Dislocation  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("measure_latency")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STORAGE_DIR = _PROJECT_ROOT / "storage"
OUTPUT_FILE = STORAGE_DIR / "latency_measurements.jsonl"
STATS_INTERVAL_S = 30  # print stats every N seconds

DISLOCATION_BUCKETS = [0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 1.00]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_pct(val: float) -> str:
    return f"{val:.4f}%"


def _print_separator(char: str = "=", width: int = 72):
    print(char * width)


def _print_live_stats(engine: LatencyArbEngine, elapsed_s: float):
    """Print a compact live stats line to stdout."""
    stats = engine.get_stats()
    print()
    _print_separator("-", 72)
    ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
    print(f"[{ts}]  Elapsed: {elapsed_s:.0f}s  |  "
          f"Binance msgs: {stats['binance_msgs']}  |  "
          f"Delta msgs: {stats['delta_msgs']}  |  "
          f"Signals: {stats['signals_generated']}")

    for symbol in engine.symbols:
        avg_d = stats.get("avg_dislocation_pct", {}).get(symbol)
        max_d = stats.get("max_dislocation_pct", {}).get(symbol)
        avg_l = stats.get("avg_latency_ms", {}).get(symbol)
        trade_pct = stats.get(f"tradeable_pct_{symbol}")

        if avg_d is not None:
            print(
                f"  {symbol:12s}  avg_disl={avg_d:.4f}%  "
                f"max_disl={max_d:.4f}%  "
                f"avg_latency={avg_l:.0f}ms  "
                f"tradeable={trade_pct:.1f}%"
            )
        else:
            print(f"  {symbol:12s}  (no data yet)")
    _print_separator("-", 72)


def _print_final_report(engine: LatencyArbEngine, duration_s: float):
    """Print a comprehensive final report."""
    stats = engine.get_stats()
    actual_uptime = stats.get("uptime_s", duration_s)

    print()
    _print_separator("=", 72)
    print("           LATENCY MEASUREMENT REPORT")
    print(f"           {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    _print_separator("=", 72)

    print(f"\nDuration:        {actual_uptime:.0f}s ({actual_uptime / 60:.1f} min)")
    print(f"Binance msgs:    {stats['binance_msgs']:,}")
    print(f"Delta msgs:      {stats['delta_msgs']:,}")
    print(f"Signals (>0.20%): {stats['signals_generated']}")
    print(f"Dislocations:    {stats['dislocations_detected']}")

    for symbol in engine.symbols:
        history = engine.get_dislocation_history(symbol)
        if not history:
            print(f"\n--- {symbol}: NO DATA ---")
            continue

        disls = np.array([abs(d.dislocation_pct) for d in history])
        lats = np.array([d.latency_ms for d in history])
        signed = np.array([d.dislocation_pct for d in history])

        print(f"\n{'=' * 72}")
        print(f"  {symbol}")
        print(f"{'=' * 72}")
        print(f"  Samples:        {len(disls):,}")
        print(f"  Avg dislocation: {np.mean(disls):.4f}%")
        print(f"  P50 dislocation: {np.median(disls):.4f}%")
        print(f"  P75 dislocation: {np.percentile(disls, 75):.4f}%")
        print(f"  P90 dislocation: {np.percentile(disls, 90):.4f}%")
        print(f"  P95 dislocation: {np.percentile(disls, 95):.4f}%")
        print(f"  P99 dislocation: {np.percentile(disls, 99):.4f}%")
        print(f"  Max dislocation: {np.max(disls):.4f}%")
        print(f"  Std dev:         {np.std(disls):.4f}%")
        print(f"  Mean signed:     {np.mean(signed):+.4f}%  "
              f"(+ve = Binance > Delta)")

        print(f"\n  Avg latency:     {np.mean(lats):.1f}ms")
        print(f"  P50 latency:     {np.median(lats):.1f}ms")
        print(f"  P95 latency:     {np.percentile(lats, 95):.1f}ms")
        print(f"  Max latency:     {np.max(lats):.1f}ms")

        # Bucket analysis
        print(f"\n  Dislocation frequency:")
        for threshold in DISLOCATION_BUCKETS:
            count = int(np.sum(disls >= threshold))
            pct = count / len(disls) * 100.0
            print(f"    >= {threshold:.2f}%:  {count:>6,} ({pct:>5.1f}%)")

        # Estimated signals and profit
        if actual_uptime > 0:
            signals_per_hour = stats["signals_generated"] / (actual_uptime / 3600.0)
            signals_per_day = signals_per_hour * 24.0

            # Avg dislocation of tradeable events
            tradeable_disls = disls[disls >= 0.20]
            if len(tradeable_disls) > 0:
                avg_tradeable = float(np.mean(tradeable_disls))
                cost_per_trade = 0.14  # % round-trip
                avg_net_edge = avg_tradeable - cost_per_trade

                print(f"\n  --- Estimated Edge ---")
                print(f"  Signals/hour:        {signals_per_hour:.1f}")
                print(f"  Signals/day:         {signals_per_day:.0f}")
                print(f"  Avg tradeable disl:  {avg_tradeable:.4f}%")
                print(f"  Estimated cost:      {cost_per_trade:.2f}%")
                print(f"  Avg net edge:        {avg_net_edge:.4f}%")
                if avg_net_edge > 0:
                    daily_pct = signals_per_day * avg_net_edge
                    print(f"  Est. daily profit:   {daily_pct:.2f}% "
                          f"(on position size, before compounding)")
                else:
                    print(f"  Est. daily profit:   NEGATIVE (edge < cost)")
            else:
                print(f"\n  No tradeable dislocations (>= 0.20%) detected.")

    _print_separator("=", 72)


def _save_raw_data(engine: LatencyArbEngine, output_path: Path):
    """Save all dislocation history to a JSONL file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with open(output_path, "a") as f:
        for symbol in engine.symbols:
            for d in engine.get_dislocation_history(symbol):
                record = {
                    "symbol": d.symbol,
                    "binance_mid": round(d.binance_mid, 4),
                    "delta_mid": round(d.delta_mid, 4),
                    "dislocation_pct": round(d.dislocation_pct, 6),
                    "dislocation_abs": round(d.dislocation_abs, 4),
                    "direction": d.direction,
                    "latency_ms": round(d.latency_ms, 2),
                    "binance_ts": d.binance_ts,
                    "delta_ts": d.delta_ts,
                    "timestamp": d.timestamp,
                    "time_utc": datetime.fromtimestamp(
                        d.timestamp, tz=timezone.utc
                    ).isoformat(),
                }
                f.write(json.dumps(record) + "\n")
                count += 1

    logger.info("Saved %d dislocation records to %s", count, output_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_measurement(duration_minutes: float, symbols: list[str]):
    """Run the latency measurement for a fixed duration."""
    duration_s = duration_minutes * 60.0
    engine = LatencyArbEngine(symbols=symbols)

    # Handle graceful shutdown
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Interrupt received -- stopping measurement...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    print()
    _print_separator("=", 72)
    print(f"  Latency Measurement: Binance vs Delta India")
    print(f"  Symbols:  {', '.join(symbols)}")
    print(f"  Duration: {duration_minutes:.0f} minutes")
    print(f"  Output:   {OUTPUT_FILE}")
    _print_separator("=", 72)
    print("  Connecting to websockets...")
    print()

    # Start the engine in a task
    engine_task = asyncio.create_task(engine.start(measure_only=True))

    # Periodic stats printer
    start_time = time.time()
    last_stats_time = start_time

    try:
        while not stop_event.is_set():
            now = time.time()
            elapsed = now - start_time

            # Check if duration exceeded
            if elapsed >= duration_s:
                logger.info("Measurement duration reached (%.0f min)", duration_minutes)
                break

            # Print stats periodically
            if now - last_stats_time >= STATS_INTERVAL_S:
                _print_live_stats(engine, elapsed)
                last_stats_time = now

            # Also check if the engine task died unexpectedly
            if engine_task.done():
                exc = engine_task.exception()
                if exc:
                    logger.error("Engine task failed: %s", exc)
                break

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass

    # Stop engine
    await engine.stop()
    if not engine_task.done():
        engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass

    # Final report
    actual_duration = time.time() - start_time
    _print_final_report(engine, actual_duration)

    # Save raw data
    _save_raw_data(engine, OUTPUT_FILE)


def main():
    parser = argparse.ArgumentParser(
        description="Measure latency/dislocation between Binance and Delta India",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30,
        help="Measurement duration in minutes (default: 30)",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="BTC/USDT,ETH/USDT",
        help="Comma-separated list of symbols (default: BTC/USDT,ETH/USDT)",
    )
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    try:
        asyncio.run(run_measurement(args.duration, symbols))
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
