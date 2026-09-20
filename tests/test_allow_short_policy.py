"""Tests for the 2026-09-17 allow_short policy PR.

Scanners stay two-sided (no per-scanner hard SHORT block tied to one
historical win-rate number); the single point that can suppress a side is
the symbol-keyed ALLOW_SHORT policy, applied in analyze() as a hard veto
regardless of which scanner produced the setup. See ALLOW_SHORT's own
comment in strategies/scalp_strategy.py for the evidence it reflects
(ETHUSD-specific long/short asymmetry measured three ways this session,
did not replicate on BTCUSD).

Two things to verify, matching the two halves of that design:
1. A scanner with a previously-disabled SHORT path (ema_momentum) actually
   emits a SHORT setup again -- the capability is real, not just removed
   code that happens to still return None for some other reason.
2. The policy function itself does the per-symbol discrimination.
"""
import numpy as np
import pandas as pd

from strategies.scalp_strategy import ScalpStrategy, ALLOW_SHORT, _short_allowed


def _ema_momentum_bearish_df() -> pd.DataFrame:
    """40 warmup bars (mild uptrend, EMA8>EMA21) + a 10-bar downtrend leg
    (produces a bearish EMA8/21 cross) + a 4-bar pullback that brings price
    back within 0.6xATR of the cross zone without re-crossing + a bearish,
    volume-confirmed trigger candle with RSI turning down -- the scanner's
    exact 4-step SHORT sequence. Verified against the pre-quarantine code
    (git HEAD) to return None there and "short" here -- the only change is
    that the scanner's own bearish path is no longer disabled.
    """
    rng = np.random.default_rng(2)
    rows = []
    prev_close = 200.0
    for _ in range(40):
        c = prev_close + 0.15 + rng.uniform(-0.02, 0.02)
        rows.append({"open": prev_close, "close": c,
                     "high": max(prev_close, c) + 0.05,
                     "low": min(prev_close, c) - 0.05, "volume": 100.0})
        prev_close = c
    for _ in range(10):
        c = prev_close - 0.5
        rows.append({"open": prev_close, "close": c,
                     "high": max(prev_close, c) + 0.05,
                     "low": min(prev_close, c) - 0.05, "volume": 100.0})
        prev_close = c
    for _ in range(4):
        c = prev_close + 0.5
        rows.append({"open": prev_close, "close": c,
                     "high": max(prev_close, c) + 0.05,
                     "low": min(prev_close, c) - 0.05, "volume": 90.0})
        prev_close = c
    rows.append({"open": prev_close, "close": prev_close + 0.1,
                 "high": prev_close + 0.15, "low": prev_close - 0.02, "volume": 90.0})
    prev_close = rows[-1]["close"]
    trigger_close = prev_close - 0.4
    rows.append({"open": prev_close, "close": trigger_close,
                 "high": prev_close + 0.02, "low": trigger_close - 0.05, "volume": 160.0})

    df = pd.DataFrame(rows)
    df["timestamp"] = (pd.Timestamp("2026-01-01") + pd.to_timedelta(np.arange(len(df)) * 5, unit="m")).astype(np.int64) // 10**6
    return df


class TestScannersStayTwoSided:
    def test_ema_momentum_emits_short(self):
        strat = ScalpStrategy({})
        df = _ema_momentum_bearish_df()
        enriched = strat._compute_indicators(df.copy())

        result = strat._scan_ema_momentum("TEST", enriched, 0, 0)
        assert result is not None
        assert result.side.value == "short"
        assert result.name == "ema_momentum"


class TestAllowShortPolicy:
    def test_eth_short_blocked(self):
        assert _short_allowed("ETH/USDT") is False
        assert ALLOW_SHORT["ETH/USDT"] is False

    def test_btc_short_allowed(self):
        assert _short_allowed("BTC/USDT") is True

    def test_unlisted_symbol_defaults_allowed(self):
        """A symbol not in ALLOW_SHORT is not gated by assumption -- only
        entries backed by measured evidence suppress a side."""
        assert _short_allowed("SOL/USDT") is True
