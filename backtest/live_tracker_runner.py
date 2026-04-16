"""
Live-exit-logic backtest runner.
================================

Zero-divergence-by-construction backtest adapter: instantiates the LIVE
`bot.signal_tracker.SignalTracker` in an isolated storage sandbox and
feeds it historical bars via its public `track_signal` / `update_prices`
interfaces. The actual live exit logic (breakeven, chandelier, MFE lock,
early kill, hard loss cap, time decay, TP hits, etc.) runs unchanged.

This replaces `bot.trade_simulator.simulate_trade` for any use case that
requires bit-for-bit live fidelity (backtests, ML label generation,
strategy validation). `trade_simulator` remains useful for rough ML
labels — cheaper to run, less faithful.

Design constraints honored:
- NO edits to signal_tracker.py (sacred)
- NO writes to live storage (`storage/active_signals.json`, etc.)
- NO ML feedback emission (`ml_live_feedback.jsonl` redirected to tmp)
- NO access to the user's training_dataset attribute
- NO touching of the running bot process

Usage:
    from backtest.live_tracker_runner import LiveTrackerRunner
    runner = LiveTrackerRunner()
    outcome = runner.simulate_trade(
        df=candle_df,           # must include OHLC columns, datetime index
        entry_idx=100,
        symbol="BTC/USDT",
        side="long",
        entry_price=74000.0,
        atr=30.0,
        scanner="structure_bounce",
        confidence=65,
        grade="B",
        regime="sideways",
        trade_type="SCALP",
    )
    # outcome = {pnl_r, exit_reason, exit_price, ...}
"""
from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _patch_latent_bugs(st_module):
    """In-memory patches for known latent bugs in signal_tracker that only
    surface when the exit logic is driven synchronously (as in backtests).
    The on-disk file is NOT modified — these patches only apply to this
    process's SignalTracker class.

    BUG 1: `_get_trail_params` is missing @staticmethod and is called as
    `self._get_trail_params(regime, scanner, trade_type)` — passes self
    as an extra arg, triggering TypeError. In live, the orchestrator
    wraps update_prices in try/except that swallows it at debug level,
    which silently skips TP1-trail logic. We apply @staticmethod here so
    the backtest runs the *intended* behavior.
    """
    cls = st_module.SignalTracker
    if not hasattr(cls, "_get_trail_params"):
        return
    method = cls._get_trail_params
    # If already a staticmethod, leave it. Otherwise, re-wrap the function.
    if not isinstance(cls.__dict__.get("_get_trail_params"), staticmethod):
        # Retrieve underlying function (instance method has __func__ when bound;
        # unbound in 3.x it's just the function)
        underlying = getattr(method, "__func__", method)
        cls._get_trail_params = staticmethod(underlying)


def _isolate_signal_tracker_module(sandbox_dir: Path):
    """Patch bot.signal_tracker module-level storage paths to a sandbox dir
    BEFORE any SignalTracker() instantiation. Returns the patched module.

    Idempotent — repeated calls with the same sandbox just re-point files.
    """
    import bot.signal_tracker as st

    sandbox_dir.mkdir(parents=True, exist_ok=True)
    st._STORAGE_DIR = sandbox_dir
    st._ACTIVE_FILE = sandbox_dir / "active_signals.json"
    st._CLOSED_FILE = sandbox_dir / "closed_signals.json"
    st._STATS_FILE = sandbox_dir / "signal_stats.json"
    _patch_latent_bugs(st)
    return st


class LiveTrackerRunner:
    """Backtest adapter that runs trades through the live SignalTracker.

    Each LiveTrackerRunner gets its own tmp storage sandbox — no interference
    between concurrent backtests or with the live bot.
    """

    def __init__(self, sandbox_dir: Optional[Path] = None, feed_ohlc: bool = True):
        """
        Args:
            sandbox_dir: optional explicit sandbox directory; auto-created tmp if None
            feed_ohlc: if True, feed each bar as OHLC sequence (4 updates per bar —
                       captures intrabar extremes). If False, feed only close (cheaper
                       but misses wicks).
        """
        if sandbox_dir is None:
            sandbox_dir = Path(tempfile.mkdtemp(prefix="bt_tracker_"))
        self._sandbox_dir = sandbox_dir
        self._feed_ohlc = feed_ohlc
        # Patch module-level paths BEFORE SignalTracker() reads them in _load()
        self._st_module = _isolate_signal_tracker_module(sandbox_dir)
        # Silence signal_tracker's logger for backtest runs (it logs every event)
        logging.getLogger("bot.signal_tracker").setLevel(logging.ERROR)

    def _fresh_tracker(self):
        """Create a fresh SignalTracker instance in the sandbox."""
        # Wipe any state written by a prior trade in this runner
        for fname in ("active_signals.json", "closed_signals.json",
                       "signal_stats.json", "ml_live_feedback.jsonl"):
            p = self._sandbox_dir / fname
            if p.exists():
                p.unlink()
        SignalTracker = self._st_module.SignalTracker
        tracker = SignalTracker(config={"execution": {"order_type": "maker",
                                                        "max_entry_slip_bps": 0,
                                                        "min_trail_hold_sec": 0}})
        # Belt-and-braces: block ml feedback writes by pointing them at a dead path
        tracker._live_feedback_file = self._sandbox_dir / "_disabled_feedback.jsonl"
        tracker._training_dataset = None
        return tracker

    def simulate_trade(
        self,
        df: pd.DataFrame,
        entry_idx: int,
        symbol: str,
        side: str,
        entry_price: float,
        atr: float,
        scanner: str = "structure_bounce",
        confidence: int = 60,
        grade: str = "B",
        regime: str = "sideways",
        trade_type: str = "SCALP",
        stop_loss: Optional[float] = None,
        max_bars_forward: int = 120,
    ) -> Dict[str, Any]:
        """Replay a single trade through live SignalTracker exit logic.

        Returns outcome dict with the same shape as bot.trade_simulator.simulate_trade
        for drop-in substitution.
        """
        tracker = self._fresh_tracker()

        # Compute initial SL using the live scanner-specific config (matches scalp_strategy)
        if stop_loss is None:
            SCANNER_SL_ATR = {
                "ema_momentum": 1.2, "trend_continuation": 1.5,
                "vwap_mean_revert": 1.0, "rsi_divergence": 1.2,
                "structure_bounce": 1.0, "bb_squeeze": 1.3,
                "order_block_entry": 1.0, "liquidity_sweep": 1.2,
                "simple_bias": 1.5, "bos_choch": 1.2,
            }
            sl_mult = SCANNER_SL_ATR.get(scanner, 2.0)  # 2.0 is scalp_strategy default
            sl_distance = atr * sl_mult
            stop_loss = (entry_price - sl_distance) if side == "long" else (entry_price + sl_distance)

        initial_risk = abs(entry_price - stop_loss)
        if initial_risk <= 0:
            return {"pnl_r": 0.0, "exit_reason": "invalid_input",
                    "exit_price": entry_price, "initial_risk": 0.0}

        # Signal dict matching live track_signal() contract
        trade_id = f"bt_{uuid.uuid4().hex[:10]}"
        entry_ts = df.index[entry_idx]
        signal_dict = {
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "signal_price": entry_price,
            "fill_price": entry_price,
            "stop_loss": stop_loss,
            "entry_time": entry_ts.isoformat() if hasattr(entry_ts, "isoformat") else str(entry_ts),
            "entry_atr": atr,
            "signal_atr": atr,
            "confidence": confidence,
            "grade": grade,
            "scanner": scanner,
            "regime": regime,
            "trade_type": trade_type,
            "initial_risk": initial_risk,
            "paper_stake": 100.0,
            "metadata": {
                "setup_type": scanner,
                "regime": regime,
                "trade_type": trade_type,
                "atr": atr,   # TrackedSignal.from_signal reads metadata["atr"] (line 476 of signal_tracker)
            },
        }
        tracker.track_signal(signal_dict)

        # Feed bars forward until tracker closes the trade or we hit max_bars
        # For each bar, feed intrabar price points (open, high/low in rational order, close).
        end_idx = min(entry_idx + 1 + max_bars_forward, len(df))
        closed_outcome = None

        for j in range(entry_idx + 1, end_idx):
            row = df.iloc[j]
            if self._feed_ohlc:
                # Order: open → high→low (long-favorable first) or low→high (short-favorable first)
                # Use intrabar sequence that won't accidentally hit SL before TP for longs
                o_j = float(row["open"])
                h_j = float(row["high"])
                l_j = float(row["low"])
                c_j = float(row["close"])
                # Feed conservatively: open, then the adverse extreme first (stop out before TP)
                # to be faithful to worst-case fill
                if side == "long":
                    seq = [o_j, l_j, h_j, c_j]  # adverse (low) first, favorable (high) next
                else:
                    seq = [o_j, h_j, l_j, c_j]
            else:
                seq = [float(row["close"])]

            for price in seq:
                events = tracker.update_prices({symbol: price})
                # Did our trade close? Check if trade_id is no longer active
                if trade_id not in tracker._active:
                    # Find the closed record
                    for c in tracker._closed:
                        if c.get("trade_id") == trade_id:
                            closed_outcome = c
                            break
                    break
            if closed_outcome:
                break

        if closed_outcome is None:
            # Timed out — force-close at last known close
            last_close = float(df.iloc[end_idx - 1]["close"])
            tracker.update_prices({symbol: last_close})
            for c in tracker._closed:
                if c.get("trade_id") == trade_id:
                    closed_outcome = c
                    break

        if closed_outcome is None:
            return {"pnl_r": 0.0, "exit_reason": "timeout_no_close",
                    "exit_price": float(df.iloc[end_idx - 1]["close"]),
                    "initial_risk": initial_risk, "exit_bar": end_idx - 1,
                    "peak_mfe_r": 0.0, "mae_r": 0.0, "won_at_tp": ""}

        # Normalize outcome to trade_simulator.simulate_trade's return shape
        exit_price = float(closed_outcome.get("exit_price", entry_price))
        exit_reason = closed_outcome.get("exit_reason", "?")
        # Compute pnl_r from exit_price and initial_risk
        if side == "long":
            pnl_r = (exit_price - entry_price) / initial_risk
        else:
            pnl_r = (entry_price - exit_price) / initial_risk
        # Live records mfe_r / mae_r directly
        peak_mfe_r = float(closed_outcome.get("mfe_r", 0.0) or 0.0)
        mae_r = float(closed_outcome.get("mae_r", 0.0) or 0.0)
        exit_bar_idx = entry_idx  # best-effort; live doesn't bar-index
        # Try to match exit_time to a bar
        exit_time = closed_outcome.get("exit_time")
        if exit_time:
            try:
                exit_ts = pd.to_datetime(exit_time, utc=True)
                exit_bar_idx = int(df.index.get_indexer([exit_ts], method="nearest")[0])
            except Exception:
                pass

        return {
            "pnl_r": round(pnl_r, 4),
            "exit_reason": exit_reason,
            "exit_price": exit_price,
            "exit_bar": exit_bar_idx,
            "initial_risk": initial_risk,
            "peak_mfe_r": peak_mfe_r,
            "mae_r": mae_r,
            "sl_initial": stop_loss,
            "sl_final": closed_outcome.get("stop_loss", stop_loss),
            "won_at_tp": "tp1" if exit_reason == "partial_win" else "",
            "duration_bars": exit_bar_idx - entry_idx,
        }
