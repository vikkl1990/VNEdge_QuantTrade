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
from strategies.regime_filter import (
    RegimeFilter, calc_confidence_size_multiplier,
    is_scanner_allowed_in_regime, get_regime_scanner_boost,
)
from bot.ev_engine import EVEngine
from bot.feature_logger import FeatureLogger
from bot.mode_manager import get_mode_manager
from bot.training_dataset import TrainingDataset
from data.structure import build_structure_map, StructureMap

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
        bot_cfg = config.get("bot", {})
        strat_cfg = config.get("strategy", {})
        ind_cfg = strat_cfg.get("indicators", {})
        filt_cfg = strat_cfg.get("filters", {})
        risk_cfg = config.get("risk", {})
        tf_cfg = config.get("timeframes", {})

        # --- Operating Mode (centralized via ModeManager) ---
        self._mode = get_mode_manager(config)
        self.operating_mode = self._mode.mode
        self._is_learning = self._mode.is_learning()
        if self._is_learning:
            logger.info("🧠 PAPER LEARNING MODE — all signals fire, no blocking, max data collection")

        # --- Training Dataset (ML-ready trade records) ---
        self._training_dataset = TrainingDataset()

        # --- Timeframes ---
        self.primary_tf: str = tf_cfg.get("trigger", "5m")    # 5m trigger — better S/N than 1m
        self.confirm_tf: str = tf_cfg.get("primary", "15m")    # 15m for confirmation
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
        self.min_confidence: int = max(filt_cfg.get("min_confidence", 65), 65)  # lowered — confidence scoring filters naturally
        self.cooldown_sec: int = 0           # NO cooldown — let confidence scoring do the work
        self.max_signals_hr: int = 999       # NO hourly cap — every signal evaluated
        self.sl_atr_mult: float = 2.0        # SL = 2.0× 5m-ATR (tightened from 2.5)

        # --- TP ratios — optimized for high-leverage scalping with maker fees ---
        # SL 0.4% + TP1 1.5R = 0.6% move needed → BE WR 44% with maker fees
        self.tp1_rr: float = 1.5             # TP1 at 1.5R (35% exit) — BE WR 44%
        self.tp2_rr: float = 2.5             # TP2 at 2.5R (35% exit)
        self.tp3_rr: float = 4.0             # TP3 at 4.0R (30% trail)

        # --- SL/TP constraints ---
        self.min_sl_pct: float = 0.55        # 0.55% min SL (Tier 2: wider to survive noise)
        self.max_sl_pct: float = 0.95        # 0.95% max SL (Tier 2: capped for risk control)
        self.min_tp1_pct: float = 0.65       # TP1 ≥ 0.65% (Tier 2: covers fees + slippage)
        self.min_rr_ratio: float = 1.2       # min 1.2R — ensures positive EV

        # --- Liquidation safety (critical at high leverage) ---
        self.liq_sl_max_pct: float = 0.40    # SL ≤ 40% of liq buffer = WARNING
        self.liq_reject_pct: float = 0.80    # Reject only if SL ≥ 80% of liq buffer
        self.liq_min_buffer_pct: float = 0.3 # Min 0.3% — allows up to 100x super scalp

        # --- Confidence-scaled leverage ---
        # Leverage cap: max 20x (even at 95 conf) unless BTC structure_bounce
        # Tier 2 Session+Risk: prevents overleveraging
        self.leverage_map = {
            95: 20,   # Capped at 20x for safety
            90: 20,   # Same cap
            85: 15,   # A signals
            80: 15,   # Strong
            75: 10,   # Valid
            70: 10,   # Decent
            65:  5,   # Minimum
            0:   3,   # Low confidence fallback
        }

        # --- Tier 1: Edge vs Cost thresholds ---
        # Scalper offer: entry maker 0.02% + settlement 0.06% = 0.08% total
        self.scalper_cost_pct = 0.047   # entry maker only (exit free under Scalper)
        self.min_edge_high_conf = 0.18  # conservative_move ≥ 0.18% for conf 90+
        self.min_edge_low_conf = 0.25   # conservative_move ≥ 0.25% for conf < 90

        # --- Tier 2: Scanner weight tiers ---
        self.scanner_size_tiers = {
            "structure_bounce": 1.0,     # full size — best performer
            "order_block_entry": 1.0,    # full size — institutional zones
            "ema_momentum": 0.6,         # reduced — only LONG
            "trend_continuation": 0.6,   # reduced
            "rsi_divergence": 0.0,       # shadow/ML only — no trade
            "vwap_mean_revert": 0.0,     # shadow/ML only
            "liquidity_sweep": 0.0,      # shadow/ML only
            "simple_bias": 0.0,          # ML training only
        }
        self.scanner_auto_shadow_wr = 48  # auto-shadow if WR < 48% last 80 trades

        # --- RSI divergence lookback ---
        self.div_lookback: int = 30          # bars to scan for divergence (was 14)
        self.div_min_swing: float = 0.002    # minimum price swing % (was 0.001)

        # --- State (DECOUPLED per symbol — each pair has independent state) ---
        self._last_signal_time: Dict[str, float] = {}  # symbol → last signal time
        self._signal_count_hr: Dict[str, List[float]] = {}  # symbol → [timestamps]
        # Per-scanner+symbol cooldown
        self._scanner_cooldowns: Dict[str, float] = {}  # "scanner_symbol" → last fire time
        self._scanner_cooldown_sec: int = 300  # 5 minutes

        # --- Scanner weight manager (adaptive from R-performance) ---
        self._weight_manager = ScannerWeightManager()

        # --- Regime filter (per-symbol regime state) ---
        self._regime_filter = RegimeFilter()
        self._last_regime_info: Dict[str, Dict[str, Any]] = {}  # symbol → regime info

        # --- EV Engine (expected value gating) ---
        self._ev_engine = EVEngine()
        self._last_ev_results: Dict[str, Any] = {}

        # --- Feature Logger (ML training data) ---
        self._feature_logger = FeatureLogger()

        # --- Structure map (computed per bar) ---
        self._structure_map: Optional[StructureMap] = None

        # --- Self-optimization: adapt SL/TP from last 50 trades ---
        self._last_optimize_time: float = 0
        self._optimize_interval: int = 300  # re-check every 5 min
        self._sl_adjust: float = 1.0  # multiplier on SL (1.0 = default)
        self._cached_by_setup: Dict[str, Dict] = {}  # cached from signal tracker

        # --- Opportunity funnel counters (per-symbol) ---
        self._funnels: Dict[str, Dict[str, int]] = {}  # symbol → funnel counts
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

        # Track signal count per symbol (decoupled — BTC signals don't count against ETH)
        if symbol not in self._signal_count_hr:
            self._signal_count_hr[symbol] = []
        self._signal_count_hr[symbol] = [t for t in self._signal_count_hr[symbol] if now - t < 3600]

        # Per-symbol funnel
        _empty_funnel = {"scanned": 0, "strong": 0, "valid": 0, "weak": 0,
                         "near_miss": 0, "rejected": 0,
                         "blocked_regime": 0, "blocked_cost": 0, "blocked_htf": 0, "blocked_ev": 0}
        if symbol not in self._funnels:
            self._funnels[symbol] = dict(_empty_funnel)
        # Reset funnel every hour
        if now - self._funnel_reset_time > 3600:
            self._funnels = {s: dict(_empty_funnel) for s in self._funnels}
            self._funnel_reset_time = now
        # Use per-symbol funnel for this call
        self._funnel = self._funnels[symbol]

        # ── SESSION-AWARE GATING ──
        # Data from 112 trades: Asia Late 37% WR, Asia Early 50%, Europe 63%, US 57%
        # Block the worst session, restrict the marginal one
        # (Disabled in backtesting via _session_gate_enabled=False)
        if getattr(self, '_session_gate_enabled', True) is False:
            self._current_session = "europe"
            self._session_min_confidence = self.min_confidence
        ist_now = datetime.now(_IST)
        ist_hour = ist_now.hour + ist_now.minute / 60.0
        # Session context — soft confidence adjustment (no blocking)
        self._session_penalty: int = 0
        if getattr(self, '_session_gate_enabled', True) and 2.5 <= ist_hour < 9.0:
            self._current_session = "asia_late"    # 37% WR — penalty, NOT blocked
            self._session_min_confidence = 65
            self._session_penalty = -15            # reduces confidence score
        elif 9.0 <= ist_hour < 13.5:
            self._current_session = "asia_early"   # 50% WR
            self._session_min_confidence = 65
            self._session_penalty = -5
        elif 13.5 <= ist_hour < 20.5:
            self._current_session = "europe"        # 63% WR — best session, boost
            self._session_min_confidence = 65
            self._session_penalty = +5
        else:
            self._current_session = "us"            # 57% WR — decent
            self._session_min_confidence = 65
            self._session_penalty = 0

        # --- Self-optimize from recent trades ---
        self._self_optimize()

        # --- Compute indicators on primary TF ---
        df = self._compute_indicators(primary_df)

        # --- Compute 5m ATR for SL calculation (1m ATR is too noisy/tight) ---
        # 5m ATR captures real volatility; 1m ATR gets noise-stopped constantly
        self._confirm_atr: float = 0.0
        confirm_atr_series = None
        if confirm_df is not None and len(confirm_df) >= 20:
            try:
                # Use pre-computed ATR if available (backtest optimization)
                if "_precomputed_atr" in confirm_df.columns:
                    confirm_atr_series = confirm_df["_precomputed_atr"]
                elif "atr" in confirm_df.columns:
                    confirm_atr_series = confirm_df["atr"]
                else:
                    confirm_atr_series = calc_atr(confirm_df, self.atr_period)
                self._confirm_atr = float(confirm_atr_series.iloc[-1])
            except Exception:
                pass

        # ── ATR VOLATILITY TRACKING ──
        # Store ratio for hard veto layer (< 0.7 = dead market, no trading)
        self._atr_ratio: float = 1.0
        if confirm_df is not None and len(confirm_df) >= 30 and self._confirm_atr > 0 and confirm_atr_series is not None:
            try:
                atr_sma = float(confirm_atr_series.rolling(20).mean().iloc[-1])
                if atr_sma > 0:
                    self._atr_ratio = self._confirm_atr / atr_sma
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

        # --- Build structure map (S/R, order blocks, liquidity, VWAP) ---
        struct_source = confirm_df if confirm_df is not None and len(confirm_df) >= 50 else primary_df
        try:
            _struct_close = float(struct_source.iloc[-1]["close"])
            _struct_atr = self._confirm_atr if self._confirm_atr > 0 else float(df.iloc[-1].get("atr", 0))
            self._structure_map = build_structure_map(struct_source, _struct_close, _struct_atr)
        except Exception:
            self._structure_map = None

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

        # --- Detect market regime ---
        regime = self._regime_filter.detect_regime(indicators)
        self._last_regime_info[symbol] = {
            "regime": regime,
            "action": {},
            "indicators_snapshot": {
                "ema_8": indicators.get("ema_8", 0),
                "ema_21": indicators.get("ema_21", 0),
                "ema_50": indicators.get("ema_50", 0),
                "bb_bandwidth": float(last_row.get("bb_bandwidth", 0)) if not np.isnan(last_row.get("bb_bandwidth", 0)) else 0,
            },
        }

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
            "_scan_structure_bounce": "Structure Bounce",
            "_scan_liquidity_sweep": "Liquidity Sweep",
            "_scan_order_block_entry": "Order Block",
            "_scan_vwap_mean_revert": "VWAP Mean Revert",
            "_scan_simple_bias": "Simple Bias (ML)",
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

        # (funnel reset moved to per-symbol init above)

        # ══════════════════════════════════════════════════════
        # REGIME-FIRST ROUTER — Only run scanners allowed in current regime
        # This is THE core change: regime gates which scanners fire.
        # ══════════════════════════════════════════════════════
        REGIME_SCANNER_ROUTING = {
            "trending_up": [
                self._scan_trend_continuation,
                self._scan_ema_momentum,
                self._scan_structure_bounce,
            ],
            "trending_down": [
                self._scan_trend_continuation,
                self._scan_ema_momentum,
                self._scan_structure_bounce,
            ],
            "breakout": [
                self._scan_structure_bounce,
                self._scan_order_block_entry,
                self._scan_ema_momentum,
            ],
            "ranging": [
                self._scan_vwap_mean_revert,
                self._scan_rsi_divergence,
                self._scan_structure_bounce,
            ],
            "sideways": [
                self._scan_vwap_mean_revert,
                self._scan_rsi_divergence,
                self._scan_structure_bounce,
            ],
            "volatile": [
                self._scan_structure_bounce,
                self._scan_order_block_entry,
            ],
            "high_volatility": [
                self._scan_structure_bounce,
                self._scan_order_block_entry,
            ],
            "mean_reversion": [
                self._scan_vwap_mean_revert,
                self._scan_rsi_divergence,
            ],
            "quiet": [],       # NO TRADING in dead markets
            "low_liquidity": [],  # NO TRADING
        }

        # Get allowed scanners for current regime
        allowed_scanners = REGIME_SCANNER_ROUTING.get(regime, [])

        # In paper_learning mode: ALL scanners run regardless of regime
        if self._is_learning:
            allowed_scanners = list(all_scanners)  # override: run everything
            # Also add simple bias scanner — fires on any candle with volume
            allowed_scanners.append(self._scan_simple_bias)

        if not allowed_scanners:
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"REGIME VETO: {regime} — no scanners allowed",
                "indicators": indicators, "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            return []

        scan_results: List[ScanResult] = []

        for scanner in allowed_scanners:
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
                        "entry_price": result.entry_price,
                        "stop_loss": result.stop_loss,
                        "side": result.side.value if result.side else None,
                        "atr": result.atr,
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
        # Only count triggered scanners (setup_result not None) for strong/valid/weak
        # Non-triggered go to near_miss or rejected based on proximity
        for sr in scan_results:
            if sr.setup_result is not None:
                # Actually triggered — count in real tier
                if sr.tier in self._funnel:
                    self._funnel[sr.tier] += 1
            else:
                # Didn't trigger — only near_miss or rejected
                if sr.tier == TIER_NEAR_MISS:
                    self._funnel["near_miss"] += 1
                else:
                    self._funnel["rejected"] += 1

        # ── Select best tradeable result ──
        if self._is_learning:
            # LEARNING MODE: accept ALL triggered scanners, no tier filter
            tradeable = [
                sr for sr in scan_results
                if sr.setup_result is not None
                and sr.tier in (TIER_STRONG, TIER_VALID, TIER_WEAK, TIER_NEAR_MISS)
            ]
        else:
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
            # ── LEARNING MODE FALLBACK: generate bias signal for ML training ──
            if self._is_learning and near_misses:
                # Take the best near-miss as a weak signal — ML needs data
                best_near = max(near_misses, key=lambda s: s.weighted_score)
                tradeable = [best_near]
                logger.info("LEARNING: promoting near-miss %s (score=%.0f) for ML training",
                           best_near.scanner_name, best_near.weighted_score)
            else:
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

        # ══════════════════════════════════════════════════════
        # TIER 2: SCANNER VETO + WEIGHT GATE
        # Shadow scanners only log for ML, no actual trade
        # ══════════════════════════════════════════════════════
        scanner_size = self.scanner_size_tiers.get(best_sr.scanner_name, 0.6)
        if scanner_size <= 0.0 and not self._is_learning:
            self._funnel["blocked_regime"] = self._funnel.get("blocked_regime", 0) + 1
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"SCANNER SHADOW: {best_sr.scanner_name} is ML-only (no trade)",
                "indicators": indicators, "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            # Still log for ML training
            self._feature_logger.log_signal(
                symbol=symbol, scanner=best_sr.scanner_name,
                side=best.side.value if best.side else "",
                tier=best_sr.tier, score=best.confidence,
                weighted_score=best_sr.weighted_score,
                entry_price=best.entry_price, stop_loss=best.stop_loss,
                atr=best.atr, indicators=indicators, regime=regime,
                scanner_weight=best_sr.scanner_weight,
                scanner_expectancy=0, ev=0,
            )
            return []

        # Block ema_momentum SHORTS entirely (33% WR historically)
        if best_sr.scanner_name == "ema_momentum" and best.side == OrderSide.SHORT:
            if not self._is_learning:
                self.last_scan_status[symbol] = {
                    "time": now_iso, "signal": False,
                    "reason": "SCANNER VETO: ema_momentum SHORT blocked (33% WR)",
                    "indicators": indicators, "setups_checked": setups_checked,
                    "funnel": dict(self._funnel),
                }
                return []

        # ══════════════════════════════════════════════════════
        # HARD VETO LAYER — ANY veto = NO TRADE
        # Upgraded with Tier 2 strict alignment
        # ══════════════════════════════════════════════════════
        vetos = []

        # VETO 1: Scanner cooldown (anti-duplicate)
        cooldown_key = f"{best_sr.scanner_name}_{symbol}"
        last_fire = self._scanner_cooldowns.get(cooldown_key, 0)
        if now - last_fire < self._scanner_cooldown_sec:
            vetos.append(f"COOLDOWN: {best_sr.scanner_name} fired {int((now-last_fire)/60)}m ago")

        # VETO 2: HTF STRICT alignment (Tier 2 upgrade — HARD veto, not soft)
        # Signal side MUST match HTF bias (15m EMA50)
        if htf_bias != 0:
            htf_opposes = (
                (htf_bias < 0 and best.side == OrderSide.LONG) or
                (htf_bias > 0 and best.side == OrderSide.SHORT)
            )
            if htf_opposes:
                vetos.append(f"HTF STRICT: HTF={'bearish' if htf_bias < 0 else 'bullish'} vs {best.side.value}")

        # VETO 3: Session + Risk (Tier 2 upgrade)
        # Asia Late: HARD veto (not just penalty)
        # Dead UTC hours: HARD veto
        ist_now_check = datetime.now(_IST)
        utc_hour = (ist_now_check.hour - 5) % 24
        if getattr(self, '_session_gate_enabled', True):
            ist_hour_check = ist_now_check.hour + ist_now_check.minute / 60.0
            if 2.5 <= ist_hour_check < 9.0:
                vetos.append(f"ASIA LATE VETO: 02:30-09:00 IST (37% WR)")
            dead_hours = {2, 3, 4, 5, 10, 11}
            if utc_hour in dead_hours:
                vetos.append(f"DEAD SESSION: UTC hour {utc_hour}")

        # VETO 4: Volatility STRICT (Tier 2 — ATR ≥ 0.88× avg, was 0.7)
        atr_ratio = getattr(self, '_atr_ratio', 1.0)
        if atr_ratio < 0.88:
            vetos.append(f"LOW VOLATILITY: ATR ratio {atr_ratio:.2f} < 0.88")

        # VETO 5: Volume (stricter — 2.2× for 1m, 1.0× for 5m)
        last_row_vol = df.iloc[-1]
        rel_vol_check = float(last_row_vol.get("rel_vol", 1.0)) if not np.isnan(last_row_vol.get("rel_vol", 1.0)) else 0
        if rel_vol_check < 1.0:
            vetos.append(f"NO VOLUME: rel_vol={rel_vol_check:.1f} < 1.0")

        # VETO 6: CHOCH conflict (unchanged — structural break opposes signal)
        if choch_data.get("choch_detected", False):
            choch_dir = choch_data.get("direction")
            choch_strength = choch_data.get("strength", 0)
            choch_bars = choch_data.get("bars_ago", 999)
            if choch_bars <= 10 and choch_strength >= 60:
                choch_opposes = (
                    (choch_dir == "bearish" and best.side == OrderSide.LONG) or
                    (choch_dir == "bullish" and best.side == OrderSide.SHORT)
                )
                if choch_opposes:
                    vetos.append(f"CHOCH CONFLICT: {choch_dir} vs {best.side.value}")

        # VETO 7: Candle quality — body ratio must be meaningful
        trigger_candle = df.iloc[-1]
        candle_body = abs(float(trigger_candle.get("close", 0)) - float(trigger_candle.get("open", 0)))
        candle_range = float(trigger_candle.get("high", 0)) - float(trigger_candle.get("low", 0))
        if candle_range > 0:
            body_ratio = candle_body / candle_range
            if body_ratio < 0.3:
                vetos.append(f"WEAK CANDLE: body ratio {body_ratio:.2f} < 0.3")

        # VETO 8: No-Chase gate (Tier 2 — stricter impulse filter)
        _chase_atr = self._confirm_atr if self._confirm_atr > 0 else best.atr
        if _chase_atr > 0 and best.entry_price > 0:
            # Large candle body > 1.25× ATR → chasing
            if candle_body > _chase_atr * 1.25:
                vetos.append(f"NO CHASE: candle body {candle_body:.2f} > 1.25×ATR")
            # Price stretched > 0.7× ATR from EMA8
            ema8_val = float(df.iloc[-1].get("ema_8", 0))
            if ema8_val > 0:
                dist_from_ema8 = abs(float(df.iloc[-1].get("close", 0)) - ema8_val)
                if dist_from_ema8 > _chase_atr * 0.7:
                    vetos.append(f"NO CHASE: stretched {dist_from_ema8:.2f} > 0.7×ATR from EMA8")

        # VETO 9: Regime + Scanner mismatch (Tier 2 — strict routing)
        regime_scanner_ok = True
        if regime in ("ranging", "sideways", "quiet"):
            if best_sr.scanner_name not in ("structure_bounce", "vwap_mean_revert", "rsi_divergence"):
                regime_scanner_ok = False
                vetos.append(f"REGIME MISMATCH: {best_sr.scanner_name} not for {regime}")
        elif regime in ("trending_up", "trending_down"):
            if best_sr.scanner_name not in ("trend_continuation", "ema_momentum", "structure_bounce"):
                regime_scanner_ok = False
                vetos.append(f"REGIME MISMATCH: {best_sr.scanner_name} not for {regime}")
        elif regime in ("volatile", "high_volatility"):
            if best_sr.scanner_name not in ("structure_bounce", "order_block_entry"):
                regime_scanner_ok = False
                vetos.append(f"REGIME MISMATCH: {best_sr.scanner_name} not for {regime}")

        # Apply vetos (in learning mode: log but don't block)
        if vetos and not self._is_learning:
            self._funnel["blocked_regime"] = self._funnel.get("blocked_regime", 0) + 1
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"VETO: {vetos[0]}",  # show first veto
                "all_vetos": vetos,
                "indicators": indicators, "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            return []

        # In learning mode, tag the signal with would-block info
        if vetos and self._is_learning:
            best.confirmations.append(f"[WOULD_BLOCK: {len(vetos)} vetos]")

        # ── Apply confidence modifiers (boosts only, no penalties) ──

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

        # ── ALL SOFT PENALTIES — NO HARD BLOCKS ──
        # Every signal fires. Confidence score determines quality.

        # CHOCH: boost if aligned, penalize if conflicts (no block)
        if choch_data.get("choch_detected", False):
            choch_dir = choch_data.get("direction")
            choch_strength = choch_data.get("strength", 0)
            if (choch_dir == "bullish" and best.side == OrderSide.LONG) or \
               (choch_dir == "bearish" and best.side == OrderSide.SHORT):
                best.confidence = min(best.confidence + 10, 100)
                best.confirmations.append(f"CHOCH {choch_dir} (str={choch_strength})")
            elif choch_strength >= 60:
                best.confidence = max(best.confidence - 10, 0)
                best.confirmations.append(f"CHOCH conflict penalty -10")

        # Regime: boost/penalize (no block)
        regime_boost = get_regime_scanner_boost(best_sr.scanner_name, regime)
        if regime_boost > 0:
            best.confidence = min(best.confidence + regime_boost, 100)
            best.confirmations.append(f"Regime boost +{regime_boost} ({regime})")

        # EV: compute with calibrated lookup (scanner+side+regime+session)
        setup_name_ev = best_sr.scanner_name
        ev_side = best.side.value if best.side else ""
        ev_session = getattr(self, '_current_session', '')
        ev_result = self._ev_engine.compute_ev(
            setup_name_ev, self._cached_by_setup,
            regime=regime, side=ev_side, session=ev_session,
        )
        self._last_ev_results[setup_name_ev] = ev_result.to_dict()
        ev_size_mult = ev_result.size_multiplier

        # HARD EV VETO — reject negative EV trades
        if ev_result.verdict == "REJECT" and not self._is_learning:
            self._funnel["blocked_ev"] = self._funnel.get("blocked_ev", 0) + 1
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"EV REJECT: {ev_result.reason}",
                "indicators": indicators, "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            return []

        confidence_size_mult = calc_confidence_size_multiplier(best.confidence, best_sr.tier)

        # ══════════════════════════════════════════════════════
        # TIER 1: MINIMUM EDGE vs REAL COST GATE
        # Replaces old momentum gate + fee filter
        # conservative_move must exceed Scalper fee + slippage buffer
        # ══════════════════════════════════════════════════════
        _edge_atr = self._confirm_atr if self._confirm_atr > 0 else best.atr
        if _edge_atr > 0 and best.entry_price > 0:
            # conservative_move = min(TP1 distance %, 1.5 × ATR_5m %)
            atr_pct = (_edge_atr / best.entry_price) * 100
            risk_dist = abs(best.entry_price - best.stop_loss)
            tp1_dist_pct = (risk_dist * self.tp1_rr / best.entry_price) * 100
            conservative_move = min(tp1_dist_pct, 1.5 * atr_pct)

            # Required minimum based on confidence
            min_edge = self.min_edge_high_conf if best.confidence >= 90 else self.min_edge_low_conf

            if conservative_move < min_edge and not self._is_learning:
                self._funnel["blocked_cost"] = self._funnel.get("blocked_cost", 0) + 1
                self.last_scan_status[symbol] = {
                    "time": now_iso, "signal": False,
                    "reason": f"EDGE GATE: move={conservative_move:.3f}% < {min_edge:.3f}% min (conf={best.confidence})",
                    "indicators": indicators, "setups_checked": setups_checked,
                    "funnel": dict(self._funnel),
                }
                return []

        # Momentum check (dead trade prevention — stricter)
        # Last 5 candles range < 0.42× ATR → flat market (was 0.3)
        if len(df) >= 6 and best.atr > 0:
            recent_5 = df.iloc[-5:]
            recent_range = float(recent_5["high"].max() - recent_5["low"].min())
            atr_check = self._confirm_atr if self._confirm_atr > 0 else best.atr
            if recent_range < atr_check * 0.42:
                if not self._is_learning:
                    self.last_scan_status[symbol] = {
                        "time": now_iso, "signal": False,
                        "reason": f"MOMENTUM GATE: flat (range={recent_range:.2f} < 0.42×ATR={atr_check*0.42:.2f})",
                        "indicators": indicators, "setups_checked": setups_checked,
                        "funnel": dict(self._funnel),
                    }
                    return []

        # ── Build Signal ──
        signal = self._build_signal(
            symbol, best, htf_bias,
            fib_data=fib_data, choch_data=choch_data,
            primary_df=primary_df, regime=regime,
        )

        # Tag signal with tier and scanner weight info
        if signal.metadata is None:
            signal.metadata = {}
        signal.metadata["signal_tier"] = best_sr.tier
        signal.metadata["scanner_weight"] = best_sr.scanner_weight
        signal.metadata["scanner_status"] = best_sr.scanner_status
        signal.metadata["weighted_score"] = best_sr.weighted_score
        signal.metadata["impulse_penalty"] = 0  # no impulse blocking
        signal.metadata["penalties"] = best_sr.penalties
        signal.metadata["regime"] = regime
        signal.metadata["regime_size_mult"] = 1.0  # no regime blocking
        signal.metadata["regime_sl_mult"] = 1.0
        signal.metadata["confidence_size_mult"] = confidence_size_mult
        signal.metadata["ev"] = round(ev_result.ev, 4)
        signal.metadata["ev_verdict"] = ev_result.verdict
        signal.metadata["ev_size_mult"] = ev_size_mult
        signal.metadata["p_win"] = round(ev_result.p_win, 4)

        # ── Log feature vector for ML training ──
        self._feature_logger.log_signal(
            symbol=symbol, scanner=best_sr.scanner_name,
            side=best.side.value if best.side else "",
            tier=best_sr.tier, score=best.confidence,
            weighted_score=best_sr.weighted_score,
            entry_price=best.entry_price, stop_loss=best.stop_loss,
            atr=best.atr, indicators=indicators, regime=regime,
            scanner_weight=best_sr.scanner_weight,
            scanner_expectancy=ev_result.ev, ev=ev_result.ev,
            trade_id=signal.metadata.get("trade_id", ""),
        )

        self._last_signal_time[symbol] = now
        self._signal_count_hr.setdefault(symbol, []).append(now)
        # Record per-scanner cooldown
        self._scanner_cooldowns[f"{best_sr.scanner_name}_{symbol}"] = now

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

        # Record to ML training dataset
        try:
            self._training_dataset.record_from_signal(
                signal.to_dict() if hasattr(signal, 'to_dict') else {
                    "trade_id": signal.metadata.get("trade_id", ""),
                    "symbol": symbol,
                    "side": best.side.value,
                    "entry_price": signal.entry_price,
                    "stop_loss": signal.stop_loss,
                    "take_profits": signal.take_profits,
                    "confidence": signal.confidence,
                    "grade": signal.grade.value if hasattr(signal.grade, 'value') else str(signal.grade),
                    "timestamp": now_iso,
                    "metadata": signal.metadata,
                },
                would_blocks={
                    "cooldown": getattr(self, '_last_would_block_cooldown', False),
                    "rr": signal.metadata.get("would_block_rr", False),
                    "liq": signal.metadata.get("would_block_liq", False),
                },
                features=indicators,
                session=getattr(self, '_current_session', ''),
                regime=regime,
            )
        except Exception as e:
            logger.debug("Training dataset write failed: %s", e)

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
        # Skip if already pre-computed (backtest optimization)
        if "ema_8" in df.columns and "rsi" in df.columns and "atr" in df.columns:
            # Verify the last row has valid indicator values
            last = df.iloc[-1]
            if not (pd.isna(last.get("ema_8", float("nan"))) or pd.isna(last.get("rsi", float("nan")))):
                return df
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
            price_pulled_back = close <= ema8 * 1.001 and close > ema21
            rsi_recovering = 40 < rsi < 58 and rsi > rsi_prev
            bullish_candle = close > open_

            if price_pulled_back and rsi_recovering and bullish_candle:
                side = OrderSide.LONG
                confs.append("Uptrend continuation")
                score += 25

                # Bullish candle
                confs.append("Bullish candle")
                score += 10

                # EMA gap — scored, not blocked
                if ema_gap_pct > 0.08:
                    confs.append(f"EMA8>21 by {ema_gap_pct:.3f}%")
                    score += 10
                elif ema_gap_pct < 0.03:
                    score -= 10  # weak trend penalty

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

                if ema_gap_pct > 0.08:
                    confs.append(f"EMA21>8 by {ema_gap_pct:.3f}%")
                    score += 10
                elif ema_gap_pct < 0.03:
                    score -= 10  # weak trend penalty

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

        # Volume: boost or penalize (no hard block)
        rel_vol = last.get("rel_vol", 1.0)
        if np.isnan(rel_vol):
            rel_vol = 1.0
        if rel_vol < 0.8:
            score -= 10  # low volume penalty
        elif rel_vol < 1.0:
            score -= 5   # below average penalty

        # HTF alignment bonus
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        # 5m confirmation
        if confirm_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("5m aligned")
            score += 10

        # Volume bonus (already confirmed >= 1.0 above)
        if rel_vol > 1.5:
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

        # Counter-trend divergence — penalize but don't block in learning mode
        if side == OrderSide.LONG and htf_bias == -1:
            if not getattr(self, '_is_learning', False):
                return None
            score -= 15  # heavy penalty but still fires for data collection
            confs.append("COUNTER-TREND (would_block)")
        if side == OrderSide.SHORT and htf_bias == 1:
            if not getattr(self, '_is_learning', False):
                return None
            score -= 15
            confs.append("COUNTER-TREND (would_block)")

        # HTF alignment bonus (only for aligned divergences)
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
        sl = close - atr * 1.2 if side == OrderSide.LONG else close + atr * 1.2

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
    # STRUCTURE SCANNER 1: S/R Bounce
    # ==================================================================

    def _scan_structure_bounce(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Price touches a known S/R level + rejection candle."""
        sm = self._structure_map
        if sm is None:
            logger.debug("structure_bounce: no structure map")
            return None

        last = df.iloc[-1]
        close = float(last["close"])
        open_ = float(last["open"])
        high = float(last["high"])
        low = float(last["low"])
        atr = float(last.get("atr", 0))
        if atr <= 0 or np.isnan(atr):
            return None

        body = abs(close - open_)
        full_range = high - low
        if full_range <= 0:
            return None

        side = None
        confs = []
        score = 0
        target_level = None

        # Check if price is near a support level (LONG setup)
        if sm.nearest_support:
            lvl = sm.nearest_support
            dist_pct = (close - lvl.price) / close * 100
            # Within 0.5% of support OR inside the zone
            in_zone = lvl.zone_low <= close <= lvl.zone_high
            if in_zone or (0 <= dist_pct < 0.5):
                lower_wick = min(open_, close) - low
                # Relaxed rejection: wick > body OR close in upper 55% of range
                has_rejection = (lower_wick > body * 1.0 and close > (low + full_range * 0.55))
                # Also accept bullish candle at the level even without perfect wick
                bullish_at_level = (close > open_ and close > (low + full_range * 0.5))
                if has_rejection or bullish_at_level:
                    side = OrderSide.LONG
                    target_level = lvl
                    confs.append(f"S/R support bounce ({lvl.level_type})")
                    score += 30
                    if lower_wick > atr * 0.5:
                        confs.append(f"Rejection wick ({lower_wick/atr:.1f}x ATR)")
                        score += 15
                    elif lower_wick > atr * 0.3:
                        confs.append(f"Wick at level ({lower_wick/atr:.1f}x ATR)")
                        score += 10
                    if in_zone:
                        confs.append("Inside structure zone")
                        score += 5

        # Check if price is near a resistance level (SHORT setup)
        if side is None and sm.nearest_resistance:
            lvl = sm.nearest_resistance
            dist_pct = (lvl.price - close) / close * 100
            in_zone = lvl.zone_low <= close <= lvl.zone_high
            if in_zone or (0 <= dist_pct < 0.5):
                upper_wick = high - max(open_, close)
                has_rejection = (upper_wick > body * 1.0 and close < (low + full_range * 0.45))
                bearish_at_level = (close < open_ and close < (low + full_range * 0.5))
                if has_rejection or bearish_at_level:
                    side = OrderSide.SHORT
                    target_level = lvl
                    confs.append(f"S/R resistance rejection ({lvl.level_type})")
                    score += 30
                    if upper_wick > atr * 0.5:
                        confs.append(f"Rejection wick ({upper_wick/atr:.1f}x ATR)")
                        score += 15
                    elif upper_wick > atr * 0.3:
                        confs.append(f"Wick at level ({upper_wick/atr:.1f}x ATR)")
                        score += 10
                    if in_zone:
                        confs.append("Inside structure zone")
                        score += 5

        if side is None or target_level is None:
            return None

        # Level strength bonus
        score += min(target_level.strength // 5, 15)
        if target_level.touch_count >= 3:
            confs.append(f"{target_level.touch_count} touches")
            score += 10

        # Volume at level
        rel_vol = float(last.get("rel_vol", 1.0))
        if not np.isnan(rel_vol) and rel_vol > 1.2:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 10

        # HTF alignment
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        # Confluence: multiple structure types at same level
        nearby = [l for l in sm.levels if abs(l.price - target_level.price) / close < 0.003 and l != target_level]
        if nearby:
            confs.append(f"Multi-structure confluence ({len(nearby)+1} levels)")
            score += 10

        confidence = min(score, 100)

        # SL below/above the structure zone + buffer
        # CLAMP: max SL = 2× ATR or 0.5% of price (whichever is smaller)
        max_sl_dist = min(atr * 2.0, close * 0.005)
        if side == OrderSide.LONG:
            struct_sl = target_level.zone_low - close * 0.001
            sl = max(struct_sl, close - max_sl_dist)  # don't let SL be too far
        else:
            struct_sl = target_level.zone_high + close * 0.001
            sl = min(struct_sl, close + max_sl_dist)  # don't let SL be too far

        return _SetupResult(
            name="structure_bounce",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=target_level.price,  # limit entry at the level
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # STRUCTURE SCANNER 2: Liquidity Sweep
    # ==================================================================

    def _scan_liquidity_sweep(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Price sweeps below swing low (hunts stops) then reverses."""
        if len(df) < 20:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        close = float(last["close"])
        open_ = float(last["open"])
        low = float(last["low"])
        high = float(last["high"])
        atr = float(last.get("atr", 0))
        if atr <= 0 or np.isnan(atr):
            return None

        # Find recent swing lows/highs in last 50 bars
        from data.structure import find_swings
        swing_highs, swing_lows = find_swings(df, lookback=50)

        side = None
        confs = []
        score = 0
        sweep_level = 0.0

        # LONG: Price wicked below a swing low but closed above it (stop hunt → reversal)
        for idx, swing_price in reversed(swing_lows[-5:]):  # check last 5 swing lows
            if idx >= len(df) - 2:
                continue  # skip current/prev bar
            if low < swing_price and close > swing_price:
                # Sweep detected: wick went below, close came back above
                side = OrderSide.LONG
                sweep_level = swing_price
                confs.append(f"Liquidity sweep below swing low ${swing_price:.0f}")
                score += 35
                # Quality of sweep
                sweep_depth = (swing_price - low) / atr
                if sweep_depth > 0.3:
                    score += 10
                    confs.append(f"Deep sweep ({sweep_depth:.1f}x ATR)")
                break

        # SHORT: Price wicked above a swing high but closed below it
        if side is None:
            for idx, swing_price in reversed(swing_highs[-5:]):
                if idx >= len(df) - 2:
                    continue
                if high > swing_price and close < swing_price:
                    side = OrderSide.SHORT
                    sweep_level = swing_price
                    confs.append(f"Liquidity sweep above swing high ${swing_price:.0f}")
                    score += 35
                    sweep_depth = (high - swing_price) / atr
                    if sweep_depth > 0.3:
                        score += 10
                        confs.append(f"Deep sweep ({sweep_depth:.1f}x ATR)")
                    break

        if side is None:
            return None

        # Volume spike on sweep (market makers active)
        rel_vol = float(last.get("rel_vol", 1.0))
        if not np.isnan(rel_vol) and rel_vol > 1.5:
            confs.append(f"Volume spike {rel_vol:.1f}x")
            score += 15
        elif not np.isnan(rel_vol) and rel_vol > 1.0:
            score += 5

        # RSI divergence at sweep (extra confirmation)
        rsi = float(last.get("rsi", 50))
        if side == OrderSide.LONG and rsi < 40:
            confs.append(f"RSI oversold at sweep ({rsi:.0f})")
            score += 10
        elif side == OrderSide.SHORT and rsi > 60:
            confs.append(f"RSI overbought at sweep ({rsi:.0f})")
            score += 10

        # HTF alignment
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        confidence = min(score, 100)

        # SL below the sweep wick + buffer
        if side == OrderSide.LONG:
            sl = low - close * 0.001  # below the sweep wick
        else:
            sl = high + close * 0.001

        return _SetupResult(
            name="liquidity_sweep",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # LEARNING MODE: Simple Bias Scanner (fires on any directional candle)
    # ==================================================================

    def _scan_simple_bias(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Simple directional bias — fires on almost any candle.

        ONLY used in paper_learning mode to generate ML training data.
        Scores based on body ratio, volume, and EMA alignment.
        """
        if len(df) < 10:
            return None

        last = df.iloc[-1]
        atr = last.get("atr", 0)
        close = float(last["close"])
        open_ = float(last["open"])

        if atr <= 0 or np.isnan(atr):
            return None

        # Any candle with a body = directional bias
        bullish = close > open_
        body = abs(close - open_)
        candle_range = float(last["high"]) - float(last["low"])
        if candle_range <= 0:
            return None

        body_ratio = body / candle_range
        if body_ratio < 0.15:  # skip pure dojis
            return None

        side = OrderSide.LONG if bullish else OrderSide.SHORT
        confs = []
        score = 20  # base score for any directional candle

        # Body ratio bonus
        if body_ratio > 0.6:
            score += 10
            confs.append(f"Strong body ({body_ratio:.0%})")
        elif body_ratio > 0.4:
            score += 5
            confs.append(f"Decent body ({body_ratio:.0%})")
        else:
            confs.append(f"Weak body ({body_ratio:.0%})")

        # Volume
        vol_r = last.get("rel_vol", 1.0)
        if not np.isnan(vol_r) and vol_r > 1.0:
            score += 10
            confs.append(f"Volume {vol_r:.1f}x")

        # EMA alignment
        ema8 = last.get("ema_8", 0)
        ema21 = last.get("ema_21", 0)
        if side == OrderSide.LONG and ema8 > ema21:
            score += 10
            confs.append("EMA aligned")
        elif side == OrderSide.SHORT and ema8 < ema21:
            score += 10
            confs.append("EMA aligned")

        confs.insert(0, "Learning bias signal")

        sl = close - atr * self.sl_atr_mult if side == OrderSide.LONG else close + atr * self.sl_atr_mult

        return _SetupResult(
            name="simple_bias",
            side=side,
            confidence=min(score, 100),
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # STRUCTURE SCANNER 3: Order Block Entry
    # ==================================================================

    def _scan_order_block_entry(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Price returns to an unmitigated order block zone."""
        sm = self._structure_map
        if sm is None:
            return None

        last = df.iloc[-1]
        close = float(last["close"])
        open_ = float(last["open"])
        atr = float(last.get("atr", 0))
        if atr <= 0 or np.isnan(atr):
            return None

        side = None
        confs = []
        score = 0
        target_ob = None

        # Find OB levels near current price
        ob_levels = [l for l in sm.levels if l.level_type == "order_block"]

        for ob in ob_levels:
            dist_pct = abs(close - ob.price) / close * 100
            if dist_pct > 0.5:
                continue  # too far

            if ob.side == "support" and ob.zone_low <= close <= ob.zone_high:
                # Price is inside bullish OB zone
                if close > open_:  # bullish candle confirmation
                    side = OrderSide.LONG
                    target_ob = ob
                    confs.append(f"Bullish order block entry (impulse={ob.extra.get('impulse_size', 0):.1f}x ATR)")
                    score += 30
                    break

            elif ob.side == "resistance" and ob.zone_low <= close <= ob.zone_high:
                if close < open_:  # bearish candle confirmation
                    side = OrderSide.SHORT
                    target_ob = ob
                    confs.append(f"Bearish order block entry (impulse={ob.extra.get('impulse_size', 0):.1f}x ATR)")
                    score += 30
                    break

        if side is None or target_ob is None:
            return None

        # OB strength
        score += min(target_ob.strength // 4, 20)

        # Volume
        rel_vol = float(last.get("rel_vol", 1.0))
        if not np.isnan(rel_vol) and rel_vol > 1.0:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 10

        # HTF alignment
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        # 5m confirmation
        if confirm_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("5m aligned")
            score += 10

        confidence = min(score, 100)

        if side == OrderSide.LONG:
            sl = target_ob.zone_low - close * 0.001
        else:
            sl = target_ob.zone_high + close * 0.001

        return _SetupResult(
            name="order_block_entry",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=target_ob.price,  # limit at OB midpoint
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # STRUCTURE SCANNER 4: VWAP Mean Revert
    # ==================================================================

    def _scan_vwap_mean_revert(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Price at VWAP band extreme + reversal candle."""
        sm = self._structure_map
        if sm is None or sm.vwap <= 0:
            return None

        last = df.iloc[-1]
        close = float(last["close"])
        open_ = float(last["open"])
        high = float(last["high"])
        low = float(last["low"])
        atr = float(last.get("atr", 0))
        if atr <= 0 or np.isnan(atr):
            return None

        body = abs(close - open_)
        full_range = high - low
        if full_range <= 0:
            return None

        side = None
        confs = []
        score = 0

        # LONG: Price at/below VWAP lower band + bullish reversal
        if close <= sm.vwap_lower_1 and sm.vwap_lower_1 > 0:
            lower_wick = min(open_, close) - low
            if close > open_ and lower_wick > body * 0.5:
                side = OrderSide.LONG
                confs.append(f"VWAP lower band touch (VWAP=${sm.vwap:.0f})")
                score += 30

                if close <= sm.vwap_lower_2 and sm.vwap_lower_2 > 0:
                    confs.append("Below 2nd std dev — extreme")
                    score += 10

        # SHORT: Price at/above VWAP upper band + bearish reversal
        if side is None and close >= sm.vwap_upper_1 and sm.vwap_upper_1 > 0:
            upper_wick = high - max(open_, close)
            if close < open_ and upper_wick > body * 0.5:
                side = OrderSide.SHORT
                confs.append(f"VWAP upper band touch (VWAP=${sm.vwap:.0f})")
                score += 30

                if close >= sm.vwap_upper_2 and sm.vwap_upper_2 > 0:
                    confs.append("Above 2nd std dev — extreme")
                    score += 10

        if side is None:
            return None

        # Volume
        rel_vol = float(last.get("rel_vol", 1.0))
        if not np.isnan(rel_vol) and rel_vol > 1.0:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 10

        # RSI
        rsi = float(last.get("rsi", 50))
        if side == OrderSide.LONG and rsi < 35:
            confs.append(f"RSI oversold ({rsi:.0f})")
            score += 10
        elif side == OrderSide.SHORT and rsi > 65:
            confs.append(f"RSI overbought ({rsi:.0f})")
            score += 10

        # HTF alignment
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        confidence = min(score, 100)

        # SL beyond VWAP 2nd std dev band
        if side == OrderSide.LONG:
            sl = sm.vwap_lower_2 - close * 0.001 if sm.vwap_lower_2 > 0 else close - atr * 2
        else:
            sl = sm.vwap_upper_2 + close * 0.001 if sm.vwap_upper_2 > 0 else close + atr * 2

        return _SetupResult(
            name="vwap_mean_revert",
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
        primary_df: pd.DataFrame = None, regime: str = "",
    ) -> Signal:
        """Convert a SetupResult into a Signal with pro risk framework.

        Risk framework priorities:
        1. LIQUIDATION SAFETY — SL must be well inside liquidation buffer
        2. STRUCTURE + VOLATILITY SL — max(swing SL, ATR SL), clamped 0.4-1.2%
        3. TP LEVELS — TP1≥1:1, TP2≥1.5:1, TP3≥2:1, all > 2× fees
        4. REGIME ADAPTATION — wider TPs in trends, tighter in ranges
        """
        entry = setup.entry_price

        # ══════════════════════════════════════════════════════
        # STEP 1: COMPUTE VOLATILITY-BASED SL
        # ══════════════════════════════════════════════════════
        atr_for_sl = self._confirm_atr if self._confirm_atr > 0 else setup.atr
        sl_mult = self.sl_atr_mult * getattr(self, '_sl_adjust', 1.0)  # self-optimize adjustment
        vol_sl_dist = atr_for_sl * sl_mult

        # ══════════════════════════════════════════════════════
        # STEP 2: COMPUTE STRUCTURE-BASED SL (swing high/low)
        # ══════════════════════════════════════════════════════
        struct_sl_dist = vol_sl_dist  # default = same as volatility
        if primary_df is not None and len(primary_df) >= 20:
            try:
                recent = primary_df.iloc[-20:]
                if setup.side == OrderSide.LONG:
                    # SL below recent swing low
                    swing_low = float(recent["low"].min())
                    struct_sl_dist = max(entry - swing_low, 0) + entry * 0.001  # +0.1% buffer
                else:
                    # SL above recent swing high
                    swing_high = float(recent["high"].max())
                    struct_sl_dist = max(swing_high - entry, 0) + entry * 0.001  # +0.1% buffer
            except Exception:
                pass

        # ══════════════════════════════════════════════════════
        # STEP 3: FINAL SL = max(structure, volatility), clamped 0.4-1.2%
        # ══════════════════════════════════════════════════════
        sl_dist = max(struct_sl_dist, vol_sl_dist)

        # Clamp to [0.4%, 1.2%] of entry price
        min_sl_dist = entry * self.min_sl_pct / 100   # 0.4%
        max_sl_dist = entry * self.max_sl_pct / 100   # 1.2%
        sl_dist = max(min_sl_dist, min(sl_dist, max_sl_dist))

        # Add 0.1% execution buffer for slippage
        sl_dist += entry * 0.001

        if setup.side == OrderSide.LONG:
            sl = entry - sl_dist
        else:
            sl = entry + sl_dist
        risk = sl_dist

        # ══════════════════════════════════════════════════════
        # STEP 4: LIQUIDATION SAFETY CHECK
        # ══════════════════════════════════════════════════════
        # Confidence-scaled leverage (user approved up to 50x)
        leverage = 5  # default
        leverage_map = getattr(self, 'leverage_map', {})
        for conf_threshold in sorted(leverage_map.keys(), reverse=True):
            if setup.confidence >= conf_threshold:
                leverage = leverage_map[conf_threshold]
                break

        # Tier 2: Liquidation buffer must be > 2.2× SL (was 1.25×)
        liq_buffer_pct = (100.0 / leverage) - 0.5
        liq_buffer_dist = entry * liq_buffer_pct / 100
        sl_pct_actual = risk / entry * 100

        sl_pct_of_liq = (risk / liq_buffer_dist * 100) if liq_buffer_dist > 0 else 100
        risk_status = "SAFE"

        # Tier 2: liq buffer must be ≥ 2.2× SL distance
        if liq_buffer_pct > 0 and liq_buffer_pct < sl_pct_actual * 2.2:
            if not self._is_learning:
                logger.info("%s: %s REJECTED — liq buffer %.2f%% < 2.2× SL %.2f%%",
                           symbol, setup.name, liq_buffer_pct, sl_pct_actual)
                return None
            risk_status = "WARNING"

        if liq_buffer_pct < self.liq_min_buffer_pct:
            if not self._is_learning:
                logger.info("%s: %s REJECTED — liq buffer %.1f%% < %.1f%% minimum",
                           symbol, setup.name, liq_buffer_pct, self.liq_min_buffer_pct)
                return None
            risk_status = "WARNING"

        if sl_pct_of_liq >= self.liq_reject_pct * 100:
            if not self._is_learning:
                logger.info("%s: %s REJECTED — SL uses %.0f%% of liq buffer (max 50%%)",
                           symbol, setup.name, sl_pct_of_liq)
                return None
            risk_status = "WARNING"
        elif sl_pct_of_liq >= self.liq_sl_max_pct * 100:
            risk_status = "WARNING"

        # ══════════════════════════════════════════════════════
        # STEP 5: TP LEVELS — per spec with regime adaptation
        # ══════════════════════════════════════════════════════
        tp1_rr = self.tp1_rr   # 1.0R
        tp2_rr = self.tp2_rr   # 1.5R
        tp3_rr = self.tp3_rr   # 2.0R

        # Regime adaptation per spec
        is_trending = regime in ("trending_up", "trending_down", "breakout")
        is_ranging = regime in ("ranging", "sideways", "quiet")

        if is_trending:
            # Wider TPs in trend, trailing SL
            tp2_rr *= 1.2
            tp3_rr *= 1.3
        elif is_ranging:
            # Tighter TPs, faster exits
            tp2_rr *= 0.9
            tp3_rr *= 0.8

        # HTF alignment → extend TPs (trend has room)
        if htf_bias != 0:
            is_aligned = (
                (htf_bias > 0 and setup.side == OrderSide.LONG) or
                (htf_bias < 0 and setup.side == OrderSide.SHORT)
            )
            if is_aligned:
                tp2_rr *= 1.15
                tp3_rr *= 1.2

        # Setup-specific adjustments
        if setup.name == "bb_squeeze":
            tp2_rr *= 1.2
            tp3_rr *= 1.3

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

        # ── Enforce TP1 ≥ 2× trading cost (0.4% minimum per spec) ──
        min_tp1_distance = entry * self.min_tp1_pct / 100
        if abs(tp1 - entry) < min_tp1_distance:
            if setup.side == OrderSide.LONG:
                tp1 = entry + min_tp1_distance
                tp2 = max(tp2, entry + min_tp1_distance * 1.5)
                tp3 = max(tp3, entry + min_tp1_distance * 2.0)
            else:
                tp1 = entry - min_tp1_distance
                tp2 = min(tp2, entry - min_tp1_distance * 1.5)
                tp3 = min(tp3, entry - min_tp1_distance * 2.0)

        # ── Enforce minimum R:R (1:1 per spec) ──
        actual_rr = abs(tp1 - entry) / risk if risk > 0 else 0
        if actual_rr < self.min_rr_ratio and not self._is_learning:
            logger.debug("%s: %s rejected — R:R %.2f below %.2f",
                        symbol, setup.name, actual_rr, self.min_rr_ratio)
            return None

        grade = confidence_to_grade(setup.confidence)

        if setup.confidence >= 75:
            sig_type = SignalType.BUY if setup.side == OrderSide.LONG else SignalType.SELL
        else:
            sig_type = SignalType.PRE_BUY if setup.side == OrderSide.LONG else SignalType.PRE_SELL

        eff_rr = round((tp1_rr + tp2_rr) / 2, 2)
        sl_pct = round(risk / entry * 100, 3)

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
            regime=regime or MarketRegime.SIDEWAYS,  # Use detected regime, not hardcoded
            metadata={
                "setup_type": setup.name,
                "confirmations": setup.confirmations,
                "htf_bias": htf_bias,
                "atr": round(setup.atr, 2),
                "dynamic_tp_rr": [round(tp1_rr, 2), round(tp2_rr, 2), round(tp3_rr, 2)],
                "sl_pct": sl_pct,
                "sl_source": "max(structure, volatility)",
                "risk_status": risk_status,
                "leverage": leverage,
                "liq_buffer_pct": round(liq_buffer_pct, 1),
                "fib_at_level": (fib_data or {}).get("at_fib", False),
                "fib_nearest": (fib_data or {}).get("nearest_level"),
                "choch": (choch_data or {}).get("direction") if (choch_data or {}).get("choch_detected") else None,
                "choch_strength": (choch_data or {}).get("strength", 0) if (choch_data or {}).get("choch_detected") else 0,
                "regime": regime,
                # ML training data — would_block flags
                "would_block_cooldown": getattr(self, '_last_would_block_cooldown', False),
                "would_block_rr": actual_rr < self.min_rr_ratio,
                "would_block_liq": liq_buffer_pct < self.liq_min_buffer_pct,
                "session": getattr(self, '_current_session', 'unknown'),
                "operating_mode": self.operating_mode,
            },
        )

    # ------------------------------------------------------------------
    # Signal management
    # ------------------------------------------------------------------

    def _self_optimize(self) -> None:
        """Self-optimization from last 50 trades per spec section 7.

        Adjusts SL multiplier based on empirical patterns:
        - Frequent stop-outs before reversal → widen SL slightly
        - Increasing drawdowns → tighten SL + reduce size
        - TP3 rarely hits → TPs already adjusted via spec (1:1, 1.5:1, 2:1)
        """
        now = time.time()
        if now - self._last_optimize_time < self._optimize_interval:
            return
        self._last_optimize_time = now

        by_setup = self._cached_by_setup
        if not by_setup:
            return

        # Aggregate last 50 trades across all setups
        total_trades = 0
        stop_outs_with_mfe = 0  # stopped out but MFE > 0.5R (SL too tight)
        total_mae = 0.0
        total_mfe = 0.0

        for setup_name, stats in by_setup.items():
            n = stats.get("count", 0)
            total_trades += n
            # Check if avg_mae is high relative to SL (stops too tight)
            avg_mae = stats.get("avg_mae_r", 0)
            avg_mfe = stats.get("avg_mfe_r", 0)
            total_mae += avg_mae * n
            total_mfe += avg_mfe * n

        if total_trades < 10:
            return  # not enough data

        avg_mae_all = total_mae / total_trades if total_trades > 0 else 0
        avg_mfe_all = total_mfe / total_trades if total_trades > 0 else 0

        # If average MAE is close to 1.0R (meaning trades regularly hit SL)
        # but average MFE is also high (meaning price often went our way first)
        # → SL is too tight, widen slightly
        if avg_mae_all > 0.8 and avg_mfe_all > 0.5:
            self._sl_adjust = min(self._sl_adjust + 0.05, 1.3)  # max 30% wider
            logger.info("SELF-OPT: Widening SL by %.0f%% (MAE=%.2fR, MFE=%.2fR — stops too tight)",
                       (self._sl_adjust - 1) * 100, avg_mae_all, avg_mfe_all)
        # If average MAE is low and MFE is low → trades aren't moving, tighten
        elif avg_mae_all < 0.4 and avg_mfe_all < 0.3:
            self._sl_adjust = max(self._sl_adjust - 0.05, 0.8)  # max 20% tighter
            logger.info("SELF-OPT: Tightening SL by %.0f%% (MAE=%.2fR, MFE=%.2fR — dead trades)",
                       (1 - self._sl_adjust) * 100, avg_mae_all, avg_mfe_all)

    def clear_signal(self, symbol: str) -> None:
        self._last_signal_time.pop(symbol, None)

    def record_stop_loss(self, symbol: str) -> None:
        self._last_signal_time[symbol] = time.time()

    def has_active_signal(self, symbol: str) -> bool:
        return False  # scalps don't track active signals

    def get_active_signal(self, symbol: str) -> None:
        return None
