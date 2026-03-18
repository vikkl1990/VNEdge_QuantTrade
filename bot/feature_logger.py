"""
Feature Logger — Logs every signal evaluation's full feature vector to JSONL.

Purpose: Build the training dataset for future ML probability model.
Every triggered scanner result gets its features logged, along with the
outcome once the trade closes (backfilled asynchronously).

File: storage/feature_log.jsonl
Each line = one JSON object with:
  - timestamp, symbol, scanner, side, tier
  - features: rsi, adx, ema_slope, volume_ratio, atr_pct, regime, bb_bandwidth,
    htf_bias, spread_pct, candle_body_ratio, score_breakdown
  - outcome (backfilled): exit_r, win, mae_r, mfe_r, duration_s
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
_FEATURE_FILE = _STORAGE_DIR / "feature_log.jsonl"
_MAX_FILE_SIZE_MB = 50  # rotate at 50MB


class FeatureLogger:
    """Appends signal feature vectors to a JSONL file for future ML training."""

    def __init__(self) -> None:
        _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._pending_outcomes: Dict[str, int] = {}  # trade_id -> line_number
        self._total_logged = 0

    def log_signal(
        self,
        *,
        symbol: str,
        scanner: str,
        side: str,
        tier: str,
        score: float,
        weighted_score: float,
        entry_price: float = 0.0,
        stop_loss: float = 0.0,
        atr: float = 0.0,
        indicators: Optional[Dict[str, Any]] = None,
        regime: str = "",
        regime_confidence: float = 0.0,
        scanner_weight: float = 1.0,
        scanner_expectancy: float = 0.0,
        ev: float = 0.0,
        trade_id: str = "",
    ) -> None:
        """Log a complete feature vector for a triggered scanner.

        Called from scalp_strategy when a scanner triggers (regardless of
        whether the signal passes all filters).
        """
        ind = indicators or {}

        # Extract feature vector
        features = {
            # Price action
            "rsi": _safe_float(ind.get("rsi")),
            "adx": _safe_float(ind.get("adx")),
            "macd_hist": _safe_float(ind.get("macd_hist")),
            "bb_pct_b": _safe_float(ind.get("bb_pct_b")),
            "bb_bandwidth": _safe_float(ind.get("bb_bandwidth")),

            # Trend
            "ema_8": _safe_float(ind.get("ema_8")),
            "ema_21": _safe_float(ind.get("ema_21")),
            "ema_50": _safe_float(ind.get("ema_50")),
            "ema_slope": _safe_float(ind.get("ema_slope")),
            "supertrend_dir": _safe_float(ind.get("supertrend_dir")),

            # Volume
            "relative_volume": _safe_float(ind.get("relative_volume")),
            "mfi": _safe_float(ind.get("mfi")),
            "volume_spike": _safe_float(ind.get("volume_spike")),

            # Volatility
            "atr": _safe_float(atr),
            "atr_pct": round(atr / entry_price * 100, 4) if entry_price > 0 and atr > 0 else 0.0,

            # Spread / execution
            "spread_pct": _safe_float(ind.get("spread_pct")),

            # Context
            "regime": regime,
            "regime_confidence": regime_confidence,
            "htf_bias": _safe_float(ind.get("htf_bias")),
            "close": _safe_float(ind.get("close")),

            # Candle structure (last candle)
            "candle_body_ratio": _safe_float(ind.get("candle_body_ratio")),
        }

        record = {
            "ts": datetime.now(_IST).isoformat(),
            "symbol": symbol,
            "scanner": scanner,
            "side": side,
            "tier": tier,
            "score": score,
            "weighted_score": weighted_score,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "scanner_weight": scanner_weight,
            "scanner_expectancy": scanner_expectancy,
            "ev": round(ev, 4),
            "trade_id": trade_id,
            "features": features,
            # Outcome — backfilled when trade closes
            "outcome": None,
        }

        self._append(record)
        self._total_logged += 1

        if self._total_logged % 100 == 0:
            logger.info("Feature logger: %d signals logged to %s", self._total_logged, _FEATURE_FILE.name)

    def backfill_outcome(self, trade_id: str, outcome: Dict[str, Any]) -> None:
        """Backfill outcome data for a completed trade.

        Called when a trade closes. Reads the file, finds the matching
        trade_id, and updates the outcome field.

        For efficiency, only scans the last 500 lines.
        """
        if not trade_id or not _FEATURE_FILE.exists():
            return

        try:
            lines = _FEATURE_FILE.read_text(encoding="utf-8").strip().split("\n")
            updated = False

            # Scan from end (most recent) — only check last 500 lines
            search_range = min(len(lines), 500)
            for i in range(len(lines) - 1, len(lines) - search_range - 1, -1):
                if i < 0:
                    break
                try:
                    record = json.loads(lines[i])
                    if record.get("trade_id") == trade_id and record.get("outcome") is None:
                        record["outcome"] = {
                            "exit_r": round(outcome.get("exit_r", 0), 4),
                            "win": outcome.get("pnl_pct", 0) > 0,
                            "mae_r": round(outcome.get("mae_r", 0), 4),
                            "mfe_r": round(outcome.get("mfe_r", 0), 4),
                            "exit_reason": outcome.get("exit_reason", ""),
                            "duration_s": outcome.get("duration_s", 0),
                            "pnl_pct": round(outcome.get("pnl_pct", 0), 4),
                        }
                        lines[i] = json.dumps(record)
                        updated = True
                        break
                except (json.JSONDecodeError, IndexError):
                    continue

            if updated:
                _FEATURE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
                logger.debug("Feature outcome backfilled for trade %s", trade_id)

        except Exception as exc:
            logger.debug("Feature backfill failed for %s: %s", trade_id, exc)

    @property
    def total_logged(self) -> int:
        return self._total_logged

    def get_stats(self) -> Dict[str, Any]:
        """Return summary stats for dashboard."""
        file_size_mb = _FEATURE_FILE.stat().st_size / (1024 * 1024) if _FEATURE_FILE.exists() else 0
        return {
            "total_logged": self._total_logged,
            "file_size_mb": round(file_size_mb, 2),
            "file_path": str(_FEATURE_FILE),
        }

    def _append(self, record: Dict[str, Any]) -> None:
        """Append a single JSON record to the feature log."""
        try:
            # Check file size for rotation
            if _FEATURE_FILE.exists():
                size_mb = _FEATURE_FILE.stat().st_size / (1024 * 1024)
                if size_mb >= _MAX_FILE_SIZE_MB:
                    self._rotate()

            with open(_FEATURE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.debug("Feature log write failed: %s", exc)

    def _rotate(self) -> None:
        """Rotate the feature log file."""
        try:
            ts = datetime.now(_IST).strftime("%Y%m%d_%H%M%S")
            rotated = _STORAGE_DIR / f"feature_log_{ts}.jsonl"
            _FEATURE_FILE.rename(rotated)
            logger.info("Feature log rotated to %s", rotated.name)
        except Exception as exc:
            logger.warning("Feature log rotation failed: %s", exc)


def _safe_float(val: Any) -> float:
    """Convert to float safely, returning 0.0 on failure."""
    if val is None:
        return 0.0
    try:
        return round(float(val), 6)
    except (TypeError, ValueError):
        return 0.0
