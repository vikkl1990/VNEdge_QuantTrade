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

    # Ichimoku Cloud components (for context features, NOT standalone signals)
    # Tenkan-sen (conversion line): 9-period mid
    tenkan_high = h.rolling(9).max()
    tenkan_low = l.rolling(9).min()
    df["ichimoku_tenkan"] = (tenkan_high + tenkan_low) / 2
    # Kijun-sen (base line): 26-period mid
    kijun_high = h.rolling(26).max()
    kijun_low = l.rolling(26).min()
    df["ichimoku_kijun"] = (kijun_high + kijun_low) / 2
    # Senkou Span A (leading span A): midpoint of tenkan/kijun, shifted forward 26
    df["ichimoku_span_a"] = ((df["ichimoku_tenkan"] + df["ichimoku_kijun"]) / 2).shift(26)
    # Senkou Span B (leading span B): 52-period mid, shifted forward 26
    span_b_high = h.rolling(52).max()
    span_b_low = l.rolling(52).min()
    df["ichimoku_span_b"] = ((span_b_high + span_b_low) / 2).shift(26)
    # Cloud top/bottom
    df["ichimoku_cloud_top"] = pd.concat([df["ichimoku_span_a"], df["ichimoku_span_b"]], axis=1).max(axis=1)
    df["ichimoku_cloud_bottom"] = pd.concat([df["ichimoku_span_a"], df["ichimoku_span_b"]], axis=1).min(axis=1)

    # Candle components
    df["body"] = (c - o).abs()
    df["range"] = (h - l).replace(0, np.nan)
    df["body_ratio"] = df["body"] / df["range"]
    df["upper_wick"] = h - pd.concat([c, o], axis=1).max(axis=1)
    df["lower_wick"] = pd.concat([c, o], axis=1).min(axis=1) - l
    df["is_bullish"] = (c > o).astype(int)

    return df


def _build_htf_block(df_ltf: pd.DataFrame, htf_df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """Phase 4.1a — Rich per-HTF feature block.

    Computes a comprehensive feature set from a higher-timeframe dataframe
    and aligns it to the LTF index via forward-fill + shift(1) (strict past).

    Args:
        df_ltf: LTF dataframe (for index alignment)
        htf_df: HTF OHLCV dataframe (e.g. 1h or 4h candles)
        prefix: feature name prefix (e.g. "h1" or "h4")

    Returns:
        DataFrame of aligned HTF features with columns named {prefix}_*
    """
    # Lower bound: need ~20 bars for the rolling windows to make sense.
    # 4h data often has fewer bars than 1h, so accept smaller datasets.
    if htf_df is None or len(htf_df) < 20:
        return pd.DataFrame(index=df_ltf.index)

    htf = compute_indicators(htf_df.copy())
    c = htf["close"].astype(float)
    h_col = htf["high"].astype(float)
    l_col = htf["low"].astype(float)
    o_col = htf["open"].astype(float)

    feats = pd.DataFrame(index=htf.index)

    # ── Trend (EMA alignment) ──
    if "ema_8" in htf.columns and "ema_21" in htf.columns and "ema_50" in htf.columns:
        ema8 = htf["ema_8"]
        ema21 = htf["ema_21"]
        ema50 = htf["ema_50"]
        # Trend strength: EMA stack direction
        trend = np.where(
            (ema8 > ema21) & (ema21 > ema50), 1.0,
            np.where((ema8 < ema21) & (ema21 < ema50), -1.0, 0.0)
        )
        feats[f"{prefix}_trend_bias"] = trend
        # EMA slopes (normalized by ATR)
        atr_safe = htf["atr_14"].replace(0, np.nan)
        feats[f"{prefix}_ema8_slope"] = (ema8.diff(3) / atr_safe).fillna(0.0)
        feats[f"{prefix}_ema21_slope"] = (ema21.diff(5) / atr_safe).fillna(0.0)
        # Distance from EMA50 (macro stretch)
        feats[f"{prefix}_dist_ema50"] = ((c - ema50) / atr_safe).fillna(0.0)
    else:
        feats[f"{prefix}_trend_bias"] = 0.0
        feats[f"{prefix}_ema8_slope"] = 0.0
        feats[f"{prefix}_ema21_slope"] = 0.0
        feats[f"{prefix}_dist_ema50"] = 0.0

    # ── Momentum (multi-bar returns on HTF) ──
    feats[f"{prefix}_return_1"] = c.pct_change(1).fillna(0.0)
    feats[f"{prefix}_return_3"] = c.pct_change(3).fillna(0.0)
    feats[f"{prefix}_return_5"] = c.pct_change(5).fillna(0.0)

    # ── Volatility ──
    if "atr_14" in htf.columns and "atr_7" in htf.columns:
        feats[f"{prefix}_atr_ratio"] = (htf["atr_7"] / htf["atr_14"].replace(0, np.nan)).fillna(1.0)

    # ── Candle structure (last N bars) ──
    body = (c - o_col).abs()
    rng = (h_col - l_col).replace(0, np.nan)
    feats[f"{prefix}_body_ratio"] = (body / rng).fillna(0.0)
    feats[f"{prefix}_upper_wick"] = ((h_col - pd.concat([o_col, c], axis=1).max(axis=1)) / rng).fillna(0.0)
    feats[f"{prefix}_lower_wick"] = ((pd.concat([o_col, c], axis=1).min(axis=1) - l_col) / rng).fillna(0.0)

    # Last candle direction
    feats[f"{prefix}_last_direction"] = np.where(c > o_col, 1.0, np.where(c < o_col, -1.0, 0.0))

    # ── Range position (where in the 20-bar range are we?) ──
    rolling_high = h_col.rolling(20).max()
    rolling_low = l_col.rolling(20).min()
    rolling_range = (rolling_high - rolling_low).replace(0, np.nan)
    feats[f"{prefix}_range_pos"] = ((c - rolling_low) / rolling_range).fillna(0.5)

    # ── RSI for mean-reversion context ──
    if "rsi_14" in htf.columns:
        feats[f"{prefix}_rsi"] = (htf["rsi_14"] / 100.0 - 0.5).fillna(0.0)  # normalized -0.5..+0.5
    else:
        feats[f"{prefix}_rsi"] = 0.0

    # ── Align to LTF index: forward-fill + shift(1) for strictly past-looking ──
    aligned = feats.reindex(df_ltf.index, method="ffill").shift(1)
    # Fill remaining NaNs (first bars before any HTF bar exists)
    aligned = aligned.fillna(0.0)
    return aligned


def _build_orderbook_block(orderbook: Optional[dict]) -> dict:
    """Phase 5.0c — Orderbook microstructure features.

    Builds 12 features from a single L2 snapshot (dict with 'buy' and 'sell'
    lists of [price, size] levels). Returns a plain dict of scalar values.
    If orderbook is None or empty, returns all zeros (graceful degradation
    so ML pipeline never breaks when L2 isn't available).

    Features:
      ob_spread_bps       — bid-ask spread in basis points
      ob_bid_depth_5      — total bid volume within top 5 levels
      ob_bid_depth_10     — total bid volume within top 10 levels
      ob_ask_depth_5      — total ask volume within top 5 levels
      ob_ask_depth_10     — total ask volume within top 10 levels
      ob_imbalance_5      — (bid_5 - ask_5) / (bid_5 + ask_5), normalized
      ob_imbalance_10     — same for 10 levels
      ob_wall_distance    — distance to nearest 3× avg-size wall, in bps
      ob_taker_flow_proxy — imbalance_5 × spread_bps (high = pressure)
      ob_bid_slope        — linear slope of bid volume across 10 levels
      ob_ask_slope        — linear slope of ask volume across 10 levels
      ob_microprice_offset — (microprice - midprice) / midprice in bps
    """
    zeros = {
        "ob_spread_bps": 0.0, "ob_bid_depth_5": 0.0, "ob_bid_depth_10": 0.0,
        "ob_ask_depth_5": 0.0, "ob_ask_depth_10": 0.0,
        "ob_imbalance_5": 0.0, "ob_imbalance_10": 0.0,
        "ob_wall_distance": 0.0, "ob_taker_flow_proxy": 0.0,
        "ob_bid_slope": 0.0, "ob_ask_slope": 0.0, "ob_microprice_offset": 0.0,
    }
    if not orderbook or not isinstance(orderbook, dict):
        return zeros

    bids = orderbook.get("buy", [])  # [[price, size], ...]
    asks = orderbook.get("sell", [])
    if not bids or not asks:
        return zeros

    try:
        best_bid = float(bids[0][0]) if bids else 0
        best_ask = float(asks[0][0]) if asks else 0
        mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else 0

        if mid <= 0:
            return zeros

        # Spread
        spread_bps = (best_ask - best_bid) / mid * 10000.0

        # Depth (cumulative volume at N levels)
        bid_sizes = [float(lvl[1]) for lvl in bids[:10] if len(lvl) >= 2]
        ask_sizes = [float(lvl[1]) for lvl in asks[:10] if len(lvl) >= 2]
        bid_depth_5 = sum(bid_sizes[:5])
        bid_depth_10 = sum(bid_sizes[:10])
        ask_depth_5 = sum(ask_sizes[:5])
        ask_depth_10 = sum(ask_sizes[:10])

        # Imbalance (normalized -1 to +1)
        total_5 = bid_depth_5 + ask_depth_5
        total_10 = bid_depth_10 + ask_depth_10
        imbalance_5 = (bid_depth_5 - ask_depth_5) / total_5 if total_5 > 0 else 0
        imbalance_10 = (bid_depth_10 - ask_depth_10) / total_10 if total_10 > 0 else 0

        # Wall distance (nearest level with size > 3× average)
        all_sizes = bid_sizes + ask_sizes
        avg_size = sum(all_sizes) / len(all_sizes) if all_sizes else 1
        wall_threshold = avg_size * 3.0
        wall_distance_bps = 500.0  # default: far away
        for lvl in bids[:10]:
            if len(lvl) >= 2 and float(lvl[1]) > wall_threshold:
                wall_distance_bps = abs(float(lvl[0]) - mid) / mid * 10000.0
                break
        for lvl in asks[:10]:
            if len(lvl) >= 2 and float(lvl[1]) > wall_threshold:
                d = abs(float(lvl[0]) - mid) / mid * 10000.0
                wall_distance_bps = min(wall_distance_bps, d)
                break

        # Taker flow proxy
        taker_flow = imbalance_5 * spread_bps

        # Volume slope across levels (linear regression proxy)
        def _slope(sizes):
            n = len(sizes)
            if n < 2:
                return 0.0
            x_mean = (n - 1) / 2.0
            y_mean = sum(sizes) / n
            num = sum((i - x_mean) * (s - y_mean) for i, s in enumerate(sizes))
            den = sum((i - x_mean) ** 2 for i in range(n))
            return num / den if den > 0 else 0.0

        bid_slope = _slope(bid_sizes)
        ask_slope = _slope(ask_sizes)

        # Microprice offset
        microprice = (best_bid * ask_sizes[0] + best_ask * bid_sizes[0]) / (bid_sizes[0] + ask_sizes[0]) if (bid_sizes and ask_sizes and (bid_sizes[0] + ask_sizes[0]) > 0) else mid
        microprice_offset_bps = (microprice - mid) / mid * 10000.0 if mid > 0 else 0

        return {
            "ob_spread_bps": round(spread_bps, 2),
            "ob_bid_depth_5": round(bid_depth_5, 4),
            "ob_bid_depth_10": round(bid_depth_10, 4),
            "ob_ask_depth_5": round(ask_depth_5, 4),
            "ob_ask_depth_10": round(ask_depth_10, 4),
            "ob_imbalance_5": round(imbalance_5, 4),
            "ob_imbalance_10": round(imbalance_10, 4),
            "ob_wall_distance": round(wall_distance_bps, 2),
            "ob_taker_flow_proxy": round(taker_flow, 4),
            "ob_bid_slope": round(bid_slope, 4),
            "ob_ask_slope": round(ask_slope, 4),
            "ob_microprice_offset": round(microprice_offset_bps, 4),
        }
    except Exception:
        return zeros


def _build_btc_block(
    df_ltf: pd.DataFrame,
    btc_df: Optional[pd.DataFrame],
    symbol: str,
) -> pd.DataFrame:
    """Phase 5.0a — BTC cross-asset context block.

    Builds a ~12-feature block from BTC candles that gives ML the same
    "what's BTC doing right now" context a human trader has. Alts correlate
    heavily with BTC on short timeframes — currently the model is blind to
    BTC's state when scoring an ETH/SOL/XRP setup.

    Key features:
      - btc_return_1/3/5/12  (multi-bar returns)
      - btc_atr_ratio        (BTC volatility regime)
      - btc_dist_from_vwap   (BTC stretch from fair value)
      - btc_ema8_slope       (BTC trend momentum)
      - btc_trend_bias       (+1/-1/0 based on EMA stack)
      - btc_range_pos        (where in 20-bar range)
      - symbol_btc_corr_60   (rolling correlation LTF vs BTC)
      - symbol_btc_beta_60   (rolling beta)

    Returns a DataFrame aligned to df_ltf.index. Forward-fills BTC features
    to the LTF bar boundary and shift(1) for strictly-past semantics.
    If btc_df is None or insufficient, returns zeros.
    """
    # When training on BTC itself, we'd normally skip (self-correlation trivial).
    # But keeping features consistent across symbols is more important than
    # the few bytes saved — so always include them. For BTC they'll all be
    # self-derived (beta=1.0, corr=1.0).
    if btc_df is None or len(btc_df) < 50:
        return pd.DataFrame(index=df_ltf.index)

    btc = compute_indicators(btc_df.copy())
    btc_c = btc["close"].astype(float)
    btc_o = btc["open"].astype(float)
    btc_h = btc["high"].astype(float)
    btc_l = btc["low"].astype(float)

    feats = pd.DataFrame(index=btc.index)

    # Multi-bar returns (how is BTC moving recently?)
    feats["btc_return_1"] = btc_c.pct_change(1).fillna(0.0)
    feats["btc_return_3"] = btc_c.pct_change(3).fillna(0.0)
    feats["btc_return_5"] = btc_c.pct_change(5).fillna(0.0)
    feats["btc_return_12"] = btc_c.pct_change(12).fillna(0.0)  # ~1h on 5m

    # Volatility regime
    if "atr_14" in btc.columns:
        btc_atr = btc["atr_14"].replace(0, np.nan)
        feats["btc_atr_ratio"] = (btc_atr / btc_atr.rolling(100).mean().replace(0, np.nan)).fillna(1.0)
    else:
        feats["btc_atr_ratio"] = 1.0

    # VWAP distance (BTC stretched?)
    if "vwap" in btc.columns and "atr_14" in btc.columns:
        btc_vwap = btc["vwap"].replace(0, np.nan)
        btc_atr_s = btc["atr_14"].replace(0, np.nan)
        feats["btc_dist_from_vwap"] = ((btc_c - btc_vwap) / btc_atr_s).fillna(0.0)
    else:
        feats["btc_dist_from_vwap"] = 0.0

    # Trend bias + EMA slope
    if "ema_8" in btc.columns and "ema_21" in btc.columns and "ema_50" in btc.columns:
        btc_ema8 = btc["ema_8"]
        btc_ema21 = btc["ema_21"]
        btc_ema50 = btc["ema_50"]
        btc_atr_s = btc["atr_14"].replace(0, np.nan)
        # Trend bias
        trend = np.where(
            (btc_ema8 > btc_ema21) & (btc_ema21 > btc_ema50), 1.0,
            np.where((btc_ema8 < btc_ema21) & (btc_ema21 < btc_ema50), -1.0, 0.0),
        )
        feats["btc_trend_bias"] = trend
        # EMA8 slope (normalized by ATR)
        feats["btc_ema8_slope"] = (btc_ema8.diff(3) / btc_atr_s).fillna(0.0)
        # Distance from EMA50 (macro stretch)
        feats["btc_dist_ema50"] = ((btc_c - btc_ema50) / btc_atr_s).fillna(0.0)
    else:
        feats["btc_trend_bias"] = 0.0
        feats["btc_ema8_slope"] = 0.0
        feats["btc_dist_ema50"] = 0.0

    # Range position (where in 20-bar range is BTC?)
    rolling_high = btc_h.rolling(20).max()
    rolling_low = btc_l.rolling(20).min()
    rolling_range = (rolling_high - rolling_low).replace(0, np.nan)
    feats["btc_range_pos"] = ((btc_c - rolling_low) / rolling_range).fillna(0.5)

    # Breakout pressure: how close is BTC to breaking 20-bar high/low
    feats["btc_near_high_atr"] = ((rolling_high - btc_c) / btc["atr_14"].replace(0, np.nan)).fillna(0.0).clip(-5, 5)
    feats["btc_near_low_atr"] = ((btc_c - rolling_low) / btc["atr_14"].replace(0, np.nan)).fillna(0.0).clip(-5, 5)

    # ── Align BTC features to LTF index ──
    btc_aligned = feats.reindex(df_ltf.index, method="ffill").shift(1).fillna(0.0)

    # ── Cross-asset: rolling 60-bar beta + correlation LTF vs BTC ──
    # Can only compute when we have both series aligned
    try:
        ltf_c = df_ltf["close"].astype(float)
        # Align BTC close to LTF index for correlation math
        btc_c_aligned = btc_c.reindex(df_ltf.index, method="ffill").shift(1)
        ltf_ret = ltf_c.pct_change(1).fillna(0.0)
        btc_ret = btc_c_aligned.pct_change(1).fillna(0.0)

        # Rolling 60-bar correlation
        btc_aligned["symbol_btc_corr_60"] = (
            ltf_ret.rolling(60, min_periods=20)
                   .corr(btc_ret)
                   .fillna(0.0)
                   .clip(-1, 1)
        )
        # Rolling 60-bar beta = cov(sym, btc) / var(btc)
        cov = ltf_ret.rolling(60, min_periods=20).cov(btc_ret)
        var_btc = btc_ret.rolling(60, min_periods=20).var()
        beta = (cov / var_btc.replace(0, np.nan)).fillna(0.0).clip(-5, 5)
        btc_aligned["symbol_btc_beta_60"] = beta
    except Exception:
        btc_aligned["symbol_btc_corr_60"] = 0.0
        btc_aligned["symbol_btc_beta_60"] = 0.0

    return btc_aligned


def build_features(
    df: pd.DataFrame,
    htf_df: Optional[pd.DataFrame] = None,
    htf_1h_df: Optional[pd.DataFrame] = None,
    htf_4h_df: Optional[pd.DataFrame] = None,
    btc_df: Optional[pd.DataFrame] = None,
    symbol: str = "",
    orderbook: Optional[dict] = None,
) -> pd.DataFrame:
    """Build ML features from pure candle math + optional L2 orderbook.

    Returns ~35 features derived from price movement, volatility, and participation.
    Phase 5.0c adds 12 orderbook microstructure features when orderbook is provided.
    No raw indicators — only relationships.

    Phase 4.1a: added htf_1h_df + htf_4h_df for HTF feature fusion.
    Phase 5.0a: added btc_df for BTC cross-asset context features.
    """
    df = compute_indicators(df)
    # Performance fix: collect features into a plain dict to avoid DataFrame
    # column-by-column insertion fragmentation (201+ assignments). The dict
    # is converted to DataFrame in one shot before the cleanup block.
    features = {}

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
    # 21. ICHIMOKU CONTEXT FEATURES (trend/regime layer, NOT signals)
    # Used as: trend confirmation, regime quality, runner qualification.
    # NOT used as: standalone entry signals or crossover triggers.
    # ================================================================
    cloud_top = df["ichimoku_cloud_top"]
    cloud_bottom = df["ichimoku_cloud_bottom"]
    tenkan = df["ichimoku_tenkan"]
    kijun = df["ichimoku_kijun"]

    # Price vs cloud: +1 = above, -1 = below, 0 = inside cloud
    features["ichi_price_vs_cloud"] = np.where(
        c > cloud_top, 1.0,
        np.where(c < cloud_bottom, -1.0, 0.0)
    )
    # Tenkan/Kijun alignment: 1 = bullish (tenkan > kijun), -1 = bearish
    features["ichi_tk_alignment"] = np.sign(tenkan - kijun)
    # Cloud thickness (normalized by ATR — thin = weak, thick = strong trend)
    cloud_thickness = (cloud_top - cloud_bottom).abs()
    features["ichi_cloud_thickness"] = cloud_thickness / atr.replace(0, np.nan)
    # Cloud slope (is cloud rising or falling? — trend direction quality)
    features["ichi_cloud_slope"] = (cloud_top.diff(3) + cloud_bottom.diff(3)) / (2 * atr.replace(0, np.nan))
    # Distance from cloud edge (normalized by ATR)
    dist_above = (c - cloud_top) / atr.replace(0, np.nan)
    dist_below = (cloud_bottom - c) / atr.replace(0, np.nan)
    features["ichi_dist_from_cloud"] = np.where(
        c > cloud_top, dist_above,
        np.where(c < cloud_bottom, -dist_below, 0.0)
    )
    # Chikou clearance: is current price clearly above price 26 bars ago?
    # (simplified: just use return_26 normalized by ATR)
    features["ichi_chikou_clearance"] = (c - c.shift(26)) / atr.replace(0, np.nan)

    # ================================================================
    # 22. FAIR VALUE GAP (FVG) FEATURES
    # Detect imbalances in price — where candle N+1's low > candle N-1's high
    # (bullish FVG) or candle N+1's high < candle N-1's low (bearish FVG).
    # Used as: context feature for candidate quality, NOT hard filter.
    # ================================================================
    # Bullish FVG: gap between candle[i-2] high and candle[i] low
    bull_fvg_gap = l - h.shift(2)  # positive = bullish FVG exists
    bear_fvg_gap = l.shift(2) - h  # positive = bearish FVG exists

    # Nearest bullish FVG within last 20 bars (normalized by ATR)
    bull_fvg_exists = (bull_fvg_gap > 0).astype(float)
    bear_fvg_exists = (bear_fvg_gap > 0).astype(float)

    # Recent FVG count (how many FVGs in last 20 bars — more = stronger imbalance)
    features["fvg_bull_count_20"] = bull_fvg_exists.rolling(20).sum()
    features["fvg_bear_count_20"] = bear_fvg_exists.rolling(20).sum()

    # FVG imbalance ratio (bull - bear, normalized)
    total_fvg = features["fvg_bull_count_20"] + features["fvg_bear_count_20"]
    features["fvg_imbalance"] = np.where(
        total_fvg > 0,
        (features["fvg_bull_count_20"] - features["fvg_bear_count_20"]) / total_fvg,
        0.0
    )

    # Is price currently inside a recent FVG? (within last 10 bars)
    # Track the midpoint of most recent bullish FVG
    bull_fvg_mid = np.where(bull_fvg_gap > 0, (h.shift(2) + l) / 2, np.nan)
    bull_fvg_mid = pd.Series(bull_fvg_mid, index=df.index).ffill(limit=10)
    bear_fvg_mid = np.where(bear_fvg_gap > 0, (l.shift(2) + h) / 2, np.nan)
    bear_fvg_mid = pd.Series(bear_fvg_mid, index=df.index).ffill(limit=10)

    # Distance from nearest FVG midpoint (normalized by ATR)
    features["dist_from_bull_fvg"] = (c - bull_fvg_mid) / atr.replace(0, np.nan)
    features["dist_from_bear_fvg"] = (bear_fvg_mid - c) / atr.replace(0, np.nan)

    # FVG freshness: how many bars since last FVG (recent = more relevant)
    bull_fvg_bars_ago = bull_fvg_exists.groupby(
        (bull_fvg_exists != bull_fvg_exists.shift()).cumsum()
    ).cumcount()
    features["fvg_recency"] = np.where(
        features["fvg_bull_count_20"] + features["fvg_bear_count_20"] > 0,
        1.0 / (bull_fvg_bars_ago.clip(lower=1)),
        0.0
    )

    # ================================================================
    # 23. ORDER BLOCK PROXIMITY FEATURES
    # Order block = last bullish candle before a bearish move (or vice versa)
    # Approximated as: large body candle followed by reversal
    # ================================================================
    body_size = (c - o).abs()
    body_atr_ratio = body_size / atr.replace(0, np.nan)
    is_large_body = body_atr_ratio > 1.0  # body > 1 ATR = significant candle

    # Bullish OB: large bearish candle FOLLOWED BY reversal (use past confirmation only)
    # shift(1) = previous bar was large bearish, current bar is bullish = confirmed OB
    is_bearish = c < o
    is_bullish_candle = c > o
    bull_ob = is_large_body.shift(1) & is_bearish.shift(1) & is_bullish_candle  # past bar was OB, current confirms
    bear_ob = is_large_body.shift(1) & is_bullish_candle.shift(1) & is_bearish  # past bar was OB, current confirms

    # Track OB levels (use the midpoint of the OB candle)
    bull_ob_level = np.where(bull_ob, (h + l) / 2, np.nan)
    bull_ob_level = pd.Series(bull_ob_level, index=df.index).ffill(limit=30)
    bear_ob_level = np.where(bear_ob, (h + l) / 2, np.nan)
    bear_ob_level = pd.Series(bear_ob_level, index=df.index).ffill(limit=30)

    # Distance from nearest OB (normalized by ATR)
    features["dist_from_bull_ob"] = (c - bull_ob_level) / atr.replace(0, np.nan)
    features["dist_from_bear_ob"] = (bear_ob_level - c) / atr.replace(0, np.nan)

    # Is price retesting an OB? (within 0.3 ATR of OB level)
    features["at_bull_ob"] = (features["dist_from_bull_ob"].abs() < 0.3).astype(float)
    features["at_bear_ob"] = (features["dist_from_bear_ob"].abs() < 0.3).astype(float)

    # OB count in last 20 bars (market structure activity)
    features["ob_count_20"] = bull_ob.astype(float).rolling(20).sum() + bear_ob.astype(float).rolling(20).sum()

    # ================================================================
    # 24. EMA 200 + VWAP CONTEXT (trend quality features, NOT hard gates)
    # These capture what the "SMC pre-filter" suggestion wanted,
    # but as continuous features that ML can weight, not binary gates.
    # ================================================================
    ema200 = df["ema_200"]
    vwap = df["vwap"]

    # Price position relative to key levels (continuous, -1 to +1 range)
    features["price_vs_ema200"] = (c - ema200) / atr.replace(0, np.nan)
    features["price_vs_ema200_sign"] = np.sign(c - ema200)
    features["ema200_slope"] = ema200.diff(5) / atr.replace(0, np.nan)

    # VWAP + EMA200 agreement (both bullish = strong trend context)
    above_vwap = (c > vwap).astype(float)
    above_ema200 = (c > ema200).astype(float)
    features["vwap_ema200_agreement"] = above_vwap + above_ema200 - 1  # -1, 0, or +1

    # Structural alignment: price vs EMA200 vs VWAP (all aligned = strong)
    # +1 = price > VWAP > EMA200 (strong bullish), -1 = price < VWAP < EMA200 (strong bearish)
    features["structural_alignment"] = np.where(
        (c > vwap) & (vwap > ema200), 1.0,
        np.where((c < vwap) & (vwap < ema200), -1.0, 0.0)
    )

    # ================================================================
    # 25. MULTI-TIMEFRAME ALIGNMENT (when htf_df is provided)
    # Each component is a separate feature — let ML learn the weights,
    # NOT hardcoded 0.4/0.3/0.3.
    # ================================================================
    if htf_df is not None and len(htf_df) > 0:
        htf_df = compute_indicators(htf_df)
        htf_c = htf_df["close"].astype(float)
        htf_atr = htf_df["atr_14"]

        # HTF trend bias: EMA alignment on higher timeframe
        htf_ema8 = htf_df["ema_8"]
        htf_ema21 = htf_df["ema_21"]
        htf_ema50 = htf_df["ema_50"]
        htf_trend = np.where(
            (htf_ema8 > htf_ema21) & (htf_ema21 > htf_ema50), 1.0,
            np.where((htf_ema8 < htf_ema21) & (htf_ema21 < htf_ema50), -1.0, 0.0)
        )
        htf_trend_series = pd.Series(htf_trend, index=htf_df.index)

        # HTF momentum (recent returns on higher TF)
        htf_return5 = htf_c.pct_change(5)
        htf_atr_ratio = htf_df["atr_7"] / htf_atr.replace(0, np.nan)

        # Resample HTF features to match LTF index
        # shift(1) ensures we only use COMPLETED HTF bars (strictly past-looking)
        htf_trend_resampled = htf_trend_series.reindex(df.index, method="ffill").shift(1)
        htf_return_resampled = htf_return5.reindex(df.index, method="ffill").shift(1)
        htf_atr_resampled = htf_atr_ratio.reindex(df.index, method="ffill").shift(1)

        features["htf_trend_bias"] = htf_trend_resampled.fillna(0.0)
        features["htf_momentum"] = htf_return_resampled.fillna(0.0)
        features["htf_vol_ratio"] = htf_atr_resampled.fillna(1.0)

        # LTF-HTF agreement: are lower and higher timeframe aligned?
        features["tf_alignment"] = features["htf_trend_bias"] * features["trend_strength"]
    else:
        # No HTF data — fill with neutral values
        features["htf_trend_bias"] = 0.0
        features["htf_momentum"] = 0.0
        features["htf_vol_ratio"] = 1.0
        features["tf_alignment"] = 0.0

    # ════════════════════════════════════════════════════════════════
    # Phase 4.1a — 1h + 4h HTF FEATURE FUSION
    # ════════════════════════════════════════════════════════════════
    # Previously ML only saw 15m as "HTF" — but live bot uses 1h macro
    # and 4h session bias. ML was blind to the exact features that the
    # live strategy uses for its best decisions. This block adds ~14
    # features from 1h and ~14 from 4h so ML can finally learn HTF context.
    # ════════════════════════════════════════════════════════════════
    # Full list of expected HTF feature suffixes (must match _build_htf_block output)
    _HTF_SUFFIXES = (
        "trend_bias", "ema8_slope", "ema21_slope", "dist_ema50",
        "return_1", "return_3", "return_5", "atr_ratio",
        "body_ratio", "upper_wick", "lower_wick", "last_direction",
        "range_pos", "rsi",
    )

    # 1h block
    h1_block = _build_htf_block(df, htf_1h_df, prefix="h1") if htf_1h_df is not None else pd.DataFrame(index=df.index)
    for suffix in _HTF_SUFFIXES:
        col = f"h1_{suffix}"
        if col in h1_block.columns:
            features[col] = h1_block[col].values
        else:
            features[col] = 0.0

    # 4h block
    h4_block = _build_htf_block(df, htf_4h_df, prefix="h4") if htf_4h_df is not None else pd.DataFrame(index=df.index)
    for suffix in _HTF_SUFFIXES:
        col = f"h4_{suffix}"
        if col in h4_block.columns:
            features[col] = h4_block[col].values
        else:
            features[col] = 0.0

    # ── Cross-timeframe alignment features ──
    # These are the highest-signal features — they encode regime coherence
    # across 5m / 15m / 1h / 4h. Trades where ALL TFs agree should be
    # much higher probability winners.
    features["align_ltf_h1"] = features["trend_strength"] * features["h1_trend_bias"]
    features["align_ltf_h4"] = features["trend_strength"] * features["h4_trend_bias"]
    features["align_15m_h1"] = features["htf_trend_bias"] * features["h1_trend_bias"]
    features["align_h1_h4"] = features["h1_trend_bias"] * features["h4_trend_bias"]

    # Trend cascade score: how many TFs agree on direction?
    # Each TF contributes 0 or ±1 → cascade range = [-4, +4]
    def _sign(s):
        return np.sign(s).fillna(0) if hasattr(s, "fillna") else np.sign(s)
    features["trend_cascade_score"] = (
        _sign(features["trend_strength"])
        + _sign(features["htf_trend_bias"])
        + _sign(features["h1_trend_bias"])
        + _sign(features["h4_trend_bias"])
    )

    # Cascade alignment strength (0..1): how many TFs agree
    # If cascade_score is ±4 (all agree) → 1.0; if 0 (no agreement) → 0.0
    features["trend_cascade_strength"] = features["trend_cascade_score"].abs() / 4.0

    # ════════════════════════════════════════════════════════════════
    # Phase 5.0a — BTC CROSS-ASSET CONTEXT
    # ════════════════════════════════════════════════════════════════
    # Alts correlate heavily with BTC on short timeframes. Currently the
    # model is blind to BTC's state when scoring an ETH/SOL/XRP setup.
    # Adds ~12 features of BTC context that give ML the same situational
    # awareness a human trader has.
    # ════════════════════════════════════════════════════════════════
    _BTC_SUFFIXES = (
        "btc_return_1", "btc_return_3", "btc_return_5", "btc_return_12",
        "btc_atr_ratio", "btc_dist_from_vwap",
        "btc_trend_bias", "btc_ema8_slope", "btc_dist_ema50",
        "btc_range_pos", "btc_near_high_atr", "btc_near_low_atr",
        "symbol_btc_corr_60", "symbol_btc_beta_60",
    )
    btc_block = _build_btc_block(df, btc_df, symbol) if btc_df is not None else pd.DataFrame(index=df.index)
    for col in _BTC_SUFFIXES:
        if col in btc_block.columns:
            features[col] = btc_block[col].values
        else:
            features[col] = 0.0

    # ================================================================
    # Phase 5.0c: ORDERBOOK MICROSTRUCTURE FEATURES
    # 12 features from L2 snapshot — scalar values broadcast to all rows
    # (constant for this candle window since orderbook is a point-in-time snapshot)
    # ================================================================
    ob_feats = _build_orderbook_block(orderbook)
    for k, v in ob_feats.items():
        features[k] = v  # scalar → broadcast on dict→DataFrame conversion

    # ================================================================
    # 26. LIQUIDITY SWEEP / GRAB DETECTION
    # Detects stop hunts: price sweeps above equal highs (or below equal lows)
    # then reverses. The #1 high-probability SMC setup.
    # ================================================================
    # Equal highs/lows detection: 2+ recent highs/lows within 0.1% of each other
    recent_highs_max = h.rolling(20).max()
    recent_lows_min = l.rolling(20).min()

    # Count how many times high touched the rolling max (equal highs forming)
    near_high = ((recent_highs_max - h).abs() / atr.replace(0, np.nan)) < 0.15
    near_low = ((l - recent_lows_min).abs() / atr.replace(0, np.nan)) < 0.15
    features["equal_highs_count"] = near_high.astype(float).rolling(20).sum()
    features["equal_lows_count"] = near_low.astype(float).rolling(20).sum()

    # Sweep event: price exceeds recent high/low then reverses (close back inside)
    swept_high = (h > recent_highs_max.shift(1)) & (c < recent_highs_max.shift(1))
    swept_low = (l < recent_lows_min.shift(1)) & (c > recent_lows_min.shift(1))

    # Sweep size (how far past the level, normalized by ATR)
    features["sweep_high_size"] = np.where(
        swept_high, (h - recent_highs_max.shift(1)) / atr.replace(0, np.nan), 0.0
    )
    features["sweep_low_size"] = np.where(
        swept_low, (recent_lows_min.shift(1) - l) / atr.replace(0, np.nan), 0.0
    )

    # Sweep + reversal flag (sweep happened AND candle closed as reversal)
    features["sweep_high_reversal"] = (swept_high & (c < o)).astype(float)  # bearish close after high sweep
    features["sweep_low_reversal"] = (swept_low & (c > o)).astype(float)    # bullish close after low sweep

    # Recent sweep activity (any sweep in last 5 bars = setup forming)
    features["recent_sweep_bull"] = features["sweep_low_reversal"].rolling(5).max()
    features["recent_sweep_bear"] = features["sweep_high_reversal"].rolling(5).max()

    # Sweep into structural zone (FVG/OB confluence)
    # Check if sweep low touched a bull FVG (within 0.5 ATR)
    sweep_into_fvg = np.where(
        (features["sweep_low_reversal"] > 0) & (features["dist_from_bull_fvg"].abs() < 0.5),
        1.0,
        np.where(
            (features["sweep_high_reversal"] > 0) & (features["dist_from_bear_fvg"].abs() < 0.5),
            1.0, 0.0
        )
    )
    features["sweep_into_fvg"] = sweep_into_fvg

    # Post-sweep displacement: body of reversal candle / ATR
    body_vals = (c - o).abs()
    post_sweep_disp = np.where(
        (features["sweep_low_reversal"] > 0).astype(bool) | (features["sweep_high_reversal"] > 0).astype(bool),
        body_vals / atr.replace(0, np.nan),
        0.0
    )
    features["post_sweep_displacement_atr"] = post_sweep_disp

    # Reclaim strength: how far price closed back into the range after sweep
    candle_range = (h - l).replace(0, np.nan)
    reclaim_bull = (c - l) / candle_range   # for low sweeps: close relative to range
    reclaim_bear = (h - c) / candle_range   # for high sweeps: close relative to range
    features["reclaim_strength"] = np.where(
        features["sweep_low_reversal"] > 0, reclaim_bull,
        np.where(features["sweep_high_reversal"] > 0, reclaim_bear, 0.0)
    )

    # Sweep into order block (within 0.5 ATR of OB level)
    sweep_into_ob = np.where(
        (features["sweep_low_reversal"] > 0) & (features["dist_from_bull_ob"].abs() < 0.5),
        1.0,
        np.where(
            (features["sweep_high_reversal"] > 0) & (features["dist_from_bear_ob"].abs() < 0.5),
            1.0, 0.0
        )
    )
    features["sweep_into_ob"] = sweep_into_ob

    # ================================================================
    # 27. BOS / CHOCH + DISPLACEMENT
    # Break of Structure: price breaks above recent swing high (bullish BOS)
    # or below recent swing low (bearish BOS).
    # Change of Character: BOS in opposite direction of prior trend.
    # Displacement: the impulse candle that caused the break.
    # ================================================================
    # Swing highs/lows (past-only: 5-bar lookback, no future data)
    swing_high = h.rolling(5, center=False).max()
    swing_low = l.rolling(5, center=False).min()
    is_swing_high = (h == swing_high)
    is_swing_low = (l == swing_low)

    # Track most recent swing high/low levels (shift(1) to avoid using current bar)
    recent_swing_high = h.where(is_swing_high).ffill().shift(1)
    recent_swing_low = l.where(is_swing_low).ffill().shift(1)

    # BOS: current close breaks above recent swing high or below recent swing low
    bullish_bos = (c > recent_swing_high.shift(1)) & (c.shift(1) <= recent_swing_high.shift(1))
    bearish_bos = (c < recent_swing_low.shift(1)) & (c.shift(1) >= recent_swing_low.shift(1))

    features["bullish_bos"] = bullish_bos.astype(float)
    features["bearish_bos"] = bearish_bos.astype(float)

    # Displacement: body size of the BOS candle (normalized by ATR)
    body_atr = (c - o).abs() / atr.replace(0, np.nan)
    features["bos_displacement"] = np.where(
        bullish_bos.astype(bool) | bearish_bos.astype(bool), body_atr, 0.0
    )

    # Strong displacement flag (> 1.5 ATR body on BOS candle)
    features["strong_displacement"] = (features["bos_displacement"] > 1.5).astype(float)

    # CHOCH: BOS that opposes the recent trend direction
    # Prior trend approximated by EMA8 slope over last 10 bars
    ema8_slope_10 = df["ema_8"].diff(10) / atr.replace(0, np.nan)
    prior_trend_bull = ema8_slope_10 > 0.3
    prior_trend_bear = ema8_slope_10 < -0.3
    features["choch_bull"] = (bullish_bos & prior_trend_bear).astype(float)  # bullish break after downtrend
    features["choch_bear"] = (bearish_bos & prior_trend_bull).astype(float)  # bearish break after uptrend

    # BOS recency (any BOS in last 3 bars = active structure break)
    features["recent_bos_bull"] = bullish_bos.astype(float).rolling(3).max()
    features["recent_bos_bear"] = bearish_bos.astype(float).rolling(3).max()

    # BOS + FVG confluence (BOS that also created an FVG = strongest signal)
    _any_bos = (bullish_bos.astype(bool)) | (bearish_bos.astype(bool))
    _any_fvg = (bull_fvg_exists.astype(bool)) | (bear_fvg_exists.astype(bool))
    features["bos_with_fvg"] = np.where(_any_bos & _any_fvg, 1.0, 0.0)

    # Break distance from structure level (normalized by ATR)
    # For bullish BOS: how far past the recent swing high; for bearish: past swing low
    break_dist_bull = np.where(
        bullish_bos,
        (c - recent_swing_high.shift(1)) / atr.replace(0, np.nan),
        0.0
    )
    break_dist_bear = np.where(
        bearish_bos,
        (recent_swing_low.shift(1) - c) / atr.replace(0, np.nan),
        0.0
    )
    features["break_distance_atr"] = np.maximum(
        np.nan_to_num(break_dist_bull, 0) + np.nan_to_num(break_dist_bear, 0), 0
    )

    # Retest flag: did price come back to test a recently broken level?
    # Uses PAST bars only — checks if any of the last 3 bars retested a level
    retest_bull = pd.Series(0.0, index=df.index)
    retest_bear = pd.Series(0.0, index=df.index)
    for lb in range(1, 4):
        past_low = l.shift(lb)   # PAST bars' low tested the level
        past_high = h.shift(lb)  # PAST bars' high tested the level
        # Bull retest: BOS happened lb bars ago AND current price is near the broken level
        retest_bull = retest_bull.astype(bool) | (
            bullish_bos.shift(lb).fillna(False).astype(bool) & ((l - recent_swing_high.shift(lb + 1)).abs() < atr * 0.3)
        )
        # Bear retest: BOS happened lb bars ago AND current price is near the broken level
        retest_bear = retest_bear.astype(bool) | (
            bearish_bos.shift(lb).fillna(False).astype(bool) & ((h - recent_swing_low.shift(lb + 1)).abs() < atr * 0.3)
        )
    features["retest_flag"] = (retest_bull.astype(float) + retest_bear.astype(float)).clip(upper=1.0)

    # Impulse decay: how quickly displacement fades over next few bars
    # Compare body/ATR of current bar vs the BOS bar's displacement
    bos_disp = features["bos_displacement"]
    current_body_atr = body_vals / atr.replace(0, np.nan)
    impulse_decay_raw = np.where(
        bos_disp > 0.5,
        1.0 - (current_body_atr / bos_disp.replace(0, np.nan)),
        0.0
    )
    features["bos_impulse_decay"] = np.clip(np.nan_to_num(impulse_decay_raw, 0), 0, 1.0)

    # HTF alignment with BOS direction
    if "htf_trend_bias" in features:
        htf_bias = features["htf_trend_bias"]
        htf_bos_align = np.where(
            bullish_bos.astype(bool) & (htf_bias > 0), 1.0,
            np.where(
                bearish_bos.astype(bool) & (htf_bias < 0), 1.0,
                np.where(
                    (features["choch_bull"] > 0).astype(bool) | (features["choch_bear"] > 0).astype(bool),
                    -0.5, 0.0
                )
            )
        )
        features["htf_bos_alignment"] = htf_bos_align
    else:
        features["htf_bos_alignment"] = 0.0

    # ================================================================
    # 28. KILLZONE / SESSION BOOST FEATURES
    # High-probability trading windows. NOT hard blocks — continuous
    # boost/penalty for ML to weight.
    # ================================================================
    if hasattr(df.index, 'hour'):
        hour = df.index.hour
        minute = df.index.minute if hasattr(df.index, 'minute') else 0
        hour_frac = hour + minute / 60.0

        # London killzone: 07:00-10:00 UTC (peak volatility EU session)
        features["kz_london"] = ((hour_frac >= 7.0) & (hour_frac < 10.0)).astype(float)
        # NY killzone: 13:00-16:00 UTC (NY open + overlap)
        features["kz_newyork"] = ((hour_frac >= 13.0) & (hour_frac < 16.0)).astype(float)
        # Silver Bullet windows: 14:00-15:00 UTC (NY AM), 19:00-20:00 UTC (NY PM)
        features["kz_silver_bullet"] = (
            ((hour_frac >= 14.0) & (hour_frac < 15.0)) |
            ((hour_frac >= 19.0) & (hour_frac < 20.0))
        ).astype(float)
        # Dead zone: 00:00-06:00 UTC (low liquidity, noise)
        features["kz_dead_zone"] = ((hour_frac >= 0.0) & (hour_frac < 6.0)).astype(float)
        # Any killzone active (composite)
        features["kz_active"] = (
            features["kz_london"] + features["kz_newyork"] + features["kz_silver_bullet"]
        ).clip(upper=1.0)
    else:
        features["kz_london"] = 0.0
        features["kz_newyork"] = 0.0
        features["kz_silver_bullet"] = 0.0
        features["kz_dead_zone"] = 0.0
        features["kz_active"] = 0.0

    # ================================================================
    # 29. CONFLUENCE SCORE (multi-signal alignment)
    # Counts how many structural signals align. Higher = better candidate.
    # NOT a hard filter — ML learns the optimal threshold.
    # ================================================================
    # Bullish confluence components
    bull_signals = (
        features["recent_sweep_bull"] +                         # liquidity sweep
        features["recent_bos_bull"] +                           # break of structure
        (features["at_bull_ob"] if "at_bull_ob" in features else 0) +  # at order block
        (features["fvg_imbalance"] > 0.3).astype(float) +      # FVG imbalance bullish
        (features["structural_alignment"] > 0).astype(float) +  # VWAP+EMA200 aligned
        features.get("kz_active", 0)                            # in killzone
    )
    bear_signals = (
        features["recent_sweep_bear"] +
        features["recent_bos_bear"] +
        (features["at_bear_ob"] if "at_bear_ob" in features else 0) +
        (features["fvg_imbalance"] < -0.3).astype(float) +
        (features["structural_alignment"] < 0).astype(float) +
        features.get("kz_active", 0)
    )
    features["confluence_bull"] = bull_signals
    features["confluence_bear"] = bear_signals
    features["confluence_max"] = pd.concat([bull_signals, bear_signals], axis=1).max(axis=1)

    # ================================================================
    # Section 30: Funding Rate Proxy
    # ================================================================
    # Funding rate approximation: premium/discount of close vs VWAP
    # Positive = longs paying shorts, negative = shorts paying longs
    if "vwap" in df.columns:
        _closes_arr = df["close"].values  # Phase 4.1a: fix latent NameError
        vwap = df["vwap"].values
        funding_proxy = (_closes_arr - vwap) / vwap * 100  # premium in %
        features["funding_proxy"] = funding_proxy
        features["funding_proxy_ma5"] = pd.Series(funding_proxy).rolling(5).mean().values
        features["funding_positive"] = (funding_proxy > 0).astype(float)  # longs paying
    else:
        features["funding_proxy"] = 0
        features["funding_proxy_ma5"] = 0
        features["funding_positive"] = 0

    # ================================================================
    # Section 31: Cross-Pair Correlation (BTC Dominance Proxy)
    # ================================================================
    # BTC dominance proxy: if BTC is moving stronger than the pair, alts follow
    # Approximated by relative return: pair_return vs its own EMA momentum
    ret_5 = features.get("return_5", pd.Series(0, index=df.index))
    ema_slope = features.get("ema_slope_8", pd.Series(0, index=df.index))
    if isinstance(ret_5, pd.Series) and isinstance(ema_slope, pd.Series):
        # Momentum divergence: return moving one way, EMA slope the other = potential reversal
        features["momentum_ema_divergence"] = ret_5 - ema_slope * 10
        # Trend acceleration: is the move accelerating or decelerating?
        ret_10 = features.get("return_10", pd.Series(0, index=df.index))
        if isinstance(ret_10, pd.Series):
            features["trend_acceleration"] = ret_5 - ret_10 * 0.5
    else:
        features["momentum_ema_divergence"] = 0
        features["trend_acceleration"] = 0

    # Volatility regime (high/low vol state)
    atr_ratio = features.get("atr_ratio", pd.Series(1, index=df.index))
    if isinstance(atr_ratio, pd.Series):
        features["vol_regime_high"] = (atr_ratio > 1.5).astype(float)
        features["vol_regime_low"] = (atr_ratio < 0.7).astype(float)
    else:
        features["vol_regime_high"] = 0
        features["vol_regime_low"] = 0

    # ================================================================
    # Section 32: Price Level Context
    # ================================================================
    # Round number proximity (psychological S/R levels)
    # Phase 4.1a: fix latent NameError — use df["close"] instead of undefined 'closes'
    _closes_series = df["close"]
    _last_close = float(_closes_series.iloc[-1]) if len(_closes_series) > 0 else 0
    if _last_close > 100:  # BTC/ETH
        round_level = round(_last_close / 1000) * 1000
        features["dist_from_round_number"] = (abs(_closes_series - round_level) / _closes_series * 100).values
    elif _last_close > 1:  # SOL/XRP
        round_level = round(_last_close / 10) * 10
        features["dist_from_round_number"] = (abs(_closes_series - round_level) / _closes_series * 100).values
    else:
        features["dist_from_round_number"] = 0

    # ================================================================
    # CONVERT dict → DataFrame (one-shot, no fragmentation)
    # ================================================================
    features = pd.DataFrame(features, index=df.index)

    # ================================================================
    # CLEANUP: Replace NaN/inf, mark warmup period
    # ================================================================
    features.replace([np.inf, -np.inf], np.nan, inplace=True)

    # Add warmup flag — first 200 rows have unreliable indicator values
    WARMUP_ROWS = 200
    features["is_warmup"] = 0.0
    features.iloc[:WARMUP_ROWS, features.columns.get_loc("is_warmup")] = 1.0

    # Fill remaining NaN with 0.0 (after warmup flag is set)
    features.fillna(0.0, inplace=True)

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
                     threshold_r: float = 0.2,
                     fee_pct: float = 0.00059) -> pd.Series:
    """Build MFE-based label: will fee-adjusted MFE exceed threshold_r?

    Instead of asking "did the whole trade work?", asks:
    "will price move at least +threshold_r in my direction within N bars,
    AFTER accounting for round-trip trading fees?"

    Fee adjustment: subtracts round-trip fees (2 × fee_pct × entry_price)
    from MFE before comparing to threshold. This prevents labeling
    trades as "good" that would be negative EV after costs.

    Parameters
    ----------
    df : DataFrame with OHLCV + atr_14
    side : "long" or "short"
    max_bars : bars to look forward (default 30)
    threshold_r : MFE threshold in R-multiples (default 0.2)
    fee_pct : one-side fee as decimal (default 0.00055 = 0.055% taker)

    Returns
    -------
    Series of 0/1 labels (1 = fee-adjusted MFE exceeded threshold)
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

        # Fee-adjusted threshold: MFE must exceed threshold + round-trip fees
        fee_cost = 2 * fee_pct * entry
        threshold_price = threshold_r * risk + fee_cost

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
