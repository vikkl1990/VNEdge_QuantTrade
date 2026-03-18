"""
Structure Level Detection — identifies S/R, order blocks, liquidity zones, VWAP bands.

These are the price levels where institutional players operate and where
real supply/demand imbalances exist. Entries at structure levels have
genuine predictive edge vs indicator-based entries which react AFTER moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from data.indicators import calc_vwap


# ──────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────

@dataclass
class StructureLevel:
    price: float                # center of the zone
    level_type: str             # "sr", "order_block", "liquidity", "vwap_band"
    side: str                   # "support" or "resistance"
    strength: int               # 0-100
    zone_high: float            # upper edge of zone
    zone_low: float             # lower edge of zone
    touch_count: int = 0
    last_touch_bars_ago: int = 999
    extra: dict = field(default_factory=dict)

    @property
    def zone_width(self) -> float:
        return self.zone_high - self.zone_low


@dataclass
class StructureMap:
    levels: List[StructureLevel]
    nearest_support: Optional[StructureLevel] = None
    nearest_resistance: Optional[StructureLevel] = None
    vwap: float = 0.0
    vwap_upper_1: float = 0.0
    vwap_lower_1: float = 0.0
    vwap_upper_2: float = 0.0
    vwap_lower_2: float = 0.0


# ──────────────────────────────────────────────────────────────
# Swing detection (3-bar pivot)
# ──────────────────────────────────────────────────────────────

def find_swings(
    df: pd.DataFrame,
    lookback: int = 100,
) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Find swing highs and swing lows using 3-bar pivot."""
    n = min(lookback, len(df) - 2)
    if n < 5:
        return [], []

    highs = df["high"].values
    lows = df["low"].values
    start = len(df) - n

    swing_highs = []
    swing_lows = []

    for i in range(start + 1, len(df) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append((i, float(highs[i])))
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swing_lows.append((i, float(lows[i])))

    return swing_highs, swing_lows


# ──────────────────────────────────────────────────────────────
# 1. Horizontal Support/Resistance
# ──────────────────────────────────────────────────────────────

def find_horizontal_sr(
    df: pd.DataFrame,
    lookback: int = 100,
    min_touches: int = 2,
    zone_atr_frac: float = 0.3,
) -> List[StructureLevel]:
    """Find horizontal S/R levels where price has bounced 2+ times."""
    if len(df) < 20:
        return []

    atr_vals = df.get("atr")
    if atr_vals is None or len(atr_vals) < 1:
        return []
    atr = float(atr_vals.iloc[-1])
    if atr <= 0 or np.isnan(atr):
        return []

    zone_width = atr * zone_atr_frac
    current_price = float(df["close"].iloc[-1])
    total_bars = len(df)

    swing_highs, swing_lows = find_swings(df, lookback)
    all_pivots = [(idx, price, "high") for idx, price in swing_highs] + \
                 [(idx, price, "low") for idx, price in swing_lows]

    if not all_pivots:
        return []

    # Group pivots into zones
    all_pivots.sort(key=lambda x: x[1])
    zones: List[dict] = []

    for idx, price, ptype in all_pivots:
        merged = False
        for zone in zones:
            if abs(price - zone["center"]) <= zone_width:
                zone["touches"].append((idx, price, ptype))
                zone["center"] = np.mean([t[1] for t in zone["touches"]])
                merged = True
                break
        if not merged:
            zones.append({
                "center": price,
                "touches": [(idx, price, ptype)],
            })

    levels = []
    for zone in zones:
        if len(zone["touches"]) < min_touches:
            continue

        prices_in_zone = [t[1] for t in zone["touches"]]
        center = np.mean(prices_in_zone)
        z_high = max(prices_in_zone) + zone_width * 0.2
        z_low = min(prices_in_zone) - zone_width * 0.2

        # Recency bonus
        most_recent_bar = max(t[0] for t in zone["touches"])
        bars_ago = total_bars - 1 - most_recent_bar
        recency_bonus = 20 if bars_ago < 15 else (10 if bars_ago < 40 else 0)

        strength = min(len(zone["touches"]) * 20 + recency_bonus, 100)

        side = "support" if center < current_price else "resistance"

        levels.append(StructureLevel(
            price=round(center, 2),
            level_type="sr",
            side=side,
            strength=strength,
            zone_high=round(z_high, 2),
            zone_low=round(z_low, 2),
            touch_count=len(zone["touches"]),
            last_touch_bars_ago=bars_ago,
        ))

    return levels


# ──────────────────────────────────────────────────────────────
# 2. Order Blocks (ICT concept)
# ──────────────────────────────────────────────────────────────

def detect_order_blocks(
    df: pd.DataFrame,
    lookback: int = 50,
    min_impulse_atr: float = 1.5,
) -> List[StructureLevel]:
    """Find the last opposing candle before a strong impulsive move."""
    if len(df) < 10:
        return []

    atr_vals = df.get("atr")
    if atr_vals is None:
        return []
    current_price = float(df["close"].iloc[-1])
    total_bars = len(df)

    levels = []
    n = min(lookback, len(df) - 3)
    start = len(df) - n

    opens = df["open"].values
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    atrs = atr_vals.values

    for i in range(start, len(df) - 2):
        atr = atrs[i]
        if atr <= 0 or np.isnan(atr):
            continue

        body_next = abs(closes[i + 1] - opens[i + 1])
        body_curr = abs(closes[i] - opens[i])

        # Bullish OB: current candle is bearish, next is strong bullish impulse
        is_bearish = closes[i] < opens[i]
        is_bullish_impulse = closes[i + 1] > opens[i + 1] and body_next > atr * min_impulse_atr

        if is_bearish and is_bullish_impulse:
            # Check OB hasn't been revisited (unmitigated)
            ob_low = lows[i]
            ob_high = closes[i]  # bearish candle: close is bottom of body
            ob_high = max(ob_high, opens[i])

            mitigated = False
            for j in range(i + 2, len(df)):
                if lows[j] <= ob_high:
                    mitigated = True
                    break

            if not mitigated:
                bars_ago = total_bars - 1 - i
                impulse_strength = min(int(body_next / atr * 30), 80)
                levels.append(StructureLevel(
                    price=round((ob_low + ob_high) / 2, 2),
                    level_type="order_block",
                    side="support",
                    strength=impulse_strength,
                    zone_high=round(ob_high, 2),
                    zone_low=round(ob_low, 2),
                    last_touch_bars_ago=bars_ago,
                    extra={"ob_type": "bullish", "impulse_size": round(body_next / atr, 2)},
                ))

        # Bearish OB: current candle is bullish, next is strong bearish impulse
        is_bullish = closes[i] > opens[i]
        is_bearish_impulse = closes[i + 1] < opens[i + 1] and body_next > atr * min_impulse_atr

        if is_bullish and is_bearish_impulse:
            ob_high = highs[i]
            ob_low = min(opens[i], closes[i])

            mitigated = False
            for j in range(i + 2, len(df)):
                if highs[j] >= ob_low:
                    mitigated = True
                    break

            if not mitigated:
                bars_ago = total_bars - 1 - i
                impulse_strength = min(int(body_next / atr * 30), 80)
                levels.append(StructureLevel(
                    price=round((ob_low + ob_high) / 2, 2),
                    level_type="order_block",
                    side="resistance",
                    strength=impulse_strength,
                    zone_high=round(ob_high, 2),
                    zone_low=round(ob_low, 2),
                    last_touch_bars_ago=bars_ago,
                    extra={"ob_type": "bearish", "impulse_size": round(body_next / atr, 2)},
                ))

    return levels


# ──────────────────────────────────────────────────────────────
# 3. Liquidity Zones (stop-loss clusters)
# ──────────────────────────────────────────────────────────────

def find_liquidity_zones(
    df: pd.DataFrame,
    lookback: int = 100,
    equal_threshold_pct: float = 0.15,
) -> List[StructureLevel]:
    """Find clusters of equal lows/highs (liquidity pools)."""
    swing_highs, swing_lows = find_swings(df, lookback)
    current_price = float(df["close"].iloc[-1])
    total_bars = len(df)
    levels = []

    # Equal lows (buy-side liquidity below)
    if len(swing_lows) >= 2:
        for i in range(len(swing_lows)):
            for j in range(i + 1, len(swing_lows)):
                idx_i, price_i = swing_lows[i]
                idx_j, price_j = swing_lows[j]
                pct_diff = abs(price_i - price_j) / price_i * 100
                if pct_diff < equal_threshold_pct:
                    level_price = min(price_i, price_j)
                    bars_ago = total_bars - 1 - max(idx_i, idx_j)
                    strength = 70 if pct_diff < 0.05 else 50
                    if bars_ago < 20:
                        strength += 15

                    # Only add if price hasn't swept it yet
                    if level_price < current_price:
                        levels.append(StructureLevel(
                            price=round(level_price, 2),
                            level_type="liquidity",
                            side="support",
                            strength=strength,
                            zone_high=round(max(price_i, price_j), 2),
                            zone_low=round(level_price - current_price * 0.001, 2),
                            touch_count=2,
                            last_touch_bars_ago=bars_ago,
                            extra={"liq_type": "equal_lows"},
                        ))

    # Equal highs (sell-side liquidity above)
    if len(swing_highs) >= 2:
        for i in range(len(swing_highs)):
            for j in range(i + 1, len(swing_highs)):
                idx_i, price_i = swing_highs[i]
                idx_j, price_j = swing_highs[j]
                pct_diff = abs(price_i - price_j) / price_i * 100
                if pct_diff < equal_threshold_pct:
                    level_price = max(price_i, price_j)
                    bars_ago = total_bars - 1 - max(idx_i, idx_j)
                    strength = 70 if pct_diff < 0.05 else 50
                    if bars_ago < 20:
                        strength += 15

                    if level_price > current_price:
                        levels.append(StructureLevel(
                            price=round(level_price, 2),
                            level_type="liquidity",
                            side="resistance",
                            strength=strength,
                            zone_high=round(level_price + current_price * 0.001, 2),
                            zone_low=round(min(price_i, price_j), 2),
                            touch_count=2,
                            last_touch_bars_ago=bars_ago,
                            extra={"liq_type": "equal_highs"},
                        ))

    return levels


# ──────────────────────────────────────────────────────────────
# 4. VWAP Bands
# ──────────────────────────────────────────────────────────────

def calc_vwap_bands(df: pd.DataFrame) -> Tuple[float, float, float, float, float]:
    """Compute VWAP with ±1/2 std dev bands."""
    if len(df) < 20:
        return 0, 0, 0, 0, 0

    try:
        vwap_series = calc_vwap(df)
        vwap = float(vwap_series.iloc[-1])
    except Exception:
        return 0, 0, 0, 0, 0

    if vwap <= 0 or np.isnan(vwap):
        return 0, 0, 0, 0, 0

    # Compute std dev of (close - vwap)
    closes = df["close"].values[-50:]
    vwap_vals = vwap_series.values[-50:]
    valid = ~np.isnan(vwap_vals)
    if valid.sum() < 10:
        return vwap, vwap, vwap, vwap, vwap

    deviations = closes[valid] - vwap_vals[valid]
    std = float(np.std(deviations))

    return (
        vwap,
        round(vwap + std, 2),       # upper 1 std
        round(vwap - std, 2),       # lower 1 std
        round(vwap + std * 2, 2),   # upper 2 std
        round(vwap - std * 2, 2),   # lower 2 std
    )


# ──────────────────────────────────────────────────────────────
# Master: Build Structure Map
# ──────────────────────────────────────────────────────────────

def build_structure_map(
    df: pd.DataFrame,
    current_price: float,
    atr: float,
) -> StructureMap:
    """Build a complete structure map from all detection methods."""
    all_levels: List[StructureLevel] = []

    # 1. Horizontal S/R
    sr_levels = find_horizontal_sr(df, lookback=100, min_touches=2)
    all_levels.extend(sr_levels)

    # 2. Order blocks
    ob_levels = detect_order_blocks(df, lookback=50)
    all_levels.extend(ob_levels)

    # 3. Liquidity zones
    liq_levels = find_liquidity_zones(df, lookback=100)
    all_levels.extend(liq_levels)

    # 4. VWAP bands
    vwap, vwap_u1, vwap_l1, vwap_u2, vwap_l2 = calc_vwap_bands(df)

    if vwap > 0:
        # Add VWAP bands as structure levels
        band_width = abs(vwap_u1 - vwap) * 0.1
        if vwap_l1 < current_price:
            all_levels.append(StructureLevel(
                price=round(vwap_l1, 2), level_type="vwap_band", side="support",
                strength=60, zone_high=round(vwap_l1 + band_width, 2),
                zone_low=round(vwap_l1 - band_width, 2),
            ))
        if vwap_u1 > current_price:
            all_levels.append(StructureLevel(
                price=round(vwap_u1, 2), level_type="vwap_band", side="resistance",
                strength=60, zone_high=round(vwap_u1 + band_width, 2),
                zone_low=round(vwap_u1 - band_width, 2),
            ))

    # Sort by distance from current price
    for level in all_levels:
        level.extra["distance_pct"] = round(abs(level.price - current_price) / current_price * 100, 3)

    all_levels.sort(key=lambda l: abs(l.price - current_price))

    # Find nearest support and resistance
    supports = [l for l in all_levels if l.side == "support" and l.price < current_price]
    resistances = [l for l in all_levels if l.side == "resistance" and l.price > current_price]

    nearest_sup = supports[0] if supports else None
    nearest_res = resistances[0] if resistances else None

    return StructureMap(
        levels=all_levels,
        nearest_support=nearest_sup,
        nearest_resistance=nearest_res,
        vwap=vwap,
        vwap_upper_1=vwap_u1,
        vwap_lower_1=vwap_l1,
        vwap_upper_2=vwap_u2,
        vwap_lower_2=vwap_l2,
    )
