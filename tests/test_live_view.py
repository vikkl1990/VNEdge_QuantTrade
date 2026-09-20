"""Operator actions, live-view builders and the websocket hub (2026-09-20)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from bot import live_view
from bot.signal_tracker import SignalTracker, TrackedSignal
from bot.trade_calculator import calc_liquidation_price
from execution.fees import FeeModel, get_fee_model


@pytest.fixture
def tracker(monkeypatch):
    tr = SignalTracker(config={"execution": {"order_type": "taker"}})
    tr._active.clear(); tr._closed.clear(); tr._manual_close.clear()
    for name in ("_save_active", "_save_closed", "_save_stats", "_recalc_stats", "_send_ml_feedback"):
        monkeypatch.setattr(tr, name, lambda *a, **k: None)
    return tr


def _open(tr, tid="t1", symbol="BTC/USDT", side="long", entry=100_000.0, stop=99_300.0):
    ts = TrackedSignal(trade_id=tid, symbol=symbol, side=side, entry_price=entry, stop_loss=stop,
                       tp1=entry + 1400 if side == "long" else entry - 1400,
                       initial_risk=abs(entry - stop), leverage=30, paper_stake=100.0, position_size_usd=3000.0,
                       entry_time=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                       highest_price=entry + 300, lowest_price=entry - 300, fill_price_captured=True)
    tr._active[tid] = ts
    return ts


def test_close_trade_books_at_price_through_finalizer(tracker):
    _open(tracker)
    res = tracker.close_trade("t1", 100_200.0)
    assert res["ok"] and res["closed"]["exit_reason"] == "manual_close"
    assert "t1" not in tracker._active and len(tracker._closed) == 1
    c = tracker._closed[0]
    assert c["exit_price"] == 100_200.0 and c["status"] == "manual_close"
    assert c["gross_pnl_pct"] == pytest.approx(0.2, abs=1e-6)
    assert c["total_fees_usd"] > 0 and c["pnl_usd"] < c["gross_pnl_usd"]
    assert c["metadata"]["manual_close"]["by"] == "dashboard"


def test_close_trade_unknown_or_no_price(tracker):
    assert tracker.close_trade("nope", 1.0)["ok"] is False
    _open(tracker)
    assert tracker.close_trade("t1", 0)["ok"] is False
    assert "t1" in tracker._active


def test_partial_close_locks_leg_then_final_close_uses_remaining(tracker):
    ts = _open(tracker)
    res = tracker.close_partial("t1", 0.5, 100_500.0)
    assert res["ok"] and ts.position_remaining_pct == 0.5
    assert ts.tp1_pnl_locked == pytest.approx(0.5 * 0.5, abs=1e-6)   # half the position at +0.5%
    assert ts.metadata["manual_partials"][0]["fraction"] == 0.5
    tracker.close_trade("t1", 100_000.0)                             # remainder flat
    assert tracker._closed[0]["gross_pnl_pct"] == pytest.approx(0.25, abs=1e-6)


def test_partial_close_full_fraction_becomes_close(tracker):
    _open(tracker)
    res = tracker.close_partial("t1", 1.0, 100_100.0)
    assert res["ok"] is False                        # fraction must be < 1
    res = tracker.close_partial("t1", 0.995, 100_100.0)
    assert res["ok"] and "t1" not in tracker._active


def test_set_stop_and_breakeven_rules(tracker):
    ts = _open(tracker)
    assert tracker.set_stop("t1", 100_100.0, 100_050.0)["ok"] is False      # long stop above price
    r = tracker.set_stop("t1", 99_500.0, 100_050.0)
    assert r["ok"] and ts.stop_loss == 99_500.0 and ts.breakeven_set is False
    assert tracker.move_stop_to_breakeven("t1", 100_050.0)["ok"] is False  # not past entry + round trip
    r = tracker.move_stop_to_breakeven("t1", 100_600.0)
    assert r["ok"] and ts.stop_loss > 100_000.0 and ts.breakeven_set is True
    assert ts.metadata["manual_stop_moves"][-1]["new"] == ts.stop_loss


def test_close_all_needs_fresh_prices(tracker):
    _open(tracker, "a", "BTC/USDT"); _open(tracker, "b", "ETH/USDT", entry=4000.0, stop=3970.0)
    res = tracker.close_all({"BTC/USDT": 100_100.0})
    assert res["n"] == 2 and res["closed"] == 1 and res["ok"] is False
    assert "b" in tracker._active and "a" not in tracker._active


def test_enrich_position_numbers():
    fm = FeeModel(scalper_offer=True)          # the offer is on for this account (settings.yaml fees:)
    now = datetime(2026, 9, 20, 7, 50, tzinfo=timezone.utc)
    t = {"trade_id": "x", "symbol": "BTC/USDT", "side": "long", "entry_price": 100_000.0, "stop_loss": 99_300.0,
         "leverage": 30, "paper_stake": 100.0, "position_size_usd": 3000.0, "initial_risk": 700.0,
         "entry_atr": 350.0, "entry_time": (now - timedelta(minutes=10)).isoformat(), "fee_type": "taker_entry"}
    meta = {"mark": 100_500.0, "index": 100_480.0, "basis_pct": 0.02, "funding_rate": 0.01}
    e = live_view.enrich_position(t, 100_500.0, meta, fm, now=now)
    assert e["liq_price"] == calc_liquidation_price(100_000.0, 30, "long")
    assert e["liq_dist_pct"] > 0 and e["stop_dist_pct"] > 0 and e["r_now"] == pytest.approx(500 / 700, abs=0.01)
    assert e["gross_usd"] == pytest.approx(15.0, abs=0.01)
    assert e["exit_free_now"] is True and e["exit_fee_usd"] == 0.0          # 10 min < 30 min BTC window
    assert e["net_usd"] == pytest.approx(15.0 - e["entry_fee_usd"], abs=0.01)
    assert e["roe_pct"] == pytest.approx(e["net_usd"] / 100 * 100, abs=0.01)
    assert e["funding_est_usd"] < 0 and e["funding_charged"] is False       # long pays positive funding
    assert e["next_funding_sec"] == 10 * 60


def test_funding_countdown_wraps_midnight():
    assert live_view.seconds_to_next_funding(datetime(2026, 9, 20, 23, 59, 30, tzinfo=timezone.utc)) == 30
    assert live_view.seconds_to_next_funding(datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)) == 8 * 3600


def test_fee_ledger_summary_counts_fee_flips():
    fm = get_fee_model()
    closed = [
        {"trade_id": "1", "symbol": "BTC/USDT", "position_size_usd": 3000, "gross_pnl_usd": 2.0, "pnl_usd": -1.5,
         "total_fees_usd": 3.5, "fee_type": "taker_entry", "within_scalper": True, "exit_time": "2026-09-19T10:00:00+00:00", "risk_amount_usd": 21.0},
        {"trade_id": "2", "symbol": "ETH/USDT", "position_size_usd": 3000, "gross_pnl_usd": 10.0, "pnl_usd": 6.5,
         "total_fees_usd": 3.5, "fee_type": "maker_entry", "within_scalper": False, "exit_time": "2026-09-19T11:00:00+00:00"},
    ]
    led = live_view.fee_ledger(closed, fm)
    s = led["summary"]
    assert s["n"] == 2 and s["fees_usd"] == 7.0 and s["trades_flipped_by_fees"] == 1
    assert s["maker_entries"] == 1 and s["free_exits"] == 1 and s["gross_win_rate_pct"] == 100.0 and s["net_win_rate_pct"] == 50.0
    assert led["rows"][0]["trade_id"] == "2"            # newest first
    assert led["rows"][1]["fee_r"] == pytest.approx(3.5 / 21.0, abs=1e-3)
    assert led["rows"][1]["entry_fee_usd"] + led["rows"][1]["exit_fee_usd"] == pytest.approx(3.5, abs=0.01)


def test_chart_overlay_lines_markers_and_structure():
    n = 300
    t0 = 1_758_000_000
    ts = np.arange(n) * 300 + t0
    close = 100_000 + np.cumsum(np.random.default_rng(1).normal(0, 80, n))
    df = pd.DataFrame({"timestamp": ts * 1000, "open": close, "high": close + 60, "low": close - 60, "close": close, "volume": 1.0})
    iso = lambda s: datetime.fromtimestamp(s, tz=timezone.utc).isoformat()
    active = [{"symbol": "BTC/USDT", "side": "long", "entry_price": 100_000.0, "stop_loss": 99_300.0, "tp1": 101_000.0,
               "leverage": 30, "entry_time": iso(t0 + 150 * 300 + 17)}]
    closed = [{"symbol": "BTC/USDT", "side": "short", "entry_time": iso(t0 + 20 * 300), "exit_time": iso(t0 + 40 * 300),
               "pnl_usd": -2.0, "setup_type": "liquidity_sweep"},
              {"symbol": "ETH/USDT", "side": "long", "entry_time": iso(t0 + 20 * 300), "exit_time": iso(t0 + 40 * 300), "pnl_usd": 1.0}]
    ov = live_view.chart_overlay(df, "BTC/USDT", active, closed)
    kinds = {l["kind"] for l in ov["lines"]}
    assert {"entry", "stop", "tp", "liq"} <= kinds
    assert [m["time"] for m in ov["markers"]] == sorted(m["time"] for m in ov["markers"])
    assert any(m["text"] == "long open" and m["time"] == t0 + 150 * 300 for m in ov["markers"])   # snapped to the bar
    assert all(m["text"] != "1.00" for m in ov["markers"])                                           # ETH trade excluded
    st = ov["structure"]
    assert st["n_swings"] > 0 and st["state"] in ("up", "down", "range", "none") and isinstance(st["naked_levels"], list)


class _FakeWS:
    def __init__(self):
        self.sent = []; self.closed = False
    async def send_str(self, s):
        self.sent.append(json.loads(s))


def test_hub_coalesces_ticks_and_tags_frames():
    from dashboard.websocket_handler import WebSocketHub

    async def run():
        hub = WebSocketHub(); ws = _FakeWS(); await hub.add(ws)
        await hub.push_tick({"BTC/USDT": 1.0})
        await hub.push_tick({"BTC/USDT": 2.0})
        await hub.push_tick({"ETH/USDT": 3.0})
        assert len(ws.sent) == 1                                  # second and third coalesced
        await asyncio.sleep(0.7)
        assert len(ws.sent) == 2 and ws.sent[1]["data"]["prices"] == {"BTC/USDT": 2.0, "ETH/USDT": 3.0}
        assert ws.sent[0]["channel"] == "tick" and "ts" in ws.sent[0]
        await hub.push_events([{"type": "sl_hit", "message": "m", "signal": {"trade_id": "t", "symbol": "BTC/USDT", "pnl_usd": np.float64(1.5)}}])
        assert ws.sent[-1]["channel"] == "event" and ws.sent[-1]["data"]["pnl_usd"] == 1.5
        await hub.push_position([{"trade_id": "t"}]); await hub.push_position([{"trade_id": "t"}])
        assert sum(1 for f in ws.sent if f["channel"] == "position") == 1   # throttled
        ws.closed = True
        await hub.broadcast("tick", {}); assert hub.client_count == 0
    asyncio.run(run())


# ── dashboard handlers, called directly (auth middleware is not in the path) ──
@pytest.fixture
def server(tracker, monkeypatch):
    from dashboard.server import DashboardServer
    srv = DashboardServer()
    srv._signal_tracker = tracker
    srv._orchestrator = None
    srv._prices = {"BTC/USDT": 100_250.0}
    monkeypatch.setattr(srv, "add_alert", _noop_async)
    return srv


async def _noop_async(*a, **k):
    return None


def _req(method, path, **kw):
    from aiohttp.test_utils import make_mocked_request
    return make_mocked_request(method, path, **kw)


def test_live_position_endpoint(server, tracker):
    _open(tracker)
    resp = asyncio.run(server._handle_live_position(_req("GET", "/api/live/position")))
    d = json.loads(resp.text)
    assert d["n"] == 1 and d["trades"][0]["mark"] == 100_250.0 and d["trades"][0]["liq_price"] > 0
    assert d["trades"][0]["net_usd"] is not None and d["max_open"] >= 1


def test_trade_close_endpoint_and_close_all_pause(server, tracker, monkeypatch):
    _open(tracker, "a"); _open(tracker, "b", "ETH/USDT", entry=4000.0, stop=3970.0)
    async def empty(_r): return {}
    monkeypatch.setattr(server, "_action_body", empty)
    resp = asyncio.run(server._handle_trade_close(_req("POST", "/api/trade/a/close", match_info={"trade_id": "a"})))
    assert resp.status == 200 and json.loads(resp.text)["ok"] and "a" not in tracker._active
    resp = asyncio.run(server._handle_trade_close(_req("POST", "/api/trade/b/close", match_info={"trade_id": "b"})))
    assert resp.status == 409 and "no fresh price" in json.loads(resp.text)["error"]        # ETH has no price
    resp = asyncio.run(server._handle_trade_close(_req("POST", "/api/trade/zz/close", match_info={"trade_id": "zz"})))
    assert resp.status == 404
    server._prices["ETH/USDT"] = 4010.0
    resp = asyncio.run(server._handle_close_all(_req("POST", "/api/control/close-all")))
    d = json.loads(resp.text)
    assert d["ok"] and d["closed"] == 1 and d["paused"] is True and server.is_paused


def test_partial_and_stop_endpoints(server, tracker, monkeypatch):
    ts = _open(tracker)
    async def body_half(_r): return {"fraction": 0.5}
    monkeypatch.setattr(server, "_action_body", body_half)
    resp = asyncio.run(server._handle_trade_close_partial(_req("POST", "/x", match_info={"trade_id": "t1"})))
    assert resp.status == 200 and ts.position_remaining_pct == 0.5
    async def body_be(_r): return {"breakeven": True}
    monkeypatch.setattr(server, "_action_body", body_be)
    resp = asyncio.run(server._handle_trade_stop(_req("POST", "/x", match_info={"trade_id": "t1"})))
    assert resp.status == 200 and ts.stop_loss > 100_000.0                                  # price 100 250 is past entry + fees
    async def body_px(_r): return {"price": 100_400.0}
    monkeypatch.setattr(server, "_action_body", body_px)
    resp = asyncio.run(server._handle_trade_stop(_req("POST", "/x", match_info={"trade_id": "t1"})))
    assert resp.status == 409                                                              # above price for a long


def test_ledger_and_overlay_endpoints(server, tracker):
    tracker._closed.append({"trade_id": "c1", "symbol": "BTC/USDT", "side": "long", "position_size_usd": 3000, "gross_pnl_usd": 4.0,
                            "pnl_usd": 0.5, "total_fees_usd": 3.5, "fee_type": "taker_entry", "exit_time": "2026-09-19T10:00:00+00:00",
                            "entry_time": "2026-09-19T09:00:00+00:00"})
    resp = asyncio.run(server._handle_fee_ledger(_req("GET", "/api/ledger/fees?days=30")))
    d = json.loads(resp.text); assert d["summary"]["n"] == 1 and d["rows"][0]["trade_id"] == "c1"

    class DM:
        def get_candles(self, symbol, interval, limit=300):
            n = 200; t0 = 1_758_000_000
            close = 100_000 + np.cumsum(np.random.default_rng(2).normal(0, 80, n))
            return pd.DataFrame({"timestamp": (np.arange(n) * 300 + t0) * 1000, "open": close, "high": close + 50, "low": close - 50, "close": close, "volume": 1.0})
    server._data_manager = DM()
    _open(tracker)
    resp = asyncio.run(server._handle_chart_overlay(_req("GET", "/api/chart/overlay?symbol=BTC/USDT&interval=5m")))
    d = json.loads(resp.text)
    assert resp.status == 200 and {l["kind"] for l in d["lines"]} >= {"entry", "stop", "liq"} and "zigzag" in d["structure"]
