"""Tests for data/structure.py::find_fair_value_gaps (2026-09-17).

Synthetic-data tests for pure structure-detection math, same pattern used
this session for the liquidity-zone dedup fix — no live scanner/strategy
test convention exists in this codebase (confirmed before writing this),
so this stays narrowly scoped to the generator function itself.
"""
import numpy as np
import pandas as pd

from data.structure import find_fair_value_gaps


def _flat_df(n: int, price: float = 100.0, atr: float = 1.0) -> pd.DataFrame:
    closes = np.full(n, price)
    return pd.DataFrame({
        "open": closes.copy(),
        "close": closes.copy(),
        "high": closes + 0.05,
        "low": closes - 0.05,
        "atr": [atr] * n,
    })


def _bullish_gap_df(extra_rows: "list[dict] | None" = None) -> pd.DataFrame:
    """A short df ending right after an unmitigated bullish FVG (bars 0-2),
    padded with a few leading flat bars so len(df)>=10 (the function's
    minimum). Deliberately stays elevated / doesn't snap back to the
    leading baseline -- a trailing return-to-baseline would itself read as
    a second, unintended bearish gap and contaminate the test, exactly the
    kind of synthetic-data pitfall already hit once earlier this session.
    """
    pad = _flat_df(7, price=100.0)
    gap_rows = pd.DataFrame([
        {"open": 100.0, "close": 100.1, "high": 100.2, "low": 99.9, "atr": 1.0},   # i-2
        {"open": 100.2, "close": 101.0, "high": 101.2, "low": 100.15, "atr": 1.0}, # i-1 (displacement)
        {"open": 101.0, "close": 101.2, "high": 101.4, "low": 100.5, "atr": 1.0},  # i
    ])
    df = pd.concat([pad, gap_rows], ignore_index=True)
    if extra_rows:
        df = pd.concat([df, pd.DataFrame(extra_rows)], ignore_index=True)
    return df


class TestFindFairValueGaps:
    def test_detects_unmitigated_bullish_gap(self):
        df = _bullish_gap_df()
        levels = find_fair_value_gaps(df, lookback=50)
        assert len(levels) == 1
        lvl = levels[0]
        assert lvl.level_type == "fvg"
        assert lvl.side == "support"
        assert lvl.zone_low == 100.2
        assert lvl.zone_high == 100.5
        assert lvl.extra["fvg_type"] == "bullish"

    def test_mitigated_gap_is_excluded(self):
        # One more bar whose low dips back into the gap zone [100.2, 100.5].
        df = _bullish_gap_df(extra_rows=[
            {"open": 101.4, "close": 100.3, "high": 101.4, "low": 100.3, "atr": 1.0},
        ])
        levels = find_fair_value_gaps(df, lookback=50)
        assert levels == []

    def test_gap_below_min_size_is_ignored(self):
        # A gap of only 0.04 -- below the 0.15x-ATR (=0.15) noise floor.
        # Stays close to the leading flat baseline throughout, so there's
        # no secondary reversal gap to worry about either.
        pad = _flat_df(7, price=100.0)
        small_gap_rows = pd.DataFrame([
            {"open": 100.0, "close": 100.1, "high": 100.20, "low": 99.9, "atr": 1.0},
            {"open": 100.2, "close": 100.3, "high": 100.4, "low": 100.15, "atr": 1.0},
            {"open": 100.3, "close": 100.35, "high": 100.4, "low": 100.24, "atr": 1.0},
        ])
        df = pd.concat([pad, small_gap_rows], ignore_index=True)
        levels = find_fair_value_gaps(df, lookback=50)
        assert levels == []

    def test_bearish_gap_detected(self):
        # Leading baseline sits ABOVE the post-gap level and never returns
        # to it within this df, so there's no accidental bullish gap on
        # the way down into the bearish one.
        pad = _flat_df(7, price=101.0)
        gap_rows = pd.DataFrame([
            {"open": 101.0, "close": 100.9, "high": 101.1, "low": 100.8, "atr": 1.0},  # i-2
            {"open": 100.8, "close": 100.0, "high": 100.85, "low": 99.8, "atr": 1.0},  # i-1
            {"open": 100.0, "close": 99.8, "high": 100.5, "low": 99.7, "atr": 1.0},    # i
        ])
        df = pd.concat([pad, gap_rows], ignore_index=True)
        levels = find_fair_value_gaps(df, lookback=50)
        assert len(levels) == 1
        lvl = levels[0]
        assert lvl.side == "resistance"
        assert lvl.extra["fvg_type"] == "bearish"
        assert lvl.zone_low == 100.5
        assert lvl.zone_high == 100.8

    def test_no_gap_returns_empty(self):
        df = _flat_df(15)
        assert find_fair_value_gaps(df, lookback=50) == []

    def test_short_df_returns_empty(self):
        df = _flat_df(5)
        assert find_fair_value_gaps(df, lookback=50) == []
