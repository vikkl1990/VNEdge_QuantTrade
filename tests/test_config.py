"""Unit tests for config/constants.py — enums and constants.

Tests BotMode parsing, TradeGrade ordering, MarketRegime properties,
SignalType helpers, confidence_to_grade mapping.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from config.constants import (
    BotMode,
    TradeGrade,
    MarketRegime,
    SignalType,
    OrderSide,
    TradeStatus,
    confidence_to_grade,
    GRADE_THRESHOLDS,
)


# ===================================================================
# BotMode
# ===================================================================

class TestBotMode:
    def test_all_values(self):
        expected = {"signal_only", "paper", "live", "backtest", "forward_test"}
        actual = {m.value for m in BotMode}
        assert actual == expected

    def test_from_str_valid(self):
        assert BotMode.from_str("paper") == BotMode.PAPER
        assert BotMode.from_str("live") == BotMode.LIVE
        assert BotMode.from_str("signal_only") == BotMode.SIGNAL_ONLY

    def test_from_str_case_insensitive(self):
        assert BotMode.from_str("PAPER") == BotMode.PAPER
        assert BotMode.from_str("  Live  ") == BotMode.LIVE

    def test_from_str_invalid(self):
        with pytest.raises(ValueError, match="Invalid BotMode"):
            BotMode.from_str("yolo")

    def test_is_str_enum(self):
        """BotMode values should be usable as strings."""
        assert BotMode.PAPER.value == "paper"
        assert f"mode={BotMode.LIVE.value}" == "mode=live"


# ===================================================================
# TradeGrade
# ===================================================================

class TestTradeGrade:
    def test_rank_ordering(self):
        """A+ should rank higher (lower number) than A, B, C, REJECT."""
        assert TradeGrade.A_PLUS.rank < TradeGrade.A.rank
        assert TradeGrade.A.rank < TradeGrade.B.rank
        assert TradeGrade.B.rank < TradeGrade.C.rank
        assert TradeGrade.C.rank < TradeGrade.REJECT.rank

    def test_meets_minimum(self):
        assert TradeGrade.A_PLUS.meets_minimum(TradeGrade.A) is True
        assert TradeGrade.A.meets_minimum(TradeGrade.A) is True
        assert TradeGrade.B.meets_minimum(TradeGrade.A) is False
        assert TradeGrade.C.meets_minimum(TradeGrade.B) is False
        assert TradeGrade.REJECT.meets_minimum(TradeGrade.C) is False

    def test_from_str_valid(self):
        assert TradeGrade.from_str("A+") == TradeGrade.A_PLUS
        assert TradeGrade.from_str("a+") == TradeGrade.A_PLUS
        assert TradeGrade.from_str("B") == TradeGrade.B
        assert TradeGrade.from_str("REJECT") == TradeGrade.REJECT

    def test_from_str_invalid(self):
        with pytest.raises(ValueError, match="Invalid TradeGrade"):
            TradeGrade.from_str("D")

    def test_a_plus_better_than_all(self):
        """A+ should meet minimum for every grade."""
        for grade in TradeGrade:
            if grade == TradeGrade._RANK:
                continue
            assert TradeGrade.A_PLUS.meets_minimum(grade) is True

    def test_reject_meets_nothing_above(self):
        """REJECT should not meet minimum A+, A, B, or C."""
        for grade in [TradeGrade.A_PLUS, TradeGrade.A, TradeGrade.B, TradeGrade.C]:
            assert TradeGrade.REJECT.meets_minimum(grade) is False


# ===================================================================
# MarketRegime
# ===================================================================

class TestMarketRegime:
    def test_all_values(self):
        expected = {
            "trending_up", "trending_down", "sideways", "breakout",
            "mean_reversion", "high_volatility", "low_liquidity",
        }
        actual = {r.value for r in MarketRegime}
        assert actual == expected

    def test_is_trending(self):
        assert MarketRegime.TRENDING_UP.is_trending is True
        assert MarketRegime.TRENDING_DOWN.is_trending is True
        assert MarketRegime.SIDEWAYS.is_trending is False
        assert MarketRegime.BREAKOUT.is_trending is False

    def test_is_risky(self):
        assert MarketRegime.HIGH_VOLATILITY.is_risky is True
        assert MarketRegime.LOW_LIQUIDITY.is_risky is True
        assert MarketRegime.TRENDING_UP.is_risky is False
        assert MarketRegime.SIDEWAYS.is_risky is False


# ===================================================================
# SignalType
# ===================================================================

class TestSignalType:
    def test_is_entry(self):
        assert SignalType.BUY.is_entry is True
        assert SignalType.SELL.is_entry is True
        assert SignalType.PRE_BUY.is_entry is False
        assert SignalType.TP1.is_entry is False

    def test_is_pre_signal(self):
        assert SignalType.PRE_BUY.is_pre_signal is True
        assert SignalType.PRE_SELL.is_pre_signal is True
        assert SignalType.BUY.is_pre_signal is False

    def test_is_exit(self):
        assert SignalType.TP1.is_exit is True
        assert SignalType.TP2.is_exit is True
        assert SignalType.TP3.is_exit is True
        assert SignalType.EXIT.is_exit is True
        assert SignalType.FORCE_EXIT.is_exit is True
        assert SignalType.BUY.is_exit is False
        assert SignalType.REVERSE.is_exit is False


# ===================================================================
# OrderSide
# ===================================================================

class TestOrderSide:
    def test_opposite(self):
        assert OrderSide.LONG.opposite == OrderSide.SHORT
        assert OrderSide.SHORT.opposite == OrderSide.LONG


# ===================================================================
# confidence_to_grade
# ===================================================================

class TestConfidenceToGrade:
    def test_thresholds(self):
        assert confidence_to_grade(95) == TradeGrade.A_PLUS
        assert confidence_to_grade(90) == TradeGrade.A_PLUS
        assert confidence_to_grade(85) == TradeGrade.A
        assert confidence_to_grade(80) == TradeGrade.A
        assert confidence_to_grade(70) == TradeGrade.B
        assert confidence_to_grade(65) == TradeGrade.B
        assert confidence_to_grade(55) == TradeGrade.C
        assert confidence_to_grade(50) == TradeGrade.C
        assert confidence_to_grade(40) == TradeGrade.REJECT
        assert confidence_to_grade(0) == TradeGrade.REJECT

    def test_boundary_values(self):
        """Test exact boundary values."""
        assert confidence_to_grade(90) == TradeGrade.A_PLUS
        assert confidence_to_grade(89) == TradeGrade.A
        assert confidence_to_grade(80) == TradeGrade.A
        assert confidence_to_grade(79) == TradeGrade.B
        assert confidence_to_grade(65) == TradeGrade.B
        assert confidence_to_grade(64) == TradeGrade.C
        assert confidence_to_grade(50) == TradeGrade.C
        assert confidence_to_grade(49) == TradeGrade.REJECT

    def test_grade_thresholds_dict(self):
        """Thresholds should be descending."""
        values = list(GRADE_THRESHOLDS.values())
        assert values == sorted(values, reverse=True)


# ===================================================================
# TradeStatus from constants (not trade.py)
# ===================================================================

class TestTradeStatusConstants:
    def test_all_values(self):
        expected = {"pending", "open", "closed", "cancelled"}
        actual = {s.value for s in TradeStatus}
        assert actual == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
