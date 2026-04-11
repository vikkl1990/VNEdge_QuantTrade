"""
Scanner Backtester
==================
Runs each scanner against historical candle data with IDENTICAL logic
to live trading. Measures actual edge per scanner+side+regime+session.

Answers: "Does this scanner have statistically significant edge?"

Uses walk-forward validation:
- Train on months 1-2, validate on month 3
- If edge doesn't hold out-of-sample, it's fake
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ml_training.feature_builder import compute_indicators, build_labels

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).resolve().parent.parent / "storage" / "backtest_results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


class SimTrade:
    """A simulated trade for backtesting."""
    __slots__ = (
        "trade_id", "symbol", "side", "scanner", "entry_price", "stop_loss",
        "tp1", "tp2", "tp3", "entry_bar", "entry_time", "exit_price",
        "exit_bar", "exit_time", "exit_reason", "pnl_r", "mfe_r", "mae_r",
        "confidence", "grade", "regime", "session", "atr",
        "highest", "lowest", "initial_risk", "fees_pct",
    )

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.highest = self.entry_price
        self.lowest = self.entry_price
        self.exit_price = 0
        self.exit_bar = 0
        self.exit_time = None
        self.exit_reason = ""
        self.pnl_r = 0.0
        self.mfe_r = 0.0
        self.mae_r = 0.0

    def to_dict(self):
        return {k: getattr(self, k, None) for k in self.__slots__}


class ScannerBacktester:
    """Backtest scanners with live-identical logic."""

    def __init__(self, config: dict):
        self._config = config
        self._fee_rate = config.get("backtest", {}).get("fee_rate", 0.00059)  # Delta Exchange: 0.05% + 18% GST = 0.059% per side
        self._slippage_pct = config.get("backtest", {}).get("slippage_pct", 0.05) / 100
        self._scalper_window_btc = 27 * 60  # seconds
        self._scalper_window_other = 12 * 60
        self._results: Dict[str, List[dict]] = {}  # scanner → list of trades
        self._progress: Dict = {}

    def get_progress(self) -> Dict:
        return self._progress

    def get_results(self) -> Dict:
        return self._results

    def _detect_regime(self, row: pd.Series, df: pd.DataFrame, idx: int) -> str:
        """Simple regime detection matching live logic."""
        try:
            ema_21 = float(row.get("ema_21", 0))
            ema_50 = float(row.get("ema_50", 0))
            ema_200 = float(row.get("ema_200", 0))
            atr = float(row.get("atr_14", 0))
            avg_atr = df["atr_14"].iloc[max(0, idx-100):idx].mean() if idx > 100 else atr
            bb_width = float(row.get("bb_width", 0))
            close = float(row.get("close", 0))

            if avg_atr > 0 and atr / avg_atr < 0.7:
                return "quiet"

            if ema_21 > ema_50 > ema_200 and close > ema_21:
                return "trending_up"
            if ema_21 < ema_50 < ema_200 and close < ema_21:
                return "trending_down"

            if bb_width > 0:
                avg_bbw = df["bb_width"].iloc[max(0, idx-50):idx].mean()
                if avg_bbw > 0 and bb_width / avg_bbw > 1.5:
                    return "volatile"
                if avg_bbw > 0 and bb_width / avg_bbw < 0.5:
                    return "ranging"

            return "sideways"
        except Exception:
            return "sideways"

    def _detect_session(self, dt: datetime) -> str:
        """Detect trading session from UTC hour."""
        if hasattr(dt, 'hour'):
            h = dt.hour
        else:
            return "unknown"

        if 0 <= h < 6:
            return "asia_late"
        elif 6 <= h < 12:
            return "asia_early"
        elif 12 <= h < 18:
            return "europe"
        else:
            return "us"

    def _simulate_trade(self, df: pd.DataFrame, entry_idx: int,
                         side: str, entry_price: float, atr: float,
                         symbol: str, regime: str = "sideways",
                         trade_type: str = "SCALP") -> SimTrade:
        """Simulate a trade forward from entry_idx using live-identical exit logic.

        Phase 4.0 REFACTOR (2026-04-11):
        Delegates to bot.trade_simulator.simulate_trade which is the SINGLE
        source of truth for exit logic. Shared with candidate_trainer AND
        matches live bot's TRADE_TYPE_CONFIG.

        OLD BUG (pre-4.0):
          - Hardcoded sl_pct=0.0065 (0.65%) — didn't match live's ATR-based SL
          - Hardcoded TP at 1.5×ATR — didn't scale with actual risk
          - Resulting R-multiple math made every scanner look like a 17% WR loser
        """
        from bot.trade_simulator import simulate_trade, SimulatorConfig

        # Get config from live TRADE_TYPE_CONFIG
        config = SimulatorConfig.from_trade_type(trade_type)
        # Override fee rate to match backtester's setting
        config.fee_rate_per_side = self._fee_rate

        # Call the unified simulator
        outcome = simulate_trade(
            df=df,
            entry_idx=entry_idx,
            side=side,
            entry_price=entry_price,
            atr=atr,
            regime=regime,
            config=config,
            trade_type=trade_type,
        )

        # Populate SimTrade with outcome fields
        trade = SimTrade(
            trade_id=f"bt_{entry_idx}",
            symbol=symbol, side=side, scanner="",
            entry_price=entry_price,
            stop_loss=outcome.get("sl_initial", 0.0),
            tp1=0.0, tp2=0.0, tp3=0.0,  # not exposed by simulator (uses R units)
            entry_bar=entry_idx,
            entry_time=df.index[entry_idx] if hasattr(df.index[entry_idx], 'isoformat') else None,
            atr=atr,
            initial_risk=outcome.get("initial_risk", 0.0),
            confidence=0, grade="", regime=regime, session="",
            fees_pct=self._fee_rate * 2,  # round trip
        )
        trade.exit_price = outcome.get("exit_price", 0.0)
        trade.exit_bar = outcome.get("exit_bar", 0)
        trade.exit_reason = outcome.get("exit_reason", "")
        if trade.exit_bar and trade.exit_bar < len(df):
            trade.exit_time = df.index[trade.exit_bar] if hasattr(df.index[trade.exit_bar], 'isoformat') else None
        trade.highest = entry_price if side == "short" else entry_price * (1 + outcome.get("peak_mfe_r", 0) * 0.01)  # approx
        trade.lowest = entry_price if side == "long" else entry_price * (1 - outcome.get("peak_mfe_r", 0) * 0.01)
        trade.mfe_r = outcome.get("peak_mfe_r", 0.0)
        trade.mae_r = outcome.get("mae_r", 0.0)
        trade.pnl_r = outcome.get("pnl_r", 0.0)
        return trade

    def backtest_scanner(self, scanner_func, scanner_name: str,
                          df: pd.DataFrame, symbol: str,
                          htf_df: Optional[pd.DataFrame] = None) -> List[dict]:
        """Run a scanner against historical data and collect all trades.

        scanner_func should accept (row_index, df, symbol) and return
        a dict with {side, entry_price, confidence, grade} or None.
        """
        df = compute_indicators(df)
        trades = []
        cooldown_until = 0

        self._progress[scanner_name] = {
            "status": "running",
            "symbol": symbol,
            "total_bars": len(df),
            "processed": 0,
            "trades_found": 0,
        }

        # Need at least 200 bars for indicators to warm up
        start_idx = 200

        for i in range(start_idx, len(df) - 30):
            if i < cooldown_until:
                continue

            row = df.iloc[i]
            regime = self._detect_regime(row, df, i)
            session = self._detect_session(df.index[i]) if hasattr(df.index[i], 'hour') else "unknown"

            # Call scanner
            try:
                result = scanner_func(i, df, symbol)
            except Exception:
                continue

            if result is None:
                continue

            side = result.get("side", "long")
            entry_price = result.get("entry_price", float(row["close"]))
            confidence = result.get("confidence", 50)
            grade = result.get("grade", "C")

            # Apply slippage
            if side == "long":
                entry_price *= (1 + self._slippage_pct)
            else:
                entry_price *= (1 - self._slippage_pct)

            # Simulate trade
            trade = self._simulate_trade(df, i, side, entry_price,
                                          float(row["atr_14"]), symbol)
            trade.scanner = scanner_name
            trade.confidence = confidence
            trade.grade = grade
            trade.regime = regime
            trade.session = session

            trades.append(trade.to_dict())

            # Cooldown: skip next 3 bars minimum
            cooldown_until = i + 3

            if len(trades) % 100 == 0:
                self._progress[scanner_name]["trades_found"] = len(trades)

            self._progress[scanner_name]["processed"] = i

        self._progress[scanner_name] = {
            "status": "complete",
            "symbol": symbol,
            "total_bars": len(df),
            "processed": len(df),
            "trades_found": len(trades),
        }

        return trades

    def walk_forward_validate(self, trades: List[dict],
                               train_months: int = 2,
                               test_months: int = 1) -> Dict:
        """Walk-forward validation: train on N months, test on next M.

        Returns in-sample vs out-of-sample metrics to detect overfitting.
        """
        if not trades:
            return {"error": "no trades"}

        df = pd.DataFrame(trades)
        if "entry_time" not in df.columns or df["entry_time"].isna().all():
            return {"error": "no timestamps"}

        df["entry_time"] = pd.to_datetime(df["entry_time"])
        df.sort_values("entry_time", inplace=True)

        min_date = df["entry_time"].min()
        max_date = df["entry_time"].max()
        total_days = (max_date - min_date).days

        if total_days < 60:
            return {"error": f"insufficient data ({total_days} days, need 60+)"}

        # Split into windows
        windows = []
        window_start = min_date

        while window_start + timedelta(days=(train_months + test_months) * 30) <= max_date:
            train_end = window_start + timedelta(days=train_months * 30)
            test_end = train_end + timedelta(days=test_months * 30)

            train_trades = df[(df["entry_time"] >= window_start) & (df["entry_time"] < train_end)]
            test_trades = df[(df["entry_time"] >= train_end) & (df["entry_time"] < test_end)]

            if len(train_trades) >= 10 and len(test_trades) >= 5:
                train_metrics = self._calc_metrics(train_trades)
                test_metrics = self._calc_metrics(test_trades)
                windows.append({
                    "train_start": str(window_start.date()),
                    "train_end": str(train_end.date()),
                    "test_start": str(train_end.date()),
                    "test_end": str(test_end.date()),
                    "train": train_metrics,
                    "test": test_metrics,
                    "edge_holds": test_metrics.get("expectancy_r", 0) > 0,
                })

            window_start += timedelta(days=30)

        if not windows:
            return {"error": "insufficient trades per window"}

        edge_holds_count = sum(1 for w in windows if w["edge_holds"])

        return {
            "windows": windows,
            "total_windows": len(windows),
            "edge_holds_pct": round(edge_holds_count / len(windows) * 100, 1),
            "verdict": "VALID EDGE" if edge_holds_count / len(windows) >= 0.6 else "NO EDGE",
        }

    def _calc_metrics(self, df: pd.DataFrame) -> dict:
        """Calculate standard metrics from a trades DataFrame."""
        if isinstance(df, list):
            df = pd.DataFrame(df)
        if df.empty:
            return {}

        pnl_r = df["pnl_r"].astype(float)
        wins = pnl_r[pnl_r > 0]
        losses = pnl_r[pnl_r < 0]

        return {
            "trades": len(df),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / max(len(df), 1) * 100, 1),
            "expectancy_r": round(pnl_r.mean(), 4),
            "total_r": round(pnl_r.sum(), 2),
            "avg_win_r": round(wins.mean(), 3) if len(wins) > 0 else 0,
            "avg_loss_r": round(losses.mean(), 3) if len(losses) > 0 else 0,
            "profit_factor": round(wins.sum() / abs(losses.sum()), 2) if len(losses) > 0 and losses.sum() != 0 else 999,
            "max_dd_r": round(pnl_r.cumsum().cummax().subtract(pnl_r.cumsum()).max(), 2),
            "avg_mfe_r": round(df["mfe_r"].astype(float).mean(), 3) if "mfe_r" in df.columns else 0,
            "avg_mae_r": round(df["mae_r"].astype(float).mean(), 3) if "mae_r" in df.columns else 0,
        }

    def compute_calibrated_probabilities(self, trades: List[dict]) -> Dict:
        """Compute actual win probability per scanner+side+regime+session.

        This replaces the fake confidence score with real calibrated probability.
        """
        df = pd.DataFrame(trades)
        if df.empty:
            return {}

        result = {}

        # Per scanner+side
        for (scanner, side), group in df.groupby(["scanner", "side"]):
            key = f"{scanner}_{side}"
            wins = (group["pnl_r"] > 0).sum()
            total = len(group)
            result[key] = {
                "win_rate": round(wins / max(total, 1) * 100, 1),
                "trades": total,
                "expectancy_r": round(group["pnl_r"].mean(), 4),
                "sufficient_data": total >= 30,
            }

        # Per scanner+side+regime
        for (scanner, side, regime), group in df.groupby(["scanner", "side", "regime"]):
            key = f"{scanner}_{side}_{regime}"
            wins = (group["pnl_r"] > 0).sum()
            total = len(group)
            if total >= 10:
                result[key] = {
                    "win_rate": round(wins / max(total, 1) * 100, 1),
                    "trades": total,
                    "expectancy_r": round(group["pnl_r"].mean(), 4),
                    "sufficient_data": total >= 30,
                }

        # Per scanner+side+session
        for (scanner, side, session), group in df.groupby(["scanner", "side", "session"]):
            key = f"{scanner}_{side}_{session}"
            wins = (group["pnl_r"] > 0).sum()
            total = len(group)
            if total >= 10:
                result[key] = {
                    "win_rate": round(wins / max(total, 1) * 100, 1),
                    "trades": total,
                    "expectancy_r": round(group["pnl_r"].mean(), 4),
                    "sufficient_data": total >= 30,
                }

        return result

    def save_results(self, scanner_name: str, symbol: str,
                      trades: List[dict], metrics: dict):
        """Save backtest results to disk."""
        safe = f"{scanner_name}_{symbol.replace('/', '_')}".lower()
        path = RESULTS_DIR / f"{safe}.json"
        data = {
            "scanner": scanner_name,
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_trades": len(trades),
            "metrics": metrics,
            "trades": trades[-200:],  # keep last 200
        }
        path.write_text(json.dumps(data, default=str, indent=2))
        logger.info("Saved results: %s (%d trades)", path, len(trades))
