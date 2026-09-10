"""Phase 4.0 — Unified Exit Simulator.

SINGLE SOURCE OF TRUTH for "what happens after entry".

This module is used by THREE different consumers:
  1. scanner_backtester.py — bar-replay backtesting
  2. candidate_trainer.py — label generation for ML training
  3. (future) live signal_tracker parity testing

By using the same exit logic everywhere, we eliminate the training-serving
skew that was producing 17-23% WR backtests (Bug #1 + #4 + #8 in the audit).

The simulator reads its configuration from `bot.signal_tracker.TRADE_TYPE_CONFIG`
so there is NO hardcoded exit rule. Change the live config → backtest updates
automatically on the next run.

Design:
  - Pure function, no state
  - Config-driven: SL from ATR multiplier (matches live), TPs in R units
  - Replicates: chandelier trail, MFE profit lock, early kill, time decay,
    hard loss cap, breakeven lock
  - Returns full outcome dict for labeling

Usage:
    from bot.trade_simulator import simulate_trade, SimulatorConfig

    config = SimulatorConfig.from_trade_type("SCALP")
    outcome = simulate_trade(
        df=candles_1m,
        entry_idx=127,
        side="long",
        entry_price=72000.0,
        atr=50.0,
        regime="sideways",
        config=config,
    )
    # outcome = {
    #     "pnl_r": 0.43, "exit_reason": "trail_profit", "exit_bar": 145,
    #     "peak_mfe_r": 0.82, "mae_r": 0.15, "duration_bars": 18,
    #     "won": True, "exit_price": 72110.50, "breakeven_set": True,
    # }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Import TRADE_TYPE_CONFIG from the LIVE signal tracker so backtest + training
# automatically pick up any config changes. This is the heart of the fix:
# one config, one behavior, everywhere.
try:
    from bot.signal_tracker import TRADE_TYPE_CONFIG as LIVE_TRADE_TYPE_CONFIG
except Exception:
    # Fallback: hardcoded defaults matching the live values as of 2026-04-11
    LIVE_TRADE_TYPE_CONFIG = {
        "SCALP": {
            "sl_atr_mult": 0.8,
            "tp1_rr": 0.8,
            "tp2_rr": 1.2,
            "tp3_rr": 0.0,
            "early_kill_sec": 60,
            "early_kill_mfe": 0.10,
            "max_age_sec": 15 * 60,
            "extension_trigger_r": 0.15,
            "extended_age_sec": int(22.5 * 60),
            "full_extend_r": 0.3,
            "full_extended_age_sec": 30 * 60,
            "chandelier_mult_ranging": 0.8,
            "chandelier_mult_trending": 1.0,
        },
        "INTRADAY": {
            "sl_atr_mult": 1.0,
            "tp1_rr": 1.2,
            "tp2_rr": 2.0,
            "tp3_rr": 3.0,
            "early_kill_sec": 90,
            "early_kill_mfe": 0.08,
            "max_age_sec": 20 * 60,
            "extension_trigger_r": 0.15,
            "extended_age_sec": 30 * 60,
            "full_extend_r": 0.3,
            "full_extended_age_sec": 40 * 60,
            "chandelier_mult_ranging": 0.8,
            "chandelier_mult_trending": 1.2,
        },
        "RUNNER": {
            "sl_atr_mult": 0.6,
            "tp1_rr": 1.5,
            "tp2_rr": 3.0,
            "tp3_rr": 5.0,
            "early_kill_sec": 0,
            "early_kill_mfe": 0.0,
            "max_age_sec": 8 * 3600,
            "extension_trigger_r": 0.15,
            "extended_age_sec": 12 * 3600,
            "full_extend_r": 0.3,
            "full_extended_age_sec": 16 * 3600,
            "chandelier_mult_ranging": 1.0,
            "chandelier_mult_trending": 1.2,
        },
    }


# Trending regimes where chandelier mult is looser
TRENDING_REGIMES = ("trending_up", "trending_down", "breakout")


@dataclass
class SimulatorConfig:
    """Config for the unified trade simulator.

    Populate from TRADE_TYPE_CONFIG or pass custom values for experiments.
    """
    # SL definition — SINGLE IMPORTANT CHANGE vs legacy backtest:
    # Legacy used fixed 0.65% SL. Live uses atr_mult. We now follow live.
    sl_atr_mult: float = 2.0          # real live default; overridden by trade_type

    # TP levels expressed in R (not ATR), so they scale with actual risk
    tp1_r: float = 0.8
    tp2_r: float = 1.2
    tp3_r: float = 0.0                # 0 = disabled

    # Early kill
    early_kill_sec: int = 60
    early_kill_mfe: float = 0.10
    early_kill_current_r: float = -0.15  # needs current_r < this to fire

    # Time decay / max age
    max_age_sec: int = 15 * 60
    extension_trigger_r: float = 0.15
    extended_age_sec: int = int(22.5 * 60)
    full_extend_r: float = 0.3
    full_extended_age_sec: int = 30 * 60

    # Hard loss cap (in R)
    hard_loss_cap_r: float = -1.2

    # Breakeven lock
    breakeven_trigger_r: float = 0.10   # when peak MFE crosses this, move SL to +0.03R
    breakeven_lock_r: float = 0.03       # lock profit at this level

    # Chandelier trail
    chandelier_mult_ranging: float = 0.8
    chandelier_mult_trending: float = 1.0

    # MFE profit lock (tightens as MFE grows)
    mfe_lock_trigger_r: float = 0.30    # start locking when peak MFE ≥ this
    # Lock percentages by peak level — matches live:
    #   peak ≥ 1.5R → lock 0.85× peak
    #   peak ≥ 1.0R → lock 0.80× peak
    #   peak ≥ 0.5R → lock 0.70× peak
    #   peak ≥ 0.3R → lock 0.60× peak

    # Fees (round-trip as fraction of notional)
    # Half of the maker-entry / taker-exit round trip incl. GST (0.0826 % / 2),
    # so `fee_rate_per_side * 2` in fee_r matches execution/fees.FeeModel.
    fee_rate_per_side: float = 0.000413

    # Debug
    verbose: bool = False

    @classmethod
    def from_trade_type(cls, trade_type: str, scanner: Optional[str] = None) -> "SimulatorConfig":
        """Build config from live TRADE_TYPE_CONFIG.

        IMPORTANT: TRADE_TYPE_CONFIG's sl_atr_mult is only a "reference" per the
        code comment in signal_tracker.py — live strategy computes the actual SL
        using the scanner-specific value from strategies.scalp_strategy._scanner_sl_tp
        (e.g., structure_bounce=1.0 ATR, ema_momentum=1.2, trend_continuation=1.5).

        Pass `scanner` to get the live-accurate SL width. Without it, falls back
        to scalp_strategy's 2.0×ATR global default (matches live's 5m SL).
        """
        cfg = LIVE_TRADE_TYPE_CONFIG.get(trade_type, LIVE_TRADE_TYPE_CONFIG["SCALP"])
        # Scanner-specific SL/TP from live strategy config
        # Keep in sync with strategies/scalp_strategy.py _scanner_sl_tp
        SCANNER_SL_TP = {
            "ema_momentum":       {"sl_atr": 1.2, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "trend_continuation": {"sl_atr": 1.5, "tp1_rr": 2.0, "tp2_rr": 3.0, "tp3_rr": 5.0},
            "vwap_mean_revert":   {"sl_atr": 1.0, "tp1_rr": 1.2, "tp2_rr": 2.0, "tp3_rr": 3.0},
            "rsi_divergence":     {"sl_atr": 1.2, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "structure_bounce":   {"sl_atr": 1.0, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "bb_squeeze":         {"sl_atr": 1.3, "tp1_rr": 1.8, "tp2_rr": 3.0, "tp3_rr": 5.0},
            "order_block_entry":  {"sl_atr": 1.0, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "liquidity_sweep":    {"sl_atr": 1.2, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "simple_bias":        {"sl_atr": 1.5, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
            "bos_choch":          {"sl_atr": 1.2, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0},
        }
        scanner_cfg = SCANNER_SL_TP.get((scanner or "").lower(), {}) if scanner else {}
        # Global default when scanner not given: 2.0×ATR — matches scalp_strategy.sl_atr_mult
        # (the code comment in signal_tracker.TRADE_TYPE_CONFIG says "strategy uses 2.0x 5m ATR")
        default_sl_atr = 2.0
        sl_atr_mult = float(scanner_cfg.get("sl_atr") or default_sl_atr)
        tp1_r = float(scanner_cfg.get("tp1_rr") or cfg.get("tp1_rr", 0.8))
        tp2_r = float(scanner_cfg.get("tp2_rr") or cfg.get("tp2_rr", 1.2))
        tp3_r = float(scanner_cfg.get("tp3_rr") or cfg.get("tp3_rr", 0.0))
        return cls(
            sl_atr_mult=sl_atr_mult,
            tp1_r=tp1_r,
            tp2_r=tp2_r,
            tp3_r=tp3_r,
            early_kill_sec=int(cfg.get("early_kill_sec", 60)),
            early_kill_mfe=float(cfg.get("early_kill_mfe", 0.10)),
            max_age_sec=int(cfg.get("max_age_sec", 15 * 60)),
            extension_trigger_r=float(cfg.get("extension_trigger_r", 0.15)),
            extended_age_sec=int(cfg.get("extended_age_sec", int(22.5 * 60))),
            full_extend_r=float(cfg.get("full_extend_r", 0.3)),
            full_extended_age_sec=int(cfg.get("full_extended_age_sec", 30 * 60)),
            chandelier_mult_ranging=float(cfg.get("chandelier_mult_ranging", 0.8)),
            chandelier_mult_trending=float(cfg.get("chandelier_mult_trending", 1.0)),
        )


def simulate_trade(
    df,
    entry_idx: int,
    side: str,
    entry_price: float,
    atr: float,
    regime: str = "sideways",
    config: Optional[SimulatorConfig] = None,
    trade_type: str = "SCALP",
    tf_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Simulate a trade forward from entry_idx. Returns outcome dict.

    Args:
        df: pandas DataFrame with OHLCV columns (high, low, close) and datetime index
        entry_idx: bar index where entry occurs
        side: "long" or "short"
        entry_price: actual entry price (could be signal or fill)
        atr: ATR at entry bar (used for SL distance + chandelier trail width)
        regime: market regime for chandelier mult selection
        config: optional SimulatorConfig — if None, built from trade_type
        trade_type: "SCALP" | "INTRADAY" | "RUNNER" (used if config is None)
        tf_seconds: bar timeframe in seconds — if None, inferred from df index

    Returns:
        Dict with keys:
            pnl_r: realized R (fees subtracted)
            won: bool (pnl_r > 0)
            exit_price: float
            exit_bar: int (absolute index in df)
            exit_reason: str
            peak_mfe_r: float (max favorable excursion in R)
            mae_r: float (max adverse excursion in R)
            duration_bars: int
            duration_sec: int
            breakeven_set: bool (did BE trigger?)
            initial_risk: float (the R unit)
            sl_final: float (last SL value — shows trail progression)
            won_at_tp: str (which TP hit, if any: "tp1"|"tp2"|"tp3"|"")
    """
    if config is None:
        config = SimulatorConfig.from_trade_type(trade_type)

    # Infer timeframe from index if not provided
    if tf_seconds is None:
        tf_seconds = 60
        try:
            if len(df) > 1:
                idx_diff = df.index[1] - df.index[0]
                if hasattr(idx_diff, "total_seconds"):
                    tf_seconds = max(int(idx_diff.total_seconds()), 1)
        except Exception:
            pass

    # ── SL DEFINITION: ATR-based (matches live) ──
    # This is the core fix vs legacy 0.65% hardcoded SL.
    if atr <= 0 or entry_price <= 0:
        return _zero_outcome(reason="invalid_input")

    sl_distance = atr * config.sl_atr_mult
    if side == "long":
        sl = entry_price - sl_distance
    else:
        sl = entry_price + sl_distance

    initial_risk = sl_distance  # == abs(entry - sl) by construction

    # ── TP levels in R units (scales with actual risk) ──
    if side == "long":
        tp1 = entry_price + config.tp1_r * initial_risk if config.tp1_r > 0 else 0.0
        tp2 = entry_price + config.tp2_r * initial_risk if config.tp2_r > 0 else 0.0
        tp3 = entry_price + config.tp3_r * initial_risk if config.tp3_r > 0 else 0.0
    else:
        tp1 = entry_price - config.tp1_r * initial_risk if config.tp1_r > 0 else 0.0
        tp2 = entry_price - config.tp2_r * initial_risk if config.tp2_r > 0 else 0.0
        tp3 = entry_price - config.tp3_r * initial_risk if config.tp3_r > 0 else 0.0

    # Chandelier mult based on regime
    if regime in TRENDING_REGIMES:
        ch_mult = config.chandelier_mult_trending
    else:
        ch_mult = config.chandelier_mult_ranging

    # State
    highest = entry_price
    lowest = entry_price
    peak_mfe_r = 0.0
    mae_r = 0.0
    breakeven_set = False
    chandelier_stop = 0.0
    current_sl = sl
    exit_price = 0.0
    exit_bar = 0
    exit_reason = ""
    won_at_tp = ""

    # Determine max bars (effective max age — may extend if MFE grows)
    _max_bars_base = max(5, config.max_age_sec // tf_seconds)
    _max_bars_cap = min(
        len(df) - entry_idx - 1,
        max(_max_bars_base, config.full_extended_age_sec // tf_seconds),
    )
    if _max_bars_cap <= 0:
        return _zero_outcome(reason="insufficient_bars")

    for j in range(entry_idx + 1, entry_idx + 1 + _max_bars_cap):
        if j >= len(df):
            break

        row = df.iloc[j]
        try:
            h_j = float(row["high"])
            l_j = float(row["low"])
            c_j = float(row["close"])
        except Exception:
            continue

        age_bars = j - entry_idx
        age_sec = age_bars * tf_seconds

        # Update extremes
        if h_j > highest:
            highest = h_j
        if l_j < lowest:
            lowest = l_j

        # Current R (intrabar: use close for current, high/low for MFE/MAE)
        if side == "long":
            current_r = (c_j - entry_price) / initial_risk
            this_bar_mfe = (highest - entry_price) / initial_risk
            this_bar_mae = (entry_price - lowest) / initial_risk
        else:
            current_r = (entry_price - c_j) / initial_risk
            this_bar_mfe = (entry_price - lowest) / initial_risk
            this_bar_mae = (highest - entry_price) / initial_risk

        if this_bar_mfe > peak_mfe_r:
            peak_mfe_r = this_bar_mfe
        if this_bar_mae > mae_r:
            mae_r = this_bar_mae

        # ── [A] HARD LOSS CAP ──
        # If the bar CLOSE breached hard loss cap, we exit.
        # BUG FIX (2026-04-16): previously used c_j as exit_price, which overstated
        # losses on wicks. A real broker stop fills AT the cap level, not at bar
        # close. Now exit at the R-level-equivalent price. This matches live
        # behavior where a hard stop order sits at the broker.
        if current_r <= config.hard_loss_cap_r:
            # Price level that corresponds to hard_loss_cap_r
            if side == "long":
                exit_price = entry_price + (config.hard_loss_cap_r * initial_risk)
            else:
                exit_price = entry_price - (config.hard_loss_cap_r * initial_risk)
            exit_bar = j
            exit_reason = "hard_loss_cap"
            break

        # ── [B] SL HIT (intrabar check using high/low) ──
        # For longs, SL is below → check low. For shorts, SL is above → check high.
        if current_sl > 0:
            if side == "long":
                if l_j <= current_sl:
                    # Filled at SL (optimistic — real fills may slip worse)
                    exit_price = current_sl
                    exit_bar = j
                    exit_reason = "trail_profit" if (breakeven_set and current_sl > entry_price) else "sl_hit"
                    break
            else:
                if h_j >= current_sl:
                    exit_price = current_sl
                    exit_bar = j
                    exit_reason = "trail_profit" if (breakeven_set and current_sl < entry_price) else "sl_hit"
                    break

        # ── [C] TP HITS (intrabar) ──
        if tp1 > 0:
            if side == "long" and h_j >= tp1:
                exit_price = tp1
                exit_bar = j
                exit_reason = "tp1_hit"
                won_at_tp = "tp1"
                break
            elif side == "short" and l_j <= tp1:
                exit_price = tp1
                exit_bar = j
                exit_reason = "tp1_hit"
                won_at_tp = "tp1"
                break
        # Note: tp2/tp3 only reachable if we don't scale out — for training, treat TP1 as exit
        # This matches conservative scalp behavior

        # ── [D] BREAKEVEN LOCK ──
        if not breakeven_set and peak_mfe_r >= config.breakeven_trigger_r:
            if side == "long":
                be_sl = entry_price + (config.breakeven_lock_r * initial_risk)
                if be_sl > current_sl:
                    current_sl = be_sl
                    breakeven_set = True
            else:
                be_sl = entry_price - (config.breakeven_lock_r * initial_risk)
                if be_sl < current_sl:
                    current_sl = be_sl
                    breakeven_set = True

        # ── [E] CHANDELIER TRAIL ──
        # Distance from highest/lowest in terms of ATR multiple
        chandelier_dist = atr * ch_mult
        if side == "long":
            new_ch = highest - chandelier_dist
            if new_ch > chandelier_stop and new_ch > entry_price:
                chandelier_stop = new_ch
                if new_ch > current_sl:
                    current_sl = new_ch
                    breakeven_set = True  # any trail above entry = BE set
        else:
            new_ch = lowest + chandelier_dist
            if (chandelier_stop == 0 or new_ch < chandelier_stop) and new_ch < entry_price:
                chandelier_stop = new_ch
                if new_ch < current_sl:
                    current_sl = new_ch
                    breakeven_set = True

        # ── [F] MFE PROFIT LOCK (tighter as peak grows) ──
        if peak_mfe_r >= config.mfe_lock_trigger_r:
            if peak_mfe_r >= 1.5:
                _lp = 0.85
            elif peak_mfe_r >= 1.0:
                _lp = 0.80
            elif peak_mfe_r >= 0.5:
                _lp = 0.70
            else:
                _lp = 0.60
            lock_r = peak_mfe_r * _lp
            if side == "long":
                mfe_sl = entry_price + (lock_r * initial_risk)
                if mfe_sl > current_sl:
                    current_sl = mfe_sl
                    breakeven_set = True
            else:
                mfe_sl = entry_price - (lock_r * initial_risk)
                if mfe_sl < current_sl:
                    current_sl = mfe_sl
                    breakeven_set = True

        # ── [G] EARLY KILL ──
        if (config.early_kill_sec > 0
                and age_sec >= config.early_kill_sec
                and peak_mfe_r < config.early_kill_mfe
                and current_r < config.early_kill_current_r):
            exit_price = c_j
            exit_bar = j
            exit_reason = "early_kill"
            break

        # ── [H] TIME DECAY with extensions ──
        # Determine effective max age based on how much MFE we've captured
        if peak_mfe_r >= config.full_extend_r:
            _effective_max_sec = config.full_extended_age_sec
        elif peak_mfe_r >= config.extension_trigger_r:
            _effective_max_sec = config.extended_age_sec
        else:
            _effective_max_sec = config.max_age_sec

        if age_sec >= _effective_max_sec:
            exit_price = c_j
            exit_bar = j
            if current_r > 0:
                exit_reason = "time_decay_profit"
            elif abs(current_r) < 0.05:
                exit_reason = "time_decay_flat"
            else:
                exit_reason = "time_decay"
            break

    # If we ran out of bars without exit, close at last bar's close
    if exit_price == 0 and entry_idx + 1 < len(df):
        last_j = min(entry_idx + _max_bars_cap, len(df) - 1)
        exit_price = float(df.iloc[last_j]["close"])
        exit_bar = last_j
        exit_reason = "end_of_data"

    # ── Compute final PnL in R ──
    if side == "long":
        raw_r = (exit_price - entry_price) / initial_risk if initial_risk > 0 else 0.0
    else:
        raw_r = (entry_price - exit_price) / initial_risk if initial_risk > 0 else 0.0

    # Subtract round-trip fees (in R units)
    fee_r = (config.fee_rate_per_side * 2 * entry_price) / initial_risk if initial_risk > 0 else 0.0
    pnl_r = raw_r - fee_r

    duration_bars = exit_bar - entry_idx if exit_bar > entry_idx else 0
    duration_sec = duration_bars * tf_seconds

    return {
        "pnl_r": round(pnl_r, 4),
        "raw_r_before_fees": round(raw_r, 4),
        "fee_r": round(fee_r, 4),
        "won": pnl_r > 0,
        "exit_price": exit_price,
        "exit_bar": exit_bar,
        "exit_reason": exit_reason,
        "peak_mfe_r": round(peak_mfe_r, 4),
        "mae_r": round(mae_r, 4),
        "duration_bars": duration_bars,
        "duration_sec": duration_sec,
        "breakeven_set": breakeven_set,
        "initial_risk": round(initial_risk, 6),
        "sl_initial": round(sl, 6),
        "sl_final": round(current_sl, 6),
        "won_at_tp": won_at_tp,
        "entry_price": entry_price,
        "entry_bar": entry_idx,
        "side": side,
        "atr": atr,
        "regime": regime,
    }


def _zero_outcome(reason: str = "invalid") -> Dict[str, Any]:
    """Return a zero-PnL outcome for invalid inputs."""
    return {
        "pnl_r": 0.0,
        "raw_r_before_fees": 0.0,
        "fee_r": 0.0,
        "won": False,
        "exit_price": 0.0,
        "exit_bar": 0,
        "exit_reason": reason,
        "peak_mfe_r": 0.0,
        "mae_r": 0.0,
        "duration_bars": 0,
        "duration_sec": 0,
        "breakeven_set": False,
        "initial_risk": 0.0,
        "sl_initial": 0.0,
        "sl_final": 0.0,
        "won_at_tp": "",
        "entry_price": 0.0,
        "entry_bar": 0,
        "side": "",
        "atr": 0.0,
        "regime": "",
    }
