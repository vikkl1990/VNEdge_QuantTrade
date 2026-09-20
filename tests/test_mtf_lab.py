"""1m execution simulators used by scripts/mtf_lab.py (pre-registered MTF test)."""
import importlib.util
import sys

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(scope="module")
def m():
    spec = importlib.util.spec_from_file_location("mtf_lab", "scripts/mtf_lab.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mtf_lab"] = mod
    spec.loader.exec_module(mod)
    return mod


def _path(closes, wick=0.0):
    c = np.asarray(closes, float)
    return c + wick, c - wick, c


def test_to_ms_is_resolution_independent(m):
    s = pd.Series(pd.to_datetime(["2026-03-02 14:45:00"], utc=True))
    assert m.to_ms(s)[0] == 1772462700000
    assert m.to_ms(s.astype("datetime64[us, UTC]"))[0] == 1772462700000


def test_exit_stop_first_on_touch_and_close_through(m):
    H, L, C = _path([100, 100, 99.5, 99.9, 101], wick=0.4)          # bar 2 wick touches 99.1? no: low 99.1 vs stop 99.2
    xi, xp, why = m.exit_1m(H, L, C, 0, "long", 100.0, 99.2, 5)
    assert why == "stop" and xp == 99.2 and xi == 2
    xi, xp, why = m.exit_1m(H, L, C, 0, "long", 100.0, 99.2, 5, close_through=True)
    assert why != "stop"                                            # no close at or below 99.2


def test_exit_trails_one_r_behind_peak_after_one_r(m):
    # long, entry 100, stop 99 (R=1); peak at 103 -> trail to 102; next bar low 101.9 -> trail_stop at 102
    closes = [100.5, 101.5, 103, 102.5, 101.9, 100]
    H = np.array(closes) + 0.0; L = np.array(closes) - 0.0; C = np.array(closes)
    xi, xp, why = m.exit_1m(H, L, C, 0, "long", 100.0, 99.0, 60)
    assert why == "trail_stop" and xp == pytest.approx(102.0) and xi == 4


def test_exit_kills_by_minutes_of_the_signal_tf(m):
    C = np.full(200, 100.0); H = C + 0.1; L = C - 0.1                # goes nowhere
    xi, xp, why = m.exit_1m(H, L, C, 0, "long", 100.0, 99.0, 5)      # 5m tf: kill at 4*5 = 20 min
    assert why == "kill_20m" and xi == 19
    xi, xp, why = m.exit_1m(H, L, C, 0, "long", 100.0, 99.0, 15)     # 15m tf: kill at 60 min
    assert why == "kill_60m" and xi == 59


def test_fill_e1_limit_inside_zone_or_skip(m):
    H, L, C = _path([100, 99.9, 99.6, 100.2], wick=0.05)
    fi, fp = m.fill_e1(H, L, 0, "long", 100.0, 2.0, 30)              # limit 99.5; low at bar 2 = 99.55 -> not filled
    assert fi is None
    fi, fp = m.fill_e1(H, L, 0, "long", 100.0, 1.6, 30)              # limit 99.6; bar 2 low 99.55 <= 99.6
    assert fi == 2 and fp == pytest.approx(99.6)
    H, L, C = _path([100, 100.1, 100.6], wick=0.05)                   # short mirror
    fi, fp = m.fill_e2(H, L, C, 0, "short", 100.0, 1.0, 30)
    assert fi is None                                                # pulled back (high 100.65 >= 100.2) but never resumed inside window


def test_fill_e2_pullback_then_resume(m):
    # long: pullback below 100 - 0.2*ATR(=99.8) at bar 1, then a close above the max high of the previous 3 bars
    closes = [100, 99.7, 99.75, 99.8, 99.85, 100.3]
    H = np.array(closes) + 0.05; L = np.array(closes) - 0.05; C = np.array(closes)
    fi, fp = m.fill_e2(H, L, C, 0, "long", 100.0, 1.0, 30)
    assert fi == 5 and fp == pytest.approx(100.3)                    # 100.3 > max(H[2:5]) = 99.9
