"""
Base classes and data structures for the strategy engine.

Defines the Signal dataclass that all strategies emit and the abstract
BaseStrategy interface that concrete strategies must implement.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from config.constants import MarketRegime, OrderSide, SignalType, TradeGrade


# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """Immutable representation of a trading signal emitted by a strategy."""

    # Identification
    symbol: str
    signal_type: SignalType
    side: OrderSide
    trade_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # Price levels
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profits: List[float] = field(default_factory=list)
    invalidation_level: float = 0.0

    # Quality metrics
    confidence: int = 0          # 0-100
    grade: TradeGrade = TradeGrade.REJECT
    risk_reward: float = 0.0
    reason: str = ""

    # Context
    regime: MarketRegime = MarketRegime.SIDEWAYS
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Arbitrary strategy-specific data
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def is_entry(self) -> bool:
        return self.signal_type in (SignalType.BUY, SignalType.SELL)

    @property
    def is_pre_signal(self) -> bool:
        return self.signal_type in (SignalType.PRE_BUY, SignalType.PRE_SELL)

    @property
    def is_long(self) -> bool:
        return self.side == OrderSide.LONG

    @property
    def risk_amount(self) -> float:
        """Absolute distance from entry to stop loss."""
        return abs(self.entry_price - self.stop_loss)

    def meets_grade(self, minimum: str) -> bool:
        """Return True if signal grade is at least *minimum* (e.g. 'B')."""
        min_grade = TradeGrade.from_str(minimum)
        return self.grade.meets_minimum(min_grade)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "signal_type": self.signal_type.value,
            "side": self.side.value,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profits": self.take_profits,
            "invalidation_level": self.invalidation_level,
            "confidence": self.confidence,
            "grade": self.grade.value,
            "risk_reward": round(self.risk_reward, 2),
            "reason": self.reason,
            "regime": self.regime.value,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:
        return (
            f"Signal({self.signal_type.value} {self.side.value} {self.symbol} "
            f"@ {self.entry_price:.2f} | SL {self.stop_loss:.2f} | "
            f"RR {self.risk_reward:.1f} | {self.grade.value} [{self.confidence}])"
        )


# ---------------------------------------------------------------------------
# Strategy engine interface
# ---------------------------------------------------------------------------

class StrategyEngine(ABC):
    """Abstract base that every concrete strategy must subclass.

    A strategy receives multi-timeframe OHLCV data, computes indicators,
    detects regimes, and emits a list of Signal objects.
    """

    name: str = "BaseStrategy"

    @abstractmethod
    def analyze(
        self,
        symbol: str,
        candles_dict: Dict[str, pd.DataFrame],
    ) -> List[Signal]:
        """Run the full analysis pipeline.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. ``"BTC/USDT"``.
        candles_dict : dict[str, DataFrame]
            Mapping of timeframe label (e.g. ``"5m"``, ``"15m"``) to a
            DataFrame with columns ``open, high, low, close, volume``
            indexed by datetime.

        Returns
        -------
        list[Signal]
            Zero or more signals, already scored and graded.
        """
        ...

    @abstractmethod
    def get_required_timeframes(self) -> List[str]:
        """Return the list of timeframe strings the strategy needs.

        The data layer will ensure these are fetched before calling
        :meth:`analyze`.
        """
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"


# Alias so callers can use either name
BaseStrategy = StrategyEngine
