"""
Regime Filter & Dynamic Position Sizing

Maps market regime + signal properties to actionable decisions:
- Should we trade in this regime?
- What position size multiplier to use?
- Should we tighten/widen stops?
- Which scanners are allowed per regime?
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RegimeAction:
    """Action to take based on current regime."""
    allow_trade: bool = True
    size_multiplier: float = 1.0      # 0.0-1.5 position size adjustment
    sl_multiplier: float = 1.0        # SL distance adjustment (>1 = wider)
    min_confidence: int = 65          # Minimum confidence to accept
    reason: str = ""
    ev_threshold_adj: float = 0.0     # EV threshold adjustment for this regime


# ──────────────────────────────────────────────────────────────────────
# Per-Regime Scanner Permission Table
# ──────────────────────────────────────────────────────────────────────
# Each regime maps to: allowed scanners, blocked scanners, and overrides.
# If a scanner is not in the allowed set, it's blocked for that regime.
# "*" = all scanners allowed (no restriction).
#
# This is the core quant control: different regimes → different strategies.
# ──────────────────────────────────────────────────────────────────────

REGIME_SCANNER_CONFIG: Dict[str, Dict[str, Any]] = {
    "trending_up": {
        "allowed": "*",                    # All scanners can fire in trends
        "preferred": [                     # These get a confidence boost
            "ema_momentum", "momentum_ride", "post_impulse",
        ],
        "blocked": [],                     # None blocked in trend
        "confidence_boost": 5,             # Preferred scanners get +5 conf
        "size_mult": 1.0,
        "sl_mult": 1.0,
        "ev_threshold_adj": -0.05,         # Lower EV bar (trend adds edge)
    },
    "trending_down": {
        "allowed": "*",
        "preferred": [
            "ema_momentum", "momentum_ride", "post_impulse",
        ],
        "blocked": [],
        "confidence_boost": 5,
        "size_mult": 1.0,
        "sl_mult": 1.0,
        "ev_threshold_adj": -0.05,
    },
    "breakout": {
        "allowed": "*",
        "preferred": [
            "bos_choch", "momentum_surge", "ema_momentum",
        ],
        "blocked": ["vwap_reclaim"],       # VWAP less reliable in breakouts
        "confidence_boost": 8,
        "size_mult": 1.1,
        "sl_mult": 1.1,                    # Slightly wider for breakout volatility
        "ev_threshold_adj": -0.03,
    },
    "ranging": {
        "allowed": [                       # Only mean-reversion scanners
            "vwap_reclaim", "rsi_divergence", "liquidity_sweep",
            "structure_bounce",
        ],
        "preferred": ["vwap_reclaim", "liquidity_sweep"],
        "blocked": [
            "ema_momentum", "momentum_ride", "momentum_surge",
            "post_impulse", "supertrend_flip",
        ],
        "confidence_boost": 3,
        "size_mult": 0.7,
        "sl_mult": 0.85,
        "ev_threshold_adj": 0.0,
    },
    "sideways": {
        "allowed": [
            "vwap_reclaim", "rsi_divergence", "liquidity_sweep",
            "structure_bounce",
        ],
        "preferred": ["vwap_reclaim", "liquidity_sweep"],
        "blocked": [
            "ema_momentum", "momentum_ride", "momentum_surge",
            "post_impulse", "supertrend_flip",
        ],
        "confidence_boost": 3,
        "size_mult": 0.7,
        "sl_mult": 0.85,
        "ev_threshold_adj": 0.0,
    },
    "volatile": {
        "allowed": [                       # Only high-conviction scanners
            "ema_momentum", "bos_choch",
        ],
        "preferred": [],
        "blocked": [
            "vwap_reclaim", "rsi_divergence", "supertrend_flip",
            "momentum_surge",
        ],
        "confidence_boost": 0,
        "size_mult": 0.5,
        "sl_mult": 1.3,
        "ev_threshold_adj": 0.05,          # Higher bar in volatile
    },
    "high_volatility": {
        "allowed": [
            "ema_momentum", "bos_choch",
        ],
        "preferred": [],
        "blocked": [
            "vwap_reclaim", "rsi_divergence", "supertrend_flip",
            "momentum_surge",
        ],
        "confidence_boost": 0,
        "size_mult": 0.5,
        "sl_mult": 1.3,
        "ev_threshold_adj": 0.05,
    },
    "mean_reversion": {
        "allowed": [
            "vwap_reclaim", "rsi_divergence", "liquidity_sweep",
            "structure_bounce",
        ],
        "preferred": ["rsi_divergence", "liquidity_sweep"],
        "blocked": [
            "ema_momentum", "momentum_ride", "post_impulse",
            "supertrend_flip",
        ],
        "confidence_boost": 5,
        "size_mult": 0.8,
        "sl_mult": 0.9,
        "ev_threshold_adj": 0.0,
    },
    "quiet": {
        "allowed": "*",
        "preferred": ["liquidity_sweep"],  # Sweep setups work well in quiet markets
        "blocked": [],
        "confidence_boost": 3,
        "size_mult": 0.8,
        "sl_mult": 1.0,
        "ev_threshold_adj": 0.0,
    },
    "low_liquidity": {
        "allowed": [],                     # No trading in thin markets
        "preferred": [],
        "blocked": "*",
        "confidence_boost": 0,
        "size_mult": 0.0,
        "sl_mult": 1.0,
        "ev_threshold_adj": 0.10,
    },
}


def is_scanner_allowed_in_regime(scanner_name: str, regime: str) -> bool:
    """Check if a scanner is allowed to trade in the current regime.

    Returns True if the scanner can generate signals in this regime.
    """
    config = REGIME_SCANNER_CONFIG.get(regime, {})
    allowed = config.get("allowed", "*")
    blocked = config.get("blocked", [])

    # Explicit block check
    if blocked == "*" or scanner_name in blocked:
        return False

    # Allowed check
    if allowed == "*":
        return True

    return scanner_name in allowed


def get_regime_scanner_boost(scanner_name: str, regime: str) -> int:
    """Get confidence boost for preferred scanners in this regime."""
    config = REGIME_SCANNER_CONFIG.get(regime, {})
    preferred = config.get("preferred", [])
    boost = config.get("confidence_boost", 0)
    return boost if scanner_name in preferred else 0


def get_regime_ev_adjustment(regime: str) -> float:
    """Get EV threshold adjustment for this regime."""
    config = REGIME_SCANNER_CONFIG.get(regime, {})
    return config.get("ev_threshold_adj", 0.0)


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
        """Map regime + signal + scanner to trading action.

        Uses REGIME_SCANNER_CONFIG table for per-regime scanner permissions.

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
        # ── Scanner-regime permission check (table-driven) ──
        if not is_scanner_allowed_in_regime(scanner_name, regime):
            return RegimeAction(
                allow_trade=False,
                reason=f"{scanner_name} not allowed in {regime} regime",
            )

        # Get regime config for size/SL multipliers
        regime_cfg = REGIME_SCANNER_CONFIG.get(regime, {})
        cfg_size = regime_cfg.get("size_mult", 1.0)
        cfg_sl = regime_cfg.get("sl_mult", 1.0)
        cfg_ev_adj = regime_cfg.get("ev_threshold_adj", 0.0)

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
                    reason=f"Against {regime} trend",
                )
            # With trend — use config multipliers, boost for strong tier
            size_mult = cfg_size * (1.2 if signal_tier == "strong" else 1.0)
            return RegimeAction(
                allow_trade=True,
                size_multiplier=size_mult,
                sl_multiplier=cfg_sl,
                min_confidence=60,
                ev_threshold_adj=cfg_ev_adj,
                reason=f"With {regime} trend",
            )

        elif regime in ("ranging", "sideways"):
            return RegimeAction(
                allow_trade=True,
                size_multiplier=cfg_size,
                sl_multiplier=cfg_sl,
                min_confidence=70,
                ev_threshold_adj=cfg_ev_adj,
                reason=f"{regime.title()} market — reduced size, tighter stops",
            )

        elif regime in ("volatile", "high_volatility"):
            if against_trend:
                return RegimeAction(
                    allow_trade=False,
                    reason=f"Against trend in {regime} market",
                )
            return RegimeAction(
                allow_trade=True,
                size_multiplier=cfg_size,
                sl_multiplier=cfg_sl,
                min_confidence=75,
                ev_threshold_adj=cfg_ev_adj,
                reason=f"{regime.title()} — reduced size, wider stops",
            )

        elif regime == "quiet":
            return RegimeAction(
                allow_trade=True,
                size_multiplier=cfg_size,
                sl_multiplier=cfg_sl,
                min_confidence=65,
                ev_threshold_adj=cfg_ev_adj,
                reason="Quiet market — normal rules",
            )

        elif regime == "low_liquidity":
            return RegimeAction(
                allow_trade=False,
                size_multiplier=0.0,
                ev_threshold_adj=0.10,
                reason="Low liquidity — no trading",
            )

        elif regime == "breakout":
            size_mult = cfg_size * (1.2 if signal_tier == "strong" else 1.0)
            return RegimeAction(
                allow_trade=True,
                size_multiplier=size_mult,
                sl_multiplier=cfg_sl,
                min_confidence=65,
                ev_threshold_adj=cfg_ev_adj,
                reason="Breakout — momentum favored",
            )

        elif regime == "mean_reversion":
            return RegimeAction(
                allow_trade=True,
                size_multiplier=cfg_size,
                sl_multiplier=cfg_sl,
                min_confidence=68,
                ev_threshold_adj=cfg_ev_adj,
                reason="Mean reversion — reversal setups only",
            )

        # Unknown regime
        return RegimeAction(
            allow_trade=True,
            size_multiplier=0.8,
            min_confidence=70,
            ev_threshold_adj=0.0,
            reason=f"Unknown regime: {regime}",
        )


def detect_regime_transition(current_regime: str, previous_regime: str, regime_age_bars: int) -> dict:
    """Detect if we're in a regime transition.

    Args:
        current_regime: The current detected regime
        previous_regime: The previous regime (from last cycle)
        regime_age_bars: How many bars the current regime has held

    Returns:
        dict with:
            - in_transition: bool
            - transition_type: str ("trend_to_range", "range_to_trend", etc.)
            - confidence_adj: int (-10 for unstable transitions, +5 for confirmed new regime)
    """
    TREND_REGIMES = {"trending_up", "trending_down", "breakout"}
    RANGE_REGIMES = {"ranging", "sideways", "mean_reversion", "quiet"}
    VOL_REGIMES = {"volatile", "high_volatility"}

    changed = current_regime != previous_regime and previous_regime != ""

    # Determine transition type
    transition_type = "none"
    if changed:
        prev_group = (
            "trend" if previous_regime in TREND_REGIMES else
            "range" if previous_regime in RANGE_REGIMES else
            "vol" if previous_regime in VOL_REGIMES else "other"
        )
        curr_group = (
            "trend" if current_regime in TREND_REGIMES else
            "range" if current_regime in RANGE_REGIMES else
            "vol" if current_regime in VOL_REGIMES else "other"
        )
        transition_type = f"{prev_group}_to_{curr_group}"

    # Confidence adjustment based on regime age
    if regime_age_bars <= 3:
        # Just changed — unstable, penalize
        return {
            "in_transition": True,
            "transition_type": transition_type,
            "confidence_adj": -10,
        }
    elif regime_age_bars >= 30:
        # Strong hold — high confidence in regime
        return {
            "in_transition": False,
            "transition_type": "none",
            "confidence_adj": +5,
        }
    elif regime_age_bars >= 10:
        # Confirmed regime
        return {
            "in_transition": False,
            "transition_type": "none",
            "confidence_adj": +3,
        }
    else:
        # 4-9 bars — still settling
        return {
            "in_transition": False,
            "transition_type": transition_type if changed else "none",
            "confidence_adj": 0,
        }


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
