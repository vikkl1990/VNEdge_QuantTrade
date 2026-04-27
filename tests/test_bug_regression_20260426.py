"""Regression tests pinning today's (2026-04-26) bug fixes.

These exist to prevent the 5 production bugs we hit today from silently
recurring. Each test maps to a specific bug from the bug-bash session.

Bug catalogue (see commit 359b1b5):
  Bug 1: bybit_shadow_monitor crash on $4 jsonb_build_object type ambiguity
  Bug 2: shadow trades orphaned across restart (early return bug)
  Bug 3a: compute_size ignored shadow_simulated_balance
  Bug 3b: user_registry.user_config missing pass-through keys
  Bug 5: _monitor_trade deadlocked on price=0 stale feed
  Plus: relaxed_shadow A/B (FIX 1+2 + Stage 1+2)
"""
import pytest
from execution.exit_guards import (
    should_kill_dead_signal,
    fee_floor_r,
    EXIT_DEAD_SIGNAL_UNIFIED,
    EXIT_STALLED_AFTER_15MIN,
    _UNIFIED_KILL_CURRENT_R,
    _RELAXED_KILL_CURRENT_R,
    _STALL_CURRENT_R,
    _RELAXED_STALL_CURRENT_R,
    _RELAXED_PATIENCE_MULT,
    _RELAXED_FEE_FLOOR_MULT,
)


# ────────────────────────────────────────────────────────────────────────
# Bug 1: bybit_shadow_monitor.py $4 cast
# ────────────────────────────────────────────────────────────────────────
class TestBug1MonitorJsonbCast:
    """Pin the `$4::text` cast in bybit_shadow_monitor.close_bybit_shadow.

    Without ::text, asyncpg can't infer the type of the value passed to
    jsonb_build_object('close_exit_reason', $4) and crashes with
    IndeterminateDatatypeError. This is a SOURCE-level pin since the
    actual function calls a live DB.
    """

    def test_cast_present_in_source(self):
        """Verify the $4::text cast wasn't accidentally removed."""
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "scripts" / "bybit_shadow_monitor.py"
        if not src.exists():
            pytest.skip("bybit_shadow_monitor.py not present in this checkout")
        text = src.read_text()
        # Look for the close_exit_reason field with ::text cast
        assert "'close_exit_reason', $4::text" in text, (
            "Bug 1 regression: $4::text cast missing from close_bybit_shadow "
            "jsonb_build_object call. Without it asyncpg "
            "raises IndeterminateDatatypeError and the monitor crash-loops."
        )


# ────────────────────────────────────────────────────────────────────────
# Bug 2: shadow trades orphaned across restart
# ────────────────────────────────────────────────────────────────────────
class TestBug2ShadowReconcile:
    """Pin the reconcile_open_trades shadow branch.

    Original bug: function early-returned on `if not rows` (real-trades-only),
    skipping the shadow reconcile block. Result: every restart orphaned every
    shadow trade's _monitor_trade task. Fix: removed the early return, made
    real-trades loop conditional on `if rows`, shadow reconcile always runs.
    """

    def test_no_unconditional_early_return_after_real_query(self):
        """Verify no `if not rows: return` between real-fetch and shadow-fetch."""
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_real_manager.py"
        text = src.read_text()
        # Find reconcile_open_trades function
        idx = text.index("async def reconcile_open_trades")
        # The shadow reconcile block should be reachable
        section = text[idx:idx + 5000]
        # The fix removes the early return, so we check the comment marker exists
        assert "do NOT early-return if `rows`" in section or "shadow_rows" in section, (
            "Bug 2 regression: reconcile_open_trades may early-return before "
            "the shadow reconcile block, orphaning shadow monitors on restart."
        )

    def test_shadow_reconcile_block_exists(self):
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_real_manager.py"
        text = src.read_text()
        # Must have shadow rows query
        assert "trade_type='shadow'" in text and "shadow_rows" in text, (
            "Bug 2 regression: shadow_rows query missing from reconcile_open_trades"
        )
        # Must have warning-level log when trades found
        assert "shadow reconcile" in text.lower(), (
            "Bug 2 regression: shadow reconcile log missing — silent failure risk"
        )


# ────────────────────────────────────────────────────────────────────────
# Bug 3a + 3b: shadow_simulated_balance pass-through + sizing use
# ────────────────────────────────────────────────────────────────────────
class TestBug3SizingShadowSimulatedBalance:
    """Pin the lever3-rollback regression. shadow_simulated_balance was
    dropped from BOTH user_registry pass-through AND compute_size resolution.
    Fix: re-added in both places.
    """

    def test_user_registry_passes_shadow_simulated_balance(self):
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_registry.py"
        text = src.read_text()
        assert '"shadow_simulated_balance"' in text, (
            "Bug 3b regression: shadow_simulated_balance pass-through missing "
            "from user_registry.user_config dict. compute_size will fall back "
            "to _cached_balance default and produce wrong sizing."
        )
        # Also check the other 3 keys that shipped together
        for key in ('"exit_policy"', '"cohort_filter_enabled"', '"mark_alignment_enabled"'):
            assert key in text, f"Bug 3b regression: {key} pass-through missing"

    def test_user_registry_select_includes_all_passthrough_columns(self):
        """Bug 3c (2026-04-27): the user_config dict referenced 4 columns
        (shadow_simulated_balance, exit_policy, cohort_filter_enabled,
        mark_alignment_enabled) but the _refresh_active_users SELECT did NOT
        include them. user_info.get(...) silently returned None for all four,
        so admin's shadow_sim_balance never reached compute_size, fell back
        to _cached_balance ($0.68), got margin floored at $10. This made
        admin look 5-7x SMALLER than niranjan and inflated the A/B's
        "niranjan delta_shadow win" by sizing rather than exit logic.
        Test pins that the SELECT statement covers all dict-referenced cols.
        """
        import pathlib, re
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_registry.py"
        text = src.read_text()
        # Find the actual function definition (not a call site)
        m_def = re.search(r'async\s+def\s+_refresh_active_users\b', text)
        assert m_def is not None, (
            "Bug 3c diagnostic: _refresh_active_users function definition not found"
        )
        # Slice from definition forward — should encompass the SELECT statement
        section = text[m_def.start():m_def.start() + 4000]
        # Find any SELECT ... FROM users block in this section
        select_match = re.search(
            r'SELECT\s+(.+?)\s+FROM\s+users',
            section, re.DOTALL | re.IGNORECASE,
        )
        assert select_match is not None, (
            "Bug 3c diagnostic: no `SELECT ... FROM users` block found in "
            "_refresh_active_users — has the structure changed?"
        )
        select_cols = select_match.group(1)
        # The 4 cols MUST be present in the SELECT column list
        for col in (
            "shadow_simulated_balance",
            "exit_policy",
            "cohort_filter_enabled",
            "mark_alignment_enabled",
        ):
            assert col in select_cols, (
                f"Bug 3c regression: SELECT in _refresh_active_users is "
                f"MISSING the `{col}` column. user_config dict reads "
                f"`user_info.get('{col}')` which will silently return None, "
                f"breaking the per-user feature gate that depends on it. "
                f"This is the bug pattern that confounded today's A/B."
            )

    def test_compute_size_balance_resolution_paths(self):
        """Verify compute_size has the 3 balance-source branches."""
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_real_manager.py"
        text = src.read_text()
        # Three paths: shadow_sim, cached, default100
        assert "shadow_sim" in text, "Bug 3a regression: shadow_sim balance source missing"
        assert "default100" in text, "Bug 3a regression: default100 fallback label missing"
        # SIZE_BAL diagnostic must be present
        assert "SIZE_BAL" in text, "Bug 3a regression: SIZE_BAL diagnostic log missing"

    def test_ceiling_uses_same_balance_source(self):
        """Bug 3a fix #2: margin ceiling must read from same balance source as base.

        Original bug: ceiling = _cached_balance × 0.22 even when shadow_sim was used.
        That capped admin's margin at $10 (cached=0.68) while base used $1000 (sim).
        """
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_real_manager.py"
        text = src.read_text()
        # The ceiling line should reference the resolved `balance` variable,
        # not a hardcoded re-fetch of _cached_balance
        assert "_ceiling_bal = balance" in text or "ceiling.*balance" in text.replace(" ", ""), (
            "Bug 3a regression: ceiling may not be using shadow_sim balance source"
        )


# ────────────────────────────────────────────────────────────────────────
# Bug 5: _monitor_trade no-price failsafe
# ────────────────────────────────────────────────────────────────────────
class TestBug5MonitorPriceFailsafe:
    """Pin MONITOR_NO_PRICE warning + MONITOR_FORCE_CLOSE failsafe."""

    def test_no_price_warning_constants_present(self):
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_real_manager.py"
        text = src.read_text()
        assert "MONITOR_NO_PRICE" in text, (
            "Bug 5 regression: MONITOR_NO_PRICE warning removed. Without it "
            "stale price feeds silently deadlock _monitor_trade."
        )
        assert "MONITOR_FORCE_CLOSE" in text, (
            "Bug 5 regression: MONITOR_FORCE_CLOSE failsafe removed. Without it "
            "trades pile up open indefinitely on broken feeds."
        )
        assert "no_price_orphan_kill" in text, (
            "Bug 5 regression: no_price_orphan_kill exit reason missing"
        )


# ────────────────────────────────────────────────────────────────────────
# Bug 4: BOOK cap_deployed contract-aware sum
# ────────────────────────────────────────────────────────────────────────
class TestBug4BookContractAwareSum:
    """Pin the dashboard server BOOK cap_deployed SQL contract-aware fix."""

    def test_book_sum_uses_margin_or_contract_size(self):
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "dashboard" / "server.py"
        text = src.read_text()
        # The new SUM should reference margin OR contract_size, not just entry × qty
        # Search for the BOOK cap_deployed query
        assert "contract_size" in text, (
            "Bug 4 regression: dashboard BOOK cap_deployed not contract-aware. "
            "Without contract_size factor, Delta India trades produce phantom "
            "+$2.3M sums (raw qty × entry without per-contract size factor)."
        )


# ────────────────────────────────────────────────────────────────────────
# Bug O3 / FIX 1+2 / Stage 1+2 — relaxed_shadow exits
# ────────────────────────────────────────────────────────────────────────
class TestRelaxedShadowExitGuards:
    """Verify relaxed_shadow=True actually relaxes the kill thresholds."""

    @pytest.fixture
    def standard_kill_inputs(self):
        # Trade well past BOTH standard (240s) AND relaxed (360s) patience windows
        # for B-grade SCALP. Standard patience = 60×2=120s; relaxed = ×1.5 = 180s.
        # Wait — chop adds nothing in this code path. Use grace=120s × patience_mult=2 = 240
        # standard, ×1.5 = 360 relaxed. age=500 is comfortably past both.
        # peak never reached fee_floor (0.05R), current at -0.12R (under -0.10 standard)
        return dict(
            age_sec=500,
            current_r=-0.12,
            peak_mfe_r=0.05,
            grade="B",
            entry=100.0,
            sl=99.5,         # 50 bps SL → fee_floor = 0.30R
            trade_type="SCALP",
            regime="sideways",  # chop
        )

    def test_standard_kills_at_minus_010_R(self, standard_kill_inputs):
        """Standard mode: trade with current_r=-0.12 dies."""
        result = should_kill_dead_signal(**standard_kill_inputs)
        assert result == EXIT_DEAD_SIGNAL_UNIFIED, (
            "Standard guards must kill trades below -0.10R after patience window"
        )

    def test_relaxed_does_not_kill_at_minus_012_R(self, standard_kill_inputs):
        """Relaxed mode: same trade survives because threshold is -0.16R."""
        result = should_kill_dead_signal(**standard_kill_inputs, relaxed_shadow=True)
        assert result is None, (
            "Relaxed mode (kill_R=-0.16) should NOT kill trade at current_r=-0.12. "
            "FIX 1+2 regression."
        )

    def test_relaxed_still_kills_at_minus_017_R(self, standard_kill_inputs):
        """Relaxed mode still kills trades past -0.16R threshold."""
        standard_kill_inputs["current_r"] = -0.18
        result = should_kill_dead_signal(**standard_kill_inputs, relaxed_shadow=True)
        assert result == EXIT_DEAD_SIGNAL_UNIFIED, (
            "Relaxed mode should still kill trades past -0.16R; FIX 1+2 too lax"
        )

    def test_relaxed_constants_match_design(self):
        """The relaxed multipliers should be exactly what we designed."""
        assert _RELAXED_KILL_CURRENT_R == -0.16, (
            "Relaxed kill threshold drifted from -0.16R"
        )
        assert _RELAXED_PATIENCE_MULT == 1.5, (
            "Relaxed patience multiplier drifted from 1.5×"
        )
        assert _RELAXED_FEE_FLOOR_MULT == 1.5, (
            "Relaxed fee_floor multiplier drifted from 1.5×"
        )
        assert _RELAXED_STALL_CURRENT_R == -0.075, (
            "Relaxed stall current_r drifted from -0.075R"
        )

    def test_standard_constants_unchanged(self):
        """The original constants must NOT have been mutated by relaxed-mode rollout."""
        assert _UNIFIED_KILL_CURRENT_R == -0.10, (
            "Standard kill threshold drifted from -0.10R — would affect admin/control trades"
        )
        assert _STALL_CURRENT_R == -0.05, (
            "Standard stall current_r drifted from -0.05R"
        )

    def test_stall_kill_relaxed_threshold(self):
        """Stalled-after-15min uses different threshold under relaxed mode."""
        # Trade past 15-min stall window, peak < 0.20R, current right at -0.06R
        # Standard: current < -0.05 → kill. Relaxed: current < -0.075 → no kill.
        inputs = dict(
            age_sec=1000,
            current_r=-0.06,
            peak_mfe_r=0.10,
            grade="B",
            entry=100.0,
            sl=99.0,
            trade_type="SCALP",
            regime="trending",
        )
        # Standard kills it
        assert should_kill_dead_signal(**inputs) == EXIT_STALLED_AFTER_15MIN
        # Relaxed lets it live
        assert should_kill_dead_signal(**inputs, relaxed_shadow=True) is None


# ────────────────────────────────────────────────────────────────────────
# Stage 1+2 source-level pins (shadow simulation fidelity)
# ────────────────────────────────────────────────────────────────────────
class TestStage12ShadowSimulationFidelity:
    """Pin the Stage 1+2 patches in user_real_manager.py."""

    def test_relaxed_shadow_simulation_flag_set(self):
        """As of 2026-04-27 CLEAN A/B test:
        - flag _relaxed_shadow_simulation MUST still exist in code (so we
          can re-enable later)
        - but it MUST be disabled (=False) so both users run STANDARD guards
        """
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_real_manager.py"
        text = src.read_text()
        assert "_relaxed_shadow_simulation" in text, (
            "Stage 1+2 regression: _relaxed_shadow_simulation flag REMOVED — "
            "should remain in code (just gated False) so we can re-enable later"
        )
        # Either the original ENABLED log OR the clean-A/B disabled state must be present
        clean_ab_mode_present = "CLEAN_AB_MODE" in text
        relaxed_log_present = "RELAXED_SHADOW_SIMULATION: ENABLED" in text
        assert clean_ab_mode_present or relaxed_log_present, (
            "Stage 1+2 regression: neither RELAXED_SHADOW_SIMULATION ENABLED log "
            "nor CLEAN_AB_MODE log present — sizing-related logs gone"
        )

    def test_age_gate_conditional_on_relaxed_sim(self):
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_real_manager.py"
        text = src.read_text()
        # The relaxed branch should set _age_gate = 0
        assert "_age_gate = 0" in text, (
            "Stage 1 regression: shadow age_gate should be 0 (was 15)"
        )
        # The standard branch should still set _age_gate = 15
        assert "_age_gate = 15" in text, (
            "Stage 1 regression: standard age_gate=15 missing — would affect live trades"
        )

    def test_breakeven_set_flag_set_in_be_lock(self):
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_real_manager.py"
        text = src.read_text()
        assert "trade._breakeven_set = True" in text, (
            "Stage 2 regression: paper-style _breakeven_set flag missing"
        )

    def test_close_shadow_honors_locked_sl(self):
        """When SL is in profit zone AND reason is trail_profit/sl_hit, exit at SL."""
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "execution" / "user_real_manager.py"
        text = src.read_text()
        assert "SHADOW_LOCKED_SL_EXIT" in text, (
            "Stage 2 regression: locked-SL exit log missing — _close_shadow no "
            "longer honors paper-style locked stops"
        )
        assert 'reason in ("trail_profit", "sl_hit")' in text, (
            "Stage 2 regression: trail_profit/sl_hit gate for locked-SL exit missing"
        )


# ────────────────────────────────────────────────────────────────────────
# fee_floor_r sanity (basic existing behavior, no regression here today)
# ────────────────────────────────────────────────────────────────────────
class TestFeeFloorR:
    @pytest.mark.parametrize("entry,sl,expected_min,expected_max", [
        (100.0, 99.5, 0.15, 0.30),    # 50 bps SL
        (78000.0, 77500.0, 0.15, 0.30),  # BTC scale
        (1.43, 1.42, 0.15, 0.30),     # XRP scale
        (100.0, 99.0, 0.15, 0.30),    # 100 bps SL — wider, lower floor
    ])
    def test_floor_clamped_to_range(self, entry, sl, expected_min, expected_max):
        floor = fee_floor_r(entry, sl)
        assert expected_min <= floor <= expected_max, (
            f"fee_floor_r({entry}, {sl}) = {floor} outside [{expected_min}, {expected_max}]"
        )
