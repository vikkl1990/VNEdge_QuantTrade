"""
Regime Filter & Dynamic Position Sizing

Maps market regime + signal properties to actionable decisions:
- Should we trade in this regime?
- What position size multiplier to use?
- Should we tighten/widen stops?
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RegimeAction:
    """Action to take based on current regime."""
    allow_trade: bool = True
    size_multiplier: float = 1.0      # 0.0-1.5 position size adjustment
    sl_multiplier: float = 1.0        # SL distance adjustment (>1 = wider)
    min_confidence: int = 65          # Minimum confidence to accept
    reason: str = ""


class RegimeFilter:
    """Determines trading actions based on market regime detection.

    Regime detection is done by analyzing:
    1. EMA alignment (8/21/50) — trend direction and strength
    2. ATR percentile — volatility state
    3. BB bandwidth — squeeze vs expansion
    4. ADX if available — trend strength
    """

    # ATR percentile thresholds (relative to 50-bar lookback)
    HIGH_VOL_PERCENTILE = 75   # above this = volatile
    LOW_VOL_PERCENTILE = 25    # below this = quiet

    def detect_regime(self, indicators: Dict[str, Any]) -> str:
        """Detect current market regime from indicator values.

        Returns: "trending_up", "trending_down", "ranging", "volatile", "quiet"
        """
        ema8 = indicators.get("ema_8", 0)
        ema21 = indicators.get("ema_21", 0)
        ema50 = indicators.get("ema_50", 0)
        close = indicators.get("close", 0)
        atr = indicators.get("atr", 0)
        bb_bandwidth = indicators.get("bb_bandwidth", 0)

        # Full stack alignment = strong trend
        full_bull = ema8 > ema21 > ema50 and close > ema8
        full_bear = ema8 < ema21 < ema50 and close < ema8

        # Check volatility via BB bandwidth
        # High bandwidth (>0.04) = volatile/trending, Low (<0.015) = quiet/squeeze
        is_volatile = bb_bandwidth > 0.04
        is_quiet = bb_bandwidth < 0.015

        if full_bull and not is_volatile:
            return "trending_up"
        elif full_bear and not is_volatile:
            return "trending_down"
        elif is_volatile:
            return "volatile"
        elif is_quiet:
            return "quiet"
        else:
            return "ranging"

    def get_action(self, regime: str, signal_side: str, signal_tier: str,
                   scanner_name: str) -> RegimeAction:
        """Map regime + signal properties to trading action.

        Regime-Action Matrix:
        ┌────────────────┬──────────┬──────────┬───────────┬──────────┐
        │ Regime         │ With     │ Against  │ Size      │ SL       │
        ├────────────────┼──────────┼──────────┼───────────┼──────────┤
        │ Trending Up    │ FULL     │ SKIP     │ 1.0-1.2x  │ normal   │
        │ Trending Down  │ FULL     │ SKIP     │ 1.0-1.2x  │ normal   │
        │ Ranging        │ REDUCED  │ REDUCED  │ 0.7x      │ tighter  │
        │ Volatile       │ REDUCED  │ SKIP     │ 0.5x      │ wider    │
        │ Quiet          │ FULL     │ FULL     │ 0.8x      │ normal   │
        └────────────────┴──────────┴──────────┴───────────┴──────────┘
        """
        # Determine if signal aligns with regime
        with_trend = (
            (regime == "trending_up" and signal_side == "long") or
            (regime == "trending_down" and signal_side == "short")
        )
        against_trend = (
            (regime == "trending_up" and signal_side == "short") or
            (regime == "trending_down" and signal_side == "long")
        )

        if regime in ("trending_up", "trending_down"):
            if against_trend:
                return RegimeAction(
                    allow_trade=False,
                    reason=f"Against {regime} trend"
                )
            # With trend or neutral
            return RegimeAction(
                allow_trade=True,
                size_multiplier=1.2 if signal_tier == "strong" else 1.0,
                min_confidence=60,
                reason=f"With {regime} trend"
            )

        elif regime == "ranging":
            return RegimeAction(
                allow_trade=True,
                size_multiplier=0.7,
                sl_multiplier=0.85,  # tighter stops in ranges
                min_confidence=70,
                reason="Ranging market — reduced size, tighter stops"
            )

        elif regime == "volatile":
            if against_trend:
                return RegimeAction(
                    allow_trade=False,
                    reason="Against trend in volatile market"
                )
            return RegimeAction(
                allow_trade=True,
                size_multiplier=0.5,
                sl_multiplier=1.3,   # wider stops for volatility
                min_confidence=75,
                reason="Volatile — half size, wider stops"
            )

        elif regime == "quiet":
            return RegimeAction(
                allow_trade=True,
                size_multiplier=0.8,
                min_confidence=65,
                reason="Quiet market — normal rules"
            )

        # Unknown regime
        return RegimeAction(
            allow_trade=True,
            size_multiplier=0.8,
            min_confidence=70,
            reason=f"Unknown regime: {regime}"
        )


def calc_confidence_size_multiplier(confidence: int, tier: str) -> float:
    """Scale position size based on signal confidence and tier.

    Strong signals (80+) get full or boosted size.
    Valid signals (65-79) get normal size.
    Weak signals (50-64) get reduced size.
    """
    if tier == "strong":
        if confidence >= 90:
            return 1.3  # exceptional signal
        return 1.1
    elif tier == "valid":
        return 1.0
    elif tier == "weak":
        return 0.6
    return 0.5  # near_miss or rejected shouldn't trade but just in case
