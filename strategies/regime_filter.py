"""
Regime Filter & Confidence Boost Table

Maps market regime + scanner to a confidence boost for "preferred" scanners.

(2026-09-15) This module used to also carry a per-regime scanner
allow/block permission table (`REGIME_SCANNER_CONFIG["allowed"/"blocked"]`,
`is_scanner_allowed_in_regime()`) and a size/SL/EV-adjustment `RegimeAction`
matrix (`RegimeFilter.get_action()`). Neither was ever called from
strategies/scalp_strategy.py — the actual live gate has always been
`REGIME_SCANNER_ROUTING` (built per-analyze() call in scalp_strategy.py),
and the two tables disagreed (e.g. this file listed `rsi_divergence` as
"blocked" in volatile/high_volatility while the live router ran it there
anyway). Removed rather than kept in sync, since a permission table that
isn't the enforcement point is worse than no table — it looks authoritative
without being true. `get_regime_scanner_boost()` below is the one surviving,
actually-used piece, and now takes the live router's own allowed-scanner
list as an argument so a boost can never apply to a scanner the router
wouldn't run anyway.
"""

from __future__ import annotations
import logging
from typing import Dict, Any, List, Optional, Set, Tuple, Iterable

logger = logging.getLogger(__name__)

# Advanced regime detector instance (P0 upgrade)
_advanced_detector = None
def _get_detector():
    global _advanced_detector
    if _advanced_detector is None:
        from strategies.regime import MarketRegimeDetector
        _advanced_detector = MarketRegimeDetector()
    return _advanced_detector


# ──────────────────────────────────────────────────────────────────────
# Per-Regime Preferred-Scanner Confidence Boost
# ──────────────────────────────────────────────────────────────────────
# Scanners in a regime's "preferred" list get `confidence_boost` added when
# they fire in that regime. This is a scoring nudge only — it never decides
# whether a scanner is allowed to run at all; that's REGIME_SCANNER_ROUTING
# in scalp_strategy.py. get_regime_scanner_boost() cross-checks against
# that live list before applying anything (see below).
# ──────────────────────────────────────────────────────────────────────

REGIME_SCANNER_CONFIG: Dict[str, Dict[str, Any]] = {
    "trending_up": {
        # (2026-09-15) momentum_ride dropped from "preferred" here — it's
        # not in REGIME_SCANNER_ROUTING for any regime (dead scanner), so a
        # boost naming it was always inert. See module docstring.
        "preferred": ["ema_momentum", "post_impulse"],
        "confidence_boost": 5,             # Preferred scanners get +5 conf
    },
    "trending_down": {
        "preferred": ["ema_momentum"],
        "confidence_boost": 5,
    },
    "breakout": {
        # momentum_surge dropped — dead scanner, not routed anywhere live.
        "preferred": ["bos_choch", "ema_momentum"],
        "confidence_boost": 8,
    },
    "ranging": {
        "preferred": ["liquidity_sweep", "cvd_divergence", "rsi_extreme"],
        "confidence_boost": 3,
    },
    "sideways": {
        "preferred": ["liquidity_sweep", "cvd_divergence", "rsi_extreme"],
        "confidence_boost": 3,
    },
    "volatile": {
        "preferred": [],
        "confidence_boost": 0,
    },
    "high_volatility": {
        "preferred": [],
        "confidence_boost": 0,
    },
    "mean_reversion": {
        "preferred": ["rsi_divergence", "liquidity_sweep"],
        "confidence_boost": 5,
    },
    "quiet": {
        "preferred": ["liquidity_sweep"],
        "confidence_boost": 3,
    },
    "low_liquidity": {
        "preferred": [],
        "confidence_boost": 0,
    },
}


def get_regime_scanner_boost(
    scanner_name: str,
    regime: str,
    routed_scanners: Optional[Iterable[str]] = None,
) -> int:
    """Get confidence boost for preferred scanners in this regime.

    `routed_scanners` should be the live REGIME_SCANNER_ROUTING list for
    this regime (scalp_strategy.py passes its own `_regime_routing_names`).
    When given, a scanner that isn't actually routed for this regime never
    gets a boost, even if it's still listed in `preferred` above — this is
    what keeps this table from drifting out of sync with the real gate
    the way the old allow/block table did.
    """
    if routed_scanners is not None and scanner_name not in routed_scanners:
        return 0
    config = REGIME_SCANNER_CONFIG.get(regime, {})
    preferred = config.get("preferred", [])
    boost = config.get("confidence_boost", 0)
    return boost if scanner_name in preferred else 0


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

    def detect_regime(self, indicators: Dict[str, Any], df=None) -> str:
        """Detect current market regime from indicator values.

        P0 upgrade: Uses MarketRegimeDetector (ADX, ATR percentile, volume,
        BB squeeze) when a DataFrame is provided, falling back to simple
        EMA+BB detection otherwise.

        Returns: "trending_up", "trending_down", "ranging", "volatile",
                 "quiet", "breakout", "mean_reversion", "low_liquidity",
                 "high_volatility", "sideways"
        """
        # --- P0: Try advanced detector if df available ---
        if df is not None and len(df) >= 100:
            try:
                detector = _get_detector()
                ctx = detector.detect_regime(df)
                regime_str = ctx.regime.value  # MarketRegime enum -> string
                logger.debug("Advanced regime: %s (conf=%.2f, adx=%.1f, atr_pct=%.0f, vol=%.2f)",
                            regime_str, ctx.confidence, ctx.adx, ctx.atr_percentile, ctx.volume_ratio)
                return regime_str
            except Exception as e:
                logger.debug("Advanced regime detection failed, falling back: %s", e)

        # --- Fallback: simple EMA + BB detection ---
        import math
        ema8 = indicators.get("ema_8", 0)
        ema21 = indicators.get("ema_21", 0)
        ema50 = indicators.get("ema_50", 0)
        close = indicators.get("close", 0)
        atr = indicators.get("atr", 0)
        bb_bandwidth = indicators.get("bb_bandwidth", 0)

        # Guard against NaN/None — return "ranging" (safer than "quiet" which
        # blocks ALL scanners; ranging still allows mean-reversion setups)
        critical_vals = [ema8, ema21, ema50, close, bb_bandwidth]
        if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in critical_vals):
            return "ranging"

        # Full stack alignment = strong trend
        full_bull = ema8 > ema21 > ema50 and close > ema8
        full_bear = ema8 < ema21 < ema50 and close < ema8

        # Partial trend: EMAs ordered but close hasn't fully committed
        # This catches smooth drifts where close is near ema8
        partial_bull = ema8 > ema21 > ema50
        partial_bear = ema8 < ema21 < ema50

        # Price displacement from EMA50 (catches directional drift even
        # when Bollinger Bands are tight). A 0.3% displacement from the
        # slow EMA means the market IS moving, not quiet.
        ema50_displacement = abs(close - ema50) / ema50 if ema50 > 0 else 0
        has_directional_drift = ema50_displacement > 0.003  # 0.3%

        # Check volatility via BB bandwidth
        # High bandwidth (>0.04) = volatile/trending
        # Lowered quiet threshold: 0.015 → 0.008 (only truly dead markets)
        is_volatile = bb_bandwidth > 0.04
        is_quiet = bb_bandwidth < 0.008

        # 1) Full EMA stack alignment = clear trend
        if full_bull and not is_volatile:
            return "trending_up"
        elif full_bear and not is_volatile:
            return "trending_down"

        # 2) Partial EMA alignment + directional drift = trend (even if tight BB)
        #    This is the key fix: smooth 1-2% drifts have tight bands but are
        #    clearly trending. EMAs ordered + displacement = NOT quiet.
        if partial_bull and has_directional_drift:
            return "trending_up"
        elif partial_bear and has_directional_drift:
            return "trending_down"

        # 3) High volatility
        # (2026-09-15) Returns "high_volatility", not "volatile" — the two
        # regime-routing tables (REGIME_SCANNER_ROUTING in scalp_strategy.py,
        # REGIME_SCANNER_CONFIG above) carry a separate "volatile" key, but
        # MarketRegime (config/constants.py) — the enum the PRIMARY detector
        # below always returns from — has no such value and never will
        # without a wider enum change. This fallback only runs when that
        # primary detector throws, so its own output needs to land in the
        # same 7-value vocabulary or "volatile"/"quiet"/"ranging" candidates
        # get routed against dead table keys during exactly the fallback
        # window they're meant to keep the bot trading through. Confirmed
        # non-regressive: "volatile" and "high_volatility" are identical
        # scanner lists in REGIME_SCANNER_ROUTING today.
        if is_volatile:
            return "high_volatility"

        # 4) Quiet: only if BB bandwidth is very tight AND no directional drift
        #    AND no EMA ordering. This is a truly dead, flat market.
        # Mapped to "sideways" for the same reason as above — MarketRegime
        # has no QUIET value. "sideways" is a strict superset of "quiet"'s
        # scanner list (10 scanners vs. 4), so this widens access here
        # rather than narrowing it.
        if is_quiet and not has_directional_drift and not partial_bull and not partial_bear:
            return "sideways"

        # 5) Everything else = ranging (has some movement, just no clear trend)
        # Mapped to "sideways" (see above) — identical scanner list today,
        # and "sideways" is the string MarketRegime actually has.
        return "sideways"


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
