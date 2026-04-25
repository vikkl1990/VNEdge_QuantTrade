"""Unit tests for execution/exit_guards.py — Phase 5.8 unified dead-signal guard.

Covers:
  - fee_floor_r       (math + clamps + degenerate inputs)
  - grace_window_sec  (per-trade-type base + active-regime bonus)
  - should_kill_dead_signal
        * grace window blocks all kills
        * patience window blocks A+/A and B/C correctly
        * primary kill fires only when fee_floor + current_r conditions met
        * stalled_after_15min secondary kill fires at 900s
        * non-numeric inputs return None (do not kill)

The guard is purely functional, so no mocking is required.
"""
from __future__ import annotations

import os
import sys

# Allow `pytest tests/test_exit_guards.py` from the repo root without an installed package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from execution.exit_guards import (
    EXIT_DEAD_SIGNAL_UNIFIED,
    EXIT_STALLED_AFTER_15MIN,
    fee_floor_r,
    grace_window_sec,
    should_kill_dead_signal,
)


# ── fee_floor_r ─────────────────────────────────────────────────────────────
class TestFeeFloorR:
    def test_typical_065pct_sl_returns_023r(self):
        """SOL-style: 0.65% SL → floor ~= (0.001 * 1.5) / 0.0065 = 0.231."""
        floor = fee_floor_r(entry=86.0, sl=86.0 * (1 - 0.0065))
        assert 0.22 < floor < 0.24

    def test_tight_sl_caps_at_max(self):
        """0.30% SL would compute 0.50 → capped at 0.30."""
        floor = fee_floor_r(entry=100.0, sl=99.7)
        assert floor == pytest.approx(0.30, abs=1e-6)

    def test_wide_sl_floors_at_min(self):
        """2% SL would compute 0.075 → floored at 0.15."""
        floor = fee_floor_r(entry=100.0, sl=98.0)
        assert floor == pytest.approx(0.15, abs=1e-6)

    def test_short_side_uses_abs_distance(self):
        """Short with sl > entry is the same fee floor as long with sl < entry."""
        long_floor = fee_floor_r(entry=100.0, sl=99.35)
        short_floor = fee_floor_r(entry=100.0, sl=100.65)
        assert long_floor == pytest.approx(short_floor, abs=1e-9)

    @pytest.mark.parametrize(
        "entry,sl",
        [(0, 0), (-1, 100), (100, 0), (100, -1), (100, 100)],
    )
    def test_degenerate_returns_safe_default(self, entry, sl):
        assert fee_floor_r(entry=entry, sl=sl) == pytest.approx(0.20, abs=1e-9)

    def test_non_numeric_inputs_return_safe_default(self):
        assert fee_floor_r(entry=None, sl=99.0) == pytest.approx(0.20, abs=1e-9)
        assert fee_floor_r(entry="oops", sl="nope") == pytest.approx(0.20, abs=1e-9)


# ── grace_window_sec ────────────────────────────────────────────────────────
class TestGraceWindowSec:
    def test_scalp_quiet_is_120s(self):
        assert grace_window_sec("SCALP", "quiet") == 120.0

    def test_intraday_quiet_is_180s(self):
        assert grace_window_sec("INTRADAY", "quiet") == 180.0

    def test_runner_quiet_is_300s(self):
        assert grace_window_sec("RUNNER", "quiet") == 300.0

    def test_unknown_type_falls_back_to_scalp(self):
        assert grace_window_sec("WEIRDTYPE", "quiet") == 120.0

    def test_none_type_falls_back_to_scalp(self):
        assert grace_window_sec(None, None) == 120.0

    @pytest.mark.parametrize(
        "regime",
        ["trending", "trending_up", "trending_down", "breakout", "high_volatility"],
    )
    def test_active_regimes_get_15x_bonus(self, regime):
        assert grace_window_sec("SCALP", regime) == 180.0
        assert grace_window_sec("RUNNER", regime) == 450.0

    def test_inactive_regimes_get_no_bonus(self):
        for r in ["quiet", "sideways", "mean_reversion", "low_liquidity", ""]:
            assert grace_window_sec("SCALP", r) == 120.0

    def test_case_insensitive(self):
        assert grace_window_sec("scalp", "TRENDING_UP") == 180.0


# ── should_kill_dead_signal: grace window ───────────────────────────────────
class TestShouldKillGraceWindow:
    """Inside grace window, no time-based guard ever fires."""

    @pytest.mark.parametrize("age_sec", [0, 1, 30, 60, 90, 119])
    def test_inside_grace_returns_none(self, age_sec):
        # SCALP quiet → grace = 120s
        result = should_kill_dead_signal(
            age_sec=age_sec,
            current_r=-0.50,           # very underwater
            peak_mfe_r=0.0,            # no favor at all
            grade="A+",
            entry=100.0, sl=99.35,
            trade_type="SCALP", regime="quiet",
        )
        assert result is None, f"kill fired at age={age_sec}s inside grace window"

    def test_at_grace_boundary_with_no_kill_conditions(self):
        """At age == grace_sec, no kill fires unless patience ALSO satisfied."""
        # patience = grace * 4 = 480s for A+; at 120s we're past grace but not patience
        result = should_kill_dead_signal(
            age_sec=120.0,
            current_r=-0.50,
            peak_mfe_r=0.0,
            grade="A+",
            entry=100.0, sl=99.35,
            trade_type="SCALP", regime="quiet",
        )
        assert result is None


# ── should_kill_dead_signal: primary unified kill ───────────────────────────
class TestPrimaryUnifiedKill:
    """Past patience window, never reached fee floor, currently underwater."""

    def test_a_plus_scalp_fires_at_patience(self):
        # SCALP quiet: grace=120, patience_mult=4 → patience=480s
        # entry=100, sl=99.35 (0.65% sl) → fee_floor ~= 0.23R
        # peak < 0.23 AND current < -0.10 AND age >= 480 → kill
        result = should_kill_dead_signal(
            age_sec=481.0,
            current_r=-0.20,
            peak_mfe_r=0.10,
            grade="A+",
            entry=100.0, sl=99.35,
            trade_type="SCALP", regime="quiet",
        )
        assert result == EXIT_DEAD_SIGNAL_UNIFIED

    def test_b_grade_fires_earlier_than_a_grade(self):
        """B/C patience_mult=2 → patience=240s for SCALP quiet."""
        kw = dict(
            age_sec=241.0,
            current_r=-0.20,
            peak_mfe_r=0.10,
            entry=100.0, sl=99.35,
            trade_type="SCALP", regime="quiet",
        )
        # A+ with patience=480s, age=241 → not killed yet
        assert should_kill_dead_signal(grade="A+", **kw) is None
        # B with patience=240s, age=241 → killed
        assert should_kill_dead_signal(grade="B", **kw) == EXIT_DEAD_SIGNAL_UNIFIED

    def test_active_regime_extends_patience(self):
        """Trending bumps grace 1.5x → patience for A+ goes 480s → 720s."""
        kw = dict(
            age_sec=600.0,
            current_r=-0.20,
            peak_mfe_r=0.10,
            grade="A+",
            entry=100.0, sl=99.35,
            trade_type="SCALP",
        )
        # Quiet patience = 480s — kill fires
        assert should_kill_dead_signal(regime="quiet", **kw) == EXIT_DEAD_SIGNAL_UNIFIED
        # Trending patience = 720s — no kill yet
        assert should_kill_dead_signal(regime="trending_up", **kw) is None

    def test_peak_above_fee_floor_blocks_kill(self):
        """If trade reached fee floor, primary kill never fires (only stall can)."""
        result = should_kill_dead_signal(
            age_sec=600.0,
            current_r=-0.20,
            peak_mfe_r=0.25,    # above ~0.23 fee floor
            grade="A+",
            entry=100.0, sl=99.35,
            trade_type="SCALP", regime="quiet",
        )
        assert result is None

    def test_current_r_above_threshold_blocks_kill(self):
        """If current_r >= -0.10 (only mildly underwater) primary kill is blocked."""
        result = should_kill_dead_signal(
            age_sec=600.0,
            current_r=-0.05,
            peak_mfe_r=0.10,
            grade="A+",
            entry=100.0, sl=99.35,
            trade_type="SCALP", regime="quiet",
        )
        assert result is None

    def test_runner_patience_is_longest(self):
        """RUNNER grace=300s; A+ patience=1200s. A 600s underwater RUNNER survives."""
        result = should_kill_dead_signal(
            age_sec=600.0,
            current_r=-0.20,
            peak_mfe_r=0.10,
            grade="A+",
            entry=100.0, sl=99.35,
            trade_type="RUNNER", regime="quiet",
        )
        assert result is None


# ── should_kill_dead_signal: stalled_after_15min secondary kill ─────────────
class TestStalledAfter15Min:
    """Backstop for middle-zone trades that escaped the unified kill."""

    def test_fires_at_900s_with_low_peak_and_underwater(self):
        # Use a wide-SL trade so fee_floor floors at 0.15R and peak=0.18 is above
        # it → primary kill blocked → stall path is the one being tested.
        result = should_kill_dead_signal(
            age_sec=901.0,
            current_r=-0.10,
            peak_mfe_r=0.18,
            grade="B",
            entry=100.0, sl=98.0,   # 2% SL → floor = 0.15
            trade_type="SCALP", regime="quiet",
        )
        assert result == EXIT_STALLED_AFTER_15MIN

    def test_does_not_fire_when_peak_above_threshold(self):
        result = should_kill_dead_signal(
            age_sec=901.0,
            current_r=-0.10,
            peak_mfe_r=0.21,   # above 0.20 stall threshold
            grade="B",
            entry=100.0, sl=98.0,
            trade_type="SCALP", regime="quiet",
        )
        assert result is None

    def test_does_not_fire_when_current_r_above_threshold(self):
        result = should_kill_dead_signal(
            age_sec=901.0,
            current_r=-0.04,   # above -0.05 stall threshold
            peak_mfe_r=0.18,
            grade="B",
            entry=100.0, sl=98.0,
            trade_type="SCALP", regime="quiet",
        )
        assert result is None

    def test_does_not_fire_before_900s(self):
        result = should_kill_dead_signal(
            age_sec=899.0,
            current_r=-0.10,
            peak_mfe_r=0.18,
            grade="B",
            entry=100.0, sl=98.0,
            trade_type="SCALP", regime="quiet",
        )
        # 899s is past A+/A patience but not past stall threshold; for B grade
        # patience=240s so primary kill fires (not stall) — that's also fine.
        assert result in (None, EXIT_DEAD_SIGNAL_UNIFIED)

    def test_primary_kill_takes_precedence_over_stall(self):
        """When both conditions are met, primary kill is returned first."""
        # peak < fee_floor AND age past stall + patience.
        # entry/sl gives floor=0.30; peak=0.10 < 0.30, current=-0.20.
        result = should_kill_dead_signal(
            age_sec=1200.0,
            current_r=-0.20,
            peak_mfe_r=0.10,
            grade="A+",
            entry=100.0, sl=99.7,    # tight SL → floor=0.30
            trade_type="SCALP", regime="quiet",
        )
        assert result == EXIT_DEAD_SIGNAL_UNIFIED


# ── should_kill_dead_signal: defensive ──────────────────────────────────────
class TestShouldKillDefensive:
    @pytest.mark.parametrize("bad", [None, "nan", float("nan")])
    def test_non_numeric_age_returns_none(self, bad):
        # NaN compares False against everything, so it should fall through to None.
        result = should_kill_dead_signal(
            age_sec=bad,
            current_r=-0.20,
            peak_mfe_r=0.10,
            grade="A+",
            entry=100.0, sl=99.35,
            trade_type="SCALP", regime="quiet",
        )
        assert result is None

    def test_none_grade_treated_as_lowgrade(self):
        """No grade → use B/C patience multiplier (2x), not the high-grade one."""
        result = should_kill_dead_signal(
            age_sec=241.0,
            current_r=-0.20,
            peak_mfe_r=0.10,
            grade=None,
            entry=100.0, sl=99.35,
            trade_type="SCALP", regime="quiet",
        )
        assert result == EXIT_DEAD_SIGNAL_UNIFIED

    def test_missing_regime_treated_as_inactive(self):
        result = should_kill_dead_signal(
            age_sec=481.0,
            current_r=-0.20,
            peak_mfe_r=0.10,
            grade="A+",
            entry=100.0, sl=99.35,
            trade_type="SCALP", regime=None,
        )
        assert result == EXIT_DEAD_SIGNAL_UNIFIED


# ── Integration-flavored sanity check ───────────────────────────────────────
def test_pre_5_8_quick_kill_scenario_no_longer_fires():
    """The smoking-gun trade from the design doc: SOL long, age=30s, peak=0.013R.

    Pre-5.8 quick_kill cut this. Post-5.8 should let it ride.
    """
    result = should_kill_dead_signal(
        age_sec=30.0,
        current_r=-0.06,
        peak_mfe_r=0.013,
        grade="A+",
        entry=86.0, sl=86.0 * (1 - 0.0065),
        trade_type="SCALP", regime="quiet",
    )
    assert result is None


def test_real_dud_eventually_dies():
    """A trade with zero favor at 10 minutes in quiet regime should die."""
    result = should_kill_dead_signal(
        age_sec=600.0,
        current_r=-0.30,
        peak_mfe_r=0.02,
        grade="B",
        entry=86.0, sl=86.0 * (1 - 0.0065),
        trade_type="SCALP", regime="quiet",
    )
    assert result == EXIT_DEAD_SIGNAL_UNIFIED
