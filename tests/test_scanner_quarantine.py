"""Tests for the 2026-09-17 scanner quarantine (structurally false entries).

Two paths were removed for having no relationship to the edge their name
claims, not for a threshold retune:

1. ``_scan_cvd_divergence`` — ``cumsum(volume * sign(close-open))`` split at
   a window midpoint is not CVD and not a swing divergence (no
   aggressor/tick data backs it). Gated to never return a live setup outside
   ``paper_learning`` mode.
2. ``_scan_liquidity_sweep``'s rolling-15-bar-high/low fallback — fired when
   no equal-high/low cluster existed, on a plain range extreme with none of
   the "trapped liquidity" premise the scanner is named for. Deleted
   outright; a cluster-less sweep now returns None.

Same synthetic-DataFrame convention as ``test_structure_fvg.py`` — no
external fixture files, and ``_compute_indicators`` is used for indicator
fidelity (the same code the live bot runs), matching ``scripts/
diagnose_scanners.py``'s established pattern for scanner replay.
"""
import numpy as np
import pandas as pd

from strategies.scalp_strategy import ScalpStrategy


def _timestamp_index(n: int) -> pd.Series:
    return (pd.Timestamp("2026-01-01") + pd.to_timedelta(np.arange(n) * 5, unit="m")).astype(np.int64) // 10**6


def _cvd_bullish_divergence_df() -> pd.DataFrame:
    """60 flat warmup bars (indicator warmup) + 15 bars that satisfy the
    scanner's own bullish-divergence condition: net closing price still
    declines bar to bar (price_down), while later bars are strong green
    candles on rising volume (cvd trending up) -- hidden buying under a
    still-declining close. Verified against the pre-quarantine code to
    return a LONG setup when the learning-mode gate is bypassed.
    """
    rng = np.random.default_rng(0)
    n_warmup = 60
    rows = []
    prev_close = 100.0
    for _ in range(n_warmup):
        c = prev_close + rng.normal(0, 0.05)
        rows.append({"open": prev_close, "close": c,
                     "high": max(prev_close, c) + 0.05,
                     "low": min(prev_close, c) - 0.05, "volume": 100.0})
        prev_close = c

    level = prev_close
    for i in range(15):
        level -= 0.15  # steady net decline bar to bar
        prev_c = rows[-1]["close"]
        if i < 7:
            o, vol = prev_c + 0.05, 80.0       # red candle, modest volume
        else:
            o = level - 0.35                    # green candle (big body)
            vol = 80.0 + (i - 6) * 60.0          # ramping volume
        rows.append({"open": o, "close": level,
                     "high": max(o, level) + 0.05,
                     "low": min(o, level) - 0.05, "volume": vol})

    df = pd.DataFrame(rows)
    df["timestamp"] = _timestamp_index(len(df))
    return df


def _sweep_no_cluster_df() -> pd.DataFrame:
    """40 warmup bars + a 15-bar monotonic staircase (every high and low
    strictly increasing, so no two lows/highs ever land within the
    scanner's 0.25xATR tolerance -- no equal-high/low cluster is possible)
    + a trigger bar whose wick sweeps below the rolling 15-bar low and
    recloses above it with a >=55%-body reclaim candle. Verified against
    the pre-quarantine code to return a LONG setup via the now-deleted
    rolling-high/low fallback.
    """
    rng = np.random.default_rng(1)
    rows = []
    prev_close = 100.0
    for i in range(40):
        c = prev_close + 0.30 + rng.uniform(-0.02, 0.02)
        rows.append({"open": prev_close, "close": c,
                     "high": max(prev_close, c) + 0.10 + i * 0.001,
                     "low": min(prev_close, c) - 0.05, "volume": 100.0})
        prev_close = c

    for i in range(15):
        c = prev_close + 0.30 + (i * 0.002)
        rows.append({"open": prev_close, "close": c,
                     "high": max(prev_close, c) + 0.10 + i * 0.003,
                     "low": min(prev_close, c) - 0.05 + i * 0.01, "volume": 100.0})
        prev_close = c

    rolling_low = min(r["low"] for r in rows[-15:])
    sweep_low = rolling_low - 0.60
    entry_open = sweep_low + 0.30   # lower wick 0.30
    entry_close = entry_open + 0.60  # body 0.60 -> body_ratio 0.67 >= 0.55 gate
    rows.append({"open": entry_open, "close": entry_close,
                 "high": entry_close, "low": sweep_low, "volume": 150.0})

    df = pd.DataFrame(rows)
    df["timestamp"] = _timestamp_index(len(df))
    return df


class TestCvdDivergenceQuarantine:
    def test_never_returns_live_setup(self):
        strat = ScalpStrategy({})
        df = _cvd_bullish_divergence_df()
        enriched = strat._compute_indicators(df.copy())

        strat._is_learning = False
        assert strat._scan_cvd_divergence("TEST", enriched, 0, 0) is None

    def test_still_fires_in_learning_mode(self):
        """Sanity check: the fixture genuinely satisfies the divergence
        condition, and only the learning-mode gate blocks it live -- not
        some unrelated data problem making the test vacuously pass."""
        strat = ScalpStrategy({})
        df = _cvd_bullish_divergence_df()
        enriched = strat._compute_indicators(df.copy())

        strat._is_learning = True
        result = strat._scan_cvd_divergence("TEST", enriched, 0, 0)
        assert result is not None
        assert result.name == "cvd_divergence"


class TestLiquiditySweepNoFallback:
    def test_cluster_less_sweep_returns_none(self):
        strat = ScalpStrategy({})
        df = _sweep_no_cluster_df()
        enriched = strat._compute_indicators(df.copy())

        assert strat._scan_liquidity_sweep("TEST", enriched, 0, 0) is None
