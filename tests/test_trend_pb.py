"""Tests for strategies/families/trend_pb.py against its pre-registration
(docs/research/TREND_PB_PREREG_20260917.md). Synthetic bars, ATR fixed at
1.0 so every threshold reads directly in price units."""
import numpy as np
import pandas as pd
import pytest

from strategies.families.trend_pb import detect_trend_pb, FAMILY_ID


def _mk(bars, atr=1.0, rel_vol=1.2):
    """bars: list of (open, high, low, close). EMAs computed from closes so
    that in a rising series EMA21 lags below price."""
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"]).astype(float)
    df["atr"] = atr
    df["ema_8"] = df["close"].ewm(span=8, adjust=False).mean()
    df["ema_21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["rel_vol"] = rel_vol
    df["supertrend_dir"] = 1
    return df


def _long_setup(impulse_body=1.0, pullback=(-0.3, -0.3, -0.2), trigger_over=0.2, warm=40):
    """Rising warmup, an impulse bar, a shallow 3-bar pullback that holds
    above EMA21, then a trigger close above the pullback swing high."""
    bars = []
    p = 100.0
    for _ in range(warm):
        bars.append((p, p + 0.35, p - 0.15, p + 0.2))
        p += 0.2
    imp_open = p
    imp_close = p + impulse_body
    bars.append((imp_open, imp_close + 0.1, imp_open - 0.1, imp_close))     # impulse
    level = imp_close
    highs = []
    for d in pullback:
        o = level
        cl = level + d
        hi = max(o, cl) + 0.1
        highs.append(hi)
        bars.append((o, hi, min(o, cl) - 0.05, cl))                        # pullback bars
        level = cl
    swing = max(highs)
    trig_close = swing + trigger_over
    bars.append((level, trig_close + 0.05, level - 0.05, trig_close))     # trigger
    return bars, swing, min(min(o, o + d) - 0.05 for o, d in zip([imp_close] + [imp_close + sum(pullback[:i+1]) for i in range(len(pullback)-1)], pullback))


class TestTrendPbLong:
    def test_detects_body_impulse_pullback_trigger(self):
        bars, swing, _ = _long_setup()
        s = detect_trend_pb(_mk(bars))
        assert s is not None and s.family_id == FAMILY_ID and s.side == "long"
        assert s.impulse_type == "body" and s.pullback_bars == 3
        assert s.swing_level == pytest.approx(swing)
        df = _mk(bars)
        pullback_low = df["low"].iloc[-4:-1].min()
        assert s.stop == pytest.approx(pullback_low - 0.1)     # extreme - 0.1 ATR
        assert s.invalidation == "close_through_ema21" and s.expiry_bars == 6

    def test_no_trigger_no_setup(self):
        bars, swing, _ = _long_setup(trigger_over=-0.3)          # closes below the swing
        assert detect_trend_pb(_mk(bars)) is None

    def test_weak_impulse_rejected(self):
        bars, _, _ = _long_setup(impulse_body=0.5)               # < 0.7 ATR, no breakout either
        assert detect_trend_pb(_mk(bars)) is None

    def test_deep_pullback_rejected(self):
        bars, _, _ = _long_setup(pullback=(-0.6, -0.5, -0.4))    # depth > 1.2 ATR
        assert detect_trend_pb(_mk(bars)) is None

    def test_volume_veto(self):
        bars, _, _ = _long_setup()
        assert detect_trend_pb(_mk(bars, rel_vol=0.8)) is None

    def test_stretched_veto(self):
        bars, _, _ = _long_setup()
        df = _mk(bars)
        df.loc[df.index[-1], "ema_8"] = df["close"].iloc[-1] - 2.5   # close 2.5 ATR above EMA8
        assert detect_trend_pb(df) is None


class TestTrendPbShortMirror:
    def test_short_is_exact_mirror(self):
        bars, swing, _ = _long_setup()
        df = _mk(bars)
        m = df.copy()
        m["open"], m["close"] = -df["open"], -df["close"]
        m["high"], m["low"] = -df["low"], -df["high"]
        for col in ("ema_8", "ema_21", "ema_50"):
            m[col] = -df[col]
        m["supertrend_dir"] = -1
        s_long = detect_trend_pb(df)
        s_short = detect_trend_pb(m)
        assert s_short is not None and s_short.side == "short"
        assert s_short.stop == pytest.approx(-s_long.stop)
        assert s_short.swing_level == pytest.approx(-s_long.swing_level)
        assert s_short.pullback_bars == s_long.pullback_bars
