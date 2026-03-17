"""
Multi-Strategy Router — runs multiple strategies in parallel and tags
each signal with its origin (investment vs scalp).

This is the strategy the orchestrator uses.  It delegates to:
  1. MomentumTrendStrategy  → "investment" signals (longer holds)
  2. ScalpStrategy           → "scalp" signals (quick trades)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pandas as pd

from strategies.base import BaseStrategy, Signal
from strategies.momentum_trend import MomentumTrendStrategy
from strategies.scalp_strategy import ScalpStrategy

logger = logging.getLogger(__name__)


class MultiStrategy(BaseStrategy):
    """Runs both investment and scalp strategies, tagging signals."""

    name = "multi_strategy"

    def __init__(self, config: Dict[str, Any]) -> None:
        self._investment = MomentumTrendStrategy(config)
        self._scalp = ScalpStrategy(config)

    def get_required_timeframes(self) -> List[str]:
        tfs = set(self._investment.get_required_timeframes())
        tfs.update(self._scalp.get_required_timeframes())
        return list(tfs)

    def analyze(
        self,
        symbol: str,
        candles_dict: Dict[str, pd.DataFrame],
    ) -> List[Signal]:
        """Run both strategies and merge results."""
        signals: List[Signal] = []

        # Investment strategy (trend-following, higher TF)
        try:
            inv_signals = self._investment.analyze(symbol, candles_dict)
            for sig in inv_signals:
                sig.metadata["strategy_type"] = "investment"
                sig.metadata["display_section"] = "Investment / Swing"
                signals.append(sig)
        except Exception as exc:
            logger.debug("Investment strategy error: %s", exc)

        # Scalp strategy (quick trades, lower TF)
        try:
            scalp_signals = self._scalp.analyze(symbol, candles_dict)
            for sig in scalp_signals:
                sig.metadata["strategy_type"] = "scalp"
                sig.metadata["display_section"] = "Quick Scalp"
                signals.append(sig)
        except Exception as exc:
            logger.debug("Scalp strategy error: %s", exc)

        return signals

    def clear_signal(self, symbol: str) -> None:
        self._investment.clear_signal(symbol)
        self._scalp.clear_signal(symbol)

    def record_stop_loss(self, symbol: str) -> None:
        self._investment.record_stop_loss(symbol)
        self._scalp.record_stop_loss(symbol)

    def has_active_signal(self, symbol: str) -> bool:
        return (
            self._investment.has_active_signal(symbol)
            or self._scalp.has_active_signal(symbol)
        )

    def get_scan_status(self) -> Dict[str, Any]:
        """Return last scan status from both sub-strategies for dashboard."""
        result: Dict[str, Any] = {}
        for symbol in set(
            list(self._scalp.last_scan_status.keys())
            + list(self._investment.last_scan_status.keys())
        ):
            result[symbol] = {
                "scalp": self._scalp.last_scan_status.get(symbol, {}),
                "investment": self._investment.last_scan_status.get(symbol, {}),
            }
        return result
