"""Basic unit tests for signal_tracker — validates critical trade lifecycle logic.

These tests do NOT modify any production code or state files.
They test pure functions and dataclass behavior only.
"""
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_tracked_signal_creation():
    """TrackedSignal should create with correct defaults."""
    from bot.signal_tracker import TrackedSignal
    ts = TrackedSignal(
        trade_id="test123",
        symbol="BTC/USDT",
        side="short",
        entry_price=70000.0,
        stop_loss=70500.0,
    )
    assert ts.trade_id == "test123"
    assert ts.symbol == "BTC/USDT"
    assert ts.side == "short"
    assert ts.entry_price == 70000.0
    assert ts.stop_loss == 70500.0
    assert ts.status == "active"
    assert ts.tp1_hit is False
    assert ts.pnl_usd == 0.0
    assert ts.slippage_ticks == 0.0


def test_initial_risk_calculation():
    """Initial risk should be |entry - stop_loss|."""
    from bot.signal_tracker import TrackedSignal
    ts = TrackedSignal(
        trade_id="test",
        symbol="BTC/USDT",
        side="long",
        entry_price=70000.0,
        stop_loss=69500.0,
        initial_risk=500.0,
    )
    assert ts.initial_risk == 500.0


def test_r_multiple_long():
    """R-multiple for a winning long should be positive."""
    entry = 70000.0
    sl = 69500.0
    risk = abs(entry - sl)  # 500
    exit_price = 71000.0
    r_mult = (exit_price - entry) / risk
    assert r_mult == 2.0


def test_r_multiple_short():
    """R-multiple for a winning short should be positive."""
    entry = 70000.0
    sl = 70500.0
    risk = abs(entry - sl)  # 500
    exit_price = 69000.0
    r_mult = (entry - exit_price) / risk
    assert r_mult == 2.0


def test_pnl_calculation():
    """PnL should account for fees."""
    position_usd = 1000.0
    entry = 70000.0
    exit_price = 70700.0  # +1%
    pnl_pct = (exit_price - entry) / entry  # 0.01
    gross_pnl = pnl_pct * position_usd  # $10
    fee_pct = 0.0015  # 0.15% round trip
    fees = position_usd * fee_pct  # $1.50
    net_pnl = gross_pnl - fees  # $8.50
    assert abs(net_pnl - 8.50) < 0.01


def test_contract_size_mapping():
    """Contract sizes should match Delta India specs."""
    sizes = {"BTC/USDT": 0.001, "ETH/USDT": 0.01, "SOL/USDT": 1.0}
    assert sizes["BTC/USDT"] == 0.001
    assert sizes["ETH/USDT"] == 0.01
    assert sizes["SOL/USDT"] == 1.0


def test_trade_type_config():
    """Trade type configs should have required keys.

    Note: `trail_atr_mult` was removed in the Phase 4 refactor and replaced
    by `chandelier_mult_ranging` / `chandelier_mult_trending` per regime.
    """
    from bot.signal_tracker import TRADE_TYPE_CONFIG, TRADE_TYPE_SCALP, TRADE_TYPE_INTRADAY, TRADE_TYPE_RUNNER

    for tt in [TRADE_TYPE_SCALP, TRADE_TYPE_INTRADAY, TRADE_TYPE_RUNNER]:
        cfg = TRADE_TYPE_CONFIG[tt]
        assert "sl_atr_mult" in cfg
        assert "tp1_rr" in cfg
        # Regime-aware chandelier replaced the legacy trail_atr_mult constant
        assert "chandelier_mult_ranging" in cfg
        assert "chandelier_mult_trending" in cfg
        assert "max_age_sec" in cfg
        assert cfg["sl_atr_mult"] > 0
        assert cfg["max_age_sec"] > 0


def test_hold_profile_time_rules_off():
    """HOLD profile (2026-09-12): no time kills, 8h+ max age, chandelier gated.

    The old 15-20 min max age and 60-90 s early kill closed the median trade
    after 2 bars; the replay A/B showed they only added losses.
    """
    from bot.signal_tracker import TRADE_TYPE_CONFIG
    for tt, cfg in TRADE_TYPE_CONFIG.items():
        assert cfg["early_kill_sec"] == 0, tt
        assert cfg.get("no_momentum_sec", 0) == 0, tt
        assert cfg.get("dead_market_sec", 180) == 0, tt
        assert cfg["max_age_sec"] >= 8 * 3600, tt
        assert cfg["extended_age_sec"] >= cfg["max_age_sec"], tt
        assert cfg["full_extended_age_sec"] >= cfg["extended_age_sec"], tt
        assert cfg["chandelier_min_mfe_r"] >= 0.3, tt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
