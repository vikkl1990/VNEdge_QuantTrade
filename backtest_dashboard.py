#!/usr/bin/env python3
"""
Backtesting Dashboard — Dedicated web UI for running & viewing backtests.

Runs on VM2 (129.158.231.78:8080) as a standalone Flask app.
Features:
  - Run backtests on-demand from the browser
  - Equity curve chart (Chart.js)
  - Trade-by-trade results table
  - Scanner performance breakdown
  - Multi-timeframe comparison
  - Cached candle data (no re-downloads)
  - Real-time progress via SSE
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask, jsonify, render_template_string, request, Response

# ─────────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("bt_dash")

# Global state
_backtest_results: Dict[str, Any] = {}  # id → result dict
_backtest_progress: Dict[str, Dict] = {}  # id → {pct, status, message}
_candle_cache: Dict[str, Any] = {}  # "SYM_TF_START_END" → DataFrame
_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────
# Candle fetching with disk cache
# ─────────────────────────────────────────────────────────────────

CACHE_DIR = Path("storage/candle_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(symbol: str, tf: str, start: str, end: str) -> str:
    return f"{symbol.replace('/', '_')}_{tf}_{start}_{end}"


def _fetch_candles_cached(symbol: str, tf: str, start_dt: datetime, end_dt: datetime) -> "pd.DataFrame":
    """Fetch candles with disk caching — never re-downloads same data."""
    import pandas as pd

    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")
    key = _cache_key(symbol, tf, start_str, end_str)
    cache_file = CACHE_DIR / f"{key}.parquet"

    if cache_file.exists():
        logger.info("  Cache HIT: %s %s (%s to %s)", symbol, tf, start_str, end_str)
        return pd.read_parquet(cache_file)

    logger.info("  Cache MISS: fetching %s %s from exchange...", symbol, tf)

    # Use ccxt to fetch
    import ccxt
    exchange = ccxt.delta({
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
    })

    all_ohlcv = []
    since = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    while since < end_ms:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
            if not ohlcv:
                break
            all_ohlcv.extend(ohlcv)
            since = ohlcv[-1][0] + 1
            if len(ohlcv) < 1000:
                break
        except Exception as e:
            logger.warning("Fetch error: %s — retrying in 2s", e)
            time.sleep(2)
            continue

    if not all_ohlcv:
        return pd.DataFrame()

    df = pd.DataFrame(all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.set_index("timestamp")
    df = df[~df.index.duplicated(keep="first")]
    df = df[(df.index >= pd.Timestamp(start_dt)) & (df.index < pd.Timestamp(end_dt))]

    # Cache to disk
    try:
        df.to_parquet(cache_file)
        logger.info("  Cached %d candles to %s", len(df), cache_file.name)
    except Exception as e:
        logger.warning("Cache write failed: %s", e)

    return df


# ─────────────────────────────────────────────────────────────────
# Backtest engine (simplified, runs in thread)
# ─────────────────────────────────────────────────────────────────

def _run_backtest_thread(bt_id: str, params: Dict):
    """Run backtest in background thread, update progress."""
    import pandas as pd
    import numpy as np
    from config import get_config
    from strategies.scalp_strategy import ScalpStrategy
    from data.indicators import calc_atr, calc_ema, calc_rsi, calc_macd, calc_bollinger_bands, calc_supertrend, calc_relative_volume, calc_mfi, calc_volume_spike

    try:
        _backtest_progress[bt_id] = {"pct": 0, "status": "starting", "message": "Initializing..."}

        symbols = params.get("symbols", ["BTC/USDT"])
        start_str = params.get("start", "2026-03-01")
        end_str = params.get("end", "2026-03-18")
        balance = params.get("balance", 10000.0)
        trigger_tf = params.get("trigger_tf", "5m")

        start_dt = datetime.strptime(start_str, "%Y-%m-%d")
        end_dt = datetime.strptime(end_str, "%Y-%m-%d")

        # Fetch candles (cached)
        _backtest_progress[bt_id] = {"pct": 5, "status": "fetching", "message": "Fetching candle data..."}

        candle_data = {}
        tfs_needed = [trigger_tf, "15m"]
        if trigger_tf != "5m":
            tfs_needed.append("5m")

        for sym in symbols:
            candle_data[sym] = {}
            for tf in tfs_needed:
                # For trigger TF, fetch from 1m and resample if needed
                if tf == trigger_tf and tf not in ("1m", "5m", "15m", "30m", "1h"):
                    # Resample from 1m
                    df_1m = _fetch_candles_cached(sym, "1m", start_dt - timedelta(days=1), end_dt)
                    if tf == "3m":
                        df = df_1m.resample("3min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
                    elif tf == "10m":
                        df = df_1m.resample("10min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
                    else:
                        df = _fetch_candles_cached(sym, tf, start_dt - timedelta(days=1), end_dt)
                    candle_data[sym][tf] = df
                else:
                    candle_data[sym][tf] = _fetch_candles_cached(sym, tf, start_dt - timedelta(days=1), end_dt)

        # Init strategy
        config = get_config()
        strategy = ScalpStrategy(config)
        strategy._session_gate_enabled = False  # disable session filter for backtest

        # Simulation state
        equity = balance
        equity_curve = []
        trades = []
        open_positions = {}
        trade_counter = 0

        # Get trigger candles
        trigger_sym = symbols[0]
        trigger_df = candle_data[trigger_sym].get(trigger_tf, pd.DataFrame())
        if trigger_df.empty:
            _backtest_progress[bt_id] = {"pct": 100, "status": "error", "message": "No candle data available"}
            return

        total_bars = len(trigger_df)
        _backtest_progress[bt_id] = {"pct": 10, "status": "running", "message": f"Processing {total_bars} bars..."}

        # Bar-by-bar replay
        for i in range(50, total_bars):
            if i % max(1, total_bars // 20) == 0:
                pct = 10 + int(80 * i / total_bars)
                _backtest_progress[bt_id] = {
                    "pct": pct, "status": "running",
                    "message": f"Bar {i}/{total_bars} | Equity: ${equity:.2f} | Trades: {len(trades)}"
                }

            bar = trigger_df.iloc[i]
            bar_time = trigger_df.index[i]
            price = float(bar["close"])
            high = float(bar["high"])
            low = float(bar["low"])

            # Check open positions for exits
            closed_ids = []
            for tid, pos in open_positions.items():
                is_long = pos["side"] == "long"

                # Update MAE/MFE
                if is_long:
                    pos["mfe"] = max(pos["mfe"], high - pos["entry"])
                    pos["mae"] = max(pos["mae"], pos["entry"] - low)
                else:
                    pos["mfe"] = max(pos["mfe"], pos["entry"] - low)
                    pos["mae"] = max(pos["mae"], high - pos["entry"])

                risk = pos["risk"]
                mfe_r = pos["mfe"] / risk if risk > 0 else 0
                mae_r = pos["mae"] / risk if risk > 0 else 0
                age_bars = i - pos["entry_bar"]

                # TP1 check
                if not pos["tp1_hit"]:
                    tp1_hit = (high >= pos["tp1"]) if is_long else (low <= pos["tp1"])
                    if tp1_hit:
                        pos["tp1_hit"] = True
                        # Book 35% at TP1
                        tp1_pnl = abs(pos["tp1"] - pos["entry"]) / pos["entry"] * 100
                        pos["tp1_pnl"] = 0.35 * tp1_pnl
                        # Move SL to breakeven + fees
                        fee_buffer = pos["entry"] * 0.002
                        if is_long:
                            pos["sl"] = pos["entry"] + fee_buffer
                        else:
                            pos["sl"] = pos["entry"] - fee_buffer

                # TP2 check
                if pos["tp1_hit"] and not pos["tp2_hit"]:
                    tp2_hit = (high >= pos["tp2"]) if is_long else (low <= pos["tp2"])
                    if tp2_hit:
                        pos["tp2_hit"] = True
                        tp2_pnl = abs(pos["tp2"] - pos["entry"]) / pos["entry"] * 100
                        pos["tp2_pnl"] = 0.35 * tp2_pnl

                # TP3 check
                if pos["tp2_hit"] and not pos["tp3_hit"]:
                    tp3_hit = (high >= pos["tp3"]) if is_long else (low <= pos["tp3"])
                    if tp3_hit:
                        pos["tp3_hit"] = True
                        tp3_pnl = abs(pos["tp3"] - pos["entry"]) / pos["entry"] * 100
                        pos["tp3_pnl"] = 0.30 * tp3_pnl
                        # Full exit
                        total_pnl = pos["tp1_pnl"] + pos["tp2_pnl"] + pos["tp3_pnl"] - 0.16
                        equity += equity * total_pnl / 100
                        pos["exit_price"] = pos["tp3"]
                        pos["exit_reason"] = "tp3_full"
                        pos["pnl_pct"] = total_pnl
                        pos["exit_bar"] = i
                        trades.append(pos)
                        closed_ids.append(tid)
                        continue

                # SL check
                sl_hit = (low <= pos["sl"]) if is_long else (high >= pos["sl"])
                if sl_hit:
                    if pos["tp2_hit"]:
                        # Partial win
                        remaining_pnl = 0.30 * (abs(pos["sl"] - pos["entry"]) / pos["entry"] * 100)
                        if (is_long and pos["sl"] < pos["entry"]) or (not is_long and pos["sl"] > pos["entry"]):
                            remaining_pnl = -remaining_pnl
                        total_pnl = pos["tp1_pnl"] + pos["tp2_pnl"] + remaining_pnl - 0.16
                    elif pos["tp1_hit"]:
                        remaining_pnl = 0.65 * (abs(pos["sl"] - pos["entry"]) / pos["entry"] * 100)
                        if (is_long and pos["sl"] < pos["entry"]) or (not is_long and pos["sl"] > pos["entry"]):
                            remaining_pnl = -remaining_pnl
                        total_pnl = pos["tp1_pnl"] + remaining_pnl - 0.16
                    else:
                        sl_dist_pct = abs(pos["sl"] - pos["entry"]) / pos["entry"] * 100
                        total_pnl = -sl_dist_pct - 0.16
                    equity += equity * total_pnl / 100
                    pos["exit_price"] = pos["sl"]
                    pos["exit_reason"] = "stop_loss"
                    pos["pnl_pct"] = total_pnl
                    pos["exit_bar"] = i
                    pos["mfe_r"] = mfe_r
                    pos["mae_r"] = mae_r
                    trades.append(pos)
                    closed_ids.append(tid)
                    continue

                # Time stop: 15 bars with MFE < 0.2R
                if age_bars >= 15 and mfe_r < 0.2:
                    if is_long:
                        pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
                    else:
                        pnl_pct = (pos["entry"] - price) / pos["entry"] * 100
                    total_pnl = pnl_pct - 0.16
                    equity += equity * total_pnl / 100
                    pos["exit_price"] = price
                    pos["exit_reason"] = "time_stop"
                    pos["pnl_pct"] = total_pnl
                    pos["exit_bar"] = i
                    pos["mfe_r"] = mfe_r
                    pos["mae_r"] = mae_r
                    trades.append(pos)
                    closed_ids.append(tid)
                    continue

                # Max age: 60 bars
                if age_bars >= 60:
                    if is_long:
                        pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
                    else:
                        pnl_pct = (pos["entry"] - price) / pos["entry"] * 100
                    if pos["tp1_hit"]:
                        total_pnl = pos["tp1_pnl"] + (0.65 * pnl_pct) - 0.16
                    else:
                        total_pnl = pnl_pct - 0.16
                    equity += equity * total_pnl / 100
                    pos["exit_price"] = price
                    pos["exit_reason"] = "expired"
                    pos["pnl_pct"] = total_pnl
                    pos["exit_bar"] = i
                    pos["mfe_r"] = mfe_r
                    pos["mae_r"] = mae_r
                    trades.append(pos)
                    closed_ids.append(tid)

            for tid in closed_ids:
                del open_positions[tid]

            # Record equity
            equity_curve.append({"time": str(bar_time), "equity": round(equity, 2)})

            # Max 1 open position per symbol
            for sym in symbols:
                if any(p["symbol"] == sym for p in open_positions.values()):
                    continue

                # Build candle dict for strategy
                sym_candles = {}
                for tf in candle_data.get(sym, {}):
                    tf_df = candle_data[sym][tf]
                    # Get candles up to current bar time
                    mask = tf_df.index <= bar_time
                    if mask.sum() >= 50:
                        sym_candles[tf] = tf_df[mask].tail(250)

                if not sym_candles:
                    continue

                # Set trigger TF as primary
                if trigger_tf in sym_candles:
                    sym_candles["1m"] = sym_candles.get(trigger_tf, sym_candles.get("1m"))

                try:
                    signals = strategy.analyze(sym, sym_candles)
                except Exception:
                    continue

                for sig in signals:
                    trade_counter += 1
                    tid = f"bt_{trade_counter}"
                    tps = sig.take_profits if sig.take_profits else [0, 0, 0]
                    risk = abs(sig.entry_price - sig.stop_loss)

                    open_positions[tid] = {
                        "id": tid,
                        "symbol": sym,
                        "side": "long" if sig.side.value == "long" else "short",
                        "entry": sig.entry_price,
                        "sl": sig.stop_loss,
                        "tp1": tps[0] if len(tps) > 0 else 0,
                        "tp2": tps[1] if len(tps) > 1 else 0,
                        "tp3": tps[2] if len(tps) > 2 else 0,
                        "risk": risk,
                        "confidence": sig.confidence,
                        "scanner": sig.metadata.get("setup_type", "unknown"),
                        "entry_bar": i,
                        "mfe": 0, "mae": 0, "mfe_r": 0, "mae_r": 0,
                        "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                        "tp1_pnl": 0, "tp2_pnl": 0, "tp3_pnl": 0,
                    }
                    break  # 1 signal per bar

        # Close any remaining open positions at last price
        last_price = float(trigger_df.iloc[-1]["close"])
        for tid, pos in open_positions.items():
            is_long = pos["side"] == "long"
            if is_long:
                pnl_pct = (last_price - pos["entry"]) / pos["entry"] * 100
            else:
                pnl_pct = (pos["entry"] - last_price) / pos["entry"] * 100
            total_pnl = pnl_pct - 0.16
            equity += equity * total_pnl / 100
            pos["exit_price"] = last_price
            pos["exit_reason"] = "end_of_backtest"
            pos["pnl_pct"] = total_pnl
            pos["exit_bar"] = total_bars - 1
            trades.append(pos)

        # Compute summary stats
        wins = [t for t in trades if t["pnl_pct"] > 0]
        losses = [t for t in trades if t["pnl_pct"] <= 0]
        total_pnl = sum(t["pnl_pct"] for t in trades)
        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        gross_profit = sum(t["pnl_pct"] for t in wins)
        gross_loss = abs(sum(t["pnl_pct"] for t in losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else 999

        # Scanner breakdown
        from collections import defaultdict
        scanner_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0, "trades": []})
        exit_stats = defaultdict(int)
        for t in trades:
            sc = t.get("scanner", "unknown")
            if t["pnl_pct"] > 0:
                scanner_stats[sc]["w"] += 1
            else:
                scanner_stats[sc]["l"] += 1
            scanner_stats[sc]["pnl"] += t["pnl_pct"]
            scanner_stats[sc]["trades"].append(t["pnl_pct"])
            exit_stats[t.get("exit_reason", "unknown")] += 1

        scanner_summary = []
        for sc, st in sorted(scanner_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
            total = st["w"] + st["l"]
            scanner_summary.append({
                "name": sc,
                "trades": total,
                "wins": st["w"],
                "losses": st["l"],
                "wr": round(st["w"] / total * 100, 1) if total > 0 else 0,
                "pnl": round(st["pnl"], 3),
                "avg_pnl": round(st["pnl"] / total, 3) if total > 0 else 0,
            })

        # Max consecutive losses
        max_consec = 0
        consec = 0
        for t in trades:
            if t["pnl_pct"] <= 0:
                consec += 1
                max_consec = max(max_consec, consec)
            else:
                consec = 0

        # Max drawdown
        peak = balance
        max_dd = 0
        for ec in equity_curve:
            eq = ec["equity"]
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)

        result = {
            "id": bt_id,
            "params": params,
            "summary": {
                "total_trades": len(trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(win_rate, 1),
                "total_pnl_pct": round(total_pnl, 3),
                "avg_win": round(avg_win, 3),
                "avg_loss": round(avg_loss, 3),
                "profit_factor": round(pf, 2),
                "max_consec_loss": max_consec,
                "max_drawdown": round(max_dd, 2),
                "initial_balance": balance,
                "final_equity": round(equity, 2),
                "return_pct": round((equity - balance) / balance * 100, 2),
            },
            "scanners": scanner_summary,
            "exit_reasons": dict(exit_stats),
            "equity_curve": equity_curve[::max(1, len(equity_curve) // 500)],  # Downsample to 500 pts
            "trades": [{
                "id": t["id"],
                "symbol": t.get("symbol", ""),
                "side": t.get("side", ""),
                "scanner": t.get("scanner", ""),
                "entry": t.get("entry", 0),
                "exit": t.get("exit_price", 0),
                "sl": t.get("sl", 0),
                "tp1": t.get("tp1", 0),
                "confidence": t.get("confidence", 0),
                "pnl_pct": round(t.get("pnl_pct", 0), 3),
                "exit_reason": t.get("exit_reason", ""),
                "tp1_hit": t.get("tp1_hit", False),
                "tp2_hit": t.get("tp2_hit", False),
                "tp3_hit": t.get("tp3_hit", False),
                "mfe_r": round(t.get("mfe_r", 0), 2),
                "mae_r": round(t.get("mae_r", 0), 2),
            } for t in trades[-500:]],  # Last 500 trades
            "completed_at": datetime.now().isoformat(),
        }

        with _lock:
            _backtest_results[bt_id] = result

        _backtest_progress[bt_id] = {"pct": 100, "status": "done", "message": f"Complete: {len(trades)} trades, WR={win_rate:.1f}%, PnL={total_pnl:+.2f}%"}
        logger.info("Backtest %s complete: %d trades, WR=%.1f%%, PnL=%+.2f%%", bt_id, len(trades), win_rate, total_pnl)

    except Exception as e:
        logger.exception("Backtest %s failed: %s", bt_id, e)
        _backtest_progress[bt_id] = {"pct": 100, "status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────────

@app.route("/api/run", methods=["POST"])
def api_run():
    """Start a backtest run."""
    params = request.json or {}
    bt_id = str(uuid.uuid4())[:8]

    # Defaults
    params.setdefault("symbols", ["BTC/USDT"])
    params.setdefault("start", "2026-03-01")
    params.setdefault("end", "2026-03-18")
    params.setdefault("balance", 10000.0)
    params.setdefault("trigger_tf", "5m")

    thread = threading.Thread(target=_run_backtest_thread, args=(bt_id, params), daemon=True)
    thread.start()

    return jsonify({"id": bt_id, "status": "started"})


@app.route("/api/progress/<bt_id>")
def api_progress(bt_id):
    """Get backtest progress."""
    prog = _backtest_progress.get(bt_id, {"pct": 0, "status": "unknown", "message": "Not found"})
    return jsonify(prog)


@app.route("/api/result/<bt_id>")
def api_result(bt_id):
    """Get backtest results."""
    with _lock:
        result = _backtest_results.get(bt_id)
    if not result:
        return jsonify({"error": "Not found or not complete"}), 404
    return jsonify(result)


@app.route("/api/results")
def api_results_list():
    """List all completed backtests."""
    with _lock:
        summaries = []
        for bt_id, r in _backtest_results.items():
            summaries.append({
                "id": bt_id,
                "params": r.get("params", {}),
                "summary": r.get("summary", {}),
                "completed_at": r.get("completed_at", ""),
            })
    return jsonify(summaries)


@app.route("/api/cache/clear", methods=["POST"])
def api_clear_cache():
    """Clear candle cache."""
    count = 0
    for f in CACHE_DIR.glob("*.parquet"):
        f.unlink()
        count += 1
    return jsonify({"cleared": count})


# ─────────────────────────────────────────────────────────────────
# Dashboard HTML
# ─────────────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoAlgoBot — Backtest Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0a0e17; color: #e0e6ed; }
.header { background: #111827; padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #1f2937; }
.header h1 { font-size: 18px; color: #60a5fa; }
.header .tag { background: #1e40af; color: white; padding: 3px 10px; border-radius: 12px; font-size: 12px; }
.container { max-width: 1400px; margin: 0 auto; padding: 16px; }
.controls { background: #111827; border-radius: 8px; padding: 16px; margin-bottom: 16px; display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }
.control-group { display: flex; flex-direction: column; gap: 4px; }
.control-group label { font-size: 11px; color: #9ca3af; text-transform: uppercase; }
.control-group input, .control-group select { background: #1f2937; border: 1px solid #374151; color: white; padding: 6px 10px; border-radius: 4px; font-size: 13px; }
.btn { padding: 8px 20px; border-radius: 6px; border: none; cursor: pointer; font-weight: 600; font-size: 13px; }
.btn-run { background: #10b981; color: white; }
.btn-run:hover { background: #059669; }
.btn-run:disabled { background: #374151; cursor: not-allowed; }
.progress-bar { width: 100%; height: 6px; background: #1f2937; border-radius: 3px; margin-top: 8px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #3b82f6, #10b981); transition: width 0.3s; border-radius: 3px; }
.progress-text { font-size: 12px; color: #9ca3af; margin-top: 4px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px; }
.stat-card { background: #111827; border-radius: 8px; padding: 14px; text-align: center; border: 1px solid #1f2937; }
.stat-card .label { font-size: 11px; color: #6b7280; text-transform: uppercase; }
.stat-card .value { font-size: 22px; font-weight: 700; margin-top: 4px; }
.positive { color: #10b981; }
.negative { color: #ef4444; }
.neutral { color: #9ca3af; }
.chart-container { background: #111827; border-radius: 8px; padding: 16px; margin-bottom: 16px; border: 1px solid #1f2937; }
.chart-container h3 { font-size: 14px; color: #9ca3af; margin-bottom: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { background: #1f2937; padding: 8px; text-align: left; color: #9ca3af; font-weight: 600; }
td { padding: 6px 8px; border-bottom: 1px solid #1f2937; }
tr:hover { background: #1f293788; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
@media (max-width: 800px) { .two-col { grid-template-columns: 1fr; } }
.hidden { display: none; }
</style>
</head>
<body>
<div class="header">
    <h1>🧪 CryptoAlgoBot — Backtest Dashboard</h1>
    <span class="tag">VM2</span>
</div>
<div class="container">
    <!-- Controls -->
    <div class="controls">
        <div class="control-group">
            <label>Start Date</label>
            <input type="date" id="start" value="2026-01-01">
        </div>
        <div class="control-group">
            <label>End Date</label>
            <input type="date" id="end" value="2026-03-18">
        </div>
        <div class="control-group">
            <label>Symbols</label>
            <select id="symbols">
                <option value="BTC/USDT">BTC/USDT</option>
                <option value="ETH/USDT">ETH/USDT</option>
                <option value="BTC/USDT,ETH/USDT" selected>Both</option>
            </select>
        </div>
        <div class="control-group">
            <label>Trigger TF</label>
            <select id="trigger_tf">
                <option value="1m">1 min</option>
                <option value="3m">3 min</option>
                <option value="5m" selected>5 min</option>
                <option value="15m">15 min</option>
            </select>
        </div>
        <div class="control-group">
            <label>Balance</label>
            <input type="number" id="balance" value="10000">
        </div>
        <div class="control-group">
            <label>&nbsp;</label>
            <button class="btn btn-run" id="runBtn" onclick="runBacktest()">▶ Run Backtest</button>
        </div>
    </div>
    <div id="progressArea" class="hidden">
        <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
        <div class="progress-text" id="progressText">Starting...</div>
    </div>

    <!-- Summary Stats -->
    <div id="resultsArea" class="hidden">
        <div class="grid" id="statsGrid"></div>

        <!-- Equity Curve -->
        <div class="chart-container">
            <h3>EQUITY CURVE</h3>
            <canvas id="equityChart" height="120"></canvas>
        </div>

        <!-- Scanner + Exit breakdown -->
        <div class="two-col">
            <div class="chart-container">
                <h3>SCANNER PERFORMANCE</h3>
                <table id="scannerTable"><thead><tr><th>Scanner</th><th>Trades</th><th>WR</th><th>PnL%</th><th>Avg</th></tr></thead><tbody></tbody></table>
            </div>
            <div class="chart-container">
                <h3>EXIT REASONS</h3>
                <canvas id="exitChart" height="200"></canvas>
            </div>
        </div>

        <!-- Trade Log -->
        <div class="chart-container">
            <h3>TRADE LOG (last 500)</h3>
            <div style="max-height:400px;overflow-y:auto;">
                <table id="tradeTable"><thead><tr><th>#</th><th>Symbol</th><th>Side</th><th>Scanner</th><th>Entry</th><th>Exit</th><th>PnL%</th><th>Exit Reason</th><th>TP1</th><th>TP2</th><th>TP3</th><th>MFE(R)</th></tr></thead><tbody></tbody></table>
            </div>
        </div>
    </div>

    <!-- History -->
    <div class="chart-container" id="historyArea">
        <h3>BACKTEST HISTORY</h3>
        <table id="historyTable"><thead><tr><th>ID</th><th>TF</th><th>Period</th><th>Trades</th><th>WR</th><th>PnL%</th><th>PF</th><th>MaxDD</th><th>Actions</th></tr></thead><tbody></tbody></table>
    </div>
</div>

<script>
let currentBtId = null;
let pollTimer = null;
let equityChartInstance = null;
let exitChartInstance = null;

async function runBacktest() {
    const btn = document.getElementById('runBtn');
    btn.disabled = true;
    btn.textContent = '⏳ Running...';

    const syms = document.getElementById('symbols').value.split(',');
    const params = {
        symbols: syms,
        start: document.getElementById('start').value,
        end: document.getElementById('end').value,
        trigger_tf: document.getElementById('trigger_tf').value,
        balance: parseFloat(document.getElementById('balance').value),
    };

    const resp = await fetch('/api/run', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(params) });
    const data = await resp.json();
    currentBtId = data.id;

    document.getElementById('progressArea').classList.remove('hidden');
    document.getElementById('resultsArea').classList.add('hidden');
    pollProgress();
}

function pollProgress() {
    if (!currentBtId) return;
    pollTimer = setInterval(async () => {
        const resp = await fetch(`/api/progress/${currentBtId}`);
        const prog = await resp.json();
        document.getElementById('progressFill').style.width = prog.pct + '%';
        document.getElementById('progressText').textContent = prog.message;

        if (prog.status === 'done' || prog.status === 'error') {
            clearInterval(pollTimer);
            document.getElementById('runBtn').disabled = false;
            document.getElementById('runBtn').textContent = '▶ Run Backtest';
            if (prog.status === 'done') loadResult(currentBtId);
            loadHistory();
        }
    }, 1500);
}

async function loadResult(btId) {
    const resp = await fetch(`/api/result/${btId}`);
    if (!resp.ok) return;
    const r = await resp.json();
    const s = r.summary;

    // Stats grid
    const stats = [
        { label: 'Total Trades', value: s.total_trades, cls: 'neutral' },
        { label: 'Win Rate', value: s.win_rate + '%', cls: s.win_rate >= 50 ? 'positive' : 'negative' },
        { label: 'Total PnL', value: (s.total_pnl_pct >= 0 ? '+' : '') + s.total_pnl_pct + '%', cls: s.total_pnl_pct >= 0 ? 'positive' : 'negative' },
        { label: 'Profit Factor', value: s.profit_factor, cls: s.profit_factor >= 1 ? 'positive' : 'negative' },
        { label: 'Avg Win', value: '+' + s.avg_win + '%', cls: 'positive' },
        { label: 'Avg Loss', value: s.avg_loss + '%', cls: 'negative' },
        { label: 'Max Drawdown', value: s.max_drawdown + '%', cls: 'negative' },
        { label: 'Max Consec Loss', value: s.max_consec_loss, cls: s.max_consec_loss > 10 ? 'negative' : 'neutral' },
        { label: 'Final Equity', value: '$' + s.final_equity.toLocaleString(), cls: s.final_equity >= s.initial_balance ? 'positive' : 'negative' },
    ];
    document.getElementById('statsGrid').innerHTML = stats.map(s =>
        `<div class="stat-card"><div class="label">${s.label}</div><div class="value ${s.cls}">${s.value}</div></div>`
    ).join('');

    // Equity curve
    if (equityChartInstance) equityChartInstance.destroy();
    const ctx = document.getElementById('equityChart').getContext('2d');
    equityChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
            labels: r.equity_curve.map(e => e.time.substring(5, 16)),
            datasets: [{ label: 'Equity', data: r.equity_curve.map(e => e.equity), borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)', fill: true, pointRadius: 0, tension: 0.1 }]
        },
        options: { responsive: true, plugins: { legend: { display: false } }, scales: { x: { display: true, ticks: { maxTicksLimit: 10, color: '#6b7280' } }, y: { ticks: { color: '#6b7280' } } } }
    });

    // Scanner table
    const stbody = document.querySelector('#scannerTable tbody');
    stbody.innerHTML = r.scanners.map(sc =>
        `<tr><td>${sc.name}</td><td>${sc.trades}</td><td class="${sc.wr>=50?'positive':'negative'}">${sc.wr}%</td><td class="${sc.pnl>=0?'positive':'negative'}">${sc.pnl>0?'+':''}${sc.pnl}%</td><td>${sc.avg_pnl>0?'+':''}${sc.avg_pnl}%</td></tr>`
    ).join('');

    // Exit reasons chart
    if (exitChartInstance) exitChartInstance.destroy();
    const ectx = document.getElementById('exitChart').getContext('2d');
    const exitColors = { stop_loss: '#ef4444', tp3_full: '#10b981', time_stop: '#f59e0b', expired: '#6b7280', end_of_backtest: '#8b5cf6' };
    exitChartInstance = new Chart(ectx, {
        type: 'doughnut',
        data: {
            labels: Object.keys(r.exit_reasons),
            datasets: [{ data: Object.values(r.exit_reasons), backgroundColor: Object.keys(r.exit_reasons).map(k => exitColors[k] || '#374151') }]
        },
        options: { responsive: true, plugins: { legend: { position: 'right', labels: { color: '#9ca3af' } } } }
    });

    // Trade log
    const ttbody = document.querySelector('#tradeTable tbody');
    ttbody.innerHTML = r.trades.map((t, i) =>
        `<tr><td>${i+1}</td><td>${t.symbol}</td><td>${t.side}</td><td>${t.scanner}</td><td>${t.entry}</td><td>${t.exit}</td><td class="${t.pnl_pct>=0?'positive':'negative'}">${t.pnl_pct>0?'+':''}${t.pnl_pct}%</td><td>${t.exit_reason}</td><td>${t.tp1_hit?'✅':'❌'}</td><td>${t.tp2_hit?'✅':'❌'}</td><td>${t.tp3_hit?'✅':'❌'}</td><td>${t.mfe_r}R</td></tr>`
    ).join('');

    document.getElementById('resultsArea').classList.remove('hidden');
}

async function loadHistory() {
    const resp = await fetch('/api/results');
    const results = await resp.json();
    const tbody = document.querySelector('#historyTable tbody');
    tbody.innerHTML = results.map(r => {
        const s = r.summary;
        const p = r.params;
        return `<tr><td>${r.id}</td><td>${p.trigger_tf||'5m'}</td><td>${p.start} → ${p.end}</td><td>${s.total_trades}</td><td class="${s.win_rate>=50?'positive':'negative'}">${s.win_rate}%</td><td class="${s.total_pnl_pct>=0?'positive':'negative'}">${s.total_pnl_pct}%</td><td class="${s.profit_factor>=1?'positive':'negative'}">${s.profit_factor}</td><td>${s.max_drawdown}%</td><td><button class="btn" onclick="loadResult('${r.id}')" style="padding:3px 8px;background:#1e40af;color:white;font-size:11px;">View</button></td></tr>`;
    }).join('');
}

// Load history on page load
loadHistory();
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logger.info("Backtest Dashboard starting on port %d", args.port)
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)
