"""Tests for scripts/scanner_lab.py — the pure pieces of the per-pair
scanner x timeframe replay (exit resolution, fee/P&L arithmetic, the live
routing-table read). The replay loop itself is exercised by running the
script; these pin the arithmetic it is built on.
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "scanner_lab", Path(__file__).resolve().parent.parent / "scripts" / "scanner_lab.py")
lab = importlib.util.module_from_spec(_SPEC)
sys.modules["scanner_lab"] = lab  # dataclasses resolve annotations via sys.modules
_SPEC.loader.exec_module(lab)

from execution.fees import FeeModel  # noqa: E402


def _bars(highs, lows, closes=None):
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)
    closes = np.array(closes if closes is not None else (highs + lows) / 2, dtype=float)
    return highs, lows, closes


class TestSimulateTrade:
    def test_stop_wins_when_both_touched_same_bar(self):
        h, l, c = _bars([101, 105], [99, 95])
        # long from bar 0: stop 96 and tp 104 both inside bar 1's range
        idx, px, reason = lab.simulate_trade(h, l, c, 1, "long", stop=96, tp=104, max_hold_bars=10)
        assert (idx, px, reason) == (1, 96, "stop")

    def test_tp_hit_on_later_bar(self):
        h, l, c = _bars([101, 102, 106], [100, 100.5, 103])
        idx, px, reason = lab.simulate_trade(h, l, c, 0, "long", stop=99, tp=105, max_hold_bars=10)
        assert (idx, px, reason) == (2, 105, "tp1")

    def test_short_mirror(self):
        h, l, c = _bars([101, 100, 97], [99, 98, 94])
        idx, px, reason = lab.simulate_trade(h, l, c, 0, "short", stop=102, tp=95, max_hold_bars=10)
        assert (idx, px, reason) == (2, 95, "tp1")
        idx, px, reason = lab.simulate_trade(h, l, c, 0, "short", stop=100.5, tp=90, max_hold_bars=10)
        assert reason == "stop" and px == 100.5

    def test_max_hold_closes_at_last_bar_close(self):
        h, l, c = _bars([101] * 10, [99] * 10, [100.2] * 10)
        idx, px, reason = lab.simulate_trade(h, l, c, 2, "long", stop=90, tp=110, max_hold_bars=3)
        assert (idx, px, reason) == (4, 100.2, "max_hold")

    def test_end_of_data_before_max_hold(self):
        h, l, c = _bars([101] * 4, [99] * 4, [100.0] * 4)
        idx, px, reason = lab.simulate_trade(h, l, c, 2, "long", stop=90, tp=110, max_hold_bars=10)
        assert (idx, reason) == (3, "eod")


class TestFeePnl:
    def test_long_win_matches_fee_model_round_trip_outside_scalper_window(self):
        fm = FeeModel(scalper_offer=True)
        res = lab.fee_pnl(fm, "BTC/USDT", "long", 100.0, 101.0, hold_sec=3600, notional=30000.0, risk=0.5)
        assert res["gross_pct"] == pytest.approx(1.0)
        # exit leg outside the 30-min window: taker in + taker out, exit leg
        # charged on its own notional (101/100)
        expected_fee = fm.side_pct("taker") + fm.side_pct("taker") * 1.01
        assert res["fee_pct"] == pytest.approx(expected_fee, abs=1e-4)
        assert res["pnl_usd"] == pytest.approx(30000.0 * (1.0 - expected_fee) / 100, abs=0.05)
        assert res["exit_r"] == pytest.approx((100.0 * (1.0 - expected_fee) / 100) / 0.5, abs=1e-3)

    def test_btc_exit_inside_scalper_window_pays_entry_only(self):
        fm = FeeModel(scalper_offer=True)
        res = lab.fee_pnl(fm, "BTC/USDT", "long", 100.0, 101.0, hold_sec=600, notional=30000.0, risk=0.5)
        assert res["fee_pct"] == pytest.approx(fm.side_pct("taker"), abs=1e-6)

    def test_short_sign_symmetry(self):
        fm = FeeModel()
        long_ = lab.fee_pnl(fm, "ETH/USDT", "long", 100.0, 99.0, 3600, 30000.0, 0.5)
        short = lab.fee_pnl(fm, "ETH/USDT", "short", 100.0, 99.0, 3600, 30000.0, 0.5)
        assert long_["gross_pct"] == pytest.approx(-1.0)
        assert short["gross_pct"] == pytest.approx(1.0)

    def test_pnl_linear_in_notional(self):
        fm = FeeModel()
        a = lab.fee_pnl(fm, "BTC/USDT", "long", 100.0, 100.8, 3600, 30000.0, 0.5)
        b = lab.fee_pnl(fm, "BTC/USDT", "long", 100.0, 100.8, 3600, 60000.0, 0.5)
        assert b["pnl_usd"] == pytest.approx(2 * a["pnl_usd"], abs=0.02)
        assert b["exit_r"] == pytest.approx(a["exit_r"])


class TestExitModels:
    """Excursion arrays are in R: fav = best price beyond entry in the
    trade's favour, adv = worst price against it (negative when the bar
    never went against the trade at all)."""

    def test_targets_and_ladder_on_a_winning_path(self):
        fav = np.array([0.8, 1.6, 2.6, 2.6, 2.6])
        # adv < 0 means the bar's worst price stayed that far ABOVE entry:
        # bar 2's low is +0.8R (above the +0.6R trail), bar 3 pulls back to
        # 0.9R below entry, bar 4 to 1.7R below.
        adv = np.array([0.2, 0.1, -0.8, 0.9, 1.7])
        close = np.array([0.7, 1.5, 2.5, 1.0, 0.9])
        o = lab.exit_models(fav, adv, close, tp1_r=1.5, tp2_r=2.5)
        assert o["tp1"] == 1.5 and o["r1"] == 1.0 and o["r2"] == 2.0
        assert o["r3"] == -1.0                      # never reaches 3R; bar 4 adv 1.7 hits the -1R stop
        assert o["hold"] == 0.9 and o["mfe"] == 2.6 and o["mae"] == 1.7
        # ladder: 35% at 1.5 (bar 1, stop -> BE, trail 0.6), 35% at 2.5 (bar 2, trail -> 1.6),
        # runner stopped on bar 3 at the 1.6 trail (adv 0.9 >= -1.6)
        assert o["ladder"] == pytest.approx(0.35 * 1.5 + 0.35 * 2.5 + 0.30 * 1.6)
        # trail-only: stop 0.6 after bar 1, 1.6 after bar 2, hit on bar 3
        assert o["trail"] == pytest.approx(1.6)

    def test_stop_first_when_both_touched(self):
        o = lab.exit_models(np.array([1.6, 0.2]), np.array([1.0, 0.1]), np.array([-0.9, 0.1]), 1.5, 2.5)
        assert o["tp1"] == -1.0 and o["ladder"] == -1.0 and o["trail"] == -1.0 and o["r1"] == -1.0

    def test_trail_needs_1r_before_it_moves(self):
        # peaks at 0.9R then closes flat: no trail engaged, settles at the final close
        o = lab.exit_models(np.array([0.9, 0.9]), np.array([-0.2, 0.3]), np.array([0.5, 0.0]), 1.5, 2.5)
        assert o["trail"] == 0.0 and o["tp1"] == 0.0


class TestLiveAdapters:
    def test_regime_routing_read_from_live_source(self):
        routing = lab.load_regime_routing_names()
        assert "trending_up" in routing and "sideways" in routing
        assert routing["low_liquidity"] == []
        assert "liquidity_sweep" in routing["quiet"]
        for names in routing.values():
            for n in names:
                assert not n.startswith("_scan_")

    def test_delta_symbol_mapping(self):
        assert lab.delta_symbol("BTC/USDT") == "BTCUSD"
        assert lab.delta_symbol("PEPE/USDT") == "1000PEPEUSD"

    def test_fee_drag_is_fraction_of_stop(self):
        fm = FeeModel(scalper_offer=True)
        tight = lab.fee_drag_r(fm, "BTC/USDT", 30000.0, sl_pct=0.5)
        wide = lab.fee_drag_r(fm, "BTC/USDT", 30000.0, sl_pct=1.0)
        assert tight == pytest.approx(2 * wide)
        assert lab.fee_drag_r(fm, "BTC/USDT", 30000.0, sl_pct=0.0) == 999.0
