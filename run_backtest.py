#!/usr/bin/env python3
"""
Backtest Runner — Validates strategy over historical data.

Usage:
    python3 run_backtest.py                            # Last 30 days, BTC+ETH
    python3 run_backtest.py --start 2026-02-01 --end 2026-03-17
    python3 run_backtest.py --symbols BTC/USDT --balance 5000

Fetches historical 1m/5m/15m candles from Delta exchange (or CSV fallback),
replays bar-by-bar through the strategy engine with realistic fee/slippage,
and prints a full performance report.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import get_config
from strategies.scalp_strategy import ScalpStrategy
from strategies.regime_filter import RegimeFilter
from backtest.result import BacktestResult

logger = logging.getLogger("backtest")


# ─────────────────────────────────────────────────────────────────────
# Simulated Position (mirrors live signal tracker logic)
# ─────────────────────────────────────────────────────────────────────

class SimPosition:
    """Tracks a simulated position with partial TP exits and profit protection."""

    def __init__(
        self,
        trade_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        tp3: float,
        atr: float,
        entry_time: datetime,
        confidence: int = 0,
        setup_type: str = "",
    ):
        self.trade_id = trade_id
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.tp1 = tp1
        self.tp2 = tp2
        self.tp3 = tp3
        self.atr = atr
        self.entry_time = entry_time
        self.confidence = confidence
        self.setup_type = setup_type

        self.initial_risk = abs(entry_price - stop_loss)
        self.highest = entry_price
        self.lowest = entry_price
        self.mfe_r = 0.0
        self.mae_r = 0.0

        # TP tracking
        self.tp1_hit = False
        self.tp2_hit = False
        self.tp3_hit = False

        # Profit protection
        self.breakeven_set = False
        self.trail_active = False
        self.trail_price = 0.0

        # Partial PnL
        self.tp1_pnl_locked = 0.0
        self.tp2_pnl_locked = 0.0
        self.position_remaining = 1.0

        # Result
        self.closed = False
        self.exit_price = 0.0
        self.exit_time: Optional[datetime] = None
        self.exit_reason = ""

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    def pnl_at(self, price: float) -> float:
        if self.is_long:
            return ((price - self.entry_price) / self.entry_price) * 100
        return ((self.entry_price - price) / self.entry_price) * 100

    def r_at(self, price: float) -> float:
        if self.initial_risk <= 0:
            return 0.0
        if self.is_long:
            return (price - self.entry_price) / self.initial_risk
        return (self.entry_price - price) / self.initial_risk

    def calc_final_pnl(self) -> float:
        """Calculate final PnL using actual partial close data."""
        if self.tp1_pnl_locked or self.tp2_pnl_locked:
            remaining_pnl = self.position_remaining * self.pnl_at(self.exit_price)
            gross = self.tp1_pnl_locked + self.tp2_pnl_locked + remaining_pnl
        else:
            gross = self.pnl_at(self.exit_price)
        # Deduct fees (0.18% round trip)
        return gross - 0.18

    def calc_final_r(self) -> float:
        """Calculate final R-multiple using 35/35/30 split."""
        raw_r = self.r_at(self.exit_price)
        if self.tp3_hit:
            return 0.35 * self.r_at(self.tp1) + 0.35 * self.r_at(self.tp2) + 0.30 * raw_r
        elif self.tp2_hit:
            return 0.35 * self.r_at(self.tp1) + 0.35 * self.r_at(self.tp2) + 0.30 * raw_r
        elif self.tp1_hit:
            return 0.35 * self.r_at(self.tp1) + 0.65 * raw_r
        return raw_r


# ─────────────────────────────────────────────────────────────────────
# Backtest Engine (simplified, focuses on scalp strategy)
# ─────────────────────────────────────────────────────────────────────

class ScalpBacktester:
    """Bar-by-bar backtest engine matching live signal tracker exit logic."""

    FEE_RATE = 0.0006          # taker fee
    SLIPPAGE_PCT = 0.02        # 0.02% slippage per side
    COOLDOWN_SEC = 180         # 3 min between signals per symbol
    MAX_OPEN = 2               # max simultaneous positions

    def __init__(self, config: Dict[str, Any], trigger_tf: str = "1m"):
        self.config = config
        self.trigger_tf = trigger_tf  # primary analysis timeframe
        self.strategy = ScalpStrategy(config)
        # Set the strategy's primary TF to match
        self.strategy.primary_tf = trigger_tf
        self.positions: Dict[str, SimPosition] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.equity_curve: List[Tuple[datetime, float]] = []
        self.balance = 10_000.0
        self.initial_balance = 10_000.0
        self.last_signal_time: Dict[str, datetime] = {}
        self.total_fees = 0.0

    @staticmethod
    def resample_ohlcv(df_1m: pd.DataFrame, target_tf: str) -> pd.DataFrame:
        """Resample 1m candles into higher timeframes."""
        tf_map = {"1m": "1min", "3m": "3min", "5m": "5min", "10m": "10min", "15m": "15min", "30m": "30min"}
        rule = tf_map.get(target_tf)
        if rule is None or target_tf == "1m":
            return df_1m

        resampled = df_1m.resample(rule).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna(subset=["open"])

        # Copy over any pre-computed indicator columns (they'll need recomputing)
        # but keep timestamp as the higher TF bar close
        return resampled

    async def run(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        balance: float = 10_000.0,
    ) -> BacktestResult:
        """Run the full backtest."""
        self.balance = balance
        self.initial_balance = balance
        self.positions.clear()
        self.closed_trades.clear()
        self.equity_curve.clear()
        self.total_fees = 0.0

        dt_start = datetime.fromisoformat(start_date)
        dt_end = datetime.fromisoformat(end_date)

        trigger = self.trigger_tf
        logger.info("=" * 60)
        logger.info("BACKTEST STARTING")
        logger.info("  Period: %s to %s", start_date, end_date)
        logger.info("  Symbols: %s", symbols)
        logger.info("  Trigger TF: %s", trigger)
        logger.info("  Balance: $%.2f", balance)
        logger.info("=" * 60)

        # Fetch 1m data (base) + 5m + 15m for confirmation
        candle_data: Dict[str, Dict[str, pd.DataFrame]] = {}
        for symbol in symbols:
            candle_data[symbol] = {}
            for tf in ["1m", "5m", "15m"]:
                df = await self._fetch_candles(symbol, tf, dt_start, dt_end)
                if df is not None and not df.empty:
                    candle_data[symbol][tf] = df
                    logger.info("  Loaded %d %s candles for %s", len(df), tf, symbol)
                else:
                    logger.warning("  No %s data for %s", tf, symbol)

            # Resample 1m into trigger TF if not 1m/5m/15m (e.g. 3m, 10m)
            if trigger not in candle_data[symbol] and "1m" in candle_data[symbol]:
                resampled = self.resample_ohlcv(candle_data[symbol]["1m"], trigger)
                if not resampled.empty:
                    candle_data[symbol][trigger] = resampled
                    logger.info("  Resampled 1m → %d %s candles for %s", len(resampled), trigger, symbol)

        # Determine confirmation TFs relative to trigger
        # trigger = primary, next higher = confirm, next = HTF
        tf_order = ["1m", "3m", "5m", "10m", "15m", "30m"]
        trigger_idx = tf_order.index(trigger) if trigger in tf_order else 0
        confirm_tf = tf_order[min(trigger_idx + 1, len(tf_order) - 1)] if trigger_idx < len(tf_order) - 1 else "15m"
        htf_tf = tf_order[min(trigger_idx + 2, len(tf_order) - 1)] if trigger_idx < len(tf_order) - 2 else "15m"
        # Make sure we have the confirm/HTF data (resample if needed)
        for symbol in symbols:
            for needed_tf in [confirm_tf, htf_tf]:
                if needed_tf not in candle_data[symbol] and "1m" in candle_data[symbol]:
                    resampled = self.resample_ohlcv(candle_data[symbol]["1m"], needed_tf)
                    if not resampled.empty:
                        candle_data[symbol][needed_tf] = resampled

        # Update strategy TF config
        self.strategy.primary_tf = trigger
        self.strategy.confirm_tf = confirm_tf
        self.strategy.htf = htf_tf
        logger.info("  TF chain: trigger=%s → confirm=%s → HTF=%s", trigger, confirm_tf, htf_tf)

        # Get primary TF timeline
        all_timestamps = set()
        for sym_data in candle_data.values():
            if trigger in sym_data:
                all_timestamps.update(sym_data[trigger].index.tolist())
        all_timestamps = sorted(all_timestamps)

        if not all_timestamps:
            logger.error("No candle data! Aborting.")
            return self._build_result(dt_start, dt_end, symbols)

        total_bars = len(all_timestamps)
        log_interval = max(1, total_bars // 20)
        signals_generated = 0

        # ── PRE-COMPUTE INDICATORS (massive speedup) ──
        # Compute all indicators once on the full dataset,
        # then slice the pre-computed dataframe per bar.
        logger.info("Pre-computing indicators for all symbols...")
        precomputed: Dict[str, Dict[str, pd.DataFrame]] = {}
        for symbol in symbols:
            precomputed[symbol] = {}
            sym_data = candle_data.get(symbol, {})
            for tf, df in sym_data.items():
                try:
                    pc_df = self.strategy._compute_indicators(df.copy())
                    precomputed[symbol][tf] = pc_df
                    logger.info("  Pre-computed %s %s indicators (%d bars)", symbol, tf, len(pc_df))
                except Exception as exc:
                    logger.debug("Pre-compute failed for %s %s: %s", symbol, tf, exc)
                    precomputed[symbol][tf] = df

        # Pre-compute ATR on the confirmation TF
        for symbol in symbols:
            if confirm_tf in precomputed[symbol]:
                try:
                    from data.indicators import calc_atr
                    atr_series = calc_atr(precomputed[symbol][confirm_tf], 14)
                    precomputed[symbol][confirm_tf]["_precomputed_atr"] = atr_series
                except Exception:
                    pass

        logger.info("Processing %d bars...", total_bars)

        for bar_idx, current_ts in enumerate(all_timestamps):
            if bar_idx % log_interval == 0:
                pct = (bar_idx / total_bars) * 100
                open_count = sum(1 for p in self.positions.values() if not p.closed)
                logger.info(
                    "  %d/%d (%.0f%%) | Balance: $%.2f | Open: %d | Closed: %d | Signals: %d",
                    bar_idx, total_bars, pct, self.balance, open_count,
                    len(self.closed_trades), signals_generated,
                )

            for symbol in symbols:
                sym_data = precomputed.get(symbol, {})
                df_trigger = sym_data.get(trigger)
                if df_trigger is None or current_ts not in df_trigger.index:
                    continue

                bar = df_trigger.loc[current_ts]
                price = float(bar["close"])
                high = float(bar["high"])
                low = float(bar["low"])

                # 1. Process exits for open positions
                self._process_exits(symbol, price, high, low, current_ts)

                # 2. Generate new signals (every bar)
                if bar_idx < 200:  # skip first 200 bars for indicator warmup
                    continue

                # Build multi-TF candles dict — use pre-computed slices
                candles_dict: Dict[str, pd.DataFrame] = {}
                for tf, df in sym_data.items():
                    slice_df = df.loc[:current_ts]
                    if len(slice_df) >= 50:
                        candles_dict[tf] = slice_df

                if trigger not in candles_dict:
                    continue

                # Cooldown check
                if symbol in self.last_signal_time:
                    elapsed = (current_ts - self.last_signal_time[symbol]).total_seconds()
                    if elapsed < self.COOLDOWN_SEC:
                        continue

                # Max open positions check
                open_count = sum(1 for p in self.positions.values() if not p.closed)
                if open_count >= self.MAX_OPEN:
                    continue

                # Already have position for this symbol?
                if symbol in self.positions and not self.positions[symbol].closed:
                    continue

                # Run strategy — skip _compute_indicators since pre-computed
                self.strategy._last_signal_time.clear()
                self.strategy._signal_count_hr.clear()
                try:
                    result = self.strategy.analyze(symbol, candles_dict)
                    if result and len(result) > 0:
                        signal = result[0]
                        self._process_entry(signal, symbol, price, current_ts)
                        signals_generated += 1
                except Exception as exc:
                    logger.debug("Strategy error: %s", exc)

            # Record equity
            equity = self.balance
            for pos in self.positions.values():
                if not pos.closed:
                    equity += self._unrealized_pnl(pos, candle_data, current_ts)
            self.equity_curve.append((current_ts, equity))

        # Force close remaining positions
        for pos in list(self.positions.values()):
            if not pos.closed:
                self._close_position(pos, pos.entry_price, all_timestamps[-1], "backtest_end")

        logger.info("=" * 60)
        logger.info("BACKTEST COMPLETE")
        logger.info("  Total trades: %d", len(self.closed_trades))
        logger.info("  Total signals: %d", signals_generated)
        logger.info("  Final balance: $%.2f", self.balance)
        logger.info("  Return: %.2f%%", ((self.balance - self.initial_balance) / self.initial_balance) * 100)
        logger.info("=" * 60)

        return self._build_result(dt_start, dt_end, symbols)

    def _process_entry(self, signal: Any, symbol: str, price: float, ts: datetime):
        """Process a signal for entry."""
        side = getattr(signal, "side", None)
        if side is None:
            return
        side_str = side.value if hasattr(side, "value") else str(side).lower()

        entry_price = getattr(signal, "entry_price", price)
        stop_loss = getattr(signal, "stop_loss", 0)
        take_profits = getattr(signal, "take_profits", [0, 0, 0])
        atr = getattr(signal, "atr", 0) or (signal.metadata or {}).get("atr", 0)
        confidence = getattr(signal, "confidence", 0)
        setup_type = (signal.metadata or {}).get("setup_type", "")

        if not stop_loss or not entry_price:
            return

        # Apply slippage
        slip = entry_price * (self.SLIPPAGE_PCT / 100)
        if side_str == "long":
            entry_price += slip
        else:
            entry_price -= slip

        tp1 = take_profits[0] if len(take_profits) > 0 else 0
        tp2 = take_profits[1] if len(take_profits) > 1 else 0
        tp3 = take_profits[2] if len(take_profits) > 2 else 0

        pos = SimPosition(
            trade_id=str(uuid.uuid4())[:12],
            symbol=symbol,
            side=side_str,
            entry_price=entry_price,
            stop_loss=stop_loss,
            tp1=tp1, tp2=tp2, tp3=tp3,
            atr=atr,
            entry_time=ts,
            confidence=confidence,
            setup_type=setup_type,
        )
        self.positions[symbol] = pos
        self.last_signal_time[symbol] = ts

    def _process_exits(self, symbol: str, price: float, high: float, low: float, ts: datetime):
        """Check exits using the same logic as live signal tracker."""
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        if pos.closed:
            return

        # Update extremes
        pos.highest = max(pos.highest, high)
        pos.lowest = min(pos.lowest, low)

        # Update MFE/MAE
        if pos.initial_risk > 0:
            if pos.is_long:
                fav = (pos.highest - pos.entry_price) / pos.initial_risk
                adv = (pos.entry_price - pos.lowest) / pos.initial_risk
            else:
                fav = (pos.entry_price - pos.lowest) / pos.initial_risk
                adv = (pos.highest - pos.entry_price) / pos.initial_risk
            pos.mfe_r = max(pos.mfe_r, fav)
            pos.mae_r = max(pos.mae_r, adv)

        current_r = pos.r_at(price)

        # --- Hard loss cap (-2R) ---
        if current_r <= -2.0:
            self._close_position(pos, price, ts, "hard_loss_cap")
            return

        # --- MFE Profit Protection ---
        if pos.mfe_r >= 1.0 and current_r <= 0.4:
            self._close_position(pos, price, ts, "profit_protect_1R")
            return
        if pos.mfe_r >= 0.7 and current_r <= 0.15:
            self._close_position(pos, price, ts, "profit_protect_07R")
            return
        if pos.mfe_r >= 0.3 and current_r <= -0.5:
            elapsed = (ts - pos.entry_time).total_seconds()
            if elapsed >= 300:
                self._close_position(pos, price, ts, "momentum_collapse")
                return

        # --- Stop loss ---
        sl_hit = (low <= pos.stop_loss) if pos.is_long else (high >= pos.stop_loss)
        if sl_hit:
            self._close_position(pos, pos.stop_loss, ts, "stop_loss")
            return

        # --- Early breakeven at +0.5R ---
        if not pos.breakeven_set and not pos.tp1_hit and pos.initial_risk > 0:
            if current_r >= 0.5:
                fee_buffer = pos.entry_price * (0.10 / 100)
                if pos.is_long:
                    pos.stop_loss = pos.entry_price + fee_buffer
                else:
                    pos.stop_loss = pos.entry_price - fee_buffer
                pos.breakeven_set = True

        # --- TP1 check ---
        if not pos.tp1_hit and pos.tp1:
            tp1_hit = (high >= pos.tp1) if pos.is_long else (low <= pos.tp1)
            if tp1_hit:
                pos.tp1_hit = True
                tp1_pnl = pos.pnl_at(pos.tp1)
                pos.tp1_pnl_locked = round(0.35 * tp1_pnl, 4)
                pos.position_remaining = 0.65

                # Start trailing at 1.0× ATR
                trail_dist = pos.atr * 1.0 if pos.atr > 0 else abs(pos.tp1 - pos.entry_price) * 0.5
                if pos.is_long:
                    trail_sl = max(high - trail_dist, pos.entry_price + pos.entry_price * 0.002)
                else:
                    trail_sl = min(low + trail_dist, pos.entry_price - pos.entry_price * 0.002)
                pos.trail_active = True
                pos.trail_price = trail_sl
                pos.stop_loss = trail_sl

        # --- TP2 check ---
        if not pos.tp2_hit and pos.tp2 and pos.tp1_hit:
            tp2_hit = (high >= pos.tp2) if pos.is_long else (low <= pos.tp2)
            if tp2_hit:
                pos.tp2_hit = True
                tp2_pnl = pos.pnl_at(pos.tp2)
                pos.tp2_pnl_locked = round(0.35 * tp2_pnl, 4)
                pos.position_remaining = 0.30

                # Tighten trail to 0.8× ATR
                trail_dist = pos.atr * 0.8 if pos.atr > 0 else abs(pos.tp2 - pos.tp1) * 0.3
                if pos.is_long:
                    pos.trail_price = max(high - trail_dist, pos.tp1)
                else:
                    pos.trail_price = min(low + trail_dist, pos.tp1)
                pos.stop_loss = pos.trail_price

        # --- TP3 check ---
        if not pos.tp3_hit and pos.tp3 and pos.tp2_hit:
            tp3_hit = (high >= pos.tp3) if pos.is_long else (low <= pos.tp3)
            if tp3_hit:
                pos.tp3_hit = True
                self._close_position(pos, pos.tp3, ts, "tp3_full")
                return

        # --- Trail ratchet ---
        if pos.trail_active and pos.tp1_hit and not pos.tp3_hit:
            if pos.tp2_hit:
                trail_dist = pos.atr * 0.8 if pos.atr > 0 else abs(pos.tp2 - pos.tp1) * 0.3
            else:
                trail_dist = pos.atr * 1.0 if pos.atr > 0 else abs(pos.tp1 - pos.entry_price) * 0.5
            if pos.is_long:
                new_trail = high - trail_dist
                if new_trail > pos.trail_price:
                    pos.trail_price = new_trail
                    pos.stop_loss = new_trail
            else:
                new_trail = low + trail_dist
                if new_trail < pos.trail_price:
                    pos.trail_price = new_trail
                    pos.stop_loss = new_trail

        # --- ADAPTIVE time stop (matches live signal_tracker logic) ---
        elapsed = (ts - pos.entry_time).total_seconds()
        if not pos.tp1_hit:
            current_r = pos.r_at(price)

            # Base time adapts to setup type
            if pos.setup_type == 'bb_squeeze':
                base_time = 45 * 60
            elif pos.setup_type == 'rsi_divergence':
                base_time = 40 * 60
            elif pos.setup_type == 'trend_continuation':
                base_time = 25 * 60
            else:
                base_time = 20 * 60

            # Confidence adjustment
            if pos.confidence >= 85:
                base_time = int(base_time * 1.5)
            elif pos.confidence >= 75:
                base_time = int(base_time * 1.25)
            elif pos.confidence < 60:
                base_time = int(base_time * 0.75)

            # Progress adjustment
            if pos.mfe_r >= 0.3:
                base_time = int(base_time * 1.5)
            if pos.mfe_r >= 0.2 and current_r < 0:
                base_time = int(base_time * 0.7)

            mfe_threshold = 0.15 + (base_time / 3600)
            current_threshold = mfe_threshold * 0.8

            if elapsed >= base_time and pos.mfe_r < mfe_threshold and current_r < current_threshold:
                self._close_position(pos, price, ts, "time_stop")
                return
            if elapsed >= 90 * 60 and pos.mfe_r < 0.5 and current_r < 0.3:
                self._close_position(pos, price, ts, "time_stop")
                return

        # --- 4-hour expiry ---
        if elapsed >= 14400:
            self._close_position(pos, price, ts, "expired")

    def _close_position(self, pos: SimPosition, price: float, ts: datetime, reason: str):
        """Close position and record trade."""
        if pos.closed:
            return

        # Apply slippage on exit
        slip = price * (self.SLIPPAGE_PCT / 100)
        if pos.is_long:
            price -= slip
        else:
            price += slip

        pos.exit_price = price
        pos.exit_time = ts
        pos.exit_reason = reason
        pos.closed = True

        pnl_pct = pos.calc_final_pnl()
        exit_r = pos.calc_final_r()

        self.closed_trades.append({
            "trade_id": pos.trade_id,
            "symbol": pos.symbol,
            "side": pos.side,
            "setup_type": pos.setup_type,
            "confidence": pos.confidence,
            "entry_price": pos.entry_price,
            "exit_price": pos.exit_price,
            "pnl": pnl_pct * self.initial_balance * 0.01 / 100,  # approx dollar PnL
            "pnl_pct": pnl_pct,
            "exit_r": exit_r,
            "mfe_r": pos.mfe_r,
            "mae_r": pos.mae_r,
            "exit_reason": reason,
            "entry_time": pos.entry_time,
            "exit_time": ts,
            "tp1_hit": pos.tp1_hit,
            "tp2_hit": pos.tp2_hit,
            "tp3_hit": pos.tp3_hit,
            "breakeven_set": pos.breakeven_set,
        })

        # Update balance (simplified: use PnL percentage on $25 stake)
        stake = 25.0
        dollar_pnl = stake * pnl_pct / 100
        self.balance += dollar_pnl
        self.total_fees += stake * 0.18 / 100  # 0.18% round trip fees

    def _unrealized_pnl(self, pos: SimPosition, data: Dict, ts: datetime) -> float:
        sym_data = data.get(pos.symbol, {})
        df = sym_data.get("1m")
        if df is None or ts not in df.index:
            return 0.0
        price = float(df.loc[ts, "close"])
        return 25.0 * pos.pnl_at(price) / 100

    async def _fetch_candles(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> Optional[pd.DataFrame]:
        """Load candles from CSV or exchange."""
        project_root = Path(__file__).resolve().parent
        safe_sym = symbol.replace("/", "_")

        # Try CSV first
        csv_paths = [
            project_root / "data" / "candles" / f"{safe_sym}_{timeframe}.csv",
            project_root / "backtest" / "data" / f"{safe_sym}_{timeframe}.csv",
        ]
        for path in csv_paths:
            if path.exists():
                try:
                    df = pd.read_csv(path)
                    ts_col = next((c for c in df.columns if c.lower() in ("datetime", "timestamp", "date", "time")), df.columns[0])
                    df["datetime"] = pd.to_datetime(df[ts_col], utc=True)
                    df.set_index("datetime", inplace=True)
                    for col in ("open", "high", "low", "close", "volume"):
                        for src in df.columns:
                            if src.lower() == col and src != col:
                                df.rename(columns={src: col}, inplace=True)
                    mask = (df.index >= pd.Timestamp(start, tz="UTC")) & (df.index <= pd.Timestamp(end, tz="UTC"))
                    result = df.loc[mask]
                    if not result.empty:
                        logger.info("  Loaded %s %s from CSV: %s", symbol, timeframe, path.name)
                        return result
                except Exception as exc:
                    logger.debug("CSV load failed: %s", exc)

        # Fetch from exchange using CCXT directly
        try:
            import ccxt.async_support as ccxt

            exchange_cfg = self.config.get("exchange", {})
            exchange_id = exchange_cfg.get("name", "delta")

            # Map exchange names
            ccxt_id = {
                "delta": "delta",
                "binance": "binance",
                "bybit": "bybit",
            }.get(exchange_id, "binance")  # fallback to binance for data

            exchange = getattr(ccxt, ccxt_id)({
                "apiKey": exchange_cfg.get("api_key", ""),
                "secret": exchange_cfg.get("api_secret", ""),
                "options": {"defaultType": "swap"},
            })

            # Convert symbol for the exchange
            ex_symbol = symbol
            if ccxt_id == "delta":
                ex_symbol = symbol.replace("/", "")  # BTC/USDT -> BTCUSDT

            tf_ms = {"1m": 60000, "5m": 300000, "15m": 900000}.get(timeframe, 300000)
            all_rows = []
            since_ms = int(start.timestamp() * 1000)
            end_ms = int(end.timestamp() * 1000)

            logger.info("  Fetching %s %s from %s exchange...", symbol, timeframe, ccxt_id)

            while since_ms < end_ms:
                try:
                    rows = await exchange.fetch_ohlcv(ex_symbol, timeframe, since=since_ms, limit=1000)
                except Exception as fetch_err:
                    logger.debug("  Fetch chunk failed: %s", fetch_err)
                    break
                if not rows:
                    break
                all_rows.extend(rows)
                last_ts = rows[-1][0]
                if last_ts <= since_ms:
                    break
                since_ms = last_ts + tf_ms
                await asyncio.sleep(0.2)  # rate limit

            await exchange.close()

            if all_rows:
                df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df.set_index("datetime", inplace=True)
                df = df[~df.index.duplicated(keep="last")]
                df.sort_index(inplace=True)
                mask = (df.index >= pd.Timestamp(start, tz="UTC")) & (df.index <= pd.Timestamp(end, tz="UTC"))
                result_df = df.loc[mask]
                logger.info("  Fetched %d %s candles for %s", len(result_df), timeframe, symbol)

                # Cache to CSV for fast re-runs
                cache_dir = project_root / "data" / "candles"
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path = cache_dir / f"{safe_sym}_{timeframe}.csv"
                result_df.to_csv(cache_path)
                logger.info("  Cached to %s for fast re-runs", cache_path.name)

                return result_df
        except Exception as exc:
            logger.warning("Exchange fetch failed for %s %s: %s", symbol, timeframe, exc)

        return None

    def _build_result(self, start: datetime, end: datetime, symbols: List[str]) -> BacktestResult:
        return BacktestResult(
            trades=self.closed_trades,
            equity_curve=self.equity_curve,
            initial_balance=self.initial_balance,
            total_fees_paid=self.total_fees,
            start_date=start,
            end_date=end,
            symbols=symbols,
        )


# ─────────────────────────────────────────────────────────────────────
# Extended reporting
# ─────────────────────────────────────────────────────────────────────

def print_extended_report(result: BacktestResult):
    """Print detailed per-scanner and per-exit-reason analysis."""
    trades = result.trades

    if not trades:
        print("\nNo trades to analyze.")
        return

    # Per-scanner breakdown
    from collections import defaultdict
    scanner_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0, "r_sum": 0.0})
    exit_stats = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    side_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})

    for t in trades:
        scanner = t.get("setup_type", "unknown") or "unknown"
        pnl = t.get("pnl_pct", 0)
        exit_r = t.get("exit_r", 0)
        side = t.get("side", "")
        exit_reason = t.get("exit_reason", "")

        if pnl > 0:
            scanner_stats[scanner]["w"] += 1
            side_stats[side]["w"] += 1
        else:
            scanner_stats[scanner]["l"] += 1
            side_stats[side]["l"] += 1
        scanner_stats[scanner]["pnl"] += pnl
        scanner_stats[scanner]["r_sum"] += exit_r

        exit_stats[exit_reason]["count"] += 1
        exit_stats[exit_reason]["pnl"] += pnl

    print("\n" + "=" * 64)
    print("  PER-SCANNER PERFORMANCE")
    print("=" * 64)
    print(f"  {'Scanner':<20s} | {'W/L':>7s} | {'WR':>5s} | {'PnL':>8s} | {'Avg R':>6s}")
    print("-" * 64)
    for sc, st in sorted(scanner_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        total = st["w"] + st["l"]
        wr = st["w"] / total * 100 if total else 0
        avg_r = st["r_sum"] / total if total else 0
        print(f"  {sc:<20s} | {st['w']:2d}W/{st['l']:2d}L | {wr:4.0f}% | {st['pnl']:+7.2f}% | {avg_r:+5.2f}R")

    print("\n" + "=" * 64)
    print("  EXIT REASON BREAKDOWN")
    print("=" * 64)
    for reason, st in sorted(exit_stats.items(), key=lambda x: -x[1]["count"]):
        print(f"  {reason:<25s} | {st['count']:3d} trades | PnL: {st['pnl']:+7.2f}%")

    print("\n" + "=" * 64)
    print("  SIDE PERFORMANCE")
    print("=" * 64)
    for side, st in side_stats.items():
        total = st["w"] + st["l"]
        wr = st["w"] / total * 100 if total else 0
        print(f"  {side:<6s} | {st['w']:2d}W/{st['l']:2d}L (WR={wr:.0f}%) | PnL: {st['pnl']:+.2f}%")

    # TP hit rates
    tp1_count = sum(1 for t in trades if t.get("tp1_hit"))
    tp2_count = sum(1 for t in trades if t.get("tp2_hit"))
    tp3_count = sum(1 for t in trades if t.get("tp3_hit"))
    be_count = sum(1 for t in trades if t.get("breakeven_set"))

    print(f"\n  TP Hit Rates: TP1={tp1_count}/{len(trades)} ({tp1_count/len(trades)*100:.0f}%) | "
          f"TP2={tp2_count}/{len(trades)} ({tp2_count/len(trades)*100:.0f}%) | "
          f"TP3={tp3_count}/{len(trades)} ({tp3_count/len(trades)*100:.0f}%)")
    print(f"  Breakeven Protection: {be_count}/{len(trades)} ({be_count/len(trades)*100:.0f}%)")
    print("=" * 64)


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

async def run_single_tf(config, symbols, start, end, balance, trigger_tf="1m", verbose=False):
    """Run a single backtest with given trigger TF. Returns (result, trigger_tf)."""
    bt = ScalpBacktester(config, trigger_tf=trigger_tf)
    bt.strategy.max_signals_hr = 999
    bt.strategy.cooldown_sec = 0
    bt.strategy._session_gate_enabled = False
    bt.COOLDOWN_SEC = 0
    result = await bt.run(symbols, start, end, balance)
    return result, trigger_tf


async def main():
    parser = argparse.ArgumentParser(description="Backtest the crypto trading strategy")
    parser.add_argument("--start", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    parser.add_argument("--balance", type=float, default=10000.0)
    parser.add_argument("--trigger-tf", default="1m", help="Primary trigger timeframe (1m, 3m, 5m, 10m, 15m)")
    parser.add_argument("--compare", action="store_true", help="Compare ALL timeframes (1m, 3m, 5m, 10m, 15m)")
    parser.add_argument("--export", action="store_true", help="Export CSV + charts")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Default to last 7 days
    if args.end is None:
        args.end = datetime.utcnow().strftime("%Y-%m-%d")
    if args.start is None:
        end_dt = datetime.fromisoformat(args.end)
        args.start = (end_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    config = get_config()

    if args.compare:
        # ── MULTI-TF COMPARISON MODE ──
        timeframes = ["1m", "3m", "5m", "10m", "15m"]
        results = []

        for tf in timeframes:
            logger.info("\n" + "=" * 60)
            logger.info("TESTING TIMEFRAME: %s", tf)
            logger.info("=" * 60)
            result, used_tf = await run_single_tf(
                config, args.symbols, args.start, args.end, args.balance, trigger_tf=tf
            )
            results.append((tf, result))

        # Print comparison table
        print("\n" + "=" * 100)
        print("  MULTI-TIMEFRAME COMPARISON")
        print("=" * 100)
        print(f"  Period: {args.start} to {args.end} | Symbols: {', '.join(args.symbols)}")
        print("-" * 100)
        print(f"  {'TF':>4s} | {'Trades':>6s} | {'WR':>5s} | {'Return':>10s} | {'PF':>5s} | {'MaxDD':>8s} | {'TP1%':>5s} | {'TP2%':>5s} | {'Avg Dur':>10s} | {'TimeStop%':>9s} | {'Fees':>8s}")
        print("-" * 100)

        for tf, result in results:
            trades = result.total_trades
            if trades == 0:
                print(f"  {tf:>4s} | {0:>6d} | {'N/A':>5s} | {'$0.00':>10s} | {'N/A':>5s} | {'$0.00':>8s} | {'N/A':>5s} | {'N/A':>5s} | {'N/A':>10s} | {'N/A':>9s} | {'$0.00':>8s}")
                continue

            wr = result.win_rate * 100 if hasattr(result, 'win_rate') else 0
            ret = result.total_return if hasattr(result, 'total_return') else 0
            pf = result.profit_factor if hasattr(result, 'profit_factor') else 0
            dd = result.max_drawdown if hasattr(result, 'max_drawdown') else 0
            fees = result.total_fees_paid if hasattr(result, 'total_fees_paid') else 0

            # Extract from trades list
            tp1_hits = sum(1 for t in result.trades if t.get("tp1_hit"))
            tp2_hits = sum(1 for t in result.trades if t.get("tp2_hit"))
            tp1_pct = tp1_hits / trades * 100 if trades > 0 else 0
            tp2_pct = tp2_hits / trades * 100 if trades > 0 else 0

            time_stops = sum(1 for t in result.trades if t.get("exit_reason") == "time_stop")
            ts_pct = time_stops / trades * 100 if trades > 0 else 0

            durations = [t.get("duration_sec", 0) for t in result.trades if t.get("duration_sec")]
            avg_dur = sum(durations) / len(durations) if durations else 0
            dur_str = f"{int(avg_dur//3600)}h {int((avg_dur%3600)//60)}m"

            ret_str = f"${ret:+.2f}"
            pf_str = f"{pf:.2f}" if pf > 0 else "0.00"

            print(f"  {tf:>4s} | {trades:>6d} | {wr:>4.1f}% | {ret_str:>10s} | {pf_str:>5s} | ${dd:>7.2f} | {tp1_pct:>4.0f}% | {tp2_pct:>4.0f}% | {dur_str:>10s} | {ts_pct:>8.0f}% | ${fees:>7.2f}")

        print("=" * 100)
        # Highlight best
        profitable = [(tf, r) for tf, r in results if r.total_trades > 0 and hasattr(r, 'total_return') and r.total_return > 0]
        if profitable:
            best = max(profitable, key=lambda x: x[1].total_return)
            print(f"\n  BEST: {best[0]} → ${best[1].total_return:+.2f} return")
        else:
            least_loss = min([(tf, r) for tf, r in results if r.total_trades > 0], key=lambda x: abs(x[1].total_return), default=None)
            if least_loss:
                print(f"\n  LEAST LOSS: {least_loss[0]} → ${least_loss[1].total_return:+.2f}")
        return

    # ── SINGLE TF MODE ──
    result, _ = await run_single_tf(
        config, args.symbols, args.start, args.end, args.balance, trigger_tf=args.trigger_tf
    )

    # Print report
    print(result.summary())
    print_extended_report(result)

    # Export if requested
    if args.export:
        out_dir = Path(__file__).resolve().parent / "backtest" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

        csv_path = str(out_dir / f"trades_{ts}.csv")
        result.export_csv(csv_path)
        print(f"\nTrades exported: {csv_path}")

        try:
            eq_path = str(out_dir / f"equity_{ts}.png")
            result.plot_equity_curve(eq_path)
            print(f"Equity curve: {eq_path}")
        except Exception as exc:
            print(f"Could not plot equity curve: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
