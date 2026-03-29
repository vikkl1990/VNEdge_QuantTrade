"""Unit tests for execution/trade.py — Trade dataclass, TradeSide, TradeStatus, TakeProfit.

Tests creation, lifecycle transitions, PnL calculations, serialization.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime

from execution.trade import Trade, TradeSide, TradeStatus, TakeProfit


# ===================================================================
# TradeSide enum
# ===================================================================

class TestTradeSide:
    def test_long_value(self):
        assert TradeSide.LONG.value == "long"

    def test_short_value(self):
        assert TradeSide.SHORT.value == "short"

    def test_from_string(self):
        assert TradeSide("long") == TradeSide.LONG
        assert TradeSide("short") == TradeSide.SHORT

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            TradeSide("sideways")


# ===================================================================
# TradeStatus enum
# ===================================================================

class TestTradeStatus:
    def test_all_values(self):
        expected = {"pending", "open", "partial", "closed", "cancelled", "failed"}
        actual = {s.value for s in TradeStatus}
        assert actual == expected

    def test_from_string(self):
        assert TradeStatus("pending") == TradeStatus.PENDING
        assert TradeStatus("open") == TradeStatus.OPEN
        assert TradeStatus("closed") == TradeStatus.CLOSED


# ===================================================================
# TakeProfit dataclass
# ===================================================================

class TestTakeProfit:
    def test_creation(self):
        tp = TakeProfit(price=72000.0, close_pct=0.35)
        assert tp.price == 72000.0
        assert tp.close_pct == 0.35
        assert tp.hit is False
        assert tp.hit_time is None
        assert tp.order_id is None

    def test_to_dict(self):
        tp = TakeProfit(price=72000.0, close_pct=0.35, hit=True)
        d = tp.to_dict()
        assert d["price"] == 72000.0
        assert d["close_pct"] == 0.35
        assert d["hit"] is True
        assert d["hit_time"] is None

    def test_from_dict(self):
        d = {"price": 73000.0, "close_pct": 0.30, "hit": False}
        tp = TakeProfit.from_dict(d)
        assert tp.price == 73000.0
        assert tp.close_pct == 0.30
        assert tp.hit is False

    def test_roundtrip(self):
        tp = TakeProfit(price=75000.0, close_pct=0.5, hit=True,
                        hit_time=datetime(2026, 1, 1, 12, 0, 0))
        d = tp.to_dict()
        tp2 = TakeProfit.from_dict(d)
        assert tp2.price == tp.price
        assert tp2.close_pct == tp.close_pct
        assert tp2.hit == tp.hit
        assert tp2.hit_time == tp.hit_time

    def test_tp_ordering(self):
        """Multiple TPs should be ordered by price for longs."""
        tps = [
            TakeProfit(price=71000, close_pct=0.35),
            TakeProfit(price=72000, close_pct=0.35),
            TakeProfit(price=74000, close_pct=0.30),
        ]
        prices = [tp.price for tp in tps]
        assert prices == sorted(prices)  # ascending for longs


# ===================================================================
# Trade dataclass
# ===================================================================

class TestTradeCreation:
    def test_default_trade(self):
        t = Trade()
        assert t.status == TradeStatus.PENDING
        assert t.side == TradeSide.LONG
        assert t.entry_price == 0.0
        assert t.pnl == 0.0
        assert t.fees == 0.0

    def test_trade_with_fields(self):
        t = Trade(
            symbol="BTC/USDT",
            side=TradeSide.SHORT,
            entry_price=70000.0,
            stop_loss=70500.0,
            position_size=0.01,
            leverage=10,
        )
        assert t.symbol == "BTC/USDT"
        assert t.side == TradeSide.SHORT
        assert t.entry_price == 70000.0
        assert t.stop_loss == 70500.0
        assert t.leverage == 10

    def test_post_init_current_sl(self):
        """current_sl should default to stop_loss if not set."""
        t = Trade(stop_loss=69000.0)
        assert t.current_sl == 69000.0

    def test_post_init_remaining_size(self):
        """remaining_size should default to position_size if not set."""
        t = Trade(position_size=0.05)
        assert t.remaining_size == 0.05

    def test_trade_id_generated(self):
        """trade_id should be auto-generated."""
        t1 = Trade()
        t2 = Trade()
        assert len(t1.trade_id) == 12
        assert t1.trade_id != t2.trade_id


class TestTradeLifecycle:
    def test_mark_filled(self):
        t = Trade(symbol="ETH/USDT", side=TradeSide.LONG)
        t.mark_filled(fill_price=3500.0, fill_size=1.0, fee=2.0)
        assert t.status == TradeStatus.OPEN
        assert t.entry_price == 3500.0
        assert t.position_size == 1.0
        assert t.position_size_usd == 3500.0
        assert t.fees == 2.0
        assert t.remaining_size == 1.0

    def test_mark_closed_long_profit(self):
        t = Trade(symbol="BTC/USDT", side=TradeSide.LONG)
        t.mark_filled(fill_price=70000.0, fill_size=0.01)
        pnl = t.mark_closed(exit_price=71000.0, fee=1.0, reason="tp1")
        assert t.status == TradeStatus.CLOSED
        # PnL = (71000 - 70000) * 0.01 - 1.0 = 10 - 1 = 9
        assert abs(pnl - 9.0) < 0.01
        assert t.exit_reason == "tp1"
        assert t.remaining_size == 0.0

    def test_mark_closed_short_profit(self):
        t = Trade(symbol="BTC/USDT", side=TradeSide.SHORT)
        t.mark_filled(fill_price=70000.0, fill_size=0.01)
        pnl = t.mark_closed(exit_price=69000.0, fee=1.0)
        # PnL = (70000 - 69000) * 0.01 - 1.0 = 10 - 1 = 9
        assert abs(pnl - 9.0) < 0.01
        assert t.status == TradeStatus.CLOSED

    def test_mark_closed_long_loss(self):
        t = Trade(symbol="BTC/USDT", side=TradeSide.LONG)
        t.mark_filled(fill_price=70000.0, fill_size=0.01)
        pnl = t.mark_closed(exit_price=69000.0)
        # PnL = (69000 - 70000) * 0.01 = -10
        assert pnl < 0
        assert t.is_winner is False

    def test_partial_exit(self):
        t = Trade(symbol="ETH/USDT", side=TradeSide.LONG)
        t.mark_filled(fill_price=3000.0, fill_size=1.0)
        pnl1 = t.mark_partial_exit(exit_price=3100.0, exit_size=0.5, reason="tp1")
        assert t.status == TradeStatus.PARTIAL
        assert t.remaining_size == 0.5
        # PnL for first chunk: (3100-3000)*0.5 = 50
        assert abs(pnl1 - 50.0) < 0.01
        # Close remaining
        pnl2 = t.mark_partial_exit(exit_price=3200.0, exit_size=0.5, reason="tp2")
        assert t.status == TradeStatus.CLOSED
        assert t.remaining_size == 0.0

    def test_mark_cancelled(self):
        t = Trade()
        t.mark_cancelled("no fill")
        assert t.status == TradeStatus.CANCELLED
        assert t.exit_reason == "no fill"

    def test_mark_failed(self):
        t = Trade()
        t.mark_failed("api_error")
        assert t.status == TradeStatus.FAILED
        assert t.exit_reason == "api_error"


class TestTradeExcursion:
    def test_update_price_long(self):
        t = Trade(symbol="BTC/USDT", side=TradeSide.LONG)
        t.mark_filled(fill_price=70000.0, fill_size=0.01)
        t.update_price(71000.0)
        assert t.highest_price_seen == 71000.0
        assert t.unrealized_pnl > 0
        assert t.max_favorable_excursion > 0
        t.update_price(69500.0)
        assert t.lowest_price_seen == 69500.0
        assert t.unrealized_pnl < 0
        assert t.max_adverse_excursion < 0

    def test_update_price_not_open(self):
        """update_price should be a no-op for non-open trades."""
        t = Trade()
        t.update_price(50000.0)
        assert t.highest_price_seen == 0.0


class TestTradeStopTP:
    def test_stop_hit_long(self):
        t = Trade(side=TradeSide.LONG, stop_loss=69000.0)
        t.status = TradeStatus.OPEN
        assert t.is_stop_hit(69000.0) is True
        assert t.is_stop_hit(68999.0) is True
        assert t.is_stop_hit(69001.0) is False

    def test_stop_hit_short(self):
        t = Trade(side=TradeSide.SHORT, stop_loss=71000.0)
        t.status = TradeStatus.OPEN
        assert t.is_stop_hit(71000.0) is True
        assert t.is_stop_hit(71001.0) is True
        assert t.is_stop_hit(70999.0) is False

    def test_next_tp(self):
        t = Trade()
        t.take_profits = [
            TakeProfit(price=71000, close_pct=0.35, hit=True),
            TakeProfit(price=72000, close_pct=0.35, hit=False),
            TakeProfit(price=74000, close_pct=0.30, hit=False),
        ]
        nxt = t.next_tp()
        assert nxt.price == 72000

    def test_next_tp_all_hit(self):
        t = Trade()
        t.take_profits = [
            TakeProfit(price=71000, close_pct=0.35, hit=True),
            TakeProfit(price=72000, close_pct=0.35, hit=True),
        ]
        assert t.next_tp() is None

    def test_is_tp_hit_long(self):
        t = Trade(side=TradeSide.LONG)
        t.take_profits = [TakeProfit(price=71000, close_pct=0.35)]
        assert t.is_tp_hit(71000) is not None
        assert t.is_tp_hit(72000) is not None
        assert t.is_tp_hit(70999) is None

    def test_is_tp_hit_short(self):
        t = Trade(side=TradeSide.SHORT)
        t.take_profits = [TakeProfit(price=69000, close_pct=0.35)]
        assert t.is_tp_hit(69000) is not None
        assert t.is_tp_hit(68000) is not None
        assert t.is_tp_hit(69001) is None


class TestTradeSerialization:
    def test_to_dict_fields(self):
        t = Trade(symbol="SOL/USDT", side=TradeSide.LONG, entry_price=150.0)
        d = t.to_dict()
        assert d["symbol"] == "SOL/USDT"
        assert d["side"] == "long"
        assert d["entry_price"] == 150.0
        assert d["status"] == "pending"

    def test_roundtrip(self):
        t = Trade(
            symbol="ETH/USDT",
            side=TradeSide.SHORT,
            entry_price=3500.0,
            stop_loss=3600.0,
            leverage=10,
            grade="A+",
            confidence=92.0,
        )
        t.take_profits = [
            TakeProfit(price=3400, close_pct=0.35),
            TakeProfit(price=3300, close_pct=0.35),
        ]
        d = t.to_dict()
        t2 = Trade.from_dict(d)
        assert t2.symbol == t.symbol
        assert t2.side == t.side
        assert t2.entry_price == t.entry_price
        assert t2.stop_loss == t.stop_loss
        assert t2.leverage == t.leverage
        assert len(t2.take_profits) == 2
        assert t2.take_profits[0].price == 3400


class TestTradeProperties:
    def test_is_winner(self):
        t = Trade(pnl=10.0)
        assert t.is_winner is True
        t2 = Trade(pnl=-5.0)
        assert t2.is_winner is False
        t3 = Trade(pnl=0.0)
        assert t3.is_winner is False

    def test_is_open(self):
        t = Trade(status=TradeStatus.OPEN)
        assert t.is_open is True
        t2 = Trade(status=TradeStatus.PARTIAL)
        assert t2.is_open is True
        t3 = Trade(status=TradeStatus.CLOSED)
        assert t3.is_open is False

    def test_duration_no_entry(self):
        t = Trade()
        assert t.duration_seconds is None

    def test_duration_with_entry_and_exit(self):
        t = Trade()
        t.entry_time = datetime(2026, 1, 1, 12, 0, 0)
        t.exit_time = datetime(2026, 1, 1, 12, 5, 0)
        assert t.duration_seconds == 300.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
