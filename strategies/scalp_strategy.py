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
from pathlib import Path

# IST timezone
_IST = timezone(timedelta(hours=5, minutes=30))
from typing import Any, Dict, List, Optional, Tuple

from strategies.indian_market_session import IndianMarketSessionEngine, IndianSessionContext

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
    detect_regime_transition,
)
from bot.ev_engine import EVEngine
from bot.feature_logger import FeatureLogger
from bot.ml_scorer import MLScorer, ML_SERVER_URL, build_scoring_features
from bot.mode_manager import get_mode_manager
from bot.training_dataset import TrainingDataset
from data.structure import build_structure_map, StructureMap

logger = logging.getLogger("bot.strategy.scalp")

# ---------------------------------------------------------------------------
# Scanner categorization for analytics (momentum vs value)
# ---------------------------------------------------------------------------
SCANNER_CATEGORY = {
    "structure_bounce": "value",       # Mean reversion at structure levels
    "liquidity_sweep": "momentum",     # Sweep + reclaim = momentum continuation
    "bos_choch": "momentum",           # Break of structure = momentum
    "cvd_divergence": "value",         # Divergence = counter-trend value
    "rsi_divergence": "value",         # RSI divergence = counter-trend value
    "vwap_mean_revert": "value",       # Mean reversion to VWAP
    "trend_continuation": "momentum",  # Trend following = momentum
    "ema_momentum": "momentum",        # EMA crossover = momentum
    "bb_squeeze": "momentum",          # Bollinger squeeze breakout = momentum
}

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

    @property
    def confidence(self) -> int:
        """Alias for raw_score — used throughout veto layer."""
        return self.raw_score

    @confidence.setter
    def confidence(self, value: int):
        self.raw_score = value

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

    # --- Pair-specific ML probability thresholds ---
    # Overrides the hard ML veto gate on a per-pair basis.
    # Well-calibrated majors can use a slightly lower threshold;
    # less liquid / meme pairs need higher conviction.
    PAIR_ML_THRESHOLDS: Dict[str, float] = {
        "BTC/USDT": 0.48,    # BTC models are well-calibrated, slightly lower threshold OK
        "ETH/USDT": 0.48,    # Same for ETH
        "SOL/USDT": 0.50,    # Default
        "AVAX/USDT": 0.52,   # Less liquid, need higher confidence
        "LINK/USDT": 0.52,   # Less liquid
        "DOGE/USDT": 0.55,   # Meme coin, need high conviction
    }
    DEFAULT_ML_THRESHOLD: float = 0.50

    def __init__(self, config: Dict[str, Any]) -> None:
        bot_cfg = config.get("bot", {})
        strat_cfg = config.get("strategy", {})
        ind_cfg = strat_cfg.get("indicators", {})
        filt_cfg = strat_cfg.get("filters", {})
        risk_cfg = config.get("risk", {})
        tf_cfg = config.get("timeframes", {})

        exec_cfg = config.get("execution", {})
        self._order_type: str = exec_cfg.get("order_type", "maker")  # "maker" | "taker" | "auto"

        # --- Indian Market Session Engine ---
        im_cfg = config.get("indian_market", {})
        self._indian_market_enabled = im_cfg.get("enabled", False)
        if self._indian_market_enabled:
            self._indian_engine = IndianMarketSessionEngine(im_cfg)
            logger.info("Indian Market Session Engine: ENABLED (windows=%d, fno_expiry=%s)",
                        len(im_cfg.get("windows", [])), im_cfg.get("fno_expiry_day", "thursday"))
        else:
            self._indian_engine = None
            logger.info("Indian Market Session Engine: DISABLED (using legacy sessions)")
        self._session_configs = im_cfg.get("sessions", [])

        # --- Pair-specific ML thresholds (from config, falling back to class defaults) ---
        ml_cfg = config.get("ml", {})
        if ml_cfg.get("pair_thresholds"):
            self.pair_ml_thresholds = {
                str(k): float(v) for k, v in ml_cfg["pair_thresholds"].items()
            }
        else:
            self.pair_ml_thresholds = dict(self.PAIR_ML_THRESHOLDS)
        self.default_ml_threshold = float(
            ml_cfg.get("default_threshold", self.DEFAULT_ML_THRESHOLD)
        )
        logger.info(
            "ML thresholds: default=%.2f, overrides=%s",
            self.default_ml_threshold,
            {k: f"{v:.2f}" for k, v in self.pair_ml_thresholds.items()},
        )

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

        # --- Per-Scanner Minimum Thresholds (Upgrade 3) ---
        self.scanner_min_confidence = {
            "structure_bounce": 50,     # primary — veto layer handles quality
            "liquidity_sweep": 60,      # needs sweep+MSS
            "bos_choch": 65,            # needs displacement+Fib
            "trend_continuation": 60,
            "ema_momentum": 55,
            "cvd_divergence": 60,
            "rsi_divergence": 60,
            "vwap_mean_revert": 55,
            "rsi_extreme": 55,
            "bb_squeeze": 60,
            "post_impulse": 60,
            "order_block_entry": 60,
        }
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

        # --- Fee Viability Constants (Upgrade 1) ---
        self.FEE_RT_TAKER = 0.00059   # 0.059% entry only — Scalper Offer = free exit
        self.FEE_RT_MAKER = 0.00024   # maker entry only — Scalper Offer = free exit
        self.FEE_VIABILITY_MULT = 2.5 # min move must be 2.5x fees (was 4x — too strict in low vol)
        self.min_edge_high_conf = 0.18  # conservative_move ≥ 0.18% for conf 90+
        self.min_edge_low_conf = 0.25   # conservative_move ≥ 0.25% for conf < 90

        # ═══ STRUCTURE_BOUNCE_ONLY MODE ═══
        # Backtest proven: 68% WR, +164% PnL, +0.16R — ONLY profitable scanner
        # When True: only structure_bounce trades, everything else ML-only
        self.structure_bounce_only: bool = False  # ← DISABLED: all 8 scanners now active

        # Overrides when structure_bounce_only is active:
        # Was 82, now lowered to WEAK tier (50). Veto layer handles quality gating.
        self._sb_only_min_conf: int = 50       # WEAK tier threshold (veto layer handles quality)
        self._sb_only_min_tp1_pct: float = 0.72  # wider TP to cover Scalper fees
        self._sb_only_lev_cap: int = 18        # slightly conservative leverage

        # --- Tier 2: Scanner weight tiers ---
        # structure_bounce_only=True overrides these to shadow everything else
        self.scanner_size_tiers = {
            "structure_bounce": 1.0,     # ✅ Primary (82% WR)
            "order_block_entry": 0.8,    # ✅ Institutional zones
            "rsi_divergence": 0.7,       # ✅ Enabled: divergence signals
            "ema_momentum": 0.7,         # ✅ Enabled: momentum after cross
            "trend_continuation": 0.8,   # ✅ Enabled: trend pullback entry
            "vwap_mean_revert": 0.7,     # ✅ Enabled: VWAP band reversal
            "liquidity_sweep": 0.8,      # ✅ EQL/EQH sweep + displacement
            "bos_choch": 0.8,            # ✅ BOS/CHOCH with displacement
            "cvd_divergence": 0.8,       # ✅ NEW: volume-price divergence
            "simple_bias": 0.0,          # ❌ ML training only
        }
        self.scanner_auto_shadow_wr = 48  # auto-shadow if WR < 48% last 80 trades

        # Scalper window config (attached to each signal)
        # Delta India Scalper Offer (verified 2026-04-13):
        # BTC + ETH: 30 min free exit | Others: 15 min free exit
        self.scalper_windows = {"BTC": 30 * 60, "ETH": 30 * 60}

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

        # --- Research Center shadow adapter (2026-04-17) ---
        # Reads storage/research/active_vetoes.json (written by ResearchCenter UI)
        # and logs `would_veto` metadata on matching signals. SHADOW MODE only —
        # never blocks trades. Enforcement flag flips in a separate commit after
        # 7d of observation showing the vetoed cohorts are indeed losers.
        # Reloaded from disk every N scans (60s mtime check) so UI changes
        # take effect without bot restart.
        self._research_vetoes: List[Dict[str, Any]] = []
        self._research_vetoes_mtime: float = 0.0
        self._research_vetoes_check_interval: int = 60  # seconds

        # --- Research Center shadow promotion adapter (2026-04-17) ---
        # Reads storage/research/active_promotions.json (written by ResearchCenter UI
        # approve action) and attaches matching entries to prefilter context so
        # downstream scanner scoring can log `research_would_boost` metadata.
        # SHADOW MODE only — never changes trading behavior. Enforcement flag
        # flips in a separate commit after 7d of observation showing the
        # promoted cohorts sustain edge.
        self._research_promotions: List[Dict[str, Any]] = []
        self._research_promotions_mtime: float = 0.0

        # --- Scanner attrition funnel sampling (Phase 1 of scanner research, 2026-04-17) ---
        # Every ~60s per symbol, snapshot the full scanner-loop outcome
        # (allowed_scanners, which triggered, which didn't and why) to
        # storage/research/scanner_funnel.jsonl so the Research Center can
        # build per-scanner attrition reports. Pure additive logging — zero
        # effect on trading behavior. Append-only, best-effort, fail-silent.
        self._funnel_last_emit: Dict[str, float] = {}       # symbol → epoch seconds
        self._funnel_emit_interval_sec: float = 60.0

        # --- Phase 4 of scanner research: Scanner Variant Adapter (2026-04-17) ---
        # Reads storage/research/scanner_variants.json (approved by user via
        # /research UI) and applies per-scanner behavior modifications:
        #   - regime_whitelist: include scanner in more regimes (MODE_0 fix)
        #   - filter_exemption: bypass specific downstream filters (MODE_2 fix)
        #   - threshold_relax / lookback_widen: metadata only (scanner code
        #     doesn't currently honor these — proposed for future per-scanner hooks)
        #
        # SHADOW MODE (default): logs `would_*` events, no behavior change.
        # ENFORCE MODE (explicit per-scanner flip by user after 7d+30-events
        # observation): actually modifies scanner gating.
        #
        # Read-only mtime poll, fail-silent, never blocks trading.
        self._research_scanner_variants: List[Dict[str, Any]] = []
        self._research_scanner_variants_mtime: float = 0.0

        # --- Regime transition tracking (per-symbol) ---
        self._prev_regime: Dict[str, str] = {}       # symbol → previous regime
        self._regime_age: Dict[str, int] = {}         # symbol → bars held in current regime

        # --- EV Engine (expected value gating) ---
        self._ev_engine = EVEngine()
        self._last_ev_results: Dict[str, Any] = {}

        # --- ML Scorer (VM4 scoring API) ---
        # Phase 5.0a+ fix: the hardcoded default was pointing to a stale VM at
        # 129.80.31.92 which was running pre-Phase-4.2 code (last updated
        # 2026-03-21). That made every live ML call go to an ancient server
        # and bypass everything we've built — edge_verdict/family routing/
        # HTF features/ABSTAIN drift detection. The correct target is the
        # real VM4 private IP 10.0.2.4:8081. settings.yaml can still override
        # via ml.scoring_url but the default must not be a stale VM.
        ml_cfg = config.get("ml", {})
        self._ml_scorer = MLScorer(
            url=ml_cfg.get("scoring_url") or None,  # None → MLScorer resolves ML_SERVER_URL at runtime
            enabled=ml_cfg.get("enabled", True),
            shadow_mode=ml_cfg.get("shadow_mode", True),  # Start shadow — log only, no veto
        )
        self._ml_shadow_mode: bool = ml_cfg.get("shadow_mode", True)
        self._ml_veto_skip: bool = ml_cfg.get("veto_skip", True)  # block SKIP verdicts even in shadow
        self._last_ml_result: Dict[str, Any] = {}

        # Per-symbol ML probability thresholds (from calibration analysis)
        # Higher thresholds for noisier/lower-liquidity coins
        # ML thresholds: SINGLE SOURCE OF TRUTH = settings.yaml → pair_ml_thresholds
        # Removed hardcoded _ml_thresholds (was 0.62-0.70, conflicting with config 0.40-0.42)
        # ML is in shadow mode anyway — these only affect logging, not trade decisions
        self._ml_thresholds: Dict[str, float] = dict(self.pair_ml_thresholds)
        self._ml_thresholds.update(ml_cfg.get("thresholds", {}))

        # Per-scanner ATR-based SL/TP multipliers (replace fixed %)
        # Keys: (sl_atr_mult, tp1_rr, tp2_rr, tp3_rr)
        self._scanner_sl_tp: Dict[str, Dict[str, float]] = {
            "ema_momentum":       {"sl_atr": 1.2, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "trend_continuation": {"sl_atr": 1.5, "tp1_rr": 2.0, "tp2_rr": 3.0, "tp3_rr": 5.0},
            "vwap_mean_revert":   {"sl_atr": 1.0, "tp1_rr": 1.2, "tp2_rr": 2.0, "tp3_rr": 3.0},
            "rsi_divergence":     {"sl_atr": 1.2, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "structure_bounce":   {"sl_atr": 1.0, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "bb_squeeze":         {"sl_atr": 1.3, "tp1_rr": 1.8, "tp2_rr": 3.0, "tp3_rr": 5.0},
            "order_block_entry":  {"sl_atr": 1.0, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "liquidity_sweep":    {"sl_atr": 1.2, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "simple_bias":        {"sl_atr": 1.5, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
        }
        # Override from config if provided
        self._scanner_sl_tp.update(ml_cfg.get("scanner_sl_tp", {}))

        # --- Data-driven SL/TP calibration from backtest results ---
        self._calibrated_sl_tp = self._load_calibrated_sl_tp()

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

        # --- Setup lifecycle tracking (for dashboard setup cards) ---
        # {symbol: [{"scanner": name, "state": "FORMING|CONFIRMED|EXECUTABLE",
        #            "side": "long/short", "price": float, "reason": str, "updated": iso_time}]}
        self.setup_candidates: Dict[str, List[Dict[str, Any]]] = {}

        # --- Scanner co-firing correlation tracker ---
        self._scanner_cofire_log: List[Dict[str, Any]] = []  # list of {ts, symbol, scanners, timeframe}

    # ------------------------------------------------------------------
    # Scanner Co-Firing Correlation
    # ------------------------------------------------------------------

    def get_scanner_correlation(self) -> Dict[str, float]:
        """Compute co-firing frequency between scanner pairs (Jaccard-like)."""
        from collections import Counter
        if len(self._scanner_cofire_log) < 10:
            return {}
        pair_counts: Counter = Counter()
        scanner_counts: Counter = Counter()
        for entry in self._scanner_cofire_log:
            scanners = entry["scanners"]
            for s in scanners:
                scanner_counts[s] += 1
            for i, s1 in enumerate(scanners):
                for s2 in scanners[i + 1:]:
                    pair_key = tuple(sorted([s1, s2]))
                    pair_counts[pair_key] += 1

        correlations: Dict[str, float] = {}
        for (s1, s2), count in pair_counts.items():
            # Jaccard-like: co-fire / (fire_s1 + fire_s2 - co-fire)
            denom = scanner_counts[s1] + scanner_counts[s2] - count
            corr = count / denom if denom > 0 else 0
            correlations[f"{s1}+{s2}"] = round(corr, 3)
        return correlations

    # ------------------------------------------------------------------
    # Data-Driven SL/TP Calibration
    # ------------------------------------------------------------------

    def _load_calibrated_sl_tp(self) -> Dict[str, Dict[str, float]]:
        """Load scanner-specific SL/TP from backtest results if available.

        Reads /storage/ml_models/candidate_all_scanners.json for optimal
        TP/SL ratios derived from training data.  Falls back to the
        hardcoded ``_scanner_sl_tp`` defaults when no data exists.
        """
        import json as _json
        calibrated = dict(self._scanner_sl_tp)  # start from hardcoded defaults

        cal_path = Path(__file__).resolve().parent.parent / "storage" / "ml_models" / "candidate_all_scanners.json"
        if not cal_path.exists():
            logger.info("SL/TP calibration: no backtest file at %s — using hardcoded defaults", cal_path)
            return calibrated

        try:
            data = _json.loads(cal_path.read_text())
        except Exception as exc:
            logger.warning("SL/TP calibration: failed to parse %s — %s", cal_path, exc)
            return calibrated

        scanners_data = data if isinstance(data, dict) else {}
        calibrated_count = 0

        for scanner_name, defaults in self._scanner_sl_tp.items():
            scanner_stats = scanners_data.get(scanner_name)
            if not scanner_stats or not isinstance(scanner_stats, dict):
                continue

            # Extract optimal SL/TP from training results
            # Expected keys: optimal_sl_atr, optimal_tp1_rr, optimal_tp2_rr, optimal_tp3_rr
            # or: sl_atr, tp1_rr, tp2_rr, tp3_rr
            new_vals = {}
            for key, default_key in [("sl_atr", "sl_atr"), ("tp1_rr", "tp1_rr"),
                                      ("tp2_rr", "tp2_rr"), ("tp3_rr", "tp3_rr")]:
                # Check both "optimal_X" and plain "X" keys
                val = scanner_stats.get(f"optimal_{key}") or scanner_stats.get(key)
                if val is not None and isinstance(val, (int, float)) and val > 0:
                    new_vals[default_key] = float(val)

            if new_vals:
                # Sanity bounds: SL ATR mult 0.3-3.0, RR ratios 0.5-8.0
                if "sl_atr" in new_vals:
                    new_vals["sl_atr"] = max(0.3, min(3.0, new_vals["sl_atr"]))
                for rr_key in ("tp1_rr", "tp2_rr", "tp3_rr"):
                    if rr_key in new_vals:
                        new_vals[rr_key] = max(0.5, min(8.0, new_vals[rr_key]))

                # Merge with defaults (calibrated values override)
                merged = dict(defaults)
                merged.update(new_vals)
                calibrated[scanner_name] = merged
                calibrated_count += 1
                logger.info(
                    "SL/TP calibrated [%s]: sl_atr=%.2f tp1=%.1fR tp2=%.1fR tp3=%.1fR (from backtest)",
                    scanner_name,
                    merged.get("sl_atr", 0), merged.get("tp1_rr", 0),
                    merged.get("tp2_rr", 0), merged.get("tp3_rr", 0),
                )

        if calibrated_count > 0:
            logger.info("SL/TP calibration: loaded %d/%d scanners from backtest data",
                        calibrated_count, len(self._scanner_sl_tp))
        else:
            logger.info("SL/TP calibration: backtest file exists but no usable scanner data — using defaults")

        return calibrated

    # ------------------------------------------------------------------
    # Structural Pre-Filter (runs BEFORE scanners)
    # ------------------------------------------------------------------

    def _structural_prefilter(self, df, htf_df, symbol, regime) -> dict:
        """Light structural pre-filter. Soft gates, not hard blocks.

        Runs BEFORE scanners to skip obviously bad market conditions.
        Returns dict with:
            - pass: bool (False = skip this cycle entirely)
            - confidence_adj: int (-20 to +10, applied to any scanner result)
            - reason: str (why blocked, if blocked)
            - context: dict (regime, vwap_zone, atr_regime, mtf_aligned)
        """
        confidence_adj = 0
        reasons = []
        context = {"regime": regime, "vwap_zone": "normal", "atr_regime": "normal", "mtf_aligned": None}

        # ── (a) ATR Tradable Band ──
        # Use 5m ATR if available, else 1m ATR
        confirm_atr = self._confirm_atr if self._confirm_atr > 0 else 0.0
        if confirm_atr <= 0 and len(df) >= 50:
            try:
                confirm_atr = float(df["atr"].iloc[-1]) if "atr" in df.columns else 0.0
            except Exception:
                confirm_atr = 0.0

        atr_ratio = 1.0
        if confirm_atr > 0 and len(df) >= 50:
            try:
                atr_col = df["atr"] if "atr" in df.columns else None
                if atr_col is not None:
                    atr_sma50 = float(atr_col.rolling(50).mean().iloc[-1])
                    if atr_sma50 > 0:
                        atr_ratio = confirm_atr / atr_sma50
            except Exception:
                atr_ratio = 1.0

        if atr_ratio < 0.4:
            # Truly dead market — hard block
            return {
                "pass": False,
                "confidence_adj": 0,
                "reason": f"ATR PREFILTER: ratio {atr_ratio:.2f} < 0.4 — market dead",
                "context": {**context, "atr_regime": "dead"},
            }
        elif atr_ratio < 0.7:
            confidence_adj -= 10
            reasons.append(f"ATR low ({atr_ratio:.2f})")
            context["atr_regime"] = "low"
        # Adaptive ATR threshold: higher for high-beta coins
        atr_extreme_threshold = 3.5
        if symbol in ("AVAX/USDT", "DOGE/USDT", "LINK/USDT", "LTC/USDT", "ADA/USDT", "DOT/USDT", "TAO/USDT", "XRP/USDT"):
            atr_extreme_threshold = 6.0  # alt-coins have naturally higher ATR ratios
        elif symbol in ("SOL/USDT",):
            atr_extreme_threshold = 5.0  # SOL is more volatile than BTC/ETH

        if atr_ratio > atr_extreme_threshold:
            # Extreme volatility — hard block
            return {
                "pass": False,
                "confidence_adj": 0,
                "reason": f"ATR PREFILTER: ratio {atr_ratio:.2f} > {atr_extreme_threshold} — extreme volatility",
                "context": {**context, "atr_regime": "extreme"},
            }
        elif atr_ratio > 2.0:
            confidence_adj -= 5
            reasons.append(f"ATR chaotic ({atr_ratio:.2f})")
            context["atr_regime"] = "chaotic"
        else:
            context["atr_regime"] = "normal"

        # ── (b) VWAP Hybrid Veto ──
        # Architecture V2 spec: abs(dist_from_vwap) < 0.3 ATR = noise zone → HARD VETO
        # (except for vwap_mean_revert which WANTS to trade near VWAP)
        # P5 (2026-04-16): SHADOW MODE — logs would_veto but doesn't block.
        # After 7d observation, convert would_veto → hard block if vetoed trades have WR < 65%.
        #
        # Phase 4 (2026-04-17): filter_exemption variants can override this
        # veto for approved scanners. Read from context["vwap_exempt_scanners"]
        # set by downstream per-scanner emission. For now, the structural
        # prefilter is symbol-level (before per-scanner scan), so exemption
        # checks happen downstream when scanner result is evaluated.
        #
        # Layered thresholds:
        #   < 0.12 ATR = deep noise     → confidence -25 + would_veto
        #   0.12-0.30 ATR = noise zone  → confidence -20 + would_veto
        #   0.30-0.40 ATR = marginal    → confidence -10 (no veto)
        #   > 0.40 ATR = clear          → no adjustment
        _VWAP_HARD_VETO_THRESHOLD = 0.30   # Architecture V2 spec
        _VWAP_HARD_VETO_ENFORCE = False     # P5 SHADOW MODE — flip to True after validation
        try:
            last_close = float(df.iloc[-1].get("close", 0))
            last_vwap = float(df.iloc[-1].get("vwap", 0))
            _atr_for_vwap = confirm_atr if confirm_atr > 0 else float(df.iloc[-1].get("atr", 1))
            if last_vwap > 0 and _atr_for_vwap > 0:
                vwap_dist = abs(last_close - last_vwap) / _atr_for_vwap
                context["vwap_dist_atr"] = round(vwap_dist, 3)

                if vwap_dist < 0.12:
                    # Deep noise — very near VWAP
                    context["vwap_zone"] = "noise"
                    confidence_adj -= 25
                    reasons.append(f"VWAP noise zone ({vwap_dist:.3f} ATR, -25)")
                elif vwap_dist < _VWAP_HARD_VETO_THRESHOLD:
                    # V2 noise zone (< 0.30 ATR)
                    context["vwap_zone"] = "penalty"
                    confidence_adj -= 20
                    reasons.append(f"VWAP penalty zone ({vwap_dist:.2f} ATR, -20)")
                elif vwap_dist < 0.40:
                    # Marginal zone — slight penalty
                    context["vwap_zone"] = "marginal"
                    confidence_adj -= 10
                    reasons.append(f"VWAP marginal ({vwap_dist:.2f} ATR, -10)")
                else:
                    context["vwap_zone"] = "clear"

                # V2 HARD VETO: if price is within noise zone (<0.30 ATR from VWAP)
                # In shadow mode: log would_veto. In enforced mode: hard block.
                if vwap_dist < _VWAP_HARD_VETO_THRESHOLD:
                    context["vwap_would_veto"] = True
                    if _VWAP_HARD_VETO_ENFORCE:
                        return {
                            "pass": False,
                            "confidence_adj": 0,
                            "reason": f"VWAP HARD VETO: {vwap_dist:.3f} ATR < {_VWAP_HARD_VETO_THRESHOLD} (V2 spec)",
                            "context": {**context, "vwap_zone": "vetoed"},
                        }
                else:
                    context["vwap_would_veto"] = False
        except Exception:
            pass

        # ── (c) MTF Alignment ──
        # Check if HTF EMA8 > EMA21 (bullish) or EMA8 < EMA21 (bearish)
        mtf_direction = 0  # 0 = neutral, 1 = bullish, -1 = bearish
        if htf_df is not None and len(htf_df) > 5:
            try:
                htf_ema8 = float(htf_df["ema_8"].iloc[-1]) if "ema_8" in htf_df.columns else 0
                htf_ema21 = float(htf_df["ema_21"].iloc[-1]) if "ema_21" in htf_df.columns else 0
                if htf_ema8 > 0 and htf_ema21 > 0:
                    if htf_ema8 > htf_ema21:
                        mtf_direction = 1  # bullish HTF
                    elif htf_ema8 < htf_ema21:
                        mtf_direction = -1  # bearish HTF
            except Exception:
                pass
        context["mtf_aligned"] = mtf_direction
        # Note: MTF alignment scoring is applied per-signal (needs signal side),
        # so we store the direction in context for downstream use.

        # ── (d) Regime Hard Blocks ──
        if regime == "quiet" and atr_ratio < 0.4:
            return {
                "pass": False,
                "confidence_adj": 0,
                "reason": f"REGIME PREFILTER: quiet regime + ATR dead ({atr_ratio:.2f})",
                "context": context,
            }
        if regime == "low_liquidity":
            return {
                "pass": False,
                "confidence_adj": 0,
                "reason": "REGIME PREFILTER: low_liquidity — no trading",
                "context": context,
            }

        # ── Regime Transition Detection ──
        prev_regime = self._prev_regime.get(symbol, "")
        regime_age = self._regime_age.get(symbol, 0)

        if regime == prev_regime:
            self._regime_age[symbol] = regime_age + 1
        else:
            self._regime_age[symbol] = 1
            self._prev_regime[symbol] = regime
            # BotBrain: record regime transition
            if hasattr(self, '_brain') and self._brain:
                try:
                    self._brain.on_regime_change(symbol, regime, context.get("bb_width", 0.5))
                except Exception:
                    pass

        transition = detect_regime_transition(
            regime, prev_regime, self._regime_age[symbol]
        )
        if transition["confidence_adj"] != 0:
            confidence_adj += transition["confidence_adj"]
            if transition["in_transition"]:
                reasons.append(f"regime transition ({transition['transition_type']})")

        # ── Research Center shadow veto + promotion (2026-04-17) ──
        # Check if the Research Center has flagged this (scanner, regime, side)
        # as a frozen cohort OR approved a local promotion. We don't know
        # `scanner`/`side` yet at prefilter time (that's per-scanner output),
        # so we attach LOOKUP_TABLES to context and let downstream per-scanner
        # code mark `research_would_veto=True` or `research_would_boost=True`
        # on individual signals. Pure metadata, no blocking.
        try:
            self._refresh_research_vetoes()
            self._refresh_research_promotions()
            # Build a fast-lookup set of (regime, side) for any veto/promotion
            # matching this regime, regardless of scanner — so the caller can
            # quickly filter.
            applicable_vetoes = [
                v for v in self._research_vetoes
                if v.get("regime") == regime
            ]
            if applicable_vetoes:
                context["research_applicable_vetoes"] = applicable_vetoes
            applicable_promotions = [
                p for p in self._research_promotions
                if p.get("regime") == regime
            ]
            if applicable_promotions:
                context["research_applicable_promotions"] = applicable_promotions
        except Exception:
            pass  # never break prefilter on research-center read error

        return {
            "pass": True,
            "confidence_adj": confidence_adj,
            "reason": "; ".join(reasons) if reasons else "",
            "context": context,
            "regime_transition": transition,
        }

    def _refresh_research_vetoes(self) -> None:
        """Read storage/research/active_vetoes.json if mtime changed.

        Called from prefilter — CHEAP (stat + optional json load).
        Reloads at most every self._research_vetoes_check_interval seconds.
        Read-only: never writes anything, never raises.
        """
        import os, json, time
        try:
            now = time.time()
            last_check = getattr(self, "_research_vetoes_last_check", 0.0)
            if now - last_check < self._research_vetoes_check_interval:
                return
            self._research_vetoes_last_check = now

            path = Path(__file__).resolve().parent.parent / "storage" / "research" / "active_vetoes.json"
            if not path.exists():
                self._research_vetoes = []
                return

            mtime = path.stat().st_mtime
            if mtime == self._research_vetoes_mtime:
                return  # no change since last read

            with open(path) as fh:
                data = json.load(fh) or {}
            self._research_vetoes = data.get("vetoes", []) if isinstance(data, dict) else []
            self._research_vetoes_mtime = mtime
        except Exception:
            # Fail silent — research-center disk issues must never break trading
            pass

    def _refresh_research_promotions(self) -> None:
        """Read storage/research/active_promotions.json if mtime changed.

        Mirrors _refresh_research_vetoes. Loaded at most every
        self._research_vetoes_check_interval seconds (shared throttle so
        both files re-read on the same tick). Read-only, never raises.
        """
        import json
        try:
            path = Path(__file__).resolve().parent.parent / "storage" / "research" / "active_promotions.json"
            if not path.exists():
                self._research_promotions = []
                return
            mtime = path.stat().st_mtime
            if mtime == self._research_promotions_mtime:
                return
            with open(path) as fh:
                data = json.load(fh) or {}
            self._research_promotions = data.get("promotions", []) if isinstance(data, dict) else []
            self._research_promotions_mtime = mtime
        except Exception:
            pass

    def _refresh_research_scanner_variants(self) -> None:
        """Read storage/research/scanner_variants.json if mtime changed.
        Mirrors _refresh_research_vetoes / _refresh_research_promotions.
        Read-only, fail-silent, ~1 poll per minute via prefilter throttle."""
        import json
        try:
            path = Path(__file__).resolve().parent.parent / "storage" / "research" / "scanner_variants.json"
            if not path.exists():
                self._research_scanner_variants = []
                return
            mtime = path.stat().st_mtime
            if mtime == self._research_scanner_variants_mtime:
                return
            with open(path) as fh:
                data = json.load(fh) or {}
            self._research_scanner_variants = data.get("variants", []) if isinstance(data, dict) else []
            self._research_scanner_variants_mtime = mtime
        except Exception:
            pass

    def _get_scanner_variants_for(self, scanner_name: str, variant_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return active variants matching scanner_name (and optionally type).
        Safe: returns [] on any error or empty list."""
        try:
            vs = self._research_scanner_variants or []
            out = []
            for v in vs:
                if v.get("scanner") != scanner_name:
                    continue
                if variant_type is not None:
                    ptype = (v.get("proposal") or {}).get("type")
                    if ptype != variant_type:
                        continue
                out.append(v)
            return out
        except Exception:
            return []

    def _emit_variant_event(self, kind: str, payload: Dict[str, Any]) -> None:
        """Append a variant-related event (would_fire / would_exempt / enforced)
        to the funnel log. Used for shadow-mode observation tracking by the
        Research Center to determine when a variant is ready to enforce.

        Rate-limited implicitly by the caller (only fires on actual scanner
        events). Atomic single-write append, fail-silent.
        """
        import json as _json
        try:
            path = Path(__file__).resolve().parent.parent / "storage" / "research" / "scanner_funnel.jsonl"
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                **payload,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as fh:
                fh.write(_json.dumps(record, default=str) + "\n")
        except Exception:
            pass

    def _apply_regime_whitelist_variants(
        self,
        regime: str,
        symbol: str,
        current_allowed: List[Any],
        scanner_method_map: Dict[str, Any],
    ) -> List[Any]:
        """Phase-4 activation hook: consult approved `regime_whitelist` variants
        to potentially expand the allowed_scanners list for this regime.

        Shadow mode: logs `variant_would_add_scanner` events, returns current_allowed unchanged.
        Enforce mode: returns current_allowed PLUS the whitelisted scanner methods.

        Bounded by the scanner_method_map — if variant requests a scanner not
        in the map (old/renamed/unknown), it's silently skipped.

        Additionally: emits shadow events for filter_exemption variants (for
        observation tracking only — enforcement of filter bypass is deferred
        to a separate commit once observation validates). This gives the
        Research Center readiness data without any hot-path risk.
        """
        try:
            variants = self._research_scanner_variants or []
            if not variants:
                return current_allowed
            out = list(current_allowed)
            already_names = {s.__name__.replace("_scan_", "") for s in out}
            for v in variants:
                proposal = v.get("proposal") or {}
                ptype = proposal.get("type")
                scn_name = v.get("scanner")
                if not scn_name:
                    continue
                mode = v.get("mode") or "shadow"

                # --- regime_whitelist: add scanner to allowed list ---
                if ptype == "regime_whitelist":
                    target_regimes = proposal.get("target_regimes") or []
                    if regime not in target_regimes:
                        continue
                    if scn_name in already_names:
                        continue
                    scanner_fn = scanner_method_map.get(scn_name)
                    if scanner_fn is None:
                        continue
                    if mode == "enforce":
                        out.append(scanner_fn)
                        already_names.add(scn_name)
                        self._emit_variant_event("variant_enforced", {
                            "variant_type": "regime_whitelist",
                            "scanner": scn_name,
                            "symbol": symbol,
                            "regime": regime,
                            "action": "added_to_allowed",
                        })
                    else:
                        # Shadow: log what we WOULD do
                        self._emit_variant_event("variant_would_fire", {
                            "variant_type": "regime_whitelist",
                            "scanner": scn_name,
                            "symbol": symbol,
                            "regime": regime,
                            "mode": "shadow",
                            "would_add": True,
                        })

                # --- filter_exemption: shadow-only observation this commit ---
                elif ptype == "filter_exemption":
                    # Log observation events whenever this scanner IS being
                    # run in this regime — independent of whether it actually
                    # triggers. Observation = "this opportunity exists."
                    # Actual filter bypass deferred to follow-up after
                    # shadow_observation validates the variant.
                    if scn_name in already_names:
                        self._emit_variant_event("variant_observed", {
                            "variant_type": "filter_exemption",
                            "scanner": scn_name,
                            "symbol": symbol,
                            "regime": regime,
                            "mode": mode,
                            "exempt_candidates": proposal.get("exempt_candidates", []),
                        })

                # --- threshold_relax / lookback_widen / generic_relax ---
                elif ptype in ("threshold_relax", "lookback_widen", "generic_relax"):
                    # Metadata-only for now: scanner code doesn't yet honor
                    # these runtime parameter overrides. Observation event is
                    # emitted so the Research Lab can track how often the
                    # scanner was attempted and still failed — informing
                    # whether tuning this specific scanner is worth building
                    # a dedicated code hook for.
                    if scn_name in already_names:
                        self._emit_variant_event("variant_observed", {
                            "variant_type": ptype,
                            "scanner": scn_name,
                            "symbol": symbol,
                            "regime": regime,
                            "mode": mode,
                        })
            return out
        except Exception:
            # Never break allowed_scanners computation on variant-logic error
            return current_allowed

    def _emit_funnel_sample(
        self,
        symbol: str,
        regime: str,
        atr_ratio: float,
        allowed_scanners: List[Any],
        scan_results: List[Any],
        session_id: Optional[str] = None,
    ) -> None:
        """Append a compact snapshot of scanner-loop outcome to
        storage/research/scanner_funnel.jsonl. Rate-limited per-symbol to
        self._funnel_emit_interval_sec (default 60s) so the file stays
        manageable (~1 line per symbol per minute = ~1.5 MB/day for 16 symbols).

        Wrapped broadly in try/except — funnel logging must NEVER interrupt
        the trading hot-path. Atomic append via single write() call.
        """
        import json as _json, time as _time
        try:
            now = _time.time()
            last = self._funnel_last_emit.get(symbol, 0.0)
            if now - last < self._funnel_emit_interval_sec:
                return
            self._funnel_last_emit[symbol] = now

            # Build compact result list
            results = []
            for sr in scan_results:
                if sr.setup_result is not None:
                    results.append({
                        "scanner": sr.scanner_name,
                        "triggered": True,
                        "score": round(sr.weighted_score, 1),
                        "side": sr.side.value if sr.side else None,
                        "status": sr.scanner_status,
                        "weight": round(sr.scanner_weight, 2),
                    })
                else:
                    reason = ""
                    if sr.penalties:
                        reason = sr.penalties[0] if isinstance(sr.penalties[0], str) else str(sr.penalties[0])
                    results.append({
                        "scanner": sr.scanner_name,
                        "triggered": False,
                        "reason": (reason or "")[:160],  # cap length
                        "proximity": round(sr.raw_score, 1),
                        "status": sr.scanner_status,
                        "weight": round(sr.scanner_weight, 2),
                    })

            allowed_names = [s.__name__.replace("_scan_", "") for s in allowed_scanners]

            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "regime": regime,
                "atr_ratio": round(float(atr_ratio or 0), 3),
                "allowed": allowed_names,
                "session_id": session_id,
                "results": results,
            }

            path = Path(__file__).resolve().parent.parent / "storage" / "research" / "scanner_funnel.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as fh:
                fh.write(_json.dumps(record, default=str) + "\n")
        except Exception:
            # Funnel logging must never affect trading — swallow all errors
            pass

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

        # DEBUG: confirm analyze() is being entered
        _enter_key = f"_analyze_enter_{symbol}"
        _enter_cnt = getattr(self, _enter_key, 0) + 1
        setattr(self, _enter_key, _enter_cnt)
        if _enter_cnt <= 3 or _enter_cnt % 100 == 0:
            logger.info("DIAG %s | analyze() entered #%d | tfs=%s | primary_tf=%s",
                       symbol, _enter_cnt, list(candles_dict.keys()), self.primary_tf)

        # Get primary (1m) data
        primary_df = candles_dict.get(self.primary_tf)
        if primary_df is None or len(primary_df) < 50:
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": "Insufficient candle data (<50 bars)",
                "indicators": {}, "setups_checked": [],
            }
            return []

        # Confirmation and HTF — optional but add confidence
        confirm_df = candles_dict.get(self.confirm_tf)
        htf_df = candles_dict.get(self.htf)
        # 5m df for trend/momentum scanners (they need cleaner signal than 1m)
        df_5m = candles_dict.get("5m")
        # 1h df for macro trend filter (Phase 1 MTF chain)
        df_1h = candles_dict.get("1h")
        df_4h = candles_dict.get("4h")  # session-level bias (new MTF layer)

        # ── Phase 5.0a: BTC cross-asset cache ──
        # When analyze() runs for BTC/USDT, snapshot its 5m df into an instance
        # cache. Other symbols read from that cache when building ML features
        # so the model can learn from BTC's regime/trend when scoring alts.
        # Worst case staleness: 1 bar (few seconds to one minute) — fine for
        # 5m candle context features.
        if not hasattr(self, "_btc_df_cache"):
            self._btc_df_cache = None
        if symbol == "BTC/USDT" and df_5m is not None and len(df_5m) >= 50:
            self._btc_df_cache = df_5m

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

        # ── SESSION-AWARE GATING (config-driven + Indian Market Engine) ──
        # (Disabled in backtesting via _session_gate_enabled=False)
        if getattr(self, '_session_gate_enabled', True) is False:
            self._current_session = "europe"
            self._session_min_confidence = self.min_confidence
            self._session_penalty = 0
            self._indian_ctx = None
        else:
            ist_now = datetime.now(_IST)
            ist_hour = ist_now.hour + ist_now.minute / 60.0

            # --- Indian Market Session Engine (if enabled) ---
            self._indian_ctx: Optional[IndianSessionContext] = None
            if self._indian_engine is not None:
                self._indian_ctx = self._indian_engine.evaluate(ist_now)

                # Hard block during NSE auction noise (9:00-9:30 IST)
                if self._indian_ctx.block:
                    _im_block_key = f"_im_block_count_{symbol}"
                    _im_cnt = getattr(self, _im_block_key, 0) + 1
                    setattr(self, _im_block_key, _im_cnt)
                    if _im_cnt <= 3 or _im_cnt % 50 == 0:
                        logger.info("FUNNEL %s | INDIAN MARKET BLOCK #%d | %s: %s",
                                    symbol, _im_cnt, self._indian_ctx.session_name,
                                    self._indian_ctx.block_reason)
                    self.last_scan_status[symbol] = {
                        "time": now_iso, "signal": False,
                        "reason": f"INDIAN MARKET BLOCK: {self._indian_ctx.block_reason}",
                        "indicators": {}, "setups_checked": 0,
                        "funnel": dict(self._funnel),
                    }
                    return []

            # --- Determine session (config-driven or legacy fallback) ---
            self._session_penalty: int = 0
            self._session_min_confidence = 65
            self._current_session = "unknown"

            if self._session_configs:
                # Config-driven session matching
                for sess in self._session_configs:
                    s = float(sess.get("start_hour", 0))
                    e = float(sess.get("end_hour", 0))
                    if e < s:  # wraps midnight (e.g. us: 20.5 → 2.5)
                        in_window = ist_hour >= s or ist_hour < e
                    else:
                        in_window = s <= ist_hour < e
                    if in_window:
                        self._current_session = sess.get("name", "unknown")
                        self._session_penalty = int(sess.get("confidence_adj", 0))
                        self._session_min_confidence = int(sess.get("min_confidence", 65))
                        break
            else:
                # Legacy hardcoded fallback (original behavior)
                if 2.5 <= ist_hour < 9.0:
                    self._current_session = "asia_late"
                    self._session_min_confidence = 65
                    self._session_penalty = -15
                elif 9.0 <= ist_hour < 13.5:
                    self._current_session = "asia_early"
                    self._session_min_confidence = 65
                    self._session_penalty = -5
                elif 13.5 <= ist_hour < 20.5:
                    self._current_session = "europe"
                    self._session_min_confidence = 65
                    self._session_penalty = +5
                else:
                    self._current_session = "us"
                    self._session_min_confidence = 65
                    self._session_penalty = 0

            # --- Indian market overlay: flow hours override session penalty ---
            if self._indian_ctx and self._indian_ctx.is_indian_flow_hour:
                self._session_penalty = self._indian_ctx.confidence_adjustment
                if self._indian_ctx.is_fno_expiry_day:
                    self._session_penalty += self._indian_ctx.fno_expiry_boost
                self._current_session = self._indian_ctx.session_name

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

        # --- 1H MACRO TREND FILTER (Phase 1 MTF chain) ---
        # If 1h data available, compute macro trend direction
        # This acts as a strong directional filter — don't fight the hourly trend
        macro_bias = 0  # 0 = neutral, 1 = bullish, -1 = bearish
        if df_1h is not None and len(df_1h) >= 20:
            try:
                h1_close = float(df_1h.iloc[-1]["close"])
                h1_ema21 = float(df_1h.iloc[-1].get("ema_21", 0))
                h1_ema50 = float(df_1h.iloc[-1].get("ema_50", 0))
                if h1_ema21 > 0 and h1_ema50 > 0:
                    if h1_close > h1_ema21 and h1_ema21 > h1_ema50:
                        macro_bias = 1   # strong bullish
                    elif h1_close < h1_ema21 and h1_ema21 < h1_ema50:
                        macro_bias = -1  # strong bearish
                    elif h1_close > h1_ema50:
                        macro_bias = 1   # mild bullish
                    elif h1_close < h1_ema50:
                        macro_bias = -1  # mild bearish

                # Calculate 1h EMA slope for trend strength
                if len(df_1h) >= 5 and "ema_21" in df_1h.columns:
                    h1_ema_now = float(df_1h.iloc[-1]["ema_21"])
                    h1_ema_prev = float(df_1h.iloc[-4]["ema_21"])
                    h1_atr = float(df_1h.iloc[-1].get("atr", 1))
                    if h1_atr > 0 and not np.isnan(h1_ema_now) and not np.isnan(h1_ema_prev):
                        h1_slope = (h1_ema_now - h1_ema_prev) / h1_atr
                        # Strong 1h trend: amplify macro_bias
                        if abs(h1_slope) > 0.5:
                            macro_bias = 1 if h1_slope > 0 else -1
            except Exception:
                pass

        # Store macro_bias for veto layer (indicators dict created later, use _macro_bias temp)
        # --- 4H SESSION BIAS (new MTF layer) ---
        session_bias = 0  # 0=neutral, 1=bullish, -1=bearish
        if df_4h is not None and len(df_4h) >= 10:
            try:
                from data.indicators import calc_ema
                h4_close = float(df_4h.iloc[-1]["close"])
                _h4_ema21 = calc_ema(df_4h, 21)
                _h4_ema50 = calc_ema(df_4h, 50)
                if len(_h4_ema21) > 0 and len(_h4_ema50) > 0:
                    h4_e21 = float(_h4_ema21.iloc[-1])
                    h4_e50 = float(_h4_ema50.iloc[-1])
                    if h4_close > h4_e21 > h4_e50:
                        session_bias = 1
                    elif h4_close < h4_e21 < h4_e50:
                        session_bias = -1
            except Exception:
                pass

        # Combine 4h + 1h: agree=amplify, disagree=dampen
        if session_bias != 0 and macro_bias != 0:
            if session_bias == macro_bias:
                pass  # strong alignment — keep macro_bias as-is
            else:
                macro_bias = 0  # 4h and 1h disagree — go neutral
        elif session_bias != 0 and macro_bias == 0:
            macro_bias = session_bias  # 4h takes over when 1h is neutral

        _macro_bias = macro_bias
        _macro_bias_str = "bullish" if macro_bias > 0 else "bearish" if macro_bias < 0 else "neutral"

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

        # Add macro_bias to indicators (was computed earlier before dict existed)
        indicators["macro_bias"] = _macro_bias

        # --- Stochastic + OBV on primary_df for scanner access ---
        try:
            from data.indicators import calc_stochastic, calc_obv_slope
            _sk, _sd = calc_stochastic(primary_df)
            primary_df = primary_df.copy()
            primary_df["stoch_k"] = _sk
            primary_df["stoch_d"] = _sd
            primary_df["obv_slope"] = calc_obv_slope(primary_df)
            indicators["stoch_k"] = round(float(_sk.iloc[-1]), 1)
            indicators["stoch_d"] = round(float(_sd.iloc[-1]), 1)
            indicators["obv_slope"] = round(float(primary_df["obv_slope"].iloc[-1]), 3)
        except Exception as _stoch_err:
            indicators.setdefault("stoch_k", 50.0)
            indicators.setdefault("stoch_d", 50.0)
            indicators.setdefault("obv_slope", 0.0)

            indicators["obv_slope"] = 0
        indicators["session_bias"] = session_bias
        indicators["session_bias_str"] = "bullish" if session_bias > 0 else "bearish" if session_bias < 0 else "neutral"
        indicators["macro_bias_str"] = _macro_bias_str

        # --- Detect market regime + regime age tracking ---
        regime = self._regime_filter.detect_regime(indicators, df=primary_df)  # P0: advanced detector with tightened thresholds
#DISABLED#         # --- P2: 4h session bias can upgrade ranging → trending ---
#DISABLED#         # If 4h has strong direction but 5m is "ranging", the higher TF wins
#DISABLED#         if regime in ("ranging", "sideways") and session_bias != 0:
#DISABLED#             regime = "trending_up" if session_bias > 0 else "trending_down"
#DISABLED#             logger.debug("P2 OVERRIDE: 4h session_bias=%d upgraded regime to %s", session_bias, regime)

        # Regime age: how many consecutive scans this regime has been active
        if not hasattr(self, '_regime_history'):
            self._regime_history = {}  # symbol → {"regime": str, "age": int}
        prev_regime_info = self._regime_history.get(symbol, {"regime": "", "age": 0})
        if prev_regime_info["regime"] == regime:
            regime_age = prev_regime_info["age"] + 1
        else:
            regime_age = 1  # new regime
        self._regime_history[symbol] = {"regime": regime, "age": regime_age}

        self._last_regime_info[symbol] = {
            "regime": regime,
            "regime_age": regime_age,
            "action": {},
            "indicators_snapshot": {
                "ema_8": indicators.get("ema_8", 0),
                "ema_21": indicators.get("ema_21", 0),
                "ema_50": indicators.get("ema_50", 0),
                "bb_bandwidth": float(last_row.get("bb_bandwidth", 0)) if not np.isnan(last_row.get("bb_bandwidth", 0)) else 0,
            },
        }

        # ══════════════════════════════════════════════════════
        # STRUCTURAL PRE-FILTER — runs BEFORE scanners
        # Light structural checks: ATR band, VWAP noise, MTF, regime blocks
        # ══════════════════════════════════════════════════════
        prefilter = self._structural_prefilter(df, htf_df, symbol, regime)
        if not prefilter["pass"]:  # ALWAYS enforce — no learning bypass
            self._funnel["blocked_regime"] = self._funnel.get("blocked_regime", 0) + 1
            # Log every 10th block per symbol to avoid spam
            block_key = f"_prefilter_block_count_{symbol}"
            cnt = getattr(self, block_key, 0) + 1
            setattr(self, block_key, cnt)
            if cnt <= 3 or cnt % 50 == 0:
                logger.info("FUNNEL %s | PREFILTER BLOCK #%d | regime=%s | reason=%s",
                           symbol, cnt, regime, prefilter.get("reason", "?"))
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": prefilter.get("reason") or prefilter.get("reasons", "?"),
                "indicators": indicators, "setups_checked": [],
                "funnel": dict(self._funnel),
            }
            return []

        # Store prefilter context for downstream use (confidence adjustments, MTF direction)
        self._prefilter_result = prefilter

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
            "_scan_bos_choch": "BOS/CHOCH",
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

        # BOS/CHOCH: needs structural break with displacement
        scanner_diagnostics["BOS/CHOCH"] = "Scanning for break of structure with displacement > 0.4x ATR"

        # Liquidity Sweep: needs stop hunt at equal highs/lows with reclaim
        scanner_diagnostics["Liquidity Sweep"] = "Scanning for stop hunt at equal highs/lows with reclaim"

        # (funnel reset moved to per-symbol init above)

        # ══════════════════════════════════════════════════════
        # REGIME-FIRST ROUTER — Only run scanners allowed in current regime
        # This is THE core change: regime gates which scanners fire.
        # ══════════════════════════════════════════════════════
        REGIME_SCANNER_ROUTING = {
            # --- TRENDING: all scanners + P5 additions ---
            "trending_up": [
                self._scan_trend_continuation,
                self._scan_ema_momentum,
                self._scan_structure_bounce,
                self._scan_bos_choch,
                self._scan_liquidity_sweep,
                self._scan_cvd_divergence,
                self._scan_vwap_mean_revert,
                self._scan_rsi_divergence,
                self._scan_post_impulse,             # P5: catch re-entry after impulse
                self._scan_bb_squeeze,               # P5: squeeze breakout in trend
                self._scan_rsi_extreme,              # P5: extreme RSI reversal
            ],
            "trending_down": [
                self._scan_trend_continuation,
                self._scan_ema_momentum,
                self._scan_structure_bounce,
                self._scan_bos_choch,
                self._scan_liquidity_sweep,
                self._scan_cvd_divergence,
                self._scan_vwap_mean_revert,
                self._scan_rsi_divergence,
                self._scan_bb_squeeze,               # P5: squeeze breakout
                self._scan_rsi_extreme,              # P5: extreme RSI
            ],
            # --- BREAKOUT: momentum + structure ---
            "breakout": [
                self._scan_bos_choch,
                self._scan_structure_bounce,
                self._scan_order_block_entry,
                self._scan_ema_momentum,
                self._scan_liquidity_sweep,
                self._scan_bb_squeeze,               # P5: squeeze = breakout signal
                self._scan_trend_continuation,       # P1: trend starts from breakout
            ],
            # --- RANGING: P1 expanded from 4 → 9 scanners ---
            "ranging": [
                self._scan_liquidity_sweep,
                self._scan_vwap_mean_revert,
                self._scan_structure_bounce,
                self._scan_rsi_divergence,
                self._scan_cvd_divergence,           # P1: volume-price div ideal for ranging
                self._scan_bos_choch,                # P1: structure breaks signal range exit
                self._scan_order_block_entry,        # P1: institutional levels work always
                self._scan_rsi_extreme,              # P5: extreme RSI reversal at range edges
                self._scan_bb_squeeze,               # P5: squeeze breakout = range exit
            ],
            # --- SIDEWAYS: same as ranging ---
            "sideways": [
                self._scan_liquidity_sweep,
                self._scan_vwap_mean_revert,
                self._scan_structure_bounce,
                self._scan_rsi_divergence,
                self._scan_cvd_divergence,
                self._scan_bos_choch,
                self._scan_order_block_entry,
                self._scan_rsi_extreme,
                self._scan_bb_squeeze,
            ],
            # --- VOLATILE: P5 expanded ---
            "volatile": [
                self._scan_structure_bounce,
                self._scan_bos_choch,
                self._scan_order_block_entry,
                self._scan_liquidity_sweep,
                self._scan_rsi_extreme,              # P5: extreme RSI in volatile = strong
                self._scan_rsi_divergence,           # P5: divergence in volatile
            ],
            "high_volatility": [
                self._scan_structure_bounce,
                self._scan_bos_choch,
                self._scan_order_block_entry,
                self._scan_liquidity_sweep,
                self._scan_rsi_extreme,
                self._scan_rsi_divergence,
            ],
            # --- MEAN_REVERSION: very limited — data: 7.7% WR with full set ---
            "mean_reversion": [
                self._scan_liquidity_sweep,          # only sweep setups in dead markets
                self._scan_structure_bounce,          # strong S/R only
            ],
            # --- QUIET: limited set (P5: not completely empty) ---
            "quiet": [
                self._scan_liquidity_sweep,          # sweeps work in quiet
                self._scan_structure_bounce,         # S/R still valid
                self._scan_rsi_extreme,              # P5: extreme RSI in quiet
            ],
            "low_liquidity": [],  # NO TRADING — volume too thin
        }

        # ── Per-symbol cooling period: skip after 3 consecutive losses ──
        cool_ts_key = f"_cool_until_{symbol}"
        cool_until = getattr(self, cool_ts_key, 0)
        if time.time() < cool_until:
            remaining = int(cool_until - time.time())
            if remaining % 600 < 5:  # log roughly every 10 min
                logger.info("FUNNEL %s | COOLING OFF | %d sec remaining after 3 consecutive losses", symbol, remaining)
            return []

        # Get allowed scanners for current regime
        allowed_scanners = REGIME_SCANNER_ROUTING.get(regime, [])

        # --- Phase 4: Scanner Variant Adapter (regime_whitelist variants) ---
        # Consult approved variants to potentially expand allowed_scanners.
        # Shadow mode logs `variant_would_fire`; enforce mode adds scanners.
        # Refreshed via mtime poll — cheap ~1x/min.
        try:
            self._refresh_research_scanner_variants()
            if self._research_scanner_variants:
                # Build full scanner method map once for variant lookup
                _all_scanner_map = {
                    "ema_momentum": self._scan_ema_momentum,
                    "vwap_bounce": self._scan_vwap_bounce,
                    "trend_continuation": self._scan_trend_continuation,
                    "rsi_divergence": self._scan_rsi_divergence,
                    "supertrend_flip": self._scan_supertrend_flip,
                    "bb_squeeze": self._scan_bb_squeeze,
                    "structure_bounce": self._scan_structure_bounce,
                    "liquidity_sweep": self._scan_liquidity_sweep,
                    "bos_choch": self._scan_bos_choch,
                    "cvd_divergence": self._scan_cvd_divergence,
                    "simple_bias": self._scan_simple_bias,
                    "order_block_entry": self._scan_order_block_entry,
                    "vwap_mean_revert": self._scan_vwap_mean_revert,
                    "rsi_extreme": self._scan_rsi_extreme,
                    "momentum_ride": self._scan_momentum_ride,
                    "bb_band_walk": self._scan_bb_band_walk,
                    "post_impulse": self._scan_post_impulse,
                    "momentum_surge": self._scan_momentum_surge,
                }
                allowed_scanners = self._apply_regime_whitelist_variants(
                    regime=regime,
                    symbol=symbol,
                    current_allowed=allowed_scanners,
                    scanner_method_map=_all_scanner_map,
                )
        except Exception:
            pass  # Never fail the scan loop on variant-adapter error

        # ── Indian Market Regime Override ──
        # During Indian flow hours, if regime is "quiet", override to allow
        # a limited set of ranging scanners. Indian retail flow creates setups
        # the regime detector misses. NEVER override trending/breakout/volatile.
        indian_ctx = getattr(self, '_indian_ctx', None)
        if indian_ctx and indian_ctx.regime_override == "ranging_limited" and not allowed_scanners:
            if regime in ("quiet",):
                _scanner_map = {
                    "liquidity_sweep": self._scan_liquidity_sweep,
                    "vwap_mean_revert": self._scan_vwap_mean_revert,
                    "structure_bounce": self._scan_structure_bounce,
                    "rsi_divergence": self._scan_rsi_divergence,
                }
                _ranging_names = self._indian_engine.get_ranging_limited_scanners() if self._indian_engine else []
                allowed_scanners = [_scanner_map[s] for s in _ranging_names if s in _scanner_map]
                _im_ovr_key = f"_im_override_count_{symbol}"
                _im_ovr_cnt = getattr(self, _im_ovr_key, 0) + 1
                setattr(self, _im_ovr_key, _im_ovr_cnt)
                if _im_ovr_cnt <= 3 or _im_ovr_cnt % 100 == 0:
                    logger.info("FUNNEL %s | INDIAN MARKET OVERRIDE #%d | quiet→ranging_limited | %s | scanners=%s",
                                symbol, _im_ovr_cnt, indian_ctx.session_label,
                                [s for s in _ranging_names if s in _scanner_map])

        # In paper_learning mode: STILL respect regime routing, but add
        # liquidity_sweep to all regimes for data collection.
        # DO NOT override regime routing — that's what caused garbage trades.
        if self._is_learning and allowed_scanners:
            # Add data-collection scanners to regime-routed list (not override it)
            learning_extras = [self._scan_liquidity_sweep, self._scan_simple_bias]
            for extra in learning_extras:
                if extra not in allowed_scanners:
                    allowed_scanners.append(extra)
        elif self._is_learning and not allowed_scanners:
            # Quiet/low_liquidity regime: still NO TRADING even in learning mode
            pass

        if not allowed_scanners:
            block_key = f"_regime_block_count_{symbol}"
            cnt = getattr(self, block_key, 0) + 1
            setattr(self, block_key, cnt)
            if cnt <= 3 or cnt % 50 == 0:
                logger.info("FUNNEL %s | REGIME BLOCK #%d | regime=%s — no scanners allowed",
                           symbol, cnt, regime)
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"REGIME VETO: {regime} — no scanners allowed",
                "indicators": indicators, "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            return []

        scan_results: List[ScanResult] = []

        # Funnel diagnostic: log which scanners are being tried
        pass_key = f"_scanner_pass_count_{symbol}"
        pass_cnt = getattr(self, pass_key, 0) + 1
        setattr(self, pass_key, pass_cnt)
        if pass_cnt <= 3 or pass_cnt % 100 == 0:
            scanner_list = [s.__name__.replace("_scan_", "") for s in allowed_scanners]
            logger.info("FUNNEL %s | SCANNERS RUNNING #%d | regime=%s | scanners=%s | atr_ratio=%.2f",
                       symbol, pass_cnt, regime, scanner_list, self._atr_ratio)

        for scanner in allowed_scanners:
            label = scanner_names.get(scanner.__name__, scanner.__name__)
            setup_name = scanner.__name__.replace("_scan_", "")
            diag = scanner_diagnostics.get(label, "")
            scanner_weight = self._weight_manager.get_weight(setup_name)
            scanner_status = self._weight_manager.get_status(setup_name)

            self._funnel["scanned"] += 1

            try:
                # MTF scanner routing:
                # All non-structure scanners → 5m primary (6-month backtest proves 5m is best)
                # Only structure_bounce stays on 1m (it needs fast wick detection)
                _5m_scanners = (
                    self._scan_trend_continuation, self._scan_ema_momentum, self._scan_bos_choch,
                    self._scan_cvd_divergence, self._scan_rsi_divergence, self._scan_vwap_mean_revert,
                )
                if scanner in _5m_scanners and df_5m is not None and len(df_5m) >= 50:
                    scanner_df = df_5m
                else:
                    scanner_df = df

                # Try 15m first for structure/bos scanners (higher TF = higher quality)
                result = None
                # Try 15m FIRST for ALL scanners (universal quality opportunity)
                if confirm_df is not None and len(confirm_df) >= 30:
                    result = scanner(symbol, confirm_df, htf_bias, confirm_bias)
                    if result is not None:
                        # 15m signal gets a quality bonus
                        result = _SetupResult(
                            name=result.name, side=result.side,
                            confidence=min(result.confidence + 10, 100),
                            confirmations=result.confirmations + ["15m timeframe (+10 quality)"],
                            entry_price=result.entry_price, stop_loss=result.stop_loss, atr=result.atr,
                        )

                # Fall back to primary TF if 15m didn't trigger
                if result is None:
                    result = scanner(symbol, scanner_df, htf_bias, confirm_bias)

                if result is not None:
                    # --- 4h Session Bias Scoring Bonus ---
                    _sess_bias = indicators.get("session_bias", 0)
                    if _sess_bias != 0:
                        _side_val = 1 if result.side == OrderSide.LONG else -1
                        if _sess_bias == _side_val:
                            # Trade aligns with 4h session — bonus
                            result = _SetupResult(
                                name=result.name, side=result.side,
                                confidence=min(result.confidence + 8, 100),
                                confirmations=result.confirmations + ["4h session aligned (+8)"],
                                entry_price=result.entry_price, stop_loss=result.stop_loss, atr=result.atr,
                            )
                        elif _sess_bias == -_side_val:
                            # Trade against 4h session — penalty
                            result = _SetupResult(
                                name=result.name, side=result.side,
                                confidence=max(result.confidence - 12, 0),
                                confirmations=result.confirmations + ["4h session conflict (-12)"],
                                entry_price=result.entry_price, stop_loss=result.stop_loss, atr=result.atr,
                            )

                    # --- Fib 0.618 Confluence Bonus ---
                    if result.name in ("structure_bounce", "bos_choch", "liquidity_sweep") and fib_data.get("at_fib"):
                        _fib_trend = fib_data.get("trend", "unknown")
                        _side_ok = (result.side == OrderSide.LONG and _fib_trend == "up") or                                    (result.side == OrderSide.SHORT and _fib_trend == "down")
                        if _side_ok:
                            result = _SetupResult(
                                name=result.name, side=result.side,
                                confidence=min(result.confidence + 10, 100),
                                confirmations=result.confirmations + ["Fib 0.618 confluence (+10)"],
                                entry_price=result.entry_price, stop_loss=result.stop_loss, atr=result.atr,
                            )

                    # Scanner triggered — compute weighted score
                    raw_score = result.confidence
                    # Apply scanner performance weight to confidence
                    adjusted_conf = self._weight_manager.get_confidence_adjustment(setup_name, raw_score)
                    weighted = adjusted_conf * scanner_weight
                    tier = _tier_from_score(weighted)
                    confs = list(result.confirmations)
                    penalties = []

                    # ── Score normalization: newer scanners score lower naturally ──
                    # structure_bounce has many confirmation sources (S/R, wick, volume,
                    # HTF, confluence) giving it 75-90 scores. Newer scanners have fewer
                    # sources giving 40-65. Add a base boost so they can compete fairly.
                    _score_boost = {
                        "rsi_divergence": 12,
                        "cvd_divergence": 12,
                        "vwap_mean_revert": 10,
                        "ema_momentum": 8,
                        "trend_continuation": 8,
                        "liquidity_sweep": 5,
                        "bos_choch": 5,
                    }
                    boost = _score_boost.get(setup_name, 0)
                    if boost > 0:
                        weighted += boost
                        confs.append(f"Score boost +{boost} (scanner normalization)")
                        tier = _tier_from_score(weighted)

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

        # Funnel diagnostic: summarize scanner results
        triggered = [sr for sr in scan_results if sr.setup_result is not None]
        not_triggered = [sr for sr in scan_results if sr.setup_result is None]
        if pass_cnt <= 5 or pass_cnt % 100 == 0:
            t_names = [f"{sr.scanner_name}({sr.weighted_score:.0f})" for sr in triggered]
            nt_names = [sr.scanner_name for sr in not_triggered]
            logger.info("FUNNEL %s | SCAN RESULT #%d | triggered=%s | no_trigger=%s",
                       symbol, pass_cnt, t_names or "NONE", nt_names)

        # --- Research Center: persist scanner-loop outcome for funnel analysis ---
        # Pure additive, rate-limited to 1/min per symbol, fail-silent.
        self._emit_funnel_sample(
            symbol=symbol,
            regime=regime,
            atr_ratio=float(getattr(self, "_atr_ratio", 0) or 0),
            allowed_scanners=allowed_scanners,
            scan_results=scan_results,
        )

        # ── Update setup lifecycle candidates for dashboard ──
        _lifecycle_candidates: List[Dict[str, Any]] = []
        for sr in scan_results:
            if sr.setup_result is not None:
                # Scanner triggered — CONFIRMED (passed scanner checks)
                _lifecycle_candidates.append({
                    "scanner": sr.scanner_name,
                    "state": "CONFIRMED",
                    "side": sr.side.value if sr.side else "unknown",
                    "price": round(sr.entry_price, 2) if sr.entry_price else round(float(df.iloc[-1].get("close", 0)), 2),
                    "score": round(sr.weighted_score, 1),
                    "tier": sr.tier,
                    "reason": ", ".join(sr.confirmations[:2]) if sr.confirmations else sr.scanner_name,
                    "updated": now_iso,
                })
            elif sr.tier == TIER_NEAR_MISS:
                # Near-miss — FORMING (pattern starting but not confirmed)
                _lifecycle_candidates.append({
                    "scanner": sr.scanner_name,
                    "state": "FORMING",
                    "side": sr.side.value if sr.side else "watch",
                    "price": round(float(df.iloc[-1].get("close", 0)), 2),
                    "score": round(sr.weighted_score, 1),
                    "tier": sr.tier,
                    "reason": sr.penalties[0][:60] if sr.penalties else "Approaching trigger",
                    "updated": now_iso,
                })
        # Sort by score desc and keep top 3
        _lifecycle_candidates.sort(key=lambda c: c["score"], reverse=True)
        self.setup_candidates[symbol] = _lifecycle_candidates[:3]

        # ── Log scanner co-firing for correlation analysis ──
        triggered_scanner_names = [sr.scanner_name for sr in scan_results if sr.setup_result is not None]
        if len(triggered_scanner_names) > 0:
            self._scanner_cofire_log.append({
                "ts": time.time(),
                "symbol": symbol,
                "scanners": triggered_scanner_names,
                "timeframe": self.primary_tf,
            })
            # Keep only last 500 entries
            if len(self._scanner_cofire_log) > 500:
                self._scanner_cofire_log = self._scanner_cofire_log[-500:]

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

        # ── Confluence bonus: boost when multiple scanners agree on same side ──
        # Scale: 2 scanners = +18, 3 scanners = +22, 4+ scanners = +25
        # structure_bounce is already dominant (82% WR) — cap its bonus at +8
        # Goal: lift dormant scanners (CVD, RSI, vwap_mean_revert) past their min_conf threshold
        if len(tradeable) >= 2:
            side_groups: Dict[str, List] = {}
            for sr in tradeable:
                side_val = sr.side.value if sr.side else "none"
                side_groups.setdefault(side_val, []).append(sr)

            for side_val, group in side_groups.items():
                if len(group) >= 2:
                    n = len(group)
                    # Tiered confluence bonus: more scanners = higher conviction
                    confluence_bonus = 18 if n == 2 else 22 if n == 3 else 25
                    scanner_names_list = [sr.scanner_name for sr in group]
                    for sr in group:
                        # structure_bounce doesn't need a big lift — it already dominates
                        # Give it a small acknowledgment so its score still reflects confluence
                        bonus_for_sr = min(8, confluence_bonus) if sr.scanner_name == "structure_bounce" else confluence_bonus
                        sr.weighted_score += bonus_for_sr
                        sr.confirmations.append(
                            f"Multi-scanner confluence ({'+'.join(scanner_names_list)}) +{bonus_for_sr}"
                        )
                    if pass_cnt <= 5 or pass_cnt % 50 == 0:
                        non_sb = [s for s in scanner_names_list if s != "structure_bounce"]
                        logger.info(
                            "FUNNEL %s | CONFLUENCE #%d | %d scanners agree on %s: %s "
                            "(non-SB +%d, SB +%d)",
                            symbol, pass_cnt, n, side_val, scanner_names_list,
                            confluence_bonus, min(8, confluence_bonus),
                        )

        # Pick best by weighted score
        best_sr = max(tradeable, key=lambda s: s.weighted_score)
        best = best_sr.setup_result

        if pass_cnt <= 5 or pass_cnt % 100 == 0:
            logger.info("FUNNEL %s | TRADEABLE #%d | scanner=%s side=%s score=%.0f tier=%s tradeable=%s",
                       symbol, pass_cnt, best_sr.scanner_name,
                       best.side.value if best.side else "?", best_sr.weighted_score,
                       best_sr.tier, self._weight_manager.is_tradeable(best_sr.scanner_name))

        # ── Block zero-confidence trades (should never happen) ──
        if best_sr.confidence <= 0 or best_sr.weighted_score <= 0:
            logger.warning("FUNNEL %s | ZERO CONF BLOCK | conf=%d score=%.0f — rejecting unscored signal",
                          symbol, best_sr.confidence, best_sr.weighted_score)
            return []

        # ── Block momentum_trend / investment strategy signals ──
        if best_sr.scanner_name in ("momentum_trend", "simple_bias", "investment"):
            logger.info("FUNNEL %s | INVESTMENT BLOCK | scanner=%s — not allowed in scalp",
                       symbol, best_sr.scanner_name)
            return []

        # ── Block empty regime (no regime = no trade) ──
        if not regime or regime.strip() == "":
            if pass_cnt <= 3 or pass_cnt % 50 == 0:
                logger.info("FUNNEL %s | EMPTY REGIME BLOCK | no regime detected", symbol)
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": "EMPTY REGIME: no regime detected",
                "indicators": {}, "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            return []

        # bos_choch: re-enabled with strict quality (0.8 ATR displacement, 1.5x volume, 5m primary)
        # Previously 40% WR on 1m noise → now requires strong displacement + volume on 5m

        # ── Exempt mean-reversion scanners from VWAP proximity penalty ──
        # These scanners WANT price near VWAP — penalizing them for being
        # in the noise/penalty zone is the opposite of what they need.
        # Uses stored prefilter context (vwap_zone) instead of string matching.
        _reversion_names = ("vwap_mean_revert", "rsi_divergence", "cvd_divergence")
        if best_sr.scanner_name in _reversion_names:
            _pf_result = getattr(self, '_prefilter_result', {})
            _vwap_zone = _pf_result.get('context', {}).get('vwap_zone', 'clear')
            if _vwap_zone in ('noise', 'penalty'):
                # Undo the exact penalty that was applied: -25 for noise, -20 for penalty
                _vwap_reversal = 25 if _vwap_zone == 'noise' else 20
                best_sr.weighted_score += _vwap_reversal
                best_sr.confirmations.append(
                    f"[VWAP penalty reversed +{_vwap_reversal} for mean-reversion ({_vwap_zone} zone)]"
                )

        # ── Setup strength veto: scanner-specific thresholds ──
        # Tuned from 6-month backtest + live WR data.  Each scanner's
        # threshold is the LOWEST score that still delivers edge.
        SCANNER_MIN_CONF = {
            "structure_bounce": 50,      # relaxed from 55 — proven workhorse, more entries
            "order_block_entry": 55,
            "bos_choch": 48,             # reactivated — displacement+vol quality gates
            "liquidity_sweep": 45,       # +494R 6mo, reclaim body+sweep depth gates
            "trend_continuation": 52,    # relaxed from 55, regime-adjusted in trends
            "ema_momentum": 50,          # relaxed from 52, regime-adjusted in trends
            "vwap_mean_revert": 47,      # relaxed from 48, exempt from VWAP prefilter
            "rsi_divergence": 50,        # +353R 6mo backtest, regime-adjusted in ranging
            "cvd_divergence": 50,        # +886R 6mo backtest, #1 scanner
        }
        has_confluence = any("confluence" in c.lower() for c in best_sr.confirmations)
        if has_confluence:
            MIN_SETUP_STRENGTH = 48  # confluence already validates quality
        else:
            MIN_SETUP_STRENGTH = SCANNER_MIN_CONF.get(best_sr.scanner_name, 55)

        # ── Regime-aware threshold adjustments ──
        # Specific scanners perform better in specific regimes — lower the bar
        # when the market context favors their setup type.
        _REGIME_ADJ = {
            "bos_choch":          {"trending_up": -5, "trending_down": -5, "breakout": -5},
            "liquidity_sweep":    {"ranging": -5, "quiet": -5},
            "trend_continuation": {"trending_up": -5, "trending_down": -5},
            "ema_momentum":       {"trending_up": -5, "trending_down": -5},
            "rsi_divergence":     {"ranging": -5, "quiet": -5},
            "cvd_divergence":     {"ranging": -5, "quiet": -5},
            "vwap_mean_revert":   {"ranging": -5, "quiet": -5},
        }
        _radj = _REGIME_ADJ.get(best_sr.scanner_name, {}).get(regime, 0)
        if _radj != 0 and not has_confluence:
            MIN_SETUP_STRENGTH = max(MIN_SETUP_STRENGTH + _radj, 40)  # floor 40
            best_sr.confirmations.append(
                f"[REGIME_ADJ: {_radj}, {regime} favors {best_sr.scanner_name}]"
            )

        # ── Fibonacci confluence bonus ──
        # If signal aligns with 0.618 or 0.786 Fib level, boost confidence
        _fib_at = indicators.get("fib_at_level", False)
        _fib_nearest = str(indicators.get("fib_nearest", ""))
        if _fib_at and _fib_nearest in ("0.618", "0.786", "0.5"):
            fib_bonus = 15 if _fib_nearest in ("0.618", "0.786") else 10
            best_sr.weighted_score += fib_bonus
            best_sr.confirmations.append(f"Fib confluence ({_fib_nearest}) +{fib_bonus}")

        # ── Universal volume gate ──
        # All scanners require minimum volume confirmation (rel_vol ≥ 1.2)
        _rel_vol_gate = float(last_row.get("rel_vol", 1.0)) if not np.isnan(float(last_row.get("rel_vol", 1.0))) else 1.0
        if _rel_vol_gate < 0.8 and best_sr.scanner_name not in ("structure_bounce",):
            # Very low volume — penalize new scanners (structure_bounce exempt as workhorse)
            best_sr.weighted_score -= 10
            best_sr.confirmations.append(f"LOW VOL PENALTY: rel_vol={_rel_vol_gate:.1f} < 0.8")

        # Regime adjustment: trending_down is our strongest regime — be more permissive
        if regime == "trending_down" and best_sr.scanner_name in ("liquidity_sweep", "cvd_divergence"):
            MIN_SETUP_STRENGTH = max(MIN_SETUP_STRENGTH - 5, 45)
        # trending_up LONG needs higher bar (64% WR vs 86% SHORT)
        side_val = best_sr.side.value if hasattr(best_sr.side, 'value') else str(best_sr.side)
        if regime == "trending_up" and side_val == "long":
            MIN_SETUP_STRENGTH = max(MIN_SETUP_STRENGTH + 5, 60)

        # ── Timeout prevention: require higher confidence for INTRADAY ──
        # Timeouts avg conf=70, winners avg conf=81. Raise bar for INTRADAY.
        trade_type_est = "INTRADAY"  # estimated — will be classified later
        if best_sr.weighted_score >= 80:
            trade_type_est = "RUNNER"
        elif best_sr.weighted_score < 60:
            trade_type_est = "SCALP"
        if trade_type_est == "INTRADAY" and best_sr.weighted_score < 75:
            # INTRADAY below 75 = high timeout risk
            best_sr.confirmations.append("[INTRADAY_CONF_PENALTY: -8]")
            best_sr.weighted_score -= 8

        # ── Block dead hours: 11:00 UTC (10 timeouts, worst hour) ──
        import time as _time
        utc_hour = _time.gmtime().tm_hour
        if utc_hour == 11:
            best_sr.confirmations.append("[DEAD_HOUR_PENALTY: -12, 11UTC]")
            best_sr.weighted_score -= 12
        if best_sr.weighted_score < MIN_SETUP_STRENGTH and not self._is_learning:
            self._funnel["weak_setup_veto"] = self._funnel.get("weak_setup_veto", 0) + 1
            if pass_cnt <= 5 or pass_cnt % 100 == 0:
                logger.info("FUNNEL %s | WEAK SETUP VETO #%d | score=%.0f < %d — skipping weak entry",
                           symbol, pass_cnt, best_sr.weighted_score, MIN_SETUP_STRENGTH)
            return []

        # ── Apply structural prefilter confidence adjustments ──
        _pf_adj = getattr(self, '_prefilter_result', {}).get('confidence_adj', 0)
        _pf_ctx = getattr(self, '_prefilter_result', {}).get('context', {})
        # Exempt mean-reversion scanners from VWAP confidence penalty (same logic as weighted_score reversal)
        _reversion_conf_names = ("vwap_mean_revert", "rsi_divergence", "cvd_divergence")
        if best.name in _reversion_conf_names:
            _vwap_z = _pf_ctx.get('vwap_zone', 'clear')
            if _vwap_z == 'noise':
                _pf_adj += 25  # undo the -25 noise penalty
            elif _vwap_z == 'penalty':
                _pf_adj += 20  # undo the -20 penalty
        # MTF alignment scoring: +5 if aligned with signal, -10 if opposed
        _mtf_dir = _pf_ctx.get('mtf_aligned', 0)
        if _mtf_dir != 0 and best.side is not None:
            _signal_long = best.side == OrderSide.LONG
            if (_signal_long and _mtf_dir > 0) or (not _signal_long and _mtf_dir < 0):
                _pf_adj += 5   # aligned with HTF
            elif (_signal_long and _mtf_dir < 0) or (not _signal_long and _mtf_dir > 0):
                _pf_adj += -10  # opposed to HTF

        if _pf_adj != 0 and not self._is_learning:
            _new_conf = max(best.confidence + _pf_adj, 30)
            _pf_notes = []
            if _pf_adj < 0:
                _pf_notes.append(f"[PREFILTER_PENALTY: {_pf_adj}]")
            else:
                _pf_notes.append(f"[PREFILTER_BONUS: +{_pf_adj}]")
            best = _SetupResult(
                name=best.name, side=best.side,
                confidence=_new_conf,
                confirmations=best.confirmations + _pf_notes,
                entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
            )

        # ══════════════════════════════════════════════════════
        # TIER 2: SCANNER VETO + WEIGHT GATE
        # Shadow scanners only log for ML, no actual trade
        # ══════════════════════════════════════════════════════
        # ══════════════════════════════════════════════════════
        # GATE 14b: STRUCTURE_BOUNCE_ONLY ENFORCEMENT
        # When active: ONLY structure_bounce (and order_block as secondary) trade
        # Everything else → ML log only, no actual trade
        # ══════════════════════════════════════════════════════
        scanner_size = self.scanner_size_tiers.get(best_sr.scanner_name, 0.6)

        # structure_bounce_only mode: DISABLED — now allowing bos_choch + liquidity_sweep
        # These scanners have been fixed and validated (17 triggers on Mar 24)
        if self.structure_bounce_only and not self._is_learning:
            if best_sr.scanner_name not in ("structure_bounce", "order_block_entry",
                                             "bos_choch", "liquidity_sweep"):
                scanner_size = 0.0  # shadow only ML-training scanners

            # Apply stricter thresholds for SB-only mode
            if best_sr.scanner_name == "structure_bounce":
                if best.confidence < self._sb_only_min_conf:
                    scanner_size = 0.0  # below SB-only min confidence

        # Shadow proven-negative scanners EVEN in learning mode
        # simple_bias: 25% WR, fires on any EMA alignment → pure noise
        if best_sr.scanner_name in ("simple_bias",):
            scanner_size = 0.0  # ML log only, no trade — even in learning

        # Shadow scanners: block from trading (log for ML only)
        # Block if: scanner_size=0 in non-learning mode
        _force_shadow = best_sr.scanner_name in ("simple_bias",)
        if scanner_size <= 0.0 and (not self._is_learning or _force_shadow):
            if pass_cnt <= 5 or pass_cnt % 100 == 0:
                logger.info("FUNNEL %s | SCANNER SHADOW #%d | scanner=%s conf=%d min_conf=%d size=%.1f",
                           symbol, pass_cnt, best_sr.scanner_name, best.confidence,
                           self._sb_only_min_conf, scanner_size)
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
        soft_vetos = []  # P0 fix: initialize early (used before line 2315)

        # VETO 1: Scanner cooldown (anti-duplicate) — ALWAYS enforced, even in learning mode
        # This prevents signal spam (same scanner+symbol every minute)
        cooldown_key = f"{best_sr.scanner_name}_{symbol}"
        last_fire = self._scanner_cooldowns.get(cooldown_key, 0)
        if now - last_fire < self._scanner_cooldown_sec:
            elapsed = int(now - last_fire)
            if pass_cnt <= 5 or pass_cnt % 100 == 0:
                logger.info("FUNNEL %s | COOLDOWN BLOCK #%d | scanner=%s elapsed=%ds need=%ds",
                           symbol, pass_cnt, best_sr.scanner_name, elapsed, self._scanner_cooldown_sec)
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"COOLDOWN: {best_sr.scanner_name} fired {elapsed}s ago (need {self._scanner_cooldown_sec}s)",
                "indicators": indicators, "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            return []  # Hard block — no signal spam

        # VETO 1b: Side-conflict cooldown — prevent LONG→SHORT→LONG flip within 3 min
        # Also ALWAYS enforced, prevents whipsaw noise
        side_key = f"{symbol}_{best.side.value}"
        opposite_side = "short" if best.side == OrderSide.LONG else "long"
        opposite_key = f"{symbol}_{opposite_side}"
        last_opposite = self._scanner_cooldowns.get(f"_side_{opposite_key}", 0)
        side_cooldown_sec = 180  # 3 minutes
        if now - last_opposite < side_cooldown_sec and last_opposite > 0:
            elapsed = int(now - last_opposite)
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"SIDE CONFLICT: {opposite_side.upper()} signal {elapsed}s ago, blocking {best.side.value.upper()} flip",
                "indicators": indicators, "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            return []  # Hard block — no whipsaw

        # VETO 2: HTF STRICT alignment (Tier 2 upgrade — HARD veto, not soft)
        # Signal side MUST match HTF bias (15m EMA50)
        if htf_bias != 0:
            htf_opposes = (
                (htf_bias < 0 and best.side == OrderSide.LONG) or
                (htf_bias > 0 and best.side == OrderSide.SHORT)
            )
            if htf_opposes:
                vetos.append(f"HTF STRICT: HTF={'bearish' if htf_bias < 0 else 'bullish'} vs {best.side.value}")

        # VETO 2b: 5m Momentum Gate (prevents timeout entries)
        # If 5m EMA8 slope disagrees with entry direction → soft penalty
        # If BOTH 5m and 15m disagree → hard block (trade will timeout)
        if df_5m is not None and len(df_5m) >= 10 and "ema_8" in df_5m.columns:
            ema8_5m = df_5m["ema_8"].dropna()
            if len(ema8_5m) >= 5:
                _best_atr = getattr(best, "atr", getattr(best_sr, "atr", 1.0)) if best else 1.0
                slope_5m = (float(ema8_5m.iloc[-1]) - float(ema8_5m.iloc[-5])) / max(_best_atr, 0.001)
                side_val = best.side.value if hasattr(best.side, "value") else str(best.side)
                mtf_agrees = (slope_5m > 0.1 and side_val == "long") or (slope_5m < -0.1 and side_val == "short")
                mtf_disagrees = (slope_5m < -0.2 and side_val == "long") or (slope_5m > 0.2 and side_val == "short")

                if mtf_disagrees:
                    if htf_bias != 0 and ((htf_bias < 0 and side_val == "long") or (htf_bias > 0 and side_val == "short")):
                        vetos.append("MTF BLOCK: 5m(%.2f) + 15m both against %s" % (slope_5m, side_val))
                    else:
                        soft_vetos.append("5m MOMENTUM: slope=%.2f against %s (-15)" % (slope_5m, side_val))
                        _cur_conf = getattr(best, "confidence", getattr(best, "raw_score", 50))
                        _cur_confs = getattr(best, "confirmations", [])
                        best_sr = best_sr._replace(weighted_score=max(best_sr.weighted_score - 15, 20)) if hasattr(best_sr, "_replace") else best_sr
                        if hasattr(best_sr, "weighted_score"):
                            best_sr.weighted_score = max(best_sr.weighted_score - 15, 20)
                            best_sr.confirmations = list(best_sr.confirmations) + ["[5M_PENALTY: -15, slope=%.2f]" % slope_5m]
                elif mtf_agrees:
                    if hasattr(best_sr, "weighted_score"):
                        best_sr.weighted_score = min(best_sr.weighted_score + 10, 100)
                        best_sr.confirmations = list(best_sr.confirmations) + ["[5M_BOOST: +10, slope=%.2f]" % slope_5m]

        # VETO 2c: 15m Structure Confirmation for INTRADAY/RUNNER trades
        # Higher timeframe must show structure agreement for larger trade types
        trade_type = getattr(best_sr, '_trade_type', '') or ''
        if not trade_type:
            # Estimate trade type from confidence
            _tt_conf = best.confidence if best else 50
            trade_type = "RUNNER" if _tt_conf >= 85 else "INTRADAY" if _tt_conf >= 60 else "SCALP"
        if trade_type in ("INTRADAY", "RUNNER") and htf_bias != 0:
            # For longer trades, also check 15m EMA21 slope for structure confirmation
            htf_df = candles_dict.get("15m") if candles_dict else None
            if htf_df is not None and len(htf_df) >= 10 and "ema_21" in htf_df.columns:
                ema21_15m = htf_df["ema_21"].dropna()
                if len(ema21_15m) >= 5:
                    _htf_atr = getattr(best, "atr", 1.0)
                    slope_15m = (float(ema21_15m.iloc[-1]) - float(ema21_15m.iloc[-5])) / max(_htf_atr, 0.001)
                    side_val = best.side.value if hasattr(best.side, "value") else str(best.side)
                    _15m_opposes = (slope_15m < -0.15 and side_val == "long") or (slope_15m > 0.15 and side_val == "short")
                    if _15m_opposes:
                        soft_vetos.append("15m STRUCTURE: EMA21 slope=%.2f against %s (-12)" % (slope_15m, side_val))
                        if hasattr(best_sr, "weighted_score"):
                            best_sr.weighted_score = max(best_sr.weighted_score - 12, 20)
                            best_sr.confirmations = list(best_sr.confirmations) + ["[15M_STRUCTURE_PENALTY: -12]"]

        # VETO 2d: MTF HARD REQUIREMENT for INTRADAY / RUNNER
        # Scalp trades can fire against macro — they close in <5 min anyway.
        # INTRADAY (target: 1-4h hold) and RUNNER (4h+) need the macro behind them.
        # Hard block:  1h macro DIRECTLY opposes signal side → trade will grind against trend
        # Hard block:  15m structure (confirm_bias) DIRECTLY opposes signal side
        # Soft (-10):  1h macro is neutral (no tailwind) → penalise but don't fully block
        if trade_type in ("INTRADAY", "RUNNER"):
            _side_str = best.side.value if hasattr(best.side, "value") else str(best.side)
            _sig_long = _side_str == "long"

            # --- 1H macro alignment ---
            _1h_opposes = (_macro_bias < 0 and _sig_long) or (_macro_bias > 0 and not _sig_long)
            _1h_neutral  = _macro_bias == 0

            if _1h_opposes:
                # 1h trend is actively running against the trade — hard veto
                _mb_label = "bearish" if _macro_bias < 0 else "bullish"
                vetos.append(
                    f"MTF {trade_type}: 1h macro={_mb_label} directly opposes {_side_str}"
                )
            elif _1h_neutral and df_1h is not None:
                # 1h ranging/neutral = no macro tailwind for a longer hold
                if hasattr(best_sr, "weighted_score"):
                    best_sr.weighted_score = max(best_sr.weighted_score - 10, 20)
                    best_sr.confirmations = list(best_sr.confirmations) + [
                        f"[1H_NEUTRAL_PENALTY: -10, {trade_type} needs macro tailwind]"
                    ]

            # --- 15m structure alignment (confirm_bias = 15m EMA50) ---
            if confirm_bias != 0:
                _15m_opposes = (confirm_bias < 0 and _sig_long) or (confirm_bias > 0 and not _sig_long)
                if _15m_opposes:
                    _cb_label = "bearish" if confirm_bias < 0 else "bullish"
                    vetos.append(
                        f"MTF {trade_type}: 15m structure={_cb_label} opposes {_side_str}"
                    )

        # VETO 3: Session (Indian Market aware)
        # During Indian flow hours: skip dead-hour blocking (Indian flow overrides)
        # Otherwise: 2-5 UTC hard block, 10-11 UTC soft penalty
        _v3_indian_ctx = getattr(self, '_indian_ctx', None)
        if getattr(self, '_session_gate_enabled', True):
            if _v3_indian_ctx and _v3_indian_ctx.is_indian_flow_hour:
                # Indian flow hour — confidence boost already applied via _session_penalty
                # Do NOT apply dead-hour blocking during active Indian market flow
                pass
            else:
                ist_now_check = datetime.now(_IST)
                utc_hour = (ist_now_check.hour - 5) % 24
                dead_hours_hard = {2, 3, 4, 5}    # genuinely dead — hard block
                dead_hours_soft = {10, 11}         # soft penalty only
                if utc_hour in dead_hours_hard:
                    vetos.append(f"DEAD SESSION: UTC hour {utc_hour} (2-5 UTC low liquidity)")
                elif utc_hour in dead_hours_soft and not self._is_learning:
                    _session_penalty = -10
                    best = _SetupResult(
                        name=best.name, side=best.side,
                        confidence=max(best.confidence + _session_penalty, 30),
                        confirmations=best.confirmations + [f"[SESSION_PENALTY: {_session_penalty}, UTC {utc_hour}]"],
                        entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
                    )

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

        # VETO 7: Candle Quality SCORING (not hard filter)
        # Score 0-4: body_ratio, close_in_direction, displacement, volume
        # < 2 = BLOCK, == 2 = PENALTY (-15), >= 3 = pass
        trigger_candle = df.iloc[-1]
        _cq_close = float(trigger_candle.get("close", 0))
        _cq_open = float(trigger_candle.get("open", 0))
        _cq_high = float(trigger_candle.get("high", 0))
        _cq_low = float(trigger_candle.get("low", 0))
        candle_body = abs(_cq_close - _cq_open)
        candle_range = _cq_high - _cq_low
        _cq_atr = self._confirm_atr if self._confirm_atr > 0 else best.atr

        candle_score = 0
        candle_score_details = []

        # (1) Body ratio > 0.4
        if candle_range > 0:
            body_ratio = candle_body / candle_range
            if body_ratio > 0.4:
                candle_score += 1
                candle_score_details.append(f"body={body_ratio:.2f}>0.4")
            else:
                candle_score_details.append(f"body={body_ratio:.2f}<0.4")

        # (2) Close in direction (long: upper 60%, short: lower 40%)
        if candle_range > 0 and best.side is not None:
            close_position = (_cq_close - _cq_low) / candle_range
            if best.side == OrderSide.LONG and close_position > 0.4:
                candle_score += 1
                candle_score_details.append(f"close_pos={close_position:.2f}>0.4")
            elif best.side == OrderSide.SHORT and close_position < 0.6:
                candle_score += 1
                candle_score_details.append(f"close_pos={close_position:.2f}<0.6")
            else:
                candle_score_details.append(f"close_pos={close_position:.2f} wrong dir")

        # (3) Displacement > 0.3× ATR
        if _cq_atr > 0:
            displacement = candle_body / _cq_atr
            if displacement > 0.3:
                candle_score += 1
                candle_score_details.append(f"disp={displacement:.2f}>0.3")
            else:
                candle_score_details.append(f"disp={displacement:.2f}<0.3")

        # (4) Volume percentile > 50th
        _cq_vol = float(trigger_candle.get("rel_vol", 1.0)) if not np.isnan(trigger_candle.get("rel_vol", 1.0)) else 0
        if _cq_vol > 1.0:
            candle_score += 1
            candle_score_details.append(f"vol={_cq_vol:.1f}x>1.0")
        else:
            candle_score_details.append(f"vol={_cq_vol:.1f}x<1.0")

        _cq_summary = f"candle_score={candle_score}/4 ({', '.join(candle_score_details)})"
        if candle_score < 2:
            vetos.append(f"WEAK CANDLE: {_cq_summary}")
        elif candle_score == 2 and not self._is_learning:
            # Marginal candle — penalty not block
            _candle_penalty = -15
            best = _SetupResult(
                name=best.name, side=best.side,
                confidence=max(best.confidence + _candle_penalty, 30),
                confirmations=best.confirmations + [f"[CANDLE_QUALITY_PENALTY: {_candle_penalty}, {_cq_summary}]"],
                entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
            )

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

        # VETO 9: Regime + Scanner mismatch
        # REMOVED the hard whitelist — the REGIME_SCANNER_ROUTING already controls
        # which scanners run per regime. If a scanner triggered, it was allowed to run.
        # Only block quiet regime (absolute no-trade rule).
        # Exception: during Indian flow hours, ranging_limited override already
        # selected which scanners are allowed — don't re-block them here.
        regime_scanner_ok = True
        _v9_indian_ctx = getattr(self, '_indian_ctx', None)
        _v9_indian_override = _v9_indian_ctx and _v9_indian_ctx.regime_override == "ranging_limited"
        if regime in ("quiet",) and not _v9_indian_override:
            if best_sr.scanner_name != "structure_bounce":
                regime_scanner_ok = False
                vetos.append(f"REGIME MISMATCH: {best_sr.scanner_name} blocked in quiet market")

        # VETO 10: Regime-Side conflict — block counter-trend for momentum scanners
        # Exception: mean-reversion scanners (rsi_divergence, cvd_divergence, vwap_mean_revert)
        # are DESIGNED to trade counter-trend — don't block them
        _reversion_scanners = ("rsi_divergence", "cvd_divergence", "vwap_mean_revert")
        if best_sr.scanner_name not in _reversion_scanners:
            if regime in ("trending_up",) and best.side == OrderSide.SHORT:
                vetos.append(f"REGIME SIDE: SHORT blocked in {regime} — counter-trend")
            elif regime in ("trending_down",) and best.side == OrderSide.LONG:
                vetos.append(f"REGIME SIDE: LONG blocked in {regime} — counter-trend")
        else:
            # Reversion scanners: soft penalty instead of hard veto
            if regime in ("trending_up",) and best.side == OrderSide.SHORT:
                soft_vetos.append(f"COUNTER-TREND REVERSION: {best_sr.scanner_name} SHORT in {regime} (-10)")
            elif regime in ("trending_down",) and best.side == OrderSide.LONG:
                soft_vetos.append(f"COUNTER-TREND REVERSION: {best_sr.scanner_name} LONG in {regime} (-10)")

        # VETO 10b: 1H MACRO TREND — block trades fighting hourly trend
        # This is the highest-timeframe filter. If 1h is strongly bearish,
        # don't go LONG even if 1m/5m show a bounce (it's counter-macro).
        # Exception: reversion scanners get soft penalty instead
        _macro = indicators.get("macro_bias", 0)
        if _macro != 0 and best_sr.scanner_name not in _reversion_scanners:
            if _macro < 0 and best.side == OrderSide.LONG:
                soft_vetos.append(f"1H MACRO BEARISH: LONG against hourly trend (-15)")
            elif _macro > 0 and best.side == OrderSide.SHORT:
                soft_vetos.append(f"1H MACRO BULLISH: SHORT against hourly trend (-15)")

        # --- SESSION BIAS VETO (Upgrade 4) ---
        _sb = indicators.get("session_bias", 0)
        if _sb != 0:
            _sb_opposes = (_sb < 0 and best.side == OrderSide.LONG) or (_sb > 0 and best.side == OrderSide.SHORT)
            if _sb_opposes and best.confidence < 85:
                vetos.append(f"SESSION VETO: 4h={'bearish' if _sb<0 else 'bullish'} vs {best.side.value} (conf={best.confidence}<85)")
            elif not _sb_opposes:
                # Session-aligned boost
                best = _SetupResult(
                    name=best.name, side=best.side,
                    confidence=min(best.confidence + 5, 100),
                    confirmations=best.confirmations + [f"[SESSION_BOOST: +5, 4h={'bull' if _sb>0 else 'bear'}]"],
                    entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
                )

        # VETO 11: VWAP Direction Filter — SOFTENED to confidence penalty (-15)
        # Was: hard block. Now: -15 confidence penalty (lets good setups through)
        # LONG: price > VWAP AND trend_strength > threshold
        # SHORT: price < VWAP AND trend_strength < -threshold
        last_close = float(df.iloc[-1].get("close", 0))
        last_vwap = float(df.iloc[-1].get("vwap", 0))
        _ema8_v = float(df.iloc[-1].get("ema_8", 0))
        _ema21_v = float(df.iloc[-1].get("ema_21", 0))
        _atr_v = self._confirm_atr if self._confirm_atr > 0 else best.atr
        _trend_str = (_ema8_v - _ema21_v) / _atr_v if _atr_v > 0 else 0.0
        _vwap_threshold = 0.3  # ATR-normalized trend strength threshold

        _vwap_penalty = 0
        if last_vwap > 0:
            if best.side == OrderSide.LONG:
                if last_close < last_vwap and _trend_str < _vwap_threshold:
                    _vwap_penalty = -15
            elif best.side == OrderSide.SHORT:
                if last_close > last_vwap and _trend_str > -_vwap_threshold:
                    _vwap_penalty = -15
        if _vwap_penalty != 0 and not self._is_learning:
            best = _SetupResult(
                name=best.name, side=best.side,
                confidence=max(best.confidence + _vwap_penalty, 30),
                confirmations=best.confirmations + [f"[VWAP_DIR_PENALTY: {_vwap_penalty}]"],
                entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
            )

        # VETO 12: ATR Quiet Market — no trade if market is too quiet
        if atr_ratio < 0.5:
            vetos.append(f"ATR DEAD: ratio {atr_ratio:.2f} < 0.50 — market too quiet for any setup")

        # VETO 13: ATR Chaotic — SOFTENED to confidence penalty (-10)
        # Was: hard block. Now: -10 confidence penalty (lets strong setups through)
        if atr_ratio > 1.5:
            # Check body ratio average — if candles are mostly wicks, it's choppy
            _recent_bodies = df.iloc[-5:].apply(
                lambda r: abs(r.get("close", 0) - r.get("open", 0)) /
                          max(r.get("high", 0) - r.get("low", 0), 1e-8), axis=1
            )
            _avg_body = float(_recent_bodies.mean())
            if _avg_body < 0.35 and not self._is_learning:
                _chaotic_penalty = -10
                best = _SetupResult(
                    name=best.name, side=best.side,
                    confidence=max(best.confidence + _chaotic_penalty, 30),
                    confirmations=best.confirmations + [f"[ATR_CHAOTIC_PENALTY: {_chaotic_penalty}]"],
                    entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
                )

        # Separate hard vs soft vetos for structure_bounce
        # structure_bounce is our best scanner (73% WR, +35% PnL) — don't kill it easily.
        # Only truly dangerous vetos stay hard. Others become confidence penalties.
        is_sb = best_sr.scanner_name == "structure_bounce"

        # For structure_bounce: only HTF, CHOCH, and REGIME MISMATCH are hard vetos
        # Everything else (ATR, Volume, No-Chase, Candle quality, Cooldown, Session) → soft penalty
        sb_hard_prefixes = ("CHOCH CONFLICT:", "REGIME MISMATCH:", "REGIME SIDE:")
        # HTF STRICT moved to soft — in choppy markets 1H often disagrees with 5m entries
        sb_soft_prefixes = ("HTF STRICT:", "LOW VOLATILITY:", "NO VOLUME:", "NO CHASE:", "WEAK CANDLE:",
                            "COOLDOWN:")

        # ── P0 HOTFIX (2026-04-10): Bear-HTF low-conf long veto ──
        # Data-driven: 4 consecutive losses on 2026-04-10 were all LONG structure_bounce
        # trades against bearish HTF (htf_bias=-1) with raw confidence 38-53 (<70).
        # Root cause: HTF STRICT was globally soft for structure_bounce, letting
        # counter-HTF low-conf longs fire. Paper WR dropped to 50%, real WR to 20%.
        #
        # Surgical fix: HTF STRICT escalates to HARD veto when ALL of:
        #   1. Scanner == structure_bounce (SB-specific problem)
        #   2. htf_bias != 0 (actual bias exists)
        #   3. pre-adjustment confidence < 70 (high-conf setups still fire)
        #
        # Feature flag allows toggling off without code change.
        # Zero impact on: neutral-HTF trades, high-conf (≥70) trades, non-SB scanners.
        if not hasattr(self, '_htf_strict_hard_for_lowconf_sb'):
            self._htf_strict_hard_for_lowconf_sb = True  # default ON
        _sb_hard_lowconf = (
            self._htf_strict_hard_for_lowconf_sb
            and is_sb
            and htf_bias != 0
            and getattr(best, 'confidence', 100) < 70
        )

        # ── P0.8 EXPANSION (2026-04-10 PM): Counter-HTF veto for ALL momentum scanners ──
        # Loss taxonomy data showed 23 counter-HTF losses across BOTH long+short, NOT just SB.
        # bos_choch, liquidity_sweep, order_block_entry, momentum scanners all leak counter-HTF.
        #
        # Stricter rule for non-reversion scanners: HTF STRICT becomes HARD when:
        #   1. Scanner is a momentum/breakout scanner (not reversion)
        #   2. htf_bias clearly opposes side (not just != 0)
        #   3. pre-adjustment confidence < 75 (slightly higher threshold than P0)
        #
        # Reversion scanners (rsi_divergence, cvd_divergence, vwap_mean_revert) are EXEMPT
        # because they're designed to trade counter-trend.
        if not hasattr(self, '_htf_strict_hard_for_momentum'):
            self._htf_strict_hard_for_momentum = True  # default ON
        _reversion_scanners_p08 = ("rsi_divergence", "cvd_divergence", "vwap_mean_revert")
        _is_reversion = best_sr.scanner_name in _reversion_scanners_p08
        _is_momentum_scanner = (
            not _is_reversion
            and not is_sb  # SB has its own P0 rule
            and best_sr.scanner_name not in ("structure_bounce",)
        )
        _momentum_hard_counter = (
            self._htf_strict_hard_for_momentum
            and _is_momentum_scanner
            and htf_bias != 0
            and getattr(best, 'confidence', 100) < 75
        )

        # ── P3.11 CHOP REGIME LONG GATE ──
        # Data: 5 of 5 formal real trades today were LONG SCALP structure_bounce
        # in high_volatility/mean_reversion regimes, all failed (mfe_3min_dead,
        # early_kill, sl_hit). Paper historical showed 97% of losses in chop.
        # Today's formal real trades had 0% WR in chop longs.
        #
        # Rule: block LONG SCALPs in chop regimes when BOTH:
        #   1. regime in (high_volatility, mean_reversion, sideways) — chop
        #   2. ml_probability < 0.55 (ML doesn't strongly believe)
        #   3. htf_bias <= 0 (no bullish tailwind)
        #
        # ML-bless escape: if ml_prob >= 0.55 AND htf_bias > 0, trade passes.
        # A+ grade escape: grade=A+ overrides everything (high conviction preserved).
        if not hasattr(self, '_p3_11_chop_long_gate'):
            self._p3_11_chop_long_gate = True  # default ON
        _chop_regimes_p311 = ("high_volatility", "mean_reversion", "sideways")
        _side_str_p311 = best.side.value if best.side else ""
        _regime_lower_p311 = str(regime).lower() if regime else ""
        _ml_prob_p311 = float(getattr(best, 'ml_probability', 0) or indicators.get('ml_probability', 0) or 0)
        _grade_p311 = getattr(best, 'grade', '') or ''
        _chop_long_trap = (
            self._p3_11_chop_long_gate
            and _side_str_p311 == "long"
            and _regime_lower_p311 in _chop_regimes_p311
            and htf_bias <= 0  # no bullish HTF support
            and _grade_p311 != "A+"  # A+ override
            and _ml_prob_p311 < 0.55  # ML not strongly bullish
        )

        # ── P3.7 SIDEWAYS SCANNER-SPECIFIC GATE (DATA-DRIVEN 2026-04-10) ──
        # Source: Phase 3.18 loss taxonomy — 47 of 68 losses (70% of $ loss)
        # came from structure_bounce:sideways:long specifically.
        #
        # P3.11 only blocks chop longs when htf_bias <= 0. This misses sideways
        # trades where HTF is neutral/bullish but price action is still chop.
        # The data shows THOSE trades are equally deadly.
        #
        # Rule: block structure_bounce LONG in SIDEWAYS regime when:
        #   1. is_sb (structure_bounce only)
        #   2. regime exactly == sideways (not high_volatility, not ranging)
        #   3. side = long
        #   4. confidence < 75 (high-conf preserved)
        #   5. ml_prob < 0.60 (ML escape hatch)
        #   6. grade != A+ (A+ override preserved)
        #
        # This is SURGICAL: doesn't touch short trades, doesn't touch non-SB,
        # doesn't touch high-vol/mean-rev (those are covered by P3.11 when
        # htf aligned against). ONLY targets the exact 47-loss combo.
        if not hasattr(self, '_p3_7_sideways_sb_long_gate'):
            self._p3_7_sideways_sb_long_gate = True  # default ON
        _conf_p37 = getattr(best, 'confidence', 100)
        _p37_sideways_trap = (
            self._p3_7_sideways_sb_long_gate
            and is_sb
            and _regime_lower_p311 == "sideways"
            and _side_str_p311 == "long"
            and _conf_p37 < 75
            and _ml_prob_p311 < 0.60
            and _grade_p311 != "A+"
        )

        hard_vetos = []
        soft_vetos = []
        conf_penalty = 0

        # ── P3.11: fire chop-long gate BEFORE veto loop (synthetic hard veto) ──
        if _chop_long_trap:
            hard_vetos.append(
                f"P3.11 CHOP LONG TRAP: regime={_regime_lower_p311} "
                f"htf={htf_bias} ml={_ml_prob_p311:.2f} grade={_grade_p311} [P3_11_CHOP_LONG_HARD]"
            )
            try:
                from bot import pipeline_metrics as _pm
                _pm.record_hotfix_veto(
                    "p3_11_chop_long_block",
                    f"{symbol}_{_regime_lower_p311}_ml{_ml_prob_p311:.2f}_htf{htf_bias}"
                )
            except Exception:
                pass

        # ── P3.7: fire sideways scanner-specific gate ──
        if _p37_sideways_trap:
            hard_vetos.append(
                f"P3.7 SIDEWAYS SB LONG: regime=sideways "
                f"conf={_conf_p37} ml={_ml_prob_p311:.2f} grade={_grade_p311} [P3_7_SIDEWAYS_SB_HARD]"
            )
            try:
                from bot import pipeline_metrics as _pm
                _pm.record_hotfix_veto(
                    "p3_7_sideways_sb_long",
                    f"{symbol}_conf{_conf_p37}_ml{_ml_prob_p311:.2f}_grade{_grade_p311}"
                )
            except Exception:
                pass

        for v in vetos:
            # P0: HTF STRICT → HARD for SB low-conf counter-HTF
            if _sb_hard_lowconf and v.startswith("HTF STRICT:"):
                hard_vetos.append(v + " [P0_LOWCONF_HARD]")
                # Phase 3.2: count P0 effectiveness
                try:
                    from bot import pipeline_metrics as _pm
                    _sym_p0 = symbol
                    _conf_p0 = getattr(best, 'confidence', 0)
                    _pm.record_hotfix_veto("p0_lowconf_bear_htf", f"{_sym_p0}_conf{_conf_p0}")
                except Exception:
                    pass
                continue
            # P0.8: HTF STRICT → HARD for momentum scanners (non-SB, non-reversion)
            if _momentum_hard_counter and v.startswith("HTF STRICT:"):
                hard_vetos.append(v + " [P0_8_MOMENTUM_HARD]")
                try:
                    from bot import pipeline_metrics as _pm
                    _conf_p08 = getattr(best, 'confidence', 0)
                    _pm.record_hotfix_veto("p0_8_momentum_counter_htf",
                                           f"{symbol}_{best_sr.scanner_name}_conf{_conf_p08}")
                except Exception:
                    pass
                continue
            if is_sb and any(v.startswith(p) for p in sb_soft_prefixes):
                soft_vetos.append(v)
                conf_penalty += 8  # -8 confidence per soft veto
            elif is_sb and not any(v.startswith(p) for p in sb_hard_prefixes):
                # Unknown veto type for SB → soft
                soft_vetos.append(v)
                conf_penalty += 8
            else:
                hard_vetos.append(v)

        # Track veto stats for debugging (exposed to dashboard)
        # Reset every 15 minutes so dashboard shows CURRENT blocks, not lifetime cumulative
        import time as _time
        if not hasattr(self, '_veto_stats'):
            self._veto_stats = {}
            self._veto_stats_reset_at = _time.time()
        if _time.time() - self._veto_stats_reset_at > 900:  # 15 min
            self._veto_stats = {}
            self._veto_stats_reset_at = _time.time()
        for v in vetos:
            veto_type = v.split(":")[0].strip()
            self._veto_stats[veto_type] = self._veto_stats.get(veto_type, 0) + 1

        # Apply hard vetos — ALWAYS enforced, even in learning mode
        # These exist for a reason: HTF mismatch, dead session, regime conflict, CHOCH conflict
        # Letting garbage through in learning mode corrupts the training data
        if hard_vetos:
            if pass_cnt <= 5 or pass_cnt % 100 == 0:
                logger.info("FUNNEL %s | HARD VETO #%d | scanner=%s | vetos=%s | soft=%s",
                           symbol, pass_cnt, best_sr.scanner_name, hard_vetos, soft_vetos)
            self._funnel["blocked_regime"] = self._funnel.get("blocked_regime", 0) + 1
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"VETO: {hard_vetos[0]}",
                "all_vetos": hard_vetos,
                "soft_vetos": soft_vetos,
                "indicators": indicators, "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            return []

        # Apply soft veto confidence penalty (always — learning mode included)
        if soft_vetos:
            best = _SetupResult(
                name=best.name, side=best.side,
                confidence=max(best.confidence - conf_penalty, 40),
                confirmations=best.confirmations + [f"[SOFT_PENALTY: -{conf_penalty} from {len(soft_vetos)} vetos]"],
                entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
            )

        # ── Apply confidence modifiers ──

        # ── Regime Age modifier ──
        # Fresh regime transitions are unreliable, mature regimes are trustworthy
        if not self._is_learning:
            if regime_age < 3:
                _ra_adj = -10
                best = _SetupResult(
                    name=best.name, side=best.side,
                    confidence=max(best.confidence + _ra_adj, 30),
                    confirmations=best.confirmations + [f"[REGIME_AGE_PENALTY: {_ra_adj}, age={regime_age}]"],
                    entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
                )
            elif regime_age > 30:
                _ra_adj = +8
                best = _SetupResult(
                    name=best.name, side=best.side,
                    confidence=min(best.confidence + _ra_adj, 100),
                    confirmations=best.confirmations + [f"[REGIME_AGE_BOOST: +{_ra_adj}, age={regime_age}]"],
                    entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
                )
            elif regime_age > 10:
                _ra_adj = +3
                best = _SetupResult(
                    name=best.name, side=best.side,
                    confidence=min(best.confidence + _ra_adj, 100),
                    confirmations=best.confirmations + [f"[REGIME_AGE_BOOST: +{_ra_adj}, age={regime_age}]"],
                    entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
                )

        # ── P2 FIX: Long-side confidence penalty ──
        # Data shows: Long WR=73.9% vs Short WR=83.1% (+9.2% gap)
        # Long PnL=+18.95% vs Short PnL=+38.85% (shorts 2x more profitable)
        # Apply -8 penalty to longs in non-trending regimes
        if not self._is_learning:
            side_str = "long" if best.side == OrderSide.LONG else "short"
            is_long = side_str == "long"
            regime_str = str(regime).lower()
            # Penalty for longs in quiet/ranging/unknown regimes
            if is_long and regime_str not in ("trending_up", "breakout"):
                _long_adj = -8
                best = _SetupResult(
                    name=best.name, side=best.side,
                    confidence=max(best.confidence + _long_adj, 30),
                    confirmations=best.confirmations + [f"[LONG_PENALTY: {_long_adj}, regime={regime_str}]"],
                    entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
                )
            # Bonus for shorts in trending_down (proven edge)
            elif not is_long and regime_str == "trending_down":
                _short_adj = +5
                best = _SetupResult(
                    name=best.name, side=best.side,
                    confidence=min(best.confidence + _short_adj, 100),
                    confirmations=best.confirmations + [f"[SHORT_TREND_BOOST: +{_short_adj}]"],
                    entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
                )

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

        # HARD EV VETO — reject negative EV trades (ALWAYS enforced)
        if ev_result.verdict == "REJECT":
            if pass_cnt <= 5 or pass_cnt % 100 == 0:
                logger.info("FUNNEL %s | EV REJECT #%d | scanner=%s ev=%s reason=%s",
                           symbol, pass_cnt, best_sr.scanner_name, ev_result.verdict, ev_result.reason)
            self._funnel["blocked_ev"] = self._funnel.get("blocked_ev", 0) + 1
            self.last_scan_status[symbol] = {
                "time": now_iso, "signal": False,
                "reason": f"EV REJECT: {ev_result.reason}",
                "indicators": indicators, "setups_checked": setups_checked,
                "funnel": dict(self._funnel),
            }
            return []

        confidence_size_mult = calc_confidence_size_multiplier(best.confidence, best_sr.tier)

        # ── Adaptive sizing: confluence bonus ──
        if has_confluence:
            confidence_size_mult *= 1.25  # 25% larger position on multi-scanner agreement
            logger.info("CONFLUENCE SIZE BOOST: %s %s | mult=%.2f → %.2f (confluence)",
                       symbol, best_sr.scanner_name, confidence_size_mult / 1.25, confidence_size_mult)

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

            if conservative_move < min_edge and not self._is_learning and not is_sb:
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
                if not self._is_learning and not is_sb:
                    self.last_scan_status[symbol] = {
                        "time": now_iso, "signal": False,
                        "reason": f"MOMENTUM GATE: flat (range={recent_range:.2f} < 0.42×ATR={atr_check*0.42:.2f})",
                        "indicators": indicators, "setups_checked": setups_checked,
                        "funnel": dict(self._funnel),
                    }
                    return []
                elif is_sb:
                    # Structure bounce at quiet S/R can still work — just penalize
                    best = _SetupResult(
                        name=best.name, side=best.side,
                        confidence=max(best.confidence - 10, 40),
                        confirmations=best.confirmations + ["[FLAT_MKT_PENALTY: -10]"],
                        entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
                    )

        # ══════════════════════════════════════════════════════
        # TIER 1: SPREAD CHECK (Liquidity Gate)
        # BTC/ETH: spread must be < 0.018%
        # AVAX/others: spread must be < 0.045%
        # Uses high-low of current candle as spread proxy
        # ══════════════════════════════════════════════════════
        if best.entry_price > 0 and candle_range > 0:
            spread_pct = (candle_range / best.entry_price) * 100
            max_spread = 0.045 if "AVAX" in symbol else 0.018
            # Use minimum of candle range and ATR as spread estimate
            # Real spread check needs L2 data — this is a proxy
            if spread_pct < 0.001:  # suspiciously tight — likely stale data
                if not self._is_learning:
                    self.last_scan_status[symbol] = {
                        "time": now_iso, "signal": False,
                        "reason": f"SPREAD GATE: spread {spread_pct:.4f}% suspiciously tight (stale data?)",
                        "indicators": indicators, "setups_checked": setups_checked,
                        "funnel": dict(self._funnel),
                    }
                    return []

        # ══════════════════════════════════════════════════════
        # PROJECTED DURATION VETO
        # Reject if ATR suggests trade won't complete in Scalper window
        # BTC window: 27 min, others: 12 min
        # Estimate: bars_to_tp = TP1_dist / (ATR_1bar × directional_factor)
        # ══════════════════════════════════════════════════════
        if _edge_atr > 0 and best.entry_price > 0:
            scalper_window_min = 30 if ("BTC" in symbol or "ETH" in symbol) else 15
            risk_dist = abs(best.entry_price - best.stop_loss)
            tp1_dist = risk_dist * self.tp1_rr
            # ATR per 5m bar → estimated bars to reach TP1
            # Directional factor: ~40% of ATR is directional on average
            directional_atr = _edge_atr * 0.4
            if directional_atr > 0:
                est_bars_to_tp = tp1_dist / directional_atr
                est_minutes_to_tp = est_bars_to_tp * 5  # 5m bars
                if est_minutes_to_tp > scalper_window_min * 1.5:  # 50% buffer
                    if not self._is_learning and not is_sb:
                        self.last_scan_status[symbol] = {
                            "time": now_iso, "signal": False,
                            "reason": f"DURATION VETO: est {est_minutes_to_tp:.0f}m to TP1 > {scalper_window_min}m window",
                            "indicators": indicators, "setups_checked": setups_checked,
                            "funnel": dict(self._funnel),
                        }
                        return []

        # ══════════════════════════════════════════════════════
        
        
        # --- Fib 0.618 HARD GATE for bos_choch (Upgrade 3) ---
        if best_sr.scanner_name == "bos_choch":
            _fib_at = fib_data.get("at_fib", False) if fib_data else False
            _fib_nearest = fib_data.get("nearest_level", "") if fib_data else ""
            if not (_fib_at and _fib_nearest in ("0.618", "0.786", "0.5")):
                self._funnel["blocked_fib_gate"] = self._funnel.get("blocked_fib_gate", 0) + 1
                logger.info("FIB HARD GATE: %s bos_choch requires Fib 0.618/0.786/0.5 — BLOCKED (nearest=%s at_fib=%s)",
                           symbol, _fib_nearest, _fib_at)
                return []

# --- Fee Viability Gate (Upgrade 1) ---
        if best.entry_price > 0 and best.stop_loss > 0:
            _fee_rt = self.FEE_RT_MAKER if getattr(self, "_order_type", "auto") != "taker_only" else self.FEE_RT_TAKER
            _min_move_pct = _fee_rt * self.FEE_VIABILITY_MULT
            _tp1_dist = abs(best.entry_price * (1 + self._scanner_sl_tp.get(best_sr.scanner_name, {}).get("tp1_rr", 1.5) * abs(best.stop_loss - best.entry_price) / best.entry_price) - best.entry_price) if best.entry_price > 0 else 0
            _expected_pct = _tp1_dist / best.entry_price if best.entry_price > 0 else 0
            if _expected_pct < _min_move_pct and _expected_pct > 0:
                self._funnel["blocked_fee"] = self._funnel.get("blocked_fee", 0) + 1
                logger.info("FEE GATE: %s %s expected=%.4f%% < min=%.4f%% (3x fees) — BLOCKED",
                           symbol, best_sr.scanner_name, _expected_pct*100, _min_move_pct*100)
                return []

        # ML SCORING GATE — VM2 ML API validation
        # After all rule-based vetos pass, score via ML model.
        # Shadow mode: log ML verdict but never veto.
        # Live mode: veto if ML probability < per-symbol threshold.
        # ══════════════════════════════════════════════════════
        ml_result = {"probability": 0.5, "verdict": "SKIPPED"}
        try:
            # Build feature vector matching training features
            # Compute HTF trend strength for ML features (legacy param, kept for compat)
            _htf_ts = 0.0
            if htf_df is not None and len(htf_df) > 5:
                _ema8_htf = float(htf_df["ema_8"].iloc[-1]) if "ema_8" in htf_df.columns else 0
                _ema21_htf = float(htf_df["ema_21"].iloc[-1]) if "ema_21" in htf_df.columns else 0
                _atr_htf = float(htf_df.get("atr_14", htf_df.get("atr", pd.Series([1]))).iloc[-1])
                _htf_ts = (_ema8_htf - _ema21_htf) / _atr_htf if _atr_htf > 0 else 0.0
            # Phase 4.1b: pass 15m + 1h + 4h HTF frames so unified_features
            # can compute the full 204-feature schema (was 73 pre-4.1b).
            # Phase 5.0a: also pass cached BTC 5m df for cross-asset features.
            # Phase 5.0c: pass orderbook snapshot from cache if available
            _ob_snap = None
            try:
                _ob_cache = getattr(self, "_ob_cache", None)
                if _ob_cache is not None:
                    _ob_snap = _ob_cache.get(symbol)
            except Exception:
                pass
            ml_features = build_scoring_features(
                df, idx=-1, side=best.side.value if best.side else "long",
                symbol=symbol,
                htf_bias=float(htf_bias),
                htf_trend_strength=_htf_ts,
                htf_15m=htf_df,   # 15m (already loaded above as htf_df)
                htf_1h=df_1h,     # 1h (Phase 4.1a macro trend features)
                htf_4h=df_4h,     # 4h (Phase 4.1a session/structure features)
                btc_df=getattr(self, "_btc_df_cache", None),  # Phase 5.0a
                orderbook=_ob_snap,  # Phase 5.0c
            )
            # Add context features not in candle data
            ml_features["confidence"] = float(best.confidence)
            ml_features["weighted_score"] = float(best_sr.weighted_score)

            # Score via VM2 ML API
            # Phase 4.5: pass symbol + side so server can route to the family
            # model (liquid_majors / secondary / high_beta) before falling back
            # to the per-scanner model.
            ml_result = self._ml_scorer.score_candidate(
                scanner_name=best_sr.scanner_name,
                features=ml_features,
                symbol=symbol,
                side=best.side,
            )
            self._last_ml_result[symbol] = ml_result

            # Phase 4.2: None means ABSTAIN (model missing, schema drift, API error)
            # Treat as 0.5 for logging purposes, but the verdict will be ABSTAIN_*
            # which downstream code can detect and skip blocking.
            _ml_prob_raw = ml_result.get("probability")
            ml_prob = float(_ml_prob_raw) if _ml_prob_raw is not None else 0.5
            ml_verdict = ml_result.get("verdict", "?")
            ml_latency = ml_result.get("latency_ms", 0)
            ml_is_abstain = str(ml_verdict).startswith("ABSTAIN")

            # ── Phase 4.8: SELECTIVE ML GATING by edge_verdict ──
            # Walk-forward OOS evaluation (Phase 4.3) classifies each trained
            # (scanner, family) model as one of:
            #   HOLDS    — OOS AUC ≥ 0.58 AND std ≤ 0.05  → earn the right to tighten
            #   WEAK     — OOS AUC ≥ 0.54               → soft blend only
            #   UNCLEAR  — between (noisy signal)         → slight penalty
            #   NO_EDGE  — OOS AUC ≤ 0.52                 → blocked at save time
            # The dashboard returns the verdict with every score response.
            # HOLDS models earn tighter gates (threshold 0.55 minimum); the
            # rest use the per-symbol default. This implements "ML acts as a
            # real gate only on models that have proved OOS edge".
            _edge_verdict = ml_result.get("edge_verdict")  # None for pre-4.5 models
            _ml_resolved_scope = ml_result.get("resolved_scope", "scanner")
            _ml_resolved_family = ml_result.get("resolved_family")

            # Pre-decision log: always log what ML thinks + the edge_verdict
            logger.info(
                "ML SCORE [%s] %s %s: prob=%.3f verdict=%s edge=%s scope=%s fam=%s "
                "latency=%.0fms | scanner=%s conf=%d regime=%s session=%s",
                "SHADOW" if self._ml_shadow_mode else "LIVE",
                best.side.value.upper() if best.side else "?",
                symbol, ml_prob, ml_verdict, _edge_verdict or "-",
                _ml_resolved_scope, _ml_resolved_family or "-",
                ml_latency,
                best_sr.scanner_name, best.confidence, regime,
                getattr(self, '_current_session', ''),
            )

            # ── Phase A.5: QUANTILE RANKING ──
            # Rolling deque of recent ML probabilities per (scanner, family). The
            # Phase 4.8 HOLDS tightening uses fixed 0.55 which is blind to regime —
            # in chop only the top 5% of candidates will clear, while in a strong
            # trend the top 40% will. Phase A.5 makes the threshold adaptive: take
            # the top 15% of the rolling window so trade volume stays normalized
            # across regimes without losing selectivity.
            #
            # Only populated for HOLDS model calls (we don't want WEAK/UNCLEAR
            # scores polluting the HOLDS distribution). Fallback to Phase 4.8
            # fixed threshold when the deque has fewer than 20 samples.
            if not hasattr(self, "_ml_prob_history"):
                from collections import deque as _deque
                self._ml_prob_history: Dict[str, "_deque"] = {}
            # Key includes family so each (scanner, family) has its own distribution
            _quant_key = f"{best_sr.scanner_name}__{_ml_resolved_family or 'none'}"
            if not ml_is_abstain and _ml_prob_raw is not None:
                _dq = self._ml_prob_history.get(_quant_key)
                if _dq is None:
                    from collections import deque as _deque2
                    _dq = _deque2(maxlen=100)
                    self._ml_prob_history[_quant_key] = _dq
                _dq.append(ml_prob)

            # ── ML HARD VETO GATE (pair-specific thresholds) ──
            # Data proves: trades below the ML threshold lose money.
            # ML GATE — SHADOW MODE until retrained on clean (enforced) data.
            # ML was trained on paper_learning garbage — it has no real edge.
            # Log the ML verdict but DO NOT hard-block.
            # Re-enable hard-block after retraining on 500+ clean enforced-mode trades.
            #
            # Phase 4.2: If ML abstained (no model, schema drift, API error),
            # SKIP all ML-based blocking. Fail-open so a broken ML server
            # doesn't stop trading. Other hotfix gates (P0-P4, P3.6, P3.7,
            # P3.11, P3.22) still protect.
            if ml_is_abstain:
                logger.info(
                    "ML ABSTAIN (%s) — skipping ML veto gate, relying on hotfix stack",
                    ml_verdict,
                )
                _ml_conf_adj = 0
            elif not self._is_learning:
                _ml_conf_adj = 0
                # ── Phase 4.8: compute effective threshold from edge_verdict ──
                # Base: per-symbol threshold from pair_ml_thresholds
                _base_threshold = self.pair_ml_thresholds.get(symbol, self.default_ml_threshold)

                # Verdict-aware adjustment:
                #  HOLDS   → Phase A.5 quantile rank (top 15% of rolling window)
                #            falling back to Phase 4.8 fixed 0.55 when n < 20
                #  WEAK    → no change. Soft blend only (hotfix stack protects).
                #  UNCLEAR → slight penalty (base + 0.03). Noisy model, prefer caution.
                #  NO_EDGE → shouldn't reach here (blocked at save), but we fail-safe to base.
                #  None    → pre-4.5 model (no edge_verdict). Use base unchanged.
                if _edge_verdict == "HOLDS":
                    # Phase A.5: adaptive quantile — top 15% of rolling window
                    _dq_hist = self._ml_prob_history.get(_quant_key)
                    if _dq_hist is not None and len(_dq_hist) >= 20:
                        import numpy as _np_local
                        _q85 = float(_np_local.percentile(list(_dq_hist), 85))
                        # Floor 0.42, ceiling 0.75 (was 0.55 — too high for sideways/asia sessions)
                        _q_thresh = max(0.42, min(0.75, _q85))
                        ml_threshold = max(_base_threshold, _q_thresh)
                        _verdict_action = "QUANTILE_HOLDS"
                        logger.info(
                            "A.5 QUANTILE: %s %s n=%d p85=%.3f → threshold=%.3f",
                            symbol, best_sr.scanner_name, len(_dq_hist), _q85, ml_threshold,
                        )
                    else:
                        ml_threshold = max(_base_threshold, 0.55)
                        _verdict_action = "TIGHTEN_HOLDS"
                elif _edge_verdict == "UNCLEAR":
                    ml_threshold = min(_base_threshold + 0.03, 0.60)
                    _verdict_action = "PENALTY_UNCLEAR"
                elif _edge_verdict == "NO_EDGE":
                    # Fail-safe: NO_EDGE models shouldn't be served but if one
                    # slipped past the Phase 4.5 gate, defang it with a very
                    # high threshold (0.75) — effectively skip every trade.
                    ml_threshold = max(_base_threshold, 0.75)
                    _verdict_action = "DEFANG_NO_EDGE"
                else:
                    # WEAK or None: base threshold, no tightening
                    ml_threshold = _base_threshold
                    _verdict_action = "BASE"

                # Log the effective gating decision
                if _edge_verdict in ("HOLDS", "UNCLEAR", "NO_EDGE"):
                    logger.info(
                        "ML GATE [%s]: %s %s edge=%s base=%.2f → effective=%.2f",
                        _verdict_action, symbol, best_sr.scanner_name,
                        _edge_verdict, _base_threshold, ml_threshold,
                    )

                if ml_prob < ml_threshold:
                    self._funnel["blocked_ml"] = self._funnel.get("blocked_ml", 0) + 1
                    # Log for ML retraining data collection
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
                    if self._ml_veto_skip:
                        # SKIP veto active — block the trade
                        logger.info(
                            "ML SKIP VETO: %s %s prob=%.3f < %.2f threshold (scanner=%s) — BLOCKED",
                            best.side.value.upper() if best.side else "?",
                            symbol, ml_prob, ml_threshold, best_sr.scanner_name,
                        )
                        return []
                    else:
                        # Shadow only — log but don't block
                        logger.info(
                            "ML SHADOW (no block): %s %s prob=%.3f < %.2f threshold (scanner=%s verdict=%s)",
                            best.side.value.upper() if best.side else "?",
                            symbol, ml_prob, ml_threshold, best_sr.scanner_name, ml_verdict,
                        )

                        # ── Phase 3.6 P3.6 CONSERVATIVE ML LIVE BLOCK ──
                        # Loss taxonomy showed 40 of 65 losses (62%, -$77) had ml_verdict=WEAK.
                        # Even though ML is in shadow mode, we can SAFELY block the worst combos:
                        # WEAK ML + low conf + no HTF tailwind = guaranteed loser pattern.
                        # This adds a SECOND-OPINION check that fires only when MULTIPLE quality
                        # signals are weak, minimizing false positives.
                        if not hasattr(self, '_p3_6_ml_conservative_block'):
                            self._p3_6_ml_conservative_block = True  # default ON
                        try:
                            _verdict_upper = str(ml_verdict).upper()
                            _conf_p36 = getattr(best, 'confidence', 100)
                            _side_str_p36 = best.side.value if best.side else ""
                            _htf_aligned_p36 = (
                                (htf_bias > 0 and _side_str_p36 == "long") or
                                (htf_bias < 0 and _side_str_p36 == "short")
                            )
                            _grade_p36 = getattr(best, 'grade', '') or ''
                            _regime_lower_p36 = str(regime).lower() if regime else ""
                            _chop_regimes_p36 = ("high_volatility", "sideways", "mean_reversion", "ranging")
                            _is_chop_p36 = _regime_lower_p36 in _chop_regimes_p36

                            # ── P3.22 TIGHTENING (2026-04-11) ──
                            # OLD: only blocked WEAK + conf<70 + not-HTF-aligned
                            # NEW: data-driven reasons to block ML=WEAK:
                            #
                            # Rule A (original): WEAK + conf<70 + not HTF aligned
                            # Rule B (NEW):      WEAK + chop regime (regardless of HTF/grade/conf)
                            #                    Justification: today's losers were all chop+WEAK
                            #                    including A+ grades that paper WOULD have traded
                            # Rule C (NEW):      WEAK + ml_prob < 0.45 (very low prob)
                            #                    Justification: ML is telling us NO
                            #
                            # A+ ESCAPE (preserved): grade=A+ with htf_aligned=True AND trending
                            # regime still fires (high-conviction setups)
                            _a_plus_escape = (
                                _grade_p36 == "A+"
                                and _htf_aligned_p36
                                and _regime_lower_p36 in ("trending_up", "trending_down", "breakout")
                            )

                            _rule_a = (
                                _verdict_upper == "WEAK"
                                and _conf_p36 < 70
                                and not _htf_aligned_p36
                            )
                            _rule_b = (
                                _verdict_upper == "WEAK"
                                and _is_chop_p36
                                and not _a_plus_escape
                            )
                            _rule_c = (
                                _verdict_upper == "WEAK"
                                and float(ml_prob) < 0.45
                                and not _a_plus_escape
                            )
                            _p36_block = (
                                self._p3_6_ml_conservative_block
                                and (_rule_a or _rule_b or _rule_c)
                            )
                            if _p36_block:
                                _trigger_rule = (
                                    "A (low_conf+not_htf)" if _rule_a else
                                    ("B (chop_regime)" if _rule_b else "C (very_low_prob)")
                                )
                                logger.warning(
                                    "P3.6 ML LIVE BLOCK [rule %s]: %s %s prob=%.3f WEAK conf=%d grade=%s regime=%s — BLOCKED",
                                    _trigger_rule, _side_str_p36.upper(), symbol, ml_prob,
                                    _conf_p36, _grade_p36, _regime_lower_p36,
                                )
                                try:
                                    from bot import pipeline_metrics as _pm
                                    _pm.record_hotfix_veto(
                                        "p3_6_ml_weak_block",
                                        f"{symbol}_{_side_str_p36}_{_trigger_rule}_conf{_conf_p36}_prob{ml_prob:.2f}",
                                    )
                                except Exception:
                                    pass
                                self._funnel["blocked_ml_p3_6"] = self._funnel.get("blocked_ml_p3_6", 0) + 1
                                return []
                        except Exception as _p36_exc:
                            logger.debug("P3.6 ML check failed: %s", _p36_exc)
                elif ml_prob < 0.55:
                    _ml_conf_adj = 0    # baseline, no adjustment
                elif ml_prob >= 0.65:
                    _ml_conf_adj = +10  # high conviction bonus

                if _ml_conf_adj != 0:
                    _adj_label = f"ML_{'PENALTY' if _ml_conf_adj < 0 else 'BONUS'}: {_ml_conf_adj:+d} (prob={ml_prob:.3f})"
                    best = _SetupResult(
                        name=best.name, side=best.side,
                        confidence=max(best.confidence + _ml_conf_adj, 30),
                        confirmations=best.confirmations + [f"[{_adj_label}]"],
                        entry_price=best.entry_price, stop_loss=best.stop_loss, atr=best.atr,
                    )
                    logger.info(
                        "ML GRADUATED: %s %s prob=%.3f → conf_adj=%+d (new_conf=%d)",
                        best.side.value.upper() if best.side else "?",
                        symbol, ml_prob, _ml_conf_adj, best.confidence,
                    )

        except Exception as e:
            # Fail-open: if ML scoring fails, proceed with the trade
            logger.warning("ML scoring failed for %s (fail-open): %s", symbol, e)
            ml_result = {"probability": 0.5, "verdict": "ERROR", "error": str(e)}

        # ── Apply per-scanner SL/TP overrides (prefer calibrated, fall back to hardcoded) ──
        scanner_exits = self._calibrated_sl_tp.get(best_sr.scanner_name, {})
        if scanner_exits:
            # Store for _build_signal to use
            self._active_scanner_exits = scanner_exits
        else:
            self._active_scanner_exits = {}

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
        signal.metadata["regime_age"] = regime_age
        signal.metadata["timeframe"] = self.primary_tf  # P3 fix: track which TF triggered
        signal.metadata["regime_size_mult"] = 1.0  # no regime blocking
        signal.metadata["regime_sl_mult"] = 1.0
        signal.metadata["confidence_size_mult"] = confidence_size_mult
        signal.metadata["ev"] = round(ev_result.ev, 4)
        signal.metadata["ev_verdict"] = ev_result.verdict
        signal.metadata["ev_size_mult"] = ev_size_mult
        signal.metadata["p_win"] = round(ev_result.p_win, 4)

        # ── ML scoring metadata ──
        # probability is None when the ML server is unreachable/abstains
        # (Phase 4.2) — round(None) raised TypeError and killed the whole
        # scalp analysis for the symbol. Store None as-is.
        # Downstream consumers (signal_tracker, logging) do float()/%.3f on
        # this field, so fall back to neutral 0.5; ml_verdict still carries
        # UNREACHABLE/ABSTAIN so the outage is visible.
        _ml_prob = ml_result.get("probability")
        signal.metadata["ml_probability"] = round(_ml_prob, 4) if isinstance(_ml_prob, (int, float)) else 0.5
        signal.metadata["ml_verdict"] = ml_result.get("verdict", "?")
        signal.metadata["ml_latency_ms"] = ml_result.get("latency_ms", 0)
        signal.metadata["ml_shadow_mode"] = self._ml_shadow_mode
        # Phase 5 CVD veto: pass CVD proxy to orchestrator for universal veto check
        signal.metadata["cvd_proxy_10"] = ml_features.get("mkt_cvd_proxy_10", ml_features.get("cvd_proxy_10", 0))
        signal.metadata["ml_threshold"] = self._ml_thresholds.get(symbol, 0.65)
        signal.metadata["ml_model_version"] = ml_result.get("model_version", "unknown")
        # Phase 4.5: which model scope actually scored this trade
        signal.metadata["ml_resolved_scope"] = ml_result.get("resolved_scope", "scanner")
        signal.metadata["ml_resolved_family"] = ml_result.get("resolved_family")
        signal.metadata["ml_file_key"] = ml_result.get("file_key", best_sr.scanner_name)
        # Phase 4.8: edge verdict + effective threshold applied (for per-verdict calibration analysis)
        signal.metadata["ml_edge_verdict"] = ml_result.get("edge_verdict")
        signal.metadata["ml_oos_mean"] = ml_result.get("oos_mean")
        signal.metadata["ml_overfit_gap"] = ml_result.get("overfit_gap")
        # _effective_threshold is set in the veto gate block above if the path ran
        try:
            signal.metadata["ml_effective_threshold"] = ml_threshold
            signal.metadata["ml_verdict_action"] = _verdict_action
        except NameError:
            # ABSTAIN path skipped the gate entirely — no effective threshold applied
            signal.metadata["ml_effective_threshold"] = None
            signal.metadata["ml_verdict_action"] = "ABSTAIN_BYPASS" if ml_is_abstain else "LEARNING_BYPASS"
        # Phase 4.6: snapshot the feature vector + prob at entry time so closed-loop
        # analytics can correlate what we scored WITH vs what actually happened.
        # Cap at 32 feature names to keep metadata small — these are typically the
        # top features we already log for debugging.
        signal.metadata["ml_features_matched"] = ml_result.get("features_matched", 0)
        signal.metadata["ml_features_expected"] = ml_result.get("features_expected", 0)
        signal.metadata["ml_match_pct"] = ml_result.get("match_pct", 1.0)
        signal.metadata["ml_feature_schema_hash"] = ml_result.get("feature_schema_hash", "")

        # ══════════════════════════════════════════════════════════════
        # SNIPER SYSTEM (2026-04-12): 5 precision features
        # ══════════════════════════════════════════════════════════════

        # ── SNIPER 1: Confluence Score ──
        # Count how many scanners agree on this symbol + direction.
        # Already computed at line ~1750 as confluence_bonus. Here we
        # formalize it as a metadata field for the real qualify gate.
        _confluence_count = 1  # at least the triggering scanner
        try:
            _triggered = [sr for sr in scan_results if sr.setup_result is not None]
            _same_side = [sr for sr in _triggered if sr.side == best.side]
            _confluence_count = len(_same_side)
        except Exception:
            pass
        signal.metadata["confluence_count"] = _confluence_count
        signal.metadata["confluence_scanners"] = ",".join(
            [sr.scanner_name for sr in _same_side] if '_same_side' in dir() else [best_sr.scanner_name]
        )

        # ── SNIPER 2: Conviction Score (0-100) ──
        # Composite of ML prob + grade + confluence + regime alignment.
        # Used for position sizing and sniper mode ranking.
        _conviction = 0
        try:
            _ml_p = float(signal.metadata.get("ml_probability", 0.5) or 0.5)
            _grade_mult = {"A+": 1.0, "A": 0.85, "B": 0.65, "C": 0.45}.get(signal.grade, 0.4)
            _regime_mult = 1.2 if regime in ("trending_up", "trending_down", "breakout") else 0.8 if regime in ("sideways", "quiet") else 1.0
            _confluence_mult = 1.0 + (_confluence_count - 1) * 0.15  # +15% per extra scanner
            _conviction = int(min(100, _ml_p * 100 * _grade_mult * _regime_mult * _confluence_mult))
        except Exception:
            _conviction = 50
        signal.metadata["conviction_score"] = _conviction

        # ── SNIPER 3: Sniper Mode (top-N daily filter) ──
        # If conviction < daily threshold, mark as "sniper_skip".
        # Real qualify gate uses this to only take the best signals.
        # Default: top 30% conviction = sniper_min_conviction=70
        _sniper_min = int(getattr(self, '_sniper_min_conviction', 60) or 60)
        signal.metadata["sniper_eligible"] = _conviction >= _sniper_min
        if _conviction < _sniper_min:
            signal.metadata["sniper_skip"] = f"conviction={_conviction}<{_sniper_min}"

        # ── SNIPER 4: 1m Candle Confirmation ──
        # Check if the most recent 1m candle confirms the signal direction.
        # Avoids entering at the TOP of a move (candle already extended).
        try:
            _1m_df = candles_dict.get("1m") if candles_dict else None
            if _1m_df is not None and len(_1m_df) >= 2:
                _1m_close = float(_1m_df.iloc[-1]["close"])
                _1m_prev = float(_1m_df.iloc[-2]["close"])
                _1m_dir = "up" if _1m_close > _1m_prev else "down"
                _sig_dir = "down" if best.side and best.side.value in ("short", "sell") else "up"
                signal.metadata["1m_confirmed"] = (_1m_dir == _sig_dir)
                signal.metadata["1m_direction"] = _1m_dir
            else:
                signal.metadata["1m_confirmed"] = True  # no 1m data = allow
        except Exception:
            signal.metadata["1m_confirmed"] = True

        # ── SNIPER 5: Kill Zone Check ──
        # Check if current price is near a key level (round number,
        # daily high/low, or VWAP). Snipers prefer entries AT key levels.
        try:
            _price = float(best.entry_price or 0)
            if _price > 0:
                # Round number proximity (within 0.3%)
                if _price > 100:
                    _round = round(_price / 1000) * 1000
                elif _price > 1:
                    _round = round(_price / 10) * 10
                else:
                    _round = round(_price, 1)
                _dist_pct = abs(_price - _round) / _price * 100
                signal.metadata["near_round_number"] = _dist_pct < 0.3
                signal.metadata["round_number_dist_pct"] = round(_dist_pct, 3)

                # VWAP proximity (from indicators)
                _vwap = float(indicators.get("vwap", 0) or 0) if indicators else 0
                if _vwap > 0:
                    _vwap_dist = abs(_price - _vwap) / _price * 100
                    signal.metadata["near_vwap"] = _vwap_dist < 0.2
                    signal.metadata["vwap_dist_pct"] = round(_vwap_dist, 3)

                # Kill zone composite
                _in_kill_zone = (
                    signal.metadata.get("near_round_number", False) or
                    signal.metadata.get("near_vwap", False) or
                    _confluence_count >= 2
                )
                signal.metadata["in_kill_zone"] = _in_kill_zone
        except Exception:
            signal.metadata["in_kill_zone"] = True  # default allow

        # ── Per-scanner SL/TP config ──
        if scanner_exits:
            signal.metadata["scanner_sl_atr"] = scanner_exits.get("sl_atr", self.sl_atr_mult)
            signal.metadata["scanner_tp1_rr"] = scanner_exits.get("tp1_rr", self.tp1_rr)
            signal.metadata["scanner_tp2_rr"] = scanner_exits.get("tp2_rr", self.tp2_rr)
            signal.metadata["scanner_tp3_rr"] = scanner_exits.get("tp3_rr", self.tp3_rr)

        # ── Scalper window: attach to signal for tracker to enforce ──
        coin_base = symbol.split("/")[0] if "/" in symbol else symbol[:3]
        # Delta Scalper Offer: BTC+ETH=30min, others=15min
        signal.metadata["scalper_window_sec"] = self.scalper_windows.get(coin_base, 15 * 60)
        signal.metadata["structure_bounce_only"] = self.structure_bounce_only
        # Determine entry order type from config
        if self._order_type == "maker":
            signal.metadata["order_type"] = "post_only"
        elif self._order_type == "taker":
            signal.metadata["order_type"] = "market"
        else:  # auto: maker within scalper window (set at signal level, real_manager resolves)
            signal.metadata["order_type"] = "post_only"

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
        # Record per-side cooldown (prevents LONG→SHORT→LONG flip)
        side_val = best.side.value if best.side else "long"
        self._scanner_cooldowns[f"_side_{symbol}_{side_val}"] = now

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
            "SCALP %s [%s]: %s %s | conf=%d w=%.1fx grade=%s | ML=%.3f/%s | %s",
            best.name, best_sr.tier.upper(), best.side.value.upper(), symbol,
            signal.confidence, best_sr.scanner_weight, signal.grade.value,
            signal.metadata["ml_probability"], ml_result.get("verdict", "?"),
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

        # ── Mark winning candidate as EXECUTABLE in lifecycle ──
        existing = self.setup_candidates.get(symbol, [])
        for cand in existing:
            if cand["scanner"] == best_sr.scanner_name and cand["state"] == "CONFIRMED":
                cand["state"] = "EXECUTABLE"
                cand["price"] = round(signal.entry_price, 2)
                cand["reason"] = f"LIVE: {best.side.value.upper()} @ {signal.entry_price:.2f}"
                cand["updated"] = now_iso
                break
        else:
            # Insert executable entry if not already tracked
            existing.insert(0, {
                "scanner": best_sr.scanner_name,
                "state": "EXECUTABLE",
                "side": best.side.value if best.side else "unknown",
                "price": round(signal.entry_price, 2),
                "score": round(best_sr.weighted_score, 1),
                "tier": best_sr.tier,
                "reason": f"LIVE: {best.side.value.upper()} @ {signal.entry_price:.2f}",
                "updated": now_iso,
            })
        self.setup_candidates[symbol] = existing[:3]

        logger.info("FUNNEL %s | SIGNAL EMITTED | scanner=%s side=%s conf=%d entry=%.2f sl=%.2f",
                   symbol, best_sr.scanner_name, best.side.value if best.side else "?",
                   best.confidence, signal.entry_price, signal.stop_loss)
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

    def get_setup_lifecycle(self, symbol: str = None) -> Dict:
        """Return current setup lifecycle states for dashboard display.

        If symbol is given, returns candidates for that symbol only.
        Otherwise returns all symbols' candidates flattened and sorted by score.
        """
        if symbol:
            return {"candidates": self.setup_candidates.get(symbol, [])}

        # Flatten all symbols, add symbol key, sort by score desc
        all_candidates = []
        for sym, candidates in self.setup_candidates.items():
            for c in candidates:
                entry = dict(c)
                entry["symbol"] = sym
                all_candidates.append(entry)
        all_candidates.sort(key=lambda c: c.get("score", 0), reverse=True)
        return {"candidates": all_candidates[:6]}

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
        """SEQUENCE-BASED EMA Momentum: cross → pullback → RSI turn → volume confirm.

        Instead of triggering on the cross candle itself, we look for a 4-step sequence:
        1. EMA8/21 cross occurred within last 6 candles (the event)
        2. Price pulled back to test the cross area (within 0.5× ATR of EMA midpoint)
        3. RSI turned in signal direction (rising for LONG, falling for SHORT)
        4. Current candle has volume > 1.0× avg and closes in signal direction

        This avoids firing on the cross candle (which is often the worst entry) and
        waits for the pullback-and-go confirmation.
        """
        if len(df) < 8:
            return None

        last = df.iloc[-1]
        atr = last["atr"]
        close = last["close"]
        open_ = last["open"]

        if atr <= 0 or np.isnan(atr):
            return None

        # --- STEP 1: Find EMA cross in last 15 candles (NOT current bar) ---
        # Extended from 6→12→15 — crosses are rare on 5m, need wider window
        cross_idx = None
        cross_type = None  # "bullish" or "bearish"
        for i in range(2, min(16, len(df))):
            bar = df.iloc[-i]
            bar_prev = df.iloc[-i - 1] if (i + 1) <= len(df) else None
            if bar_prev is None:
                continue
            e8 = bar["ema_8"]
            e21 = bar["ema_21"]
            e8p = bar_prev["ema_8"]
            e21p = bar_prev["ema_21"]
            if np.isnan(e8) or np.isnan(e21) or np.isnan(e8p) or np.isnan(e21p):
                continue
            if e8p <= e21p and e8 > e21:
                cross_idx = i
                cross_type = "bullish"
                break
            elif e8p >= e21p and e8 < e21:
                cross_idx = i
                cross_type = "bearish"
                break

        if cross_idx is None:
            return None

        # DATA: ema_momentum SHORT = 0% WR — block bearish entirely
        if cross_type == "bearish":
            return None

        side = OrderSide.LONG if cross_type == "bullish" else OrderSide.SHORT

        # --- STEP 2: Price pulled back to EMA cross area ---
        ema8_now = last["ema_8"]
        ema21_now = last["ema_21"]
        if np.isnan(ema8_now) or np.isnan(ema21_now):
            return None
        ema_mid = (ema8_now + ema21_now) / 2
        pullback_dist = abs(close - ema_mid) / atr
        # Must be near the EMA zone (within 0.6× ATR)
        if pullback_dist > 0.6:
            return None
        # For LONG: must still be above EMA21 (trend intact)
        if side == OrderSide.LONG and close < ema21_now * 0.998:
            return None
        # For SHORT: must still be below EMA21
        if side == OrderSide.SHORT and close > ema21_now * 1.002:
            return None

        # --- STEP 3: RSI turning in signal direction ---
        rsi = last["rsi"]
        rsi_prev = df.iloc[-2]["rsi"]
        rsi_prev2 = df.iloc[-3]["rsi"] if len(df) >= 4 else rsi_prev
        if np.isnan(rsi) or np.isnan(rsi_prev):
            return None
        if side == OrderSide.LONG:
            rsi_turning = rsi > rsi_prev and rsi_prev <= rsi_prev2  # bottom formed
            rsi_range_ok = 30 < rsi < 70  # relaxed from 35-65
        else:
            rsi_turning = rsi < rsi_prev and rsi_prev >= rsi_prev2  # top formed
            rsi_range_ok = 30 < rsi < 70  # relaxed from 35-65
        if not (rsi_turning and rsi_range_ok):
            return None

        # --- STEP 4: Current candle confirms (directional close + volume) ---
        rel_vol = last.get("rel_vol", 1.0)
        if np.isnan(rel_vol):
            rel_vol = 1.0
        if side == OrderSide.LONG:
            if close <= open_:  # need bullish candle
                return None
        else:
            if close >= open_:  # need bearish candle
                return None
        if rel_vol < 0.7:  # relaxed from 0.8 — 5m volume can be patchy
            return None

        # --- ALL 4 STEPS PASSED: Build signal ---
        confs = []
        score = 0

        confs.append(f"EMA 8/21 {cross_type} cross ({cross_idx} bars ago)")
        score += 30

        confs.append(f"Pullback to EMA zone ({pullback_dist:.2f}× ATR)")
        score += 15

        confs.append(f"RSI turning {rsi_prev:.0f}→{rsi:.0f}")
        score += 15

        if rel_vol > 1.2:
            confs.append(f"Volume {rel_vol:.1f}x avg")
            score += 15
        elif rel_vol > 0.9:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 10
        else:
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

        # Candle quality: body must be meaningful (not doji)
        body_ratio = body / full_range if full_range > 0 else 0
        if body_ratio < 0.25:
            return None  # doji/spinning top = unreliable bounce signal

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
        """SEQUENCE-BASED Trend Continuation: impulse → pullback → hold → trigger.

        4-step event sequence over last 8 candles:
        1. IMPULSE: A candle with body > 0.8× ATR in trend direction (within last 8 bars)
        2. PULLBACK: 2-4 candle pullback (lower highs for LONG, higher lows for SHORT)
        3. HOLD: Price stays above EMA21 (LONG) or below EMA21 (SHORT) during pullback
        4. TRIGGER: Current candle is bullish/bearish with volume > 0.9× avg

        This replaces the old "EMA aligned + RSI + candle" snapshot check that fired
        on every single bar in a trend, producing 148 signals in 7 days at 12% WR.
        """
        if len(df) < 8:
            return None

        last = df.iloc[-1]
        atr = last["atr"]
        close = last["close"]
        open_ = last["open"]

        if atr <= 0 or np.isnan(atr):
            return None

        ema8 = last["ema_8"]
        ema21 = last["ema_21"]
        ema50 = last["ema_50"]

        if np.isnan(ema8) or np.isnan(ema21) or np.isnan(ema50):
            return None

        # Determine trend direction from EMA alignment
        # Relaxed: only need ema8 vs ema21 (not strict triple alignment)
        is_uptrend = ema8 > ema21
        is_downtrend = ema8 < ema21
        if not is_uptrend and not is_downtrend:
            return None

        side = OrderSide.LONG if is_uptrend else OrderSide.SHORT
        ema_gap_pct = abs(ema8 - ema21) / ema21 * 100 if ema21 > 0 else 0
        if ema_gap_pct < 0.01:
            return None  # EMAs too close (relaxed from 0.02)

        # Bonus for triple alignment (ema8 > ema21 > ema50)
        triple_aligned = (ema8 > ema21 > ema50) if is_uptrend else (ema8 < ema21 < ema50)

        # --- STEP 1: Find impulse candle in last 8 bars ---
        impulse_idx = None
        impulse_body = 0
        for i in range(2, min(9, len(df))):
            bar = df.iloc[-i]
            bar_body = abs(bar["close"] - bar["open"])
            bar_atr = bar.get("atr", atr)
            if bar_atr <= 0 or np.isnan(bar_atr):
                bar_atr = atr
            if bar_body < bar_atr * 0.25:
                continue  # relaxed 0.5→0.35→0.30→0.25 for 5m candles (smaller bodies)
            # Must be in trend direction
            if side == OrderSide.LONG and bar["close"] > bar["open"]:
                impulse_idx = i
                impulse_body = bar_body
                break
            elif side == OrderSide.SHORT and bar["close"] < bar["open"]:
                impulse_idx = i
                impulse_body = bar_body
                break

        if impulse_idx is None:
            return None  # No recent impulse = no continuation setup

        # --- STEP 2: Pullback between impulse and now (2-4 candles) ---
        pullback_bars = impulse_idx - 1  # bars between impulse and current
        if pullback_bars < 1 or pullback_bars > 5:
            return None  # Need 1-5 bar pullback, not too fast not too slow

        pullback_candles = [df.iloc[-j] for j in range(2, impulse_idx)]
        if not pullback_candles:
            return None

        # Verify pullback structure
        if side == OrderSide.LONG:
            # Pullback = lower highs or consolidation (not new highs)
            impulse_high = df.iloc[-impulse_idx]["high"]
            pullback_made_new_high = any(c["high"] > impulse_high for c in pullback_candles)
            if pullback_made_new_high:
                return None  # Not a pullback, still impulsing
            # Check pullback depth: shallow = good (< 1.0× ATR)
            pullback_low = min(c["low"] for c in pullback_candles)
            pullback_depth = impulse_high - pullback_low
            if pullback_depth > atr * 1.5:
                return None  # Too deep — trend may be failing
        else:
            impulse_low = df.iloc[-impulse_idx]["low"]
            pullback_made_new_low = any(c["low"] < impulse_low for c in pullback_candles)
            if pullback_made_new_low:
                return None
            pullback_high = max(c["high"] for c in pullback_candles)
            pullback_depth = pullback_high - impulse_low
            if pullback_depth > atr * 1.5:
                return None

        # --- STEP 3: Hold — price mostly stayed above EMA21 (LONG) or below (SHORT) ---
        # Relaxed: allow 1 bar to briefly violate (5m candles are noisy)
        ema_violations = 0
        for pc in pullback_candles:
            if side == OrderSide.LONG:
                pc_ema21 = pc.get("ema_21", ema21)
                if np.isnan(pc_ema21):
                    pc_ema21 = ema21
                if pc["close"] < pc_ema21 * 0.995:
                    ema_violations += 1
            else:
                pc_ema21 = pc.get("ema_21", ema21)
                if np.isnan(pc_ema21):
                    pc_ema21 = ema21
                if pc["close"] > pc_ema21 * 1.005:
                    ema_violations += 1
        if ema_violations > 1:  # allow 1 brief dip, block 2+
            return None

        # --- STEP 4: Trigger candle — directional close + volume ---
        rel_vol = last.get("rel_vol", 1.0)
        if np.isnan(rel_vol):
            rel_vol = 1.0
        if side == OrderSide.LONG:
            if close <= open_:
                return None  # Need bullish trigger
        else:
            if close >= open_:
                return None  # Need bearish trigger
        if rel_vol < 0.7:
            return None  # Need some volume on trigger (relaxed from 0.8)

        # Candle body quality — trigger candle should be meaningful
        body = abs(close - open_)
        if body < atr * 0.2:
            return None  # Doji/spinning top = weak trigger (relaxed from 0.3)

        # --- ALL 4 STEPS PASSED: Score the setup ---
        confs = []
        score = 0

        confs.append(f"{'Up' if is_uptrend else 'Down'}trend continuation")
        score += 25

        confs.append(f"Impulse ({impulse_body/atr:.1f}× ATR, {impulse_idx} bars ago)")
        score += 15

        confs.append(f"{pullback_bars}-bar pullback (depth {pullback_depth/atr:.1f}× ATR)")
        score += 10

        confs.append(f"Held above EMA21 during pullback")
        score += 10

        if ema_gap_pct > 0.08:
            confs.append(f"EMA gap {ema_gap_pct:.3f}%")
            score += 10

        # MACD confirmation
        macd_hist = last.get("macd_hist", 0)
        if (side == OrderSide.LONG and macd_hist > 0) or (side == OrderSide.SHORT and macd_hist < 0):
            confs.append("MACD aligned")
            score += 10

        # Supertrend
        st_dir = last.get("supertrend_dir", 0)
        if (side == OrderSide.LONG and st_dir == 1) or (side == OrderSide.SHORT and st_dir == -1):
            confs.append("Supertrend agrees")
            score += 5

        # Volume quality
        if rel_vol > 1.5:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 10
        elif rel_vol > 1.0:
            confs.append(f"Volume {rel_vol:.1f}x")
            score += 5

        # HTF alignment bonus
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        # 5m confirmation
        if confirm_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("5m aligned")
            score += 10

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

        # Bullish divergence - tuned for 5m crypto (6-month backtest: +353R, 40.6% WR)
        if (close <= price_min * 1.005  # price near recent low (relaxed)
            and rsi_diff_bull >= 4       # RSI 4+ points higher
            and rsi_now < 52             # RSI below midpoint (relaxed from 48)
            and rsi_at_min < 42):        # Original RSI was low (relaxed from 38)
            side = OrderSide.LONG
            confs.append(f"Bullish RSI divergence ({rsi_at_min:.0f}→{rsi_now:.0f})")
            score += 35

            if rsi_now < 30:
                confs.append("RSI deep oversold")
                score += 15
            elif rsi_now < 35:
                score += 8
            if rsi_diff_bull >= 12:
                confs.append("Strong divergence")
                score += 10

        # Bearish divergence - tuned for 5m crypto
        elif (close >= price_max * 0.995  # price near recent high (relaxed)
              and rsi_diff_bear >= 4       # RSI 4+ points lower
              and rsi_now > 48             # RSI above midpoint (relaxed from 52)
              and rsi_at_max > 55):        # Original RSI was elevated (relaxed from 58)
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
        """SEQUENCE-BASED Structure Bounce: approach → rejection → volume → confirmation.

        4-step event sequence:
        1. APPROACH: Price moved toward S/R level within last 3 candles (within 0.3%)
        2. REJECTION: A candle at the level with wick > 50% of range, close inside level
        3. VOLUME: Volume spike on the rejection candle (> 1.2× avg)
        4. CONFIRMATION: Current candle closes in signal direction away from level

        This replaces the old single-candle check that triggered on any candle near S/R.
        """
        sm = self._structure_map
        if sm is None:
            logger.debug("structure_bounce: no structure map")
            return None

        if len(df) < 4:
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
        rejection_bar_idx = None  # which bar had the rejection

        # --- STEP 1+2: Find approach + rejection in last 5 bars ---
        for check_offset in range(1, min(6, len(df))):
            bar = df.iloc[-check_offset]
            bar_close = float(bar["close"])
            bar_open = float(bar["open"])
            bar_high = float(bar["high"])
            bar_low = float(bar["low"])
            bar_body = abs(bar_close - bar_open)
            bar_range = bar_high - bar_low
            if bar_range <= 0:
                continue

            # Check SUPPORT bounce (LONG)
            if sm.nearest_support:
                lvl = sm.nearest_support
                in_zone = lvl.zone_low <= bar_low <= lvl.zone_high
                dist_pct = (bar_low - lvl.price) / bar_close * 100 if bar_close > 0 else 999
                if in_zone or (abs(dist_pct) < 0.5):
                    lower_wick = min(bar_open, bar_close) - bar_low
                    # Rejection: wick > 50% of range AND close in upper half
                    has_rejection = (lower_wick > bar_range * 0.30 and
                                     bar_close > (bar_low + bar_range * 0.45))
                    if has_rejection:
                        side = OrderSide.LONG
                        target_level = lvl
                        rejection_bar_idx = check_offset
                        confs.append(f"S/R support rejection ({lvl.level_type})")
                        score += 30
                        if lower_wick > atr * 0.5:
                            confs.append(f"Rejection wick ({lower_wick/atr:.1f}× ATR)")
                            score += 15
                        elif lower_wick > atr * 0.3:
                            confs.append(f"Wick at level ({lower_wick/atr:.1f}× ATR)")
                            score += 10
                        if in_zone:
                            confs.append("Inside structure zone")
                            score += 5
                        break

            # Check RESISTANCE rejection (SHORT)
            if sm.nearest_resistance:
                lvl = sm.nearest_resistance
                in_zone = lvl.zone_low <= bar_high <= lvl.zone_high
                dist_pct = (lvl.price - bar_high) / bar_close * 100 if bar_close > 0 else 999
                if in_zone or (abs(dist_pct) < 0.5):
                    upper_wick = bar_high - max(bar_open, bar_close)
                    has_rejection = (upper_wick > bar_range * 0.30 and
                                     bar_close < (bar_low + bar_range * 0.55))
                    if has_rejection:
                        side = OrderSide.SHORT
                        target_level = lvl
                        rejection_bar_idx = check_offset
                        confs.append(f"S/R resistance rejection ({lvl.level_type})")
                        score += 30
                        if upper_wick > atr * 0.5:
                            confs.append(f"Rejection wick ({upper_wick/atr:.1f}× ATR)")
                            score += 15
                        elif upper_wick > atr * 0.3:
                            confs.append(f"Wick at level ({upper_wick/atr:.1f}× ATR)")
                            score += 10
                        if in_zone:
                            confs.append("Inside structure zone")
                            score += 5
                        break

        if side is None or target_level is None:
            return None

        # --- STEP 3: Volume spike on or near rejection candle ---
        rejection_bar = df.iloc[-rejection_bar_idx]
        rej_vol = float(rejection_bar.get("rel_vol", 1.0))
        if np.isnan(rej_vol):
            rej_vol = 1.0
        # Also check current bar volume
        curr_vol = float(last.get("rel_vol", 1.0))
        if np.isnan(curr_vol):
            curr_vol = 1.0
        best_vol = max(rej_vol, curr_vol)

        if best_vol > 1.5:
            confs.append(f"Volume spike {best_vol:.1f}×")
            score += 15
        elif best_vol > 1.2:
            confs.append(f"Volume {best_vol:.1f}×")
            score += 10
        elif best_vol > 0.9:
            score += 5
        else:
            # Low volume at structure = weak bounce, still allow but penalize
            score -= 5

        # --- STEP 4: Confirmation candle (current bar closes away from level) ---
        if rejection_bar_idx == 1:
            # Rejection IS the current candle — accept if it already closed in direction
            if side == OrderSide.LONG and close <= open_:
                return None  # Need bullish close for confirmation
            elif side == OrderSide.SHORT and close >= open_:
                return None
        else:
            # Rejection was earlier — current candle must confirm direction
            if side == OrderSide.LONG:
                if close <= open_:
                    return None  # bearish = no confirmation
                # Must be moving away from support
                if close < target_level.price:
                    return None  # still below level
            else:
                if close >= open_:
                    return None
                if close > target_level.price:
                    return None  # still above level

            confs.append(f"Confirmation candle ({rejection_bar_idx - 1} bar delay)")
            score += 5

        # Level strength bonus
        score += min(target_level.strength // 5, 15)
        if target_level.touch_count >= 3:
            confs.append(f"{target_level.touch_count} touches")
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
        max_sl_dist = min(atr * 2.0, close * 0.005)
        if side == OrderSide.LONG:
            struct_sl = target_level.zone_low - close * 0.001
            sl = max(struct_sl, close - max_sl_dist)
        else:
            struct_sl = target_level.zone_high + close * 0.001
            sl = min(struct_sl, close + max_sl_dist)

        
        # --- Stochastic + OBV Filter (Upgrade 6) ---
        _stk = float(df.iloc[-1].get("stoch_k", 50)) if "stoch_k" in df.columns else 50.0
        _obv = float(df.iloc[-1].get("obv_slope", 0)) if "obv_slope" in df.columns else 0.0
        if side == OrderSide.LONG and _stk > 80:
            return None  # Don't buy at overbought
        if side == OrderSide.SHORT and _stk < 20:
            return None  # Don't sell at oversold
        if side == OrderSide.LONG and _obv < -1.5:
            return None  # Distribution — don't buy
        if side == OrderSide.SHORT and _obv > 1.5:
            return None  # Accumulation — don't sell
        # Stochastic cross bonus
        if len(df) >= 2 and "stoch_k" in df.columns and "stoch_d" in df.columns:
            _prev_k = float(df.iloc[-2].get("stoch_k", 50))
            _stk_d = float(df.iloc[-1].get("stoch_d", 50))
            if side == OrderSide.LONG and _prev_k <= _stk_d and _stk > _stk_d:
                confs.append("Stoch bullish cross (+5)")
                score += 5
            elif side == OrderSide.SHORT and _prev_k >= _stk_d and _stk < _stk_d:
                confs.append("Stoch bearish cross (+5)")
                score += 5

        return _SetupResult(
            name="structure_bounce",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=target_level.price,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # STRUCTURE SCANNER 2: Liquidity Sweep
    # ==================================================================

    def _scan_liquidity_sweep(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Liquidity sweep: price raids equal highs/lows, fails, snaps back with displacement.

        Better than plain structure_bounce because it encodes:
        - Trapped breakout traders (stop run)
        - Reversal intent (reclaim + displacement)
        - Structural confluence (sweep into FVG/OB)
        """
        if len(df) < 30:
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

        lookback = min(50, len(df) - 2)
        window = df.iloc[-(lookback + 1):-1]  # exclude current bar

        # ── Step 1: Find equal highs/lows clusters ──
        # At least 2 touches within tolerance = liquidity pool
        # Relaxed tolerance: 0.25 ATR (was 0.15 — too tight for 1m candles)
        tolerance = atr * 0.25
        highs = window["high"].values
        lows = window["low"].values

        # Equal highs: cluster of 2+ bars with highs within tolerance
        eq_high_level = 0.0
        eq_high_count = 0
        for i in range(len(highs) - 1, max(-1, len(highs) - 30), -1):
            h = highs[i]
            touches = sum(1 for j in range(len(highs)) if j != i and abs(highs[j] - h) < tolerance)
            if touches >= 1 and h > eq_high_level:  # Relaxed: 1 touch = 2 bars
                eq_high_level = h
                eq_high_count = touches + 1
                break

        # Equal lows: cluster of 2+ bars with lows within tolerance
        eq_low_level = 0.0
        eq_low_count = 0
        for i in range(len(lows) - 1, max(-1, len(lows) - 30), -1):
            l_val = lows[i]
            touches = sum(1 for j in range(len(lows)) if j != i and abs(lows[j] - l_val) < tolerance)
            if touches >= 1 and (eq_low_level == 0 or l_val < eq_low_level):  # Relaxed
                eq_low_level = l_val
                eq_low_count = touches + 1
                break

        side = None
        confs = []
        score = 0
        sweep_level = 0.0

        # ── Step 2: Detect sweep ──
        # LONG: Price raids below equal lows, closes back above
        if eq_low_level > 0 and low < eq_low_level and close > eq_low_level:
            side = OrderSide.LONG
            sweep_level = eq_low_level
            sweep_size = (eq_low_level - low) / atr
            confs.append(f"Sweep below EQL ({eq_low_count} touches) at ${eq_low_level:.0f}")
            score += 38  # base 38 — sweep+reclaim is high-quality

            # Sweep depth scoring
            if sweep_size > 0.5:
                score += 15
                confs.append(f"Deep sweep ({sweep_size:.2f}x ATR)")
            elif sweep_size > 0.15:
                score += 10

            # Volume on reclaim candle (key confirmation)
            rel_vol = float(last.get("rel_vol", 1.0))
            if not np.isnan(rel_vol) and rel_vol > 1.3:
                score += 10
                confs.append(f"Volume reclaim {rel_vol:.1f}x")
            elif not np.isnan(rel_vol) and rel_vol > 1.0:
                score += 5

        # SHORT: Price raids above equal highs, closes back below
        if side is None and eq_high_level > 0 and high > eq_high_level and close < eq_high_level:
            side = OrderSide.SHORT
            sweep_level = eq_high_level
            sweep_size = (high - eq_high_level) / atr
            confs.append(f"Sweep above EQH ({eq_high_count} touches) at ${eq_high_level:.0f}")
            score += 38  # base 38

            if sweep_size > 0.5:
                score += 15
                confs.append(f"Deep sweep ({sweep_size:.2f}x ATR)")
            elif sweep_size > 0.15:
                score += 10

            # Volume on reclaim candle
            rel_vol = float(last.get("rel_vol", 1.0))
            if not np.isnan(rel_vol) and rel_vol > 1.3:
                score += 10
                confs.append(f"Volume reclaim {rel_vol:.1f}x")
            elif not np.isnan(rel_vol) and rel_vol > 1.0:
                score += 5

        # Fallback: rolling min/max sweep (simpler, more reliable)
        if side is None:
            # Use rolling 15-bar high/low as structure
            recent_window = df.iloc[-18:-3]
            if len(recent_window) >= 8:
                rolling_high = float(recent_window["high"].max())
                rolling_low = float(recent_window["low"].min())

                # Sweep below rolling low + reclaim
                if low < rolling_low and close > rolling_low:
                    side = OrderSide.LONG
                    sweep_level = rolling_low
                    confs.append(f"Sweep below rolling low {rolling_low:.2f}")
                    score += 35  # boosted from 28 — rolling sweep still valid
                # Sweep above rolling high + reclaim
                elif high > rolling_high and close < rolling_high:
                    side = OrderSide.SHORT
                    sweep_level = rolling_high
                    confs.append(f"Sweep above rolling high {rolling_high:.2f}")
                    score += 35  # boosted from 28

        if side is None:
            return None

        # ── Step 3: Reclaim strength — HARD GATE at body ≥ 55% ──
        # Weak-bodied reclaims are fakeouts. Data shows sub-55% body sweeps
        # drag WR down. Hard reject instead of soft penalty.
        body = abs(close - open_)
        candle_range = high - low if high > low else atr * 0.01
        body_ratio = body / candle_range
        if body_ratio < 0.55:
            return None  # hard gate: weak reclaim = not a real sweep

        if side == OrderSide.LONG:
            close_position = (close - low) / candle_range
        else:
            close_position = (high - close) / candle_range

        if close_position > 0.65:
            score += 15
            confs.append(f"Strong reclaim (body={body_ratio:.0%}, close_pos={close_position:.0%})")
        else:
            score += 8
            confs.append(f"Reclaim candle (body={body_ratio:.0%})")

        # Minimum sweep depth: 0.35 ATR
        if side == OrderSide.LONG:
            sweep_depth = (sweep_level - low) / atr if atr > 0 else 0
        else:
            sweep_depth = (high - sweep_level) / atr if atr > 0 else 0
        if sweep_depth < 0.35 and body_ratio < 0.55:
            score -= 8  # shallow sweep + weak reclaim = noise
            confs.append(f"Shallow sweep ({sweep_depth:.2f} ATR)")
        elif sweep_depth > 0.5:
            score += 5
            confs.append(f"Deep sweep ({sweep_depth:.2f} ATR)")

        # ── Step 4: Displacement check ──
        # Body must show real directional intent
        displacement = body / atr if atr > 0 else 0
        if displacement > 0.6:  # relaxed from 0.8
            score += 15
            confs.append(f"Strong displacement ({displacement:.1f}x ATR)")
        elif displacement > 0.4:
            score += 8
            confs.append(f"Moderate displacement ({displacement:.1f}x ATR)")
        elif displacement < 0.2:
            score -= 10  # weak reclaim, probably fake

        # ── Step 5: Volume confirmation ──
        rel_vol = float(last.get("rel_vol", 1.0))
        if not np.isnan(rel_vol) and rel_vol > 1.5:
            confs.append(f"Volume spike {rel_vol:.1f}x")
            score += 10
        elif not np.isnan(rel_vol) and rel_vol > 1.0:
            score += 5

        # ── Step 6: Sweep into structural zone ──
        sm = self._structure_map
        if sm is not None:
            # Check if sweep touched an order block
            if side == OrderSide.LONG:
                for ob in getattr(sm, 'demand_zones', []):
                    if isinstance(ob, dict) and low <= ob.get('high', 0) and low >= ob.get('low', float('inf')):
                        score += 10
                        confs.append("Sweep into demand zone/OB")
                        break
            else:
                for ob in getattr(sm, 'supply_zones', []):
                    if isinstance(ob, dict) and high >= ob.get('low', float('inf')) and high <= ob.get('high', 0):
                        score += 10
                        confs.append("Sweep into supply zone/OB")
                        break

        # ── Step 7: HTF alignment ──
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15
        elif htf_bias == (-1 if side == OrderSide.LONG else 1):
            score -= 5  # counter-trend penalty but don't block

        confidence = max(min(score, 100), 0)

        # SL below/above the sweep wick + small buffer
        if side == OrderSide.LONG:
            sl = low - atr * 0.15
        else:
            sl = high + atr * 0.15

        # ── Opposite liquidity pool as TP target ──
        # After sweeping lows, target the equal highs (and vice versa).
        # Clamp to 1R–4R range; fall back to default RR-based TPs if no pool
        # or if pool is outside the clamp range.
        risk = abs(close - sl)
        opp_pool_tp = 0.0
        if risk > 0:
            if side == OrderSide.LONG and eq_high_level > close:
                opp_dist_r = (eq_high_level - close) / risk
                if 1.0 <= opp_dist_r <= 4.0:
                    opp_pool_tp = eq_high_level
                    confs.append(f"TP→ EQH pool ${eq_high_level:.0f} ({opp_dist_r:.1f}R)")
            elif side == OrderSide.SHORT and eq_low_level > 0 and eq_low_level < close:
                opp_dist_r = (close - eq_low_level) / risk
                if 1.0 <= opp_dist_r <= 4.0:
                    opp_pool_tp = eq_low_level
                    confs.append(f"TP→ EQL pool ${eq_low_level:.0f} ({opp_dist_r:.1f}R)")

        result = _SetupResult(
            name="liquidity_sweep",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=close,
            stop_loss=sl,
            atr=atr,
        )
        # Attach opposite pool TP for downstream TP override
        result._opp_pool_tp = opp_pool_tp  # type: ignore[attr-defined]
        return result

    # ==================================================================
    # BOS / CHOCH + Displacement Scanner
    # ==================================================================

    def _scan_bos_choch(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """Break of Structure / Change of Character with displacement + confirmation.

        Backward-looking confirmation candle pattern:
          bar[-2]: the BREAK candle (closes beyond rolling high/low with displacement)
          bar[-1]: the CONFIRMATION candle (closes in direction, body ≥ 55%)
          bar[0] (current): entry candle (we enter at current close after confirmation)

        This prevents premature entries on noise/false breaks that previously
        produced 0% WR and triggered the hard_loss_cap at -2R.
        """
        if len(df) < 22:
            return None

        atr = float(df.iloc[-1].get("atr", 0))
        if atr <= 0 or np.isnan(atr):
            return None

        # bar[-2] = break candle, bar[-1] = confirmation candle, bar[0] = entry
        break_bar = df.iloc[-3]   # the candle that broke structure
        confirm_bar = df.iloc[-2]  # the follow-through candle
        entry_bar = df.iloc[-1]    # current candle (we enter here)

        # ── Step 1: Rolling structure levels ──
        # Use bars 5-22 (skip last 4 to avoid detecting break/confirm as structure)
        structure_window = df.iloc[-22:-4]
        if len(structure_window) < 8:
            return None

        rolling_high = float(structure_window["high"].max())
        rolling_low = float(structure_window["low"].min())

        # Pre-break bars must have been inside structure
        pre_break = df.iloc[-5:-3]
        pre_closes = [float(r["close"]) for _, r in pre_break.iterrows()]

        break_close = float(break_bar["close"])
        break_open = float(break_bar["open"])
        break_high = float(break_bar["high"])
        break_low = float(break_bar["low"])
        break_body = abs(break_close - break_open)

        side = None
        confs = []
        score = 0

        # ── Step 2: Detect break on bar[-2] ──
        # Bullish BOS: break_bar closes above rolling high, pre-break was below
        if break_close > rolling_high and min(pre_closes) <= rolling_high:
            side = OrderSide.LONG
            break_dist = (break_close - rolling_high) / atr
            confs.append(f"BOS above {rolling_high:.2f} ({break_dist:.2f}x ATR)")
            score += 25
            if break_dist > 0.5:
                score += 10

        # Bearish BOS: break_bar closes below rolling low, pre-break was above
        elif break_close < rolling_low and max(pre_closes) >= rolling_low:
            side = OrderSide.SHORT
            break_dist = (rolling_low - break_close) / atr
            confs.append(f"BOS below {rolling_low:.2f} ({break_dist:.2f}x ATR)")
            score += 25
            if break_dist > 0.5:
                score += 10

        if side is None:
            return None

        # ── Step 3: Displacement on break candle ──
        displacement = break_body / atr if atr > 0 else 0
        if displacement > 1.2:
            score += 20
            confs.append(f"Strong displacement ({displacement:.1f}x ATR)")
        elif displacement > 0.8:
            score += 12
            confs.append(f"Good displacement ({displacement:.1f}x ATR)")
        elif displacement > 0.6:
            score += 3
        else:
            return None  # below 0.6 ATR = noise break

        # ── Step 4: CONFIRMATION CANDLE on bar[-1] ──
        # Must close in the direction of the break with body ≥ 55%
        conf_close = float(confirm_bar["close"])
        conf_open = float(confirm_bar["open"])
        conf_high = float(confirm_bar["high"])
        conf_low = float(confirm_bar["low"])
        conf_body = abs(conf_close - conf_open)
        conf_range = conf_high - conf_low if conf_high > conf_low else atr * 0.01
        conf_body_ratio = conf_body / conf_range

        # Direction check: confirmation must close in break direction
        if side == OrderSide.LONG and conf_close <= conf_open:
            return None  # confirmation candle closed bearish — break not confirmed
        if side == OrderSide.SHORT and conf_close >= conf_open:
            return None  # confirmation candle closed bullish — break not confirmed

        # Body quality: ≥ 55% required
        if conf_body_ratio < 0.55:
            return None  # weak confirmation candle — not enough conviction

        # Confirmation must hold the break level
        if side == OrderSide.LONG and conf_close < rolling_high:
            return None  # closed back below structure — failed break
        if side == OrderSide.SHORT and conf_close > rolling_low:
            return None  # closed back above structure — failed break

        score += 15
        confs.append(f"Confirmed: follow-through candle ({conf_body_ratio:.0%} body)")

        # ── Step 5: Break candle quality ──
        break_range = break_high - break_low if break_high > break_low else atr * 0.01
        break_body_ratio = break_body / break_range
        if break_body_ratio > 0.55:
            score += 10
            confs.append(f"Clean break candle ({break_body_ratio:.0%} body)")
        elif break_body_ratio < 0.25:
            score -= 10

        # ── Step 6: Volume on break candle ──
        break_vol = float(break_bar.get("rel_vol", 1.0))
        if not np.isnan(break_vol) and break_vol > 2.0:
            confs.append(f"Volume spike {break_vol:.1f}x on break")
            score += 15
        elif not np.isnan(break_vol) and break_vol > 1.5:
            confs.append(f"Volume {break_vol:.1f}x confirmed")
            score += 8
        elif not np.isnan(break_vol) and break_vol < 1.3:
            return None  # breaks need ≥1.3x volume

        # ── Step 7: HTF alignment ──
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15
        elif htf_bias == (-1 if side == OrderSide.LONG else 1):
            score -= 5

        # ── Step 8: CHOCH detection (break against EMA trend) ──
        if "ema_21" in df.columns and "ema_8" in df.columns:
            ema8 = float(entry_bar.get("ema_8", 0))
            ema21 = float(entry_bar.get("ema_21", 0))
            if ema8 > 0 and ema21 > 0:
                if side == OrderSide.LONG and ema8 < ema21:
                    confs.append("CHOCH: bullish break in bearish EMA")
                    score += 10
                elif side == OrderSide.SHORT and ema8 > ema21:
                    confs.append("CHOCH: bearish break in bullish EMA")
                    score += 10

        confidence = max(min(score, 100), 0)

        # Entry at current bar close (after confirmation)
        entry_close = float(entry_bar["close"])
        entry_low = float(entry_bar["low"])
        entry_high = float(entry_bar["high"])

        # SL: behind the structure level + buffer
        if side == OrderSide.LONG:
            sl = min(entry_low, break_low, rolling_high) - atr * 0.3
        else:
            sl = max(entry_high, break_high, rolling_low) + atr * 0.3

        
        # --- MSS Confirmation Gate (Upgrade 3) ---
        # Confirming candle: body >= 55% of range, close in trade direction
        _mss_body = abs(close - open_)
        _mss_range = high - low if 'high' in dir() else float(last.get("high",0)) - float(last.get("low",0))
        if _mss_range > 0 and (_mss_body / _mss_range) < 0.55:
            return None  # Weak candle — not MSS confirmation
        if side == OrderSide.LONG and close <= open_:
            return None  # Must be bullish for long MSS
        if side == OrderSide.SHORT and close >= open_:
            return None  # Must be bearish for short MSS

        return _SetupResult(
            name="bos_choch",
            side=side,
            confidence=confidence,
            confirmations=confs,
            entry_price=entry_close,
            stop_loss=sl,
            atr=atr,
        )

    # ==================================================================
    # ==================================================================
    # CVD (Cumulative Volume Delta) Scanner
    # ==================================================================

    def _scan_cvd_divergence(
        self, symbol: str, df: pd.DataFrame, htf_bias: int, confirm_bias: int,
    ) -> Optional[_SetupResult]:
        """CVD Divergence: price vs volume delta discrepancy.

        Detects when price makes new high/low but volume delta disagrees:
        - Bearish CVD div: price higher high + volume declining = hidden selling
        - Bullish CVD div: price lower low + volume increasing = hidden buying

        This captures the "smart money" divergence that MACD/RSI miss.
        Uses volume × direction as proxy for CVD when real delta unavailable.
        """
        if len(df) < 15:
            return None

        last = df.iloc[-1]
        atr = float(last.get("atr", 0))
        if atr <= 0 or np.isnan(atr):
            return None

        close = float(last["close"])
        open_ = float(last["open"])
        high_val = float(last["high"])
        low_val = float(last["low"])
        volume = float(last.get("volume", 0))
        body = abs(close - open_)

        # Build CVD proxy: cumulative (volume × direction)
        # direction = +1 if close > open, -1 if close < open, 0 if doji
        lookback = min(15, len(df) - 1)
        window = df.iloc[-lookback:]
        cvd = 0.0
        cvd_values = []
        for _, bar in window.iterrows():
            bar_close = float(bar["close"])
            bar_open = float(bar["open"])
            bar_vol = float(bar.get("volume", 0))
            if bar_close > bar_open:
                cvd += bar_vol
            elif bar_close < bar_open:
                cvd -= bar_vol
            cvd_values.append(cvd)

        if len(cvd_values) < 10:
            return None

        # Price trend: compare first half vs second half
        half = len(window) // 2
        price_first = float(window.iloc[:half]["close"].mean())
        price_second = float(window.iloc[half:]["close"].mean())
        cvd_first = sum(cvd_values[:half]) / half
        cvd_second = sum(cvd_values[half:]) / (len(cvd_values) - half)

        # Rolling highs/lows
        recent_high = float(window["high"].max())
        recent_low = float(window["low"].min())
        recent_vol_avg = float(window["volume"].mean()) if "volume" in window.columns else 1

        side = None
        confs = []
        score = 0

        # Bearish CVD divergence: price trending up but CVD trending down
        # RELAXED: was 1.001/0.8, now 1.0005/0.9 — matches 6-month backtest edge
        price_up = price_second > price_first * 1.0005
        cvd_down = cvd_second < cvd_first * 0.9 if cvd_first > 0 else cvd_second < 0

        if price_up and cvd_down:
            # Price higher but CVD lower = hidden selling
            # Don't require current bar bearish — the divergence IS the signal
            side = OrderSide.SHORT
            div_strength = abs(cvd_first - cvd_second) / max(abs(cvd_first), 1)
            confs.append(f"CVD bearish divergence (strength={div_strength:.1f})")
            score += 35
            if div_strength > 1.5:
                score += 15
                confs.append("Strong volume-price disconnect")
            elif div_strength > 0.5:
                score += 8
            # Bonus if current bar confirms
            if close < open_:
                score += 5
                confs.append("Bearish confirmation candle")

        # Bullish CVD divergence: price trending down but CVD trending up
        price_down = price_second < price_first * 0.9995
        cvd_up = cvd_second > cvd_first * 1.1 if cvd_first > 0 else cvd_second > 0

        if side is None and price_down and cvd_up:
            # Price lower but CVD higher = hidden buying
            side = OrderSide.LONG
            div_strength = abs(cvd_second - cvd_first) / max(abs(cvd_first), 1)
            confs.append(f"CVD bullish divergence (strength={div_strength:.1f})")
            score += 35
            if div_strength > 1.5:
                score += 15
                confs.append("Strong volume-price disconnect")
            elif div_strength > 0.5:
                score += 8
            if close > open_:
                score += 5
                confs.append("Bullish confirmation candle")

        if side is None:
            return None

        # Candle quality
        candle_range = high_val - low_val if high_val > low_val else atr * 0.01
        body_ratio = body / candle_range
        if body_ratio > 0.5:
            score += 10
            confs.append(f"Clean reversal candle ({body_ratio:.0%} body)")

        # Volume confirmation
        rel_vol = volume / recent_vol_avg if recent_vol_avg > 0 else 1
        if rel_vol > 1.5:
            score += 10
            confs.append(f"Volume spike {rel_vol:.1f}x")
        elif rel_vol > 1.0:
            score += 5

        # HTF alignment
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            score += 15
            confs.append("HTF aligned")
        elif htf_bias == (-1 if side == OrderSide.LONG else 1):
            score -= 5

        # Price at extreme (near recent high for short, near recent low for long)
        if side == OrderSide.SHORT and high_val >= recent_high * 0.998:
            score += 10
            confs.append("At recent high — reversal zone")
        elif side == OrderSide.LONG and low_val <= recent_low * 1.002:
            score += 10
            confs.append("At recent low — reversal zone")

        confidence = max(min(score, 100), 0)

        # SL beyond the extreme
        if side == OrderSide.LONG:
            sl = recent_low - atr * 0.3
        else:
            sl = recent_high + atr * 0.3

        return _SetupResult(
            name="cvd_divergence",
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
        if body_ratio < 0.05:  # only skip absolute flat candles
            return None

        side = OrderSide.LONG if bullish else OrderSide.SHORT
        confs = ["Learning bias signal"]
        score = 40  # base score — enough for NEAR_MISS tier (35+) to fire in learning

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

        # Fallback: calculate VWAP bands from dataframe if structure_map unavailable
        vwap_val = 0.0
        vwap_upper = 0.0
        vwap_lower = 0.0
        if sm is not None and sm.vwap > 0:
            vwap_val = sm.vwap
            vwap_upper = getattr(sm, 'vwap_upper_1', 0)
            vwap_lower = getattr(sm, 'vwap_lower_1', 0)

        if vwap_val <= 0 and "vwap" in df.columns:
            vwap_val = float(df.iloc[-1].get("vwap", 0))

        if vwap_val <= 0:
            return None

        # Calculate bands from ATR if not available
        last_atr = float(df.iloc[-1].get("atr", 0))
        if vwap_upper <= 0 and last_atr > 0:
            vwap_upper = vwap_val + last_atr * 1.5
        if vwap_lower <= 0 and last_atr > 0:
            vwap_lower = vwap_val - last_atr * 1.5

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
        if close <= vwap_lower and vwap_lower > 0:
            lower_wick = min(open_, close) - low
            if close > open_ and lower_wick > body * 0.3:  # relaxed from 0.5
                side = OrderSide.LONG
                confs.append(f"VWAP lower band touch (VWAP=${vwap_val:.0f})")
                score += 35  # raised from 30 — scanner needs higher base to clear threshold

                vwap_lower_2 = getattr(sm, 'vwap_lower_2', vwap_val - last_atr * 2.5) if sm else vwap_val - last_atr * 2.5
                if close <= vwap_lower_2 and vwap_lower_2 > 0:
                    confs.append("Below 2nd std dev — extreme")
                    score += 10

        # SHORT: Price at/above VWAP upper band + bearish reversal
        if side is None and close >= vwap_upper and vwap_upper > 0:
            upper_wick = high - max(open_, close)
            if close < open_ and upper_wick > body * 0.3:  # relaxed from 0.5
                side = OrderSide.SHORT
                confs.append(f"VWAP upper band touch (VWAP=${vwap_val:.0f})")
                score += 35  # raised from 30 — scanner needs higher base to clear threshold

                vwap_upper_2 = getattr(sm, 'vwap_upper_2', vwap_val + last_atr * 2.5) if sm else vwap_val + last_atr * 2.5
                if close >= vwap_upper_2 and vwap_upper_2 > 0:
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
        if side == OrderSide.LONG and rsi < 40:
            confs.append(f"RSI oversold ({rsi:.0f})")
            score += 10
        elif side == OrderSide.SHORT and rsi > 60:
            confs.append(f"RSI overbought ({rsi:.0f})")
            score += 10

        # HTF alignment
        if htf_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("HTF aligned")
            score += 15

        # P3: Additional confirmations to raise score ceiling
        # MACD agreement
        macd_hist = float(last.get("macd_hist", 0))
        if not np.isnan(macd_hist):
            if (side == OrderSide.LONG and macd_hist > 0) or (side == OrderSide.SHORT and macd_hist < 0):
                confs.append("MACD aligned")
                score += 10

        # Candle body quality (strong reversal candle)
        body_ratio = body / full_range if full_range > 0 else 0
        if body_ratio > 0.55:
            confs.append("Strong reversal candle")
            score += 8

        # 5m confirmation bias
        if confirm_bias == (1 if side == OrderSide.LONG else -1):
            confs.append("5m aligned")
            score += 5

        # Supertrend agreement
        st_dir = float(last.get("supertrend_dir", 0))
        if (side == OrderSide.LONG and st_dir == 1) or (side == OrderSide.SHORT and st_dir == -1):
            confs.append("Supertrend agrees")
            score += 5

        confidence = min(score, 100)

        # SL beyond VWAP 2nd std dev band (P3 fix: guard against sm=None)
        vwap_lower_2 = getattr(sm, 'vwap_lower_2', 0) if sm else 0
        vwap_upper_2 = getattr(sm, 'vwap_upper_2', 0) if sm else 0
        if side == OrderSide.LONG:
            sl = vwap_lower_2 - close * 0.001 if vwap_lower_2 > 0 else close - atr * 2
        else:
            sl = vwap_upper_2 + close * 0.001 if vwap_upper_2 > 0 else close + atr * 2

        
        # --- Stochastic + OBV Filter (Upgrade 6) ---
        _stk = float(df.iloc[-1].get("stoch_k", 50)) if "stoch_k" in df.columns else 50.0
        _obv = float(df.iloc[-1].get("obv_slope", 0)) if "obv_slope" in df.columns else 0.0
        if side == OrderSide.LONG and _stk > 80:
            return None  # Don't buy at overbought
        if side == OrderSide.SHORT and _stk < 20:
            return None  # Don't sell at oversold
        if side == OrderSide.LONG and _obv < -1.5:
            return None  # Distribution — don't buy
        if side == OrderSide.SHORT and _obv > 1.5:
            return None  # Accumulation — don't sell
        # Stochastic cross bonus
        if len(df) >= 2 and "stoch_k" in df.columns and "stoch_d" in df.columns:
            _prev_k = float(df.iloc[-2].get("stoch_k", 50))
            _stk_d = float(df.iloc[-1].get("stoch_d", 50))
            if side == OrderSide.LONG and _prev_k <= _stk_d and _stk > _stk_d:
                confs.append("Stoch bullish cross (+5)")
                score += 5
            elif side == OrderSide.SHORT and _prev_k >= _stk_d and _stk < _stk_d:
                confs.append("Stoch bearish cross (+5)")
                score += 5

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
        # STEP 1: COMPUTE VOLATILITY-BASED SL (per-scanner ATR mult)
        # ══════════════════════════════════════════════════════
        atr_for_sl = self._confirm_atr if self._confirm_atr > 0 else setup.atr
        # Per-scanner SL ATR multiplier (defaults to global self.sl_atr_mult)
        scanner_exits = getattr(self, '_active_scanner_exits', {})
        sl_mult_base = scanner_exits.get("sl_atr", self.sl_atr_mult)
        sl_mult = sl_mult_base * getattr(self, '_sl_adjust', 1.0)  # self-optimize adjustment
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

        # SB-only mode: cap leverage further
        if self.structure_bounce_only and setup.name == "structure_bounce":
            leverage = min(leverage, self._sb_only_lev_cap)  # 18x max

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
        # Per-scanner TP ratios (fall back to global defaults)
        tp1_rr = scanner_exits.get("tp1_rr", self.tp1_rr)
        tp2_rr = scanner_exits.get("tp2_rr", self.tp2_rr)
        tp3_rr = scanner_exits.get("tp3_rr", self.tp3_rr)

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

        # Liquidity sweep: use opposite pool as TP1 if available and within range
        opp_pool_tp = getattr(setup, "_opp_pool_tp", 0.0)
        if setup.name == "liquidity_sweep" and opp_pool_tp > 0 and risk > 0:
            tp1_rr = abs(opp_pool_tp - entry) / risk
            tp1_rr = max(1.0, min(tp1_rr, 4.0))  # clamp 1R–4R

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

        # ── Enforce TP1 minimum — SB-only mode uses stricter threshold ──
        effective_min_tp1 = self._sb_only_min_tp1_pct if (self.structure_bounce_only and setup.name == "structure_bounce") else self.min_tp1_pct
        min_tp1_distance = entry * effective_min_tp1 / 100
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
            regime=regime if regime else MarketRegime.SIDEWAYS,  # Fix: empty string is falsy, use explicit check
            metadata={
                "setup_type": setup.name,
                "scanner_category": SCANNER_CATEGORY.get(setup.name, "unknown"),
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
                "indian_market": getattr(self, '_indian_ctx', None) and getattr(self, '_indian_ctx').session_label or "",
                "indian_flow_hour": getattr(self, '_indian_ctx', None) and getattr(self, '_indian_ctx').is_indian_flow_hour or False,
                "vwap_zone": getattr(self, '_prefilter_result', {}).get("context", {}).get("vwap_zone", "normal"),
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

    def get_ml_stats(self) -> Dict[str, Any]:
        """Return ML scoring stats for dashboard display."""
        return {
            "scorer": self._ml_scorer.get_stats(),
            "shadow_mode": self._ml_shadow_mode,
            "thresholds": self._ml_thresholds,
            "last_results": dict(self._last_ml_result),
            "scanner_sl_tp": self._scanner_sl_tp,
        }

    # ==================================================================
    # Per-symbol cooling: triggered by orchestrator on trade close
    # ==================================================================

    def notify_trade_close(self, symbol: str, pnl_usd: float) -> None:
        """Update per-symbol consecutive loss counter.
        If 3 consecutive losses on same symbol, cool off for 30 minutes.
        """
        key = f"_consec_losses_{symbol}"
        cool_key = f"_cool_until_{symbol}"
        if pnl_usd <= 0:
            current = getattr(self, key, 0) + 1
            setattr(self, key, current)
            if current >= 3:
                import time as _t
                setattr(self, cool_key, _t.time() + 1800)  # 30 min cooldown
                setattr(self, key, 0)  # reset counter
                logger.warning(
                    "SYMBOL COOLING: %s — 3 consecutive losses, pausing for 30 minutes", symbol
                )
        else:
            setattr(self, key, 0)  # reset on win

    # ==================================================================
    # Setup Lifecycle — expose forming/near-trigger setups to dashboard
    # ==================================================================

    def get_setup_lifecycle(self) -> Dict[str, Any]:
        """Return current forming setups from last scan status."""
        candidates = []
        for symbol, status in self.last_scan_status.items():
            if not isinstance(status, dict):
                continue
            reason = status.get("reason", "")
            signal = status.get("signal", False)
            indicators = status.get("indicators", {})
            funnel = status.get("funnel", {})

            # Get regime and scanner info
            regime = indicators.get("regime", "unknown")
            atr_ratio = indicators.get("atr_ratio", 0)

            # Build candidate info
            candidate = {
                "symbol": symbol,
                "regime": regime,
                "atr_ratio": round(atr_ratio, 2) if atr_ratio else 0,
                "signal": signal,
                "reason": reason[:80] if reason else "scanning",
                "time": status.get("time", ""),
                "scanners_checked": funnel.get("scanners_checked", 0),
                "triggered": funnel.get("triggered", 0),
                "vetoed": funnel.get("vetoed", 0),
                "blocked_regime": funnel.get("blocked_regime", 0),
            }
            candidates.append(candidate)

        return {"candidates": candidates}
