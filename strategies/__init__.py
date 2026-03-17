"""Strategy engine package."""

from strategies.base import BaseStrategy, Signal, StrategyEngine
from strategies.momentum_trend import MomentumTrendStrategy
from strategies.scalp_strategy import ScalpStrategy
from strategies.regime import MarketRegimeDetector, RegimeContext
from strategies.scoring import ScoreBreakdown, SignalScorer

__all__ = [
    "BaseStrategy",
    "Signal",
    "StrategyEngine",
    "MomentumTrendStrategy",
    "ScalpStrategy",
    "MarketRegimeDetector",
    "RegimeContext",
    "ScoreBreakdown",
    "SignalScorer",
]
