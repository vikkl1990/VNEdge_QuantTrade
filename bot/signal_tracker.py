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
SCALPER_WINDOW_BTC = 30 * 60   # 30 minutes — BTC Scalper offer window
SCALPER_WINDOW_OTHER = 15 * 60  # 15 minutes — all other futures


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
            cs = 0.001 if "BTC" in sym else 0.01
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
        RISK_PCT = 0.75        # risk 0.75% per trade
        risk_amount = ACCOUNT_SIZE * RISK_PCT / 100  # $7.50 risk per trade

        # Position size = risk / SL_distance
        # If SL is 0.5% away, position = $7.50 / 0.005 = $1500
        # If SL is 1.0% away, position = $7.50 / 0.01 = $750
        if sl_dist_pct > 0:
            position_usd = risk_amount / (sl_dist_pct / 100)
        else:
            position_usd = risk_amount * 100  # fallback

        # ── SUPER SCALP LEVERAGE (20x-100x, $50-$100 margin) ──
        # Aggressive leverage for high-confidence scalps
        # Liquidation safety checked separately in strategy
        if confidence >= 95:
            max_lev = 100
            paper_stake = 100.0
            lev_cap_source = "super_scalp_95+_100x"
        elif confidence >= 90:
            max_lev = 75
            paper_stake = 100.0
            lev_cap_source = "super_scalp_90+_75x"
        elif confidence >= 85:
            max_lev = 50
            paper_stake = 75.0
            lev_cap_source = "super_scalp_85+_50x"
        elif confidence >= 80:
            max_lev = 40
            paper_stake = 75.0
            lev_cap_source = "super_scalp_80+_40x"
        elif confidence >= 75:
            max_lev = 30
            paper_stake = 50.0
            lev_cap_source = "super_scalp_75+_30x"
        elif confidence >= 65:
            max_lev = 25
            paper_stake = 50.0
            lev_cap_source = "super_scalp_65+_25x"
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
        if "BTC" in symbol.upper():
            contract_sz = 0.001   # 1 contract = 0.001 BTC
        elif "ETH" in symbol.upper():
            contract_sz = 0.01    # 1 contract = 0.01 ETH
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

        return cls(
            trade_id=sig.get("trade_id", ""),
            symbol=sig.get("symbol", ""),
            side=sig.get("side", "long"),
            entry_price=entry,
            stop_loss=sl,
            tp1=float(tps[0]) if len(tps) > 0 else 0.0,
            tp2=float(tps[1]) if len(tps) > 1 else 0.0,
            tp3=float(tps[2]) if len(tps) > 2 else 0.0,
            confidence=confidence,
            grade=str(sig.get("grade", "")),
            setup_type=meta.get("setup_type", ""),
            strategy_type=meta.get("strategy_type", "scalp"),
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
            entry_time=sig.get("timestamp", datetime.now(timezone.utc).isoformat()),
            highest_price=entry,
            lowest_price=entry,
        )


class SignalTracker:
    """Tracks open signals for TP/SL closure and maintains P&L + win rate stats."""

    def __init__(self) -> None:
        _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._active: Dict[str, TrackedSignal] = {}  # trade_id -> TrackedSignal
        self._closed: List[Dict[str, Any]] = []
        self._stats: Dict[str, Any] = {}
        self._lock = asyncio.Lock()  # protects _active/_closed state mutations
        self._exchange_balance: Optional[float] = None  # real exchange purse balance
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
        ts = TrackedSignal.from_signal(signal_dict)
        if not ts.entry_price or not ts.stop_loss:
            logger.warning("Cannot track signal %s: missing entry/SL", ts.trade_id)
            return
        if ts.trade_id in self._active:
            return  # already tracking

        # ── DUPLICATE PREVENTION: max 1 per symbol+side (active) ──
        for existing in self._active.values():
            if existing.symbol == ts.symbol and existing.side == ts.side:
                logger.info(
                    "DUPLICATE BLOCKED (active): %s %s %s — already have %s open",
                    ts.trade_id[:8], ts.symbol, ts.side, existing.trade_id[:8],
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
                        except:
                            pass
        except:
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
        """
        events = []
        to_close = []

        for tid, ts in self._active.items():
            price = prices.get(ts.symbol)
            if price is None:
                continue

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

            # -- Early Invalidation Exit: Hard Loss Cap (-2R) --
            # Force close if adverse excursion exceeds 2R (gap/slippage beyond SL)
            if ts.initial_risk > 0:
                if is_long:
                    current_adverse_r = (ts.entry_price - price) / ts.initial_risk
                else:
                    current_adverse_r = (price - ts.entry_price) / ts.initial_risk
                if current_adverse_r >= 2.0:
                    ts.exit_price = price
                    ts.exit_reason = "hard_loss_cap"
                    ts.exit_time = now_iso
                    ts.exit_reason_detailed = "hard_loss_cap_2r"
                    ts.status = "stopped"
                    ts.pnl_pct = self._calc_pnl(ts, price)
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

            # -- FEE-AWARE Profit Protection --
            # Only protect at levels that are NET profitable after 0.18% fees
            # Minimum profitable exit = 0.76R (covers fees with margin)
            # REMOVED: 0.3R and 0.15R protection — both net negative
            if ts.initial_risk > 0 and ts.mfe_r >= 1.0:
                if is_long:
                    current_r = (price - ts.entry_price) / ts.initial_risk
                else:
                    current_r = (ts.entry_price - price) / ts.initial_risk

                profit_protect = False
                # Only protect if we can lock at least 0.8R (net positive)
                if ts.mfe_r >= 1.5 and current_r <= 0.8:
                    profit_protect = True
                    exit_reason_tag = "profit_protect_15R"
                    exit_detail = f"Profit protect: MFE {ts.mfe_r:.2f}R → {current_r:.2f}R (locked +0.8R)"
                elif ts.mfe_r >= 1.0 and current_r <= -0.5:
                    # Had 1.0R profit but now losing — momentum collapse
                    profit_protect = True
                    exit_reason_tag = "momentum_collapse"
                    exit_detail = f"Momentum collapse: MFE {ts.mfe_r:.2f}R → {current_r:.2f}R"

                if profit_protect:
                    ts.exit_price = price
                    ts.exit_reason = exit_reason_tag
                    ts.exit_time = now_iso
                    ts.exit_reason_detailed = exit_reason_tag
                    ts.status = "stopped" if current_r <= 0 else "partial_win"
                    ts.pnl_pct = self._calc_pnl(ts, price)
                    to_close.append(tid)
                    events.append({
                        "type": exit_reason_tag,
                        "signal": ts.to_dict(),
                        "message": (
                            f"PROFIT PROTECT: {ts.symbol} {ts.side} @ {price:.2f} | "
                            f"{exit_detail} | PnL: {ts.pnl_pct:+.2f}%"
                        ),
                    })
                    logger.info(
                        "Profit protect: %s %s @ %.2f | %s | PnL: %.2f%%",
                        ts.symbol, ts.side, price, exit_detail, ts.pnl_pct,
                    )
                    continue

            # -- Check Stop Loss --
            sl_hit = (price <= ts.stop_loss) if is_long else (price >= ts.stop_loss)
            if sl_hit and not ts.sl_hit:
                ts.sl_hit = True
                ts.exit_price = price
                ts.exit_time = now_iso
                ts.pnl_pct = self._calc_pnl(ts, price)
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

                # FEE-AWARE trail — minimum lock must exceed fees (0.76R)
                # Any exit below 0.76R is NET NEGATIVE after 0.18% round-trip fees
                # Data: trail at 0.3R = +0.106% gross - 0.180% fee = -0.074% NET LOSS
                # Only trail at 1.0R+ where exit is genuinely profitable
                trail_levels = [
                    (3.0, 2.5),   # 83% locked
                    (2.5, 2.0),   # 80% locked
                    (2.0, 1.6),   # 80% locked
                    (1.5, 1.2),   # 80% locked
                    (1.0, 0.8),   # 80% locked — MINIMUM profitable trail
                    # REMOVED: 0.7/0.5/0.3 trails — all net-negative after fees
                ]

                for trigger_r, lock_r in trail_levels:
                    if current_r_trail >= trigger_r:
                        # Lock at least this much profit
                        lock_dist = ts.initial_risk * lock_r
                        # Also ensure we cover fees (0.28% of entry)
                        fee_cover = ts.entry_price * 0.0028
                        lock_dist = max(lock_dist, fee_cover)

                        if is_long:
                            new_sl = ts.entry_price + lock_dist
                        else:
                            new_sl = ts.entry_price - lock_dist

                        # Only tighten, never widen
                        should_update = (
                            (is_long and new_sl > ts.stop_loss) or
                            (not is_long and new_sl < ts.stop_loss)
                        )
                        if should_update:
                            ts.stop_loss = new_sl
                            if not ts.breakeven_set:
                                ts.breakeven_set = True
                            logger.info(
                                "TRAIL: %s %s @ %.2f | +%.2fR → lock +%.1fR | SL → %.2f",
                                ts.symbol, ts.side, price, current_r_trail, lock_r, ts.stop_loss,
                            )
                        break  # only apply highest matching level

            # -- Check TP levels (in order) --
            if not ts.tp1_hit and ts.tp1:
                tp1_hit = (price >= ts.tp1) if is_long else (price <= ts.tp1)
                if tp1_hit:
                    ts.tp1_hit = True
                    ts.tp1_time = now_iso
                    ts.status = "tp1_hit"

                    # Book partial profit: 60% of position at TP1
                    if is_long:
                        tp1_pnl = ((price - ts.entry_price) / ts.entry_price) * 100
                    else:
                        tp1_pnl = ((ts.entry_price - price) / ts.entry_price) * 100
                    ts.tp1_pnl_locked = round(0.35 * tp1_pnl, 4)  # 35% at TP1
                    ts.position_remaining_pct = 0.65

                    # Start trailing at 1.0× ATR (earlier than waiting for TP2)
                    atr_trail_dist = ts.signal_atr * 1.0 if ts.signal_atr > 0 else abs(ts.tp1 - ts.entry_price) * 0.5
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

                    # Tighten ATR trail to 0.8× ATR (runner protection)
                    atr_trail_dist = ts.signal_atr * 0.8 if ts.signal_atr > 0 else abs(ts.tp2 - ts.tp1) * 0.3
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
                    ts.pnl_pct = self._calc_pnl(ts, price)
                    ts.exit_reason_detailed = "tp3_full_win"
                    ts.status = "tp3_hit"
                    to_close.append(tid)
                    events.append({
                        "type": "tp3_hit",
                        "signal": ts.to_dict(),
                        "message": f"TP3 FULL WIN: {ts.symbol} {ts.side} @ {price:.2f} | PnL: {ts.pnl_pct:+.2f}%",
                    })

            # -- ATR TRAILING STOP RATCHET (after TP1 or TP2) --
            # Trail distance tightens as TPs are hit:
            #   After TP1: 1.0× ATR (protecting 40% remaining)
            #   After TP2: 0.8× ATR (protecting 15% runner)
            if ts.atr_trail_active and ts.tp1_hit and not ts.tp3_hit:
                if ts.tp2_hit:
                    atr_trail_dist = ts.signal_atr * 0.8 if ts.signal_atr > 0 else abs(ts.tp2 - ts.tp1) * 0.3
                else:
                    atr_trail_dist = ts.signal_atr * 1.0 if ts.signal_atr > 0 else abs(ts.tp1 - ts.entry_price) * 0.5
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
                        ts.pnl_pct = self._calc_pnl(ts, price)
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

            # -- PATCH 4: Dead-trade time stop --
            # Exit stale trades that haven't reached +0.3R within time limit
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

                    # ADAPTIVE TIME STOP — adapts to setup, confidence, volatility, and trade progress
                    if is_long:
                        current_r = (price - ts.entry_price) / risk if risk > 0 else 0
                    else:
                        current_r = (ts.entry_price - price) / risk if risk > 0 else 0

                    # Base time limit adapts to setup type
                    setup = getattr(ts, 'setup_type', '')
                    conf = getattr(ts, 'confidence', 0)

                    if setup == 'bb_squeeze':
                        base_time = 45 * 60    # squeeze breakouts need more time
                    elif setup == 'rsi_divergence':
                        base_time = 40 * 60    # divergences take time to play out
                    elif setup == 'trend_continuation':
                        base_time = 25 * 60    # trends should move quickly
                    else:
                        base_time = 20 * 60    # default (ema_momentum etc)

                    # High confidence → give more time (quality setups deserve patience)
                    if conf >= 85:
                        base_time = int(base_time * 1.5)  # 50% more time
                    elif conf >= 75:
                        base_time = int(base_time * 1.25)  # 25% more time
                    elif conf < 60:
                        base_time = int(base_time * 0.75)  # cut time for low-conf

                    # If trade is making progress (MFE > 0.3R), extend time
                    if max_fav_r >= 0.3:
                        base_time = int(base_time * 1.5)  # trade showed life, give it room

                    # If trade went positive but is now retreating, tighter time
                    if max_fav_r >= 0.2 and current_r < 0:
                        base_time = int(base_time * 0.7)  # was working, now failing

                    # Adaptive thresholds: higher MFE threshold for longer times
                    mfe_threshold = 0.15 + (base_time / (60 * 60))  # scales with time
                    current_threshold = mfe_threshold * 0.8

                    dead_trade = False

                    # SMART TIME STOP: Never close if price is above entry
                    # If we're not losing, there's no reason to exit
                    # Only time-stop trades that are LOSING and going nowhere
                    if current_r >= 0:
                        dead_trade = False  # above entry → HOLD, never time-stop
                    elif age_sec >= base_time and max_fav_r < mfe_threshold and current_r < -0.2:
                        dead_trade = True  # below entry, never moved, losing → close
                    # Hard backstop: 4 hours max for any trade below entry
                    elif age_sec >= 4 * 3600 and current_r < 0:
                        dead_trade = True

                    if dead_trade:
                            ts.exit_price = price
                            ts.exit_reason = "time_stop_dead_trade"
                            ts.exit_time = now_iso
                            ts.pnl_pct = self._calc_pnl(ts, price)
                            ts.time_stop_triggered = True
                            ts.exit_reason_detailed = "time_stop_dead_trade"
                            ts.status = "expired"
                            to_close.append(tid)
                            logger.info(
                                "TIME STOP: %s %s | age=%dm | max_fav=%.2fR | current=%.2fR | PnL: %+.2f%%",
                                ts.symbol, ts.side, int(age_sec / 60),
                                max_fav_r, current_r, ts.pnl_pct,
                            )
                            events.append({
                                "type": "time_stop",
                                "signal": ts.to_dict(),
                                "message": (
                                    f"TIME STOP: {ts.symbol} {ts.side} @ {price:.2f} | "
                                    f"Dead {int(age_sec/60)}min, max {max_fav_r:.2f}R | "
                                    f"PnL: {ts.pnl_pct:+.2f}%"
                                ),
                            })
                            continue
                except (ValueError, TypeError):
                    pass

            # -- Scalper timer: partial close before window expires --
            # BTC: 30 min window, others: 15 min
            # If profitable and nearing window end, close to get free exit fee
            try:
                entry_dt_sc = datetime.fromisoformat(ts.entry_time)
                age_sc = (datetime.now(timezone.utc) - entry_dt_sc).total_seconds()
                scalper_window = SCALPER_WINDOW_BTC if "BTC" in ts.symbol else SCALPER_WINDOW_OTHER
                # 80% of window elapsed + still in profit → close to lock free exit
                if age_sc >= scalper_window * 0.80 and ts.status == "active":
                    if is_long:
                        sc_r = (price - ts.entry_price) / risk if risk > 0 else 0
                    else:
                        sc_r = (ts.entry_price - price) / risk if risk > 0 else 0
                    if sc_r > 0.1:  # in profit
                        ts.exit_price = price
                        ts.exit_reason = "scalper_timer"
                        ts.exit_time = now_iso
                        ts.pnl_pct = self._calc_pnl(ts, price)
                        ts.exit_reason_detailed = f"scalper_timer_{int(scalper_window/60)}m"
                        ts.status = "expired"
                        to_close.append(tid)
                        logger.info(
                            "SCALPER TIMER: %s %s | age=%dm/%dm | R=%.2fR | PnL: %+.2f%% (free exit)",
                            ts.symbol, ts.side, int(age_sc/60), int(scalper_window/60),
                            sc_r, ts.pnl_pct,
                        )
                        events.append({
                            "type": "scalper_timer",
                            "signal": ts.to_dict(),
                            "message": f"SCALPER: {ts.symbol} {ts.side} closed at {int(age_sc/60)}m (free exit) | PnL: {ts.pnl_pct:+.2f}%",
                        })
                        continue
            except (ValueError, TypeError):
                pass

            # -- Check expiry (4 hours) — applies to ALL non-closed statuses --
            try:
                entry_dt = datetime.fromisoformat(ts.entry_time)
                age = (datetime.now(timezone.utc) - entry_dt).total_seconds()
                if age > MAX_SIGNAL_AGE and ts.status in ("active", "tp1_hit", "tp2_hit"):
                    ts.exit_price = price
                    ts.exit_reason = "expired"
                    ts.exit_time = now_iso
                    ts.pnl_pct = self._calc_pnl(ts, price)
                    ts.exit_reason_detailed = "expired_4h"
                    ts.status = "expired"
                    to_close.append(tid)
                    events.append({
                        "type": "expired",
                        "signal": ts.to_dict(),
                        "message": f"EXPIRED: {ts.symbol} {ts.side} @ {price:.2f} | PnL: {ts.pnl_pct:+.2f}%",
                    })
            except (ValueError, TypeError):
                pass

        # Close completed signals
        for tid in to_close:
            ts = self._active.pop(tid)
            self._closed.append(ts.to_dict())

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
        """Return recent closed signals."""
        return self._closed[-limit:]

    def get_stats(self) -> Dict[str, Any]:
        """Return current performance statistics."""
        if not self._stats:
            self._recalc_stats()
        return self._stats.copy()

    @property
    def active_count(self) -> int:
        return len(self._active)

    # ------------------------------------------------------------------
    # P&L calculation
    # ------------------------------------------------------------------

    # Delta Exchange fee schedule
    TAKER_FEE_PCT = 0.06    # 0.06% per side (taker)
    MAKER_FEE_PCT = 0.04    # 0.04% per side (maker) — not used for market orders
    SETTLEMENT_FEE_PCT = 0.06  # 0.06% settlement fee on close

    @staticmethod
    def _calc_pnl(ts: TrackedSignal, exit_price: float) -> float:
        """Calculate P&L percentage for a signal (gross and net).

        Position split: 35% TP1, 35% TP2, 30% runner
        - TP1 (60%): Primary profit lock at 1.5R
        - TP2 (25%): Extended target at 2.0R+
        - TP3 (15%): ATR-trailed runner for big moves

        Uses ACTUAL locked PnL from partial closes when available,
        not fictional splits assuming TPs were hit at target prices.

        Fees applied:
        - Entry: taker fee (0.06%) on full position
        - Exit: taker fee (0.06%) on full position
        - Settlement: 0.06% on close
        Total round-trip fees: ~0.18% of position value
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
            # Real partial closes happened — use locked values + remaining at exit
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

        # Calculate fees as % of position
        # Entry taker fee + Exit taker fee + Settlement fee
        fee_pct = (
            SignalTracker.TAKER_FEE_PCT      # entry
            + SignalTracker.TAKER_FEE_PCT     # exit
            + SignalTracker.SETTLEMENT_FEE_PCT  # settlement
        )  # = 0.18% total round-trip

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

        # Calculate exit R-multiple: net P&L expressed in risk units
        if ts.initial_risk > 0:
            if is_long:
                raw_r = (exit_price - ts.entry_price) / ts.initial_risk
            else:
                raw_r = (ts.entry_price - exit_price) / ts.initial_risk

            def r_at(price: float) -> float:
                if is_long:
                    return (price - ts.entry_price) / ts.initial_risk
                return (ts.entry_price - price) / ts.initial_risk

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
        # Use real exchange balance if available, otherwise fallback
        if self._exchange_balance is not None:
            paper_balance = self._exchange_balance
        else:
            paper_balance = total_pnl_usd  # just show cumulative P&L

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
            "paper_stake_per_trade": 25.0,
            "fee_schedule": {
                "taker_pct": self.TAKER_FEE_PCT,
                "settlement_pct": self.SETTLEMENT_FEE_PCT,
                "round_trip_pct": self.TAKER_FEE_PCT * 2 + self.SETTLEMENT_FEE_PCT,
            },
            # R-Multiple metrics (global)
            "r_metrics": self._calc_global_r_metrics(all_r_values, all_mae, all_mfe, win_count, total),
        }

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

        return {
            "avg_r": round(avg_r, 4),
            "total_r": round(sum(r_values), 4),
            "expectancy_r": round(expectancy, 4),
            "avg_win_r": round(avg_win, 4),
            "avg_loss_r": round(avg_loss, 4),
            "best_r": round(max(r_values), 4),
            "worst_r": round(min(r_values), 4),
            "avg_mae_r": round(avg_mae, 4),
            "avg_mfe_r": round(avg_mfe, 4),
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
                data = json.loads(_ACTIVE_FILE.read_text())
                for d in data:
                    ts = TrackedSignal.from_dict(d)
                    self._active[ts.trade_id] = ts
                logger.info("Loaded %d active tracked signals", len(self._active))
        except Exception as exc:
            logger.warning("Failed to load active signals: %s", exc)

        try:
            if _CLOSED_FILE.exists():
                self._closed = json.loads(_CLOSED_FILE.read_text())
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

                # ── AUTO-DEDUP: Remove duplicate entries (same symbol+side+entry) ──
                seen_keys = set()
                deduped = []
                for t in self._closed:
                    key = f"{t.get('symbol','')}_{t.get('side','')}_{t.get('entry_price',0)}"
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
