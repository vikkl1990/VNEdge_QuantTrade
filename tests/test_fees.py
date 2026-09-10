"""Fee model tests — execution/fees.py and its use in the signal tracker."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pytest

from execution.fees import FeeLeg, FeeModel


@pytest.fixture
def fm():
    return FeeModel(maker_rate=0.0002, taker_rate=0.0005, gst_rate=0.18)


class TestRates:
    def test_per_side_with_gst(self, fm):
        assert fm.side_pct("maker") == pytest.approx(0.0236)
        assert fm.side_pct("taker") == pytest.approx(0.059)

    def test_round_trip(self, fm):
        assert fm.round_trip_pct("maker", "taker") == pytest.approx(0.0826)
        assert fm.round_trip_pct("taker", "taker") == pytest.approx(0.118)

    def test_free_exit_promo_only_when_enabled(self):
        promo = FeeModel(free_exit=True)
        assert promo.round_trip_pct("maker", "taker") == pytest.approx(0.0236)
        assert FeeModel().round_trip_pct("maker", "taker") == pytest.approx(0.0826)

    def test_per_product_override(self, fm):
        fm.per_product["BTC/USDT"] = {"maker": 0.0001, "taker": 0.0004}
        assert fm.side_pct("taker", "BTC/USDT") == pytest.approx(0.0472)
        assert fm.side_pct("taker", "ETH/USDT") == pytest.approx(0.059)

    def test_update_from_products_reports_drift(self, fm):
        products = [
            {"id": 27, "maker_commission_rate": "0.0002", "taker_commission_rate": "0.0005"},
            {"id": 99, "maker_commission_rate": "0.0003", "taker_commission_rate": "0.0007"},
        ]
        diffs = fm.update_from_products(products, {"BTC/USDT": 27, "XYZ/USDT": 99})
        assert len(diffs) == 1 and diffs[0].startswith("XYZ/USDT")
        assert fm.side_pct("taker", "XYZ/USDT") == pytest.approx(0.0826)


class TestEntryLiquidity:
    def test_auto_maker_when_no_slippage(self, fm):
        assert fm.entry_liquidity("auto", 0.0) == "maker"

    def test_auto_taker_when_slipped(self, fm):
        assert fm.entry_liquidity("auto", 12.5) == "taker"

    def test_explicit_modes(self, fm):
        assert fm.entry_liquidity("taker_only", 0.0) == "taker"
        assert fm.entry_liquidity("maker", 30.0) == "maker"


class TestTradeFees:
    def test_single_exit_leg(self, fm):
        res = fm.trade_fees(100.0, "maker", [FeeLeg(1.0, 100.0)])
        assert res.entry_pct == pytest.approx(0.0236)
        assert res.exit_pct == pytest.approx(0.059)
        assert res.total_pct == pytest.approx(0.0826)

    def test_each_partial_leg_is_charged(self, fm):
        legs = [FeeLeg(0.35, 100.0), FeeLeg(0.35, 100.0), FeeLeg(0.30, 100.0)]
        res = fm.trade_fees(100.0, "maker", legs)
        # three legs at the entry price sum to one full taker exit
        assert res.exit_pct == pytest.approx(0.059)
        assert len(res.legs) == 4

    def test_exit_fee_scales_with_leg_price(self, fm):
        higher = fm.trade_fees(100.0, "taker", [FeeLeg(1.0, 110.0)])
        lower = fm.trade_fees(100.0, "taker", [FeeLeg(1.0, 90.0)])
        assert higher.exit_pct == pytest.approx(0.059 * 1.1)
        assert lower.exit_pct == pytest.approx(0.059 * 0.9)

    def test_usd_on_full_notional(self, fm):
        # $1,000 notional taker leg → $0.59, regardless of leverage
        assert fm.leg_fee_usd(1000.0, "taker") == pytest.approx(0.59)
        assert fm.leg_fee_usd(1000.0, "maker") == pytest.approx(0.236)

    def test_funding_off_by_default(self, fm):
        res = fm.trade_fees(100.0, "maker", [FeeLeg(1.0, 100.0)], hold_seconds=8 * 3600)
        assert res.funding_pct == 0.0
        on = FeeModel(apply_funding=True, funding_rate_8h=0.0001)
        res2 = on.trade_fees(100.0, "maker", [FeeLeg(1.0, 100.0)], hold_seconds=8 * 3600)
        assert res2.funding_pct == pytest.approx(0.01)

    def test_zero_entry_price_is_safe(self, fm):
        assert fm.trade_fees(0.0, "maker", [FeeLeg(1.0, 1.0)]).total_pct == 0.0


class TestTrackerIntegration:
    def _tracked(self, **over):
        from bot.signal_tracker import TrackedSignal
        base = dict(trade_id="t1", symbol="BTC/USDT", side="long", entry_price=100.0,
                    stop_loss=99.0, tp1=101.0, tp2=102.0, tp3=103.0,
                    entry_time="2026-09-10T00:00:00+00:00", position_size_usd=1000.0)
        base.update(over)
        ts = TrackedSignal(**base)
        ts.initial_risk = 1.0
        return ts

    def test_simple_win_net_of_maker_entry_and_taker_exit(self):
        from bot.signal_tracker import SignalTracker
        ts = self._tracked()
        net = SignalTracker._calc_pnl(ts, 101.0, "maker")
        # gross +1.0%, fees 0.0236 + 0.059 × 1.01
        assert ts.gross_pnl_pct == pytest.approx(1.0)
        assert ts.total_fees_pct == pytest.approx(0.0236 + 0.059 * 1.01, abs=1e-4)
        assert net == pytest.approx(1.0 - ts.total_fees_pct, abs=1e-4)
        # exit_r is net move in risk units: net% × 100 / 1.0
        assert ts.exit_r == pytest.approx(net, abs=1e-3)

    def test_slipped_entry_is_charged_as_taker(self):
        from bot.signal_tracker import SignalTracker
        ts = self._tracked()
        ts.slippage_bps = 8.0
        SignalTracker._calc_pnl(ts, 101.0, "auto")
        assert ts.fee_type == "taker_entry"
        assert ts.total_fees_pct == pytest.approx(0.059 + 0.059 * 1.01, abs=1e-4)

    def test_three_leg_exit_pays_three_exit_fees_once_each(self):
        from bot.signal_tracker import SignalTracker
        ts = self._tracked()
        ts.tp1_hit = ts.tp2_hit = ts.tp3_hit = True
        SignalTracker._calc_pnl(ts, 103.0, "maker")
        expected_exit = 0.059 * (0.35 * 1.01 + 0.35 * 1.02 + 0.30 * 1.03)
        assert ts.total_fees_pct == pytest.approx(0.0236 + expected_exit, abs=1e-4)
