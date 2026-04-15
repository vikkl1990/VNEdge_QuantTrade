"""Unit tests for observability module."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utils.observability import inc_metric, get_metrics_text, _metrics


def test_inc_metric():
    _metrics["test_counter"] = 0
    inc_metric("test_counter")
    assert _metrics["test_counter"] == 1
    inc_metric("test_counter", 5)
    assert _metrics["test_counter"] == 6


def test_metrics_text_format():
    inc_metric("trades_total")
    text = get_metrics_text()
    assert "vnedge_trades_total" in text
    assert "# TYPE" in text


if __name__ == "__main__":
    test_inc_metric()
    test_metrics_text_format()
    print("Observability tests passed")
