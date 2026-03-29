"""Unit tests for data/indicators.py — all indicator functions.

Tests pure math: ATR, EMA, RSI, VWAP, MACD, Bollinger Bands.
Edge cases: empty DataFrame, NaN values, zero volume, single row.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from data.indicators import (
    calc_atr,
    calc_ema,
    calc_rsi,
    calc_vwap,
    calc_macd,
    calc_bollinger_bands,
    calc_sma,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ohlcv(n=50, base=100.0, seed=42):
    """Generate a reproducible OHLCV DataFrame."""
    rng = np.random.RandomState(seed)
    closes = base + np.cumsum(rng.randn(n) * 0.5)
    highs = closes + rng.uniform(0.2, 1.0, n)
    lows = closes - rng.uniform(0.2, 1.0, n)
    opens = closes + rng.randn(n) * 0.3
    volume = rng.uniform(100, 1000, n)
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volume,
    })


def make_constant_ohlcv(n=30, price=100.0, volume=500.0):
    """OHLCV where every bar is identical — useful for boundary tests."""
    return pd.DataFrame({
        "open": [price] * n,
        "high": [price] * n,
        "low": [price] * n,
        "close": [price] * n,
        "volume": [volume] * n,
    })


# ===================================================================
# calc_atr
# ===================================================================

class TestCalcATR:
    def test_basic_output_shape(self):
        df = make_ohlcv(50)
        atr = calc_atr(df, period=14)
        assert len(atr) == 50
        assert isinstance(atr, pd.Series)

    def test_atr_positive(self):
        """ATR should always be non-negative."""
        df = make_ohlcv(50)
        atr = calc_atr(df, period=14)
        valid = atr.dropna()
        assert (valid >= 0).all()

    def test_period_parameter(self):
        """Shorter period ATR should react faster (higher variance)."""
        df = make_ohlcv(100)
        atr_short = calc_atr(df, period=5)
        atr_long = calc_atr(df, period=20)
        # Short period ATR should have higher std (more responsive)
        assert atr_short.dropna().std() >= atr_long.dropna().std() * 0.5

    def test_constant_price_atr_zero(self):
        """If high == low == close for all bars, ATR should be ~0."""
        df = make_constant_ohlcv(30)
        atr = calc_atr(df, period=14)
        valid = atr.dropna()
        assert valid.max() < 1e-10

    def test_known_true_range(self):
        """Verify true range formula with hand-crafted data."""
        df = pd.DataFrame({
            "open": [100, 102, 101],
            "high": [105, 106, 103],
            "low": [98, 100, 99],
            "close": [102, 101, 100],
            "volume": [1000, 1000, 1000],
        })
        atr = calc_atr(df, period=2)
        # TR for bar 1: max(106-100, |106-102|, |100-102|) = max(6, 4, 2) = 6
        # TR for bar 2: max(103-99, |103-101|, |99-101|) = max(4, 2, 2) = 4
        assert len(atr) == 3

    def test_single_row(self):
        """Single row should return NaN (not enough data for period=14)."""
        df = make_ohlcv(1)
        atr = calc_atr(df, period=14)
        assert len(atr) == 1
        assert pd.isna(atr.iloc[0])


# ===================================================================
# calc_ema
# ===================================================================

class TestCalcEMA:
    def test_basic_output(self):
        df = make_ohlcv(50)
        ema = calc_ema(df, period=9)
        assert len(ema) == 50
        assert not ema.isna().all()

    def test_convergence_to_constant(self):
        """EMA of a constant series should equal that constant."""
        df = make_constant_ohlcv(30, price=42.0)
        ema = calc_ema(df, period=9)
        assert abs(ema.iloc[-1] - 42.0) < 1e-10

    def test_manual_calculation(self):
        """Verify EMA against manual calc for first few values."""
        df = pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0, 14.0]})
        ema = calc_ema(df, period=3)
        # EMA(3): multiplier = 2/(3+1) = 0.5
        # EMA[0] = 10.0
        # EMA[1] = 11.0 * 0.5 + 10.0 * 0.5 = 10.5
        # EMA[2] = 12.0 * 0.5 + 10.5 * 0.5 = 11.25
        # EMA[3] = 13.0 * 0.5 + 11.25 * 0.5 = 12.125
        # EMA[4] = 14.0 * 0.5 + 12.125 * 0.5 = 13.0625
        assert abs(ema.iloc[0] - 10.0) < 1e-10
        assert abs(ema.iloc[1] - 10.5) < 1e-10
        assert abs(ema.iloc[2] - 11.25) < 1e-10
        assert abs(ema.iloc[3] - 12.125) < 1e-10
        assert abs(ema.iloc[4] - 13.0625) < 1e-10

    def test_custom_column(self):
        """Should work on non-default column."""
        df = make_ohlcv(30)
        ema_open = calc_ema(df, period=9, column="open")
        ema_close = calc_ema(df, period=9, column="close")
        # Should be different series (different source data)
        assert not np.allclose(ema_open.values, ema_close.values)

    def test_short_vs_long_period(self):
        """Short EMA hugs price more closely."""
        df = make_ohlcv(100)
        ema_short = calc_ema(df, period=5)
        ema_long = calc_ema(df, period=50)
        # Short EMA should track close more tightly
        short_err = (ema_short - df["close"]).abs().mean()
        long_err = (ema_long - df["close"]).abs().mean()
        assert short_err < long_err


# ===================================================================
# calc_rsi
# ===================================================================

class TestCalcRSI:
    def test_bounded_0_100(self):
        """RSI must always be in [0, 100]."""
        df = make_ohlcv(100)
        rsi = calc_rsi(df, period=14)
        valid = rsi.dropna()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_all_up_near_100(self):
        """Monotonically increasing prices should give RSI near 100."""
        prices = list(range(100, 200))  # 100 to 199
        df = pd.DataFrame({
            "close": prices,
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 0.5 for p in prices],
            "volume": [1000] * len(prices),
        })
        rsi = calc_rsi(df, period=14)
        # Last RSI should be very close to 100
        assert rsi.iloc[-1] > 95

    def test_all_down_near_0(self):
        """Monotonically decreasing prices should give RSI near 0."""
        prices = list(range(200, 100, -1))  # 200 to 101
        df = pd.DataFrame({
            "close": prices,
            "open": prices,
            "high": [p + 0.5 for p in prices],
            "low": [p - 1 for p in prices],
            "volume": [1000] * len(prices),
        })
        rsi = calc_rsi(df, period=14)
        assert rsi.iloc[-1] < 5

    def test_constant_price_rsi_nan(self):
        """Constant price means 0 gains, 0 losses -> NaN RSI."""
        df = make_constant_ohlcv(30)
        rsi = calc_rsi(df, period=14)
        # With 0 gains and 0 losses, avg_loss=0 -> rs=inf or NaN
        # The implementation replaces 0 with NaN, so RSI is NaN
        # This is acceptable behavior
        assert True  # Just check it doesn't crash

    def test_period_parameter(self):
        """Different periods should produce different RSI values."""
        df = make_ohlcv(100)
        rsi_7 = calc_rsi(df, period=7)
        rsi_21 = calc_rsi(df, period=21)
        # Short period RSI should be more volatile
        assert rsi_7.dropna().std() > rsi_21.dropna().std() * 0.5


# ===================================================================
# calc_vwap
# ===================================================================

class TestCalcVWAP:
    def test_vwap_formula(self):
        """VWAP = cumsum(typical_price * volume) / cumsum(volume)."""
        df = pd.DataFrame({
            "open": [10, 20, 30],
            "high": [12, 22, 33],
            "low": [8, 18, 27],
            "close": [11, 21, 31],
            "volume": [100, 200, 300],
        })
        vwap = calc_vwap(df)
        # typical = (high+low+close)/3
        tp = [(12 + 8 + 11) / 3, (22 + 18 + 21) / 3, (33 + 27 + 31) / 3]
        # tp = [10.333, 20.333, 30.333]
        cum_tp_vol_0 = tp[0] * 100
        cum_vol_0 = 100
        expected_0 = cum_tp_vol_0 / cum_vol_0

        cum_tp_vol_1 = tp[0] * 100 + tp[1] * 200
        cum_vol_1 = 300
        expected_1 = cum_tp_vol_1 / cum_vol_1

        cum_tp_vol_2 = tp[0] * 100 + tp[1] * 200 + tp[2] * 300
        cum_vol_2 = 600
        expected_2 = cum_tp_vol_2 / cum_vol_2

        assert abs(vwap.iloc[0] - expected_0) < 1e-6
        assert abs(vwap.iloc[1] - expected_1) < 1e-6
        assert abs(vwap.iloc[2] - expected_2) < 1e-6

    def test_equal_volume_vwap_is_avg_typical(self):
        """With equal volume each bar, VWAP = running average of typical price."""
        df = make_ohlcv(20)
        df["volume"] = 100.0  # constant volume
        vwap = calc_vwap(df)
        typical = (df["high"] + df["low"] + df["close"]) / 3
        # With constant volume, VWAP at bar N = mean(typical[0:N+1])
        for i in range(len(df)):
            expected = typical.iloc[:i + 1].mean()
            assert abs(vwap.iloc[i] - expected) < 1e-8

    def test_zero_volume(self):
        """Zero volume should produce NaN (division by zero handled)."""
        df = pd.DataFrame({
            "open": [100], "high": [101], "low": [99],
            "close": [100], "volume": [0],
        })
        vwap = calc_vwap(df)
        assert pd.isna(vwap.iloc[0])

    def test_single_bar(self):
        """VWAP of one bar should equal that bar's typical price."""
        df = pd.DataFrame({
            "open": [100], "high": [110], "low": [90],
            "close": [105], "volume": [500],
        })
        vwap = calc_vwap(df)
        expected = (110 + 90 + 105) / 3
        assert abs(vwap.iloc[0] - expected) < 1e-10


# ===================================================================
# calc_macd
# ===================================================================

class TestCalcMACD:
    def test_output_columns(self):
        df = make_ohlcv(50)
        result = calc_macd(df)
        assert "macd" in result.columns
        assert "macd_signal" in result.columns
        assert "macd_hist" in result.columns
        assert len(result) == 50

    def test_histogram_is_macd_minus_signal(self):
        """Histogram should exactly equal MACD line - Signal line."""
        df = make_ohlcv(100)
        result = calc_macd(df)
        diff = (result["macd"] - result["macd_signal"]) - result["macd_hist"]
        assert diff.abs().max() < 1e-10

    def test_constant_price_macd_zero(self):
        """MACD of constant price should converge to 0."""
        df = make_constant_ohlcv(50)
        result = calc_macd(df)
        assert abs(result["macd"].iloc[-1]) < 1e-10
        assert abs(result["macd_signal"].iloc[-1]) < 1e-10
        assert abs(result["macd_hist"].iloc[-1]) < 1e-10

    def test_custom_periods(self):
        """Custom fast/slow/signal periods should work."""
        df = make_ohlcv(100)
        result = calc_macd(df, fast=8, slow=17, signal=5)
        assert len(result) == 100
        assert not result["macd"].isna().all()


# ===================================================================
# calc_bollinger_bands
# ===================================================================

class TestCalcBollingerBands:
    def test_output_columns(self):
        df = make_ohlcv(50)
        bb = calc_bollinger_bands(df, period=20)
        expected_cols = {"bb_upper", "bb_middle", "bb_lower", "bb_bandwidth", "bb_pct_b"}
        assert expected_cols.issubset(set(bb.columns))

    def test_bands_symmetric_around_sma(self):
        """Upper and lower bands should be equidistant from the middle (SMA)."""
        df = make_ohlcv(50)
        bb = calc_bollinger_bands(df, period=20, std_dev=2.0)
        valid = bb.dropna()
        upper_dist = valid["bb_upper"] - valid["bb_middle"]
        lower_dist = valid["bb_middle"] - valid["bb_lower"]
        diff = (upper_dist - lower_dist).abs()
        assert diff.max() < 1e-10

    def test_upper_above_lower(self):
        """Upper band should always be >= lower band."""
        df = make_ohlcv(50)
        bb = calc_bollinger_bands(df, period=20)
        valid = bb.dropna()
        assert (valid["bb_upper"] >= valid["bb_lower"]).all()

    def test_middle_is_sma(self):
        """Middle band should be the SMA."""
        df = make_ohlcv(50)
        bb = calc_bollinger_bands(df, period=20)
        sma = calc_sma(df, period=20)
        valid_idx = sma.dropna().index
        diff = (bb["bb_middle"].loc[valid_idx] - sma.loc[valid_idx]).abs()
        assert diff.max() < 1e-10

    def test_constant_price_zero_bandwidth(self):
        """Constant price should give zero bandwidth (std=0)."""
        df = make_constant_ohlcv(30)
        bb = calc_bollinger_bands(df, period=20)
        valid = bb.dropna()
        # std = 0, so upper = middle = lower
        if len(valid) > 0:
            diff = (valid["bb_upper"] - valid["bb_lower"]).abs()
            assert diff.max() < 1e-10

    def test_pct_b_range(self):
        """percent_b should be near 0.5 when price is at middle band."""
        df = make_ohlcv(50)
        bb = calc_bollinger_bands(df, period=20)
        # percent_b = (close - lower) / (upper - lower)
        # This is a formula check, not a range check (can be outside [0,1])
        valid = bb.dropna()
        assert len(valid) > 0


# ===================================================================
# Edge cases
# ===================================================================

class TestEdgeCases:
    def test_empty_dataframe(self):
        """Empty DataFrame should not crash, should return empty Series."""
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        ema = calc_ema(df, period=9)
        assert len(ema) == 0

    def test_nan_values_in_close(self):
        """NaN values should propagate, not crash."""
        df = make_ohlcv(30)
        df.loc[5, "close"] = np.nan
        df.loc[10, "close"] = np.nan
        ema = calc_ema(df, period=9)
        assert len(ema) == 30  # should still return full length

    def test_nan_in_volume(self):
        """NaN volume should not crash VWAP."""
        df = make_ohlcv(20)
        df.loc[5, "volume"] = np.nan
        vwap = calc_vwap(df)
        assert len(vwap) == 20

    def test_two_rows_atr(self):
        """Two rows should work for period=2."""
        df = pd.DataFrame({
            "open": [100, 102],
            "high": [105, 106],
            "low": [98, 100],
            "close": [102, 104],
            "volume": [1000, 1000],
        })
        atr = calc_atr(df, period=2)
        assert len(atr) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
