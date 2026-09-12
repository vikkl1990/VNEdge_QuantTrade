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


class TestScalperOffer:
    """Delta Exchange India Scalper Offer: free close within a window measured
    from position OPEN (30 min BTC/ETH, 15 min everything else), judged per
    leg, never for a liquidation. Off by default — confirmed live + joined
    on this account 2026-09-12, enabled in settings.yaml."""

    def test_off_by_default(self):
        fm = FeeModel()
        assert fm.scalper_offer is False
        leg = FeeLeg(1.0, 100.0, "taker", elapsed_sec=60)
        assert fm.exit_leg_is_free(leg, "BTC/USDT") is False

    def test_window_lookup_by_base_asset(self):
        fm = FeeModel(scalper_offer=True)
        assert fm.scalper_window_sec("BTC/USDT") == 1800
        assert fm.scalper_window_sec("ETH/USDT") == 1800
        assert fm.scalper_window_sec("DOGE/USDT") == 900
        assert fm.scalper_window_sec(None) == 900

    def test_leg_inside_window_is_free(self):
        fm = FeeModel(scalper_offer=True)
        leg = FeeLeg(1.0, 100.0, "taker", elapsed_sec=899)
        assert fm.exit_leg_is_free(leg, "DOGE/USDT") is True

    def test_leg_outside_window_pays(self):
        fm = FeeModel(scalper_offer=True)
        leg = FeeLeg(1.0, 100.0, "taker", elapsed_sec=901)
        assert fm.exit_leg_is_free(leg, "DOGE/USDT") is False

    def test_btc_gets_the_wider_window(self):
        fm = FeeModel(scalper_offer=True)
        leg = FeeLeg(1.0, 100.0, "taker", elapsed_sec=1200)  # 20 min
        assert fm.exit_leg_is_free(leg, "BTC/USDT") is True   # inside 30 min
        assert fm.exit_leg_is_free(leg, "DOGE/USDT") is False  # outside 15 min

    def test_liquidation_never_free(self):
        fm = FeeModel(scalper_offer=True)
        leg = FeeLeg(1.0, 100.0, "taker", elapsed_sec=1, liquidation=True)
        assert fm.exit_leg_is_free(leg, "BTC/USDT") is False

    def test_custom_windows_override_default(self):
        fm = FeeModel(scalper_offer=True, scalper_windows={"BTC": 600, "_default": 300})
        assert fm.scalper_window_sec("BTC/USDT") == 600
        assert fm.scalper_window_sec("SOL/USDT") == 300

    def test_each_leg_judged_independently_in_trade_fees(self):
        # TP1 partial closes inside the window (free); the final leg closes
        # outside it (pays) — same trade, two different outcomes.
        fm = FeeModel(scalper_offer=True)
        legs = [
            FeeLeg(0.5, 101.0, "taker", elapsed_sec=300),   # inside 900s -> free
            FeeLeg(0.5, 102.0, "taker", elapsed_sec=1200),  # outside 900s -> pays
        ]
        res = fm.trade_fees(100.0, "maker", legs, symbol="SOL/USDT")
        assert res.legs[1]["scalper_free"] is True
        assert res.legs[1]["pct"] == 0.0
        assert res.legs[2]["scalper_free"] is False
        assert res.legs[2]["pct"] == pytest.approx(0.059 * 0.5 * 1.02, abs=1e-5)

    def test_from_config_reads_scalper_offer_block(self):
        cfg = {"fees": {"scalper_offer": {"enabled": True, "windows": {"BTC": 600, "_default": 200}}}}
        fm = FeeModel.from_config(cfg)
        assert fm.scalper_offer is True
        assert fm.scalper_window_sec("BTC/USDT") == 600
        assert fm.scalper_window_sec("XRP/USDT") == 200

    def test_from_config_defaults_off_and_uses_standard_windows(self):
        fm = FeeModel.from_config({"fees": {}})
        assert fm.scalper_offer is False
        assert fm.scalper_window_sec("BTC/USDT") == 1800
        assert fm.scalper_window_sec("XRP/USDT") == 900

    def test_round_trip_pct_never_credits_the_offer(self):
        # Pre-trade estimate has no hold time to check — must stay conservative.
        fm = FeeModel(scalper_offer=True)
        assert fm.round_trip_pct("maker", "taker") == pytest.approx(0.0826)

    def test_leg_fee_usd_entry_always_pays(self):
        fm = FeeModel(scalper_offer=True)
        # is_entry=True must never be waived even with elapsed_sec=0
        assert fm.leg_fee_usd(1000.0, "maker", is_entry=True) == pytest.approx(0.236)

    def test_leg_fee_usd_exit_credits_when_inside_window(self):
        fm = FeeModel(scalper_offer=True)
        assert fm.leg_fee_usd(1000.0, "taker", "BTC/USDT", elapsed_sec=100) == 0.0
        assert fm.leg_fee_usd(1000.0, "taker", "BTC/USDT", elapsed_sec=9999) == pytest.approx(0.59)

    def test_describe_mentions_scalper_offer_when_on(self):
        fm = FeeModel(scalper_offer=True)
        assert "Scalper Offer ON" in fm.describe()
        assert "Scalper Offer" not in FeeModel().describe()


class TestTrackerHonorsScalperOffer:
    def _tracked(self, **over):
        from bot.signal_tracker import TrackedSignal
        base = dict(trade_id="t1", symbol="BTC/USDT", side="long", entry_price=100.0,
                    stop_loss=99.0, tp1=101.0, tp2=102.0, tp3=103.0,
                    entry_time="2026-09-10T00:00:00+00:00", position_size_usd=1000.0)
        base.update(over)
        ts = TrackedSignal(**base)
        ts.initial_risk = 1.0
        return ts

    def test_full_close_inside_window_is_free_when_offer_enabled(self):
        from execution.fees import FeeModel, set_fee_model
        from bot.signal_tracker import SignalTracker
        set_fee_model(FeeModel(maker_rate=0.0002, taker_rate=0.0005, gst_rate=0.18, scalper_offer=True))
        try:
            ts = self._tracked(exit_time="2026-09-10T00:10:00+00:00")  # 10 min, inside 30 min BTC window
            net = SignalTracker._calc_pnl(ts, 101.0, "maker")
            assert ts.total_fees_pct == pytest.approx(0.0236, abs=1e-4)  # entry only
            assert ts.within_scalper is True
            assert net == pytest.approx(1.0 - 0.0236, abs=1e-4)
        finally:
            set_fee_model(FeeModel(maker_rate=0.0002, taker_rate=0.0005, gst_rate=0.18))

    def test_full_close_outside_window_pays_exit_fee(self):
        from execution.fees import FeeModel, set_fee_model
        from bot.signal_tracker import SignalTracker
        set_fee_model(FeeModel(maker_rate=0.0002, taker_rate=0.0005, gst_rate=0.18, scalper_offer=True))
        try:
            ts = self._tracked(exit_time="2026-09-10T01:00:00+00:00")  # 1h, outside 30 min BTC window
            SignalTracker._calc_pnl(ts, 101.0, "maker")
            assert ts.total_fees_pct == pytest.approx(0.0236 + 0.059 * 1.01, abs=1e-4)
            assert ts.within_scalper is False
        finally:
            set_fee_model(FeeModel(maker_rate=0.0002, taker_rate=0.0005, gst_rate=0.18))

    def test_tp1_leg_inside_window_free_but_final_leg_outside_pays(self):
        from execution.fees import FeeModel, set_fee_model
        from bot.signal_tracker import SignalTracker
        set_fee_model(FeeModel(maker_rate=0.0002, taker_rate=0.0005, gst_rate=0.18, scalper_offer=True))
        try:
            ts = self._tracked(
                tp1_time="2026-09-10T00:05:00+00:00",   # 5 min: free
                exit_time="2026-09-10T00:45:00+00:00",  # 45 min: pays
            )
            ts.tp1_hit = True
            SignalTracker._calc_pnl(ts, 102.0, "maker")
            expected_exit = 0.059 * 0.65 * 1.02  # only the post-TP1 leg pays
            assert ts.total_fees_pct == pytest.approx(0.0236 + expected_exit, abs=1e-4)
        finally:
            set_fee_model(FeeModel(maker_rate=0.0002, taker_rate=0.0005, gst_rate=0.18))
