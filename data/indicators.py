"""
Technical indicator calculations for the crypto trading bot.

All functions accept a pandas DataFrame with OHLCV columns
(open, high, low, close, volume) and return Series or DataFrames.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Trend indicators
# ---------------------------------------------------------------------------

def calc_ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    """Exponential Moving Average."""
    return df[column].ewm(span=period, adjust=False).mean()


def calc_sma(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    """Simple Moving Average."""
    return df[column].rolling(window=period).mean()


def calc_ema_slope(ema: pd.Series, lookback: int = 3) -> pd.Series:
    """Normalised slope of an EMA over *lookback* bars (percent change)."""
    return ema.pct_change(periods=lookback) * 100


# ---------------------------------------------------------------------------
# Momentum indicators
# ---------------------------------------------------------------------------

def calc_rsi(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    """Relative Strength Index using Wilder smoothing."""
    delta = df[column].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def calc_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    column: str = "close",
) -> pd.DataFrame:
    """MACD line, signal line, and histogram."""
    ema_fast = df[column].ewm(span=fast, adjust=False).mean()
    ema_slow = df[column].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "macd_signal": signal_line, "macd_hist": histogram},
        index=df.index,
    )


def calc_mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Money Flow Index."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    raw_money_flow = typical_price * df["volume"]
    delta = typical_price.diff()

    pos_flow = raw_money_flow.where(delta > 0, 0.0).rolling(window=period).sum()
    neg_flow = raw_money_flow.where(delta <= 0, 0.0).rolling(window=period).sum()

    money_ratio = pos_flow / neg_flow.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + money_ratio))


# ---------------------------------------------------------------------------
# Volatility indicators
# ---------------------------------------------------------------------------

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def calc_atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR as a percentage of closing price."""
    return (calc_atr(df, period) / df["close"]) * 100.0


def calc_bollinger_bands(
    df: pd.DataFrame,
    period: int = 20,
    std_dev: float = 2.0,
    column: str = "close",
) -> pd.DataFrame:
    """Bollinger Bands: upper, middle, lower, bandwidth, percent_b."""
    middle = df[column].rolling(window=period).mean()
    std = df[column].rolling(window=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    bandwidth = ((upper - lower) / middle) * 100.0
    percent_b = (df[column] - lower) / (upper - lower).replace(0, np.nan)
    return pd.DataFrame(
        {
            "bb_upper": upper,
            "bb_middle": middle,
            "bb_lower": lower,
            "bb_bandwidth": bandwidth,
            "bb_pct_b": percent_b,
        },
        index=df.index,
    )


def calc_supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """Supertrend indicator returning line value and direction (+1 up, -1 down)."""
    atr = calc_atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0

    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    supertrend = pd.Series(np.nan, index=df.index)
    direction = pd.Series(1, index=df.index, dtype=int)

    for i in range(1, len(df)):
        prev_upper = upper_band.iloc[i - 1] if not np.isnan(upper_band.iloc[i - 1]) else upper_band.iloc[i]
        prev_lower = lower_band.iloc[i - 1] if not np.isnan(lower_band.iloc[i - 1]) else lower_band.iloc[i]

        # Adjust bands to prevent flipping back too soon
        if lower_band.iloc[i] < prev_lower and df["close"].iloc[i - 1] > prev_lower:
            lower_band.iloc[i] = prev_lower
        if upper_band.iloc[i] > prev_upper and df["close"].iloc[i - 1] < prev_upper:
            upper_band.iloc[i] = prev_upper

        prev_st = supertrend.iloc[i - 1]
        if np.isnan(prev_st):
            prev_st = upper_band.iloc[i]

        if prev_st == prev_upper:
            if df["close"].iloc[i] > upper_band.iloc[i]:
                supertrend.iloc[i] = lower_band.iloc[i]
                direction.iloc[i] = 1
            else:
                supertrend.iloc[i] = upper_band.iloc[i]
                direction.iloc[i] = -1
        else:
            if df["close"].iloc[i] < lower_band.iloc[i]:
                supertrend.iloc[i] = upper_band.iloc[i]
                direction.iloc[i] = -1
            else:
                supertrend.iloc[i] = lower_band.iloc[i]
                direction.iloc[i] = 1

    return pd.DataFrame(
        {"supertrend": supertrend, "supertrend_dir": direction},
        index=df.index,
    )


# ---------------------------------------------------------------------------
# Volume indicators
# ---------------------------------------------------------------------------

def calc_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-Weighted Average Price (intraday, reset each session).

    For simplicity this computes a cumulative VWAP across the entire
    DataFrame.  In production, pass a single session's worth of data or
    add session boundaries.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum()
    cum_tp_vol = (typical * df["volume"]).cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def calc_volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Simple moving average of volume."""
    return df["volume"].rolling(window=period).mean()


def calc_volume_spike(df: pd.DataFrame, period: int = 20, multiplier: float = 2.0) -> pd.Series:
    """Boolean series: True where volume exceeds *multiplier* x SMA."""
    vol_sma = calc_volume_sma(df, period)
    return df["volume"] > (vol_sma * multiplier)


def calc_relative_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Current volume divided by its SMA (>1 means above average)."""
    vol_sma = calc_volume_sma(df, period)
    return df["volume"] / vol_sma.replace(0, np.nan)


# ---------------------------------------------------------------------------
# Trend strength
# ---------------------------------------------------------------------------

def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index with +DI / -DI."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)

    # Zero out whichever is smaller
    plus_dm[plus_dm < minus_dm] = 0.0
    minus_dm[minus_dm < plus_dm] = 0.0

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    alpha = 1.0 / period
    atr_smooth = tr.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean() / atr_smooth.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean() / atr_smooth.replace(0, np.nan)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

    return pd.DataFrame(
        {"adx": adx, "plus_di": plus_di, "minus_di": minus_di},
        index=df.index,
    )


# ---------------------------------------------------------------------------
# Convenience: compute all indicators at once
# ---------------------------------------------------------------------------

def calc_all_indicators(
    df: pd.DataFrame,
    *,
    ema_periods: tuple[int, ...] = (9, 21, 50, 200),
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    atr_period: int = 14,
    bb_period: int = 20,
    bb_std: float = 2.0,
    supertrend_period: int = 10,
    supertrend_mult: float = 3.0,
    adx_period: int = 14,
    mfi_period: int = 14,
    vol_sma_period: int = 20,
) -> pd.DataFrame:
    """Compute a full indicator suite and merge into the DataFrame."""
    result = df.copy()

    # EMAs
    for p in ema_periods:
        result[f"ema_{p}"] = calc_ema(df, p)

    # RSI
    result["rsi"] = calc_rsi(df, rsi_period)

    # MACD
    macd_df = calc_macd(df, macd_fast, macd_slow, macd_signal)
    result = pd.concat([result, macd_df], axis=1)

    # ATR
    result["atr"] = calc_atr(df, atr_period)
    result["atr_pct"] = calc_atr_pct(df, atr_period)

    # Bollinger Bands
    bb = calc_bollinger_bands(df, bb_period, bb_std)
    result = pd.concat([result, bb], axis=1)

    # Supertrend
    st = calc_supertrend(df, supertrend_period, supertrend_mult)
    result = pd.concat([result, st], axis=1)

    # ADX
    adx_df = calc_adx(df, adx_period)
    result = pd.concat([result, adx_df], axis=1)

    # MFI
    result["mfi"] = calc_mfi(df, mfi_period)

    # VWAP
    result["vwap"] = calc_vwap(df)

    # Volume
    result["volume_sma"] = calc_volume_sma(df, vol_sma_period)
    result["volume_spike"] = calc_volume_spike(df, vol_sma_period)
    result["relative_volume"] = calc_relative_volume(df, vol_sma_period)

    return result


# ---------------------------------------------------------------------------
# Fibonacci Retracement
# ---------------------------------------------------------------------------

def calc_fib_retracement(
    df: pd.DataFrame,
    lookback: int = 50,
) -> dict:
    """Find the most recent significant swing and return Fibonacci levels.

    Scans the last *lookback* bars for the swing high/low, determines trend
    direction, and calculates 23.6%, 38.2%, 50%, 61.8%, 78.6% retracement
    levels.

    Returns
    -------
    dict with keys:
        trend : str  ("up" or "down")
        swing_high, swing_low : float
        levels : dict[str, float]  e.g. {"0.236": 74500.0, "0.382": ...}
        current_level : str | None  nearest Fib level the price is at
        at_fib : bool  True if price is within 0.15% of a key level
    """
    if len(df) < lookback:
        lookback = len(df)
    if lookback < 10:
        return {"trend": "unknown", "levels": {}, "at_fib": False}

    window = df.iloc[-lookback:]
    high_idx = window["high"].idxmax()
    low_idx = window["low"].idxmin()
    swing_high = float(window["high"].max())
    swing_low = float(window["low"].min())
    close = float(df["close"].iloc[-1])

    if swing_high == swing_low:
        return {"trend": "unknown", "levels": {}, "at_fib": False}

    # Determine trend: if high came AFTER low → uptrend (retracing down)
    # Use positional index for comparison
    high_pos = window.index.get_loc(high_idx)
    low_pos = window.index.get_loc(low_idx)
    trend = "up" if high_pos > low_pos else "down"

    diff = swing_high - swing_low
    fib_ratios = {
        "0.236": 0.236,
        "0.382": 0.382,
        "0.500": 0.500,
        "0.618": 0.618,
        "0.786": 0.786,
    }

    levels = {}
    if trend == "up":
        # Uptrend retracement: levels measured DOWN from swing_high
        for name, ratio in fib_ratios.items():
            levels[name] = swing_high - diff * ratio
    else:
        # Downtrend retracement: levels measured UP from swing_low
        for name, ratio in fib_ratios.items():
            levels[name] = swing_low + diff * ratio

    # Check if current price is near a key Fib level
    threshold = close * 0.0015  # within 0.15% of price
    nearest_level = None
    at_fib = False
    min_dist = float("inf")
    for name, level in levels.items():
        dist = abs(close - level)
        if dist < min_dist:
            min_dist = dist
            nearest_level = name
        if dist <= threshold:
            at_fib = True

    return {
        "trend": trend,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "levels": levels,
        "nearest_level": nearest_level,
        "at_fib": at_fib,
        "fib_distance_pct": (min_dist / close * 100) if close > 0 else 999,
    }


# ---------------------------------------------------------------------------
# Change of Character (CHOCH) — Market Structure Break
# ---------------------------------------------------------------------------

def detect_choch(
    df: pd.DataFrame,
    lookback: int = 30,
    min_swing_pct: float = 0.002,
) -> dict:
    """Detect Change of Character (CHOCH) in market structure.

    CHOCH occurs when price breaks a key structure level:
    - Bullish CHOCH: downtrend making lower lows, then breaks above a
      previous lower high → structure shift to bullish
    - Bearish CHOCH: uptrend making higher highs, then breaks below a
      previous higher low → structure shift to bearish

    Parameters
    ----------
    df : DataFrame with OHLCV columns
    lookback : bars to scan for swing points
    min_swing_pct : minimum swing size (as fraction of price) to qualify

    Returns
    -------
    dict with keys:
        choch_detected : bool
        direction : str  "bullish" | "bearish" | None
        break_level : float | None  the structure level that was broken
        strength : int  0-100 quality of the CHOCH signal
        bars_ago : int  how many bars ago the CHOCH occurred
    """
    result = {
        "choch_detected": False,
        "direction": None,
        "break_level": None,
        "strength": 0,
        "bars_ago": 0,
    }

    n = min(lookback, len(df) - 2)
    if n < 10:
        return result

    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    price = closes[-1]

    # --- Find swing highs and swing lows (3-bar pivot) ---
    swing_highs = []  # (index, price)
    swing_lows = []   # (index, price)

    start = len(df) - n
    for i in range(start + 1, len(df) - 1):
        # Swing high: higher than both neighbours
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_pct = (highs[i] - min(lows[i - 1], lows[i + 1])) / price
            if swing_pct >= min_swing_pct:
                swing_highs.append((i, float(highs[i])))

        # Swing low: lower than both neighbours
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swing_pct = (max(highs[i - 1], highs[i + 1]) - lows[i]) / price
            if swing_pct >= min_swing_pct:
                swing_lows.append((i, float(lows[i])))

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return result

    # --- Detect structure: series of HH/HL (uptrend) or LH/LL (downtrend) ---

    # Check last few swing points for pattern
    recent_highs = swing_highs[-3:]  # last 3 swing highs
    recent_lows = swing_lows[-3:]    # last 3 swing lows

    # Uptrend: higher highs AND higher lows
    making_hh = len(recent_highs) >= 2 and recent_highs[-1][1] > recent_highs[-2][1]
    making_hl = len(recent_lows) >= 2 and recent_lows[-1][1] > recent_lows[-2][1]
    is_uptrend = making_hh and making_hl

    # Downtrend: lower highs AND lower lows
    making_lh = len(recent_highs) >= 2 and recent_highs[-1][1] < recent_highs[-2][1]
    making_ll = len(recent_lows) >= 2 and recent_lows[-1][1] < recent_lows[-2][1]
    is_downtrend = making_lh and making_ll

    # --- Bearish CHOCH: was in uptrend, now price breaks below last higher low ---
    if is_uptrend and len(recent_lows) >= 2:
        last_hl = recent_lows[-1][1]  # most recent higher low
        # Current price broke below the higher low
        if price < last_hl:
            bars_since = len(df) - 1 - recent_lows[-1][0]
            strength = 50
            # Stronger if the break is decisive (full candle body below)
            if closes[-1] < last_hl and max(closes[-1], df["open"].iloc[-1]) < last_hl:
                strength += 20  # body fully below
            # Stronger if volume confirms
            vol_sma = df["volume"].iloc[-20:].mean() if len(df) >= 20 else df["volume"].mean()
            if vol_sma > 0 and df["volume"].iloc[-1] > vol_sma * 1.3:
                strength += 15
            # Stronger if multiple HH/HL preceded (established trend)
            if len(swing_highs) >= 3 and swing_highs[-1][1] > swing_highs[-2][1] > swing_highs[-3][1]:
                strength += 15

            result = {
                "choch_detected": True,
                "direction": "bearish",
                "break_level": last_hl,
                "strength": min(strength, 100),
                "bars_ago": bars_since,
            }

    # --- Bullish CHOCH: was in downtrend, now price breaks above last lower high ---
    if is_downtrend and len(recent_highs) >= 2:
        last_lh = recent_highs[-1][1]  # most recent lower high
        if price > last_lh:
            bars_since = len(df) - 1 - recent_highs[-1][0]
            strength = 50
            if closes[-1] > last_lh and min(closes[-1], df["open"].iloc[-1]) > last_lh:
                strength += 20
            vol_sma = df["volume"].iloc[-20:].mean() if len(df) >= 20 else df["volume"].mean()
            if vol_sma > 0 and df["volume"].iloc[-1] > vol_sma * 1.3:
                strength += 15
            if len(swing_lows) >= 3 and swing_lows[-1][1] < swing_lows[-2][1] < swing_lows[-3][1]:
                strength += 15

            result = {
                "choch_detected": True,
                "direction": "bullish",
                "break_level": last_lh,
                "strength": min(strength, 100),
                "bars_ago": bars_since,
            }

    return result
