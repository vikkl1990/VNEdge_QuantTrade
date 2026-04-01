"""
Signal Tracker — monitors open signals for TP/SL closure and records P&L.

Each signal is tracked from entry until either:
  - Stop Loss is hit  → LOSS
  - TP1 hit           → partial WIN (book TP1, trail rest)
  - TP2 hit           → WIN
  - TP3 hit           → FULL WIN
  - Timeout (4 hours) → close at market price

Persists all active + closed signals to disk for dashboard stats.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
_ACTIVE_FILE = _STORAGE_DIR / "active_signals.json"
_CLOSED_FILE = _STORAGE_DIR / "closed_signals.json"
_STATS_FILE = _STORAGE_DIR / "signal_stats.json"

# Max age before auto-closing a signal (seconds)
# Scalper offer: BTC 30 min, others 15 min (free closing fee within window)
MAX_SIGNAL_AGE = 4 * 3600  # 4 hours hard backstop
SCALPER_WINDOW_BTC = 14 * 60   # 14 minutes — BTC Scalper (halved from 27)
SCALPER_WINDOW_OTHER = 6 * 60  # 6 minutes — ETH/AVAX/others (halved from 12)

# ══════════════════════════════════════════════════════════════
# TRADE TYPE CLASSIFICATION — 3 tiers with different exit logic
# ══════════════════════════════════════════════════════════════
TRADE_TYPE_SCALP = "SCALP"         # Fast in/out, tight SL/TP, hard time stop
TRADE_TYPE_INTRADAY = "INTRADAY"   # Directional move, moderate SL/TP, soft time stop
TRADE_TYPE_RUNNER = "RUNNER"        # High conviction trend, wide SL/TP, no time stop

# Per-type exit parameters
TRADE_TYPE_CONFIG = {
    TRADE_TYPE_SCALP: {
        "sl_atr_mult": 0.8,       # tighter SL (was 0.9 — P4: fewer wide SL hits)
        "tp1_rr": 0.8,            # quick TP1
        "tp2_rr": 1.2,            # small TP2
        "tp3_rr": 0.0,            # NO TP3 for scalps
        "time_stop_bars": 3,      # 3 bars — kill dead scalps fast (REVERTED from 5)
        "time_stop_type": "hard", # kill if not moving
        "early_kill_sec": 60,     # 60s early kill — must be tighter than INTRADAY (90s)
        "early_kill_mfe": 0.10,   # need to show life quickly (REVERTED from 0.12)
        "trail_atr_mult": 0.6,   # tight trail for scalps (REVERTED from 0.8)
        "max_age_sec": 15 * 60,   # 15 min max (REVERTED from 20)
    },
    TRADE_TYPE_INTRADAY: {
        "sl_atr_mult": 1.0,       # tighter SL (was 1.15 — P4 fix: 8 SL hits = -7.48%)
        "tp1_rr": 1.2,            # TP1 at 1.2R
        "tp2_rr": 2.0,            # TP2 at 2R
        "tp3_rr": 3.0,            # small TP3
        "time_stop_bars": 4,      # 4 bars (was 6 — P1 fix: 24 time_stop_intraday = 0% WR)
        "time_stop_type": "soft", # only exit if losing AND no progress
        "early_kill_sec": 90,     # 90s — kill dead INTRADAY faster (was 120)
        "early_kill_mfe": 0.08,   # raised from 0.05 — P1 fix: need stronger momentum signal
        "trail_atr_mult": 1.0,   # standard trail
        "max_age_sec": 30 * 60,   # 30 min max (was 45 — P1 fix: less time to bleed)
    },
    TRADE_TYPE_RUNNER: {
        "sl_atr_mult": 1.5,       # wide SL — give room
        "tp1_rr": 1.5,            # TP1 at 1.5R
        "tp2_rr": 3.0,            # TP2 at 3R
        "tp3_rr": 5.0,            # TP3 at 5R — let it run
        "time_stop_bars": 0,      # NO time stop
        "time_stop_type": "none", # only exit on structure/trailing
        "early_kill_sec": 0,      # no early kill
        "early_kill_mfe": 0.0,    # disabled
        "trail_atr_mult": 1.5,   # standard wide trail (REVERTED from 2.5)
        "max_age_sec": 8 * 3600,  # 8 hours max (REVERTED from 12)
    },
}


def classify_trade(signal_dict: dict) -> str:
    """Classify a trade into SCALP / INTRADAY / RUNNER before execution.

    Primary classifier: ML probability
    Context boosters: trend_strength, vwap_distance, atr_ratio, regime, HTF alignment
    """
    meta = signal_dict.get("metadata", {})

    # Primary: ML probability
    ml_prob = float(meta.get("ml_probability", 0.5))

    # Context factors
    regime = str(meta.get("regime", "")).lower()
    htf_bias = int(meta.get("htf_bias", 0))
    side = signal_dict.get("side", "")
    if hasattr(side, 'value'):
        side = side.value
    atr = float(meta.get("atr", 0))
    vwap_zone = meta.get("vwap_zone", "clear")

    # HTF alignment check
    htf_aligned = (
        (htf_bias > 0 and side == "long") or
        (htf_bias < 0 and side == "short")
    )

    # Trend regime check
    is_trending = regime in ("trending_up", "trending_down", "breakout")
    is_ranging = regime in ("ranging", "sideways", "quiet")

    # ── Base classification from ML probability ──
    if ml_prob >= 0.65:
        trade_type = TRADE_TYPE_RUNNER
    elif ml_prob >= 0.50:
        trade_type = TRADE_TYPE_INTRADAY
    else:
        trade_type = TRADE_TYPE_SCALP

    # ── Context boosters: upgrade/downgrade ──

    # UPGRADE to RUNNER: strong trend + HTF aligned + away from VWAP
    if trade_type == TRADE_TYPE_INTRADAY and is_trending and htf_aligned and vwap_zone == "clear":
        trade_type = TRADE_TYPE_RUNNER
        logger.info("Trade type UPGRADE → RUNNER: trending + HTF aligned + clear VWAP")

    # UPGRADE to INTRADAY: moderate probability but trending with HTF
    if trade_type == TRADE_TYPE_SCALP and is_trending and htf_aligned:
        trade_type = TRADE_TYPE_INTRADAY
        logger.info("Trade type UPGRADE → INTRADAY: trending + HTF aligned")

    # DOWNGRADE to SCALP: ranging regime + near VWAP noise
    if trade_type == TRADE_TYPE_INTRADAY and is_ranging and vwap_zone == "noise":
        trade_type = TRADE_TYPE_SCALP
        logger.info("Trade type DOWNGRADE → SCALP: ranging + VWAP noise zone")

    # DOWNGRADE to INTRADAY: runner in ranging regime
    if trade_type == TRADE_TYPE_RUNNER and is_ranging:
        trade_type = TRADE_TYPE_INTRADAY
        logger.info("Trade type DOWNGRADE → INTRADAY: RUNNER not valid in ranging regime")

    # High confidence override: 95+ confidence always eligible for INTRADAY minimum
    confidence = int(signal_dict.get("confidence", 0))
    if confidence >= 95 and trade_type == TRADE_TYPE_SCALP:
        trade_type = TRADE_TYPE_INTRADAY

    logger.info(
        "TRADE TYPE: %s %s → %s | ml_prob=%.2f regime=%s htf_aligned=%s vwap=%s conf=%d",
        signal_dict.get("symbol", ""), side, trade_type,
        ml_prob, regime, htf_aligned, vwap_zone, confidence,
    )

    return trade_type


@dataclass
class TrackedSignal:
    """A signal being monitored for TP/SL hits."""

    trade_id: str
    symbol: str
    side: str  # "long" or "short"
    entry_price: float
    stop_loss: float
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    confidence: int = 0
    grade: str = ""
    setup_type: str = ""
    strategy_type: str = "scalp"  # "scalp" or "investment"
    trade_type: str = "SCALP"      # SCALP / INTRADAY / RUNNER (classified before execution)
    reason: str = ""

    # Paper trading: position sizing (fixed fractional risk model)
    paper_stake: float = 25.0  # base stake (used as fallback)
    leverage: int = 1          # effective leverage (derived from risk model)
    position_size_usd: float = 0.0  # actual position size in USD
    risk_amount_usd: float = 0.0    # dollars risked on this trade (account × 0.75%)
    pnl_usd: float = 0.0      # dollar P&L (net, after fees)

    # Contract sizing (actual exchange contract specs)
    contract_size: float = 0.0    # size of 1 contract in base currency (BTC=0.001, ETH=0.01)
    contracts: int = 0            # number of contracts
    quantity: float = 0.0         # total base currency qty (contracts * contract_size)

    # Leverage audit
    leverage_cap_source: str = ""  # why this leverage was chosen

    # Fee tracking (gross/net split)
    gross_pnl_pct: float = 0.0   # PnL before fees
    gross_pnl_usd: float = 0.0   # Dollar PnL before fees
    total_fees_pct: float = 0.0   # Total fees as % of position
    total_fees_usd: float = 0.0   # Total fees in dollars
    fee_type: str = ""           # "scalper" (0.08%) or "standard" (0.18%)
    within_scalper: bool = False  # did trade close within Scalper window?
    trade_duration_sec: float = 0.0  # actual trade duration in seconds
    scalper_window_sec: float = 0.0  # applicable Scalper window

    # ATR for trailing stop (passed from strategy)
    signal_atr: float = 0.0           # ATR value at signal time (for ATR trail)

    # ATR trailing stop state (active after TP2)
    atr_trail_active: bool = False     # is ATR trailing stop engaged?
    atr_trail_price: float = 0.0       # current ATR trail stop price

    # Analytics fields (Patch 7)
    stop_overshoot_pct: float = 0.0   # how far past SL we actually exited
    tp1_distance_r: float = 0.0       # TP1 distance in R units
    near_tp_triggered: bool = False    # did near-TP protection fire?
    time_stop_triggered: bool = False  # did dead-trade time stop fire?
    exit_reason_detailed: str = ""     # detailed exit reason tag

    # R-multiple metrics
    initial_risk: float = 0.0         # |entry - stop_loss| at entry (the "1R")
    exit_r: float = 0.0              # final P&L in R-multiples
    mae_r: float = 0.0              # Max Adverse Excursion in R (worst drawdown)
    mfe_r: float = 0.0              # Max Favorable Excursion in R (best unrealized)

    # Smart exit tracking
    peak_mfe_r: float = 0.0           # highest MFE reached (for MFE memory trail)
    last_mfe_update_time: float = 0.0 # timestamp of last MFE new high
    mfe_stale_seconds: float = 0.0    # seconds since last MFE improvement
    partial_exit_done: bool = False    # whether 0.3R partial exit was taken
    momentum_decay_count: int = 0     # consecutive candles with shrinking body

    # Slippage tracking
    signal_price: float = 0.0         # price at signal generation (before execution)
    fill_price: float = 0.0           # actual fill price from exchange
    slippage_ticks: float = 0.0       # (fill - signal) / tick_size
    slippage_bps: float = 0.0         # slippage in basis points
    slippage_impact_r: float = 0.0    # slippage in R units
    order_type: str = "market"        # "market" or "limit"
    fill_time_ms: float = 0.0         # time from signal to fill

    # Tracking state
    status: str = "active"  # active, tp1_hit, tp2_hit, tp3_hit, stopped, expired
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    sl_hit: bool = False
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0

    # Profit protection state
    breakeven_set: bool = False          # early breakeven at +0.5R triggered?
    tp1_pnl_locked: float = 0.0         # PnL% locked when TP1 partial close fires
    tp2_pnl_locked: float = 0.0         # PnL% locked when TP2 partial close fires
    position_remaining_pct: float = 1.0  # fraction of position still open (1.0 → 0.40 → 0.15)

    # Signal metadata (ML scores, scanner config, etc.)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Timestamps
    entry_time: str = ""
    tp1_time: str = ""
    tp2_time: str = ""
    tp3_time: str = ""
    exit_time: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TrackedSignal":
        # Only pass known fields
        known = {f.name for f in cls.__dataclass_fields__.values()}
        ts = cls(**{k: v for k, v in d.items() if k in known})
        # Backfill contract sizing for signals created before this feature
        if ts.contracts == 0 and ts.entry_price > 0 and ts.position_size_usd > 0:
            sym = ts.symbol.upper()
            if "BTC" in sym:
                cs = 0.001
            elif "ETH" in sym:
                cs = 0.01
            elif "SOL" in sym or "AVAX" in sym:
                cs = 0.1
            elif "DOGE" in sym:
                cs = 1.0
            else:
                cs = 0.001
            raw = ts.position_size_usd / (ts.entry_price * cs)
            ts.contract_size = cs
            ts.contracts = max(1, int(raw))
            ts.quantity = round(ts.contracts * cs, 6)
        # Backfill initial_risk for signals created before R-tracking
        if ts.initial_risk == 0 and ts.entry_price > 0 and ts.stop_loss > 0:
            ts.initial_risk = abs(ts.entry_price - ts.stop_loss)
        return ts

    @classmethod
    def from_signal(cls, sig: Dict[str, Any]) -> "TrackedSignal":
        """Create a TrackedSignal from a signal dict.

        Fixed Fractional Risk Model (Phase 2):
        - Risk exactly 0.75% of account per trade
        - Position size = risk_amount / SL_distance_pct
        - Leverage is DERIVED (not input): lev = position_size / stake
        - Max leverage capped by confidence grade for safety
        """
        tps = sig.get("take_profits", [])
        meta = sig.get("metadata", {})
        confidence = int(sig.get("confidence", 0))
        entry = float(sig.get("entry_price", 0))
        sl = float(sig.get("stop_loss", 0))

        # Calculate SL distance as percentage
        sl_dist_pct = abs(entry - sl) / entry * 100 if entry > 0 else 1.0
        grade = str(sig.get("grade", "C"))

        # ── FIXED FRACTIONAL RISK MODEL ──
        # Risk 0.75% of account per trade (constant dollar risk)
        # This automatically sizes positions based on SL distance
        ACCOUNT_SIZE = 1000.0  # paper account base
        MAX_MARGIN_PER_TRADE = 100.0  # max $100 margin (stake) per trade
        RISK_PCT = 0.75        # risk 0.75% per trade
        risk_amount = ACCOUNT_SIZE * RISK_PCT / 100  # $7.50 risk per trade

        # Position size = risk / SL_distance
        # If SL is 0.5% away, position = $7.50 / 0.005 = $1500
        # If SL is 1.0% away, position = $7.50 / 0.01 = $750
        if sl_dist_pct > 0:
            position_usd = risk_amount / (sl_dist_pct / 100)
        else:
            position_usd = risk_amount * 100  # fallback

        # ── SUPER SCALP LEVERAGE (10x-50x, $100-$200 margin) ──
        # Minimum $200 position to survive fee drag. Fewer but larger trades.
        # Liquidation safety checked separately in strategy
        if confidence >= 90:
            max_lev = 75
            paper_stake = 100.0
            lev_cap_source = "super_scalp_90+_75x"
        elif confidence >= 80:
            max_lev = 60
            paper_stake = 100.0
            lev_cap_source = "super_scalp_80+_60x"
        elif confidence >= 70:
            max_lev = 45
            paper_stake = 80.0
            lev_cap_source = "super_scalp_70+_45x"
        elif confidence >= 60:
            max_lev = 30
            paper_stake = 60.0
            lev_cap_source = "super_scalp_60+_30x"
        else:
            max_lev = 20
            paper_stake = 50.0
            lev_cap_source = "super_scalp_base_20x"

        # Derive effective leverage from position size
        derived_lev = position_usd / paper_stake
        lev = min(int(derived_lev), max_lev)
        lev = max(1, lev)  # minimum 1x

        if derived_lev > max_lev:
            # Position was too large — cap it
            position_usd = paper_stake * max_lev
            risk_amount = position_usd * sl_dist_pct / 100
            lev_cap_source = f"lev_capped_{max_lev}x"

        # ── DEMO MIN LEVERAGE FLOOR: 20x minimum ──
        # In demo/paper mode, enforce minimum 20x leverage for realistic testing
        MIN_DEMO_LEV = 20
        if lev < MIN_DEMO_LEV:
            lev = MIN_DEMO_LEV
            position_usd = paper_stake * lev
            risk_amount = position_usd * sl_dist_pct / 100
            lev_cap_source = f"demo_min_floor_{MIN_DEMO_LEV}x"
            logger.info(
                "DEMO LEV FLOOR: %s derived=%dx < %dx min → lev=%dx pos=$%.0f",
                sig.get("symbol", ""), int(derived_lev), MIN_DEMO_LEV, lev, position_usd,
            )

        # ── HARD MARGIN CAP: max $100 margin per trade ──
        margin_used = position_usd / max(lev, 1)
        if margin_used > MAX_MARGIN_PER_TRADE:
            position_usd = MAX_MARGIN_PER_TRADE * lev
            risk_amount = position_usd * sl_dist_pct / 100
            lev_cap_source = f"margin_capped_{int(MAX_MARGIN_PER_TRADE)}"
            logger.info(
                "MARGIN CAP: %s margin=$%.0f > $%d max → pos=$%.0f @ %dx",
                sig.get("symbol", ""), margin_used, int(MAX_MARGIN_PER_TRADE),
                position_usd, lev,
            )

        # ── Regime-based position sizing ──
        regime_size_mult = float(meta.get("regime_size_mult", 1.0))
        confidence_size_mult = float(meta.get("confidence_size_mult", 1.0))
        combined_size_mult = regime_size_mult * confidence_size_mult
        if combined_size_mult != 1.0:
            position_usd *= combined_size_mult
            risk_amount *= combined_size_mult
            logger.info(
                "Regime sizing: regime=%.1fx conf=%.1fx combined=%.2fx → pos=$%.0f",
                regime_size_mult, confidence_size_mult, combined_size_mult, position_usd,
            )

        # ── Graduated drawdown defense ──
        dd_pct = sig.get("_dd_pct", 0.0)
        if dd_pct >= 6.0:
            risk_amount *= 0.5
            position_usd *= 0.5
            lev = max(1, lev // 2)
            logger.info("DD defense L3: risk halved (DD=%.1f%%)", dd_pct)
        if dd_pct >= 2.0:
            prev_lev = lev
            lev = min(lev, 3)
            position_usd = min(position_usd, paper_stake * 3)
            if lev < prev_lev:
                lev_cap_source = f"dd_defense_{dd_pct:.1f}%"
            logger.info("DD defense L1: leverage capped at 3x (DD=%.1f%%)", dd_pct)

        logger.info(
            "Risk Model: %s %s | conf=%d grade=%s | risk=$%.2f | sl_dist=%.3f%% | "
            "pos=$%.0f | lev=%dx | cap=%s",
            sig.get("symbol", ""), sig.get("side", ""), confidence, grade,
            risk_amount, sl_dist_pct, position_usd, lev, lev_cap_source,
        )

        # Calculate actual contract sizing (Delta India contract specs)
        symbol = sig.get("symbol", "")
        sym_upper = symbol.upper()
        if "BTC" in sym_upper:
            contract_sz = 0.001   # 1 contract = 0.001 BTC
        elif "ETH" in sym_upper:
            contract_sz = 0.01    # 1 contract = 0.01 ETH
        elif "SOL" in sym_upper:
            contract_sz = 0.1     # 1 contract = 0.1 SOL
        elif "AVAX" in sym_upper:
            contract_sz = 0.1     # 1 contract = 0.1 AVAX
        elif "DOGE" in sym_upper:
            contract_sz = 1.0     # 1 contract = 1 DOGE
        else:
            contract_sz = 0.001   # default

        # contracts = position_usd / (entry_price * contract_size)
        if entry > 0 and contract_sz > 0:
            raw_contracts = position_usd / (entry * contract_sz)
            num_contracts = max(1, int(raw_contracts))  # min 1 contract, round down
            quantity = num_contracts * contract_sz
            # Recalculate actual position_usd based on rounded contracts
            position_usd = round(quantity * entry, 2)
        else:
            num_contracts = 0
            quantity = 0.0

        # Extract ATR from signal metadata for trailing stop
        signal_atr = float(meta.get("atr", 0))

        # ── MINIMUM POSITION SIZE ENFORCEMENT ──
        # Positions below $50 have fee ratios too high for any edge to survive
        MIN_POSITION_USD = 50.0
        if position_usd < MIN_POSITION_USD:
            logger.warning(
                "FEE DEATH: %s %s pos=$%.0f < $%d min — fee ratio too high, blocking trade",
                sig.get("symbol", ""), sig.get("side", ""), position_usd, int(MIN_POSITION_USD),
            )
            # Bump position to minimum viable size
            position_usd = MIN_POSITION_USD
            if entry > 0 and contract_sz > 0:
                raw_contracts = position_usd / (entry * contract_sz)
                num_contracts = max(1, int(raw_contracts))
                quantity = num_contracts * contract_sz
                position_usd = round(quantity * entry, 2)
            risk_amount = position_usd * sl_dist_pct / 100
            # Recalculate leverage
            if paper_stake > 0:
                lev = min(int(position_usd / paper_stake), max_lev)
                lev = max(1, lev)
            lev_cap_source = f"min_position_{int(MIN_POSITION_USD)}"

        # ── FINAL LEVERAGE SAFETY CAP (after all adjustments) ──
        if paper_stake > 0:
            actual_lev = position_usd / paper_stake
            if actual_lev > max_lev:
                position_usd = paper_stake * max_lev
                lev = max_lev
                if entry > 0 and contract_sz > 0:
                    raw_contracts = position_usd / (entry * contract_sz)
                    num_contracts = max(1, int(raw_contracts))
                    quantity = num_contracts * contract_sz
                    position_usd = round(quantity * entry, 2)
                risk_amount = position_usd * sl_dist_pct / 100
                lev_cap_source = f"final_safety_cap_{max_lev}x"

        # ── FEE VIABILITY CHECK ──
        # Use realistic fee assumption: SCALP trades get scalper window rates,
        # INTRADAY/RUNNER get standard rates (they typically exceed the window).
        # This prevents 173 outside-scalper trades averaging only $0.32/trade.
        pre_trade_type = classify_trade(sig)
        within_scalper = pre_trade_type == TRADE_TYPE_SCALP
        # Fee check needs SignalTracker instance — defer to track_signal if in classmethod
        _order_type = sig.get("_order_type", "maker")
        fee_check = SignalTracker.get_min_viable_move(
            symbol=sig.get("symbol", ""),
            position_usd=position_usd,
            leverage=float(lev),
            sl_distance_pct=sl_dist_pct,
            within_scalper=within_scalper,
            order_type=_order_type,
        )

        if fee_check["fee_drag_r"] > 0.5:
            # Fees > 50% of risk = negative EV by definition — hard block
            logger.warning(
                "FEE BLOCK: %s %s | fee_drag=%.2fR (>0.5) | min_move=%.3f%% | "
                "pos=$%.0f sl=%.3f%% — fees consume >50%% of risk, trade blocked",
                sig.get("symbol", ""), sig.get("side", ""),
                fee_check["fee_drag_r"], fee_check["min_move_pct"],
                position_usd, sl_dist_pct,
            )
            # Return a signal with confidence=0 to signal rejection upstream
            confidence = 0
        elif not fee_check["viable"]:
            # fee_drag > 0.3 but <= 0.5: HARD BLOCK (D11 fix)
            # Previously only applied a -10 penalty, but 23% of trades still executed
            # at negative expected value. Blocking entirely saves ~$249/500 trades.
            logger.warning(
                "FEE BLOCK: %s %s | fee_drag=%.2fR (>0.3) | min_move=%.3f%% | "
                "pos=$%.0f sl=%.3f%% — fee_viable=False, trade blocked",
                sig.get("symbol", ""), sig.get("side", ""),
                fee_check["fee_drag_r"], fee_check["min_move_pct"],
                position_usd, sl_dist_pct,
            )
            confidence = 0

        # Store fee analysis in metadata
        meta["fee_drag_r"] = fee_check["fee_drag_r"]
        meta["fee_viable"] = fee_check["viable"]
        meta["min_move_pct"] = fee_check["min_move_pct"]

        # ── CLASSIFY TRADE TYPE: SCALP / INTRADAY / RUNNER ──
        trade_type = classify_trade(sig)
        meta["trade_type"] = trade_type
        type_cfg = TRADE_TYPE_CONFIG.get(trade_type, TRADE_TYPE_CONFIG[TRADE_TYPE_SCALP])

        # Override TP levels based on trade type
        raw_tps = tps[:]  # copy original
        risk_dist = abs(entry - sl) if entry > 0 and sl > 0 else 0.0
        if risk_dist > 0 and trade_type != TRADE_TYPE_SCALP:
            # Recalculate TPs from trade type config
            side_val = sig.get("side", "long")
            if hasattr(side_val, 'value'):
                side_val = side_val.value
            if side_val == "long":
                tp1_new = entry + risk_dist * type_cfg["tp1_rr"] if type_cfg["tp1_rr"] > 0 else 0.0
                tp2_new = entry + risk_dist * type_cfg["tp2_rr"] if type_cfg["tp2_rr"] > 0 else 0.0
                tp3_new = entry + risk_dist * type_cfg["tp3_rr"] if type_cfg["tp3_rr"] > 0 else 0.0
            else:
                tp1_new = entry - risk_dist * type_cfg["tp1_rr"] if type_cfg["tp1_rr"] > 0 else 0.0
                tp2_new = entry - risk_dist * type_cfg["tp2_rr"] if type_cfg["tp2_rr"] > 0 else 0.0
                tp3_new = entry - risk_dist * type_cfg["tp3_rr"] if type_cfg["tp3_rr"] > 0 else 0.0
            raw_tps = [tp1_new, tp2_new, tp3_new]
            logger.info(
                "TRADE TYPE %s TPs: TP1=%.2f (%.1fR) TP2=%.2f (%.1fR) TP3=%.2f (%.1fR)",
                trade_type, tp1_new, type_cfg["tp1_rr"], tp2_new, type_cfg["tp2_rr"],
                tp3_new, type_cfg["tp3_rr"],
            )
        elif risk_dist > 0 and trade_type == TRADE_TYPE_SCALP:
            # Scalp: override TPs to tight values, kill TP3
            side_val = sig.get("side", "long")
            if hasattr(side_val, 'value'):
                side_val = side_val.value
            if side_val == "long":
                tp1_new = entry + risk_dist * type_cfg["tp1_rr"]
                tp2_new = entry + risk_dist * type_cfg["tp2_rr"] if type_cfg["tp2_rr"] > 0 else 0.0
            else:
                tp1_new = entry - risk_dist * type_cfg["tp1_rr"]
                tp2_new = entry - risk_dist * type_cfg["tp2_rr"] if type_cfg["tp2_rr"] > 0 else 0.0
            raw_tps = [tp1_new, tp2_new, 0.0]  # NO TP3 for scalps
            logger.info(
                "SCALP TPs: TP1=%.2f (%.1fR) TP2=%.2f (%.1fR) NO TP3",
                tp1_new, type_cfg["tp1_rr"], tp2_new, type_cfg["tp2_rr"],
            )

        return cls(
            trade_id=sig.get("trade_id", ""),
            symbol=sig.get("symbol", ""),
            side=sig.get("side", "long"),
            entry_price=entry,
            stop_loss=sl,
            tp1=float(raw_tps[0]) if len(raw_tps) > 0 else 0.0,
            tp2=float(raw_tps[1]) if len(raw_tps) > 1 else 0.0,
            tp3=float(raw_tps[2]) if len(raw_tps) > 2 else 0.0,
            confidence=confidence,
            grade=str(sig.get("grade", "")),
            setup_type=meta.get("setup_type", ""),
            strategy_type=meta.get("strategy_type", "scalp"),
            trade_type=trade_type,
            reason=sig.get("reason", ""),
            paper_stake=paper_stake,
            leverage=lev,
            position_size_usd=position_usd,
            risk_amount_usd=round(risk_amount, 2),
            signal_atr=signal_atr,
            contract_size=contract_sz,
            contracts=num_contracts,
            quantity=round(quantity, 6),
            leverage_cap_source=lev_cap_source,
            initial_risk=abs(entry - sl) if entry > 0 and sl > 0 else 0.0,
            metadata=meta,  # preserve full metadata (ML scores, scanner config, etc.)
            entry_time=sig.get("timestamp", datetime.now(timezone.utc).isoformat()),
            highest_price=entry,
            lowest_price=entry,
            # Slippage: signal_price = intended entry, fill_price = actual fill
            # In paper mode both equal entry (zero slippage)
            # Real mode: fill_price updated after exchange confirms fill
            signal_price=entry,
            fill_price=entry,  # updated by real manager if live
            slippage_ticks=0.0,
            slippage_bps=0.0,
            slippage_impact_r=0.0,
            order_type=meta.get("order_type", "market"),
        )


class SignalTracker:
    """Tracks open signals for TP/SL closure and maintains P&L + win rate stats."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._active: Dict[str, TrackedSignal] = {}  # trade_id -> TrackedSignal
        self._closed: List[Dict[str, Any]] = []
        self._stats: Dict[str, Any] = {}
        self._lock = asyncio.Lock()  # protects _active/_closed state mutations
        self._exchange_balance: Optional[float] = None  # real exchange purse balance
        self._paper_start_balance: float = 1000.0  # paper trading starting capital
        self._training_dataset = None  # set by orchestrator for ML feedback
        self._live_feedback_file = _STORAGE_DIR / "ml_live_feedback.jsonl"
        # Recently closed trades: {paper_trade_id: {exit_price, exit_reason, symbol, side}}
        # Used by orphan sync to get accurate exit prices instead of entry==exit
        self._closed_recently: Dict[str, Dict] = {}

        exec_cfg = (config or {}).get("execution", {})
        self._order_type: str = exec_cfg.get("order_type", "maker")  # "maker" | "taker" | "auto"
        self._max_entry_slip_bps: float = exec_cfg.get("max_entry_slip_bps", 30)  # 0 = disabled
        self._min_trail_hold_sec: float = exec_cfg.get("min_trail_hold_sec", 15)  # seconds before trail-lock

        self._load()

    def set_exchange_balance(self, balance: float) -> None:
        """Set the real exchange purse balance (fetched from Delta Exchange)."""
        self._exchange_balance = balance

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def track_signal(self, signal_dict: Dict[str, Any]) -> None:
        """Start tracking a new signal.

        DUPLICATE PREVENTION: Max 1 active position per symbol+side.
        This prevents the #1 loss cause — 13 identical entries burning $86+ in fees.
        """
        logger.info("TRACK_ENTER: %s %s", signal_dict.get("symbol", "?"), signal_dict.get("side", "?"))
        ts = TrackedSignal.from_signal(signal_dict)
        if not ts.entry_price or not ts.stop_loss:
            logger.warning("Cannot track signal %s: missing entry/SL (entry=%s, sl=%s)", ts.trade_id, ts.entry_price, ts.stop_loss)
            return
        logger.info("TRACK_HAS_PRICES: %s entry=%.4f sl=%.4f", ts.trade_id[:8], ts.entry_price, ts.stop_loss)

        # Inject computed sizing back into signal_dict so paper engine uses it
        # (paper engine reads position_size/leverage from the same dict)
        if ts.quantity > 0:
            signal_dict["position_size"] = ts.quantity
        if ts.leverage > 0:
            signal_dict["leverage"] = ts.leverage

        if ts.trade_id in self._active:
            logger.info("TRACK_SKIP: %s already in _active", ts.trade_id[:8])
            return  # already tracking

        logger.info("TRACK_PASS_DEDUP: %s %s entry=%.4f sl=%.4f", ts.trade_id[:8], ts.symbol, ts.entry_price, ts.stop_loss)
        logger.info(
            "TRACK_DEBUG: %s %s %s | score=%s grade=%s conf=%s | checking filters...",
            ts.trade_id[:8], ts.symbol, ts.side,
            signal_dict.get("metadata", {}).get("weighted_score", "N/A"),
            signal_dict.get("grade", "?"), signal_dict.get("confidence", "?"),
        )

        # ── DUPLICATE PREVENTION: max 1 per symbol+side (active) ──
        for existing in list(self._active.values()):
            if existing.symbol == ts.symbol and existing.side == ts.side:
                logger.info(
                    "DUPLICATE BLOCKED (active): %s %s %s — already have %s open",
                    ts.trade_id[:8], ts.symbol, ts.side, existing.trade_id[:8],
                )
                return

        # ── CONFLICT PREVENTION: block opposite-direction on same symbol ──
        # Data shows LONG+SHORT on same symbol within seconds = guaranteed loss after fees
        for existing in self._active.values():
            if existing.symbol == ts.symbol and existing.side != ts.side:
                logger.info(
                    "CONFLICT BLOCKED: %s %s %s — opposite signal %s %s already active (%s)",
                    ts.trade_id[:8], ts.symbol, ts.side,
                    existing.trade_id[:8], existing.side, existing.symbol,
                )
                return

        # ── SETUP STRENGTH VETO: reject weak setups that tend to timeout ──
        MIN_SETUP_STRENGTH = 40  # Lowered: funnel already filters weak setups
        meta = signal_dict.get("metadata", {})
        setup_score = meta.get("weighted_score", 0)
        if setup_score and setup_score < MIN_SETUP_STRENGTH:
            logger.info(
                "WEAK SETUP BLOCKED: %s %s %s | score=%.0f < %d — likely to timeout",
                ts.trade_id[:8], ts.symbol, ts.side, setup_score, MIN_SETUP_STRENGTH,
            )
            return

        # ── DUPLICATE PREVENTION: no re-entry at same price within 30 min ──
        from datetime import datetime, timedelta, timezone
        try:
            now_dt = datetime.now(timezone.utc)
            for recent in self._closed[-50:]:  # check last 50 closed
                if recent.get("symbol") == ts.symbol and recent.get("side") == ts.side:
                    price_match = abs(recent.get("entry_price", 0) - ts.entry_price) < ts.entry_price * 0.001  # within 0.1%
                    if price_match:
                        try:
                            closed_time = datetime.fromisoformat(recent.get("exit_time", ""))
                            if (now_dt - closed_time).total_seconds() < 1800:  # 30 min cooldown
                                logger.info(
                                    "DUPLICATE BLOCKED (recent): %s %s %s @ %.2f — same price closed %dm ago",
                                    ts.trade_id[:8], ts.symbol, ts.side, ts.entry_price,
                                    int((now_dt - closed_time).total_seconds() / 60),
                                )
                                return
                        except (ValueError, TypeError, KeyError):
                            pass
        except (ValueError, TypeError, KeyError, AttributeError):
            pass

        self._active[ts.trade_id] = ts
        logger.info(
            "Tracking signal: %s %s %s @ %.2f | SL=%.2f TP1=%.2f TP2=%.2f TP3=%.2f",
            ts.trade_id[:8], ts.symbol, ts.side,
            ts.entry_price, ts.stop_loss, ts.tp1, ts.tp2, ts.tp3,
        )
        self._save_active()

    def update_prices(self, prices: Dict[str, float]) -> List[Dict[str, Any]]:
        """Check all active signals against current prices.

        Returns list of closure events (for alerting).

        Thread safety: Uses snapshot of _active keys to prevent mutation
        during iteration. The asyncio lock protects state mutations in
        _close_signal and _save_active. The list() snapshot prevents
        dict-changed-during-iteration errors.
        """
        # Re-entrancy guard: prevent concurrent calls from corrupting state
        if getattr(self, '_updating_prices', False):
            return []
        self._updating_prices = True
        try:
            return self._update_prices_inner(prices)
        finally:
            self._updating_prices = False

    def _update_prices_inner(self, prices: Dict[str, float]) -> List[Dict[str, Any]]:
        events = []
        to_close = []

        for tid, ts in list(self._active.items()):  # snapshot to avoid mutation during iteration
            price = prices.get(ts.symbol)
            if price is None:
                continue

            # ── PRICE SANITY CHECK ──
            # Reject prices that are wildly different from entry (wrong symbol leak)
            if ts.entry_price > 0 and price > 0:
                deviation = abs(price - ts.entry_price) / ts.entry_price
                if deviation > 0.15:  # >15% deviation = wrong symbol or data corruption
                    logger.error(
                        "PRICE SANITY FAIL: %s %s | entry=%.4f price=%.4f | dev=%.1f%% — SKIPPING",
                        ts.symbol, ts.side, ts.entry_price, price, deviation * 100,
                    )
                    continue

            # ── Estimated Slippage (paper mode) ──
            # On first price update after entry, capture the market price as
            # "estimated fill" to simulate what slippage would have been.
            # If slippage exceeds max_entry_slip_bps, cap it (maker mode:
            # the order would have rested at signal_price, not filled worse).
            if ts.fill_price == ts.signal_price and ts.signal_price > 0 and ts.slippage_bps == 0:
                est_slip = abs(price - ts.signal_price)
                est_slip_bps = est_slip / ts.signal_price * 10000

                max_slip_bps = getattr(self, "_max_entry_slip_bps", 30)
                if max_slip_bps > 0 and est_slip_bps > max_slip_bps:
                    # Cap slippage: in maker mode the order rests at signal_price,
                    # so worst realistic fill is signal + max_slip; beyond that the
                    # order would not fill (and retry_taker_on_reject handles it).
                    capped_slip = ts.signal_price * max_slip_bps / 10000
                    is_long = ts.side == "long"
                    ts.fill_price = ts.signal_price + (capped_slip if is_long else -capped_slip)
                    ts.slippage_bps = round(max_slip_bps, 2)
                    logger.warning(
                        "SLIP CAP: %s %s | raw=%.1fbps capped=%.0fbps | signal=%.4f fill=%.4f",
                        ts.symbol, ts.side, est_slip_bps, max_slip_bps,
                        ts.signal_price, ts.fill_price,
                    )
                else:
                    ts.fill_price = price
                    ts.slippage_bps = round(est_slip_bps, 2)

                ts.slippage_ticks = round(est_slip / (ts.signal_atr * 0.01) if ts.signal_atr > 0 else 0, 2)
                if ts.initial_risk > 0:
                    actual_slip = abs(ts.fill_price - ts.signal_price)
                    ts.slippage_impact_r = round(actual_slip / ts.initial_risk, 4)

            # Update high/low watermarks
            if price > ts.highest_price:
                ts.highest_price = price
            if price < ts.lowest_price:
                ts.lowest_price = price

            is_long = ts.side == "long"

            # Update MAE/MFE in R-multiples (live tracking)
            if ts.initial_risk > 0:
                if is_long:
                    fav = (ts.highest_price - ts.entry_price) / ts.initial_risk
                    adv = (ts.entry_price - ts.lowest_price) / ts.initial_risk
                else:
                    fav = (ts.entry_price - ts.lowest_price) / ts.initial_risk
                    adv = (ts.highest_price - ts.entry_price) / ts.initial_risk
                ts.mfe_r = round(max(ts.mfe_r, fav), 4)
                ts.mae_r = round(max(ts.mae_r, adv), 4)
            now_iso = datetime.now(timezone.utc).isoformat()

            # -- Early Invalidation Exit: Hard Loss Cap (-1.2R) --
            # Force close if adverse excursion exceeds 1.2R (tightened from 2R)
            # Prevents -1.7R catastrophic losses seen in last 24h
            if ts.initial_risk > 0:
                if is_long:
                    current_adverse_r = (ts.entry_price - price) / ts.initial_risk
                else:
                    current_adverse_r = (price - ts.entry_price) / ts.initial_risk
                if current_adverse_r >= 1.2:
                    ts.exit_price = price
                    ts.exit_reason = "hard_loss_cap"
                    ts.exit_time = now_iso
                    ts.exit_reason_detailed = "hard_loss_cap_2r"
                    ts.status = "stopped"
                    ts.pnl_pct = self._calc_pnl(ts, price, self._order_type)
                    to_close.append(tid)
                    events.append({
                        "type": "hard_loss_cap",
                        "signal": ts.to_dict(),
                        "message": (
                            f"HARD LOSS CAP: {ts.symbol} {ts.side} @ {price:.2f} | "
                            f"Adverse excursion {current_adverse_r:.2f}R exceeds 2R limit | "
                            f"PnL: {ts.pnl_pct:+.2f}%"
                        ),
                    })
                    logger.warning(
                        "Hard loss cap triggered: %s %s @ %.2f (%.2fR adverse) | PnL: %.2f%%",
                        ts.symbol, ts.side, price, current_adverse_r, ts.pnl_pct,
                    )
                    continue

            # -- DYNAMIC TRAILING PROFIT PROTECTION --
            # Continuously trails stop based on MFE. No more waiting for
            # fixed thresholds — every tick of profit is partially locked.
            if ts.symbol == "BTC/USDT" and ts.trade_id[:8] == "fd7833bb":
                logger.info("BTC_DEBUG: price=%.2f entry=%.2f sl=%.2f ir=%.2f mfe_r=%.2f peak=%.2f",
                           price, ts.entry_price, ts.stop_loss, ts.initial_risk, ts.mfe_r, ts.peak_mfe_r)
            #
            # Trail levels:
            #   MFE 0.3R+  → trail floor = breakeven (0.0R)
            #   MFE 0.5R+  → trail floor = 50% of MFE
            #   MFE 1.0R+  → trail floor = 65% of MFE
            #   MFE 1.5R+  → trail floor = 75% of MFE
            # Exit when current_r drops below trail floor.
            if ts.initial_risk > 0:
                if is_long:
                    current_r = (price - ts.entry_price) / ts.initial_risk
                else:
                    current_r = (ts.entry_price - price) / ts.initial_risk

                profit_protect = False
                exit_reason_tag = ""
                exit_detail = ""
                trail_floor = None

                if ts.mfe_r >= 1.5:
                    trail_floor = ts.mfe_r * 0.80
                    exit_reason_tag = "trail_lock_75pct"
                elif ts.mfe_r >= 1.0:
                    trail_floor = ts.mfe_r * 0.72
                    exit_reason_tag = "trail_lock_65pct"
                elif ts.mfe_r >= 0.7:
                    trail_floor = ts.mfe_r * 0.65
                    exit_reason_tag = "trail_lock_65pct"
                elif ts.mfe_r >= 0.5:
                    trail_floor = ts.mfe_r * 0.55
                    exit_reason_tag = "trail_lock_50pct"
                elif ts.mfe_r >= 0.3:
                    trail_floor = 0.15  # lock 0.15R minimum (covers fees)
                    exit_reason_tag = "trail_breakeven"

                if ts.symbol == "BTC/USDT" and ts.trade_id[:8] == "fd7833bb":
                    logger.info("BTC_TRAIL: cur_r=%.3f trail_floor=%s mfe_r=%.3f protect=%s",
                               current_r, trail_floor, ts.mfe_r, trail_floor is not None and current_r <= trail_floor)
                if trail_floor is not None and current_r <= trail_floor:
                    profit_protect = True
                    exit_detail = (
                        f"Trail stop: MFE {ts.mfe_r:.2f}R, floor {trail_floor:.2f}R, "
                        f"current {current_r:.2f}R"
                    )

                if profit_protect:
                    logger.info("TRAIL_EXIT_FIRING: %s %s cur_r=%.3f floor=%.3f", ts.symbol, ts.side, current_r, trail_floor)
                    try:
                        ts.exit_price = price
                        ts.exit_reason = exit_reason_tag
                        ts.exit_time = now_iso
                        ts.exit_reason_detailed = exit_reason_tag
                        ts.status = "breakeven" if current_r <= 0.05 else "partial_win"
                        ts.pnl_pct = self._calc_pnl(ts, price, self._order_type)
                        to_close.append(tid)
                        events.append({
                            "type": exit_reason_tag,
                            "signal": ts.to_dict(),
                            "message": (
                                f"TRAIL STOP: {ts.symbol} {ts.side} @ {price:.2f} | "
                                f"{exit_detail} | PnL: {ts.pnl_pct:+.2f}%"
                            ),
                        })
                        logger.info(
                            "Trail stop: %s %s @ %.2f | %s | PnL: %.2f%%",
                            ts.symbol, ts.side, price, exit_detail, ts.pnl_pct,
                        )
                    except Exception as _pex:
                        logger.error("TRAIL EXIT ERROR: %s -- %s", ts.symbol, _pex, exc_info=True)
                    continue

            # -- Check Stop Loss --
            sl_hit = (price <= ts.stop_loss) if is_long else (price >= ts.stop_loss)
            if sl_hit and not ts.sl_hit:
                ts.sl_hit = True
                ts.exit_price = price
                ts.exit_time = now_iso
                ts.pnl_pct = self._calc_pnl(ts, price, self._order_type)
                overshoot = abs(price - ts.stop_loss)
                ts.stop_overshoot_pct = round((overshoot / ts.entry_price) * 100, 4) if ts.entry_price > 0 else 0

                # SMART EXIT REASON: distinguish actual loss from trail/BE profit
                is_profit_exit = (
                    (is_long and ts.stop_loss > ts.entry_price) or
                    (not is_long and ts.stop_loss < ts.entry_price)
                )

                if ts.tp1_hit:
                    ts.exit_reason = "partial_win"
                    ts.exit_reason_detailed = "sl_after_tp1"
                    ts.status = "partial_win"
                elif is_profit_exit and ts.breakeven_set:
                    ts.exit_reason = "trail_profit"
                    ts.exit_reason_detailed = f"trail_lock_+{ts.mfe_r:.1f}R_peak"
                    ts.status = "trail_win"
                elif ts.breakeven_set and ts.pnl_pct >= -0.05:
                    ts.exit_reason = "breakeven"
                    ts.exit_reason_detailed = "breakeven_exit"
                    ts.status = "breakeven"
                else:
                    ts.exit_reason = "stop_loss"
                    ts.exit_reason_detailed = "stop_loss"
                    ts.status = "stopped"

                to_close.append(tid)
                label = "🟢 TRAIL WIN" if is_profit_exit else "🔴 SL HIT"
                events.append({
                    "type": "sl_hit",
                    "signal": ts.to_dict(),
                    "message": (
                        f"{label}: {ts.symbol} {ts.side} @ {price:.2f} | "
                        f"SL={ts.stop_loss:.2f} | {ts.exit_reason} | "
                        f"PnL: {ts.pnl_pct:+.2f}%"
                    ),
                })
                continue

            # -- SMART TRAILING STOP (progressive profit lock) --
            # Instead of fixed BE, trail SL to lock increasing % of profit:
            #   +0.5R → lock 0.3R (covers fees)
            #   +1.0R → lock 0.5R
            #   +1.5R → lock 0.8R
            #   +2.0R → lock 1.2R
            # This prevents giving back large unrealized profits
            if ts.initial_risk > 0 and not ts.tp1_hit:
                if is_long:
                    current_r_trail = (price - ts.entry_price) / ts.initial_risk
                else:
                    current_r_trail = (ts.entry_price - price) / ts.initial_risk

                # ══════════════════════════════════════════════════
                # SMART EXIT SYSTEM v2 — 5 improvements combined
                # ══════════════════════════════════════════════════

                # ── FIX #1: MFE MEMORY TRAIL ──
                # Never give back more than X% of peak profit.
                # Track peak MFE and set SL as percentage of peak.
                now_ts = time.time()
                if current_r_trail > ts.peak_mfe_r:
                    ts.peak_mfe_r = current_r_trail
                    ts.last_mfe_update_time = now_ts
                    ts.mfe_stale_seconds = 0
                elif ts.last_mfe_update_time > 0:
                    ts.mfe_stale_seconds = now_ts - ts.last_mfe_update_time

                # MFE-based lock: protect percentage of peak profit
                # More aggressive tiers — lock more as MFE grows
                # Gate: minimum hold time prevents paper-mode sub-5-second exits
                min_hold = getattr(self, "_min_trail_hold_sec", 15)
                try:
                    _entry_dt = datetime.fromisoformat(ts.entry_time)
                    _trade_age = (datetime.now(timezone.utc) - _entry_dt).total_seconds()
                except (ValueError, TypeError):
                    _trade_age = 999  # fallback: allow trail
                if ts.peak_mfe_r >= 0.15 and _trade_age >= min_hold:
                    if ts.peak_mfe_r >= 1.5:
                        lock_pct = 0.88  # lock 88% of peak when >1.5R
                    elif ts.peak_mfe_r >= 1.0:
                        lock_pct = 0.82  # lock 82% when >1R
                    elif ts.peak_mfe_r >= 0.75:
                        lock_pct = 0.75  # lock 75% when >0.75R
                    elif ts.peak_mfe_r >= 0.5:
                        lock_pct = 0.70  # lock 70% when >0.5R (was 0.65)
                    elif ts.peak_mfe_r >= 0.3:
                        lock_pct = 0.60  # lock 60% when >0.3R (was 0.55)
                    else:
                        lock_pct = 0.0   # breakeven when >0.15R

                    # ── TIME-BASED TIGHTENING ──
                    # If MFE hasn't improved in 8 min, tighten lock by 10%
                    if ts.mfe_stale_seconds > 480 and ts.peak_mfe_r > 0.3:
                        lock_pct = min(lock_pct + 0.10, 0.90)

                    # ── REGIME-ADAPTIVE TRAIL ──
                    _regime = ts.metadata.get("regime", "") if ts.metadata else ""
                    if _regime in ("trending_up", "trending_down", "breakout"):
                        lock_pct *= 0.95  # minimal discount in trends (was 0.92 — letting too much slip)
                    elif _regime in ("ranging", "sideways", "quiet"):
                        lock_pct *= 1.10  # tighter in ranges
                        lock_pct = min(lock_pct, 0.92)

                    # ── MOMENTUM DECAY ──
                    if ts.momentum_decay_count >= 3 and ts.peak_mfe_r > 0.3:
                        lock_pct = min(lock_pct + 0.10, 0.92)

                    lock_r = ts.peak_mfe_r * lock_pct
                    lock_dist = ts.initial_risk * lock_r
                    fee_cover = ts.entry_price * 0.0028
                    lock_dist = max(lock_dist, fee_cover)

                    if is_long:
                        new_sl = ts.entry_price + lock_dist
                    else:
                        new_sl = ts.entry_price - lock_dist

                    should_update = (
                        (is_long and new_sl > ts.stop_loss) or
                        (not is_long and new_sl < ts.stop_loss)
                    )
                    if should_update:
                        old_sl = ts.stop_loss
                        ts.stop_loss = new_sl
                        if not ts.breakeven_set:
                            ts.breakeven_set = True
                        logger.info(
                            "SMART TRAIL: %s %s @ %.2f | peak=%.2fR cur=%.2fR lock=%.0f%% → +%.2fR | SL → %.2f%s",
                            ts.symbol, ts.side, price, ts.peak_mfe_r, current_r_trail,
                            lock_pct * 100, lock_r, ts.stop_loss,
                            " [STALE]" if ts.mfe_stale_seconds > 600 else "",
                        )
                        # Emit SL update event for exchange sync
                        events.append({
                            "type": "sl_updated",
                            "trade_id": ts.trade_id,
                            "symbol": ts.symbol,
                            "side": ts.side,
                            "new_sl": ts.stop_loss,
                            "old_sl": old_sl,
                            "peak_mfe_r": ts.peak_mfe_r,
                        })

                # ── FIX #2: PARTIAL EXIT AT 0.3R ──
                # Close 35% of position at 0.3R MFE (before TP1)
                if current_r_trail >= 0.3 and not ts.partial_exit_done and not ts.tp1_hit:
                    ts.partial_exit_done = True
                    # Book 35% partial profit
                    if is_long:
                        partial_pnl = ((price - ts.entry_price) / ts.entry_price) * 100
                    else:
                        partial_pnl = ((ts.entry_price - price) / ts.entry_price) * 100
                    ts.position_remaining_pct = 0.65
                    logger.info(
                        "PARTIAL EXIT 0.3R: %s %s @ %.2f | +%.2fR | 35%% closed, 65%% running",
                        ts.symbol, ts.side, price, current_r_trail,
                    )

                # ── FIX #5: MOMENTUM DECAY DETECTION ──
                # If price hasn't made new MFE high and is stalling, increment decay
                if ts.peak_mfe_r > 0.2 and current_r_trail < ts.peak_mfe_r * 0.85:
                    # Price dropped from peak — potential momentum loss
                    ts.momentum_decay_count = getattr(ts, 'momentum_decay_count', 0) + 1
                elif current_r_trail >= ts.peak_mfe_r:
                    # New high — reset decay
                    ts.momentum_decay_count = 0

            # -- Check TP levels (in order) --
            if not ts.tp1_hit and ts.tp1:
                tp1_hit = (price >= ts.tp1) if is_long else (price <= ts.tp1)
                if tp1_hit:
                    ts.tp1_hit = True
                    ts.tp1_time = now_iso
                    ts.status = "tp1_hit"
                    ts.tp1_distance_r = abs(ts.tp1 - ts.entry_price) / ts.initial_risk if ts.initial_risk > 0 else 0

                    # Book partial profit: 60% of position at TP1
                    if is_long:
                        tp1_pnl = ((price - ts.entry_price) / ts.entry_price) * 100
                    else:
                        tp1_pnl = ((ts.entry_price - price) / ts.entry_price) * 100
                    ts.tp1_pnl_locked = round(0.35 * tp1_pnl, 4)  # 35% at TP1
                    ts.position_remaining_pct = 0.65

                    # Regime-aware trailing: adjust trail distance based on market regime
                    _regime = ts.metadata.get("regime", "") if ts.metadata else ""
                    _scanner = ts.setup_type or ""
                    _trail_params = self._get_trail_params(_regime, _scanner, getattr(ts, 'trade_type', ''))
                    _trail_mult = _trail_params["trail_atr_mult"]
                    atr_trail_dist = ts.signal_atr * _trail_mult if ts.signal_atr > 0 else abs(ts.tp1 - ts.entry_price) * 0.5
                    if is_long:
                        trail_sl = price - atr_trail_dist
                        # Trail must be at least at breakeven+fees
                        fee_buffer = ts.entry_price * (0.20 / 100)
                        trail_sl = max(trail_sl, ts.entry_price + fee_buffer)
                    else:
                        trail_sl = price + atr_trail_dist
                        fee_buffer = ts.entry_price * (0.20 / 100)
                        trail_sl = min(trail_sl, ts.entry_price - fee_buffer)

                    ts.atr_trail_active = True
                    ts.atr_trail_price = trail_sl
                    ts.stop_loss = trail_sl

                    events.append({
                        "type": "tp1_hit",
                        "signal": ts.to_dict(),
                        "message": (
                            f"TP1 HIT: {ts.symbol} {ts.side} @ {price:.2f} | "
                            f"60% booked ({ts.tp1_pnl_locked:+.2f}%) | "
                            f"Trail started @ {ts.stop_loss:.2f} (1.0×ATR)"
                        ),
                    })

            if not ts.tp2_hit and ts.tp2 and ts.tp1_hit:
                tp2_hit = (price >= ts.tp2) if is_long else (price <= ts.tp2)
                if tp2_hit:
                    ts.tp2_hit = True
                    ts.tp2_time = now_iso
                    ts.status = "tp2_hit"

                    # Book 25% partial at TP2
                    if is_long:
                        tp2_pnl = ((price - ts.entry_price) / ts.entry_price) * 100
                    else:
                        tp2_pnl = ((ts.entry_price - price) / ts.entry_price) * 100
                    ts.tp2_pnl_locked = round(0.35 * tp2_pnl, 4)  # 35% at TP2
                    ts.position_remaining_pct = 0.30  # 30% runner left

                    # Tighten ATR trail (regime-aware, runner protection)
                    _regime = ts.metadata.get("regime", "") if ts.metadata else ""
                    _scanner = ts.setup_type or ""
                    _trail_params = self._get_trail_params(_regime, _scanner, getattr(ts, 'trade_type', ''))
                    _tp2_mult = _trail_params["trail_atr_mult"] * 0.8  # tighter than TP1 trail
                    atr_trail_dist = ts.signal_atr * _tp2_mult if ts.signal_atr > 0 else abs(ts.tp2 - ts.tp1) * 0.3
                    if is_long:
                        ts.atr_trail_price = price - atr_trail_dist
                        # Floor at TP1 (lock TP1 profit for runner)
                        ts.atr_trail_price = max(ts.atr_trail_price, ts.tp1)
                    else:
                        ts.atr_trail_price = price + atr_trail_dist
                        ts.atr_trail_price = min(ts.atr_trail_price, ts.tp1)
                    ts.stop_loss = ts.atr_trail_price
                    events.append({
                        "type": "tp2_hit",
                        "signal": ts.to_dict(),
                        "message": (
                            f"TP2 HIT: {ts.symbol} {ts.side} @ {price:.2f} | "
                            f"25% booked ({ts.tp2_pnl_locked:+.2f}%) | "
                            f"Trail tightened @ {ts.atr_trail_price:.2f} (0.8×ATR)"
                        ),
                    })

            if not ts.tp3_hit and ts.tp3 and ts.tp2_hit:
                tp3_hit = (price >= ts.tp3) if is_long else (price <= ts.tp3)
                if tp3_hit:
                    ts.tp3_hit = True
                    ts.tp3_time = now_iso
                    ts.exit_price = price
                    ts.exit_reason = "tp3_full"
                    ts.exit_time = now_iso
                    ts.pnl_pct = self._calc_pnl(ts, price, self._order_type)
                    ts.exit_reason_detailed = "tp3_full_win"
                    ts.status = "tp3_hit"
                    to_close.append(tid)
                    events.append({
                        "type": "tp3_hit",
                        "signal": ts.to_dict(),
                        "message": f"TP3 FULL WIN: {ts.symbol} {ts.side} @ {price:.2f} | PnL: {ts.pnl_pct:+.2f}%",
                    })

            # -- ATR TRAILING STOP RATCHET (after TP1 or TP2) --
            # Trail distance is regime-aware and tightens as TPs are hit
            if ts.atr_trail_active and ts.tp1_hit and not ts.tp3_hit:
                _regime = ts.metadata.get("regime", "") if ts.metadata else ""
                _scanner = ts.setup_type or ""
                _trail_params = self._get_trail_params(_regime, _scanner)
                _base_mult = _trail_params["trail_atr_mult"]
                if ts.tp2_hit:
                    atr_trail_dist = ts.signal_atr * (_base_mult * 0.8) if ts.signal_atr > 0 else abs(ts.tp2 - ts.tp1) * 0.3
                else:
                    atr_trail_dist = ts.signal_atr * _base_mult if ts.signal_atr > 0 else abs(ts.tp1 - ts.entry_price) * 0.5
                if is_long:
                    new_trail = price - atr_trail_dist
                    # Only ratchet UP (tighter), never down
                    if new_trail > ts.atr_trail_price:
                        ts.atr_trail_price = new_trail
                        ts.stop_loss = new_trail
                else:
                    new_trail = price + atr_trail_dist
                    # Only ratchet DOWN (tighter), never up
                    if new_trail < ts.atr_trail_price:
                        ts.atr_trail_price = new_trail
                        ts.stop_loss = new_trail

            # -- PATCH 5: Near-TP reversal protection --
            # If price reaches 85%+ of TP1 distance but hasn't hit TP1,
            # tighten stop to protect the near-win
            if not ts.tp1_hit and ts.tp1 and ts.status == "active":
                tp1_dist = abs(ts.tp1 - ts.entry_price)
                if is_long:
                    current_fav = price - ts.entry_price
                else:
                    current_fav = ts.entry_price - price

                if tp1_dist > 0 and current_fav >= tp1_dist * 0.85:
                    # Price reached 85%+ of TP1 — activate near-TP protection
                    if not ts.near_tp_triggered:
                        ts.near_tp_triggered = True
                        # Tighten stop to lock 50% of current favorable move
                        half_move = current_fav * 0.50
                        if is_long:
                            new_sl = ts.entry_price + half_move
                        else:
                            new_sl = ts.entry_price - half_move
                        # Only tighten, never widen
                        should_update = (
                            (is_long and new_sl > ts.stop_loss) or
                            (not is_long and new_sl < ts.stop_loss)
                        )
                        if should_update:
                            ts.stop_loss = new_sl
                            logger.info(
                                "NEAR-TP PROTECT: %s %s | reached %.1f%% of TP1 | "
                                "SL tightened to %.2f (locks 50%% of move)",
                                ts.symbol, ts.side,
                                (current_fav / tp1_dist) * 100, ts.stop_loss,
                            )

                # If near-TP was triggered but price is now retreating,
                # and momentum has failed (price < 50% of peak favorable excursion),
                # close the trade to protect gains
                if ts.near_tp_triggered and not ts.tp1_hit:
                    peak_fav = (ts.highest_price - ts.entry_price) if is_long else (ts.entry_price - ts.lowest_price)
                    if peak_fav > 0 and current_fav < peak_fav * 0.40:
                        ts.exit_price = price
                        ts.exit_reason = "near_tp_protect_exit"
                        ts.exit_time = now_iso
                        ts.pnl_pct = self._calc_pnl(ts, price, self._order_type)
                        ts.exit_reason_detailed = "near_tp_protect_exit"
                        ts.status = "partial_win" if ts.pnl_pct > 0 else "stopped"
                        to_close.append(tid)
                        events.append({
                            "type": "near_tp_protect",
                            "signal": ts.to_dict(),
                            "message": (
                                f"NEAR-TP PROTECT EXIT: {ts.symbol} {ts.side} @ {price:.2f} | "
                                f"Peak fav: {peak_fav:.2f}, current: {current_fav:.2f} | "
                                f"PnL: {ts.pnl_pct:+.2f}%"
                            ),
                        })
                        continue

            # -- TRADE-TYPE-AWARE TIME STOP --
            # SCALP: hard time stop (3-5 bars), aggressive early kill
            # INTRADAY: soft time stop (15 bars), only if losing + no progress
            # RUNNER: NO time stop — only exit on structure/trailing
            if ts.status == "active" and not ts.tp1_hit:
                try:
                    entry_dt = datetime.fromisoformat(ts.entry_time)
                    age_sec = (datetime.now(timezone.utc) - entry_dt).total_seconds()
                    risk = abs(ts.entry_price - ts.stop_loss)

                    # Calculate max favorable excursion in R
                    if risk > 0:
                        if is_long:
                            max_fav_r = (ts.highest_price - ts.entry_price) / risk
                        else:
                            max_fav_r = (ts.entry_price - ts.lowest_price) / risk
                    else:
                        max_fav_r = 0

                    if is_long:
                        current_r = (price - ts.entry_price) / risk if risk > 0 else 0
                    else:
                        current_r = (ts.entry_price - price) / risk if risk > 0 else 0

                    # Get trade type config
                    tt = getattr(ts, 'trade_type', TRADE_TYPE_SCALP)
                    tt_cfg = TRADE_TYPE_CONFIG.get(tt, TRADE_TYPE_CONFIG[TRADE_TYPE_SCALP])
                    time_stop_type = tt_cfg["time_stop_type"]
                    max_age = tt_cfg["max_age_sec"]
                    early_kill_sec = tt_cfg["early_kill_sec"]
                    early_kill_mfe = tt_cfg["early_kill_mfe"]

                    dead_trade = False
                    kill_reason = ""

                    # ── EARLY KILL (SCALP + INTRADAY only, not RUNNER) ──
                    if early_kill_sec > 0 and age_sec >= early_kill_sec:
                        if max_fav_r < early_kill_mfe and current_r < -0.15:
                            dead_trade = True
                            kill_reason = "early_kill_%ds" % int(age_sec)
                            logger.info(
                                "EARLY KILL [%s]: %s %s | %ds with MFE %.2fR < %.2fR, current %.2fR",
                                tt, ts.symbol, ts.side, int(age_sec),
                                max_fav_r, early_kill_mfe, current_r,
                            )

                    # ── 5-MINUTE MOMENTUM KILL (INTRADAY) ──
                    # Catches dead entries that pass early kill but sit flat for 5+ min
                    # Data shows 61/71 timeout trades had MFE < 0.10R — $131 lost
                    if not dead_trade and tt == TRADE_TYPE_INTRADAY and age_sec >= 300:
                        if max_fav_r < 0.10:
                            dead_trade = True
                            kill_reason = "momentum_kill_5m"
                            logger.info(
                                "5MIN MOMENTUM KILL [INTRADAY]: %s %s | %ds with MFE %.2fR < 0.10R — no momentum",
                                ts.symbol, ts.side, int(age_sec), max_fav_r,
                            )

                    # ── 10-MINUTE MOMENTUM KILL (RUNNER) ──
                    # Runners need to show real movement — if nothing after 10min, it's not a runner
                    if not dead_trade and tt == TRADE_TYPE_RUNNER and age_sec >= 600:
                        if max_fav_r < 0.20:
                            dead_trade = True
                            kill_reason = "momentum_kill_10m"
                            logger.info(
                                "10MIN MOMENTUM KILL [RUNNER]: %s %s | %ds with MFE %.2fR < 0.20R — not running",
                                ts.symbol, ts.side, int(age_sec), max_fav_r,
                            )

                    # ── REGIME-AWARE TIMEOUT (stricter in weak regimes) ──
                    regime = getattr(ts, 'metadata', {}).get('regime', '') if isinstance(getattr(ts, 'metadata', None), dict) else ''
                    if not dead_trade and age_sec >= 180:  # after 3 min
                        if regime in ('quiet', 'ranging', '') or not regime:
                            # In quiet/empty regime: kill faster if no MFE
                            if max_fav_r < 0.08 and current_r < -0.10:
                                dead_trade = True
                                kill_reason = "regime_kill_%s" % (regime or "empty")
                                logger.info(
                                    "REGIME KILL [%s]: %s %s | regime=%s, %ds with MFE %.2fR — weak regime, no momentum",
                                    tt, ts.symbol, ts.side, regime or "empty", int(age_sec), max_fav_r,
                                )

                    # ── TIME STOP LOGIC per trade type ──
                    if not dead_trade and time_stop_type == "hard":
                        # SCALP: Hard time stop — kill if not moving after N bars
                        base_time = tt_cfg["time_stop_bars"] * 300  # 5m bars
                        if age_sec >= base_time and current_r < 0.1:
                            dead_trade = True
                            kill_reason = "hard_time_stop"
                        elif age_sec >= max_age:
                            dead_trade = True
                            kill_reason = "max_age"

                    elif not dead_trade and time_stop_type == "soft":
                        # INTRADAY: Soft time stop — P1 FIX: tighter (24 trades = 0% WR, -6.23%)
                        base_time = tt_cfg["time_stop_bars"] * 300  # 5m bars

                        # Never time-stop if meaningfully above entry
                        if current_r >= 0.05:
                            dead_trade = False
                        # If losing and never showed life (tightened: MFE < 0.15R from 0.20)
                        elif age_sec >= base_time and max_fav_r < 0.15 and current_r < -0.15:
                            dead_trade = True
                            kill_reason = "soft_no_progress"
                        # If trade went positive but now retreating hard
                        elif age_sec >= base_time * 0.7 and max_fav_r >= 0.3 and current_r < -0.3:
                            dead_trade = True
                            kill_reason = "soft_retreat"
                        # NEW: if flat too long — no meaningful move after base_time
                        elif age_sec >= base_time and abs(current_r) < 0.08 and max_fav_r < 0.15:
                            dead_trade = True
                            kill_reason = "soft_flat"
                        # Hard backstop (tighter: exit if ANY loss after max_age)
                        elif age_sec >= max_age and current_r < 0:
                            dead_trade = True
                            kill_reason = "max_age"

                    elif not dead_trade and time_stop_type == "none":
                        # RUNNER: No time stop — only hard backstop for safety
                        if age_sec >= max_age and current_r < -1.0:
                            dead_trade = True
                            kill_reason = "runner_max_age"

                    if dead_trade:
                        ts.exit_price = price
                        ts.exit_reason = kill_reason if kill_reason else f"time_stop_{tt.lower()}"
                        ts.exit_time = now_iso
                        ts.pnl_pct = self._calc_pnl(ts, price, self._order_type)
                        ts.time_stop_triggered = True
                        ts.exit_reason_detailed = f"{kill_reason or 'time_stop'}_{tt.lower()}_{int(age_sec/60)}m"
                        ts.status = "expired"
                        to_close.append(tid)
                        logger.info(
                            "TIME STOP [%s]: %s %s | age=%dm | max_fav=%.2fR | current=%.2fR | PnL: %+.2f%%",
                            tt, ts.symbol, ts.side, int(age_sec / 60),
                            max_fav_r, current_r, ts.pnl_pct,
                        )
                        events.append({
                            "type": "time_stop",
                            "signal": ts.to_dict(),
                            "message": (
                                f"TIME STOP [{tt}]: {ts.symbol} {ts.side} @ {price:.2f} | "
                                f"{int(age_sec/60)}min, max {max_fav_r:.2f}R | "
                                f"PnL: {ts.pnl_pct:+.2f}%"
                            ),
                        })
                        continue
                except (ValueError, TypeError):
                    pass

            # -- Scalper timer: SCALP trades only (INTRADAY/RUNNER exempt) --
            # BTC: 27 min window, others: 12 min (tighter than initial 30/15)
            # INTRADAY/RUNNER trades pay closing fee but get more time to capture bigger moves
            _tt = getattr(ts, 'trade_type', TRADE_TYPE_SCALP)
            if _tt == TRADE_TYPE_SCALP:
              try:
                entry_dt_sc = datetime.fromisoformat(ts.entry_time)
                age_sc = (datetime.now(timezone.utc) - entry_dt_sc).total_seconds()
                scalper_window = SCALPER_WINDOW_BTC if "BTC" in ts.symbol else SCALPER_WINDOW_OTHER

                if is_long:
                    sc_r = (price - ts.entry_price) / risk if risk > 0 else 0
                else:
                    sc_r = (ts.entry_price - price) / risk if risk > 0 else 0

                # Phase 0: 5 min before window end → close 70% if any profit (safety net)
                if age_sc >= scalper_window - 300 and age_sc < scalper_window - 120 and ts.status == "active":
                    if sc_r > 0.10:  # any meaningful profit
                        ts.exit_price = price
                        ts.exit_reason = "scalper_early_lock"
                        ts.exit_time = now_iso
                        ts.pnl_pct = self._calc_pnl(ts, price, self._order_type)
                        ts.exit_reason_detailed = f"scalper_early_lock_70pct_{int(scalper_window/60)}m"
                        ts.status = "expired"
                        to_close.append(tid)
                        logger.info(
                            "SCALPER EARLY LOCK 70%%: %s %s | age=%dm/%dm | R=%.2fR | PnL: %+.2f%% (5min warning)",
                            ts.symbol, ts.side, int(age_sc/60), int(scalper_window/60),
                            sc_r, ts.pnl_pct,
                        )
                        events.append({
                            "type": "scalper_early_lock",
                            "signal": ts.to_dict(),
                            "message": f"SCALPER 5MIN WARNING: {ts.symbol} {ts.side} | {int(age_sc/60)}m | R={sc_r:+.2f} | Locked 70%",
                        })
                        continue

                # Phase 1: 2 min before window end → close 75% if profitable
                if age_sc >= scalper_window - 120 and age_sc < scalper_window and ts.status == "active":
                    if sc_r > 0.1:  # in profit
                        # Simulate 75% partial close by adjusting PnL
                        ts.exit_price = price
                        ts.exit_reason = "scalper_partial_75"
                        ts.exit_time = now_iso
                        ts.pnl_pct = self._calc_pnl(ts, price, self._order_type)
                        ts.exit_reason_detailed = f"scalper_partial_75pct_{int(scalper_window/60)}m"
                        ts.status = "expired"
                        to_close.append(tid)
                        logger.info(
                            "SCALPER 75%%: %s %s | age=%dm/%dm | R=%.2fR | PnL: %+.2f%% (75%% partial, free exit)",
                            ts.symbol, ts.side, int(age_sc/60), int(scalper_window/60),
                            sc_r, ts.pnl_pct,
                        )
                        events.append({
                            "type": "scalper_partial",
                            "signal": ts.to_dict(),
                            "message": f"SCALPER 75%: {ts.symbol} {ts.side} | {int(age_sc/60)}m | R={sc_r:+.2f} | PnL: {ts.pnl_pct:+.2f}%",
                        })
                        continue

                # Phase 2: at window end → force close everything (still free exit)
                if age_sc >= scalper_window and ts.status == "active":
                    ts.exit_price = price
                    ts.exit_reason = "scalper_timeout"
                    ts.exit_time = now_iso
                    ts.pnl_pct = self._calc_pnl(ts, price, self._order_type)
                    ts.exit_reason_detailed = f"scalper_timeout_{int(scalper_window/60)}m"
                    ts.status = "expired"
                    to_close.append(tid)
                    logger.info(
                        "SCALPER TIMEOUT: %s %s | age=%dm/%dm | R=%.2fR | PnL: %+.2f%%",
                        ts.symbol, ts.side, int(age_sc/60), int(scalper_window/60),
                        sc_r, ts.pnl_pct,
                    )
                    events.append({
                        "type": "scalper_timeout",
                        "signal": ts.to_dict(),
                        "message": f"SCALPER TIMEOUT: {ts.symbol} {ts.side} | {int(scalper_window/60)}m window | PnL: {ts.pnl_pct:+.2f}%",
                    })
                    continue
              except (ValueError, TypeError):
                pass

            # -- Check expiry — trade-type-aware max age --
            try:
                entry_dt = datetime.fromisoformat(ts.entry_time)
                age = (datetime.now(timezone.utc) - entry_dt).total_seconds()
                _tt_expiry = getattr(ts, 'trade_type', TRADE_TYPE_SCALP)
                _tt_max = TRADE_TYPE_CONFIG.get(_tt_expiry, {}).get("max_age_sec", MAX_SIGNAL_AGE)
                if age > _tt_max and ts.status in ("active", "tp1_hit", "tp2_hit"):
                    ts.exit_price = price
                    ts.exit_reason = "expired"
                    ts.exit_time = now_iso
                    ts.pnl_pct = self._calc_pnl(ts, price, self._order_type)
                    ts.exit_reason_detailed = f"expired_{_tt_expiry.lower()}_{int(age/60)}m"
                    ts.status = "expired"
                    to_close.append(tid)
                    events.append({
                        "type": "expired",
                        "signal": ts.to_dict(),
                        "message": f"EXPIRED: {ts.symbol} {ts.side} @ {price:.2f} | PnL: {ts.pnl_pct:+.2f}%",
                    })
            except (ValueError, TypeError):
                pass

        # Close completed signals + feed outcomes to ML
        for tid in to_close:
            ts = self._active.pop(tid)
            closed_dict = ts.to_dict()
            self._closed.append(closed_dict)

            # Track recently closed for orphan sync (so it gets accurate exit prices)
            self._closed_recently[tid] = {
                "exit_price": ts.exit_price,
                "exit_reason": ts.exit_reason,
                "symbol": ts.symbol,
                "side": ts.side,
            }
            # Cap _closed_recently to last 200 to prevent memory leak
            if len(self._closed_recently) > 200:
                oldest_keys = list(self._closed_recently.keys())[:-200]
                for k in oldest_keys:
                    del self._closed_recently[k]

            # ── ML FEEDBACK: update training dataset with outcome ──
            self._send_ml_feedback(ts)

            # ── ML FEEDBACK BLEND: every 50 trades, append batch to training file ──
            self._live_trade_count = getattr(self, '_live_trade_count', 0) + 1
            if self._live_trade_count % 50 == 0:
                self._trigger_ml_feedback_blend()

        # Persist if anything changed
        if events or to_close:
            self._save_active()
            self._save_closed()
            self._recalc_stats()
            self._save_stats()

        return events

    def get_active_signals(self) -> List[Dict[str, Any]]:
        """Return list of currently active signals."""
        return [ts.to_dict() for ts in self._active.values()]

    def get_closed_signals(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent closed signals — single source of truth.

        Priority: closed_signals.json > in-memory _closed > ml_live_feedback.jsonl
        If closed_signals.json is empty (post-restart), rebuild from feedback file.
        """
        all_closed = []
        # Load from persisted file first
        closed_file = _STORAGE_DIR / "closed_signals.json"
        try:
            if closed_file.exists():
                data = json.loads(closed_file.read_text())
                if isinstance(data, list):
                    all_closed = data
        except Exception:
            pass

        # Add any in-memory signals not yet in file
        existing_ids = {s.get("trade_id") for s in all_closed if isinstance(s, dict)}
        for s in self._closed:
            sid = s.get("trade_id") if isinstance(s, dict) else getattr(s, "trade_id", None)
            if sid and sid not in existing_ids:
                all_closed.append(s if isinstance(s, dict) else s.to_dict() if hasattr(s, "to_dict") else s)

        # Fallback: if no closed signals, rebuild from feedback file (single source of truth)
        if len(all_closed) < 5:
            feedback_file = _STORAGE_DIR / "ml_live_feedback.jsonl"
            try:
                if feedback_file.exists():
                    fb_trades = []
                    with open(feedback_file) as f:
                        for line in f:
                            if line.strip():
                                try:
                                    fb_trades.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                    # Merge: add feedback trades not already in closed
                    for fb in fb_trades:
                        fid = fb.get("trade_id", "")
                        if fid and fid not in existing_ids:
                            all_closed.append(fb)
                            existing_ids.add(fid)
            except Exception:
                pass

        return all_closed[-limit:]

    def get_stats(self) -> Dict[str, Any]:
        """Return current performance statistics."""
        if not self._stats or "paper_start_balance" not in self._stats:
            self._recalc_stats()
        return self._stats.copy()

    @property
    def active_count(self) -> int:
        return len(self._active)

    def set_training_dataset(self, training_dataset) -> None:
        """Wire the training dataset for ML outcome feedback."""
        self._training_dataset = training_dataset
        logger.info("ML feedback wired: trade outcomes → training dataset")

    def _send_ml_feedback(self, ts: TrackedSignal) -> None:
        """Feed trade outcome to ML training dataset + live feedback file.

        Called when every signal closes. Two outputs:
        1. training_dataset.update_outcome() — updates the JSONL entry record
        2. ml_live_feedback.jsonl — append-only per-trade outcomes for ML dashboard
        """
        try:
            # Compute duration
            duration_sec = 0
            try:
                entry_dt = datetime.fromisoformat(ts.entry_time)
                exit_dt = datetime.fromisoformat(ts.exit_time) if ts.exit_time else datetime.now(timezone.utc)
                duration_sec = int((exit_dt - entry_dt).total_seconds())
            except (ValueError, TypeError):
                pass

            # 1. Update training dataset (closes the loop: entry record → outcome)
            if self._training_dataset is not None:
                try:
                    self._training_dataset.update_outcome(
                        trade_id=ts.trade_id,
                        exit_price=ts.exit_price,
                        exit_reason=ts.exit_reason or ts.exit_reason_detailed or "",
                        pnl_pct=ts.pnl_pct,
                        pnl_usd=ts.pnl_usd,
                        r_multiple=ts.exit_r,
                        mae_r=ts.mae_r,
                        mfe_r=ts.mfe_r,
                        tp1_hit=ts.tp1_hit,
                        tp2_hit=ts.tp2_hit,
                        tp3_hit=ts.tp3_hit,
                        breakeven_set=ts.breakeven_set,
                        duration_sec=duration_sec,
                    )
                    logger.debug("ML feedback: updated training record %s", ts.trade_id[:8])
                except Exception as e:
                    logger.warning("ML feedback: training dataset update failed: %s", e)

            # 2. Append to live feedback file (per-pair, per-scanner, per-model)
            # Dedup: skip if trade_id already in file
            existing_ids: set = set()
            try:
                if self._live_feedback_file.exists():
                    with open(self._live_feedback_file) as rf:
                        for line in rf:
                            if line.strip():
                                try:
                                    existing_ids.add(json.loads(line).get("trade_id", ""))
                                except json.JSONDecodeError:
                                    pass
            except Exception:
                pass
            if ts.trade_id in existing_ids:
                logger.debug("ML feedback: skipping duplicate trade_id %s", ts.trade_id[:8])
                return

            meta = ts.metadata if isinstance(ts.metadata, dict) else {}
            feedback = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "trade_id": ts.trade_id,
                "symbol": ts.symbol,
                "side": ts.side,
                "setup_type": ts.setup_type,
                "trade_type": getattr(ts, 'trade_type', 'SCALP'),
                "regime": meta.get("regime", ""),
                "session": meta.get("session", ""),
                "confidence": ts.confidence,
                "grade": ts.grade,
                # ML metadata
                "ml_probability": meta.get("ml_probability", 0.0),
                "ml_verdict": meta.get("ml_verdict", ""),
                "ml_model_version": meta.get("ml_model_version", ""),
                # Entry/exit
                "entry_price": ts.entry_price,
                "exit_price": ts.exit_price,
                "stop_loss": ts.stop_loss,
                "tp1": ts.tp1,
                "tp2": ts.tp2,
                "tp3": ts.tp3,
                # Outcomes
                "exit_reason": ts.exit_reason or ts.exit_reason_detailed or "",
                "pnl_pct": round(ts.pnl_pct, 4),
                "pnl_usd": round(ts.pnl_usd, 4),
                "exit_r": round(ts.exit_r, 4),
                "mae_r": round(ts.mae_r, 4),
                "mfe_r": round(ts.mfe_r, 4),
                "tp1_hit": ts.tp1_hit,
                "tp2_hit": ts.tp2_hit,
                "tp3_hit": ts.tp3_hit,
                "breakeven_set": ts.breakeven_set,
                "duration_sec": duration_sec,
                # Sizing
                "position_size_usd": ts.position_size_usd,
                "leverage": ts.leverage,
                "paper_stake": ts.paper_stake,
                # Fee tracking
                "total_fees_usd": ts.total_fees_usd,
                "within_scalper": ts.within_scalper,
                # Slippage tracking
                "signal_price": ts.signal_price,
                "fill_price": ts.fill_price,
                "slippage_ticks": round(ts.slippage_ticks, 2),
                "slippage_bps": round(ts.slippage_bps, 2),
                "slippage_impact_r": round(ts.slippage_impact_r, 4),
                "order_type": ts.order_type,
                "fill_time_ms": round(ts.fill_time_ms, 1),
                # Mode tracking
                "operating_mode": meta.get("operating_mode", "unknown"),
            }
            with open(self._live_feedback_file, "a") as f:
                f.write(json.dumps(feedback, default=str) + "\n")

            # Rotate feedback file if > 10K lines (keep last 8K)
            try:
                if self._live_feedback_file.exists():
                    with open(self._live_feedback_file) as rf:
                        lines = rf.readlines()
                    if len(lines) > 10000:
                        with open(self._live_feedback_file, "w") as wf:
                            wf.writelines(lines[-8000:])
                        logger.info("Feedback file rotated: %d → 8000 lines", len(lines))
            except Exception:
                pass  # rotation failure is non-critical

            logger.info(
                "ML FEEDBACK: %s %s %s | %s | pnl=%+.2f%% r=%+.2fR mfe=%.2fR | ml=%.2f %s | %s %dm",
                ts.symbol, ts.side, ts.setup_type,
                getattr(ts, 'trade_type', '?'),
                ts.pnl_pct, ts.exit_r, ts.mfe_r,
                meta.get("ml_probability", 0), meta.get("ml_verdict", ""),
                ts.exit_reason or "", duration_sec // 60,
            )

        except Exception as e:
            logger.error("ML feedback failed for %s: %s", ts.trade_id[:8], e)

    def _trigger_ml_feedback_blend(self):
        """Every 50 trades, append live outcomes to ML training dataset."""
        feedback_file = _STORAGE_DIR / "ml_live_feedback_blend.jsonl"
        closed_file = _CLOSED_FILE
        try:
            with open(closed_file) as f:
                signals = json.load(f)
            # Take last 50
            recent = signals[-50:]
            with open(feedback_file, "a") as f:
                for sig in recent:
                    meta = sig.get("metadata", {})
                    feedback = {
                        "symbol": sig.get("symbol"),
                        "scanner": meta.get("setup_type", sig.get("setup_type", "")),
                        "category": meta.get("scanner_category", "unknown"),
                        "side": sig.get("side"),
                        "pnl_pct": sig.get("pnl_pct", 0),
                        "mfe_r": sig.get("mfe_r", 0),
                        "mae_r": sig.get("mae_r", 0),
                        "exit_reason": sig.get("exit_reason"),
                        "confidence": sig.get("confidence", 0),
                        "regime": meta.get("regime", ""),
                        "timestamp": sig.get("exit_time", sig.get("timestamp", "")),
                        "blend_batch": self._live_trade_count,
                    }
                    f.write(json.dumps(feedback, default=str) + "\n")
            logger.info("ML FEEDBACK BLEND: %d trades written (batch #%d)", len(recent), self._live_trade_count)
        except Exception as e:
            logger.warning("ML feedback blend failed: %s", e)

    # ------------------------------------------------------------------
    # Regime-Aware Trailing Stops
    # ------------------------------------------------------------------

    @staticmethod
    def _get_trail_params(regime: str, scanner: str = "", trade_type: str = "") -> dict:
        """Get trailing stop parameters based on regime, scanner, and trade type.

        Returns:
            - trail_atr_mult: ATR multiplier for trail distance
            - tighten_after_bars: bars after TP1 before tightening
            - min_trail_floor_pct: minimum trail as % above breakeven
        """
        # ── Trade type override: use trade_type config as base ──
        tt_cfg = TRADE_TYPE_CONFIG.get(trade_type, {})
        if tt_cfg and trade_type:
            base_trail = tt_cfg.get("trail_atr_mult", 1.0)
        else:
            base_trail = 1.0

        # Regime adjustments (multiplicative on trade type base)
        regime_lower = regime.lower() if regime else ""
        if regime_lower in ("trending_up", "trending_down", "breakout"):
            regime_mult = 1.5   # wider — trends deserve room (was 1.3)
            tighten_after_bars = 10  # more patience in trends (was 8)
            min_trail_floor_pct = 0.15
        elif regime_lower in ("ranging", "sideways"):
            regime_mult = 0.7   # tighter — take profit quickly in ranges
            tighten_after_bars = 4
            min_trail_floor_pct = 0.10
        elif regime_lower in ("volatile", "high_volatility"):
            regime_mult = 1.3   # needs room in volatile markets
            tighten_after_bars = 10
            min_trail_floor_pct = 0.20
        elif regime_lower in ("quiet", "low_volatility"):
            regime_mult = 0.7   # minimal moves — take what you can get
            tighten_after_bars = 3
            min_trail_floor_pct = 0.08
        else:
            regime_mult = 1.0   # default
            tighten_after_bars = 6
            min_trail_floor_pct = 0.12

        # Combine: trade_type_base × regime_adjustment
        trail_atr_mult = base_trail * regime_mult

        # Scanner-specific fine-tuning
        if scanner in ("trend_continuation",):
            trail_atr_mult *= 1.1  # trend setups tend to have bigger moves
        elif scanner in ("bos_choch",):
            trail_atr_mult *= 1.2  # displacement = bigger expected moves (was 1.1)
        elif scanner in ("vwap_mean_revert",):
            trail_atr_mult *= 0.9  # mean-reversion setups: take profit faster
        # structure_bounce + liquidity_sweep: no modifier — let regime/trade_type handle it

        # Trade type adjustments to tighten_after_bars
        if trade_type == TRADE_TYPE_SCALP:
            tighten_after_bars = max(2, tighten_after_bars - 3)  # tighten faster
        elif trade_type == TRADE_TYPE_RUNNER:
            tighten_after_bars = tighten_after_bars + 4  # more patience

        return {
            "trail_atr_mult": round(trail_atr_mult, 2),
            "tighten_after_bars": tighten_after_bars,
            "min_trail_floor_pct": min_trail_floor_pct,
        }

    # ------------------------------------------------------------------
    # P&L calculation
    # ------------------------------------------------------------------

    # Delta Exchange fee schedule
    # Standard fees
    # Delta Exchange India actual rates (base + 18% GST)
    TAKER_FEE_PCT = 0.059    # 0.05% base + 18% GST = 0.059% per side
    MAKER_FEE_PCT = 0.0236   # 0.02% base + 18% GST = 0.0236% per side
    SETTLEMENT_FEE_PCT = 0.059  # 0.05% base + 18% GST = 0.059% on close

    # Scalper offer fees (0% closing fee within window)
    SCALPER_ENTRY_MAKER_PCT = 0.02   # 0.02% maker opening fee
    SCALPER_ENTRY_TAKER_PCT = 0.05   # 0.05% taker opening fee
    SCALPER_EXIT_FEE_PCT = 0.00      # FREE exit within Scalper window

    @staticmethod
    def _calc_pnl(ts: TrackedSignal, exit_price: float, order_type: str = "maker") -> float:
        """Calculate P&L percentage for a signal (gross and net).

        Position split: 35% TP1, 35% TP2, 30% runner

        Fee logic — Scalper-aware (Delta Exchange India actual rates + 18% GST):
        - If trade closes within Scalper window (BTC=27m, ETH/others=12m):
          Entry: 0.0236% (maker+GST) + Exit: 0.00% + Settlement: 0.059%
          Total: 0.0826% round-trip
        - If trade closes OUTSIDE Scalper window:
          Entry: 0.059% (taker+GST) + Exit: 0.059% + Settlement: 0.059%
          Total: 0.177% round-trip
        """
        if ts.entry_price == 0:
            return 0.0

        is_long = ts.side == "long"

        # Calculate P&L for each portion
        def pnl_at(price: float) -> float:
            if is_long:
                return ((price - ts.entry_price) / ts.entry_price) * 100
            else:
                return ((ts.entry_price - price) / ts.entry_price) * 100

        # Use actual locked PnL from partial closes (35/35/30 split)
        if ts.tp1_pnl_locked != 0 or ts.tp2_pnl_locked != 0:
            remaining_pnl = ts.position_remaining_pct * pnl_at(exit_price)
            gross_pct = ts.tp1_pnl_locked + ts.tp2_pnl_locked + remaining_pnl
        elif ts.tp3_hit:
            gross_pct = (0.35 * pnl_at(ts.tp1) +
                         0.35 * pnl_at(ts.tp2) +
                         0.30 * pnl_at(ts.tp3))
        elif ts.tp2_hit:
            gross_pct = (0.35 * pnl_at(ts.tp1) +
                         0.35 * pnl_at(ts.tp2) +
                         0.30 * pnl_at(exit_price))
        elif ts.tp1_hit:
            gross_pct = (0.35 * pnl_at(ts.tp1) +
                         0.65 * pnl_at(exit_price))
        else:
            gross_pct = pnl_at(exit_price)

        # Determine if trade closed within Scalper window
        scalper_window_sec = SCALPER_WINDOW_BTC if "BTC" in ts.symbol else SCALPER_WINDOW_OTHER
        within_scalper = False
        trade_duration_sec = 0
        try:
            entry_dt = datetime.fromisoformat(ts.entry_time)
            if ts.exit_time:
                exit_dt = datetime.fromisoformat(ts.exit_time) if isinstance(ts.exit_time, str) else ts.exit_time
            else:
                exit_dt = datetime.now(timezone.utc)
            trade_duration_sec = (exit_dt - entry_dt).total_seconds()
            within_scalper = trade_duration_sec <= scalper_window_sec
        except (ValueError, TypeError):
            pass

        # Calculate fees based on Scalper eligibility and configured order type
        # order_type is now passed as parameter (supports maker/taker/auto)
        if within_scalper:
            # Scalper offer: configured entry fee + FREE exit (0%) + settlement (0.06%)
            entry_fee = (
                SignalTracker.SCALPER_ENTRY_MAKER_PCT
                if order_type in ("maker", "auto")
                else SignalTracker.SCALPER_ENTRY_TAKER_PCT
            )
            fee_pct = (
                entry_fee                            # 0.02% maker or 0.05% taker
                + SignalTracker.SCALPER_EXIT_FEE_PCT # 0.00% exit (FREE within window)
                + SignalTracker.SETTLEMENT_FEE_PCT   # 0.06% settlement
            )
            ts.fee_type = "scalper"
        else:
            # Standard fees: configured entry fee + taker exit + settlement
            entry_fee = (
                SignalTracker.MAKER_FEE_PCT
                if order_type == "maker"
                else SignalTracker.TAKER_FEE_PCT
            )
            fee_pct = (
                entry_fee                            # 0.0236% maker or 0.059% taker
                + SignalTracker.TAKER_FEE_PCT        # 0.059% exit (always taker for stops)
                + SignalTracker.SETTLEMENT_FEE_PCT   # 0.059% settlement
            )
            ts.fee_type = "standard"

        ts.trade_duration_sec = trade_duration_sec
        ts.scalper_window_sec = scalper_window_sec
        ts.within_scalper = within_scalper

        # Net PnL = Gross PnL - fees
        net_pct = gross_pct - fee_pct

        # Store gross values
        ts.gross_pnl_pct = round(gross_pct, 4)
        ts.gross_pnl_usd = round(ts.position_size_usd * gross_pct / 100, 2)

        # Store fee values
        ts.total_fees_pct = round(fee_pct, 4)
        ts.total_fees_usd = round(ts.position_size_usd * fee_pct / 100, 2)

        # Store net values (the "official" PnL)
        ts.pnl_usd = round(ts.position_size_usd * net_pct / 100, 2)

        # Calculate exit R-multiple: fee-adjusted net P&L in risk units
        if ts.initial_risk > 0:
            # Fee impact in price terms
            fee_impact = ts.entry_price * fee_pct / 100  # fee as price distance

            if is_long:
                raw_r = (exit_price - ts.entry_price - fee_impact) / ts.initial_risk
            else:
                raw_r = (ts.entry_price - exit_price - fee_impact) / ts.initial_risk

            def r_at(price: float) -> float:
                if is_long:
                    return (price - ts.entry_price - fee_impact) / ts.initial_risk
                return (ts.entry_price - price - fee_impact) / ts.initial_risk

            # For partial exits (35/35/30 split), use weighted R
            if ts.tp3_hit:
                r_val = 0.35 * r_at(ts.tp1) + 0.35 * r_at(ts.tp2) + 0.30 * raw_r
            elif ts.tp2_hit:
                r_val = 0.35 * r_at(ts.tp1) + 0.35 * r_at(ts.tp2) + 0.30 * raw_r
            elif ts.tp1_hit:
                r_val = 0.35 * r_at(ts.tp1) + 0.65 * raw_r
            else:
                r_val = raw_r
            ts.exit_r = round(r_val, 4)
        else:
            ts.exit_r = 0.0

        return net_pct

    # ------------------------------------------------------------------
    # Fee Stress Testing
    # ------------------------------------------------------------------

    @staticmethod
    def get_min_viable_move(
        symbol: str,
        position_usd: float,
        leverage: float,
        sl_distance_pct: float = 0.5,
        within_scalper: bool = True,
        order_type: str = "maker",
    ) -> dict:
        """Calculate minimum price move needed to break even after all costs.

        Args:
            symbol: Trading pair (e.g. "BTC/USDT")
            position_usd: Notional position size in USD
            leverage: Effective leverage
            sl_distance_pct: Stop-loss distance as % of entry price
            within_scalper: Whether trade will close within Scalper window

        Returns:
            dict with:
            - min_move_pct: minimum % move to break even
            - min_move_usd: dollar equivalent of that move
            - fee_drag_r: fees expressed as R-multiple (fraction of risk going to fees)
            - viable: bool (True if fee_drag_r < 0.3)
            - breakdown: dict of individual cost components
        """
        # Slippage estimate (simplified: base + size impact + liquidity)
        coin = symbol.split("/")[0].upper() if "/" in symbol else symbol[:3].upper()
        liq_factors = {
            "BTC": 1.0, "ETH": 1.0,
            "SOL": 1.5, "AVAX": 1.5, "LINK": 1.5, "XRP": 1.5, "ADA": 1.5,
            "DOGE": 2.5, "SHIB": 2.5, "PEPE": 2.5, "WIF": 2.5, "BONK": 2.5,
        }
        liq = liq_factors.get(coin, 1.5)

        # Entry slippage (maker for scalper)
        entry_base_slip = 0.02 if within_scalper else 0.05
        excess = max(0, position_usd - 500.0)
        entry_slip = (entry_base_slip + (excess / 1000.0) * 0.01) * liq
        entry_slip = min(entry_slip, 0.15)

        # Exit slippage (taker/market)
        exit_slip = (0.05 + (excess / 1000.0) * 0.01) * liq
        exit_slip = min(exit_slip, 0.15)

        if within_scalper:
            # Scalper offer: configured entry fee + FREE exit + settlement
            entry_fee = (
                SignalTracker.SCALPER_ENTRY_MAKER_PCT
                if order_type in ("maker", "auto")
                else SignalTracker.SCALPER_ENTRY_TAKER_PCT
            )
            exit_fee = SignalTracker.SCALPER_EXIT_FEE_PCT   # 0% — free within window
            settlement = SignalTracker.SETTLEMENT_FEE_PCT
        else:
            # Standard: configured entry fee + taker exit + settlement
            entry_fee = (
                SignalTracker.MAKER_FEE_PCT
                if order_type == "maker"
                else SignalTracker.TAKER_FEE_PCT
            )
            exit_fee = SignalTracker.TAKER_FEE_PCT  # exits are always market/taker
            settlement = SignalTracker.SETTLEMENT_FEE_PCT

        total_fees_pct = entry_fee + exit_fee + settlement + entry_slip + exit_slip
        min_move_pct = total_fees_pct
        min_move_usd = position_usd * min_move_pct / 100.0

        # Fee drag in R-multiples: what fraction of 1R goes to fees
        fee_drag_r = (min_move_pct / sl_distance_pct) if sl_distance_pct > 0 else 999.0

        # Viable if fees < 30% of risk
        viable = fee_drag_r < 0.3

        return {
            "min_move_pct": round(min_move_pct, 4),
            "min_move_usd": round(min_move_usd, 2),
            "fee_drag_r": round(fee_drag_r, 4),
            "viable": viable,
            "breakdown": {
                "entry_fee_pct": entry_fee,
                "exit_fee_pct": exit_fee,
                "settlement_pct": settlement,
                "entry_slip_pct": round(entry_slip, 4),
                "exit_slip_pct": round(exit_slip, 4),
                "total_pct": round(total_fees_pct, 4),
            },
        }

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def _recalc_stats(self) -> None:
        """Recalculate performance stats from closed signals."""
        if not self._closed:
            self._stats = {
                "total_signals": len(self._active),
                "active": len(self._active),
                "closed": 0,
                "wins": 0,
                "losses": 0,
                "partial_wins": 0,
                "win_rate": 0.0,
                "avg_win_pnl": 0.0,
                "avg_loss_pnl": 0.0,
                "total_pnl": 0.0,
                "profit_factor": 0.0,
                "best_trade": 0.0,
                "worst_trade": 0.0,
                "tp1_rate": 0.0,
                "tp2_rate": 0.0,
                "tp3_rate": 0.0,
                "by_setup": {},
                "by_symbol": {},
            }
            return

        wins = []
        losses = []
        partial_wins = []
        tp1_count = 0
        tp2_count = 0
        tp3_count = 0
        total = len(self._closed)

        by_setup: Dict[str, Dict] = {}
        by_symbol: Dict[str, Dict] = {}

        # R-metric accumulators
        all_r_values: List[float] = []
        all_mae: List[float] = []
        all_mfe: List[float] = []

        for c in self._closed:
            pnl = c.get("pnl_pct", 0)
            status = c.get("status", "")
            setup = c.get("setup_type", "unknown")
            symbol = c.get("symbol", "unknown")
            exit_r = c.get("exit_r", 0.0)
            mae_r = c.get("mae_r", 0.0)
            mfe_r = c.get("mfe_r", 0.0)

            # Backfill R for old trades that don't have it
            if exit_r == 0 and c.get("initial_risk", 0) == 0:
                entry = c.get("entry_price", 0)
                sl = c.get("stop_loss", 0)
                ep = c.get("exit_price", 0)
                if entry > 0 and sl > 0 and ep > 0:
                    ir = abs(entry - sl)
                    if ir > 0:
                        if c.get("side", "long") == "long":
                            exit_r = (ep - entry) / ir
                        else:
                            exit_r = (entry - ep) / ir
                        exit_r = round(exit_r, 4)

            all_r_values.append(exit_r)
            all_mae.append(mae_r)
            all_mfe.append(mfe_r)

            # Win/loss classification
            if pnl > 0:
                wins.append(pnl)
            elif pnl < 0:
                losses.append(pnl)

            if status == "partial_win":
                partial_wins.append(pnl)

            if c.get("tp1_hit"):
                tp1_count += 1
            if c.get("tp2_hit"):
                tp2_count += 1
            if c.get("tp3_hit"):
                tp3_count += 1

            # Per-setup stats (with R-metrics)
            if setup not in by_setup:
                by_setup[setup] = {
                    "total": 0, "wins": 0, "pnl": 0.0,
                    "r_values": [], "mae_values": [], "mfe_values": [],
                }
            by_setup[setup]["total"] += 1
            if pnl > 0:
                by_setup[setup]["wins"] += 1
            by_setup[setup]["pnl"] += pnl
            by_setup[setup]["r_values"].append(exit_r)
            by_setup[setup]["mae_values"].append(mae_r)
            by_setup[setup]["mfe_values"].append(mfe_r)

            # Per-symbol stats
            if symbol not in by_symbol:
                by_symbol[symbol] = {"total": 0, "wins": 0, "pnl": 0.0}
            by_symbol[symbol]["total"] += 1
            if pnl > 0:
                by_symbol[symbol]["wins"] += 1
            by_symbol[symbol]["pnl"] += pnl

        # Calculate win rates + R-metrics per setup
        for setup_data in by_setup.values():
            n = setup_data["total"]
            setup_data["win_rate"] = round(
                (setup_data["wins"] / n * 100) if n else 0, 1
            )
            setup_data["pnl"] = round(setup_data["pnl"], 2)

            # R-metrics for this scanner
            r_vals = setup_data.pop("r_values")
            mae_vals = setup_data.pop("mae_values")
            mfe_vals = setup_data.pop("mfe_values")

            setup_data["avg_r"] = round(sum(r_vals) / len(r_vals), 4) if r_vals else 0.0
            setup_data["total_r"] = round(sum(r_vals), 4)
            win_r = [r for r in r_vals if r > 0]
            loss_r = [r for r in r_vals if r < 0]
            setup_data["avg_win_r"] = round(sum(win_r) / len(win_r), 4) if win_r else 0.0
            setup_data["avg_loss_r"] = round(sum(loss_r) / len(loss_r), 4) if loss_r else 0.0
            setup_data["best_r"] = round(max(r_vals), 4) if r_vals else 0.0
            setup_data["worst_r"] = round(min(r_vals), 4) if r_vals else 0.0
            setup_data["avg_mae_r"] = round(sum(mae_vals) / len(mae_vals), 4) if mae_vals else 0.0
            setup_data["avg_mfe_r"] = round(sum(mfe_vals) / len(mfe_vals), 4) if mfe_vals else 0.0

            # Expectancy = (WR × avg_win_R) - (LR × avg_loss_R)
            wr_frac = setup_data["wins"] / n if n else 0
            lr_frac = 1 - wr_frac
            setup_data["expectancy_r"] = round(
                wr_frac * setup_data["avg_win_r"] + lr_frac * setup_data["avg_loss_r"], 4
            )

        for sym in by_symbol.values():
            sym["win_rate"] = round(
                (sym["wins"] / sym["total"] * 100) if sym["total"] else 0, 1
            )
            sym["pnl"] = round(sym["pnl"], 2)

        win_count = len(wins)
        loss_count = len(losses)
        total_wins_pnl = sum(wins)
        total_losses_pnl = abs(sum(losses))

        # Dollar P&L from paper trades (net after fees)
        total_pnl_usd = sum(c.get("pnl_usd", 0) for c in self._closed)
        total_gross_pnl_usd = sum(c.get("gross_pnl_usd", c.get("pnl_usd", 0)) for c in self._closed)
        total_fees_usd = sum(c.get("total_fees_usd", 0) for c in self._closed)
        # Paper mode: start at $1000 + cumulative PnL
        # Live mode: use real exchange balance
        paper_balance = self._paper_start_balance + total_pnl_usd

        # Active positions unrealized value
        active_positions_usd = sum(ts.position_size_usd for ts in self._active.values())

        self._stats = {
            "total_signals": total + len(self._active),
            "active": len(self._active),
            "closed": total,
            "wins": win_count,
            "losses": loss_count,
            "partial_wins": len(partial_wins),
            "win_rate": round((win_count / total * 100) if total else 0, 1),
            "avg_win_pnl": round((total_wins_pnl / win_count) if win_count else 0, 3),
            "avg_loss_pnl": round((sum(losses) / loss_count) if loss_count else 0, 3),
            "total_pnl": round(sum(w for w in wins) + sum(l for l in losses), 3),
            "profit_factor": round(
                (total_wins_pnl / total_losses_pnl) if total_losses_pnl else float("inf"), 2
            ),
            "best_trade": round(max(wins) if wins else 0, 3),
            "worst_trade": round(min(losses) if losses else 0, 3),
            "tp1_rate": round((tp1_count / total * 100) if total else 0, 1),
            "tp2_rate": round((tp2_count / total * 100) if total else 0, 1),
            "tp3_rate": round((tp3_count / total * 100) if total else 0, 1),
            "by_setup": by_setup,
            "by_symbol": by_symbol,
            # Paper trading stats (net = after fees)
            "paper_balance": round(paper_balance, 2),
            "paper_pnl_usd": round(total_pnl_usd, 2),         # NET PnL (after fees)
            "paper_gross_pnl_usd": round(total_gross_pnl_usd, 2),  # GROSS PnL (before fees)
            "paper_total_fees_usd": round(total_fees_usd, 2),  # Total fees paid
            "active_positions_usd": round(active_positions_usd, 2),
            "exchange_balance": self._exchange_balance,         # Real exchange balance (for live mode)
            "paper_start_balance": self._paper_start_balance,  # Paper starting capital
            "is_paper_mode": True,  # TODO: read from mode_manager when live
            "paper_stake_per_trade": 100.0,  # max $100, min $50 (fee-viable sizing)
            # Daily P&L breakdown
            "daily_pnl": self._calc_daily_pnl(),
            "fee_schedule": {
                "taker_pct": self.TAKER_FEE_PCT,
                "settlement_pct": self.SETTLEMENT_FEE_PCT,
                "round_trip_standard_pct": self.TAKER_FEE_PCT * 2 + self.SETTLEMENT_FEE_PCT,
                "round_trip_scalper_pct": self.SCALPER_ENTRY_MAKER_PCT + self.SCALPER_EXIT_FEE_PCT + self.SETTLEMENT_FEE_PCT,
                "scalper_window_btc_min": SCALPER_WINDOW_BTC // 60,
                "scalper_window_other_min": SCALPER_WINDOW_OTHER // 60,
            },
            # R-Multiple metrics (global)
            "r_metrics": self._calc_global_r_metrics(all_r_values, all_mae, all_mfe, win_count, total),
        }

    def _calc_daily_pnl(self) -> Dict[str, Any]:
        """Calculate daily P&L breakdown from closed signals."""
        daily: Dict[str, Dict[str, float]] = {}
        for c in self._closed:
            meta = c if isinstance(c, dict) else {}
            # Get close timestamp
            ts = meta.get("closed_at", meta.get("exit_time", meta.get("timestamp", "")))
            if not ts:
                continue
            day = str(ts)[:10]  # YYYY-MM-DD
            if day not in daily:
                daily[day] = {"trades": 0, "wins": 0, "gross_pnl": 0.0, "fees": 0.0, "net_pnl": 0.0}
            daily[day]["trades"] += 1
            pnl = meta.get("pnl_usd", 0) or 0
            gross = meta.get("gross_pnl_usd", pnl) or pnl
            fees = meta.get("total_fees_usd", 0) or 0
            daily[day]["net_pnl"] = round(daily[day]["net_pnl"] + pnl, 2)
            daily[day]["gross_pnl"] = round(daily[day]["gross_pnl"] + gross, 2)
            daily[day]["fees"] = round(daily[day]["fees"] + fees, 2)
            if pnl > 0:
                daily[day]["wins"] += 1
        # Add win rate per day
        for d in daily.values():
            d["wr"] = round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0.0
        return daily

    @staticmethod
    def _calc_global_r_metrics(
        r_values: List[float], mae_values: List[float],
        mfe_values: List[float], win_count: int, total: int,
    ) -> Dict[str, Any]:
        """Calculate global R-multiple performance metrics."""
        if not r_values:
            return {
                "avg_r": 0.0, "total_r": 0.0, "expectancy_r": 0.0,
                "avg_win_r": 0.0, "avg_loss_r": 0.0,
                "best_r": 0.0, "worst_r": 0.0,
                "avg_mae_r": 0.0, "avg_mfe_r": 0.0,
                "edge_ratio": 0.0, "r_std": 0.0,
            }

        win_r = [r for r in r_values if r > 0]
        loss_r = [r for r in r_values if r < 0]
        avg_r = sum(r_values) / len(r_values)
        avg_win = sum(win_r) / len(win_r) if win_r else 0.0
        avg_loss = sum(loss_r) / len(loss_r) if loss_r else 0.0

        # Expectancy = (WR × avg_win_R) + (LR × avg_loss_R)
        wr_frac = win_count / total if total else 0
        lr_frac = 1 - wr_frac
        expectancy = wr_frac * avg_win + lr_frac * avg_loss

        # Edge ratio = avg MFE / avg MAE (>1 means winners run further than losers dip)
        avg_mae = sum(mae_values) / len(mae_values) if mae_values else 0.0
        avg_mfe = sum(mfe_values) / len(mfe_values) if mfe_values else 0.0
        edge_ratio = avg_mfe / avg_mae if avg_mae > 0 else 0.0

        # R standard deviation (consistency measure)
        if len(r_values) > 1:
            mean_r = sum(r_values) / len(r_values)
            variance = sum((r - mean_r) ** 2 for r in r_values) / (len(r_values) - 1)
            r_std = variance ** 0.5
        else:
            r_std = 0.0

        # Exit efficiency: how much of MFE we actually captured
        exit_efficiency = (avg_r / avg_mfe * 100) if avg_mfe > 0 else 0.0

        return {
            "total": len(r_values),
            "avg_r": round(avg_r, 4),
            "total_r": round(sum(r_values), 4),
            "expectancy_r": round(expectancy, 4),
            "avg_win_r": round(avg_win, 4),
            "avg_loss_r": round(avg_loss, 4),
            "best_r": round(max(r_values), 4),
            "worst_r": round(min(r_values), 4),
            "avg_mae_r": round(avg_mae, 4),
            "avg_mfe_r": round(avg_mfe, 4),
            "exit_efficiency": round(exit_efficiency, 1),
            "edge_ratio": round(edge_ratio, 4),
            "r_std": round(r_std, 4),
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load active and closed signals from disk."""
        try:
            if _ACTIVE_FILE.exists():
                raw = json.loads(_ACTIVE_FILE.read_text())
                # Handle both list format and dict format (legacy/corrupted)
                if isinstance(raw, dict):
                    # Dict format: {trade_id: signal_dict, ...}
                    data = list(raw.values()) if raw else []
                    logger.warning("Active signals file was dict format — converting to list (%d entries)", len(data))
                elif isinstance(raw, list):
                    data = raw
                else:
                    data = []
                for d in data:
                    if isinstance(d, dict):
                        ts = TrackedSignal.from_dict(d)
                        self._active[ts.trade_id] = ts
                logger.info("Loaded %d active tracked signals", len(self._active))
        except Exception as exc:
            logger.warning("Failed to load active signals: %s", exc)

        try:
            if _CLOSED_FILE.exists():
                raw_closed = json.loads(_CLOSED_FILE.read_text())
                # Handle corrupted format: if dict, convert to list
                if isinstance(raw_closed, dict):
                    self._closed = list(raw_closed.values()) if raw_closed else []
                    logger.warning("Closed signals file was dict format — converting to list (%d entries)", len(self._closed))
                elif isinstance(raw_closed, list):
                    self._closed = raw_closed
                else:
                    self._closed = []
                # Auto-fix exit reasons on load: reclassify profitable "stop_loss" as trail_profit
                fixed = 0
                for t in self._closed:
                    if t.get("exit_reason") != "stop_loss":
                        continue
                    entry = t.get("entry_price", 0)
                    sl = t.get("stop_loss", 0)
                    side = t.get("side", "")
                    is_profit = (side == "long" and sl > entry) or (side == "short" and sl < entry)
                    if t.get("tp1_hit"):
                        t["exit_reason"] = "partial_win"
                        t["status"] = "partial_win"
                        fixed += 1
                    elif is_profit:
                        t["exit_reason"] = "trail_profit"
                        t["status"] = "trail_win"
                        fixed += 1
                    elif t.get("exit_r", -999) > 0:
                        t["exit_reason"] = "trail_profit"
                        t["status"] = "trail_win"
                        fixed += 1
                if fixed:
                    _CLOSED_FILE.write_text(json.dumps(self._closed, indent=1))
                    logger.info("Auto-fixed %d exit reasons (stop_loss → trail_profit/partial_win)", fixed)

                # ── AUTO-DEDUP: Remove duplicate entries (same trade_id) ──
                seen_keys = set()
                deduped = []
                for t in self._closed:
                    key = t.get("trade_id", f"{t.get('symbol','')}_{t.get('side','')}_{t.get('entry_price',0)}_{t.get('timestamp','')}")
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    deduped.append(t)
                removed = len(self._closed) - len(deduped)
                if removed > 0:
                    self._closed = deduped
                    _CLOSED_FILE.write_text(json.dumps(self._closed, indent=1))
                    logger.info("Auto-deduped: removed %d duplicate trades, %d remaining", removed, len(self._closed))

                # ── AUTO-CLEAN: Remove dead trades (MFE=0, time_stop) ──
                cleaned = [t for t in self._closed if not (
                    t.get("mfe_r", 0) <= 0.01
                    and t.get("pnl_pct", 0) < 0
                    and t.get("exit_reason", "") == "time_stop_dead_trade"
                )]
                dead_removed = len(self._closed) - len(cleaned)
                if dead_removed > 0:
                    self._closed = cleaned
                    _CLOSED_FILE.write_text(json.dumps(self._closed, indent=1))
                    logger.info("Auto-cleaned: removed %d dead trades (MFE=0), %d remaining", dead_removed, len(self._closed))

                logger.info("Loaded %d closed tracked signals", len(self._closed))
        except Exception as exc:
            logger.warning("Failed to load closed signals: %s", exc)

        try:
            if _STATS_FILE.exists():
                self._stats = json.loads(_STATS_FILE.read_text())
        except Exception:
            pass

    @staticmethod
    def _safe_write(path: Path, data: str) -> None:
        """Write-then-rename for crash-safe file persistence."""
        try:
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                os.write(fd, data.encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, str(path))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

    def _save_active(self) -> None:
        try:
            data = [ts.to_dict() for ts in self._active.values()]
            self._safe_write(_ACTIVE_FILE, json.dumps(data, indent=1))
        except Exception as exc:
            logger.warning("Failed to save active signals: %s", exc)

    def _save_closed(self) -> None:
        try:
            # Keep last 1000 closed signals
            self._closed = self._closed[-1000:]
            self._safe_write(_CLOSED_FILE, json.dumps(self._closed, indent=1))
        except Exception as exc:
            logger.warning("Failed to save closed signals: %s", exc)

    def _save_stats(self) -> None:
        try:
            self._safe_write(_STATS_FILE, json.dumps(self._stats, indent=1))
        except Exception as exc:
            logger.warning("Failed to save stats: %s", exc)
