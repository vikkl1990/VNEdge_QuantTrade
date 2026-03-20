"""
Canonical Trade Dataset
========================
One clean table storing every signal and trade with full context.
This is the TRUTH SOURCE for ML, analytics, scanner ranking, and dashboard.

Each row = one signal event with:
- Setup metadata (scanner, side, symbol, regime, session)
- Feature values at signal time
- Trade outcome (entry, exit, MAE, MFE, result_r, slippage)
- Would-block flags (what would have been filtered)
- Confidence breakdown from scanner
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

DATASET_DIR = Path(__file__).resolve().parent.parent / "storage" / "trade_dataset"
DATASET_DIR.mkdir(parents=True, exist_ok=True)

DATASET_FILE = DATASET_DIR / "canonical_trades.parquet"
DATASET_CSV = DATASET_DIR / "canonical_trades.csv"


# Schema for canonical trade dataset
TRADE_COLUMNS = [
    # Metadata
    "timestamp", "symbol", "side", "scanner", "timeframe",
    "regime", "session", "stability",
    # Signal
    "confidence", "grade", "entry_price",
    # Features at signal time (top features only, not all 40+)
    "f_trend_strength", "f_dist_from_vwap", "f_atr_ratio",
    "f_vol_compression", "f_volume_zscore", "f_body_ratio",
    "f_range_vs_atr", "f_trend_change", "f_vol_change",
    "f_momentum_accel", "f_dist_from_ema21",
    # Trade outcome
    "exit_price", "exit_reason", "duration_sec",
    "pnl_pct", "pnl_usd", "result_r",
    "mae_r", "mfe_r", "slippage_pct",
    # Would-block flags (logged but not enforced in research mode)
    "would_block_regime", "would_block_htf",
    "would_block_ev", "would_block_session",
    "would_block_volatility", "would_block_duplicate",
    "would_block_conflict",
    # ML prediction (filled after model trains)
    "ml_probability", "ml_model_version",
]


class TradeDataset:
    """Manages the canonical trade dataset."""

    def __init__(self):
        self._trades: List[Dict] = []
        self._load_existing()

    def _load_existing(self):
        """Load existing dataset from disk."""
        if DATASET_FILE.exists():
            try:
                df = pd.read_parquet(DATASET_FILE)
                self._trades = df.to_dict("records")
                logger.info("Loaded %d existing trades from dataset", len(self._trades))
            except Exception:
                pass

    def add_trade(self, trade: Dict):
        """Add a completed trade to the dataset."""
        # Ensure all columns exist
        clean = {col: trade.get(col) for col in TRADE_COLUMNS}
        clean["timestamp"] = clean["timestamp"] or datetime.now(timezone.utc).isoformat()
        self._trades.append(clean)

        # Auto-save every 50 trades
        if len(self._trades) % 50 == 0:
            self.save()

    def add_signal(self, signal_data: Dict):
        """Add a signal event (may not result in a trade yet).
        Call update_trade_outcome() later when trade completes."""
        clean = {col: signal_data.get(col) for col in TRADE_COLUMNS}
        clean["timestamp"] = clean["timestamp"] or datetime.now(timezone.utc).isoformat()
        self._trades.append(clean)
        return len(self._trades) - 1  # return index for later update

    def update_trade_outcome(self, index: int, outcome: Dict):
        """Update a signal with its trade outcome."""
        if 0 <= index < len(self._trades):
            for key, val in outcome.items():
                if key in TRADE_COLUMNS:
                    self._trades[index][key] = val

    def save(self):
        """Save dataset to disk."""
        if not self._trades:
            return
        df = pd.DataFrame(self._trades)
        try:
            df.to_parquet(DATASET_FILE, index=False)
        except Exception:
            df.to_csv(DATASET_CSV, index=False)
        logger.info("Saved %d trades to dataset", len(df))

    def to_dataframe(self) -> pd.DataFrame:
        """Get dataset as DataFrame for analysis."""
        if not self._trades:
            return pd.DataFrame(columns=TRADE_COLUMNS)
        return pd.DataFrame(self._trades)

    def get_stats(self) -> Dict:
        """Get dataset summary stats."""
        if not self._trades:
            return {"total": 0}
        df = self.to_dataframe()
        completed = df[df["result_r"].notna()]
        return {
            "total_signals": len(df),
            "completed_trades": len(completed),
            "by_scanner": completed.groupby("scanner")["result_r"].agg(
                ["count", "mean"]
            ).to_dict("index") if len(completed) > 0 else {},
            "by_regime": completed.groupby("regime")["result_r"].agg(
                ["count", "mean"]
            ).to_dict("index") if len(completed) > 0 and "regime" in completed.columns else {},
        }
