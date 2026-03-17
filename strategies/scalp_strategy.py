"""
Quick Scalp Strategy — designed for fast BTC/USDT entries on 1m/5m.

Generates LONG and SHORT signals using multiple independent setup types,
each requiring only 2-3 confirmations.  Trades are meant to be held for
minutes to an hour, with tight stops and quick take-profits.

Setup Types
-----------
1. EMA Momentum Cross   — Fast EMA cross + RSI + volume surge
2. VWAP Reclaim/Reject  — Price reclaims VWAP with volume confirmation
3. RSI Divergence        — Hidden/regular divergence at extremes
4. Supertrend Flip       — Direction change + MACD confirmation
5. BB Squeeze Breakout   — Bollinger squeeze releasing with momentum
6. Momentum Surge        — MACD histogram flip + RSI cross 50 + volume

Each setup is scored independently.  A signal is emitted when ANY setup
meets its confirmation threshold (typically 2-3 checks).  This produces
far more signals than the multi-indicator confluence strategy, which is
the point — quick scalps with tight risk management.

Risk Profile (per trade)
------------------------
- Stop Loss  : 0.8-1.2 ATR (tight)
- TP1        : 1:1 R:R  (close 50%)
- TP2        : 2:1 R:R  (close 30%)
- TP3        : 3:1 R:R  (trail remaining 20%)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# IST timezone
_IST = timezone(timedelta(hours=5, minutes=30))
from typing import Any, Dict, List, Optional, Tuple

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
    calc_atr,
    calc_bollinger_bands,
    calc_ema,
    calc_fib_retracement,
    calc_macd,
    calc_mfi,
    calc_relative_volume,
    calc_rsi,
    calc_supertrend,
    calc_volume_spike,
    calc_vwap,
    detect_choch,
)
from strategies.base import BaseStrategy, Signal
from strategies.scanner_weights import ScannerWeightManager, STATUS_ACTIVE, STATUS_REDUCED

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal tiers (graduated output instead of binary pass/fail)
# ---------------------------------------------------------------------------
TIER_STRONG = "strong"         # Score >= 80: high confidence, take full size
TIER_VALID = "valid"           # Score >= 65: normal signal
TIER_WEAK = "weak"             # Score >= 50: reduced size, log as opportunity
TIER_NEAR_MISS = "near_miss"   # Score >= 35: setup forming, dashboard only
TIER_REJECTED = "rejected"     # Score < 35: not viable


def _tier_from_score(score: float) -> str:
    """Map weighted score to signal tier."""
    if score >= 80:
        return TIER_STRONG
    if score >= 65:
        return TIER_VALID
    if score >= 50:
        return TIER_WEAK
    if score >= 35:
        return TIER_NEAR_MISS
    return TIER_REJECTED


# ---------------------------------------------------------------------------
# Setup result containers
# ---------------------------------------------------------------------------

@dataclass
class _SetupResult:
    """Result from a single setup check."""
    name: str
    side: OrderSide
    confidence: int              # 0-100
    confirmations: List[str]
    entry_price: float = 0.0
    stop_loss: float = 0.0
    atr: float = 0.0


@dataclass
class ScanResult:
    """Graduated result from a scanner — always produced, never None."""
    scanner_name: str
    side: Optional[OrderSide]
    raw_score: int                    # 0-100 before weighting
    weighted_score: float             # after scanner weight applied
    tier: str                         # strong/valid/weak/near_miss/rejected
    confirmations: List[str] = field(default_factory=list)
    penalties: List[str] = field(default_factory=list)
    hard_blocked: bool = False
    block_reason: str = ""
    entry_price: float = 0.0
    stop_loss: float = 0.0
    atr: float = 0.0
    scanner_weight: float = 1.0
    scanner_status: str = "active"
    setup_result: Optional[_SetupResult] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scanner": self.scanner_name,
            "side": self.side.value if self.side else None,
            "raw_score": self.raw_score,
            "weighted_score": round(self.weighted_score, 1),
            "tier": self.tier,
            "confirmations": self.confirmations,
            "penalties": self.penalties,
            "hard_blocked": self.hard_blocked,
            "block_reason": self.block_reason,
            "scanner_weight": self.scanner_weight,
            "scanner_status": self.scanner_status,
        }


class ScalpStrategy(BaseStrategy):
    """Quick-scalp strategy with multiple independent setup types.

    Unlike MomentumTrendStrategy which needs 4+ confirmations across 10
    dimensions, this strategy fires on ANY single setup that passes its
    own 2-3 confirmation checks.  More signals, tighter risk.
    """

    name = "quick_scalp"

    def __init__(self, config: Dict[str, Any]) -> None:
        strat_cfg = config.get("strategy", {})
        ind_cfg = strat_cfg.get("indicators", {})
        filt_cfg = strat_cfg.get("filters", {})
        risk_cfg = config.get("risk", {})
        tf_cfg = config.get("timeframes", {})

        # --- Timeframes ---
        self.primary_tf: str = tf_cfg.get("trigger", "1m")    # Use 1m for scalps
        self.confirm_tf: str = tf_cfg.get("primary", "5m")     # 5m for confirmation
        self.htf: str = tf_cfg.get("higher", "15m")            # 15m for bias

        # --- Fast EMA set for scalping ---
        self.ema_fast: int = 8
        self.ema_slow: int = 21
        self.ema_trend: int = 50

        # --- Indicator params ---
        self.rsi_period: int = ind_cfg.get("rsi", {}).get("period", 14)
        self.atr_period: int = ind_cfg.get("atr", {}).get("period", 14)
        self.bb_period: int = ind_cfg.get("bollinger", {}).get("period", 20)
        self.bb_std: float = ind_cfg.get("bollinger", {}).get("std_dev", 2.0)
        self.st_period: int = ind_cfg.get("supertrend", {}).get("period", 10)
        self.st_mult: float = ind_cfg.get("supertrend", {}).get("multiplier", 3.0)

        # --- Scalp-specific thresholds ---
        self.min_confidence: int = max(filt_cfg.get("min_confidence", 70), 65)  # lowered from 72 — too few signals
        self.cooldown_sec: int = 180         # 3 min between signals (was 2 — reduce whipsaw)
        self.max_signals_hr: int = 8         # fewer, higher quality signals (was 10)
        self.sl_atr_mult: float = 2.5          # SL = 2.5× 5m-ATR (was 1.8× 1m-ATR — too tight, noise kills)
        self.tp1_rr: float = 0.8             # TP1 at 0.8R — closer for faster partial profit capture
        self.tp2_rr: float = 2.0             # TP2 at 2.0R — realistic extended target
        self.tp3_rr: float = 4.0             # TP3 at 4.0R — small runner

        # --- Minimum SL/TP distances (% of price) ---
        # Review data: 0.25% SL still gets noise-stopped on BTC ($185 at $74k)
        # BTC 5m candles wick 0.3-0.5% routinely. Need ≥0.40% floor.
        self.min_sl_pct: float = 0.40        # raised from 0.25% — BTC 5m wicks 0.3%+
        self.min_tp1_pct: float = 0.30       # lowered for 0.8R TP1 — still covers fees
        self.min_rr_ratio: float = 0.8       # matches new 0.8R TP1 target

        # --- RSI divergence lookback ---
        self.div_lookback: int = 30          # bars to scan for divergence (was 14)
        self.div_min_swing: float = 0.002    # minimum price swing % (was 0.001)

        # --- State ---
        self._last_signal_time: Dict[str, float] = {}
        self._signal_count_hr: List[float] = []

        # --- Scanner weight manager (adaptive from R-performance) ---
        self._weight_manager = ScannerWeightManager()

        # --- Opportunity funnel counters (for dashboard) ---
        self._funnel: Dict[str, int] = {
            "scanned": 0, "strong": 0, "valid": 0, "weak": 0,
            "near_miss": 0, "rejected": 0,
            "blocked_regime": 0, "blocked_cost": 0, "blocked_htf": 0,
        }
        self._funnel_reset_time: float = time.time()

        # --- Scan status (for dashboard "why no signal" display) ---
        self.last_scan_status: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------

    def get_required_timeframes(self) -> List[str]:
        return [self.primary_tf, self.confirm_tf, self.htf]

    def analyze(
        self,
        symbol: str,
        candles_dict: Dict[str, pd.DataFrame],
    ) -> List[Signal]:
        """Run all setup scans and emit signals for any that trigger."""
        now = time.time()
        now_iso = datetime.now(_IST).isoformat()

        # Get primary (1m) data
        primary_df = candles_dict.get(self.primary_tf)
        if primary_df is None or len(primary_df) < 50:
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": "Insufficient candle data (<50 bars)",
                "indicators": {}, "setups_checked": [],
            }
            return []

        # Confirmation (5m) and HTF (15m) — optional but add confidence
        confirm_df = candles_dict.get(self.confirm_tf)
        htf_df = candles_dict.get(self.htf)

        # Rate limit
        self._signal_count_hr = [t for t in self._signal_count_hr if now - t < 3600]
        if len(self._signal_count_hr) >= self.max_signals_hr:
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"Rate limited ({len(self._signal_count_hr)}/{self.max_signals_hr} signals this hour)",
                "indicators": {}, "setups_checked": [],
            }
            return []

        # Cooldown per symbol
        last_sig = self._last_signal_time.get(symbol, 0)
        if now - last_sig < self.cooldown_sec:
            remaining = int(self.cooldown_sec - (now - last_sig))
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"Cooldown active ({remaining}s remaining)",
                "indicators": {}, "setups_checked": [],
            }
            return []

        # ── SESSION-AWARE GATING ──
        # Data from 112 trades: Asia Late 37% WR, Asia Early 50%, Europe 63%, US 57%
        # Block the worst session, restrict the marginal one
        ist_now = datetime.now(_IST)
        ist_hour = ist_now.hour + ist_now.minute / 60.0
        if 2.5 <= ist_hour < 9.0:
            # Asia Late (02:30-09:00 IST) — 37% WR, worst session
            # BLOCK all trading — this session destroys edge
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"SESSION GATE: Asia Late (02:30-09:00 IST) blocked — 37% WR historically",
                "indicators": {}, "setups_checked": [],
            }
            return []

        # Session context for confidence adjustment later
        if 9.0 <= ist_hour < 13.5:
            self._current_session = "asia_early"   # 50% WR — raise min confidence
            self._session_min_confidence = 75       # only high-confidence trades
        elif 13.5 <= ist_hour < 20.5:
            self._current_session = "europe"        # 63% WR — best session
            self._session_min_confidence = 65       # normal threshold
        else:
            self._current_session = "us"            # 57% WR — decent
            self._session_min_confidence = 70       # slightly raised

        # --- Compute indicators on primary TF ---
        df = self._compute_indicators(primary_df)

        # --- Compute 5m ATR for SL calculation (1m ATR is too noisy/tight) ---
        # 5m ATR captures real volatility; 1m ATR gets noise-stopped constantly
        self._confirm_atr: float = 0.0
        if confirm_df is not None and len(confirm_df) >= 20:
            try:
                confirm_atr_series = calc_atr(confirm_df, self.atr_period)
                self._confirm_atr = float(confirm_atr_series.iloc[-1])
            except Exception:
                pass

        # --- Determine HTF bias (simple: above/below EMA 50) ---
        htf_bias = self._get_htf_bias(htf_df)
        confirm_bias = self._get_htf_bias(confirm_df)

        # --- Fibonacci & CHOCH on 5m (more reliable than 1m noise) ---
        fib_data = {}
        choch_data = {}
        fib_source = confirm_df if confirm_df is not None and len(confirm_df) >= 50 else primary_df
        choch_source = confirm_df if confirm_df is not None and len(confirm_df) >= 30 else primary_df
        try:
            fib_data = calc_fib_retracement(fib_source, lookback=50)
        except Exception:
            fib_data = {"trend": "unknown", "levels": {}, "at_fib": False}
        try:
            choch_data = detect_choch(choch_source, lookback=30)
        except Exception:
            choch_data = {"choch_detected": False, "direction": None}

        # --- Extract current indicator values for status ---
        last_row = df.iloc[-1]
        indicators = {}
        try:
            indicators = {
                "rsi": round(float(last_row.get("rsi", 0)), 1),
                "ema_8": round(float(last_row.get("ema_8", 0)), 2),
                "ema_21": round(float(last_row.get("ema_21", 0)), 2),
                "ema_50": round(float(last_row.get("ema_50", 0)), 2),
                "macd": round(float(last_row.get("macd", 0)), 4),
                "macd_signal": round(float(last_row.get("macd_signal", 0)), 4),
                "atr": round(float(last_row.get("atr", 0)), 2),
                "bb_upper": round(float(last_row.get("bb_upper", 0)), 2),
                "bb_lower": round(float(last_row.get("bb_lower", 0)), 2),
                "supertrend_dir": int(last_row.get("supertrend_direction", 0)),
                "rel_vol": round(float(last_row.get("rel_vol", 0)), 2),
                "close": round(float(last_row.get("close", 0)), 2),
                "htf_bias": "Bullish" if htf_bias == 1 else ("Bearish" if htf_bias == -1 else "Neutral"),
                "fib_at_level": fib_data.get("at_fib", False),
                "fib_nearest": fib_data.get("nearest_level", ""),
                "choch": choch_data.get("direction", None) if choch_data.get("choch_detected") else None,
            }
        except Exception:
            pass

        # --- Run all setup scans ---
        setups: List[_SetupResult] = []
        scanner_names = {
            "_scan_ema_momentum": "EMA Momentum",
            "_scan_trend_continuation": "Trend Continuation",
            "_scan_rsi_divergence": "RSI Divergence",
            "_scan_rsi_extreme": "RSI Extreme",
            "_scan_momentum_ride": "Momentum Ride",
            "_scan_bb_band_walk": "BB Band Walk",
            "_scan_post_impulse": "Post-Impulse",
            "_scan_supertrend_flip": "Supertrend Flip",
            "_scan_bb_squeeze": "BB Squeeze",
            "_scan_momentum_surge": "Momentum Surge",
        }
        setups_checked = []

        # Pre-compute diagnostic reasons for why each scanner won't fire
        last_row_diag = df.iloc[-1]
        prev_row_diag = df.iloc[-2] if len(df) >= 2 else last_row_diag
        _rsi = indicators.get("rsi", 0)
        _ema8 = indicators.get("ema_8", 0)
        _ema21 = indicators.get("ema_21", 0)
        _close = indicators.get("close", 0)
        _open = float(last_row_diag.get("open", 0))
        _rel_vol = indicators.get("rel_vol", 0)
        _st_dir = indicators.get("supertrend_dir", 0)
        _rsi_prev = round(float(prev_row_diag.get("rsi", 0)), 1)
        _ema8_prev = float(prev_row_diag.get("ema_8", 0))
        _ema21_prev = float(prev_row_diag.get("ema_21", 0))
        _bw = round(float(last_row_diag.get("bb_bandwidth", 0)), 4) if not np.isnan(last_row_diag.get("bb_bandwidth", 0)) else 0
        _bw_prev = round(float(prev_row_diag.get("bb_bandwidth", 0)), 4) if not np.isnan(prev_row_diag.get("bb_bandwidth", 0)) else 0
        _pct_b = round(float(last_row_diag.get("bb_pct_b", 0.5)), 2)

        scanner_diagnostics = {}

        # EMA Momentum: needs EMA8/21 cross
        ema_crossed = (_ema8_prev <= _ema21_prev and _ema8 > _ema21)
        if ema_crossed:
            scanner_diagnostics["EMA Momentum"] = "Bullish cross detected — checking confirmations"
        else:
            if _ema8 > _ema21:
                scanner_diagnostics["EMA Momentum"] = f"No cross: EMA8({_ema8:.0f}) already above EMA21({_ema21:.0f}), need fresh cross"
            elif _ema8 < _ema21:
                scanner_diagnostics["EMA Momentum"] = f"No cross: EMA8({_ema8:.0f}) below EMA21({_ema21:.0f}), bearish crosses disabled"
            else:
                scanner_diagnostics["EMA Momentum"] = "EMAs converged, no cross yet"

        # Trend Continuation: needs pullback to EMA zone
        if _ema8 > _ema21:
            _pb = _close <= _ema8 * 1.001 and _close > _ema21
            _rsi_rec = 40 < _rsi < 58 and _rsi > _rsi_prev
            _bull_candle = _close > _open
            missing = []
            if not _pb:
                if _close > _ema8 * 1.001:
                    missing.append(f"price({_close:.0f}) above EMA8({_ema8:.0f}), no pullback")
                else:
                    missing.append(f"price({_close:.0f}) below EMA21({_ema21:.0f})")
            if not _rsi_rec:
                if _rsi >= 58:
                    missing.append(f"RSI({_rsi}) too high (need 40-58)")
                elif _rsi <= 40:
                    missing.append(f"RSI({_rsi}) too low (need 40-58)")
                elif _rsi <= _rsi_prev:
                    missing.append(f"RSI declining ({_rsi_prev}→{_rsi}), need rising")
            if not _bull_candle:
                missing.append("bearish candle (need bullish)")
            scanner_diagnostics["Trend Continuation"] = " | ".join(missing) if missing else "Conditions met — checking score"
        elif _ema8 < _ema21:
            _pb = _close >= _ema8 * 0.999 and _close < _ema21
            _rsi_rec = 42 < _rsi < 60 and _rsi < _rsi_prev
            _bear_candle = _close < _open
            missing = []
            if not _pb:
                if _close < _ema8 * 0.999:
                    missing.append(f"price({_close:.0f}) below EMA8({_ema8:.0f}), no pullback")
                else:
                    missing.append(f"price({_close:.0f}) above EMA21({_ema21:.0f})")
            if not _rsi_rec:
                if _rsi >= 60:
                    missing.append(f"RSI({_rsi}) too high for short (need 42-60)")
                elif _rsi <= 42:
                    missing.append(f"RSI({_rsi}) too low for short (need 42-60)")
                elif _rsi >= _rsi_prev:
                    missing.append(f"RSI rising ({_rsi_prev}→{_rsi}), need declining")
            if not _bear_candle:
                missing.append("bullish candle (need bearish)")
            scanner_diagnostics["Trend Continuation"] = " | ".join(missing) if missing else "Conditions met — checking score"
        else:
            scanner_diagnostics["Trend Continuation"] = "EMAs flat, no trend"

        # RSI Divergence: needs extreme RSI + price divergence
        if 30 <= _rsi <= 60:
            scanner_diagnostics["RSI Divergence"] = f"RSI({_rsi}) in neutral zone (need <40 for bullish div or >60 for bearish div)"
        elif _rsi < 30:
            scanner_diagnostics["RSI Divergence"] = f"RSI({_rsi}) oversold — looking for price lower-low with RSI higher-low"
        elif _rsi > 60:
            scanner_diagnostics["RSI Divergence"] = f"RSI({_rsi}) elevated — looking for price higher-high with RSI lower-high (need >60 + divergence)"
        else:
            scanner_diagnostics["RSI Divergence"] = f"RSI({_rsi}) — checking divergence patterns"

        # RSI Extreme: needs RSI < 35 turning up OR RSI > 65 turning down
        if _rsi < 35:
            if _rsi > _rsi_prev:
                scanner_diagnostics["RSI Extreme"] = f"RSI({_rsi}) oversold & turning up ({_rsi_prev}→{_rsi}) — checking candle confirmation"
            else:
                scanner_diagnostics["RSI Extreme"] = f"RSI({_rsi}) oversold but still falling ({_rsi_prev}→{_rsi}), need turn-up"
        elif _rsi > 65:
            if _rsi < _rsi_prev:
                scanner_diagnostics["RSI Extreme"] = f"RSI({_rsi}) overbought & turning down ({_rsi_prev}→{_rsi}) — checking candle confirmation"
            else:
                scanner_diagnostics["RSI Extreme"] = f"RSI({_rsi}) overbought but still rising ({_rsi_prev}→{_rsi}), need turn-down"
        else:
            scanner_diagnostics["RSI Extreme"] = f"RSI({_rsi}) in normal range (need <35 or >65)"

        # BB Squeeze: needs recent squeeze + expansion
        if _bw_prev > 0:
            squeeze_info = f"BW prev={_bw_prev:.4f}, now={_bw:.4f}"
            expanding = _bw > _bw_prev * 1.05
            if not expanding:
                scanner_diagnostics["BB Squeeze"] = f"No squeeze breakout: bandwidth not expanding ({squeeze_info})"
            else:
                if _pct_b > 0.75:
                    scanner_diagnostics["BB Squeeze"] = f"Squeeze expanding upward (%B={_pct_b}) — checking volume"
                elif _pct_b < 0.25:
                    scanner_diagnostics["BB Squeeze"] = f"Squeeze expanding downward (%B={_pct_b}) — checking volume"
                else:
                    scanner_diagnostics["BB Squeeze"] = f"Squeeze expanding but %B({_pct_b}) in middle — no direction"
        else:
            scanner_diagnostics["BB Squeeze"] = "Insufficient BB data"

        # Momentum Ride: needs full EMA stack + RSI 58-82 rising + MACD accel + vol > 1.5x
        _macd_hist = float(last_row_diag.get("macd_hist", 0))
        _macd_hist_prev = float(prev_row_diag.get("macd_hist", 0))
        _ema50 = indicators.get("ema_50", 0)
        full_stack = _ema8 > _ema21 > _ema50
        missing_ride = []
        if not full_stack:
            missing_ride.append(f"EMA stack not aligned ({_ema8:.0f}/{_ema21:.0f}/{_ema50:.0f})")
        if _rsi <= 58 or _rsi >= 82:
            missing_ride.append(f"RSI({_rsi}) outside 58-82 range")
        elif _rsi <= _rsi_prev:
            missing_ride.append(f"RSI declining ({_rsi_prev}→{_rsi})")
        if _macd_hist <= 0:
            missing_ride.append(f"MACD histogram negative ({_macd_hist:.2f})")
        elif _macd_hist <= _macd_hist_prev:
            missing_ride.append(f"MACD not accelerating ({_macd_hist_prev:.2f}→{_macd_hist:.2f})")
        if _rel_vol <= 1.5:
            missing_ride.append(f"Volume too low ({_rel_vol:.1f}x, need >1.5x)")
        if _close <= _ema8:
            missing_ride.append(f"Price({_close:.0f}) below EMA8({_ema8:.0f})")
        scanner_diagnostics["Momentum Ride"] = " | ".join(missing_ride) if missing_ride else "Conditions met — checking stretch/impulse filter"

        # BB Band Walk: needs price above BB_upper for 2+ candles + volume
        _bb_upper = indicators.get("bb_upper", 0)
        _bb_lower = indicators.get("bb_lower", 0)
        if _bb_upper > 0:
            if _close > _bb_upper:
                scanner_diagnostics["BB Band Walk"] = f"Price({_close:.0f}) above BB_upper({_bb_upper:.0f}) — checking 2-candle confirmation + volume"
            else:
                pct_from_bb = (_bb_upper - _close) / _close * 100 if _close > 0 else 0
                scanner_diagnostics["BB Band Walk"] = f"Price({_close:.0f}) below BB_upper({_bb_upper:.0f}), {pct_from_bb:.2f}% away"
        else:
            scanner_diagnostics["BB Band Walk"] = "Insufficient BB data"

        # Post-Impulse: needs recent impulse candle + current small candle + pullback
        scanner_diagnostics["Post-Impulse"] = f"Scanning last 3-8 candles for impulse (body > 0.8x ATR) + current small candle + shallow pullback"

        # ── Reset funnel counters every hour ──
        if now - self._funnel_reset_time > 3600:
            self._funnel = {k: 0 for k in self._funnel}
            self._funnel_reset_time = now

        # ── Run ALL scanners (including shadow/suppressed for data) ──
        all_scanners = [
            self._scan_ema_momentum,
            self._scan_trend_continuation,
            self._scan_rsi_divergence,
            self._scan_rsi_extreme,
            self._scan_momentum_ride,
            self._scan_bb_band_walk,
            self._scan_post_impulse,
            self._scan_supertrend_flip,
            self._scan_bb_squeeze,
            self._scan_momentum_surge,
        ]

        scan_results: List[ScanResult] = []

        for scanner in all_scanners:
            label = scanner_names.get(scanner.__name__, scanner.__name__)
            setup_name = scanner.__name__.replace("_scan_", "")
            diag = scanner_diagnostics.get(label, "")
            scanner_weight = self._weight_manager.get_weight(setup_name)
            scanner_status = self._weight_manager.get_status(setup_name)

            self._funnel["scanned"] += 1

            try:
                result = scanner(symbol, df, htf_bias, confirm_bias)

                if result is not None:
                    # Scanner triggered — compute weighted score
                    raw_score = result.confidence
                    # Apply scanner performance weight to confidence
                    adjusted_conf = self._weight_manager.get_confidence_adjustment(setup_name, raw_score)
                    weighted = adjusted_conf * scanner_weight
                    tier = _tier_from_score(weighted)
                    confs = list(result.confirmations)
                    penalties = []

                    # ── Soft penalty: ema_momentum SHORT (33% WR historically) ──
                    if setup_name == "ema_momentum" and result.side == OrderSide.SHORT:
                        weighted *= 0.5
                        penalties.append("ema_momentum SHORT: -50% (33% WR historically)")
                        tier = _tier_from_score(weighted)

                    sr = ScanResult(
                        scanner_name=setup_name,
                        side=result.side,
                        raw_score=raw_score,
                        weighted_score=round(weighted, 1),
                        tier=tier,
                        confirmations=confs,
                        penalties=penalties,
                        entry_price=result.entry_price,
                        stop_loss=result.stop_loss,
                        atr=result.atr,
                        scanner_weight=scanner_weight,
                        scanner_status=scanner_status,
                        setup_result=result,
                    )
                    scan_results.append(sr)
                    setups_checked.append({
                        "name": label, "triggered": True,
                        "confidence": raw_score,
                        "weighted_score": round(weighted, 1),
                        "tier": tier,
                        "scanner_status": scanner_status,
                        "scanner_weight": scanner_weight,
                    })
                else:
                    # Scanner didn't trigger — record as near-miss or rejected
                    # Estimate a "proximity score" from diagnostics
                    proximity = self._estimate_proximity_score(diag)
                    weighted = proximity * scanner_weight
                    tier = _tier_from_score(weighted)

                    sr = ScanResult(
                        scanner_name=setup_name,
                        side=None,
                        raw_score=proximity,
                        weighted_score=round(weighted, 1),
                        tier=tier,
                        penalties=[diag] if diag else [],
                        scanner_weight=scanner_weight,
                        scanner_status=scanner_status,
                    )
                    scan_results.append(sr)
                    setups_checked.append({
                        "name": label, "triggered": False,
                        "reason": diag,
                        "proximity_score": proximity,
                        "tier": tier,
                        "scanner_status": scanner_status,
                        "scanner_weight": scanner_weight,
                    })

            except Exception as exc:
                logger.debug("Setup scanner %s failed: %s", scanner.__name__, exc)
                setups_checked.append({
                    "name": label, "triggered": False,
                    "error": str(exc), "reason": diag,
                    "scanner_status": scanner_status,
                })

        # ── Update funnel counters ──
        for sr in scan_results:
            if sr.tier in self._funnel:
                self._funnel[sr.tier] += 1

        # ── Select best tradeable result ──
        tradeable = [
            sr for sr in scan_results
            if sr.setup_result is not None
            and sr.tier in (TIER_STRONG, TIER_VALID, TIER_WEAK)
            and self._weight_manager.is_tradeable(sr.scanner_name)
        ]

        # Sort near-misses for dashboard visibility
        near_misses = [
            sr for sr in scan_results
            if sr.tier == TIER_NEAR_MISS and sr.setup_result is not None
        ]

        if not tradeable:
            # Report best near-miss for dashboard insight
            best_near = max(near_misses, key=lambda s: s.weighted_score) if near_misses else None
            reason = "No setup conditions met"
            if best_near:
                reason = f"Near miss: {best_near.scanner_name} scored {best_near.weighted_score:.0f} (need 50+)"

            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": reason,
                "indicators": indicators,
                "setups_checked": setups_checked,
                "near_misses": [sr.to_dict() for sr in near_misses[:3]],
                "funnel": dict(self._funnel),
            }
            return []

        # Pick best by weighted score
        best_sr = max(tradeable, key=lambda s: s.weighted_score)
        best = best_sr.setup_result

        # ── Fibonacci confidence modifier ──
        if fib_data.get("at_fib", False):
            fib_trend = fib_data.get("trend", "unknown")
            nearest = fib_data.get("nearest_level", "")
            dist_pct = fib_data.get("fib_distance_pct", 999)
            trend_aligned = (
                (fib_trend == "up" and best.side == OrderSide.LONG) or
                (fib_trend == "down" and best.side == OrderSide.SHORT)
            )
            if trend_aligned and dist_pct < 0.15:
                if nearest in ("0.500", "0.618"):
                    best.confidence = min(best.confidence + 12, 100)
                    best.confirmations.append(f"Fib {nearest} level (golden zone)")
                elif nearest in ("0.382", "0.786"):
                    best.confidence = min(best.confidence + 8, 100)
                    best.confirmations.append(f"Fib {nearest} level")
                else:
                    best.confidence = min(best.confidence + 5, 100)
                    best.confirmations.append(f"Near Fib {nearest}")

        # ── CHOCH filter (hard block — structural break is risk-critical) ──
        if choch_data.get("choch_detected", False):
            choch_dir = choch_data.get("direction")
            choch_strength = choch_data.get("strength", 0)
            choch_bars = choch_data.get("bars_ago", 999)
            if choch_bars <= 10 and choch_strength >= 60:
                conflicts = (
                    (choch_dir == "bearish" and best.side == OrderSide.LONG) or
                    (choch_dir == "bullish" and best.side == OrderSide.SHORT)
                )
                if conflicts:
                    self._funnel["blocked_htf"] += 1
                    self.last_scan_status[symbol] = {
                        "time": now_iso, "signal": False,
                        "reason": f"CHOCH filter: {choch_dir} structure break blocks {best.side.value} ({best.name})",
                        "indicators": indicators,
                        "setups_checked": setups_checked,
                        "funnel": dict(self._funnel),
                    }
                    return []
                # CHOCH aligns → boost
                if (choch_dir == "bullish" and best.side == OrderSide.LONG) or \
                   (choch_dir == "bearish" and best.side == OrderSide.SHORT):
                    best.confidence = min(best.confidence + 10, 100)
                    best.confirmations.append(f"CHOCH {choch_dir} (str={choch_strength})")

        # ── Session-aware confidence threshold ──
        # Weak signals (tier=weak) get a lower threshold — they're still valid
        session_min = getattr(self, '_session_min_confidence', self.min_confidence)
        effective_min = max(self.min_confidence, session_min)
        # Lower the floor for strong-weighted scanners
        if best_sr.scanner_weight >= 1.2:
            effective_min = max(effective_min - 5, 55)
        if best.confidence < effective_min and best_sr.tier != TIER_WEAK:
            session_name = getattr(self, '_current_session', 'unknown')
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"Best setup '{best.name}' confidence {best.confidence} < {effective_min} threshold (session: {session_name})",
                "indicators": indicators,
                "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            return []

        # ── FEE-AWARE TRADE FILTER (hard block — no edge below fee threshold) ──
        _fee_atr = getattr(self, '_confirm_atr', 0) or best.atr
        if _fee_atr > 0 and best.entry_price > 0:
            expected_move_pct = (_fee_atr / best.entry_price) * 100
            round_trip_fee_pct = 0.18
            if expected_move_pct < round_trip_fee_pct * 2:
                self._funnel["blocked_cost"] += 1
                self.last_scan_status[symbol] = {
                    "time": now_iso, "signal": False,
                    "reason": f"FEE FILTER: expected move {expected_move_pct:.3f}% < 2× fees ({round_trip_fee_pct*2:.3f}%)",
                    "indicators": indicators,
                    "setups_checked": setups_checked,
                    "funnel": dict(self._funnel),
                }
                return []

        # ── IMPULSE-CHASE FILTER (converted to soft penalty for most cases) ──
        last_row = df.iloc[-1]
        _impulse_atr = getattr(self, '_confirm_atr', 0) or best.atr
        impulse_penalty = 0
        if _impulse_atr > 0 and best.entry_price > 0:
            # 1. Large candle body → penalty (not hard block unless extreme)
            candle_body = abs(float(last_row.get("close", 0)) - float(last_row.get("open", 0)))
            if candle_body > _impulse_atr * 1.5:
                # Extreme chasing — hard block
                self.last_scan_status[symbol] = {
                    "time": now_iso, "signal": False,
                    "reason": f"IMPULSE: extreme candle body {candle_body:.2f} > 1.5× ATR — hard block",
                    "indicators": indicators,
                    "setups_checked": setups_checked,
                    "funnel": dict(self._funnel),
                }
                return []
            elif candle_body > _impulse_atr * 1.0:
                impulse_penalty += 10
                best_sr.penalties.append(f"Large candle body ({candle_body:.1f} > 1.0×ATR)")

            # 2. Price stretched from EMA8 → penalty (not hard block)
            ema8_val = float(last_row.get("ema_8", 0))
            close_val = float(last_row.get("close", 0))
            if ema8_val > 0:
                dist_from_ema8 = abs(close_val - ema8_val)
                if dist_from_ema8 > _impulse_atr * 0.8:
                    impulse_penalty += 15
                    best_sr.penalties.append(f"Stretched from EMA8 ({dist_from_ema8:.1f} > 0.8×ATR)")
                elif dist_from_ema8 > _impulse_atr * 0.5:
                    impulse_penalty += 8
                    best_sr.penalties.append(f"Extended from EMA8 ({dist_from_ema8:.1f} > 0.5×ATR)")

            # 3. Three consecutive expansion candles → penalty
            if len(df) >= 4:
                bodies = []
                for i in range(-3, 0):
                    row = df.iloc[i]
                    body = float(row.get("close", 0)) - float(row.get("open", 0))
                    bodies.append(body)
                same_dir = all(b > 0 for b in bodies) or all(b < 0 for b in bodies)
                all_large = all(abs(b) > _impulse_atr * 0.6 for b in bodies)
                if same_dir and all_large:
                    impulse_penalty += 20
                    best_sr.penalties.append("3 consecutive expansion candles")

        # Apply impulse penalty to confidence
        if impulse_penalty > 0:
            best.confidence = max(best.confidence - impulse_penalty, 0)
            # Re-check: if penalty drops below minimum, reject
            if best.confidence < 50:
                self.last_scan_status[symbol] = {
                    "time": now_iso, "signal": False,
                    "reason": f"IMPULSE penalty dropped confidence to {best.confidence} (penalty: {impulse_penalty}pts)",
                    "indicators": indicators,
                    "setups_checked": setups_checked,
                    "funnel": dict(self._funnel),
                }
                return []

        # ── Build Signal ──
        signal = self._build_signal(symbol, best, htf_bias, fib_data=fib_data, choch_data=choch_data)

        # Tag signal with tier and scanner weight info
        if signal.metadata is None:
            signal.metadata = {}
        signal.metadata["signal_tier"] = best_sr.tier
        signal.metadata["scanner_weight"] = best_sr.scanner_weight
        signal.metadata["scanner_status"] = best_sr.scanner_status
        signal.metadata["weighted_score"] = best_sr.weighted_score
        signal.metadata["impulse_penalty"] = impulse_penalty
        signal.metadata["penalties"] = best_sr.penalties

        self._last_signal_time[symbol] = now
        self._signal_count_hr.append(now)

        self.last_scan_status[symbol] = {
            "time": now_iso, "signal": True,
            "reason": f"Signal: {best.name} {best.side.value.upper()} [{best_sr.tier}] w={best_sr.scanner_weight:.1f}x",
            "indicators": indicators,
            "setups_checked": setups_checked,
            "setup_name": best.name,
            "confidence": best.confidence,
            "tier": best_sr.tier,
            "weighted_score": best_sr.weighted_score,
            "scanner_weight": best_sr.scanner_weight,
            "funnel": dict(self._funnel),
        }

        logger.info(
            "SCALP %s [%s]: %s %s | conf=%d w=%.1fx grade=%s | %s",
            best.name, best_sr.tier.upper(), best.side.value.upper(), symbol,
            signal.confidence, best_sr.scanner_weight, signal.grade.value,
            ", ".join(best.confirmations),
        )
        return [signal]

    def _estimate_proximity_score(self, diag: str) -> int:
        """Estimate how close a non-triggering scanner was to firing.

        Returns 0-49 score based on diagnostic text analysis.
        Used to identify near-misses for dashboard visibility.
        """
        if not diag:
            return 10
        # Count how many conditions are described as "checking" or "met"
        score = 15  # base: scanner ran
        diag_lower = diag.lower()
        if "conditions met" in diag_lower or "checking" in diag_lower:
            score += 20  # Most conditions passed
        if "detected" in diag_lower:
            score += 10
        # Penalty indicators
        pipe_count = diag.count("|")
        if pipe_count == 0:
            score += 10  # Only one issue
        elif pipe_count == 1:
            score += 5   # Two issues
        # Specific near-miss patterns
        if "away" in diag_lower and any(c.isdigit() for c in diag):
            score += 5  # Quantified distance — close
        return min(score, 49)  # Never reach 50 (that's weak signal territory)

    # ------------------------------------------------------------------
    # Indicator computation (lightweight for 1m data)
    # ------------------------------------------------------------------

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute a lean set of indicators for scalp analysis."""
        result = df.copy()

        # EMAs
        result["ema_8"] = calc_ema(df, self.ema_fast)
        result["ema_21"] = calc_ema(df, self.ema_slow)
        result["ema_50"] = calc_ema(df, self.ema_trend)

        # RSI
        result["rsi"] = calc_rsi(df, self.rsi_period)

        # MACD (fast settings for scalp: 8, 17, 9)
        macd = calc_macd(df, fast=8, slow=17, signal=9)
        result = pd.concat([result, macd], axis=1)

        # ATR
        result["atr"] = calc_atr(df, self.atr_period)

        # Bollinger Bands
        bb = calc_bollinger_bands(df, self.bb_period, self.bb_std)
        result = pd.concat([result, bb], axis=1)

        # Supertrend
        st = calc_supertrend(df, self.st_period, self.st_mult)
        result = pd.concat([result, st], axis=1)

        # VWAP
        result["vwap"] = calc_vwap(df)

        # Volume
        result["vol_sma"] = df["volume"].rolling(20).mean()
        result["rel_vol"] = df["volume"] / result["vol_sma"].replace(0, np.nan)
        result["vol_spike"] = df["volume"] > (result["vol_sma"] * 1.5)

        return result

    def _get_htf_bias(self, htf_df: Optional[pd.DataFrame]) -> int:
        """Return +1 bullish, -1 bearish, 0 neutral from HTF."""
        if htf_df is None or len(htf_df) < 50:
            return 0
        ema_21 = calc_ema(htf_df, 21).iloc[-1]
        ema_50 = calc_ema(htf_df, 50).iloc[-1]
        close = htf_df["close"].iloc[-1]
        if close > ema_21 > ema_50:
            return 1
        elif close < ema_21 < ema_50:
            return -1
        return 0

    # ==================================================================
    # SETUP 1: EMA Momentum Cross
    # ==================================================================

    def _scan_ema_momentum(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """EMA 8/21 cross with RSI and volume confirmation.

        LONG:  EMA8 crosses above EMA21, RSI 40-70, volume above avg
        SHORT: EMA8 crosses below EMA21, RSI 30-60, volume above avg
        """
        if len(df) < 3:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]

        ema8_now = last["ema_8"]
        ema21_now = last["ema_21"]
        ema8_prev = prev["ema_8"]
        ema21_prev = prev["ema_21"]
        rsi = last["rsi"]
        rel_vol = last.get("rel_vol", 1.0)
        atr = last["atr"]
        close = last["close"]

        if atr <= 0 or np.isnan(atr):
            return None

        # Detect cross
        bullish_cross = ema8_prev <= ema21_prev and ema8_now > ema21_now
        bearish_cross = ema8_prev >= ema21_prev and ema8_now < ema21_now

        if not bullish_cross and not bearish_cross:
            return None

        # DATA: ema_momentum LONG = 100% WR, SHORT = 0% WR (0 favorable movement)
        # Block bearish crosses entirely — EMA cross shorts don't work in this market
        if bearish_cross:
            return None

        side = OrderSide.LONG if bullish_cross else OrderSide.SHORT
        confs = []
        score = 0

        # Cross itself = 30 pts
        confs.append(f"EMA 8/21 {'bullish' if bullish_cross else 'bearish'} cross")
        score += 30

        # RSI in sweet spot
        if side == OrderSide.LONG and 40 < rsi < 70:
            confs.append(f"RSI {rsi:.0f} (healthy)")
            score += 15
        elif side == OrderSide.SHORT and 30 < rsi < 60:
            confs.append(f"RSI {rsi:.0f} (healthy)")
            score += 15

        # Volume above average
        if rel_vol > 1.2:
            confs.append(f"Volume {rel_vol:.1f}x avg")
            score += 15
        elif rel_vol > 0.8:
            score += 5

        # Price above/below EMA 50 (trend alignment)
        if side == OrderSide.LONG and close > last["ema_50"]:
            confs.append("Above EMA 50")
            score += 10
        elif side == OrderSide.SHORT and close < last["ema_50"]:
            confs.append("Below EMA 50")
            score += 10

        # HTF alignment bonus
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15
        elif htf_bias == 0:
            score += 5

        # 5m confirmation alignment bonus
        if confirm_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("5m aligned")
            score += 10

        # Supertrend agreement
        st_dir = last.get("supertrend_dir", 0)
        if (side == OrderSide.LONG and st_dir == 1) or (side == OrderSide.SHORT and st_dir == -1):
            confs.append("Supertrend agrees")
            score += 5

        confidence = min(score, 100)
        sl = close - atr * self.sl_atr_mult if side == OrderSide.LONG else close + atr * self.sl_atr_mult

        return _SetupResult(
            name="ema_momentum",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # SETUP 2: VWAP Bounce
    # ==================================================================

    def _scan_vwap_bounce(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Price touches VWAP and bounces with rejection wick + volume.

        LONG:  Price dips to/below VWAP, closes above with long lower wick
        SHORT: Price spikes to/above VWAP, closes below with long upper wick
        """
        if len(df) < 3:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        vwap = last.get("vwap", 0)
        atr = last["atr"]
        close = last["close"]
        open_ = last["open"]
        high = last["high"]
        low = last["low"]

        if vwap <= 0 or atr <= 0 or np.isnan(vwap) or np.isnan(atr):
            return None

        body = abs(close - open_)
        full_range = high - low
        if full_range == 0:
            return None

        lower_wick = min(open_, close) - low
        upper_wick = high - max(open_, close)

        # Distance from VWAP (as fraction of ATR)
        dist_to_vwap = abs(close - vwap) / atr

        # Must be near VWAP (within 0.5 ATR)
        if dist_to_vwap > 0.5:
            return None

        confs = []
        score = 0
        side = None

        # LONG: price dipped below/near VWAP and bounced
        if close > vwap and low <= vwap * 1.001 and lower_wick > body * 0.8:
            side = OrderSide.LONG
            confs.append("VWAP bounce (bullish)")
            score += 30

            if lower_wick > body * 1.5:
                confs.append("Strong rejection wick")
                score += 15
            else:
                score += 5

        # SHORT: price spiked above/near VWAP and rejected
        elif close < vwap and high >= vwap * 0.999 and upper_wick > body * 0.8:
            side = OrderSide.SHORT
            confs.append("VWAP rejection (bearish)")
            score += 30

            if upper_wick > body * 1.5:
                confs.append("Strong rejection wick")
                score += 15
            else:
                score += 5

        if side is None:
            return None

        # Volume confirmation
        rel_vol = last.get("rel_vol", 1.0)
        if rel_vol > 1.5:
            confs.append(f"Volume spike {rel_vol:.1f}x")
            score += 15
        elif rel_vol > 1.0:
            score += 5

        # RSI support
        rsi = last["rsi"]
        if side == OrderSide.LONG and rsi < 45:
            confs.append(f"RSI oversold zone ({rsi:.0f})")
            score += 10
        elif side == OrderSide.SHORT and rsi > 55:
            confs.append(f"RSI overbought zone ({rsi:.0f})")
            score += 10

        # HTF alignment
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        confidence = min(score, 100)
        sl = close - atr * self.sl_atr_mult if side == OrderSide.LONG else close + atr * self.sl_atr_mult

        return _SetupResult(
            name="vwap_bounce",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # SETUP 2b: Trend Continuation (most frequent signal)
    # ==================================================================

    def _scan_trend_continuation(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Trend continuation: EMA alignment + RSI pullback + candle in direction.

        This is the bread-and-butter setup that fires in any trending market.
        It doesn't need a cross or flip — just existing trend + slight pullback.

        LONG:  EMA8 > EMA21, RSI pulled back from higher levels, bullish candle
        SHORT: EMA8 < EMA21, RSI pulled back from lower levels, bearish candle
        """
        if len(df) < 5:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        atr = last["atr"]
        close = last["close"]
        open_ = last["open"]

        if atr <= 0 or np.isnan(atr):
            return None

        ema8 = last["ema_8"]
        ema21 = last["ema_21"]
        ema50 = last["ema_50"]
        rsi = last["rsi"]
        rsi_prev = prev["rsi"]
        macd_hist = last.get("macd_hist", 0)
        st_dir = last.get("supertrend_dir", 0)

        if np.isnan(rsi) or np.isnan(ema8):
            return None

        confs = []
        score = 0
        side = None

        # --- LONG: Uptrend + genuine pullback + bullish reversal candle ---
        if ema8 > ema21:
            # Trend exists
            ema_gap_pct = (ema8 - ema21) / ema21 * 100 if ema21 > 0 else 0

            # Require a REAL pullback: price must have dipped toward EMA zone
            # Not just RSI 40-65 which is true almost always in an uptrend
            price_pulled_back = close <= ema8 * 1.001 and close > ema21  # price near/below EMA8 but above EMA21
            rsi_recovering = 40 < rsi < 58 and rsi > rsi_prev  # RSI turning up from pullback
            bullish_candle = close > open_

            if price_pulled_back and rsi_recovering and bullish_candle:
                side = OrderSide.LONG
                confs.append("Uptrend continuation")
                score += 25

                # Bullish candle
                confs.append("Bullish candle")
                score += 10

                # EMA alignment strength — require meaningful gap
                if ema_gap_pct > 0.03:
                    confs.append(f"EMA8>21 by {ema_gap_pct:.3f}%")
                    score += 10
                elif ema_gap_pct < 0.01:
                    score -= 5  # Weak trend, penalize

                # Price in the ideal pullback zone (between EMA8 and EMA21)
                if close < ema8 and close > ema21:
                    confs.append("Price pullback to EMA zone")
                    score += 15  # Best entry zone

                # MACD positive
                if macd_hist > 0:
                    confs.append("MACD positive")
                    score += 10

                # Supertrend bullish
                if st_dir == 1:
                    confs.append("Supertrend bullish")
                    score += 10

        # --- SHORT: Downtrend + genuine pullback + bearish reversal candle ---
        elif ema8 < ema21:
            ema_gap_pct = (ema21 - ema8) / ema21 * 100 if ema21 > 0 else 0

            price_pulled_back = close >= ema8 * 0.999 and close < ema21
            rsi_recovering = 42 < rsi < 60 and rsi < rsi_prev
            bearish_candle = close < open_

            if price_pulled_back and rsi_recovering and bearish_candle:
                side = OrderSide.SHORT
                confs.append("Downtrend continuation")
                score += 25

                confs.append("Bearish candle")
                score += 10

                if ema_gap_pct > 0.03:
                    confs.append(f"EMA21>8 by {ema_gap_pct:.3f}%")
                    score += 10
                elif ema_gap_pct < 0.01:
                    score -= 5

                if close < ema8:
                    confs.append("Price < EMA8")
                    score += 5
                elif close < ema21:
                    confs.append("Price pullback to EMA zone")
                    score += 10

                if macd_hist < 0:
                    confs.append("MACD negative")
                    score += 10

                if st_dir == -1:
                    confs.append("Supertrend bearish")
                    score += 10

        if side is None:
            return None

        # HTF alignment bonus
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        # 5m confirmation
        if confirm_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("5m aligned")
            score += 10

        # Volume (any volume is fine, bonus for above average)
        rel_vol = last.get("rel_vol", 1.0)
        if not np.isnan(rel_vol) and rel_vol > 1.0:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 5

        confidence = min(score, 100)
        sl = close - atr * self.sl_atr_mult if side == OrderSide.LONG else close + atr * self.sl_atr_mult

        return _SetupResult(
            name="trend_continuation",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # SETUP 3: RSI Divergence
    # ==================================================================

    def _scan_rsi_divergence(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Detect RSI divergence (price vs RSI discrepancy).

        Bullish divergence: price lower low + RSI higher low → LONG
        Bearish divergence: price higher high + RSI lower high → SHORT
        """
        lookback = min(self.div_lookback, len(df) - 2)
        if lookback < 5:
            return None

        last = df.iloc[-1]
        atr = last["atr"]
        close = last["close"]
        rsi_now = last["rsi"]

        if atr <= 0 or np.isnan(atr) or np.isnan(rsi_now):
            return None

        window = df.iloc[-(lookback + 1):]
        prices = window["close"].values
        rsis = window["rsi"].values

        if np.any(np.isnan(rsis)):
            return None

        confs = []
        score = 0
        side = None

        # Find swing lows/highs in the window
        # Bullish: current price near recent low, RSI higher than at that low
        price_min_idx = np.argmin(prices[:-1])  # exclude current bar
        price_min = prices[price_min_idx]
        rsi_at_min = rsis[price_min_idx]

        # Bearish: current price near recent high, RSI lower than at that high
        price_max_idx = np.argmax(prices[:-1])
        price_max = prices[price_max_idx]
        rsi_at_max = rsis[price_max_idx]

        # Pre-calculate divergence strength
        rsi_diff_bull = rsi_now - rsi_at_min
        rsi_diff_bear = rsi_at_max - rsi_now

        # Bullish divergence - require STRONG divergence
        if (close <= price_min * 1.001  # price very near/below the recent low
            and rsi_diff_bull >= 8       # RSI must be 8+ points higher (was 3)
            and rsi_now < 40             # RSI must be in oversold territory (was 45)
            and rsi_at_min < 30):        # Original RSI was truly oversold
            side = OrderSide.LONG
            confs.append(f"Bullish RSI divergence ({rsi_at_min:.0f}→{rsi_now:.0f})")
            score += 35

            if rsi_now < 30:
                confs.append("RSI deep oversold")
                score += 15
            if rsi_diff_bull >= 15:
                confs.append("Strong divergence")
                score += 10

        # Bearish divergence - require STRONG divergence
        elif (close >= price_max * 0.999  # price very near/above the recent high
              and rsi_diff_bear >= 8       # RSI must be 8+ points lower (was 3)
              and rsi_now > 60             # RSI must be in overbought territory (was 55)
              and rsi_at_max > 70):        # Original RSI was truly overbought
            side = OrderSide.SHORT
            confs.append(f"Bearish RSI divergence ({rsi_at_max:.0f}→{rsi_now:.0f})")
            score += 35

            if rsi_now > 70:
                confs.append("RSI deep overbought")
                score += 15
            if rsi_diff_bear >= 15:
                confs.append("Strong divergence")
                score += 10

        if side is None:
            return None

        # Volume confirmation
        rel_vol = last.get("rel_vol", 1.0)
        if rel_vol > 1.2:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 10

        # MACD divergence agreement
        macd_hist = last.get("macd_hist", 0)
        if side == OrderSide.LONG and macd_hist > 0:
            confs.append("MACD hist positive")
            score += 10
        elif side == OrderSide.SHORT and macd_hist < 0:
            confs.append("MACD hist negative")
            score += 10

        # HTF alignment
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        # Candle confirmation (bullish/bearish close)
        if side == OrderSide.LONG and close > last["open"]:
            confs.append("Bullish candle")
            score += 10
        elif side == OrderSide.SHORT and close < last["open"]:
            confs.append("Bearish candle")
            score += 10

        confidence = min(score, 100)
        sl = close - atr * 1.2 if side == OrderSide.LONG else close + atr * 1.2  # slightly wider for reversal

        return _SetupResult(
            name="rsi_divergence",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # SETUP 4: Supertrend Flip
    # ==================================================================

    def _scan_supertrend_flip(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Supertrend direction change + MACD confirmation.

        LONG:  Supertrend flips from -1 to +1 (bearish → bullish)
        SHORT: Supertrend flips from +1 to -1 (bullish → bearish)
        """
        if len(df) < 3:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]

        st_now = last.get("supertrend_dir", 0)
        st_prev = prev.get("supertrend_dir", 0)
        atr = last["atr"]
        close = last["close"]

        if atr <= 0 or np.isnan(atr):
            return None

        # Detect flip
        bullish_flip = st_prev == -1 and st_now == 1
        bearish_flip = st_prev == 1 and st_now == -1

        if not bullish_flip and not bearish_flip:
            return None

        side = OrderSide.LONG if bullish_flip else OrderSide.SHORT
        confs = []
        score = 0
        required_confirms = 0  # Must have at least 2 confirmations beyond the flip

        confs.append(f"Supertrend flip {'bullish' if bullish_flip else 'bearish'}")
        score += 20  # Reduced base (was 30) — flip alone is not enough

        # MACD confirmation (REQUIRED — no flip without momentum)
        macd_hist = last.get("macd_hist", 0)
        if side == OrderSide.LONG and macd_hist > 0:
            confs.append("MACD positive")
            score += 15
            required_confirms += 1
        elif side == OrderSide.SHORT and macd_hist < 0:
            confs.append("MACD negative")
            score += 15
            required_confirms += 1

        # EMA alignment
        if side == OrderSide.LONG and last["ema_8"] > last["ema_21"]:
            confs.append("EMA 8 > 21")
            score += 10
            required_confirms += 1
        elif side == OrderSide.SHORT and last["ema_8"] < last["ema_21"]:
            confs.append("EMA 8 < 21")
            score += 10
            required_confirms += 1

        # Volume — require above-average volume for flip validation
        rel_vol = last.get("rel_vol", 1.0)
        if rel_vol > 1.5:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 15
            required_confirms += 1
        elif rel_vol > 1.2:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 5

        # RSI must be favorable (not extreme against the trade)
        rsi = last["rsi"]
        if side == OrderSide.LONG and 30 < rsi < 60:
            confs.append(f"RSI healthy ({rsi:.0f})")
            score += 10
            required_confirms += 1
        elif side == OrderSide.SHORT and 40 < rsi < 70:
            confs.append(f"RSI healthy ({rsi:.0f})")
            score += 10
            required_confirms += 1

        # HTF alignment (important for flips)
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15
            required_confirms += 1

        # 5m confirmation
        if confirm_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("5m aligned")
            score += 10
            required_confirms += 1

        # GATE: Reject if fewer than 2 extra confirmations
        if required_confirms < 2:
            return None

        confidence = min(score, 100)
        sl = close - atr * self.sl_atr_mult if side == OrderSide.LONG else close + atr * self.sl_atr_mult

        return _SetupResult(
            name="supertrend_flip",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # SETUP 5: Bollinger Band Squeeze Breakout
    # ==================================================================

    def _scan_bb_squeeze(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """BB squeeze releasing with directional momentum.

        Detect when Bollinger bandwidth was compressed (squeeze) and is now
        expanding, with a directional candle + volume to confirm breakout.
        """
        if len(df) < 25:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        atr = last["atr"]
        close = last["close"]

        if atr <= 0 or np.isnan(atr):
            return None

        bw_now = last.get("bb_bandwidth", 0)
        bw_prev = prev.get("bb_bandwidth", 0)
        pct_b = last.get("bb_pct_b", 0.5)

        if np.isnan(bw_now) or np.isnan(bw_prev):
            return None

        # Check for squeeze: recent bandwidth was in bottom 25% of last 100 bars
        bw_series = df["bb_bandwidth"].dropna().iloc[-100:]
        if len(bw_series) < 20:
            return None
        bw_25th = bw_series.quantile(0.25)

        # Was recently squeezed (prev bar in bottom 25%) and now expanding
        was_squeezed = bw_prev <= bw_25th
        is_expanding = bw_now > bw_prev * 1.05  # 5% bandwidth increase

        if not (was_squeezed and is_expanding):
            return None

        confs = []
        score = 0
        side = None

        # Direction from %B and candle
        if pct_b > 0.75 and close > last["open"]:
            side = OrderSide.LONG
            confs.append("BB squeeze breakout UP")
            score += 30
        elif pct_b < 0.25 and close < last["open"]:
            side = OrderSide.SHORT
            confs.append("BB squeeze breakout DOWN")
            score += 30
        else:
            return None

        # Volume must confirm
        rel_vol = last.get("rel_vol", 1.0)
        if rel_vol > 1.5:
            confs.append(f"Volume surge {rel_vol:.1f}x")
            score += 20
        elif rel_vol > 1.0:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 10
        else:
            return None  # No volume = fake breakout

        # MACD confirmation
        macd_hist = last.get("macd_hist", 0)
        if side == OrderSide.LONG and macd_hist > 0:
            confs.append("MACD positive")
            score += 10
        elif side == OrderSide.SHORT and macd_hist < 0:
            confs.append("MACD negative")
            score += 10

        # HTF alignment
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        # Strong body candle
        body = abs(close - last["open"])
        full_range = last["high"] - last["low"]
        if full_range > 0 and body / full_range > 0.6:
            confs.append("Strong body candle")
            score += 10

        confidence = min(score, 100)
        sl = close - atr * self.sl_atr_mult if side == OrderSide.LONG else close + atr * self.sl_atr_mult

        return _SetupResult(
            name="bb_squeeze",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # SETUP 6: RSI Extreme Reversal (Oversold/Overbought Bounce)
    # ==================================================================

    def _scan_rsi_extreme(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Catch reversals from extreme oversold/overbought conditions.

        LONG:  RSI < 30 (oversold) + bullish reversal candle + RSI turning up
        SHORT: RSI > 70 (overbought) + bearish reversal candle + RSI turning down

        This fills the gap when all other scanners fail during extreme moves.
        """
        if len(df) < 5:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]
        atr = last["atr"]
        close = last["close"]
        open_ = last["open"]

        if atr <= 0 or np.isnan(atr):
            return None

        rsi = last["rsi"]
        rsi_prev = prev["rsi"]
        rsi_prev2 = prev2["rsi"]

        if np.isnan(rsi) or np.isnan(rsi_prev):
            return None

        confs = []
        score = 0
        side = None

        # --- OVERSOLD BOUNCE (LONG) ---
        # RSI was deeply oversold and is now turning up
        if rsi < 35 and rsi > rsi_prev and rsi_prev < 35:
            bullish_candle = close > open_
            # Price showing rejection (lower wick > body)
            body = abs(close - open_)
            lower_wick = min(close, open_) - last["low"]
            has_rejection = lower_wick > body * 0.5 if body > 0 else lower_wick > atr * 0.3

            if bullish_candle or has_rejection:
                side = OrderSide.LONG
                confs.append(f"RSI oversold bounce ({rsi:.0f})")
                score += 30

                if rsi < 25:
                    confs.append("Extreme oversold")
                    score += 10

                if bullish_candle:
                    confs.append("Bullish candle")
                    score += 10

                if has_rejection:
                    confs.append("Lower wick rejection")
                    score += 10

        # --- OVERBOUGHT REVERSAL (SHORT) ---
        elif rsi > 65 and rsi < rsi_prev and rsi_prev > 65:
            bearish_candle = close < open_
            body = abs(close - open_)
            upper_wick = last["high"] - max(close, open_)
            has_rejection = upper_wick > body * 0.5 if body > 0 else upper_wick > atr * 0.3

            if bearish_candle or has_rejection:
                side = OrderSide.SHORT
                confs.append(f"RSI overbought reversal ({rsi:.0f})")
                score += 30

                if rsi > 75:
                    confs.append("Extreme overbought")
                    score += 10

                if bearish_candle:
                    confs.append("Bearish candle")
                    score += 10

                if has_rejection:
                    confs.append("Upper wick rejection")
                    score += 10

        if side is None:
            return None

        # Volume confirmation
        rel_vol = last.get("rel_vol", 1.0)
        if not np.isnan(rel_vol) and rel_vol > 1.2:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 15
        elif not np.isnan(rel_vol) and rel_vol > 0.8:
            score += 5

        # Supertrend alignment (bonus, not required)
        st_dir = last.get("supertrend_dir", 0)
        if (side == OrderSide.LONG and st_dir == 1) or (side == OrderSide.SHORT and st_dir == -1):
            confs.append("Supertrend agrees")
            score += 10

        # BB %B at extreme (confirms oversold/overbought at BB boundary)
        pct_b = last.get("bb_pct_b", 0.5)
        if not np.isnan(pct_b):
            if side == OrderSide.LONG and pct_b < 0.1:
                confs.append("At lower Bollinger Band")
                score += 10
            elif side == OrderSide.SHORT and pct_b > 0.9:
                confs.append("At upper Bollinger Band")
                score += 10

        # HTF alignment (bonus)
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 10

        # 5m confirmation
        if confirm_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("5m aligned")
            score += 5

        confidence = min(score, 100)
        sl = close - atr * self.sl_atr_mult if side == OrderSide.LONG else close + atr * self.sl_atr_mult

        return _SetupResult(
            name="rsi_extreme",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # SETUP 7: Momentum Ride (Trend Already In Motion)
    # ==================================================================

    def _scan_momentum_ride(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Catch strong established trends with all indicators aligned.

        Unlike EMA Momentum (needs cross) or Trend Continuation (needs pullback),
        this fires when the trend is ALREADY running with confirmed momentum.

        LONG:  EMA8>21>50, RSI 58-82 rising, MACD accelerating, volume > 1.5x
        SHORT: EMA8<21<50, RSI 18-42 falling, MACD declining, volume > 1.5x

        LONG-ONLY initially (SHORT disabled — EMA momentum SHORT was 33% WR).
        """
        if len(df) < 10:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        atr = last["atr"]
        close = last["close"]

        if atr <= 0 or np.isnan(atr):
            return None

        ema8 = last["ema_8"]
        ema21 = last["ema_21"]
        ema50 = last["ema_50"]
        rsi = last["rsi"]
        rsi_prev = prev["rsi"]
        macd_hist = last.get("macd_hist", 0)
        macd_hist_prev = prev.get("macd_hist", 0)
        rel_vol = last.get("rel_vol", 1.0)
        st_dir = last.get("supertrend_dir", 0)

        if np.isnan(rsi) or np.isnan(ema8):
            return None

        confs = []
        score = 0
        side = None

        # --- LONG: Full trend alignment + momentum ---
        if (ema8 > ema21 > ema50                      # Full EMA stack
            and 58 < rsi < 82                           # Strong but not blow-off
            and rsi > rsi_prev                          # RSI still rising
            and macd_hist > 0                           # MACD positive
            and macd_hist > macd_hist_prev              # MACD accelerating
            and rel_vol > 1.5                           # Strong volume
            and close > ema8):                          # Price riding above fast EMA

            # Check price not too stretched from EMA8 (< 0.4% for BTC, scaled)
            dist_from_ema8_pct = abs(close - ema8) / close * 100 if close > 0 else 999
            if dist_from_ema8_pct > 0.4:
                return None  # Too stretched, would be chasing

            # Don't enter on impulse candles
            body = abs(close - last["open"])
            if body > atr * 1.0:
                return None  # Impulse candle, wait for pause

            side = OrderSide.LONG

            # Score
            confs.append("Full EMA stack bullish (8>21>50)")
            score += 25

            confs.append(f"RSI {rsi:.0f} rising ({rsi_prev:.0f}→{rsi:.0f})")
            score += 15

            confs.append(f"MACD accelerating ({macd_hist_prev:.2f}→{macd_hist:.2f})")
            score += 15

            confs.append(f"Volume {rel_vol:.1f}x")
            score += 15
            if rel_vol > 2.5:
                score += 5  # Extra for very strong volume

            if htf_bias == 1:
                confs.append("HTF aligned bullish")
                score += 15

            if confirm_bias == 1:
                confs.append("5m aligned")
                score += 10

            if st_dir == 1:
                confs.append("Supertrend bullish")
                score += 5

        # SHORT disabled for now (data shows EMA momentum SHORT = 33% WR)

        if side is None:
            return None

        confidence = min(score, 100)
        sl = close - atr * self.sl_atr_mult if side == OrderSide.LONG else close + atr * self.sl_atr_mult

        return _SetupResult(
            name="momentum_ride",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # SETUP 8: BB Band Walk (Bollinger Band Breakout Continuation)
    # ==================================================================

    def _scan_bb_band_walk(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Price breaking and sustaining above/below Bollinger Band.

        "Walking the band" — when price rides the upper/lower BB with volume,
        this is a classic institutional momentum pattern.

        LONG:  Close > BB_upper for 2+ candles, volume > 1.3x, EMA aligned
        SHORT: Close < BB_lower for 2+ candles, volume > 1.3x, EMA aligned
        """
        if len(df) < 25:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        atr = last["atr"]
        close = last["close"]

        if atr <= 0 or np.isnan(atr):
            return None

        bb_upper = last.get("bb_upper", 0)
        bb_lower = last.get("bb_lower", 0)
        bb_mid = last.get("bb_middle", (bb_upper + bb_lower) / 2 if bb_upper and bb_lower else 0)
        pct_b = last.get("bb_pct_b", 0.5)
        bw_now = last.get("bb_bandwidth", 0)
        rel_vol = last.get("rel_vol", 1.0)
        rsi = last["rsi"]
        ema8 = last["ema_8"]
        ema21 = last["ema_21"]
        macd_hist = last.get("macd_hist", 0)

        if np.isnan(bb_upper) or np.isnan(rsi) or bb_upper <= 0:
            return None

        prev_close = prev["close"]
        prev_bb_upper = prev.get("bb_upper", 0)
        prev_bb_lower = prev.get("bb_lower", 0)

        confs = []
        score = 0
        side = None

        # --- LONG: Price above upper BB for 2+ candles ---
        near_prev_upper = prev_close >= prev_bb_upper * 0.999 if prev_bb_upper > 0 else False
        if (close > bb_upper                           # Currently above upper band
            and near_prev_upper                        # Previous candle also near/above
            and pct_b > 1.0                            # Numerically above band
            and rel_vol > 1.3                          # Volume confirms breakout
            and 55 < rsi < 85                          # Bullish but not extreme blow-off
            and ema8 > ema21):                         # Trend confirms

            side = OrderSide.LONG
            confs.append("BB band walk — price above upper band")
            score += 25

            confs.append("2+ candles at/above upper BB")
            score += 15

            confs.append(f"Volume {rel_vol:.1f}x")
            score += 15
            if rel_vol > 2.0:
                score += 5

            if ema8 > ema21:
                confs.append("EMA8 > EMA21")
                score += 10

            if macd_hist > 0:
                confs.append("MACD positive")
                score += 10

            if htf_bias == 1:
                confs.append("HTF aligned")
                score += 10

            # Strong body candle (not just wick spike)
            body = abs(close - last["open"])
            full_range = last["high"] - last["low"]
            if full_range > 0 and body / full_range > 0.5 and close > last["open"]:
                confs.append("Strong bullish body")
                score += 5

        # --- SHORT: Price below lower BB for 2+ candles ---
        near_prev_lower = prev_close <= prev_bb_lower * 1.001 if prev_bb_lower > 0 else False
        if side is None and (close < bb_lower
            and near_prev_lower
            and pct_b < 0.0
            and rel_vol > 1.3
            and 15 < rsi < 45
            and ema8 < ema21):

            side = OrderSide.SHORT
            confs.append("BB band walk — price below lower band")
            score += 25

            confs.append("2+ candles at/below lower BB")
            score += 15

            confs.append(f"Volume {rel_vol:.1f}x")
            score += 15
            if rel_vol > 2.0:
                score += 5

            if macd_hist < 0:
                confs.append("MACD negative")
                score += 10

            if htf_bias == -1:
                confs.append("HTF aligned")
                score += 10

            body = abs(close - last["open"])
            full_range = last["high"] - last["low"]
            if full_range > 0 and body / full_range > 0.5 and close < last["open"]:
                confs.append("Strong bearish body")
                score += 5

        if side is None:
            return None

        # Stop loss at BB middle band (natural invalidation)
        if side == OrderSide.LONG:
            sl_bb_mid = bb_mid - atr * 0.1  # Small buffer below midline
            sl_atr = close - atr * self.sl_atr_mult
            sl = max(sl_bb_mid, sl_atr)  # Use the tighter of the two
        else:
            sl_bb_mid = bb_mid + atr * 0.1
            sl_atr = close + atr * self.sl_atr_mult
            sl = min(sl_bb_mid, sl_atr)

        confidence = min(score, 100)
        return _SetupResult(
            name="bb_band_walk",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # SETUP 9: Post-Impulse Re-Entry (Micro-Pullback After Strong Move)
    # ==================================================================

    def _scan_post_impulse(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Catch the 1-3 candle pause after a strong impulse leg.

        After a big directional move, price typically pauses briefly before
        continuing. The impulse filter correctly blocks the initial chase;
        this scanner catches the re-entry after the impulse settles.

        LONG:  Recent bullish impulse, current candle small, price still above EMA8
        SHORT: Recent bearish impulse, current candle small, price still below EMA8

        LONG-ONLY initially (SHORT disabled based on historical data).
        """
        if len(df) < 10:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        atr = last["atr"]
        close = last["close"]

        if atr <= 0 or np.isnan(atr):
            return None

        ema8 = last["ema_8"]
        ema21 = last["ema_21"]
        rsi = last["rsi"]
        rsi_prev = prev["rsi"]
        macd_hist = last.get("macd_hist", 0)
        rel_vol = last.get("rel_vol", 1.0)

        if np.isnan(rsi) or np.isnan(ema8):
            return None

        # --- Look back 3-8 candles for a recent impulse candle ---
        recent_impulse = None
        impulse_high = 0
        impulse_idx = -1

        for i in range(3, min(9, len(df))):
            bar = df.iloc[-i]
            bar_body = abs(bar["close"] - bar["open"])
            bar_atr = bar.get("atr", atr)
            bar_vol = bar.get("rel_vol", 1.0)

            # Impulse = large body (> 0.8x ATR) with above-average volume
            if bar_body > bar_atr * 0.8 and bar_vol > 1.3:
                if bar["close"] > bar["open"]:  # Bullish impulse
                    if recent_impulse is None or bar_body > recent_impulse:
                        recent_impulse = bar_body
                        impulse_high = bar["high"]
                        impulse_idx = i

        if recent_impulse is None:
            return None  # No recent impulse found

        # --- Current candle must be small (impulse has paused) ---
        current_body = abs(close - last["open"])
        if current_body > atr * 0.5:
            return None  # Still impulsing, not paused

        # --- Price has pulled back but shallowly ---
        recent_high = max(df["high"].iloc[-impulse_idx:].values)
        pullback_depth = recent_high - close
        if pullback_depth > atr * 1.0:
            return None  # Too deep — not a micro-pullback
        if pullback_depth < 0:
            return None  # No pullback at all, price still making highs

        # --- Price still above EMA8 (trend intact) ---
        if close <= ema8:
            return None

        # --- RSI still healthy (not crashed) ---
        if rsi < 50 or rsi > 82:
            return None

        # --- MACD still positive ---
        if macd_hist <= 0:
            return None

        # --- EMA alignment ---
        if ema8 <= ema21:
            return None

        # --- Volume on pullback declining (healthy, not distribution) ---
        impulse_vol = df.iloc[-impulse_idx].get("rel_vol", 1.0)
        pullback_vol_declining = rel_vol < impulse_vol * 0.8

        # All conditions met — build the signal
        side = OrderSide.LONG
        confs = []
        score = 0

        confs.append(f"Post-impulse pause ({impulse_idx} bars ago)")
        score += 20

        confs.append(f"Small candle (body {current_body/atr:.1f}x ATR)")
        score += 15

        confs.append(f"Shallow pullback ({pullback_depth/atr:.1f}x ATR from high)")
        score += 15

        confs.append("Price above EMA8")
        score += 10

        if rsi > rsi_prev or (rsi_prev - rsi) < 3:
            confs.append(f"RSI stabilizing ({rsi:.0f})")
            score += 10

        if pullback_vol_declining:
            confs.append("Volume declining on pullback")
            score += 10

        if macd_hist > 0:
            confs.append("MACD positive")
            score += 10

        if htf_bias == 1:
            confs.append("HTF aligned")
            score += 10

        if confirm_bias == 1:
            confs.append("5m aligned")
            score += 5

        confidence = min(score, 100)

        # Stop loss below the impulse candle's low (structural)
        impulse_low = df.iloc[-impulse_idx]["low"]
        sl_structural = impulse_low - atr * 0.1
        sl_atr = close - atr * self.sl_atr_mult
        sl = max(sl_structural, sl_atr)  # Use tighter of the two

        # Cap SL at 0.5% of price (risk discipline)
        max_sl_dist = close * 0.005
        if (close - sl) > max_sl_dist:
            sl = close - max_sl_dist

        return _SetupResult(
            name="post_impulse",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # SETUP 10: Momentum Surge (DISABLED — 38% WR)
    # ==================================================================

    def _scan_momentum_surge(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """MACD histogram flip + RSI crossing 50 + volume spike.

        Catches the moment momentum shifts decisively with volume.
        """
        if len(df) < 5:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]
        atr = last["atr"]
        close = last["close"]

        if atr <= 0 or np.isnan(atr):
            return None

        macd_now = last.get("macd_hist", 0)
        macd_prev = prev.get("macd_hist", 0)
        rsi_now = last["rsi"]
        rsi_prev = prev["rsi"]

        if np.isnan(macd_now) or np.isnan(macd_prev) or np.isnan(rsi_now):
            return None

        confs = []
        score = 0
        side = None

        # MACD histogram flip
        macd_bull_flip = macd_prev <= 0 and macd_now > 0
        macd_bear_flip = macd_prev >= 0 and macd_now < 0

        # RSI crossing 50
        rsi_cross_up = rsi_prev <= 50 and rsi_now > 50
        rsi_cross_down = rsi_prev >= 50 and rsi_now < 50

        # Need at least MACD flip
        if macd_bull_flip:
            side = OrderSide.LONG
            confs.append("MACD histogram flip bullish")
            score += 25
        elif macd_bear_flip:
            side = OrderSide.SHORT
            confs.append("MACD histogram flip bearish")
            score += 25
        else:
            return None

        # RSI cross 50 — REQUIRED (not optional bonus)
        rsi_confirmed = False
        if side == OrderSide.LONG and rsi_cross_up:
            confs.append(f"RSI crossed above 50 ({rsi_now:.0f})")
            score += 20
            rsi_confirmed = True
        elif side == OrderSide.SHORT and rsi_cross_down:
            confs.append(f"RSI crossed below 50 ({rsi_now:.0f})")
            score += 20
            rsi_confirmed = True
        elif side == OrderSide.LONG and rsi_now > 55:
            # Allow if RSI already well above 50 (crossed recently)
            confs.append(f"RSI above 55 ({rsi_now:.0f})")
            score += 10
            rsi_confirmed = True
        elif side == OrderSide.SHORT and rsi_now < 45:
            confs.append(f"RSI below 45 ({rsi_now:.0f})")
            score += 10
            rsi_confirmed = True

        # Volume spike — REQUIRED (not optional bonus)
        vol_spike = last.get("vol_spike", False)
        rel_vol = last.get("rel_vol", 1.0)
        vol_confirmed = False
        if vol_spike:
            confs.append(f"Volume spike {rel_vol:.1f}x")
            score += 20
            vol_confirmed = True
        elif rel_vol > 1.3:
            confs.append(f"Volume elevated {rel_vol:.1f}x")
            score += 10
            vol_confirmed = True

        # GATE: Both RSI cross AND volume must confirm (was optional before)
        if not rsi_confirmed or not vol_confirmed:
            return None

        # Consecutive candles in direction (momentum building)
        if side == OrderSide.LONG:
            green_count = sum(1 for i in range(-3, 0) if df.iloc[i]["close"] > df.iloc[i]["open"])
            if green_count >= 2:
                confs.append(f"{green_count} consecutive green candles")
                score += 10
        else:
            red_count = sum(1 for i in range(-3, 0) if df.iloc[i]["close"] < df.iloc[i]["open"])
            if red_count >= 2:
                confs.append(f"{red_count} consecutive red candles")
                score += 10

        # HTF alignment
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        # EMA position
        if side == OrderSide.LONG and close > last["ema_21"]:
            score += 5
        elif side == OrderSide.SHORT and close < last["ema_21"]:
            score += 5

        confidence = min(score, 100)
        sl = close - atr * self.sl_atr_mult if side == OrderSide.LONG else close + atr * self.sl_atr_mult

        return _SetupResult(
            name="momentum_surge",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ------------------------------------------------------------------
    # Signal builder
    # ------------------------------------------------------------------

    def _build_signal(
        self, symbol: str, setup: _SetupResult, htf_bias: int,
        *, fib_data: dict = None, choch_data: dict = None,
    ) -> Signal:
        """Convert a SetupResult into a Signal dataclass.

        Dynamic TP/SL based on:
        - Confidence level: higher confidence → more aggressive TPs
        - Setup type: trend_continuation gets wider TPs, scalps get tighter
        - HTF alignment: aligned with higher TF → extend TPs
        - Volatility (ATR): adjusts stop distance
        """
        entry = setup.entry_price

        # ── Recalculate SL using 5m ATR (not 1m) for wider, more stable stops ──
        # The individual setup scanners use 1m ATR which is too tight (noise stops).
        # 5m ATR gives a realistic volatility measure that survives normal wicks.
        if self._confirm_atr > 0:
            atr_for_sl = self._confirm_atr  # 5m ATR
        else:
            atr_for_sl = setup.atr  # fallback to 1m ATR

        if setup.side == OrderSide.LONG:
            sl = entry - atr_for_sl * self.sl_atr_mult
        else:
            sl = entry + atr_for_sl * self.sl_atr_mult

        risk = abs(entry - sl)

        # ── Enforce minimum SL distance ──
        min_sl_distance = entry * self.min_sl_pct / 100  # 0.40% of price
        if risk < min_sl_distance:
            # Widen SL to minimum distance
            if setup.side == OrderSide.LONG:
                sl = entry - min_sl_distance
            else:
                sl = entry + min_sl_distance
            risk = min_sl_distance

        # ── Dynamic TP multipliers based on conditions ──
        # TP1 at 0.8R for faster partial profit capture (70% exit)
        # TP2/TP3 remain extended for runners (30% continues)
        tp1_rr = self.tp1_rr   # 0.8R — close, fast profit lock
        tp2_rr = self.tp2_rr   # 2.0R base
        tp3_rr = self.tp3_rr   # 4.0R base (tiny runner)

        # Confidence boost: high confidence → extend TP2/TP3 only
        # DON'T extend TP1 — we want it to hit quickly and lock in 70%
        if setup.confidence >= 85:
            tp2_rr *= 1.2   # 3.0R
            tp3_rr *= 1.3   # 5.2R
        elif setup.confidence >= 70:
            tp2_rr *= 1.1   # 2.75R
            tp3_rr *= 1.15  # 4.6R

        # HTF alignment: if HTF agrees, extend TPs (trend has more room)
        if htf_bias != 0:
            is_aligned = (
                (htf_bias > 0 and setup.side == OrderSide.LONG) or
                (htf_bias < 0 and setup.side == OrderSide.SHORT)
            )
            if is_aligned:
                tp2_rr *= 1.2
                tp3_rr *= 1.3

        # Setup-specific adjustments
        if setup.name == "trend_continuation":
            tp2_rr *= 1.1
            tp3_rr *= 1.2
        elif setup.name == "bb_squeeze":
            tp2_rr *= 1.3
            tp3_rr *= 1.5
        elif setup.name == "momentum_surge":
            tp2_rr *= 1.2
            tp3_rr *= 1.4
        # NOTE: rsi_divergence no longer gets tighter TPs — mean reversion
        # needs room to play out

        # Calculate final TP levels
        if setup.side == OrderSide.LONG:
            tp1 = entry + risk * tp1_rr
            tp2 = entry + risk * tp2_rr
            tp3 = entry + risk * tp3_rr
            invalidation = sl - setup.atr * 0.3
        else:
            tp1 = entry - risk * tp1_rr
            tp2 = entry - risk * tp2_rr
            tp3 = entry - risk * tp3_rr
            invalidation = sl + setup.atr * 0.3

        # ── Enforce minimum TP1 distance ──
        min_tp1_distance = entry * self.min_tp1_pct / 100  # 0.20% of price
        if abs(tp1 - entry) < min_tp1_distance:
            if setup.side == OrderSide.LONG:
                tp1 = entry + min_tp1_distance
                tp2 = entry + min_tp1_distance * 2.0
                tp3 = entry + min_tp1_distance * 3.5
            else:
                tp1 = entry - min_tp1_distance
                tp2 = entry - min_tp1_distance * 2.0
                tp3 = entry - min_tp1_distance * 3.5

        # ── Enforce minimum Risk:Reward ratio ──
        actual_rr = abs(tp1 - entry) / risk if risk > 0 else 0
        if actual_rr < self.min_rr_ratio:
            logger.debug(
                "%s: %s rejected — R:R %.2f below minimum %.2f",
                symbol, setup.name, actual_rr, self.min_rr_ratio,
            )
            return None

        grade = confidence_to_grade(setup.confidence)

        # Signal type: BUY/SELL if confidence >= 75, else PRE_BUY/PRE_SELL
        if setup.confidence >= 75:
            sig_type = SignalType.BUY if setup.side == OrderSide.LONG else SignalType.SELL
        else:
            sig_type = SignalType.PRE_BUY if setup.side == OrderSide.LONG else SignalType.PRE_SELL

        # Effective RR for display
        eff_rr = round((tp1_rr + tp2_rr) / 2, 2)

        return Signal(
            symbol=symbol,
            signal_type=sig_type,
            side=setup.side,
            entry_price=round(entry, 2),
            stop_loss=round(sl, 2),
            take_profits=[round(tp1, 2), round(tp2, 2), round(tp3, 2)],
            invalidation_level=round(invalidation, 2),
            confidence=setup.confidence,
            grade=grade,
            risk_reward=eff_rr,
            reason=f"SCALP {setup.name}: {', '.join(setup.confirmations[:4])}",
            regime=MarketRegime.SIDEWAYS,  # scalps work in any regime
            metadata={
                "setup_type": setup.name,
                "confirmations": setup.confirmations,
                "htf_bias": htf_bias,
                "atr": round(setup.atr, 2),
                "dynamic_tp_rr": [round(tp1_rr, 2), round(tp2_rr, 2), round(tp3_rr, 2)],
                "fib_at_level": (fib_data or {}).get("at_fib", False),
                "fib_nearest": (fib_data or {}).get("nearest_level"),
                "choch": (choch_data or {}).get("direction") if (choch_data or {}).get("choch_detected") else None,
                "choch_strength": (choch_data or {}).get("strength", 0) if (choch_data or {}).get("choch_detected") else 0,
            },
        )

    # ------------------------------------------------------------------
    # Signal management
    # ------------------------------------------------------------------

    def clear_signal(self, symbol: str) -> None:
        self._last_signal_time.pop(symbol, None)

    def record_stop_loss(self, symbol: str) -> None:
        self._last_signal_time[symbol] = time.time()

    def has_active_signal(self, symbol: str) -> bool:
        return False  # scalps don't track active signals

    def get_active_signal(self, symbol: str) -> None:
        return None
