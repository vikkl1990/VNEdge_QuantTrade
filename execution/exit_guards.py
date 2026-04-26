"""Phase 5.8 — Unified exit-guard helpers.

This module is the single source of truth for the dead-signal kill cascade
that was previously stacked across `user_real_manager.py`,
`bot/signal_tracker.py`, and `bot/trade_simulator.py`.

Background
----------
Pre-5.8 the bot ran four overlapping fast-kill guards:

    quick_kill         age >  30s,  peak <= 0.02R, current < -0.05R
    no_proof_of_life   age >  90/180s, peak < 0.05R, current < 0    (A+/A only)
    early_kill         age >  60-90s,  peak < kill_mfe, current < 0 (B/C only)
    zombie_kill        age > 600s, peak < 0.10R, current < 0

Together they cut trades faster than the round-trip taker fee floor could be
recovered (`fees ~= 10bp r/t == ~0.15R on a 0.65% SL`). 24h shadow_live data
2026-04-24 → 2026-04-25: 12% WR, -$12.27 net, 64% of losses were pure fees.

5.8 collapses the four guards into one fee-floor-aware check. See
`docs/EXIT_GUARD_REFACTOR_5_8.md` for the full design + rollout plan.

Pure functions
--------------
Everything here takes primitive arguments (entry, sl, age_sec, regime, ...)
rather than a Trade or self reference. That keeps the module trivially
unit-testable and importable from all three engines (real, paper, backtest)
without any one of them having to import the others.
"""

from __future__ import annotations

from typing import Optional


# ── Constants ───────────────────────────────────────────────────────────────
# Round-trip taker fee on Delta India is ~10bp = 0.0010 (5bp per side).
# Maker+taker is ~7bp; we use the conservative taker number so the floor is
# never optimistic. A user-specific calibration step is deferred to 5.9.
_RT_FEE_PCT = 0.0010
# 1.5x safety margin on top of the bare fee — covers slippage + half-tick noise.
_FEE_FLOOR_SAFETY = 1.5
# Sanity bounds on the fee floor so a degenerate SL distance can't drive it
# to 0 or to something absurd. 0.15R is the floor for wide-SL trades; 0.30R
# is the cap for tight-SL high-grade A+ trades.
_FEE_FLOOR_MIN_R = 0.15
_FEE_FLOOR_MAX_R = 0.30

# Grace window — minimum hold time before any time-based guard can fire.
# Tuned to be longer than the median paper revive time (45s @ 0.12R) so we
# don't decapitate the trades the bot is actually pricing correctly.
_GRACE_BASE_SEC = {
    "RUNNER": 300.0,    # 5 min — RUNNERs need room
    "INTRADAY": 180.0,  # 3 min
    "SCALP": 120.0,     # 2 min — was 30-60s pre-5.8, this fixes the leak
}
_GRACE_DEFAULT_SEC = _GRACE_BASE_SEC["SCALP"]
# Active regimes get +50% grace; chop-to-drop cycles run longer in trend.
_ACTIVE_REGIMES = frozenset(
    {"trending", "trending_up", "trending_down", "breakout", "high_volatility"}
)
_ACTIVE_REGIME_BONUS = 1.5

# Patience multipliers (how many grace windows we wait past grace before
# the unified kill is allowed to fire).
_PATIENCE_HIGHGRADE = 4.0  # A+/A — most patient
_PATIENCE_LOWGRADE = 2.0   # B/C  — less patient

# Stalled-after-15min secondary kill — replaces zombie_kill. Same window as
# the prior zombie_kill (600s) is too aggressive given the new 120s grace,
# so we extend to 900s and keep the peak<0.20R + current<-0.05R conditions.
_STALL_AGE_SEC = 900.0
_STALL_PEAK_R = 0.20
_STALL_CURRENT_R = -0.05

# Primary unified kill — current_r threshold below which we consider the
# trade demonstrably underwater. Matches old quick_kill's -0.05R but is
# only checked AFTER patience window, not after 30s.
_UNIFIED_KILL_CURRENT_R = -0.10

# Exit reason strings. New values in db/migrations/014_exit_guard_refactor.sql.
EXIT_DEAD_SIGNAL_UNIFIED = "dead_signal_unified"
EXIT_STALLED_AFTER_15MIN = "stalled_after_15min"


# ── Helpers ─────────────────────────────────────────────────────────────────
def fee_floor_r(entry: float, sl: float) -> float:
    """Return the minimum peak_mfe_r required to break even on round-trip fees.

    The result is `(rt_fee_pct * safety) / sl_distance_pct`, clamped to
    `[_FEE_FLOOR_MIN_R, _FEE_FLOOR_MAX_R]`.

    Tight-SL trades (e.g. high-grade A+ at 0.30%) hit the upper cap (0.30R)
    because every tick of favor maps to a smaller fraction of fee. Wide-SL
    trades (e.g. 2%) hit the lower cap (0.15R).

    Defensive: if entry or sl is missing/invalid, returns the safe default
    (midpoint of bounds). Never raises.
    """
    try:
        e = float(entry)
        s = float(sl)
    except (TypeError, ValueError):
        return 0.20
    if e <= 0 or s <= 0:
        return 0.20
    sl_dist_pct = abs(e - s) / e
    if sl_dist_pct <= 0:
        return 0.20
    floor = (_RT_FEE_PCT * _FEE_FLOOR_SAFETY) / max(sl_dist_pct, 0.001)
    return max(_FEE_FLOOR_MIN_R, min(_FEE_FLOOR_MAX_R, floor))


def grace_window_sec(trade_type: Optional[str], regime: Optional[str]) -> float:
    """Return the grace window (seconds) for the given trade type + regime.

    SCALP = 120s, INTRADAY = 180s, RUNNER = 300s. Active regimes
    (trending/breakout/high_vol) get a 1.5x bonus to absorb the longer
    chop-to-drop cycle. Unknown trade types fall back to SCALP.
    """
    tt = (trade_type or "SCALP").upper()
    base = _GRACE_BASE_SEC.get(tt, _GRACE_DEFAULT_SEC)
    rg = (regime or "").lower()
    if rg in _ACTIVE_REGIMES:
        return base * _ACTIVE_REGIME_BONUS
    return base


# 2026-04-26 — RELAXED_SHADOW mode multipliers (FIX 1 + FIX 2 from
# "Delta Exit Logic Disaster" deep-dive review).
# Rationale: shadow trades incur ~3 bps entry slippage + ~3 bps exit slippage
# the standard guards weren't calibrated for. Result: defensive exits killed
# trades on noise that paper would have survived (8/9 Delta exit categories
# net negative, $47/$50 daily loss came from these guards firing on slippage
# not on real adverse movement). When `relaxed_shadow=True`:
#   - kill threshold widens from -0.10R → -0.16R (absorbs the 0.06R hole)
#   - fee_floor required multiplied by 1.5× (raises bar; trades have more
#     room to recover before being deemed "never made fees")
#   - patience window stretched 1.5× (gives late-developing winners time)
#   - stall current_r relaxed from -0.05R → -0.075R
# Gating: enabled per-user via UserRealManager._relaxed_shadow_exits.
# A/B test: niranjan = treatment, admin = control.
_RELAXED_KILL_CURRENT_R = -0.16
_RELAXED_PATIENCE_MULT  = 1.5
_RELAXED_FEE_FLOOR_MULT = 1.5
_RELAXED_STALL_CURRENT_R = -0.075


def should_kill_dead_signal(
    age_sec: float,
    current_r: float,
    peak_mfe_r: float,
    grade: Optional[str],
    entry: float,
    sl: float,
    trade_type: Optional[str],
    regime: Optional[str],
    relaxed_shadow: bool = False,
) -> Optional[str]:
    """Unified dead-signal guard.

    Replaces the quick_kill / no_proof_of_life / early_kill / zombie_kill
    cascade with a single fee-floor-aware decision.

    Returns the exit-reason string if the trade should die, otherwise None.

    Decision tree:

        1. If `age_sec < grace_sec` (grace window) → return None.
           Inside grace only the initial 1R SL protects.
        2. If past patience window AND never reached fee_floor AND
           currently underwater more than _UNIFIED_KILL_CURRENT_R →
           return EXIT_DEAD_SIGNAL_UNIFIED.
        3. If age >= 15min AND peak < 0.20R AND current < -0.05R →
           return EXIT_STALLED_AFTER_15MIN. (Backstop for the
           middle-zone trades that survived #1+#2 but never developed.)
        4. Otherwise return None.

    `relaxed_shadow=True` (FIX 1+2, 2026-04-26): widens kill threshold,
    raises fee_floor bar, stretches patience, relaxes stall current_r.
    Designed to compensate for shadow execution's ~6 bps slippage hole.
    Gated per-user via UserRealManager flag (A/B test).

    All numeric inputs are coerced to float; non-numeric input returns None
    (do not kill — let the other guards handle it).
    """
    try:
        age_sec_f = float(age_sec)
        current_r_f = float(current_r)
        peak_mfe_r_f = float(peak_mfe_r)
    except (TypeError, ValueError):
        return None

    floor = fee_floor_r(entry, sl)
    grace = grace_window_sec(trade_type, regime)

    # Apply relaxed-shadow multipliers (slippage-absorbing)
    kill_threshold = _UNIFIED_KILL_CURRENT_R
    stall_current  = _STALL_CURRENT_R
    if relaxed_shadow:
        floor          = floor * _RELAXED_FEE_FLOOR_MULT
        kill_threshold = _RELAXED_KILL_CURRENT_R
        stall_current  = _RELAXED_STALL_CURRENT_R

    # Inside grace window — no time-based kill is allowed.
    if age_sec_f < grace:
        return None

    g = (grade or "").upper()
    patience_mult = _PATIENCE_HIGHGRADE if g in ("A+", "A") else _PATIENCE_LOWGRADE
    patience_sec = grace * patience_mult
    if relaxed_shadow:
        patience_sec = patience_sec * _RELAXED_PATIENCE_MULT

    # Primary kill: past patience, never recovered fees, currently underwater.
    if (
        age_sec_f >= patience_sec
        and peak_mfe_r_f < floor
        and current_r_f < kill_threshold
    ):
        return EXIT_DEAD_SIGNAL_UNIFIED

    # Secondary kill: stalled in the middle-zone for 15+ minutes.
    if (
        age_sec_f >= _STALL_AGE_SEC
        and peak_mfe_r_f < _STALL_PEAK_R
        and current_r_f < stall_current
    ):
        return EXIT_STALLED_AFTER_15MIN

    return None


__all__ = [
    "EXIT_DEAD_SIGNAL_UNIFIED",
    "EXIT_STALLED_AFTER_15MIN",
    "fee_floor_r",
    "grace_window_sec",
    "should_kill_dead_signal",
]
