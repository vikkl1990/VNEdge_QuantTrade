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
    # 9c. TRANSITION / DELTA FEATURES (state change > state)
    # How things are changing matters more than where they are.
    # ================================================================

    # delta_vwap_dist: Change in VWAP distance over 3 and 5 bars
    vwap_dist_now = (c - df["vwap"]) / atr.replace(0, np.nan)
    vwap_dist_3ago = (c.shift(3) - df["vwap"].shift(3)) / atr.shift(3).replace(0, np.nan)
    vwap_dist_5ago = (c.shift(5) - df["vwap"].shift(5)) / atr.shift(5).replace(0, np.nan)
    features["delta_vwap_dist_3"] = vwap_dist_now - vwap_dist_3ago
    features["delta_vwap_dist_5"] = vwap_dist_now - vwap_dist_5ago

    # delta_trend_strength: Change in (EMA8-EMA21)/ATR over 3 and 5 bars
    trend_now = (df["ema_8"] - df["ema_21"]) / atr.replace(0, np.nan)
    trend_3ago = (df["ema_8"].shift(3) - df["ema_21"].shift(3)) / atr.shift(3).replace(0, np.nan)
    trend_5ago = (df["ema_8"].shift(5) - df["ema_21"].shift(5)) / atr.shift(5).replace(0, np.nan)
    features["delta_trend_strength_3"] = trend_now - trend_3ago
    features["delta_trend_strength_5"] = trend_now - trend_5ago

    # delta_atr_ratio: Change in ATR ratio (ATR / rolling ATR mean) over 3 and 5 bars
    atr_rolling_mean = atr.rolling(100).mean().replace(0, np.nan)
    atr_ratio_now = atr / atr_rolling_mean
    atr_ratio_3ago = atr.shift(3) / atr.shift(3).rolling(100).mean().replace(0, np.nan)
    atr_ratio_5ago = atr.shift(5) / atr.shift(5).rolling(100).mean().replace(0, np.nan)
    features["delta_atr_ratio_3"] = atr_ratio_now - atr_ratio_3ago
    features["delta_atr_ratio_5"] = atr_ratio_now - atr_ratio_5ago

    # impulse_decay: Bars since last impulse candle (body > 0.8x ATR). Lower = recent impulse.
    is_impulse_candle = (df["body"] > 0.8 * atr).astype(float)
    bars_since_impulse = pd.Series(np.nan, index=df.index)
    last_imp_idx = -999
    for i in range(len(df)):
        if is_impulse_candle.iloc[i] > 0:
            last_imp_idx = i
        bars_since_impulse.iloc[i] = float(i - last_imp_idx) if last_imp_idx >= 0 else 50.0
    features["transition_impulse_decay"] = bars_since_impulse.clip(0, 50) / 50.0  # normalize 0-1

    # vwap_reversion_speed: Rate of return toward VWAP over 3 bars
    # Positive = reverting toward VWAP, negative = moving away
    features["transition_vwap_reversion_speed"] = (vwap_dist_3ago.abs() - vwap_dist_now.abs()) / 3.0

    # momentum_acceleration: Second derivative of price (return_5 - return_5_shifted_5)
    return_5 = c.pct_change(5)
    features["delta_momentum_acceleration"] = return_5 - return_5.shift(5)

    # vol_regime_change: Difference between current vol z-score and 5-bar-ago vol z-score
    vol_zscore_now = (v - df["vol_sma_20"]) / df["vol_std_20"].replace(0, np.nan)
    vol_zscore_5ago = (v.shift(5) - df["vol_sma_20"].shift(5)) / df["vol_std_20"].shift(5).replace(0, np.nan)
    features["delta_vol_regime_change"] = vol_zscore_now - vol_zscore_5ago

    # slope_change_ema8: Change in EMA8 slope (current slope - previous slope)
    ema8_slope_now = df["ema_8"].pct_change(3)
    ema8_slope_prev = df["ema_8"].pct_change(3).shift(3)
    features["delta_slope_change_ema8"] = ema8_slope_now - ema8_slope_prev

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


    # ================================================================
    # 11. FVG (Fair Value Gap) — Structural Imbalance Detection
    # Markets revisit price inefficiencies. FVGs measure gap between
    # candle[i] low and candle[i-2] high (bullish) or vice versa.
    # ================================================================
    # Bullish FVG: low[i] > high[i-2] (gap up, price skipped a zone)
    fvg_bull = (l > h.shift(2)).astype(float)
    fvg_bull_size = (l - h.shift(2)).clip(lower=0) / atr.replace(0, np.nan)
    features["fvg_bullish"] = fvg_bull
    features["fvg_bull_size"] = fvg_bull_size
    
    # Bearish FVG: high[i] < low[i-2] (gap down)
    fvg_bear = (h < l.shift(2)).astype(float)
    fvg_bear_size = (l.shift(2) - h).clip(lower=0) / atr.replace(0, np.nan)
    features["fvg_bearish"] = fvg_bear
    features["fvg_bear_size"] = fvg_bear_size
    
    # Any FVG present (directional agnostic)
    features["fvg_present"] = ((fvg_bull + fvg_bear) > 0).astype(float)
    
    # FVG within recent N bars (is there an unfilled gap nearby?)
    features["fvg_bull_recent_5"] = fvg_bull.rolling(5).max().fillna(0)
    features["fvg_bear_recent_5"] = fvg_bear.rolling(5).max().fillna(0)
    features["fvg_bull_recent_10"] = fvg_bull.rolling(10).max().fillna(0)
    features["fvg_bear_recent_10"] = fvg_bear.rolling(10).max().fillna(0)
    
    # Largest recent FVG size (strength of imbalance)
    features["fvg_max_bull_size_10"] = fvg_bull_size.rolling(10).max().fillna(0)
    features["fvg_max_bear_size_10"] = fvg_bear_size.rolling(10).max().fillna(0)
    
    # FVG alignment with trend (FVG in trend direction = stronger signal)
    features["fvg_trend_aligned"] = (
        fvg_bull * (features["trend_strength"] > 0).astype(float) +
        fvg_bear * (features["trend_strength"] < 0).astype(float)
    )

    # ================================================================
    # 12. VOLUME INTELLIGENCE (buy/sell imbalance, CVD proxy)
    # ================================================================
    # Buy/sell imbalance: close position * normalized volume
    features["buy_sell_imbalance"] = (features["close_position"] - 0.5) * 2.0 * (v / df["vol_sma_20"].replace(0, np.nan))
    # CVD proxy: cumulative buy/sell pressure over 10 bars
    cvd_raw = ((c - l) / df["range"].replace(0, np.nan) - 0.5) * v
    features["cvd_proxy_10"] = cvd_raw.rolling(10).sum() / (df["vol_sma_20"] * 10).replace(0, np.nan)
    features["cvd_proxy_10"] = features["cvd_proxy_10"].clip(-5, 5)
    # Volume spike ratio (3-bar)
    features["vol_spike_ratio_3"] = v / v.rolling(3).mean().replace(0, np.nan)

    # ================================================================
    # 13. VWAP BANDS (normalized distance)
    # ================================================================
    vwap_dev = (c - df["vwap"]).rolling(20).std()
    features["vwap_band_distance"] = (c - df["vwap"]) / vwap_dev.replace(0, np.nan)
    features["vwap_upper_band"] = (df["vwap"] + 2 * vwap_dev - c) / atr.replace(0, np.nan)
    features["vwap_lower_band"] = (c - df["vwap"] + 2 * vwap_dev) / atr.replace(0, np.nan)

    # ================================================================
    # 14. ATR EXPANSION RATIO (10-bar lookback)
    # ================================================================
    features["atr_expansion_10"] = atr / atr.shift(10).replace(0, np.nan)
    # ATR regime flags
    features["atr_quiet"] = (features["vol_regime"] < 0.5).astype(float)
    features["atr_expanding"] = (features["atr_expansion_10"] > 1.2).astype(float)
    # Chaotic: high ATR + poor body structure
    body_avg_5 = df["body_ratio"].rolling(5).mean()
    features["atr_chaotic"] = ((features["vol_regime"] > 1.5) & (body_avg_5 < 0.4)).astype(float)

    # ================================================================
    # 15. EMA SLOPE ACCELERATION (second derivative)
    # ================================================================
    features["ema_slope_accel"] = features["ema_slope_8"] - df["ema_8"].pct_change(3).shift(3)

    # ================================================================
    # 16. MTF ALIGNMENT
    # ================================================================
    ema200 = df["ema_200"]
    # EMA alignment score: how many EMAs agree
    bull_ema = (
        (c > df["ema_8"]).astype(int) +
        (df["ema_8"] > df["ema_21"]).astype(int) +
        (df["ema_21"] > df["ema_50"]).astype(int) +
        (df["ema_50"] > ema200).astype(int)
    )
    features["ema_alignment"] = (bull_ema - 2) / 2.0  # -1 to +1
    # Distance from EMA 200
    features["dist_from_ema200"] = (c - ema200) / atr.replace(0, np.nan)
    # HTF bias placeholder (filled at scoring time)
    features["htf_bias"] = 0.0
    features["htf_trend_strength"] = 0.0

    # ================================================================
    # 17. FVG ENHANCED (distance + alignment)
    # ================================================================
    # FVG size in ATR (max of bull/bear)
    features["fvg_size_atr"] = pd.concat([fvg_bull_size, fvg_bear_size], axis=1).max(axis=1)
    # FVG distance placeholder (computed per-bar in live scorer)
    features["fvg_distance"] = 10.0  # default far
    features["fvg_alignment_score"] = 0.0  # computed live

    # ================================================================
    # 18. ORDER BLOCK PROXY
    # ================================================================
    # Impulse detection: body > 1.5 ATR
    is_impulse = (df["body"] > 1.5 * atr).astype(float)
    # Distance to last impulse bar
    impulse_bars_ago = pd.Series(0.0, index=df.index)
    last_impulse = -999
    for i in range(len(df)):
        if is_impulse.iloc[i] > 0:
            last_impulse = i
        impulse_bars_ago.iloc[i] = (i - last_impulse) / 10.0 if last_impulse >= 0 else 10.0
    features["ob_distance"] = impulse_bars_ago.clip(0, 10)
    features["ob_impulse_strength"] = (df["body"] / atr.replace(0, np.nan)).clip(0, 5)
    # Consolidation size before impulse (5-bar range / ATR)
    features["ob_consolidation_size"] = (
        (h.rolling(5).max() - l.rolling(5).min()) / atr.replace(0, np.nan)
    ).clip(0, 5)

    # ================================================================
    # 19. REGIME FEATURES (continuous, for ML)
    # ================================================================
    features["regime_trend_score"] = features["ema_alignment"]
    features["regime_vol_score"] = features["vol_regime"]
    features["regime_range_score"] = features["range_position"]

    # ================================================================
    # 20. REGIME INTERACTION FEATURES
    # Let ML learn which features matter in which regime
    # ================================================================
    # Trend x momentum interaction
    features["trend_x_return5"] = features["trend_strength"] * features["return_5"]
    features["trend_x_ema_slope"] = features["trend_strength"] * features["ema_slope_8"]
    # Volatility x volume interaction
    features["vol_x_volume"] = features["atr_expansion"] * features["volume_zscore"]
    # VWAP x trend interaction
    features["vwap_x_trend"] = features["dist_from_vwap"] * features["trend_strength"]
    # Range position x volatility regime
    features["range_x_regime_vol"] = features["range_position"] * features["vol_regime"]

    # ================================================================
    # CLEANUP: Replace NaN/inf with 0.0 for all features
    # ================================================================
    features = features.replace([np.inf, -np.inf], 0.0).fillna(0.0)

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


def build_tp_vs_sl_labels(df: pd.DataFrame, side: str, tp_r: float = 1.5,
                           sl_r: float = 1.0, max_bars: int = 60) -> pd.Series:
    """Build TP vs SL label: did TP1 hit before SL?

    This is the BEST label for trading ML because:
    - Reflects actual trade outcome
    - Incorporates risk/reward naturally
    - Includes failure cases (SL hit, timeout)
    - Usually produces balanced classes (40-60%)

    Parameters
    ----------
    df : DataFrame with OHLCV + atr_14
    side : "long" or "short"
    tp_r : take-profit in R-multiples of ATR (default 1.5)
    sl_r : stop-loss in R-multiples of ATR (default 1.0)
    max_bars : max bars to look forward (default 60)

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

    labels = np.full(len(df), -1, dtype=int)  # -1 = not computed

    for i in range(len(df) - max_bars):
        entry = c[i]
        risk = atr[i]
        if risk <= 0 or np.isnan(risk):
            labels[i] = 0
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
                # Timeout — neither TP nor SL hit
                labels[i] = 0
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
            else:
                labels[i] = 0

    # Replace -1 with 0 for tail rows
    labels[labels == -1] = 0
    return pd.Series(labels, index=df.index, name="label")


def build_relative_performance_labels(df: pd.DataFrame, side: str,
                                       forward_bars: int = 12,
                                       top_pct: float = 0.30) -> pd.Series:
    """Build relative performance label: top N% of moves → positive.

    label = future_return / ATR, then:
    - top 30% → 1 (positive)
    - bottom 70% → 0 (negative)

    This creates:
    - Stable distribution (always ~30% positive regardless of regime)
    - Adaptive threshold (per market conditions)
    - No dependency on fixed price levels

    Parameters
    ----------
    df : DataFrame with OHLCV + atr_14
    side : "long" or "short"
    forward_bars : bars to look forward (default 12)
    top_pct : fraction considered "positive" (default 0.30 = top 30%)

    Returns
    -------
    Series of 0/1 labels
    """
    if "atr_14" not in df.columns:
        df = compute_indicators(df)

    c = df["close"].astype(float)
    atr = df["atr_14"].astype(float)

    future_close = c.shift(-forward_bars)
    future_return = future_close - c

    if side == "short":
        future_return = -future_return

    # Normalize by ATR
    normalized = future_return / atr.replace(0, np.nan)
    normalized = normalized.clip(-5, 5)

    # Use rolling percentile for adaptive threshold (250-bar window)
    # This makes the threshold adapt to current market conditions
    threshold = normalized.rolling(250, min_periods=50).quantile(1.0 - top_pct)

    # Fallback: use global percentile where rolling isn't available
    global_threshold = normalized.quantile(1.0 - top_pct)
    threshold = threshold.fillna(global_threshold)

    labels = (normalized >= threshold).astype(int)
    # NaN rows (no future data) → 0
    labels = labels.fillna(0).astype(int)

    return labels.rename("label")
