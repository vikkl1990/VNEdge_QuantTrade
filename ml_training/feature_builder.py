"""
Feature Builder — Pure Candle Math
====================================
Candles contain 3 types of information:
1. Price movement — where price moved
2. Volatility — how aggressively it moved
3. Participation (volume) — how much conviction was behind it

Everything else (RSI, MACD, patterns) is just a transformation of these.
ML learns RELATIONSHIPS between features, not indicators.

Feature categories:
1. Momentum (returns, NOT raw price)
2. Volatility (ATR ratio, range vs ATR)
3. Candle structure (body/wick ratios — rejection, absorption, indecision)
4. Trend strength (EMA gap normalized, slope)
5. Volume intelligence (z-score, NOT raw volume)
6. Market context (VWAP deviation, distance from extremes)
7. Compression/expansion (squeeze detection from ranges)
8. Time features (session encoding)
9. Recent behavior memory (last N candles aggregates)
10. Trade-specific features (SL/TP distance — added at scoring time)
"""

import numpy as np
import pandas as pd
from typing import Optional


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute base indicators needed for features and backtesting scanners."""
    df = df.copy()
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float)

    # EMAs (needed for trend features and scanner compatibility)
    for period in [8, 13, 21, 50, 100, 200]:
        df[f"ema_{period}"] = c.ewm(span=period, adjust=False).mean()

    # ATR (core volatility measure)
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14).mean()
    df["atr_7"] = tr.rolling(7).mean()

    # RSI (kept for scanner compatibility, NOT used as raw feature)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # Bollinger Bands (for scanner compatibility)
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["bb_upper"] = sma20 + 2 * std20
    df["bb_lower"] = sma20 - 2 * std20
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / sma20.replace(0, np.nan)

    # VWAP
    cum_vol = v.cumsum()
    cum_vp = (c * v).cumsum()
    df["vwap"] = cum_vp / cum_vol.replace(0, np.nan)

    # Volume rolling stats
    df["vol_sma_20"] = v.rolling(20).mean()
    df["vol_std_20"] = v.rolling(20).std()
    df["rel_vol"] = v / df["vol_sma_20"].replace(0, np.nan)

    # MACD (for scanner compatibility)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Candle components
    df["body"] = (c - o).abs()
    df["range"] = (h - l).replace(0, np.nan)
    df["body_ratio"] = df["body"] / df["range"]
    df["upper_wick"] = h - pd.concat([c, o], axis=1).max(axis=1)
    df["lower_wick"] = pd.concat([c, o], axis=1).min(axis=1) - l
    df["is_bullish"] = (c > o).astype(int)

    return df


def build_features(df: pd.DataFrame, htf_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Build ML features from pure candle math.

    Returns ~35 features derived from price movement, volatility, and participation.
    No raw indicators — only relationships.
    """
    df = compute_indicators(df)
    features = pd.DataFrame(index=df.index)

    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float)
    atr = df["atr_14"]

    # ================================================================
    # 1. MOMENTUM (returns, not raw price)
    # ================================================================
    features["return_1"] = c.pct_change(1)
    features["return_3"] = c.pct_change(3)
    features["return_5"] = c.pct_change(5)
    features["return_10"] = c.pct_change(10)
    features["return_20"] = c.pct_change(20)
    # Momentum acceleration: short-term vs medium-term
    features["momentum_accel"] = features["return_1"] - features["return_5"]

    # ================================================================
    # 2. VOLATILITY (how aggressively price moved)
    # ================================================================
    # ATR as ratio of price (normalized)
    features["atr_ratio"] = atr / c.replace(0, np.nan)
    # Current candle range vs ATR (is this candle normal or extreme?)
    features["range_vs_atr"] = df["range"] / atr.replace(0, np.nan)
    # Short-term vs long-term ATR (volatility expanding or contracting?)
    features["atr_expansion"] = df["atr_7"] / atr.replace(0, np.nan)
    # Volatility regime (current ATR vs 100-bar average)
    features["vol_regime"] = atr / atr.rolling(100).mean().replace(0, np.nan)

    # ================================================================
    # 3. CANDLE STRUCTURE (rejection, absorption, indecision — pure math)
    # ================================================================
    features["body_ratio"] = df["body_ratio"]
    features["upper_wick_ratio"] = df["upper_wick"] / df["range"].replace(0, np.nan)
    features["lower_wick_ratio"] = df["lower_wick"] / df["range"].replace(0, np.nan)
    # Body displacement in ATR units (meaningful move?)
    features["body_displacement"] = df["body"] / atr.replace(0, np.nan)
    # Close position within range (0=at low, 1=at high)
    features["close_position"] = (c - l) / df["range"].replace(0, np.nan)

    # ================================================================
    # 4. TREND STRENGTH (EMA relationships, not raw EMA values)
    # ================================================================
    # EMA gap normalized by ATR (trend strength)
    features["trend_strength"] = (df["ema_8"] - df["ema_21"]) / atr.replace(0, np.nan)
    # Longer-term trend
    features["trend_strength_long"] = (df["ema_21"] - df["ema_50"]) / atr.replace(0, np.nan)
    # EMA slope (rate of change of the trend itself)
    features["ema_slope_8"] = df["ema_8"].pct_change(3)
    features["ema_slope_21"] = df["ema_21"].pct_change(5)
    # Price distance from EMA (how stretched are we?)
    features["dist_from_ema21"] = (c - df["ema_21"]) / atr.replace(0, np.nan)

    # ================================================================
    # 5. VOLUME INTELLIGENCE (z-score, not raw volume)
    # ================================================================
    # Volume z-score (how abnormal is current volume?)
    features["volume_zscore"] = (v - df["vol_sma_20"]) / df["vol_std_20"].replace(0, np.nan)
    # Volume spike ratio
    features["volume_spike"] = v / df["vol_sma_20"].replace(0, np.nan)

    # ================================================================
    # 6. MARKET CONTEXT (are we stretched? at extremes? in equilibrium?)
    # ================================================================
    # Distance from VWAP (normalized by ATR)
    features["dist_from_vwap"] = (c - df["vwap"]) / atr.replace(0, np.nan)
    # Distance from session high/low (20-bar rolling as proxy)
    rolling_high = h.rolling(20).max()
    rolling_low = l.rolling(20).min()
    rolling_range = (rolling_high - rolling_low).replace(0, np.nan)
    features["dist_from_high"] = (c - rolling_high) / atr.replace(0, np.nan)
    features["dist_from_low"] = (c - rolling_low) / atr.replace(0, np.nan)
    # Position within range (0=at bottom, 1=at top)
    features["range_position"] = (c - rolling_low) / rolling_range

    # ================================================================
    # 7. COMPRESSION / EXPANSION (squeeze detection from ranges)
    # ================================================================
    # Range last 5 vs last 20 (compression → breakout coming)
    range_5 = df["range"].rolling(5).mean()
    range_20 = df["range"].rolling(20).mean()
    features["vol_compression"] = range_5 / range_20.replace(0, np.nan)

    # ================================================================
    # 8. TIME FEATURES (cyclical encoding)
    # ================================================================
    if hasattr(df.index, 'hour'):
        hour = df.index.hour
        features["hour_sin"] = np.sin(2 * np.pi * hour / 24)
        features["hour_cos"] = np.cos(2 * np.pi * hour / 24)
        # Session encoding: Asia=0, EU=1, US=2
        features["session"] = np.where(hour < 8, 0, np.where(hour < 16, 1, 2))
        features["dow_sin"] = np.sin(2 * np.pi * df.index.dayofweek / 7)
    else:
        features["hour_sin"] = 0
        features["hour_cos"] = 0
        features["session"] = 1
        features["dow_sin"] = 0

    # ================================================================
    # 9. STATE TRANSITION / DELTA FEATURES (how things are CHANGING)
    # State change is often more predictive than state itself.
    # ================================================================
    # Trend change: is trend accelerating, decelerating, or reversing?
    features["trend_change"] = features["trend_strength"] - (
        (df["ema_8"].shift(5) - df["ema_21"].shift(5)) / atr.shift(5).replace(0, np.nan)
    )
    # Trend change (short-term, 3 bars)
    features["trend_change_3"] = features["trend_strength"] - (
        (df["ema_8"].shift(3) - df["ema_21"].shift(3)) / atr.shift(3).replace(0, np.nan)
    )
    # Long trend change (medium-term momentum shift)
    features["trend_long_change"] = features["trend_strength_long"] - (
        (df["ema_21"].shift(5) - df["ema_50"].shift(5)) / atr.shift(5).replace(0, np.nan)
    )
    # Volatility change: expanding or contracting?
    features["vol_change"] = atr / atr.shift(5).replace(0, np.nan) - 1
    # ATR ratio change (is volatility regime shifting?)
    atr_ratio_prev = atr.shift(3) / atr.shift(3).rolling(100).mean().replace(0, np.nan)
    features["atr_ratio_change"] = features["vol_regime"] - atr_ratio_prev
    # VWAP distance change (are we moving toward or away from fair value?)
    vwap_dist_prev = (c.shift(3) - df["vwap"].shift(3)) / atr.shift(3).replace(0, np.nan)
    features["vwap_dist_change"] = features["dist_from_vwap"] - vwap_dist_prev
    # VWAP reversion speed (same as above, kept for compat)
    features["vwap_reversion_speed"] = features["vwap_dist_change"]
    # Volume momentum: is volume increasing or fading?
    features["volume_change"] = v / v.rolling(5).mean().replace(0, np.nan) - 1
    # Impulse decay: how fast is a recent move fading?
    # Compare 1-bar return to 5-bar return — if 1-bar is small but 5-bar is big, impulse is decaying
    features["impulse_decay"] = features["return_1"].abs() / features["return_5"].abs().replace(0, np.nan)
    features["impulse_decay"] = features["impulse_decay"].clip(0, 5)
    # Range contraction/expansion rate (squeeze speed)
    features["range_change"] = (
        df["range"].rolling(3).mean() / df["range"].rolling(10).mean().replace(0, np.nan)
    )
    # EMA slope change (is the trend accelerating or decelerating?)
    features["ema_slope_change"] = features["ema_slope_8"] - df["ema_8"].pct_change(3).shift(3)

    # ================================================================
    # 9b. RECENT BEHAVIOR MEMORY (short-term patterns)
    # ================================================================
    features["last_3_return"] = c.pct_change(3)
    features["last_5_volatility"] = df["range"].rolling(5).mean() / atr.replace(0, np.nan)
    bullish_count = df["is_bullish"].rolling(5).sum()
    features["trend_persistence"] = (bullish_count - 2.5) / 2.5

    # ================================================================
    # 10. DERIVED MULTI-TIMEFRAME (rolling windows, no extra data needed)
    # ================================================================
    # 5-bar derived features (≈5m context on 1m, or 25m on 5m)
    features["return_5bar"] = c.pct_change(5)
    features["atr_5bar"] = df["range"].rolling(5).mean() / c.replace(0, np.nan)
    # 15-bar derived features (≈15m context on 1m, or 75m on 5m)
    features["return_15bar"] = c.pct_change(15)
    features["atr_15bar"] = df["range"].rolling(15).mean() / c.replace(0, np.nan)
    # Cross-timeframe trend agreement: short vs medium vs long
    ema_fast = df["ema_8"]
    ema_med = df["ema_21"]
    ema_slow = df["ema_50"]
    features["tf_agreement"] = (
        np.sign(ema_fast - ema_med) + np.sign(ema_med - ema_slow)
    ) / 2.0  # -1 = aligned bearish, +1 = aligned bullish, 0 = mixed

    return features


def build_labels(df: pd.DataFrame, side: str, tp_r: float = 1.5,
                 sl_r: float = 1.0, max_bars: int = 60) -> pd.Series:
    """Build binary labels: did price reach TP before SL within max_bars?

    This is the CORE ML target:
    "Given entry at this bar, will TP be hit before SL within max_bars?"

    Parameters
    ----------
    df : DataFrame with OHLCV + atr_14
    side : "long" or "short"
    tp_r : take-profit in R-multiples of ATR
    sl_r : stop-loss in R-multiples of ATR
    max_bars : max bars to look forward

    Returns
    -------
    Series of 0/1 labels (1 = TP hit first, 0 = SL hit or timeout)
    """
    if "atr_14" not in df.columns:
        df = compute_indicators(df)

    c = df["close"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    atr = df["atr_14"].astype(float).values

    labels = np.zeros(len(df), dtype=int)

    for i in range(len(df) - max_bars):
        entry = c[i]
        risk = atr[i]
        if risk <= 0 or np.isnan(risk):
            continue

        if side == "long":
            tp_price = entry + tp_r * risk
            sl_price = entry - sl_r * risk
            for j in range(i + 1, min(i + max_bars + 1, len(df))):
                if l[j] <= sl_price:
                    labels[i] = 0
                    break
                if h[j] >= tp_price:
                    labels[i] = 1
                    break
        else:
            tp_price = entry - tp_r * risk
            sl_price = entry + sl_r * risk
            for j in range(i + 1, min(i + max_bars + 1, len(df))):
                if h[j] >= sl_price:
                    labels[i] = 0
                    break
                if l[j] <= tp_price:
                    labels[i] = 1
                    break

    return pd.Series(labels, index=df.index, name="label")


def build_regression_labels(df: pd.DataFrame, side: str,
                             forward_bars: int = 12) -> pd.Series:
    """Build soft regression labels: future_return / ATR.

    Instead of binary TP/SL, this captures the continuous outcome —
    how far price moved in the signal direction, normalized by ATR.

    Returns: Series of float values (positive = moved in signal direction)
    """
    if "atr_14" not in df.columns:
        df = compute_indicators(df)

    c = df["close"].astype(float)
    atr = df["atr_14"].astype(float)

    future_close = c.shift(-forward_bars)
    future_return = future_close - c

    if side == "short":
        future_return = -future_return  # flip for shorts

    # Normalize by ATR
    label = future_return / atr.replace(0, np.nan)
    label = label.clip(-5, 5)  # cap extreme values

    return label.rename("label")


def build_directional_labels(df: pd.DataFrame, forward_bars: int = 12,
                              threshold_atr: float = 0.2) -> pd.Series:
    """Build simpler directional label: will price move up > threshold?

    Easier problem for ML than TP/SL binary.
    Returns: 1 if price moves up > threshold*ATR, 0 otherwise.
    """
    if "atr_14" not in df.columns:
        df = compute_indicators(df)

    c = df["close"].astype(float)
    atr = df["atr_14"].astype(float)

    future_close = c.shift(-forward_bars)
    future_return_atr = (future_close - c) / atr.replace(0, np.nan)

    label = (future_return_atr > threshold_atr).astype(int)
    return label.rename("label")


def build_mfe_labels(df: pd.DataFrame, side: str, max_bars: int = 30,
                     threshold_r: float = 0.2) -> pd.Series:
    """Build MFE-based label: will Max Favorable Excursion exceed threshold_r?

    Instead of asking "did the whole trade work?", asks:
    "will price move at least +0.2R in my direction within N bars?"

    This generalizes better because:
    - Captures edge even when trade management (SL/TP/trail) is imperfect
    - Less sensitive to exact exit timing
    - More aligned with "is there directional edge here?"

    Parameters
    ----------
    df : DataFrame with OHLCV + atr_14
    side : "long" or "short"
    max_bars : bars to look forward (default 30)
    threshold_r : MFE threshold in R-multiples (default 0.2)

    Returns
    -------
    Series of 0/1 labels (1 = MFE exceeded threshold)
    """
    if "atr_14" not in df.columns:
        df = compute_indicators(df)

    c = df["close"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    atr = df["atr_14"].astype(float).values

    labels = np.zeros(len(df), dtype=int)

    for i in range(len(df) - max_bars):
        entry = c[i]
        risk = atr[i]
        if risk <= 0 or np.isnan(risk):
            continue

        threshold_price = threshold_r * risk

        # Track max favorable excursion over next max_bars
        if side == "long":
            best = 0.0
            for j in range(i + 1, min(i + max_bars + 1, len(df))):
                excursion = h[j] - entry
                if excursion > best:
                    best = excursion
                if best >= threshold_price:
                    labels[i] = 1
                    break
        else:
            best = 0.0
            for j in range(i + 1, min(i + max_bars + 1, len(df))):
                excursion = entry - l[j]
                if excursion > best:
                    best = excursion
                if best >= threshold_price:
                    labels[i] = 1
                    break

    return pd.Series(labels, index=df.index, name="label")


def build_mfe_regression_labels(df: pd.DataFrame, side: str,
                                 max_bars: int = 30) -> pd.Series:
    """Build continuous MFE labels: max favorable excursion in R-multiples.

    Returns the actual MFE value (how far price moved in your favor),
    not binary. Useful for regression or for calibrating thresholds.

    Returns
    -------
    Series of float values (MFE in R-multiples, always >= 0)
    """
    if "atr_14" not in df.columns:
        df = compute_indicators(df)

    c = df["close"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    atr = df["atr_14"].astype(float).values

    mfe_values = np.zeros(len(df))

    for i in range(len(df) - max_bars):
        entry = c[i]
        risk = atr[i]
        if risk <= 0 or np.isnan(risk):
            continue

        if side == "long":
            best_price = max(h[i + 1:i + max_bars + 1]) if i + 1 < len(df) else entry
            mfe_values[i] = max(0, (best_price - entry) / risk)
        else:
            best_price = min(l[i + 1:i + max_bars + 1]) if i + 1 < len(df) else entry
            mfe_values[i] = max(0, (entry - best_price) / risk)

    return pd.Series(mfe_values, index=df.index, name="mfe_r").clip(0, 10)
