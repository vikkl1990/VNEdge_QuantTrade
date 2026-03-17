"""
Signal quality scoring engine.

Evaluates a candidate signal across multiple dimensions and assigns a
numeric score (0-100) and a letter grade (A+ / A / B / C / Reject).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from config.constants import MarketRegime, OrderSide, TradeGrade
from data.indicators import (
    calc_atr,
    calc_ema,
    calc_relative_volume,
    calc_vwap,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Score breakdown
# ---------------------------------------------------------------------------

@dataclass
class ScoreBreakdown:
    """Detailed breakdown of how a signal was scored."""

    trend_strength: int = 0         # 0-15
    volume_quality: int = 0         # 0-15
    candle_structure: int = 0       # 0-10
    htf_alignment: int = 0          # 0-15
    vwap_distance: int = 0          # 0-10
    atr_suitability: int = 0        # 0-10
    sr_context: int = 0             # 0-10
    indicator_alignment: int = 0    # 0-10
    spread_risk: int = 0            # 0-5

    @property
    def total(self) -> int:
        return (
            self.trend_strength
            + self.volume_quality
            + self.candle_structure
            + self.htf_alignment
            + self.vwap_distance
            + self.atr_suitability
            + self.sr_context
            + self.indicator_alignment
            + self.spread_risk
        )

    def to_dict(self) -> Dict[str, int]:
        return {
            "trend_strength": self.trend_strength,
            "volume_quality": self.volume_quality,
            "candle_structure": self.candle_structure,
            "htf_alignment": self.htf_alignment,
            "vwap_distance": self.vwap_distance,
            "atr_suitability": self.atr_suitability,
            "sr_context": self.sr_context,
            "indicator_alignment": self.indicator_alignment,
            "spread_risk": self.spread_risk,
            "total": self.total,
        }


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

class SignalScorer:
    """Stateless scorer that evaluates signal quality.

    Grade mapping
    -------------
    * 90+ = A+
    * 80+ = A
    * 65+ = B
    * 50+ = C
    * < 50 = Reject
    """

    # Category caps
    MAX_TREND = 15
    MAX_VOLUME = 15
    MAX_CANDLE = 10
    MAX_HTF = 15
    MAX_VWAP = 10
    MAX_ATR = 10
    MAX_SR = 10
    MAX_IND = 10
    MAX_SPREAD = 5

    def score_signal(
        self,
        *,
        side: OrderSide,
        entry_price: float,
        stop_loss: float,
        regime: MarketRegime,
        indicators: Dict[str, Any],
        candles: pd.DataFrame,
        htf_candles: Optional[pd.DataFrame] = None,
        spread_pct: float = 0.0,
    ) -> tuple[int, TradeGrade, ScoreBreakdown]:
        """Score a candidate signal.

        Parameters
        ----------
        side : OrderSide
        entry_price, stop_loss : float
        regime : MarketRegime
        indicators : dict
            Pre-computed indicator values for the current bar, such as
            ``rsi``, ``adx``, ``macd_hist``, ``supertrend_dir``,
            ``ema_9``, ``ema_21``, ``ema_50``, ``ema_200``,
            ``relative_volume``, ``mfi``, ``bb_pct_b``, ``vwap``.
        candles : DataFrame
            Primary timeframe OHLCV.
        htf_candles : DataFrame, optional
            Higher timeframe OHLCV.
        spread_pct : float
            Current bid-ask spread as a percentage of price.

        Returns
        -------
        (score, grade, breakdown)
        """
        bd = ScoreBreakdown()

        bd.trend_strength = self._score_trend(side, indicators, regime)
        bd.volume_quality = self._score_volume(indicators)
        bd.candle_structure = self._score_candle(side, candles)
        bd.htf_alignment = self._score_htf(side, htf_candles)
        bd.vwap_distance = self._score_vwap(side, entry_price, indicators)
        bd.atr_suitability = self._score_atr(entry_price, stop_loss, indicators)
        bd.sr_context = self._score_sr(side, entry_price, candles)
        bd.indicator_alignment = self._score_indicator_alignment(side, indicators)
        bd.spread_risk = self._score_spread(spread_pct)

        score = bd.total
        grade = TradeGrade.from_score(score) if hasattr(TradeGrade, "from_score") else self._grade(score)

        logger.debug(
            "Signal scored %d (%s) | %s",
            score,
            grade.value,
            bd.to_dict(),
        )
        return score, grade, bd

    # ------------------------------------------------------------------
    # Individual scoring dimensions
    # ------------------------------------------------------------------

    def _score_trend(
        self, side: OrderSide, ind: Dict[str, Any], regime: MarketRegime
    ) -> int:
        """Score based on ADX, EMA alignment, and regime direction."""
        pts = 0
        adx = ind.get("adx", 0)

        # ADX contribution (0-7)
        if adx >= 40:
            pts += 7
        elif adx >= 30:
            pts += 5
        elif adx >= 25:
            pts += 3
        elif adx >= 20:
            pts += 1

        # EMA stack alignment (0-5)
        ema_9 = ind.get("ema_9", 0)
        ema_21 = ind.get("ema_21", 0)
        ema_50 = ind.get("ema_50", 0)
        if ema_9 and ema_21 and ema_50:
            if side == OrderSide.LONG and ema_9 > ema_21 > ema_50:
                pts += 5
            elif side == OrderSide.SHORT and ema_9 < ema_21 < ema_50:
                pts += 5
            elif side == OrderSide.LONG and ema_9 > ema_21:
                pts += 2
            elif side == OrderSide.SHORT and ema_9 < ema_21:
                pts += 2

        # Regime alignment (0-3)
        if side == OrderSide.LONG and regime == MarketRegime.TRENDING_UP:
            pts += 3
        elif side == OrderSide.SHORT and regime == MarketRegime.TRENDING_DOWN:
            pts += 3
        elif regime == MarketRegime.BREAKOUT:
            pts += 2

        return min(pts, self.MAX_TREND)

    def _score_volume(self, ind: Dict[str, Any]) -> int:
        """Score based on relative volume and MFI."""
        pts = 0
        rvol = ind.get("relative_volume", 1.0)

        # Relative volume (0-10)
        if rvol >= 3.0:
            pts += 10
        elif rvol >= 2.0:
            pts += 8
        elif rvol >= 1.5:
            pts += 6
        elif rvol >= 1.0:
            pts += 3
        else:
            pts += 0

        # MFI agreement (0-5)
        mfi = ind.get("mfi", 50)
        side_hint = ind.get("_side")  # injected by caller
        if side_hint == OrderSide.LONG and mfi > 50:
            pts += 5 if mfi > 60 else 3
        elif side_hint == OrderSide.SHORT and mfi < 50:
            pts += 5 if mfi < 40 else 3

        return min(pts, self.MAX_VOLUME)

    def _score_candle(self, side: OrderSide, df: pd.DataFrame) -> int:
        """Score based on the last candle's body/wick structure."""
        if len(df) < 2:
            return 0

        pts = 0
        last = df.iloc[-1]
        body = abs(last["close"] - last["open"])
        upper_wick = last["high"] - max(last["open"], last["close"])
        lower_wick = min(last["open"], last["close"]) - last["low"]
        full_range = last["high"] - last["low"]

        if full_range == 0:
            return 0

        body_ratio = body / full_range

        # Strong body in signal direction (0-5)
        bullish_candle = last["close"] > last["open"]
        if side == OrderSide.LONG and bullish_candle and body_ratio > 0.6:
            pts += 5
        elif side == OrderSide.SHORT and not bullish_candle and body_ratio > 0.6:
            pts += 5
        elif body_ratio > 0.4:
            pts += 2

        # Rejection wick supporting direction (0-5)
        if side == OrderSide.LONG and lower_wick > body * 1.5:
            pts += 5  # hammer / pin bar
        elif side == OrderSide.SHORT and upper_wick > body * 1.5:
            pts += 5  # shooting star
        elif side == OrderSide.LONG and lower_wick > body:
            pts += 2
        elif side == OrderSide.SHORT and upper_wick > body:
            pts += 2

        return min(pts, self.MAX_CANDLE)

    def _score_htf(self, side: OrderSide, htf_df: Optional[pd.DataFrame]) -> int:
        """Score based on higher-timeframe trend alignment."""
        if htf_df is None or len(htf_df) < 50:
            return 5  # neutral when no data

        pts = 0
        ema_21 = calc_ema(htf_df, 21).iloc[-1]
        ema_50 = calc_ema(htf_df, 50).iloc[-1]
        close = htf_df["close"].iloc[-1]

        # Price above/below key EMAs (0-8)
        if side == OrderSide.LONG:
            if close > ema_21:
                pts += 4
            if close > ema_50:
                pts += 4
        else:
            if close < ema_21:
                pts += 4
            if close < ema_50:
                pts += 4

        # EMA ordering on HTF (0-4)
        if side == OrderSide.LONG and ema_21 > ema_50:
            pts += 4
        elif side == OrderSide.SHORT and ema_21 < ema_50:
            pts += 4

        # Momentum direction on HTF (0-3)
        if len(htf_df) >= 3:
            mom = htf_df["close"].iloc[-1] - htf_df["close"].iloc[-3]
            if side == OrderSide.LONG and mom > 0:
                pts += 3
            elif side == OrderSide.SHORT and mom < 0:
                pts += 3

        return min(pts, self.MAX_HTF)

    def _score_vwap(
        self, side: OrderSide, entry: float, ind: Dict[str, Any]
    ) -> int:
        """Score based on price position relative to VWAP."""
        vwap = ind.get("vwap", 0)
        if vwap == 0 or entry == 0:
            return 5

        dist_pct = ((entry - vwap) / vwap) * 100.0
        pts = 0

        if side == OrderSide.LONG:
            # Best: price near or slightly below VWAP (value area)
            if -0.3 <= dist_pct <= 0.1:
                pts = 10
            elif -0.5 <= dist_pct <= 0.3:
                pts = 7
            elif dist_pct < -0.5:
                pts = 4  # extended below -- risky but could bounce
            else:
                pts = 2  # above VWAP, chasing
        else:
            if -0.1 <= dist_pct <= 0.3:
                pts = 10
            elif -0.3 <= dist_pct <= 0.5:
                pts = 7
            elif dist_pct > 0.5:
                pts = 4
            else:
                pts = 2

        return min(pts, self.MAX_VWAP)

    def _score_atr(
        self, entry: float, stop_loss: float, ind: Dict[str, Any]
    ) -> int:
        """Score stop distance vs ATR -- ideally 1-2 ATR."""
        atr = ind.get("atr", 0)
        if atr == 0 or entry == 0:
            return 5

        sl_dist = abs(entry - stop_loss)
        atr_ratio = sl_dist / atr

        if 1.0 <= atr_ratio <= 2.0:
            return 10
        elif 0.7 <= atr_ratio < 1.0:
            return 7
        elif 2.0 < atr_ratio <= 2.5:
            return 6
        elif 0.5 <= atr_ratio < 0.7:
            return 4
        elif atr_ratio > 2.5:
            return 2  # stop too wide
        else:
            return 1  # stop too tight

    def _score_sr(
        self, side: OrderSide, entry: float, df: pd.DataFrame
    ) -> int:
        """Score based on proximity to support/resistance levels.

        Uses simple rolling highs/lows as proxy for S/R.
        """
        if len(df) < 50:
            return 5

        pts = 0
        recent_high = df["high"].iloc[-50:].max()
        recent_low = df["low"].iloc[-50:].min()
        price_range = recent_high - recent_low
        if price_range == 0:
            return 5

        # Distance to support / resistance as fraction of range
        dist_to_support = (entry - recent_low) / price_range
        dist_to_resistance = (recent_high - entry) / price_range

        if side == OrderSide.LONG:
            # Near support = good
            if dist_to_support < 0.25:
                pts += 7
            elif dist_to_support < 0.40:
                pts += 4
            # Room to resistance
            if dist_to_resistance > 0.5:
                pts += 3
        else:
            # Near resistance = good
            if dist_to_resistance < 0.25:
                pts += 7
            elif dist_to_resistance < 0.40:
                pts += 4
            if dist_to_support > 0.5:
                pts += 3

        return min(pts, self.MAX_SR)

    def _score_indicator_alignment(
        self, side: OrderSide, ind: Dict[str, Any]
    ) -> int:
        """Score how many indicators agree with the signal direction."""
        pts = 0
        checks_passed = 0
        total_checks = 0

        # RSI
        rsi = ind.get("rsi")
        if rsi is not None:
            total_checks += 1
            if side == OrderSide.LONG and 35 < rsi < 70:
                checks_passed += 1
            elif side == OrderSide.SHORT and 30 < rsi < 65:
                checks_passed += 1

        # MACD histogram
        macd_hist = ind.get("macd_hist")
        if macd_hist is not None:
            total_checks += 1
            if side == OrderSide.LONG and macd_hist > 0:
                checks_passed += 1
            elif side == OrderSide.SHORT and macd_hist < 0:
                checks_passed += 1

        # Supertrend
        st_dir = ind.get("supertrend_dir")
        if st_dir is not None:
            total_checks += 1
            if side == OrderSide.LONG and st_dir == 1:
                checks_passed += 1
            elif side == OrderSide.SHORT and st_dir == -1:
                checks_passed += 1

        # Bollinger %B
        pct_b = ind.get("bb_pct_b")
        if pct_b is not None:
            total_checks += 1
            if side == OrderSide.LONG and pct_b < 0.7:
                checks_passed += 1
            elif side == OrderSide.SHORT and pct_b > 0.3:
                checks_passed += 1

        if total_checks > 0:
            pts = round((checks_passed / total_checks) * self.MAX_IND)

        return min(pts, self.MAX_IND)

    def _score_spread(self, spread_pct: float) -> int:
        """Deduct points for wide spreads."""
        if spread_pct <= 0.02:
            return 5
        elif spread_pct <= 0.05:
            return 4
        elif spread_pct <= 0.08:
            return 3
        elif spread_pct <= 0.10:
            return 2
        elif spread_pct <= 0.15:
            return 1
        return 0

    # ------------------------------------------------------------------
    # Grade helper (fallback if TradeGrade.from_score not available)
    # ------------------------------------------------------------------

    @staticmethod
    def _grade(score: int) -> TradeGrade:
        if score >= 90:
            return TradeGrade.A_PLUS
        elif score >= 80:
            return TradeGrade.A
        elif score >= 65:
            return TradeGrade.B
        elif score >= 50:
            return TradeGrade.C
        return TradeGrade.REJECT
