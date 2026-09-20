"""Tests for scripts/family_lab.py::simulate_family_trade — the family risk
contract (stop first, target, invalidation on a close through EMA21,
expiry at 6 bars with MFE < 0.3R, chandelier trail) on hand-built bars."""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "family_lab", Path(__file__).resolve().parent.parent / "scripts" / "family_lab.py")
fl = importlib.util.module_from_spec(_SPEC)
sys.modules["family_lab"] = fl
_SPEC.loader.exec_module(fl)


def _run(highs, lows, closes, ema21, exit_model, side="long", entry=100.0, stop=99.0):
    a = lambda x: np.array(x, dtype=float)
    return fl.simulate_family_trade(a(highs), a(lows), a(closes), a(ema21), 0, side, entry, stop, exit_model)


class TestFamilyContract:
    def test_target_1r(self):
        xi, r, reason, mfe, mae = _run([100.5, 101.2], [99.8, 100.3], [100.4, 101.0], [98, 98], "r1")
        assert (xi, r, reason) == (1, 1.0, "target") and mfe == pytest.approx(1.2)

    def test_stop_before_target_same_bar(self):
        xi, r, reason, _, _ = _run([101.6], [98.9], [101.0], [98], "r15")
        assert (r, reason) == (-1.0, "stop")

    def test_invalidation_close_through_ema21(self):
        # never hits stop (99) or target, but bar 1 closes below EMA21 (99.6)
        xi, r, reason, _, _ = _run([100.4, 100.2], [99.7, 99.3], [100.2, 99.5], [99.0, 99.6], "r15")
        assert reason == "invalidation" and xi == 1 and r == pytest.approx(-0.5)

    def test_expiry_after_6_bars_without_0_3r(self):
        h = [100.2] * 8; l = [99.9] * 8; c = [100.1] * 8; e = [99.0] * 8
        xi, r, reason, mfe, _ = _run(h, l, c, e, "r15")
        assert reason == "expiry" and xi == 5 and mfe == pytest.approx(0.2)

    def test_trail_ratchets_after_arming(self):
        # peak 1.5R on bar 1 -> trail stop at +0.5R; bar 2 dips to +0.4R -> trail_stop at +0.5R
        xi, r, reason, _, _ = _run([100.3, 101.5, 101.2], [99.9, 100.6, 100.4], [100.2, 101.4, 101.0], [98] * 3, "trail")
        assert reason == "trail_stop" and xi == 2 and r == pytest.approx(0.5)

    def test_short_mirror(self):
        xi, r, reason, _, _ = _run([100.2, 99.7], [99.5, 98.8], [99.6, 99.0], [102, 102], "r1", side="short", entry=100.0, stop=101.0)
        assert (r, reason) == (1.0, "target")
