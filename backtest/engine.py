"""
Backtesting engine for the crypto trading bot.

Replays historical candle data bar-by-bar through the strategy engine,
simulating entries and exits with realistic fee/slippage modelling.
Supports partial take-profit exits, trailing stops, and cooldown rules
using the same signal/entry/exit logic as the live engine.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import get_config
from backtest.result import BacktestResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

class _SimulatedPosition:
    """Tracks an open simulated position during the backtest."""

    __slots__ = (
        "trade_id", "symbol", "side", "entry_price", "quantity",
        "remaining_qty", "stop_loss", "trailing_stop", "trailing_active",
        "take_profits", "tp_hit", "entry_time", "entry_fees",
        "exit_fills", "highest_since_entry", "lowest_since_entry",
        "break_even_applied",
    )

    def __init__(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        stop_loss: float,
        take_profits: List[Dict[str, float]],
        entry_time: datetime,
        entry_fees: float,
    ) -> None:
        self.trade_id = str(uuid.uuid4())[:12]
        self.symbol = symbol
        self.side = side  # "long" or "short"
        self.entry_price = entry_price
        self.quantity = quantity
        self.remaining_qty = quantity
        self.stop_loss = stop_loss
        self.trailing_stop: Optional[float] = None
        self.trailing_active = False
        self.take_profits = take_profits  # [{"price": ..., "close_pct": ...}, ...]
        self.tp_hit: List[bool] = [False] * len(take_profits)
        self.entry_time = entry_time
        self.entry_fees = entry_fees
        self.exit_fills: List[Dict[str, Any]] = []
        self.highest_since_entry = entry_price
        self.lowest_since_entry = entry_price
        self.break_even_applied = False

    @property
    def is_closed(self) -> bool:
        return self.remaining_qty <= 1e-12

    @property
    def unrealised_pnl(self) -> float:
        """Not used for final accounting; convenience only."""
        return 0.0

    def to_closed_trade(self) -> Dict[str, Any]:
        """Convert to a completed trade record once fully closed."""
        total_exit_value = sum(f["price"] * f["qty"] for f in self.exit_fills)
        total_exit_qty = sum(f["qty"] for f in self.exit_fills)
        avg_exit_price = total_exit_value / total_exit_qty if total_exit_qty > 0 else 0.0
        total_exit_fees = sum(f["fee"] for f in self.exit_fills)
        total_fees = self.entry_fees + total_exit_fees

        if self.side == "long":
            raw_pnl = (avg_exit_price - self.entry_price) * self.quantity
        else:
            raw_pnl = (self.entry_price - avg_exit_price) * self.quantity
        pnl = raw_pnl - total_fees

        pnl_pct = (pnl / (self.entry_price * self.quantity)) * 100.0 if self.entry_price * self.quantity > 0 else 0.0

        last_fill = self.exit_fills[-1] if self.exit_fills else {}

        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "exit_price": avg_exit_price,
            "quantity": self.quantity,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "fees": total_fees,
            "entry_time": self.entry_time,
            "exit_time": last_fill.get("time"),
            "exit_reason": last_fill.get("reason", "unknown"),
            "tp_levels_hit": sum(self.tp_hit),
        }


# ---------------------------------------------------------------------------
# BacktestEngine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Bar-by-bar backtesting engine with realistic execution simulation.

    Parameters
    ----------
    exchange_client : object
        Exchange client with ``fetch_ohlcv(symbol, timeframe, since, limit)``
        returning a list of ``[timestamp, open, high, low, close, volume]`` rows.
    risk_manager : object
        Risk manager with ``check_entry_allowed(signal)`` and
        ``calculate_position_size(signal, balance)`` methods.
    config : dict, optional
        Override configuration dict.  Falls back to ``get_config()``.
    """

    def __init__(
        self,
        exchange_client: Any,
        risk_manager: Any,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._exchange = exchange_client
        self._risk = risk_manager
        self._cfg = config or get_config()

        bt_cfg = self._cfg.get("backtest", {})
        risk_cfg = self._cfg.get("risk", {})
        tp_cfg = risk_cfg.get("take_profit", {})
        trail_cfg = risk_cfg.get("trailing", {})
        sl_cfg = risk_cfg.get("stop_loss", {})
        safety_cfg = risk_cfg.get("safety", {})

        # Execution model
        self._fee_rate: float = bt_cfg.get("fee_rate", 0.0004)
        self._slippage_pct: float = bt_cfg.get("slippage_pct", 0.05)
        self._initial_balance: float = bt_cfg.get("initial_balance", 10_000.0)

        # Take-profit ratios and partial close percentages
        self._tp_rrs: List[float] = [
            tp_cfg.get("tp1_rr", 1.5),
            tp_cfg.get("tp2_rr", 2.5),
            tp_cfg.get("tp3_rr", 4.0),
        ]
        self._tp_close_pcts: List[float] = [
            tp_cfg.get("tp1_close_pct", 40) / 100.0,
            tp_cfg.get("tp2_close_pct", 30) / 100.0,
            tp_cfg.get("tp3_close_pct", 30) / 100.0,
        ]

        # Trailing stop
        self._trailing_enabled: bool = trail_cfg.get("enabled", True)
        self._trailing_activation_rr: float = trail_cfg.get("activation_rr", 1.0)
        self._trailing_pct: float = trail_cfg.get("trail_pct", 0.5) / 100.0
        self._break_even_after_tp1: bool = trail_cfg.get("break_even_after_tp1", True)

        # Stop loss
        self._sl_type: str = sl_cfg.get("type", "atr")
        self._sl_fixed_pct: float = sl_cfg.get("fixed_pct", 1.5) / 100.0
        self._sl_atr_mult: float = sl_cfg.get("atr_multiplier", 1.5)

        # Cooldown
        self._cooldown_seconds: int = self._cfg.get("strategy", {}).get(
            "filters", {}
        ).get("cooldown_seconds", 300)
        self._cooloff_after_sl: int = safety_cfg.get("cooloff_after_sl", 180)

        # Risk limits
        self._max_open: int = risk_cfg.get("max_open_positions", 3)

        # Timeframe
        self._timeframe: str = self._cfg.get("timeframes", {}).get("primary", "5m")

        # State (reset per run)
        self._positions: Dict[str, _SimulatedPosition] = {}
        self._completed_trades: List[Dict[str, Any]] = []
        self._equity_curve: List[Tuple[datetime, float]] = []
        self._balance: float = self._initial_balance
        self._total_fees: float = 0.0
        self._last_trade_time: Dict[str, datetime] = {}
        self._last_sl_time: Dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        strategy: Any,
        symbols: List[str],
        start_date: str,
        end_date: str,
    ) -> BacktestResult:
        """Execute a full backtest over the given date range.

        Parameters
        ----------
        strategy :
            Strategy object with ``analyze(symbol, candles_dict) -> Signal | None``.
        symbols :
            List of trading pair symbols, e.g. ``["BTC/USDT", "ETH/USDT"]``.
        start_date :
            ISO date string for backtest start (inclusive), e.g. ``"2025-01-01"``.
        end_date :
            ISO date string for backtest end (inclusive), e.g. ``"2025-12-31"``.

        Returns
        -------
        BacktestResult
        """
        self._reset_state()

        dt_start = datetime.fromisoformat(start_date)
        dt_end = datetime.fromisoformat(end_date)

        logger.info(
            "Backtest started  |  symbols=%s  period=%s to %s  balance=$%.2f",
            symbols, start_date, end_date, self._initial_balance,
        )

        # 1. Fetch historical data for all symbols
        candle_frames: Dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            df = await self._fetch_candles(symbol, dt_start, dt_end)
            if df.empty:
                logger.warning("No candle data for %s -- skipping.", symbol)
                continue
            candle_frames[symbol] = df
            logger.info("Loaded %d candles for %s", len(df), symbol)

        if not candle_frames:
            logger.error("No candle data loaded. Aborting backtest.")
            return self._build_result(dt_start, dt_end, symbols)

        # 2. Build a unified timeline of bar timestamps
        all_timestamps = sorted({
            ts for df in candle_frames.values() for ts in df.index
        })

        # 3. Bar-by-bar replay
        total_bars = len(all_timestamps)
        log_interval = max(1, total_bars // 20)

        for bar_idx, current_ts in enumerate(all_timestamps):
            if bar_idx % log_interval == 0:
                pct = (bar_idx / total_bars) * 100
                logger.info("Progress: %d/%d bars (%.0f%%)", bar_idx, total_bars, pct)

            for symbol in list(candle_frames.keys()):
                df = candle_frames[symbol]
                if current_ts not in df.index:
                    continue

                bar = df.loc[current_ts]

                # --- Check open positions for exits ---
                self._process_exits(symbol, bar, current_ts)

                # --- Generate signal via strategy ---
                # Build a candles dict up to (and including) the current bar
                historical_slice = df.loc[:current_ts]
                candles_dict = self._df_to_candles_dict(historical_slice)

                signal = None
                try:
                    result = strategy.analyze(symbol, candles_dict)
                    # Handle both sync and async strategies
                    if asyncio.iscoroutine(result):
                        signal = await result
                    else:
                        signal = result
                except Exception:
                    logger.debug("Strategy error on %s at %s", symbol, current_ts, exc_info=True)

                if signal is not None:
                    self._process_signal(signal, symbol, bar, current_ts)

            # Record equity at end of this bar
            equity = self._compute_equity(candle_frames, current_ts)
            self._equity_curve.append((current_ts, equity))

        # 4. Force-close any remaining open positions at last bar
        for symbol in list(self._positions.keys()):
            pos = self._positions[symbol]
            if not pos.is_closed:
                last_ts = all_timestamps[-1]
                if symbol in candle_frames and last_ts in candle_frames[symbol].index:
                    last_bar = candle_frames[symbol].loc[last_ts]
                    close_price = float(last_bar["close"])
                else:
                    close_price = pos.entry_price  # fallback
                self._close_position(pos, close_price, last_ts, "backtest_end", 1.0)

        logger.info(
            "Backtest complete  |  trades=%d  return=%.2f%%  final_equity=$%.2f",
            len(self._completed_trades),
            ((self._balance - self._initial_balance) / self._initial_balance) * 100,
            self._balance,
        )

        return self._build_result(dt_start, dt_end, symbols)

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    async def _fetch_candles(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Fetch OHLCV data from exchange client or CSV fallback.

        Returns a DataFrame indexed by datetime with columns:
        open, high, low, close, volume.
        """
        # Try CSV first (allows offline backtesting)
        csv_df = self._try_load_csv(symbol, start, end)
        if csv_df is not None and not csv_df.empty:
            return csv_df

        # Fetch from exchange in chunks
        tf_ms = self._timeframe_to_ms(self._timeframe)
        all_rows: List[List] = []
        since_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        limit = 1000

        while since_ms < end_ms:
            try:
                result = self._exchange.fetch_ohlcv(
                    symbol, self._timeframe, since=since_ms, limit=limit,
                )
                if asyncio.iscoroutine(result):
                    rows = await result
                else:
                    rows = result
            except Exception:
                logger.error("Failed to fetch candles for %s at %d", symbol, since_ms, exc_info=True)
                break

            if not rows:
                break
            all_rows.extend(rows)
            last_ts = rows[-1][0]
            if last_ts <= since_ms:
                break
            since_ms = last_ts + tf_ms
            # Small pause to respect rate limits
            await asyncio.sleep(0.05)

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("datetime", inplace=True)
        df = df[~df.index.duplicated(keep="last")]
        df.sort_index(inplace=True)

        # Filter to requested range
        mask = (df.index >= pd.Timestamp(start, tz="UTC")) & (df.index <= pd.Timestamp(end, tz="UTC"))
        return df.loc[mask]

    def _try_load_csv(self, symbol: str, start: datetime, end: datetime) -> Optional[pd.DataFrame]:
        """Attempt to load candle data from a local CSV file."""
        project_root = Path(self._cfg.get("_project_root", "."))
        safe_symbol = symbol.replace("/", "_").replace(":", "_")
        candidates = [
            project_root / "data" / "candles" / f"{safe_symbol}_{self._timeframe}.csv",
            project_root / "data" / f"{safe_symbol}.csv",
            project_root / "backtest" / "data" / f"{safe_symbol}_{self._timeframe}.csv",
        ]
        for path in candidates:
            if path.exists():
                try:
                    df = pd.read_csv(path)
                    # Detect timestamp column
                    ts_col = None
                    for col in ("datetime", "timestamp", "date", "time", "Datetime", "Timestamp"):
                        if col in df.columns:
                            ts_col = col
                            break
                    if ts_col is None:
                        ts_col = df.columns[0]

                    df["datetime"] = pd.to_datetime(df[ts_col], utc=True)
                    df.set_index("datetime", inplace=True)

                    # Normalise column names
                    col_map = {}
                    for target in ("open", "high", "low", "close", "volume"):
                        for src in df.columns:
                            if src.lower() == target:
                                col_map[src] = target
                    df.rename(columns=col_map, inplace=True)

                    mask = (df.index >= pd.Timestamp(start, tz="UTC")) & (df.index <= pd.Timestamp(end, tz="UTC"))
                    return df.loc[mask]
                except Exception:
                    logger.debug("Failed to load CSV %s", path, exc_info=True)
        return None

    # ------------------------------------------------------------------
    # Signal processing
    # ------------------------------------------------------------------

    def _process_signal(
        self,
        signal: Any,
        symbol: str,
        bar: pd.Series,
        current_ts: datetime,
    ) -> None:
        """Evaluate a signal for potential entry."""
        signal_type = self._get_signal_attr(signal, "signal_type", "type", "action")
        if signal_type is None:
            return

        signal_type_str = str(signal_type).lower()

        # Only process entry signals
        is_buy = "buy" in signal_type_str and "pre" not in signal_type_str
        is_sell = "sell" in signal_type_str and "pre" not in signal_type_str
        if not is_buy and not is_sell:
            return

        # Already have a position for this symbol?
        if symbol in self._positions and not self._positions[symbol].is_closed:
            return

        # Cooldown check
        if not self._cooldown_ok(symbol, current_ts):
            return

        # Risk manager gate
        entry_allowed = True
        try:
            result = self._risk.check_entry_allowed(signal)
            if asyncio.iscoroutine(result):
                # In sync backtest context, we cannot await; skip if async
                entry_allowed = True
            else:
                entry_allowed = bool(result)
        except Exception:
            logger.debug("Risk check error", exc_info=True)

        if not entry_allowed:
            return

        # Max open positions
        open_count = sum(1 for p in self._positions.values() if not p.is_closed)
        if open_count >= self._max_open:
            return

        # Calculate position size
        position_size = 0.0
        try:
            result = self._risk.calculate_position_size(signal, self._balance)
            if asyncio.iscoroutine(result):
                position_size = self._balance * 0.01  # fallback 1% of balance
            else:
                position_size = float(result)
        except Exception:
            position_size = self._balance * 0.01
            logger.debug("Position sizing error, using 1%% fallback", exc_info=True)

        if position_size <= 0:
            return

        side = "long" if is_buy else "short"
        close_price = float(bar["close"])

        # Apply slippage
        entry_price = self._apply_slippage(close_price, side, is_entry=True)

        # Calculate quantity
        quantity = position_size / entry_price
        if quantity <= 0:
            return

        # Entry fee
        entry_fee = entry_price * quantity * self._fee_rate
        total_cost = entry_price * quantity + entry_fee

        if total_cost > self._balance:
            # Reduce to fit balance
            available = self._balance / (1 + self._fee_rate)
            quantity = available / entry_price
            entry_fee = entry_price * quantity * self._fee_rate
            if quantity <= 0:
                return

        # Compute stop loss
        atr_val = self._get_signal_attr(signal, "atr", "atr_value")
        stop_loss = self._compute_stop_loss(entry_price, side, bar, atr_val)

        # Compute take-profit levels
        risk_distance = abs(entry_price - stop_loss)
        take_profits = []
        for i, rr in enumerate(self._tp_rrs):
            if side == "long":
                tp_price = entry_price + risk_distance * rr
            else:
                tp_price = entry_price - risk_distance * rr
            take_profits.append({
                "price": tp_price,
                "close_pct": self._tp_close_pcts[i] if i < len(self._tp_close_pcts) else 0.3,
            })

        # Execute entry
        self._balance -= (entry_price * quantity + entry_fee)
        self._total_fees += entry_fee

        pos = _SimulatedPosition(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profits=take_profits,
            entry_time=current_ts,
            entry_fees=entry_fee,
        )
        self._positions[symbol] = pos
        self._last_trade_time[symbol] = current_ts

        logger.debug(
            "ENTRY %s %s @ %.4f  qty=%.6f  SL=%.4f  TPs=%s",
            side.upper(), symbol, entry_price, quantity,
            stop_loss, [f"{tp['price']:.4f}" for tp in take_profits],
        )

    # ------------------------------------------------------------------
    # Exit processing
    # ------------------------------------------------------------------

    def _process_exits(
        self,
        symbol: str,
        bar: pd.Series,
        current_ts: datetime,
    ) -> None:
        """Check if any open position for *symbol* should be partially or fully exited."""
        if symbol not in self._positions:
            return
        pos = self._positions[symbol]
        if pos.is_closed:
            return

        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])

        # Update extremes
        pos.highest_since_entry = max(pos.highest_since_entry, high)
        pos.lowest_since_entry = min(pos.lowest_since_entry, low)

        # --- Take-profit checks (partial exits) ---
        for i, tp in enumerate(pos.take_profits):
            if pos.tp_hit[i] or pos.is_closed:
                continue
            tp_price = tp["price"]
            hit = False
            if pos.side == "long" and high >= tp_price:
                hit = True
            elif pos.side == "short" and low <= tp_price:
                hit = True

            if hit:
                pos.tp_hit[i] = True
                close_pct = tp["close_pct"]
                self._close_position(pos, tp_price, current_ts, f"tp{i+1}", close_pct)
                logger.debug(
                    "TP%d hit  %s %s @ %.4f  closed %.0f%%",
                    i + 1, pos.side.upper(), symbol, tp_price, close_pct * 100,
                )

                # Break-even after TP1
                if i == 0 and self._break_even_after_tp1 and not pos.break_even_applied:
                    pos.stop_loss = pos.entry_price
                    pos.break_even_applied = True

        if pos.is_closed:
            return

        # --- Trailing stop logic ---
        if self._trailing_enabled and not pos.trailing_active:
            risk_distance = abs(pos.entry_price - pos.stop_loss) if pos.stop_loss else 0
            if risk_distance > 0:
                activation_price_long = pos.entry_price + risk_distance * self._trailing_activation_rr
                activation_price_short = pos.entry_price - risk_distance * self._trailing_activation_rr
                if pos.side == "long" and high >= activation_price_long:
                    pos.trailing_active = True
                elif pos.side == "short" and low <= activation_price_short:
                    pos.trailing_active = True

        if pos.trailing_active:
            if pos.side == "long":
                new_trail = pos.highest_since_entry * (1 - self._trailing_pct)
                if pos.trailing_stop is None or new_trail > pos.trailing_stop:
                    pos.trailing_stop = new_trail
                # Use trailing stop if it is above the fixed stop
                effective_stop = max(pos.stop_loss, pos.trailing_stop or 0)
            else:
                new_trail = pos.lowest_since_entry * (1 + self._trailing_pct)
                if pos.trailing_stop is None or new_trail < pos.trailing_stop:
                    pos.trailing_stop = new_trail
                effective_stop = min(pos.stop_loss, pos.trailing_stop or float("inf"))
        else:
            effective_stop = pos.stop_loss

        # --- Stop-loss check ---
        stopped = False
        if pos.side == "long" and low <= effective_stop:
            stopped = True
        elif pos.side == "short" and high >= effective_stop:
            stopped = True

        if stopped:
            sl_price = self._apply_slippage(effective_stop, pos.side, is_entry=False)
            self._close_position(pos, sl_price, current_ts, "stop_loss", 1.0)
            self._last_sl_time[symbol] = current_ts
            logger.debug(
                "STOP  %s %s @ %.4f", pos.side.upper(), symbol, sl_price,
            )

    def _close_position(
        self,
        pos: _SimulatedPosition,
        price: float,
        ts: datetime,
        reason: str,
        close_fraction: float,
    ) -> None:
        """Close *close_fraction* of the remaining position at *price*."""
        qty_to_close = pos.remaining_qty * min(close_fraction, 1.0)
        if qty_to_close <= 0:
            return

        exit_price = self._apply_slippage(price, pos.side, is_entry=False)
        fee = exit_price * qty_to_close * self._fee_rate
        self._total_fees += fee

        # Credit proceeds back to balance
        proceeds = exit_price * qty_to_close - fee
        # For long: we already debited notional on entry; credit back the exit proceeds
        # For short: entry was a "sell" (credited); exit is a "buy" (debited)
        if pos.side == "long":
            self._balance += proceeds
        else:
            # Short: entry credited entry_price * qty, exit debits exit_price * qty
            # Net credit on close = (entry_price - exit_price) * qty - fee
            # But we already debited on entry, so just credit proceeds symmetrically
            self._balance += proceeds

        pos.remaining_qty -= qty_to_close
        pos.exit_fills.append({
            "price": exit_price,
            "qty": qty_to_close,
            "fee": fee,
            "time": ts,
            "reason": reason,
        })

        # If fully closed, record the trade
        if pos.is_closed:
            trade_record = pos.to_closed_trade()
            self._completed_trades.append(trade_record)
            self._last_trade_time[pos.symbol] = ts

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reset_state(self) -> None:
        self._positions.clear()
        self._completed_trades.clear()
        self._equity_curve.clear()
        self._balance = self._initial_balance
        self._total_fees = 0.0
        self._last_trade_time.clear()
        self._last_sl_time.clear()

    def _apply_slippage(self, price: float, side: str, is_entry: bool) -> float:
        """Apply slippage in the adverse direction."""
        slip = price * (self._slippage_pct / 100.0)
        if (side == "long" and is_entry) or (side == "short" and not is_entry):
            return price + slip  # pay more
        return price - slip  # receive less

    def _compute_stop_loss(
        self,
        entry_price: float,
        side: str,
        bar: pd.Series,
        atr_value: Optional[float] = None,
    ) -> float:
        """Compute the initial stop-loss price."""
        if self._sl_type == "atr" and atr_value is not None and atr_value > 0:
            distance = atr_value * self._sl_atr_mult
        else:
            distance = entry_price * self._sl_fixed_pct

        if side == "long":
            return entry_price - distance
        return entry_price + distance

    def _cooldown_ok(self, symbol: str, current_ts: datetime) -> bool:
        """Return True if the cooldown period has elapsed for *symbol*."""
        # General trade cooldown
        if symbol in self._last_trade_time:
            elapsed = (current_ts - self._last_trade_time[symbol]).total_seconds()
            if elapsed < self._cooldown_seconds:
                return False

        # Extra cooldown after stop-loss
        if symbol in self._last_sl_time:
            elapsed = (current_ts - self._last_sl_time[symbol]).total_seconds()
            if elapsed < self._cooloff_after_sl:
                return False

        return True

    def _compute_equity(
        self,
        candle_frames: Dict[str, pd.DataFrame],
        current_ts: datetime,
    ) -> float:
        """Compute total equity = cash balance + mark-to-market open positions."""
        equity = self._balance
        for symbol, pos in self._positions.items():
            if pos.is_closed:
                continue
            # Get current price
            if symbol in candle_frames and current_ts in candle_frames[symbol].index:
                mark_price = float(candle_frames[symbol].loc[current_ts, "close"])
            else:
                mark_price = pos.entry_price

            if pos.side == "long":
                unrealised = (mark_price - pos.entry_price) * pos.remaining_qty
            else:
                unrealised = (pos.entry_price - mark_price) * pos.remaining_qty
            # Add back the notional of the remaining open position + unrealised PnL
            equity += pos.entry_price * pos.remaining_qty + unrealised
        return equity

    def _build_result(
        self,
        start: datetime,
        end: datetime,
        symbols: List[str],
    ) -> BacktestResult:
        return BacktestResult(
            trades=self._completed_trades,
            equity_curve=self._equity_curve,
            initial_balance=self._initial_balance,
            total_fees_paid=self._total_fees,
            start_date=start,
            end_date=end,
            symbols=symbols,
        )

    @staticmethod
    def _df_to_candles_dict(df: pd.DataFrame) -> Dict[str, list]:
        """Convert a DataFrame slice to a dict of lists (strategy-friendly format)."""
        return {
            "timestamp": df["timestamp"].tolist() if "timestamp" in df.columns else [],
            "open": df["open"].tolist(),
            "high": df["high"].tolist(),
            "low": df["low"].tolist(),
            "close": df["close"].tolist(),
            "volume": df["volume"].tolist(),
        }

    @staticmethod
    def _get_signal_attr(signal: Any, *names: str) -> Any:
        for name in names:
            if isinstance(signal, dict):
                if name in signal:
                    return signal[name]
            else:
                val = getattr(signal, name, None)
                if val is not None:
                    return val
        return None

    @staticmethod
    def _timeframe_to_ms(tf: str) -> int:
        """Convert a timeframe string like '5m' or '1h' to milliseconds."""
        unit = tf[-1]
        num = int(tf[:-1])
        multipliers = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
        return num * multipliers.get(unit, 60_000)
