"""
Multi-Indicator Confluence Strategy (Momentum + Trend).

A production-grade strategy that combines:
- Multi-timeframe trend detection (EMA stack on HTF)
- Momentum confirmation (RSI, MACD histogram)
- Volatility context (ATR, Bollinger Bands, Supertrend)
- Volume validation (relative volume, MFI)
- Market structure (support/resistance proximity)
- Candle structure (body ratio, wicks)

Signal flow:
  1. Detect regime on primary timeframe
  2. Check HTF trend alignment
  3. Look for momentum setup on primary TF
  4. Confirm with volume + candle structure
  5. Score and grade the signal
  6. Emit PRE_BUY/PRE_SELL if forming, BUY/SELL if confirmed
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_IST = timezone(timedelta(hours=5, minutes=30))
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from config.constants import (
    MarketRegime,
    OrderSide,
    SignalType,
    TradeGrade,
    confidence_to_grade,
)
from data.indicators import (
    calc_adx,
    calc_all_indicators,
    calc_atr,
    calc_bollinger_bands,
    calc_ema,
    calc_macd,
    calc_mfi,
    calc_relative_volume,
    calc_rsi,
    calc_supertrend,
    calc_vwap,
    calc_volume_spike,
)
from strategies.base import BaseStrategy, Signal
from strategies.regime import MarketRegimeDetector, RegimeContext
from strategies.scoring import SignalScorer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Active signal tracker (prevents duplicate entries)
# ---------------------------------------------------------------------------

@dataclass
class _ActiveSignal:
    """Tracks an active signal to prevent duplicates and manage cooldowns."""
    symbol: str
    side: OrderSide
    signal_type: SignalType
    trade_id: str
    timestamp: float = field(default_factory=time.time)


class MomentumTrendStrategy(BaseStrategy):
    """Concrete strategy using multi-indicator confluence.

    Configurable via the ``strategy`` section of settings.yaml.
    """

    name = "multi_indicator_confluence"

    def __init__(self, config: Dict[str, Any]) -> None:
        strat_cfg = config.get("strategy", {})
        ind_cfg = strat_cfg.get("indicators", {})
        filt_cfg = strat_cfg.get("filters", {})
        risk_cfg = config.get("risk", {})
        tf_cfg = config.get("timeframes", {})

        # --- Timeframes ---
        self.primary_tf: str = tf_cfg.get("primary", "5m")
        self.higher_tf: str = tf_cfg.get("higher", "15m")
        self.trigger_tf: str = tf_cfg.get("trigger", "1m")

        # --- Indicator parameters ---
        self.ema_fast: int = ind_cfg.get("ema", {}).get("fast", 9)
        self.ema_medium: int = ind_cfg.get("ema", {}).get("medium", 21)
        self.ema_slow: int = ind_cfg.get("ema", {}).get("slow", 50)
        self.ema_trend: int = ind_cfg.get("ema", {}).get("trend", 200)

        self.rsi_period: int = ind_cfg.get("rsi", {}).get("period", 14)
        self.rsi_ob: int = ind_cfg.get("rsi", {}).get("overbought", 70)
        self.rsi_os: int = ind_cfg.get("rsi", {}).get("oversold", 30)

        self.macd_fast: int = ind_cfg.get("macd", {}).get("fast", 12)
        self.macd_slow: int = ind_cfg.get("macd", {}).get("slow", 26)
        self.macd_signal: int = ind_cfg.get("macd", {}).get("signal", 9)

        self.atr_period: int = ind_cfg.get("atr", {}).get("period", 14)
        self.atr_sl_mult: float = risk_cfg.get("stop_loss", {}).get("atr_multiplier", 1.8)  # was 1.5 — wider SL for noise protection

        self.st_period: int = ind_cfg.get("supertrend", {}).get("period", 10)
        self.st_mult: float = ind_cfg.get("supertrend", {}).get("multiplier", 3.0)

        self.bb_period: int = ind_cfg.get("bollinger", {}).get("period", 20)
        self.bb_std: float = ind_cfg.get("bollinger", {}).get("std_dev", 2.0)

        self.mfi_period: int = ind_cfg.get("mfi", {}).get("period", 14)
        self.mfi_ob: int = ind_cfg.get("mfi", {}).get("overbought", 80)
        self.mfi_os: int = ind_cfg.get("mfi", {}).get("oversold", 20)

        self.vol_spike_mult: float = ind_cfg.get("volume", {}).get("spike_multiplier", 2.0)
        self.vol_lookback: int = ind_cfg.get("volume", {}).get("lookback", 20)

        # --- Filter settings ---
        self.min_confidence: int = max(filt_cfg.get("min_confidence", 70), 70)  # was 60 — filter weak signals
        self.min_grade: str = "B"  # enforce B minimum regardless of config
        self.chop_filter: bool = filt_cfg.get("chop_filter", True)
        self.spread_max_pct: float = filt_cfg.get("spread_max_pct", 0.1)
        self.cooldown_sec: int = filt_cfg.get("cooldown_seconds", 300)
        self.dup_prevention: bool = filt_cfg.get("duplicate_prevention", True)
        self.max_signals_hr: int = filt_cfg.get("max_signals_per_hour", 10)

        # --- TP configuration (data-driven: TP1 at 1.5R hits ~70%) ---
        tp_cfg = risk_cfg.get("take_profit", {})
        self.tp1_rr: float = 1.5   # 1.5R — data shows this hits 70%+
        self.tp2_rr: float = 2.5   # 2.5R — realistic swing target
        self.tp3_rr: float = 4.0   # 4.0R — small runner (15% of position)

        # --- Sub-engines ---
        self.regime_detector = MarketRegimeDetector()
        self.scorer = SignalScorer()

        # --- State ---
        self._active_signals: Dict[str, _ActiveSignal] = {}  # key = symbol
        self._signal_history: List[float] = []  # timestamps of recent signals
        self._last_sl_time: Dict[str, float] = {}  # symbol -> timestamp of last SL

        # --- Scan status (for dashboard "why no signal" display) ---
        self.last_scan_status: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # StrategyEngine interface
    # ------------------------------------------------------------------

    def get_required_timeframes(self) -> List[str]:
        return [self.primary_tf, self.higher_tf]

    def analyze(
        self,
        symbol: str,
        candles_dict: Dict[str, pd.DataFrame],
    ) -> List[Signal]:
        """Full analysis pipeline for a single symbol."""
        now_iso = datetime.now(_IST).isoformat()
        primary_df = candles_dict.get(self.primary_tf)
        htf_df = candles_dict.get(self.higher_tf)

        if primary_df is None or len(primary_df) < 200:
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"Insufficient candle data ({len(primary_df) if primary_df is not None else 0}/200 bars)",
                "indicators": {}, "regime": "unknown",
            }
            return []

        # --- Step 1: Compute indicators ---
        df = calc_all_indicators(
            primary_df,
            ema_periods=(self.ema_fast, self.ema_medium, self.ema_slow, self.ema_trend),
            rsi_period=self.rsi_period,
            macd_fast=self.macd_fast,
            macd_slow=self.macd_slow,
            macd_signal=self.macd_signal,
            atr_period=self.atr_period,
            bb_period=self.bb_period,
            bb_std=self.bb_std,
            supertrend_period=self.st_period,
            supertrend_mult=self.st_mult,
            adx_period=self.atr_period,
            mfi_period=self.mfi_period,
            vol_sma_period=self.vol_lookback,
        )

        # --- Step 2: Detect regime ---
        regime_ctx = self.regime_detector.detect_regime(df, htf_df)

        # Extract indicator values for status
        last_row = df.iloc[-1]
        indicators = {}
        try:
            indicators = {
                "rsi": round(float(last_row.get("rsi", 0)), 1),
                "ema_9": round(float(last_row.get("ema_9", 0)), 2),
                "ema_21": round(float(last_row.get("ema_21", 0)), 2),
                "ema_50": round(float(last_row.get("ema_50", 0)), 2),
                "ema_200": round(float(last_row.get("ema_200", 0)), 2),
                "macd": round(float(last_row.get("macd", 0)), 4),
                "supertrend_dir": int(last_row.get("supertrend_direction", 0)),
                "close": round(float(last_row.get("close", 0)), 2),
            }
        except Exception:
            pass

        # --- Step 3: Regime-based confidence adjustment ---
        # Instead of blocking entirely, raise the bar for risky regimes
        regime_min_confidence = self.min_confidence  # default 70
        regime_min_confirms = 4

        if self.chop_filter and regime_ctx.regime == MarketRegime.SIDEWAYS:
            # Sideways: allow signals but require more confirmations
            regime_min_confidence = 80
            regime_min_confirms = 6
            logger.debug("%s: SIDEWAYS regime — raising bar (conf≥%d, confirms≥%d)",
                         symbol, regime_min_confidence, regime_min_confirms)

        if regime_ctx.regime == MarketRegime.HIGH_VOLATILITY:
            # High vol: allow signals but require strong confluence
            regime_min_confidence = 75
            regime_min_confirms = 5
            logger.debug("%s: HIGH_VOLATILITY regime — raising bar (conf≥%d, confirms≥%d)",
                         symbol, regime_min_confidence, regime_min_confirms)

        if regime_ctx.regime == MarketRegime.LOW_LIQUIDITY:
            # Low liquidity: still block — too dangerous
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"Risky regime: {regime_ctx.regime.value}",
                "indicators": indicators, "regime": regime_ctx.regime.value,
            }
            return []

        # --- Step 4: Rate limit check ---
        now = time.time()
        self._signal_history = [
            t for t in self._signal_history if now - t < 3600
        ]
        if len(self._signal_history) >= self.max_signals_hr:
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"Rate limited ({len(self._signal_history)}/{self.max_signals_hr} signals/hour)",
                "indicators": indicators, "regime": regime_ctx.regime.value,
            }
            return []

        # --- Step 5: Cooldown check ---
        if symbol in self._last_sl_time:
            elapsed = now - self._last_sl_time[symbol]
            if elapsed < self.cooldown_sec:
                remaining = int(self.cooldown_sec - elapsed)
                self.last_scan_status[symbol] = {
                    "time": now_iso, "signal": False,
                    "reason": f"Cooldown active ({remaining}s remaining)",
                    "indicators": indicators, "regime": regime_ctx.regime.value,
                }
                return []

        # --- Step 6: Look for setups ---
        signals: List[Signal] = []

        long_signal = self._check_long_setup(symbol, df, htf_df, regime_ctx,
                                              min_confirms=regime_min_confirms,
                                              min_conf=regime_min_confidence)
        if long_signal:
            signals.append(long_signal)

        short_signal = self._check_short_setup(symbol, df, htf_df, regime_ctx,
                                               min_confirms=regime_min_confirms,
                                               min_conf=regime_min_confidence)
        if short_signal:
            signals.append(short_signal)

        if signals:
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": True,
                "reason": f"Signal generated: {', '.join(s.signal_type.value for s in signals)}",
                "indicators": indicators, "regime": regime_ctx.regime.value,
            }
        else:
            # Build diagnostic explaining why no setups triggered
            diag = self._build_investment_diagnostics(df, regime_ctx, regime_min_confidence, regime_min_confirms)
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": "No long/short setup conditions met",
                "indicators": indicators, "regime": regime_ctx.regime.value,
                "diagnostics": diag,
            }

        return signals

    # ------------------------------------------------------------------
    # Investment diagnostics (for dashboard "why no signal")
    # ------------------------------------------------------------------

    def _build_investment_diagnostics(
        self, df: pd.DataFrame, regime: RegimeContext,
        min_conf: int, min_confirms: int,
    ) -> dict:
        """Return human-readable diagnostics for why no investment signal fired."""
        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema_f = last.get(f"ema_{self.ema_fast}", 0)
        ema_m = last.get(f"ema_{self.ema_medium}", 0)
        ema_s = last.get(f"ema_{self.ema_slow}", 0)
        ema_t = last.get(f"ema_{self.ema_trend}", 0)
        rsi = last.get("rsi", 50)
        macd_hist = last.get("macd_hist", 0)
        rel_vol = last.get("relative_volume", 1.0)
        close = last["close"]
        st_dir = last.get("supertrend_dir", 0)

        long_blocks = []
        short_blocks = []

        # LONG checks
        if close <= ema_m:
            long_blocks.append(f"Price({close:.0f}) below EMA{self.ema_medium}({ema_m:.0f})")
        else:
            if not (ema_f > ema_m > ema_s):
                long_blocks.append(f"EMA stack not bullish: {self.ema_fast}={ema_f:.0f}, {self.ema_medium}={ema_m:.0f}, {self.ema_slow}={ema_s:.0f}")
            if rsi >= self.rsi_ob:
                long_blocks.append(f"RSI({rsi:.0f}) overbought (>={self.rsi_ob})")
            elif rsi <= self.rsi_os:
                long_blocks.append(f"RSI({rsi:.0f}) oversold (<={self.rsi_os})")
            if macd_hist <= 0:
                long_blocks.append(f"MACD histogram negative ({macd_hist:.4f})")
            if rel_vol < 0.8:
                long_blocks.append(f"Low volume ({rel_vol:.1f}x)")

            # Count confirmations that WOULD be met
            confs_met = 0
            if ema_f > ema_m > ema_s:
                confs_met += 1
            if ema_t > 0 and close > ema_t:
                confs_met += 1
            if st_dir == 1:
                confs_met += 1
            if macd_hist > prev.get("macd_hist", 0):
                confs_met += 1
            if 40 < rsi < 65:
                confs_met += 1
            if not long_blocks:
                long_blocks.append(f"Confirmations: {confs_met}/{min_confirms} needed")

        # SHORT checks
        if close >= ema_m:
            short_blocks.append(f"Price({close:.0f}) above EMA{self.ema_medium}({ema_m:.0f})")
        else:
            if not (ema_f < ema_m < ema_s):
                short_blocks.append(f"EMA stack not bearish: {self.ema_fast}={ema_f:.0f}, {self.ema_medium}={ema_m:.0f}, {self.ema_slow}={ema_s:.0f}")
            if rsi >= self.rsi_ob:
                short_blocks.append(f"RSI({rsi:.0f}) overbought (good for short)")
            elif rsi <= self.rsi_os:
                short_blocks.append(f"RSI({rsi:.0f}) not oversold enough")
            if macd_hist >= 0:
                short_blocks.append(f"MACD histogram positive ({macd_hist:.4f})")

        regime_note = ""
        if regime.regime == MarketRegime.SIDEWAYS:
            regime_note = f"SIDEWAYS regime: bar raised to {min_conf} conf / {min_confirms} confirms"
        elif regime.regime == MarketRegime.HIGH_VOLATILITY:
            regime_note = f"HIGH_VOLATILITY regime: bar raised to {min_conf} conf / {min_confirms} confirms"
        elif regime.regime in (MarketRegime.TRENDING_UP, MarketRegime.BREAKOUT):
            regime_note = f"{regime.regime.value}: favorable for longs"
        elif regime.regime in (MarketRegime.TRENDING_DOWN,):
            regime_note = f"{regime.regime.value}: favorable for shorts"

        return {
            "long_blockers": long_blocks,
            "short_blockers": short_blocks,
            "regime_note": regime_note,
            "min_confidence": min_conf,
            "min_confirms": min_confirms,
        }

    # ------------------------------------------------------------------
    # Long setup detection
    # ------------------------------------------------------------------

    def _check_long_setup(
        self,
        symbol: str,
        df: pd.DataFrame,
        htf_df: Optional[pd.DataFrame],
        regime: RegimeContext,
        min_confirms: int = 4,
        min_conf: int = 70,
    ) -> Optional[Signal]:
        """Check for a long (buy) setup."""
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Duplicate check
        if self.dup_prevention and symbol in self._active_signals:
            active = self._active_signals[symbol]
            if active.side == OrderSide.LONG:
                return None

        # --- Trend filter: EMA stack ---
        ema_f = last.get(f"ema_{self.ema_fast}", 0)
        ema_m = last.get(f"ema_{self.ema_medium}", 0)
        ema_s = last.get(f"ema_{self.ema_slow}", 0)
        ema_t = last.get(f"ema_{self.ema_trend}", 0)

        price_above_ema = last["close"] > ema_m
        ema_bullish_stack = ema_f > ema_m > ema_s

        if not price_above_ema:
            return None

        # --- Momentum filter ---
        rsi = last.get("rsi", 50)
        macd_hist = last.get("macd_hist", 0)
        macd_hist_prev = prev.get("macd_hist", 0)
        supertrend_dir = last.get("supertrend_dir", 0)
        mfi = last.get("mfi", 50)

        momentum_ok = (
            self.rsi_os < rsi < self.rsi_ob  # RSI not overbought
            and macd_hist > 0                  # MACD histogram positive
        )

        if not momentum_ok:
            return None

        # --- Volume filter ---
        rel_vol = last.get("relative_volume", 1.0)
        vol_spike = last.get("volume_spike", False)

        if rel_vol < 0.8:
            logger.debug("%s: LONG rejected - low volume (%.2f)", symbol, rel_vol)
            return None

        # --- Determine signal strength ---
        strong_confirmations = 0
        confirmations = []

        # EMA stack fully aligned
        if ema_bullish_stack:
            strong_confirmations += 1
            confirmations.append("EMA stack bullish")

        # Price above EMA 200
        if ema_t > 0 and last["close"] > ema_t:
            strong_confirmations += 1
            confirmations.append("Above EMA 200")

        # Supertrend bullish
        if supertrend_dir == 1:
            strong_confirmations += 1
            confirmations.append("Supertrend bullish")

        # MACD histogram rising
        if macd_hist > macd_hist_prev:
            strong_confirmations += 1
            confirmations.append("MACD rising")

        # RSI mid-zone strength
        if 40 < rsi < 65:
            strong_confirmations += 1
            confirmations.append(f"RSI healthy ({rsi:.0f})")

        # Volume confirmation
        if vol_spike:
            strong_confirmations += 1
            confirmations.append("Volume spike")

        # MFI confirmation
        if mfi > 50:
            strong_confirmations += 1
            confirmations.append(f"MFI bullish ({mfi:.0f})")

        # Bollinger Band position
        pct_b = last.get("bb_pct_b", 0.5)
        if 0.2 < pct_b < 0.8:
            strong_confirmations += 1
            confirmations.append("BB %B in range")

        # --- Regime alignment ---
        if regime.regime in (MarketRegime.TRENDING_UP, MarketRegime.BREAKOUT):
            strong_confirmations += 1
            confirmations.append(f"Regime: {regime.regime.value}")

        # HTF alignment
        if regime.htf_trend_direction == 1:
            strong_confirmations += 1
            confirmations.append("HTF trend up")

        # Need minimum confirmations (regime-adjusted)
        if strong_confirmations < min_confirms:
            return None

        # --- Compute SL / TP levels ---
        atr = last.get("atr", 0)
        if atr <= 0:
            return None

        entry_price = last["close"]
        stop_loss = entry_price - (atr * self.atr_sl_mult)
        risk_amount = entry_price - stop_loss

        tp1 = entry_price + risk_amount * self.tp1_rr
        tp2 = entry_price + risk_amount * self.tp2_rr
        tp3 = entry_price + risk_amount * self.tp3_rr

        # Invalidation level (below the stop by a margin)
        invalidation = stop_loss - atr * 0.5

        # --- Signal type: PRE_BUY or BUY ---
        # BUY requires strong confirmation; otherwise PRE_BUY
        if strong_confirmations >= 6 and ema_bullish_stack and supertrend_dir == 1:
            signal_type = SignalType.BUY
        else:
            signal_type = SignalType.PRE_BUY

        # --- Score the signal ---
        indicator_values = {
            "rsi": rsi,
            "adx": last.get("adx", 0),
            "macd_hist": macd_hist,
            "supertrend_dir": supertrend_dir,
            "ema_9": ema_f,
            "ema_21": ema_m,
            "ema_50": ema_s,
            "relative_volume": rel_vol,
            "mfi": mfi,
            "bb_pct_b": pct_b,
            "vwap": last.get("vwap", 0),
            "atr": atr,
            "_side": OrderSide.LONG,
        }

        confidence, grade, breakdown = self.scorer.score_signal(
            side=OrderSide.LONG,
            entry_price=entry_price,
            stop_loss=stop_loss,
            regime=regime.regime,
            indicators=indicator_values,
            candles=df,
            htf_candles=htf_df,
            spread_pct=0.0,
        )

        # Apply minimum grade filter
        if not grade.meets_minimum(TradeGrade.from_str(self.min_grade)):
            logger.debug(
                "%s: LONG signal rejected - grade %s below minimum %s",
                symbol, grade.value, self.min_grade,
            )
            return None

        effective_min_conf = max(self.min_confidence, min_conf)
        if confidence < effective_min_conf:
            logger.debug(
                "%s: LONG signal rejected - confidence %d below %d",
                symbol, confidence, effective_min_conf,
            )
            return None

        # Risk-reward
        risk_reward = (tp2 - entry_price) / risk_amount if risk_amount > 0 else 0

        reason = f"LONG: {', '.join(confirmations[:5])} | {strong_confirmations} confirms"

        signal = Signal(
            symbol=symbol,
            signal_type=signal_type,
            side=OrderSide.LONG,
            entry_price=round(entry_price, 8),
            stop_loss=round(stop_loss, 8),
            take_profits=[round(tp1, 8), round(tp2, 8), round(tp3, 8)],
            invalidation_level=round(invalidation, 8),
            confidence=confidence,
            grade=grade,
            risk_reward=round(risk_reward, 2),
            reason=reason,
            regime=regime.regime,
            metadata={
                "breakdown": breakdown.to_dict(),
                "confirmations": confirmations,
                "regime_context": {
                    "adx": regime.adx,
                    "atr_percentile": regime.atr_percentile,
                    "bb_squeeze": regime.bb_squeeze,
                    "confidence": regime.confidence,
                },
                "indicators": {
                    k: round(v, 4) if isinstance(v, float) else v
                    for k, v in indicator_values.items()
                    if k != "_side"
                },
            },
        )

        # Track active signal
        self._active_signals[symbol] = _ActiveSignal(
            symbol=symbol,
            side=OrderSide.LONG,
            signal_type=signal_type,
            trade_id=signal.trade_id,
        )
        self._signal_history.append(time.time())

        logger.info("LONG signal: %s", signal)
        return signal

    # ------------------------------------------------------------------
    # Short setup detection
    # ------------------------------------------------------------------

    def _check_short_setup(
        self,
        symbol: str,
        df: pd.DataFrame,
        htf_df: Optional[pd.DataFrame],
        regime: RegimeContext,
        min_confirms: int = 4,
        min_conf: int = 70,
    ) -> Optional[Signal]:
        """Check for a short (sell) setup - mirror of long logic."""
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Duplicate check
        if self.dup_prevention and symbol in self._active_signals:
            active = self._active_signals[symbol]
            if active.side == OrderSide.SHORT:
                return None

        # --- Trend filter: EMA stack ---
        ema_f = last.get(f"ema_{self.ema_fast}", 0)
        ema_m = last.get(f"ema_{self.ema_medium}", 0)
        ema_s = last.get(f"ema_{self.ema_slow}", 0)
        ema_t = last.get(f"ema_{self.ema_trend}", 0)

        price_below_ema = last["close"] < ema_m
        ema_bearish_stack = ema_f < ema_m < ema_s

        if not price_below_ema:
            return None

        # --- Momentum filter ---
        rsi = last.get("rsi", 50)
        macd_hist = last.get("macd_hist", 0)
        macd_hist_prev = prev.get("macd_hist", 0)
        supertrend_dir = last.get("supertrend_dir", 0)
        mfi = last.get("mfi", 50)

        momentum_ok = (
            self.rsi_os < rsi < self.rsi_ob
            and macd_hist < 0
        )

        if not momentum_ok:
            return None

        # --- Volume filter ---
        rel_vol = last.get("relative_volume", 1.0)
        vol_spike = last.get("volume_spike", False)

        if rel_vol < 0.8:
            return None

        # --- Determine signal strength ---
        strong_confirmations = 0
        confirmations = []

        if ema_bearish_stack:
            strong_confirmations += 1
            confirmations.append("EMA stack bearish")

        if ema_t > 0 and last["close"] < ema_t:
            strong_confirmations += 1
            confirmations.append("Below EMA 200")

        if supertrend_dir == -1:
            strong_confirmations += 1
            confirmations.append("Supertrend bearish")

        if macd_hist < macd_hist_prev:
            strong_confirmations += 1
            confirmations.append("MACD falling")

        if 35 < rsi < 60:
            strong_confirmations += 1
            confirmations.append(f"RSI healthy ({rsi:.0f})")

        if vol_spike:
            strong_confirmations += 1
            confirmations.append("Volume spike")

        if mfi < 50:
            strong_confirmations += 1
            confirmations.append(f"MFI bearish ({mfi:.0f})")

        pct_b = last.get("bb_pct_b", 0.5)
        if 0.2 < pct_b < 0.8:
            strong_confirmations += 1
            confirmations.append("BB %B in range")

        if regime.regime in (MarketRegime.TRENDING_DOWN, MarketRegime.BREAKOUT):
            strong_confirmations += 1
            confirmations.append(f"Regime: {regime.regime.value}")

        if regime.htf_trend_direction == -1:
            strong_confirmations += 1
            confirmations.append("HTF trend down")

        # Need minimum confirmations (regime-adjusted)
        if strong_confirmations < min_confirms:
            return None

        # --- Compute SL / TP levels ---
        atr = last.get("atr", 0)
        if atr <= 0:
            return None

        entry_price = last["close"]
        stop_loss = entry_price + (atr * self.atr_sl_mult)
        risk_amount = stop_loss - entry_price

        tp1 = entry_price - risk_amount * self.tp1_rr
        tp2 = entry_price - risk_amount * self.tp2_rr
        tp3 = entry_price - risk_amount * self.tp3_rr

        invalidation = stop_loss + atr * 0.5

        if strong_confirmations >= 6 and ema_bearish_stack and supertrend_dir == -1:
            signal_type = SignalType.SELL
        else:
            signal_type = SignalType.PRE_SELL

        indicator_values = {
            "rsi": rsi,
            "adx": last.get("adx", 0),
            "macd_hist": macd_hist,
            "supertrend_dir": supertrend_dir,
            "ema_9": ema_f,
            "ema_21": ema_m,
            "ema_50": ema_s,
            "relative_volume": rel_vol,
            "mfi": mfi,
            "bb_pct_b": pct_b,
            "vwap": last.get("vwap", 0),
            "atr": atr,
            "_side": OrderSide.SHORT,
        }

        confidence, grade, breakdown = self.scorer.score_signal(
            side=OrderSide.SHORT,
            entry_price=entry_price,
            stop_loss=stop_loss,
            regime=regime.regime,
            indicators=indicator_values,
            candles=df,
            htf_candles=htf_df,
            spread_pct=0.0,
        )

        if not grade.meets_minimum(TradeGrade.from_str(self.min_grade)):
            logger.debug(
                "%s: SHORT signal rejected - grade %s below minimum %s",
                symbol, grade.value, self.min_grade,
            )
            return None

        effective_min_conf = max(self.min_confidence, min_conf)
        if confidence < effective_min_conf:
            return None

        risk_reward = (entry_price - tp2) / risk_amount if risk_amount > 0 else 0

        reason = f"SHORT: {', '.join(confirmations[:5])} | {strong_confirmations} confirms"

        signal = Signal(
            symbol=symbol,
            signal_type=signal_type,
            side=OrderSide.SHORT,
            entry_price=round(entry_price, 8),
            stop_loss=round(stop_loss, 8),
            take_profits=[round(tp1, 8), round(tp2, 8), round(tp3, 8)],
            invalidation_level=round(invalidation, 8),
            confidence=confidence,
            grade=grade,
            risk_reward=round(risk_reward, 2),
            reason=reason,
            regime=regime.regime,
            metadata={
                "breakdown": breakdown.to_dict(),
                "confirmations": confirmations,
                "regime_context": {
                    "adx": regime.adx,
                    "atr_percentile": regime.atr_percentile,
                    "bb_squeeze": regime.bb_squeeze,
                    "confidence": regime.confidence,
                },
                "indicators": {
                    k: round(v, 4) if isinstance(v, float) else v
                    for k, v in indicator_values.items()
                    if k != "_side"
                },
            },
        )

        self._active_signals[symbol] = _ActiveSignal(
            symbol=symbol,
            side=OrderSide.SHORT,
            signal_type=signal_type,
            trade_id=signal.trade_id,
        )
        self._signal_history.append(time.time())

        logger.info("SHORT signal: %s", signal)
        return signal

    # ------------------------------------------------------------------
    # Signal management
    # ------------------------------------------------------------------

    def clear_signal(self, symbol: str) -> None:
        """Clear active signal for a symbol (call after trade exit)."""
        self._active_signals.pop(symbol, None)

    def record_stop_loss(self, symbol: str) -> None:
        """Record SL hit time for cooldown enforcement."""
        self._last_sl_time[symbol] = time.time()
        self.clear_signal(symbol)

    def has_active_signal(self, symbol: str) -> bool:
        return symbol in self._active_signals

    def get_active_signal(self, symbol: str) -> Optional[_ActiveSignal]:
        return self._active_signals.get(symbol)
