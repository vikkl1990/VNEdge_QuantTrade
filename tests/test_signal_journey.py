"""Unit tests for bot.signal_journey and bot.supervisor (Phase 1).

These tests never touch production storage paths — they redirect
SignalJourney._JOURNAL_FILE to a tmp path via monkeypatch.

Run: python3 -m pytest tests/test_signal_journey.py -q
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import signal_journey as sj_mod
from bot.signal_journey import SignalJourney, StageResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_journal(tmp_path, monkeypatch):
    """Redirect JOURNAL_FILE to a tmp path so tests don't touch prod storage."""
    path = tmp_path / "signal_journeys.jsonl"
    monkeypatch.setattr(sj_mod, "_STORAGE_DIR", tmp_path)
    monkeypatch.setattr(sj_mod, "_JOURNAL_FILE", path)
    return path


# ---------------------------------------------------------------------------
# SignalJourney lifecycle
# ---------------------------------------------------------------------------

def test_journey_lifecycle(tmp_journal):
    """begin → stamp×3 → close persists a single JSONL record."""
    sig = {"trade_id": "T-1", "symbol": "BTC/USDT", "side": "long", "grade": "A"}
    SignalJourney.begin(sig)
    SignalJourney.stamp(sig, "strategy", passed=True, reason="structure_bounce")
    time.sleep(0.01)
    SignalJourney.stamp(sig, "risk_check", passed=True, reason="ok")
    SignalJourney.stamp(sig, "signal_tracker", passed=True, reason="tracked")
    SignalJourney.close(sig)

    assert tmp_journal.exists()
    lines = tmp_journal.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["trade_id"] == "T-1"
    assert rec["symbol"] == "BTC/USDT"
    assert rec["stage_count"] == 3
    assert [s["stage"] for s in rec["stages"]] == ["strategy", "risk_check", "signal_tracker"]
    assert rec["stages"][0]["passed"] is True
    assert rec["total_ms"] >= 0
    # cleanup
    assert "_journey" not in sig
    assert "_journey_start" not in sig


def test_stamp_without_begin_auto_initializes(tmp_journal):
    """stamp() on a dict without begin() should still work."""
    sig = {"trade_id": "T-2", "symbol": "ETH/USDT"}
    SignalJourney.stamp(sig, "strategy", passed=True, reason="ok")
    stages = SignalJourney.get_journey(sig)
    assert len(stages) == 1
    assert stages[0].stage == "strategy"
    SignalJourney.close(sig)


def test_close_idempotent(tmp_journal):
    """Calling close() twice produces only one JSONL record."""
    sig = {"trade_id": "T-3", "symbol": "SOL/USDT"}
    SignalJourney.begin(sig)
    SignalJourney.stamp(sig, "strategy", passed=True, reason="x")
    SignalJourney.close(sig)
    SignalJourney.close(sig)  # second close is no-op
    SignalJourney.close(sig)  # third also no-op

    lines = tmp_journal.read_text().strip().splitlines()
    assert len(lines) == 1


def test_close_empty_journey_is_noop(tmp_journal):
    """Close on a signal with no stages should not write anything."""
    sig = {"trade_id": "T-4"}
    SignalJourney.begin(sig)
    SignalJourney.close(sig)
    assert not tmp_journal.exists() or tmp_journal.stat().st_size == 0


def test_backwards_compat_non_dict(tmp_journal):
    """stamp on non-dict should not raise."""
    SignalJourney.begin(None)  # type: ignore
    SignalJourney.stamp(None, "strategy", passed=True, reason="x")  # type: ignore
    SignalJourney.close("not-a-dict")  # type: ignore
    # no exception = success


def test_backwards_compat_missing_key(tmp_journal):
    """A sig_dict that never saw begin() should still handle get_journey."""
    sig = {"trade_id": "T-5"}
    assert SignalJourney.get_journey(sig) == []


def test_stamp_latency_is_monotonic(tmp_journal):
    """Latency_ms on subsequent stamps should accumulate from previous ts."""
    sig = {"trade_id": "T-6"}
    SignalJourney.begin(sig)
    SignalJourney.stamp(sig, "a", passed=True, reason="")
    time.sleep(0.02)
    SignalJourney.stamp(sig, "b", passed=True, reason="")
    stages = SignalJourney.get_journey(sig)
    assert stages[1].latency_ms >= 15  # ~20ms with some slack
    SignalJourney.close(sig)


def test_load_by_trade_id(tmp_journal):
    """Write 3 journeys, load one by trade_id."""
    for tid in ("A", "B", "C"):
        sig = {"trade_id": tid, "symbol": f"{tid}/USDT"}
        SignalJourney.begin(sig)
        SignalJourney.stamp(sig, "strategy", passed=True, reason=f"reason-{tid}")
        SignalJourney.close(sig)

    rec = SignalJourney.load_by_trade_id("B")
    assert rec is not None
    assert rec["trade_id"] == "B"
    assert rec["symbol"] == "B/USDT"
    assert rec["stages"][0]["reason"] == "reason-B"

    # Non-existent → None
    assert SignalJourney.load_by_trade_id("NOPE") is None


def test_load_recent_tail_order(tmp_journal):
    """load_recent should return chronological order with limit."""
    for i in range(5):
        sig = {"trade_id": f"T-{i}"}
        SignalJourney.begin(sig)
        SignalJourney.stamp(sig, "strategy", passed=True, reason="")
        SignalJourney.close(sig)

    recent = SignalJourney.load_recent(limit=3)
    assert len(recent) == 3
    # chronological = oldest first among the 3 returned
    ids = [r["trade_id"] for r in recent]
    assert ids == ["T-2", "T-3", "T-4"]


def test_reason_truncation(tmp_journal):
    """Reason strings > 100 chars should be truncated."""
    sig = {"trade_id": "T-long"}
    long_reason = "x" * 500
    SignalJourney.stamp(sig, "strategy", passed=False, reason=long_reason)
    stages = SignalJourney.get_journey(sig)
    assert len(stages[0].reason) == 100
    SignalJourney.close(sig)


def test_stage_result_namedtuple_fields():
    """StageResult field contract."""
    sr = StageResult(stage="s", passed=True, reason="r", latency_ms=1.0, ts=1.0)
    assert sr.stage == "s"
    assert sr.passed is True
    assert sr.reason == "r"
    assert sr.latency_ms == 1.0
    assert sr.ts == 1.0


# ---------------------------------------------------------------------------
# Supervisor checks (read-only, mock trackers)
# ---------------------------------------------------------------------------

def test_supervisor_stuck_trade_paper():
    """A paper trade entry_time > MAX_TRADE_AGE_SEC ago should alert."""
    from bot.supervisor import Supervisor
    from datetime import datetime, timezone, timedelta

    old_iso = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    fake_ts = SimpleNamespace(entry_time=old_iso, symbol="BTC/USDT", mfe_r=0.0)
    fake_tracker = SimpleNamespace(_active={"T-stuck": fake_ts})

    sup = Supervisor(signal_tracker=fake_tracker, real_manager=None, heartbeat=None)
    alerts = sup._check_stuck_trades()
    assert len(alerts) == 1
    assert alerts[0]["check"] == "stuck_trade"
    assert "stuck" in alerts[0]["detail"].lower()


def test_supervisor_stuck_trade_real():
    """A real trade opened_at > MAX_TRADE_AGE_SEC ago should alert as critical."""
    from bot.supervisor import Supervisor

    old_ts = time.time() - (5 * 3600)
    fake_real = SimpleNamespace(opened_at=old_ts, symbol="ETH/USDT", peak_mfe_r=0.0)
    fake_manager = SimpleNamespace(real_trades={"R-stuck": fake_real}, paper_to_real={})

    sup = Supervisor(signal_tracker=None, real_manager=fake_manager, heartbeat=None)
    alerts = sup._check_stuck_trades()
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "critical"
    assert "REAL" in alerts[0]["detail"]


def test_supervisor_no_alerts_on_fresh_trade():
    """Fresh paper/real trades should produce zero alerts."""
    from bot.supervisor import Supervisor
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    fake_ts = SimpleNamespace(entry_time=now_iso, symbol="BTC/USDT", mfe_r=0.0)
    fake_real = SimpleNamespace(opened_at=time.time(), symbol="ETH/USDT", peak_mfe_r=0.0)
    fake_tracker = SimpleNamespace(_active={"T-fresh": fake_ts})
    fake_manager = SimpleNamespace(real_trades={"R-fresh": fake_real}, paper_to_real={})

    sup = Supervisor(signal_tracker=fake_tracker, real_manager=fake_manager, heartbeat=None)
    assert sup._check_stuck_trades() == []


def test_supervisor_paper_real_drift_detects_divergence():
    """When paper_r and real_r diverge > threshold, alert is emitted."""
    from bot.supervisor import Supervisor

    fake_paper_ts = SimpleNamespace(symbol="BTC/USDT", mfe_r=1.5)
    fake_real_t = SimpleNamespace(peak_mfe_r=0.2)
    fake_tracker = SimpleNamespace(_active={"P1": fake_paper_ts})
    fake_manager = SimpleNamespace(
        real_trades={"R1": fake_real_t},
        paper_to_real={"P1": "R1"},
    )

    sup = Supervisor(signal_tracker=fake_tracker, real_manager=fake_manager, heartbeat=None)
    alerts = sup._check_paper_real_drift()
    assert len(alerts) == 1
    assert alerts[0]["check"] == "paper_real_drift"
    assert alerts[0]["drift_r"] >= 0.5


def test_supervisor_paper_real_drift_below_threshold_silent():
    """Drift under threshold should not alert."""
    from bot.supervisor import Supervisor

    fake_paper_ts = SimpleNamespace(symbol="BTC/USDT", mfe_r=1.0)
    fake_real_t = SimpleNamespace(peak_mfe_r=0.9)
    fake_tracker = SimpleNamespace(_active={"P1": fake_paper_ts})
    fake_manager = SimpleNamespace(
        real_trades={"R1": fake_real_t},
        paper_to_real={"P1": "R1"},
    )

    sup = Supervisor(signal_tracker=fake_tracker, real_manager=fake_manager, heartbeat=None)
    assert sup._check_paper_real_drift() == []


def test_supervisor_status_shape():
    """status property should expose fields the dashboard expects."""
    from bot.supervisor import Supervisor

    sup = Supervisor(signal_tracker=None, real_manager=None, heartbeat=None)
    st = sup.status
    for key in ("running", "last_run", "anomaly_count", "recent_alerts", "uptime_sec"):
        assert key in st
    assert st["anomaly_count"] == 0
    assert st["recent_alerts"] == []
