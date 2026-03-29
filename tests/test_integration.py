"""
Integration tests for the VN Edge crypto bot trade pipelines.

Tests the full lifecycle of trades through TrackedSignal, SignalTracker,
RealCircuitBreaker, Trade model, and classification logic.
"""

import sys
import time
from datetime import datetime, timezone, timedelta, date
from unittest.mock import patch

import pytest

sys.path.insert(0, "/Users/scorpion/Desktop/Claude AI Crypto Bot/crypto-trading-bot")

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
    """Build a signal dict matching what the bot produces."""
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
    """Create a SignalTracker that does not touch disk."""
    tracker = SignalTracker.__new__(SignalTracker)
    tracker._active = {}
    tracker._closed = []
    tracker._stats = {}
    tracker._exchange_balance = None
    tracker._paper_start_balance = 1000.0
    tracker._training_dataset = None
    tracker._updating_prices = False
    import asyncio
    tracker._lock = asyncio.Lock()
    return tracker


# ═══════════════════════════════════════════════════════════════
# TEST 1: Long Trade Lifecycle (TP1 → trail → exit)
# ═══════════════════════════════════════════════════════════════

class TestLongTradeLifecycle:
    def test_long_tp1_trail_exit(self):
        """Signal created -> price rises -> TP1 hit -> trail engages -> SL exit."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,
            take_profits=[66400.0, 66800.0, 67500.0],
            confidence=75,
            ml_probability=0.55,
        )
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        # Verify initial state
        assert ts.side == "long"
        assert ts.entry_price == 66000.0
        assert ts.status == "active"
        assert ts.initial_risk == 500.0  # |66000 - 65500|

        # Feed price at entry (no change)
        events = tracker.update_prices({"BTCUSD": 66000.0})
        assert ts.status == "active"

        # Price rises toward TP1
        events = tracker.update_prices({"BTCUSD": 66200.0})
        assert ts.status == "active"
        assert ts.highest_price >= 66200.0

        # Price hits TP1
        events = tracker.update_prices({"BTCUSD": 66500.0})
        tp1_events = [e for e in events if e.get("type") == "tp1_hit"]
        assert len(tp1_events) >= 1, f"Expected TP1 event, got events: {[e['type'] for e in events]}"
        assert ts.tp1_hit is True
        assert ts.atr_trail_active is True
        # SL should have moved above entry (trail engaged)
        assert ts.stop_loss > ts.entry_price, f"Trail SL {ts.stop_loss} should be above entry {ts.entry_price}"

        # Price retreats back and hits the trailed SL
        trail_sl = ts.stop_loss
        events = tracker.update_prices({"BTCUSD": trail_sl - 1.0})
        # Should be closed now
        assert ts.trade_id not in tracker._active or ts.status != "active"
        assert ts.exit_reason in ("partial_win", "trail_profit", "stop_loss", "breakeven",
                                   "trail_lock_50pct", "trail_lock_65pct", "trail_breakeven"), \
            f"Unexpected exit_reason: {ts.exit_reason}"


# ═══════════════════════════════════════════════════════════════
# TEST 2: Short Trade Lifecycle
# ═══════════════════════════════════════════════════════════════

class TestShortTradeLifecycle:
    def test_short_tp1_trail_exit(self):
        """Short signal -> price drops -> TP1 hit -> trail -> exit."""
        sig = _make_signal(
            symbol="ETHUSD",
            side="short",
            entry_price=3500.0,
            stop_loss=3550.0,
            take_profits=[3460.0, 3400.0, 3300.0],
            confidence=75,
            ml_probability=0.55,
            atr=20.0,
        )
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        assert ts.side == "short"
        assert ts.initial_risk == 50.0  # |3500 - 3550|

        # Price drops to TP1
        events = tracker.update_prices({"ETHUSD": 3450.0})
        tp1_events = [e for e in events if e.get("type") == "tp1_hit"]
        assert len(tp1_events) >= 1, f"Expected TP1 event for short, got: {[e['type'] for e in events]}"
        assert ts.tp1_hit is True
        assert ts.atr_trail_active is True
        # For short, trail SL should be below entry
        assert ts.stop_loss < ts.entry_price, f"Short trail SL {ts.stop_loss} should be below entry {ts.entry_price}"

        # Price bounces and hits the trail
        trail_sl = ts.stop_loss
        events = tracker.update_prices({"ETHUSD": trail_sl + 1.0})
        assert ts.exit_reason != "", f"Trade should be closed, exit_reason is empty"


# ═══════════════════════════════════════════════════════════════
# TEST 3: Stop Loss Hit Detection
# ═══════════════════════════════════════════════════════════════

class TestSLHitDetection:
    def test_sl_hit_long(self):
        """Long trade: price drops below SL -> exit at SL."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,
            confidence=75,
            ml_probability=0.55,
        )
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        # Price drops through SL
        events = tracker.update_prices({"BTCUSD": 65400.0})
        sl_events = [e for e in events if e.get("type") == "sl_hit"]
        assert len(sl_events) >= 1 or ts.sl_hit, f"SL should have hit, events: {[e['type'] for e in events]}"
        assert ts.exit_reason in ("stop_loss", "hard_loss_cap"), f"Expected stop_loss exit, got: {ts.exit_reason}"
        assert ts.pnl_pct < 0, f"SL exit should have negative PnL, got {ts.pnl_pct}"

    def test_sl_hit_short(self):
        """Short trade: price rises above SL -> exit at SL."""
        sig = _make_signal(
            symbol="ETHUSD",
            side="short",
            entry_price=3500.0,
            stop_loss=3550.0,
            take_profits=[3460.0, 3400.0, 3300.0],
            confidence=75,
            ml_probability=0.55,
            atr=20.0,
        )
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        # Price rises through SL
        events = tracker.update_prices({"ETHUSD": 3560.0})
        assert ts.sl_hit or ts.exit_reason in ("stop_loss", "hard_loss_cap"), \
            f"Short SL should have hit at 3560 (SL=3550), exit_reason={ts.exit_reason}"
        assert ts.pnl_pct < 0


# ═══════════════════════════════════════════════════════════════
# TEST 4: Time Stop / Early Kill
# ═══════════════════════════════════════════════════════════════

class TestEarlyKillTimeStop:
    def test_early_kill_scalp(self):
        """SCALP trade killed after 120s with no MFE progress."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,
            confidence=60,
            ml_probability=0.40,  # low prob -> SCALP
            regime="ranging",
            htf_bias=0,
        )
        ts = TrackedSignal.from_signal(sig)
        assert ts.trade_type == TRADE_TYPE_SCALP, f"Expected SCALP, got {ts.trade_type}"

        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        # Manipulate entry_time to be 130s ago
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(seconds=130)).isoformat()
        # MFE never exceeds early_kill_mfe threshold (0.10R)
        # current_R is negative (-0.15R or worse)
        # Price slightly below entry: 66000 - 0.20 * 500 = 65900
        price = 65900.0
        ts.highest_price = 66020.0  # MFE ~ 0.04R (below 0.10R threshold)
        ts.lowest_price = price

        events = tracker.update_prices({"BTCUSD": price})
        time_events = [e for e in events if e.get("type") == "time_stop"]
        # Should trigger early_kill
        assert ts.exit_reason.startswith("early_kill") or ts.time_stop_triggered, \
            f"Expected early_kill, got exit_reason={ts.exit_reason}, time_stop={ts.time_stop_triggered}, events={[e['type'] for e in events]}"

    def test_momentum_kill_intraday(self):
        """INTRADAY killed after 5min with MFE < 0.10R."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,
            confidence=75,
            ml_probability=0.55,  # mid prob -> INTRADAY
            regime="trending_up",
            htf_bias=1,
        )
        ts = TrackedSignal.from_signal(sig)
        assert ts.trade_type == TRADE_TYPE_INTRADAY, f"Expected INTRADAY, got {ts.trade_type}"

        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        # Manipulate entry_time to be 310s ago (>300s = 5min)
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(seconds=310)).isoformat()
        ts.highest_price = 66030.0  # MFE ~ 0.06R (below 0.10R)
        ts.lowest_price = 65950.0
        price = 65950.0

        events = tracker.update_prices({"BTCUSD": price})
        assert ts.exit_reason.startswith("momentum_kill") or ts.time_stop_triggered, \
            f"Expected momentum_kill, got exit_reason={ts.exit_reason}, events={[e['type'] for e in events]}"


# ═══════════════════════════════════════════════════════════════
# TEST 5: Circuit Breaker
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_circuit_breaker_trips_on_daily_loss(self):
        """CB trips after exceeding daily loss limit."""
        cb = RealCircuitBreaker(daily_loss_limit=25.0, max_consecutive_losses=3)
        assert cb.is_tripped is False

        cb.record_trade(-10.0)
        assert cb.is_tripped is False
        cb.record_trade(-10.0)
        assert cb.is_tripped is False
        cb.record_trade(-10.0)  # cumulative: -$30, exceeds $25 limit
        assert cb.is_tripped is True
        assert "exceeds" in cb.trip_reason.lower() or "loss" in cb.trip_reason.lower()

    def test_circuit_breaker_trips_on_consecutive_losses(self):
        """CB trips after 3 consecutive real losses (>$0.50 each)."""
        cb = RealCircuitBreaker(daily_loss_limit=100.0, max_consecutive_losses=3)
        cb.record_trade(-2.0)
        cb.record_trade(-2.0)
        assert cb.is_tripped is False
        cb.record_trade(-2.0)  # 3rd consecutive loss
        assert cb.is_tripped is True
        assert "consecutive" in cb.trip_reason.lower()

    def test_circuit_breaker_resets_on_new_day(self):
        """CB resets when the date changes."""
        cb = RealCircuitBreaker(daily_loss_limit=25.0, max_consecutive_losses=3)
        cb.record_trade(-30.0)
        assert cb.is_tripped is True

        # Simulate new day by changing the stored date
        cb.today = str(date.today() - timedelta(days=1))
        allowed, reason = cb.is_allowed()
        # _maybe_reset_daily should trigger on is_allowed call
        assert allowed is True, f"CB should allow after day reset, reason: {reason}"
        assert cb.is_tripped is False

    def test_circuit_breaker_ignores_micro_losses(self):
        """Micro-losses (<$0.50) should not count toward consecutive streak."""
        cb = RealCircuitBreaker(daily_loss_limit=100.0, max_consecutive_losses=3)
        cb.record_trade(-2.0)
        cb.record_trade(-0.30)  # micro-loss, should not increment
        cb.record_trade(-2.0)
        assert cb.consecutive_losses == 2, f"Expected 2, got {cb.consecutive_losses}"
        assert cb.is_tripped is False


# ═══════════════════════════════════════════════════════════════
# TEST 6: Trade Classification
# ═══════════════════════════════════════════════════════════════

class TestTradeClassification:
    def test_classify_scalp_low_ml(self):
        """Low ML probability (< 0.50) -> SCALP."""
        sig = _make_signal(ml_probability=0.35, regime="ranging", htf_bias=0)
        result = classify_trade(sig)
        assert result == TRADE_TYPE_SCALP, f"Expected SCALP, got {result}"

    def test_classify_intraday_mid_ml(self):
        """Mid ML probability (0.50-0.64) -> INTRADAY."""
        sig = _make_signal(ml_probability=0.55, regime="trending_up", htf_bias=1)
        result = classify_trade(sig)
        assert result == TRADE_TYPE_INTRADAY, f"Expected INTRADAY, got {result}"

    def test_classify_runner_high_ml(self):
        """High ML probability (>= 0.65) -> RUNNER."""
        sig = _make_signal(ml_probability=0.70, regime="trending_up", htf_bias=1, vwap_zone="clear")
        result = classify_trade(sig)
        assert result == TRADE_TYPE_RUNNER, f"Expected RUNNER, got {result}"

    def test_downgrade_runner_in_ranging(self):
        """RUNNER downgraded to INTRADAY in ranging regime."""
        sig = _make_signal(ml_probability=0.70, regime="ranging", htf_bias=1)
        result = classify_trade(sig)
        assert result == TRADE_TYPE_INTRADAY, f"Expected INTRADAY (downgrade), got {result}"

    def test_upgrade_scalp_trending_htf(self):
        """SCALP upgraded to INTRADAY when trending + HTF aligned."""
        sig = _make_signal(
            side="long",
            ml_probability=0.40,
            regime="trending_up",
            htf_bias=1,
        )
        result = classify_trade(sig)
        assert result == TRADE_TYPE_INTRADAY, f"Expected INTRADAY (upgrade), got {result}"

    def test_downgrade_intraday_ranging_vwap_noise(self):
        """INTRADAY downgraded to SCALP in ranging + VWAP noise."""
        sig = _make_signal(ml_probability=0.55, regime="ranging", htf_bias=0, vwap_zone="noise")
        result = classify_trade(sig)
        assert result == TRADE_TYPE_SCALP, f"Expected SCALP (downgrade), got {result}"


# ═══════════════════════════════════════════════════════════════
# TEST 7: Fee Calculation
# ═══════════════════════════════════════════════════════════════

class TestFeeCalculation:
    def test_fee_calculation_standard(self):
        """Verify standard fee math (outside scalper window)."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,
            confidence=75,
            ml_probability=0.55,
        )
        ts = TrackedSignal.from_signal(sig)
        # Simulate a close outside scalper window
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        ts.exit_time = datetime.now(timezone.utc).isoformat()
        ts.exit_price = 66200.0

        tracker = _make_tracker()
        pnl = tracker._calc_pnl(ts, 66200.0)

        # Standard fees: 0.059% * 2 + 0.059% = 0.177%
        expected_fee_pct = 0.177
        assert abs(ts.total_fees_pct - expected_fee_pct) < 0.01, \
            f"Expected ~{expected_fee_pct}% fees, got {ts.total_fees_pct}%"
        assert ts.fee_type == "standard"
        assert ts.gross_pnl_pct > ts.total_fees_pct, \
            f"Gross PnL {ts.gross_pnl_pct}% should exceed fees {ts.total_fees_pct}% for a winning trade"

    def test_fee_calculation_scalper(self):
        """Verify scalper fee math (within scalper window)."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,
            confidence=75,
            ml_probability=0.55,
        )
        ts = TrackedSignal.from_signal(sig)
        # Simulate a close within scalper window (< 14 min for BTC)
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        ts.exit_time = datetime.now(timezone.utc).isoformat()
        ts.exit_price = 66200.0

        tracker = _make_tracker()
        pnl = tracker._calc_pnl(ts, 66200.0)

        # Scalper fees: 0.02% entry + 0.00% exit + 0.059% settlement = 0.079%
        expected_fee_pct = 0.079
        assert abs(ts.total_fees_pct - expected_fee_pct) < 0.01, \
            f"Expected ~{expected_fee_pct}% scalper fees, got {ts.total_fees_pct}%"
        assert ts.fee_type == "scalper"
        assert ts.within_scalper is True


# ═══════════════════════════════════════════════════════════════
# TEST 8: Price Sanity Check
# ═══════════════════════════════════════════════════════════════

class TestPriceSanityCheck:
    def test_price_sanity_rejects_wrong_symbol(self):
        """Price 82.95 on BTC trade should be skipped (>15% deviation from 66000)."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,
        )
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        original_highest = ts.highest_price
        original_lowest = ts.lowest_price

        # Feed a price that's clearly from a different symbol (SOL price on BTC)
        events = tracker.update_prices({"BTCUSD": 82.95})
        # Should be skipped — no state change
        assert ts.status == "active", "Price sanity should prevent any status change"
        # The price sanity check skips the entire price update, so SL/TP won't fire

    def test_price_sanity_accepts_normal_move(self):
        """Normal 1% price move should be accepted."""
        sig = _make_signal(entry_price=66000.0, stop_loss=65500.0)
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        # 1% move is well within 15% threshold
        events = tracker.update_prices({"BTCUSD": 66660.0})
        assert ts.highest_price >= 66660.0, "Normal price should be accepted"


# ═══════════════════════════════════════════════════════════════
# TEST 9: Contract Sizing
# ═══════════════════════════════════════════════════════════════

class TestContractSizing:
    def test_contract_sizing_btc(self):
        """BTC contract_size=0.001, verify integer contract count."""
        sig = _make_signal(
            symbol="BTCUSD",
            entry_price=66000.0,
            stop_loss=65500.0,
            confidence=75,
        )
        ts = TrackedSignal.from_signal(sig)

        assert ts.contract_size == 0.001, f"BTC contract size should be 0.001, got {ts.contract_size}"
        assert ts.contracts >= 1, f"Should have at least 1 contract, got {ts.contracts}"
        assert isinstance(ts.contracts, int), "Contracts must be integer"
        # Verify: quantity = contracts * contract_size
        expected_qty = ts.contracts * 0.001
        assert abs(ts.quantity - expected_qty) < 1e-6, \
            f"quantity={ts.quantity} should equal contracts*0.001={expected_qty}"
        # Verify: position_size_usd ≈ quantity * entry_price
        expected_pos = ts.quantity * 66000.0
        assert abs(ts.position_size_usd - expected_pos) < 1.0, \
            f"position_size_usd={ts.position_size_usd} should ≈ {expected_pos}"

    def test_contract_sizing_sol(self):
        """SOL contract_size=0.1."""
        sig = _make_signal(
            symbol="SOLUSD",
            entry_price=140.0,
            stop_loss=138.0,
            take_profits=[141.5, 143.0, 146.0],
            confidence=75,
            atr=2.0,
        )
        ts = TrackedSignal.from_signal(sig)

        assert ts.contract_size == 0.1, f"SOL contract size should be 0.1, got {ts.contract_size}"
        assert ts.contracts >= 1
        expected_qty = ts.contracts * 0.1
        assert abs(ts.quantity - expected_qty) < 1e-6

    def test_contract_sizing_eth(self):
        """ETH contract_size=0.01."""
        sig = _make_signal(
            symbol="ETHUSD",
            entry_price=3500.0,
            stop_loss=3450.0,
            take_profits=[3540.0, 3600.0, 3700.0],
            confidence=75,
            atr=20.0,
        )
        ts = TrackedSignal.from_signal(sig)
        assert ts.contract_size == 0.01, f"ETH contract size should be 0.01, got {ts.contract_size}"


# ═══════════════════════════════════════════════════════════════
# TEST 10: R-Multiple Calculation
# ═══════════════════════════════════════════════════════════════

class TestRMultipleCalculation:
    def test_r_multiple_long_win(self):
        """Long: entry=66000, SL=65500, exit=67000 -> R ~ 2.0R (before fees)."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,
            confidence=75,
            ml_probability=0.55,
        )
        ts = TrackedSignal.from_signal(sig)
        assert ts.initial_risk == 500.0

        # Close outside scalper window for known fee rate
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        ts.exit_time = datetime.now(timezone.utc).isoformat()

        tracker = _make_tracker()
        pnl = tracker._calc_pnl(ts, 67000.0)

        # Gross R = (67000 - 66000) / 500 = 2.0
        # Fee impact = 66000 * 0.00177 / 500 ≈ 0.234R
        # Net R ≈ 2.0 - 0.234 ≈ 1.77
        assert ts.exit_r > 1.5, f"Expected exit_r > 1.5R for a 2R gross win, got {ts.exit_r}"
        assert ts.exit_r < 2.1, f"Expected exit_r < 2.1 (fees reduce it), got {ts.exit_r}"

    def test_r_multiple_short_loss(self):
        """Short: entry=3500, SL=3550, exit=3530 -> R ~ -0.6R (before fees)."""
        sig = _make_signal(
            symbol="ETHUSD",
            side="short",
            entry_price=3500.0,
            stop_loss=3550.0,
            take_profits=[3460.0, 3400.0, 3300.0],
            confidence=75,
            ml_probability=0.55,
            atr=20.0,
        )
        ts = TrackedSignal.from_signal(sig)
        assert ts.initial_risk == 50.0  # |3500 - 3550|

        ts.entry_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        ts.exit_time = datetime.now(timezone.utc).isoformat()

        tracker = _make_tracker()
        pnl = tracker._calc_pnl(ts, 3530.0)

        # Gross R for short = (3500 - 3530) / 50 = -0.6
        # Fee impact makes it worse
        assert ts.exit_r < -0.5, f"Expected exit_r < -0.5 for a losing short, got {ts.exit_r}"
        assert ts.exit_r > -1.5, f"exit_r shouldn't be worse than -1.5, got {ts.exit_r}"

    def test_r_multiple_breakeven(self):
        """Exit at entry price -> R near 0 (slightly negative due to fees)."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,
            confidence=75,
        )
        ts = TrackedSignal.from_signal(sig)
        ts.entry_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        ts.exit_time = datetime.now(timezone.utc).isoformat()

        tracker = _make_tracker()
        pnl = tracker._calc_pnl(ts, 66000.0)

        # Exit at entry -> gross R = 0, but fees make it slightly negative
        assert ts.exit_r < 0, f"Breakeven should be slightly negative after fees, got {ts.exit_r}"
        assert ts.exit_r > -0.5, f"Breakeven fee drag shouldn't exceed -0.5R, got {ts.exit_r}"


# ═══════════════════════════════════════════════════════════════
# TEST: Trade Model (execution/trade.py)
# ═══════════════════════════════════════════════════════════════

class TestTradeModel:
    def test_trade_stop_hit_long(self):
        """Trade.is_stop_hit correctly detects long SL breach."""
        trade = Trade(
            symbol="BTCUSD",
            side=TradeSide.LONG,
            entry_price=66000.0,
            stop_loss=65500.0,
            current_sl=65500.0,
        )
        assert trade.is_stop_hit(65600.0) is False
        assert trade.is_stop_hit(65500.0) is True
        assert trade.is_stop_hit(65400.0) is True

    def test_trade_stop_hit_short(self):
        """Trade.is_stop_hit correctly detects short SL breach."""
        trade = Trade(
            symbol="ETHUSD",
            side=TradeSide.SHORT,
            entry_price=3500.0,
            stop_loss=3550.0,
            current_sl=3550.0,
        )
        assert trade.is_stop_hit(3540.0) is False
        assert trade.is_stop_hit(3550.0) is True
        assert trade.is_stop_hit(3560.0) is True

    def test_trade_tp_hit(self):
        """Trade.is_tp_hit detects take-profit levels."""
        trade = Trade(
            symbol="BTCUSD",
            side=TradeSide.LONG,
            entry_price=66000.0,
            take_profits=[
                TakeProfit(price=66400.0, close_pct=0.35),
                TakeProfit(price=66800.0, close_pct=0.35),
            ],
        )
        assert trade.is_tp_hit(66300.0) is None
        tp = trade.is_tp_hit(66500.0)
        assert tp is not None
        assert tp.price == 66400.0

    def test_trade_mark_filled_and_close(self):
        """Trade mark_filled -> mark_closed lifecycle."""
        trade = Trade(symbol="BTCUSD", side=TradeSide.LONG)
        trade.mark_filled(fill_price=66000.0, fill_size=0.003, fee=0.05)
        assert trade.status == TradeStatus.OPEN
        assert trade.position_size_usd == 66000.0 * 0.003

        pnl = trade.mark_closed(exit_price=66200.0, fee=0.05, reason="tp1")
        assert trade.status == TradeStatus.CLOSED
        assert pnl > 0, f"Should be profitable, PnL={pnl}"
        assert trade.fees == 0.10  # 0.05 entry + 0.05 exit

    def test_trade_serialization(self):
        """Trade round-trips through to_dict/from_dict."""
        trade = Trade(
            symbol="BTCUSD",
            side=TradeSide.LONG,
            entry_price=66000.0,
            stop_loss=65500.0,
            take_profits=[TakeProfit(price=66400.0, close_pct=0.35)],
        )
        d = trade.to_dict()
        restored = Trade.from_dict(d)
        assert restored.symbol == "BTCUSD"
        assert restored.side == TradeSide.LONG
        assert restored.entry_price == 66000.0
        assert len(restored.take_profits) == 1


# ═══════════════════════════════════════════════════════════════
# TEST: MFE/MAE R-tracking during update_prices
# ═══════════════════════════════════════════════════════════════

class TestMFEMAETracking:
    def test_mfe_mae_long(self):
        """MFE and MAE in R-multiples track correctly for longs."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,
            confidence=75,
            ml_probability=0.55,
        )
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        # Price goes up 1R
        tracker.update_prices({"BTCUSD": 66500.0})
        assert ts.mfe_r >= 0.9, f"MFE should be ~1.0R after +500, got {ts.mfe_r}"

        # Price drops back below entry
        tracker.update_prices({"BTCUSD": 65800.0})
        assert ts.mae_r >= 0.3, f"MAE should be >= 0.3R after dip to 65800, got {ts.mae_r}"
        # MFE should still reflect the peak
        assert ts.mfe_r >= 0.9, f"MFE should remain at peak, got {ts.mfe_r}"


# ═══════════════════════════════════════════════════════════════
# TEST: TrackedSignal from_dict / to_dict round-trip
# ═══════════════════════════════════════════════════════════════

class TestTrackedSignalSerialization:
    def test_round_trip(self):
        """TrackedSignal survives to_dict -> from_dict."""
        sig = _make_signal()
        ts = TrackedSignal.from_signal(sig)
        d = ts.to_dict()
        restored = TrackedSignal.from_dict(d)
        assert restored.trade_id == ts.trade_id
        assert restored.symbol == ts.symbol
        assert restored.entry_price == ts.entry_price
        assert restored.initial_risk == ts.initial_risk
        assert restored.contract_size == ts.contract_size


# ═══════════════════════════════════════════════════════════════
# TEST: Hard Loss Cap (-1.2R)
# ═══════════════════════════════════════════════════════════════

class TestHardLossCap:
    def test_hard_loss_cap_triggers(self):
        """Trade exits when adverse excursion exceeds 1.2R."""
        sig = _make_signal(
            entry_price=66000.0,
            stop_loss=65500.0,  # 500 risk
            confidence=75,
            ml_probability=0.55,
        )
        ts = TrackedSignal.from_signal(sig)
        tracker = _make_tracker()
        tracker._active[ts.trade_id] = ts

        # Move the SL out of the way to test hard loss cap independently
        ts.stop_loss = 64000.0  # very wide SL so normal SL doesn't fire first

        # Price drops 1.3R below entry: 66000 - 1.3 * 500 = 65350
        events = tracker.update_prices({"BTCUSD": 65350.0})
        cap_events = [e for e in events if e.get("type") == "hard_loss_cap"]
        assert len(cap_events) >= 1, f"Expected hard_loss_cap event, got: {[e['type'] for e in events]}"
        assert ts.exit_reason == "hard_loss_cap"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=long"])
