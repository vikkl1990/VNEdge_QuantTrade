"""Unit tests for BrainMemory — verify matrix operations don't break trading."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from bot.brain_memory import BrainMemory, PerformanceCell


def test_record_outcome_creates_cell():
    m = BrainMemory(storage_dir=None) if hasattr(BrainMemory.__init__, 'storage_dir') else BrainMemory()
    m._matrix.clear()
    m.record_outcome("structure_bounce", "trending_up", "BTC/USDT", 14, True, 10.0, 1.5, 180)
    cell = m.get_performance("structure_bounce", "trending_up", "BTC/USDT", "14", min_samples=1)
    assert cell is not None
    assert cell.wins == 1
    assert cell.win_rate == 100.0


def test_wildcard_aggregation():
    m = BrainMemory()
    m._matrix.clear()
    for _ in range(5):
        m.record_outcome("ema_momentum", "ranging", "BTC/USDT", 10, False, -5.0, -1.0, 120)
    # Should aggregate at all 4 levels
    assert len(m._matrix) >= 4
    cell = m.get_performance("ema_momentum", "ranging", "BTC/USDT", "10", min_samples=1)
    assert cell.win_rate == 0.0
    assert cell.losses == 5


def test_get_bad_hours():
    m = BrainMemory()
    m._hourly_perf.clear()
    for _ in range(15):
        m.record_outcome("structure_bounce", "ranging", "BTC/USDT", 3, False, -2.0, -1.0, 60)
    bad = m.get_bad_hours(min_trades=10, max_wr=35.0)
    assert 3 in bad


def test_predict_next_regime_no_data():
    m = BrainMemory()
    pred, prob = m.predict_next_regime("UNKNOWN/USDT")
    assert pred == "unknown"
    assert prob == 0.0


def test_save_load_roundtrip(tmp_path):
    m = BrainMemory(storage_dir=tmp_path)
    m.record_outcome("structure_bounce", "trending_up", "BTC/USDT", 14, True, 10.0, 1.5, 180)
    m.save()
    m2 = BrainMemory(storage_dir=tmp_path)
    assert m2._total_observations == 1
    assert len(m2._matrix) >= 4


if __name__ == "__main__":
    test_record_outcome_creates_cell()
    test_wildcard_aggregation()
    test_get_bad_hours()
    test_predict_next_regime_no_data()
    print("All tests passed (manual run)")
