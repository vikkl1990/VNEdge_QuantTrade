"""
Market regime detection engine.

Classifies current market conditions into one of several regimes so the
strategy can adapt its entry logic, filter sizes, and risk parameters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from config.constants import MarketRegime
from data.indicators import (
    calc_adx,
    calc_atr,
    calc_atr_pct,
    calc_bollinger_bands,
    calc_ema,
    calc_ema_slope,
    calc_volume_sma,
)

logger = logging.getLogger(__name__)


@dataclass
class RegimeContext:
    """Rich context returned alongside the regime classification."""

    regime: MarketRegime
    adx: float = 0.0
    atr_percentile: float = 0.0
    ema_slope: float = 0.0
    bb_bandwidth: float = 0.0
    bb_squeeze: bool = False
    volume_ratio: float = 0.0
    trend_direction: int = 0        # +1 up, -1 down, 0 neutral
    htf_trend_direction: int = 0    # higher-timeframe bias
    confidence: float = 0.0         # 0-1 how sure we are of the label


class MarketRegimeDetector:
    """Stateless detector that labels the current market regime.

    Uses a combination of ADX, ATR percentile, EMA slope, and Bollinger
    bandwidth to produce a :class:`MarketRegime` enum value.
    """

    # ----- configurable thresholds -----
    ADX_TREND_THRESHOLD: float = 25.0
    ADX_STRONG_TREND: float = 40.0
    ATR_HIGH_VOL_PERCENTILE: float = 85.0
    ATR_LOW_VOL_PERCENTILE: float = 20.0
    EMA_SLOPE_THRESHOLD: float = 0.15       # % per 3 bars
    BB_SQUEEZE_PERCENTILE: float = 20.0     # bandwidth percentile
    BB_EXPANSION_PERCENTILE: float = 80.0
    VOLUME_LOW_LIQUIDITY: float = 0.15      # relative to 20-bar SMA (lowered from 0.3 — Delta India has thinner volume)

    def __init__(
        self,
        adx_period: int = 14,
        atr_period: int = 14,
        ema_period: int = 50,
        bb_period: int = 20,
        bb_std: float = 2.0,
        vol_sma_period: int = 20,
        atr_lookback: int = 100,
    ) -> None:
        self.adx_period = adx_period
        self.atr_period = atr_period
        self.ema_period = ema_period
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.vol_sma_period = vol_sma_period
        self.atr_lookback = atr_lookback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect_regime(
        self,
        df: pd.DataFrame,
        htf_df: Optional[pd.DataFrame] = None,
    ) -> RegimeContext:
        """Classify the market regime for the primary-timeframe DataFrame.

        Parameters
        ----------
        df : DataFrame
            Primary timeframe OHLCV data (e.g. 5 m).
        htf_df : DataFrame, optional
            Higher timeframe OHLCV data (e.g. 15 m) for directional bias.

        Returns
        -------
        RegimeContext
            Regime label with supporting metrics.
        """
        if len(df) < self.atr_lookback:
            logger.warning(
                "Insufficient data for regime detection (%d bars, need %d)",
                len(df),
                self.atr_lookback,
            )
            return RegimeContext(regime=MarketRegime.SIDEWAYS)

        # --- Compute components ---
        adx_data = calc_adx(df, self.adx_period)
        adx_val = float(adx_data["adx"].iloc[-1])
        plus_di = float(adx_data["plus_di"].iloc[-1])
        minus_di = float(adx_data["minus_di"].iloc[-1])

        atr_series = calc_atr_pct(df, self.atr_period)
        atr_pct_now = float(atr_series.iloc[-1])
        atr_lookback_window = atr_series.iloc[-self.atr_lookback :]
        atr_percentile = float(
            (atr_lookback_window < atr_pct_now).sum()
            / len(atr_lookback_window)
            * 100.0
        )

        ema = calc_ema(df, self.ema_period)
        slope = calc_ema_slope(ema, lookback=3)
        ema_slope_val = float(slope.iloc[-1]) if not np.isnan(slope.iloc[-1]) else 0.0

        bb = calc_bollinger_bands(df, self.bb_period, self.bb_std)
        bb_bw = float(bb["bb_bandwidth"].iloc[-1])
        bw_history = bb["bb_bandwidth"].iloc[-self.atr_lookback :]
        bw_percentile = float(
            (bw_history < bb_bw).sum() / len(bw_history) * 100.0
        )
        bb_squeeze = bw_percentile < self.BB_SQUEEZE_PERCENTILE

        vol_sma = calc_volume_sma(df, self.vol_sma_period)
        vol_ratio = float(df["volume"].iloc[-1] / vol_sma.iloc[-1]) if vol_sma.iloc[-1] > 0 else 0.0

        # Trend direction from DI crossover
        if plus_di > minus_di:
            trend_dir = 1
        elif minus_di > plus_di:
            trend_dir = -1
        else:
            trend_dir = 0

        # HTF bias
        htf_trend = 0
        if htf_df is not None and len(htf_df) >= self.ema_period:
            htf_ema = calc_ema(htf_df, self.ema_period)
            htf_slope = calc_ema_slope(htf_ema, lookback=3)
            htf_slope_val = float(htf_slope.iloc[-1]) if not np.isnan(htf_slope.iloc[-1]) else 0.0
            if htf_slope_val > self.EMA_SLOPE_THRESHOLD:
                htf_trend = 1
            elif htf_slope_val < -self.EMA_SLOPE_THRESHOLD:
                htf_trend = -1

        # --- Classification logic ---
        regime, confidence = self._classify(
            adx_val=adx_val,
            atr_percentile=atr_percentile,
            ema_slope=ema_slope_val,
            bb_squeeze=bb_squeeze,
            bw_percentile=bw_percentile,
            vol_ratio=vol_ratio,
            trend_dir=trend_dir,
        )

        ctx = RegimeContext(
            regime=regime,
            adx=round(adx_val, 2),
            atr_percentile=round(atr_percentile, 1),
            ema_slope=round(ema_slope_val, 4),
            bb_bandwidth=round(bb_bw, 4),
            bb_squeeze=bb_squeeze,
            volume_ratio=round(vol_ratio, 2),
            trend_direction=trend_dir,
            htf_trend_direction=htf_trend,
            confidence=round(confidence, 2),
        )

        logger.debug("Regime detected: %s (conf=%.2f)", regime.value, confidence)
        return ctx

    # ------------------------------------------------------------------
    # Internal classification
    # ------------------------------------------------------------------

    def _classify(
        self,
        adx_val: float,
        atr_percentile: float,
        ema_slope: float,
        bb_squeeze: bool,
        bw_percentile: float,
        vol_ratio: float,
        trend_dir: int,
    ) -> tuple[MarketRegime, float]:
        """Return (regime, confidence) based on indicator readings."""

        # 1) Low liquidity check (volume way below average)
        if vol_ratio < self.VOLUME_LOW_LIQUIDITY:
            return MarketRegime.LOW_LIQUIDITY, 0.85

        # 2) High volatility override (extreme ATR)
        if atr_percentile >= self.ATR_HIGH_VOL_PERCENTILE:
            # Could still be a strong trend -- distinguish
            if adx_val >= self.ADX_STRONG_TREND:
                regime = (
                    MarketRegime.TRENDING_UP if trend_dir >= 0
                    else MarketRegime.TRENDING_DOWN
                )
                return regime, 0.80
            return MarketRegime.HIGH_VOLATILITY, 0.75

        # 3) Breakout: BB squeeze releasing + rising ADX
        if bb_squeeze and adx_val > self.ADX_TREND_THRESHOLD:
            return MarketRegime.BREAKOUT, 0.70

        # 4) BB expansion without strong ADX = breakout just starting
        if bw_percentile >= self.BB_EXPANSION_PERCENTILE and adx_val > 20:
            return MarketRegime.BREAKOUT, 0.60

        # 5) Strong trend
        if adx_val >= self.ADX_TREND_THRESHOLD:
            if abs(ema_slope) >= self.EMA_SLOPE_THRESHOLD:
                if ema_slope > 0 and trend_dir >= 0:
                    return MarketRegime.TRENDING_UP, min(0.55 + adx_val / 100, 0.95)
                elif ema_slope < 0 and trend_dir <= 0:
                    return MarketRegime.TRENDING_DOWN, min(0.55 + adx_val / 100, 0.95)

            # ADX trending but slope ambiguous
            if trend_dir > 0:
                return MarketRegime.TRENDING_UP, 0.55
            elif trend_dir < 0:
                return MarketRegime.TRENDING_DOWN, 0.55

        # 6) Mean reversion: low ADX + low volatility + tight bandwidth
        if (
            adx_val < self.ADX_TREND_THRESHOLD
            and atr_percentile < self.ATR_LOW_VOL_PERCENTILE
            and bb_squeeze
        ):
            return MarketRegime.MEAN_REVERSION, 0.65

        # 7) Sideways default
        return MarketRegime.SIDEWAYS, 0.50
