"""
Integration tests for the VN Edge crypto bot trade pipelines.

Tests the full lifecycle of trades through TrackedSignal, SignalTracker,
RealCircuitBreaker, Trade model, StateManager, and PaperExecutionEngine.
"""

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta, date
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bot.signal_tracker import (
    TrackedSignal,
    SignalTracker,
    classify_trade,
    TRADE_TYPE_SCALP,
    TRADE_TYPE_INTRADAY,
    TRADE_TYPE_RUNNER,
    TRADE_TYPE_CONFIG,
)
from execution.real_manager import RealCircuitBreaker
from execution.trade import Trade, TradeSide, TradeStatus, TakeProfit
from utils.state_manager import StateManager


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_signal(
    symbol="BTCUSD",
    side="long",
    entry_price=66000.0,
    stop_loss=65500.0,
    take_profits=None,
    confidence=75,
    grade="B+",
    ml_probability=0.55,
    regime="trending_up",
    htf_bias=1,
    atr=150.0,
    vwap_zone="clear",
    trade_id="test-001",
    timestamp=None,
):
    if take_profits is None:
        take_profits = [66400.0, 66800.0, 67500.0]
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profits": take_profits,
        "confidence": confidence,
        "grade": grade,
        "reason": "test_signal",
        "timestamp": timestamp,
        "metadata": {
            "ml_probability": ml_probability,
            "regime": regime,
            "htf_bias": htf_bias,
            "atr": atr,
            "vwap_zone": vwap_zone,
            "setup_type": "momentum",
            "strategy_type": "scalp",
        },
    }


def _make_tracker():
    tracker = SignalTracker.__new__(SignalTracker)
    tracker._active = {}
    tracker._closed = []
    tracker._stats = {}
    tracker._exchange_balance = None
    tracker._paper_start_balance = 1000.0
    tracker._training_dataset = None
    tracker._updating_prices = False
    tracker._lock = asyncio.Lock()
    return tracker


def _make_full_tracker():
    tracker = _make_tracker()
    tracker._order_type = "maker"
    tracker._closed_recently = {}
    tracker._max_entry_slip_bps = 0
    tracker._min_trail_hold_sec = 0
    tracker._recent_candles = {}
    tracker.CHANDELIER_SHADOW = False
    tracker._live_feedback_file = None
    tracker._live_trade_count = 0
    tracker._save_active = lambda: None
    tracker._save_closed = lambda: None
    tracker._save_stats = lambda: None
    tracker._recalc_stats = lambda: None
    tracker._send_ml_feedback = lambda ts: None
    tracker._trigger_ml_feedback_blend = lambda: None
    return tracker


def _make_paper_config():
    return {
        "paper_trading": {
            "initial_balance": 1000.0,
            "taker_fee_rate": 0.0006,
            "maker_fee_rate": 0.0004,
            "fee_rate": 0.0006,
            "settlement_fee_rate": 0.0006,
            "slippage_pct": 0.05,
        },
        "risk": {
            "default_leverage": 5,
            "risk_per_trade_pct": 1.0,
            "take_profit": {
                "tp1_close_pct": 40,
                "tp2_close_pct": 30,
                "tp3_close_pct": 30,
            },
            "trailing": {
                "enabled": True,
                "activation_rr": 1.0,
                "trail_pct": 0.5,
                "break_even_after_tp1": True,
            },
        },
    }


# ═══════════════════════════════════════════════════════════════
# TEST 1: Long Trade Lifecycle
# ═══════════════════════════════════════════════════════════════

class TestLongTradeLifecycle:
    def test_long_tp1_trail_exit(self):
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0,
                           take_profits=[66400.0, 66800.0, 67500.0],
                           confidence=75, ml_probability=0.55)
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        assert ts.side == "long"
        assert ts.entry_price == 66000.0
        assert ts.status == "active"
        assert ts.initial_risk == 500.0

        events = tracker.update_prices({"BTCUSD": 66000.0})
        assert ts.status == "active"

        events = tracker.update_prices({"BTCUSD": 66200.0})
        assert ts.status == "active"
        assert ts.highest_price >= 66200.0

        events = tracker.update_prices({"BTCUSD": 66500.0})
        tp1_events = [e for e in events if e.get("type") == "tp1_hit"]
        assert len(tp1_events) >= 1, f"Expected TP1 event, got: {[e['type'] for e in events]}"
        assert ts.tp1_hit is True
        assert ts.atr_trail_active is True
        assert ts.stop_loss > ts.entry_price

        trail_sl = ts.stop_loss
        events = tracker.update_prices({"BTCUSD": trail_sl - 1.0})
        assert ts.trade_id not in tracker._active or ts.status != "active"
        assert ts.exit_reason in ("partial_win", "trail_profit", "stop_loss", "breakeven",
                                   "chandelier_trail", "trail_lock_50pct", "trail_lock_65pct",
                                   "trail_breakeven"), f"exit_reason: {ts.exit_reason}"


# ═══════════════════════════════════════════════════════════════
# TEST 2: Short Trade Lifecycle
# ═══════════════════════════════════════════════════════════════

class TestShortTradeLifecycle:
    def test_short_tp1_trail_exit(self):
        sig = _make_signal(symbol="ETHUSD", side="short", entry_price=3500.0,
                           stop_loss=3550.0, take_profits=[3460.0, 3400.0, 3300.0],
                           confidence=75, ml_probability=0.55, atr=20.0)
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        assert ts.side == "short"
        assert ts.initial_risk == 50.0

        events = tracker.update_prices({"ETHUSD": 3450.0})
        tp1_events = [e for e in events if e.get("type") == "tp1_hit"]
        assert len(tp1_events) >= 1
        assert ts.tp1_hit is True
        assert ts.stop_loss < ts.entry_price

        trail_sl = ts.stop_loss
        events = tracker.update_prices({"ETHUSD": trail_sl + 1.0})
        assert ts.exit_reason != ""


# ═══════════════════════════════════════════════════════════════
# TEST 3: Stop Loss Hit Detection
# ═══════════════════════════════════════════════════════════════

class TestSLHitDetection:
    def test_sl_hit_long(self):
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0, confidence=75, ml_probability=0.55)
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_full_tracker()
        tracker._active[ts.trade_id] = ts

        events = tracker.update_prices({"BTCUSD": 65400.0})
        sl_events = [e for e in events if e.get("type") == "sl_hit"]
        assert len(sl_events) >= 1 or ts.sl_hit or ts.exit_reason == "hard_loss_cap"
        assert ts.exit_reason in ("stop_loss", "hard_loss_cap")
        assert ts.pnl_pct < 0

    def test_sl_hit_short(self):
        sig = _make_signal(symbol="ETHUSD", side="short", entry_price=3500.0,
                           stop_loss=3550.0, take_profits=[3460.0, 3400.0, 3300.0],
                           confidence=75, ml_probability=0.55, atr=20.0)
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_full_tracker()
        tracker._active[ts.trade_id] = ts

        events = tracker.update_prices({"ETHUSD": 3560.0})
        assert ts.sl_hit or ts.exit_reason in ("stop_loss", "hard_loss_cap")
        assert ts.pnl_pct < 0


# ═══════════════════════════════════════════════════════════════
# TEST 4: Time Stop / Early Kill
# ═══════════════════════════════════════════════════════════════

class TestEarlyKillTimeStop:
    def test_early_kill_scalp(self):
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0, confidence=60,
                           ml_probability=0.40, regime="ranging", htf_bias=0)
        ts = TrackedSignal.from_signal(sig)
        assert ts.trade_type == TRADE_TYPE_SCALP

        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(seconds=130)).isoformat()
        ts.highest_price = 66020.0
        ts.lowest_price = 65900.0

        events = tracker.update_prices({"BTCUSD": 65900.0})
        assert ts.exit_reason.startswith("early_kill") or ts.time_stop_triggered, \
            f"Expected early_kill, got {ts.exit_reason}"

    def test_momentum_kill_intraday(self):
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0, confidence=75,
                           ml_probability=0.55, regime="trending_up", htf_bias=1)
        ts = TrackedSignal.from_signal(sig)
        assert ts.trade_type == TRADE_TYPE_INTRADAY

        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(seconds=310)).isoformat()
        ts.highest_price = 66030.0
        ts.lowest_price = 65950.0

        events = tracker.update_prices({"BTCUSD": 65950.0})
        assert ts.exit_reason.startswith("momentum_kill") or ts.time_stop_triggered, \
            f"Expected momentum_kill, got {ts.exit_reason}"


# ═══════════════════════════════════════════════════════════════
# TEST 5: Circuit Breaker
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_circuit_breaker_trips_on_daily_loss(self):
        cb = RealCircuitBreaker(daily_loss_limit=25.0, max_consecutive_losses=3)
        assert cb.is_tripped is False
        cb.record_trade(-10.0)
        cb.record_trade(-10.0)
        cb.record_trade(-10.0)
        assert cb.is_tripped is True
        assert "exceeds" in cb.trip_reason.lower() or "loss" in cb.trip_reason.lower()

    def test_circuit_breaker_trips_on_consecutive_losses(self):
        cb = RealCircuitBreaker(daily_loss_limit=100.0, max_consecutive_losses=3)
        cb.record_trade(-2.0)
        cb.record_trade(-2.0)
        assert cb.is_tripped is False
        cb.record_trade(-2.0)
        assert cb.is_tripped is True
        assert "consecutive" in cb.trip_reason.lower()

    def test_circuit_breaker_resets_on_new_day(self):
        cb = RealCircuitBreaker(daily_loss_limit=25.0, max_consecutive_losses=3)
        cb.record_trade(-30.0)
        assert cb.is_tripped is True
        # The breaker keeps its day in UTC; use the same clock here so the
        # test doesn't break in evenings east of UTC.
        cb.today = str(datetime.now(timezone.utc).date() - timedelta(days=1))
        allowed, reason = cb.is_allowed()
        assert allowed is True
        assert cb.is_tripped is False

    def test_circuit_breaker_ignores_micro_losses(self):
        cb = RealCircuitBreaker(daily_loss_limit=100.0, max_consecutive_losses=3)
        cb.record_trade(-2.0)
        cb.record_trade(-0.30)
        cb.record_trade(-2.0)
        assert cb.consecutive_losses == 2
        assert cb.is_tripped is False


# ═══════════════════════════════════════════════════════════════
# TEST 6: Trade Classification
# ═══════════════════════════════════════════════════════════════

class TestTradeClassification:
    def test_classify_scalp_low_ml(self):
        sig = _make_signal(ml_probability=0.35, regime="ranging", htf_bias=0)
        assert classify_trade(sig) == TRADE_TYPE_SCALP

    def test_classify_intraday_mid_ml(self):
        sig = _make_signal(ml_probability=0.55, regime="trending_up", htf_bias=1)
        assert classify_trade(sig) == TRADE_TYPE_INTRADAY

    def test_classify_runner_high_ml(self):
        sig = _make_signal(ml_probability=0.70, regime="trending_up", htf_bias=1, vwap_zone="clear")
        assert classify_trade(sig) == TRADE_TYPE_RUNNER

    def test_downgrade_runner_in_ranging(self):
        sig = _make_signal(ml_probability=0.70, regime="ranging", htf_bias=1)
        assert classify_trade(sig) == TRADE_TYPE_INTRADAY

    def test_upgrade_scalp_trending_htf(self):
        sig = _make_signal(side="long", ml_probability=0.40, regime="trending_up", htf_bias=1)
        assert classify_trade(sig) == TRADE_TYPE_INTRADAY

    def test_downgrade_intraday_ranging_vwap_noise(self):
        sig = _make_signal(ml_probability=0.55, regime="ranging", htf_bias=0, vwap_zone="noise")
        assert classify_trade(sig) == TRADE_TYPE_SCALP


# ═══════════════════════════════════════════════════════════════
# TEST 7: Fee Calculation
# ═══════════════════════════════════════════════════════════════

class TestFeeCalculation:
    def test_fee_calculation_standard(self):
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0, confidence=75, ml_probability=0.55)
        ts = TrackedSignal.from_signal(sig)
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        ts.exit_time = datetime.now(timezone.utc).isoformat()
        ts.exit_price = 66200.0
        tracker = _make_tracker()
        pnl = tracker._calc_pnl(ts, 66200.0)
        expected_fee_pct = 0.177
        assert abs(ts.total_fees_pct - expected_fee_pct) < 0.01
        assert ts.fee_type == "standard"
        assert ts.gross_pnl_pct > ts.total_fees_pct

    def test_fee_calculation_scalper(self):
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0, confidence=75, ml_probability=0.55)
        ts = TrackedSignal.from_signal(sig)
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        ts.exit_time = datetime.now(timezone.utc).isoformat()
        ts.exit_price = 66200.0
        tracker = _make_tracker()
        pnl = tracker._calc_pnl(ts, 66200.0)
        expected_fee_pct = 0.079
        assert abs(ts.total_fees_pct - expected_fee_pct) < 0.01
        assert ts.fee_type == "scalper"
        assert ts.within_scalper is True


# ═══════════════════════════════════════════════════════════════
# TEST 8: Price Sanity Check
# ═══════════════════════════════════════════════════════════════

class TestPriceSanityCheck:
    def test_price_sanity_rejects_wrong_symbol(self):
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0)
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts
        events = tracker.update_prices({"BTCUSD": 82.95})
        assert ts.status == "active"

    def test_price_sanity_accepts_normal_move(self):
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0)
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts
        events = tracker.update_prices({"BTCUSD": 66660.0})
        assert ts.highest_price >= 66660.0


# ═══════════════════════════════════════════════════════════════
# TEST 9: Contract Sizing
# ═══════════════════════════════════════════════════════════════

class TestContractSizing:
    def test_contract_sizing_btc(self):
        sig = _make_signal(symbol="BTCUSD", entry_price=66000.0, stop_loss=65500.0, confidence=75)
        ts = TrackedSignal.from_signal(sig)
        assert ts.contract_size == 0.001
        assert ts.contracts >= 1
        assert isinstance(ts.contracts, int)
        assert abs(ts.quantity - ts.contracts * 0.001) < 1e-6
        assert abs(ts.position_size_usd - ts.quantity * 66000.0) < 1.0

    def test_contract_sizing_sol(self):
        sig = _make_signal(symbol="SOLUSD", entry_price=140.0, stop_loss=138.0,
                           take_profits=[141.5, 143.0, 146.0], confidence=75, atr=2.0)
        ts = TrackedSignal.from_signal(sig)
        assert ts.contract_size == 0.1
        assert ts.contracts >= 1
        assert abs(ts.quantity - ts.contracts * 0.1) < 1e-6

    def test_contract_sizing_eth(self):
        sig = _make_signal(symbol="ETHUSD", entry_price=3500.0, stop_loss=3450.0,
                           take_profits=[3540.0, 3600.0, 3700.0], confidence=75, atr=20.0)
        ts = TrackedSignal.from_signal(sig)
        assert ts.contract_size == 0.01


# ═══════════════════════════════════════════════════════════════
# TEST 10: R-Multiple Calculation
# ═══════════════════════════════════════════════════════════════

class TestRMultipleCalculation:
    def test_r_multiple_long_win(self):
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0, confidence=75, ml_probability=0.55)
        ts = TrackedSignal.from_signal(sig)
        assert ts.initial_risk == 500.0
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        ts.exit_time = datetime.now(timezone.utc).isoformat()
        tracker = _make_tracker()
        pnl = tracker._calc_pnl(ts, 67000.0)
        assert ts.exit_r > 1.5
        assert ts.exit_r < 2.1

    def test_r_multiple_short_loss(self):
        sig = _make_signal(symbol="ETHUSD", side="short", entry_price=3500.0, stop_loss=3550.0,
                           take_profits=[3460.0, 3400.0, 3300.0], confidence=75,
                           ml_probability=0.55, atr=20.0)
        ts = TrackedSignal.from_signal(sig)
        assert ts.initial_risk == 50.0
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        ts.exit_time = datetime.now(timezone.utc).isoformat()
        tracker = _make_tracker()
        pnl = tracker._calc_pnl(ts, 3530.0)
        assert ts.exit_r < -0.5
        assert ts.exit_r > -1.5

    def test_r_multiple_breakeven(self):
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0, confidence=75)
        ts = TrackedSignal.from_signal(sig)
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        ts.exit_time = datetime.now(timezone.utc).isoformat()
        tracker = _make_tracker()
        pnl = tracker._calc_pnl(ts, 66000.0)
        assert ts.exit_r < 0
        assert ts.exit_r > -0.5

