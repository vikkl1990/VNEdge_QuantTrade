"""trend_pb — trend-pullback family detector.

Pre-registered in docs/research/TREND_PB_PREREG_20260917.md; the definition
there is authoritative and this file implements it literally. Replaces
ema_momentum / trend_continuation / momentum_ride / post_impulse /
supertrend_flip once (and only once) it passes that note's kill rules.

Closed-bar only: the last row of `df` is the trigger candidate; everything
else is frozen history. Two-sided by construction — the short side is the
exact mirror of the long side (prices negated), so there is no separate
short recipe to drift. Regime gating, allow_short and any hour filter live
ABOVE this function (policy), not inside it.

Required columns (ScalpStrategy._compute_indicators provides them):
open, high, low, close, atr, ema_8, ema_21, ema_50. Optional, score/veto:
rel_vol_tod (volume vs same-hour median; falls back to rel_vol), rsi_pct
(RSI percentile), supertrend_dir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

FAMILY_ID = "trend_pb"


@dataclass
class FamilySetup:
    family_id: str
    side: str                 # "long" | "short"
    trigger_close: float      # close of the trigger bar (maker limit reference)
    stop: float               # pattern-defined: pullback extreme -/+ 0.1 ATR
    atr: float
    impulse_type: str         # "body" | "breakout"
    impulse_bars_ago: int     # bars between impulse bar and trigger bar
    pullback_bars: int
    pullback_depth_atr: float
    swing_level: float        # the pullback swing the trigger took out
    score: int
    confirmations: List[str] = field(default_factory=list)
    # contract constants (part of the definition, not tunables)
    invalidation: str = "close_through_ema21"
    expiry_bars: int = 6
    expiry_min_mfe_r: float = 0.3


def _detect_long(o, h, l, c, atr, e8, e21, e50, *, impulse_atr, max_pullback,
                 min_pullback, breakout_lookback, breakout_atr, depth_atr) -> Optional[dict]:
    """Long-side geometry on arrays already oriented so that 'up' is
    favourable. Returns a dict of the matched geometry or None."""
    n = len(c)
    for pb in range(min_pullback, max_pullback + 1):
        imp = n - 2 - pb                  # impulse bar index
        if imp - breakout_lookback < 0:
            break
        a_imp = atr[imp]
        if not (a_imp > 0) or np.isnan(a_imp):
            continue
        body = c[imp] - o[imp]
        prior_high = h[imp - breakout_lookback:imp].max()
        is_body = body >= impulse_atr * a_imp
        is_break = (c[imp] - prior_high) >= breakout_atr * a_imp
        if not (is_body or is_break):
            continue
        imp_high, imp_low = h[imp], l[imp]
        pl = l[imp + 1:n - 1]
        ph = h[imp + 1:n - 1]
        pc = c[imp + 1:n - 1]
        pe21 = e21[imp + 1:n - 1]
        if len(pl) != pb:
            continue
        # pullback integrity
        if (pl < imp_low).any() or (pc < imp_low).any():
            continue                      # new extreme against the impulse
        if (ph > imp_high).any():
            continue                      # still impulsing, not a pullback
        depth = imp_high - pl.min()
        a_now = atr[n - 1]
        if not (a_now > 0) or np.isnan(a_now) or depth > depth_atr * a_now:
            continue
        # location: pullback lows hold EMA21, or the broken level for a breakout impulse
        holds_ema = bool((pl >= pe21).all())
        holds_break = bool(is_break and (pl >= prior_high).all())
        if not (holds_ema or holds_break):
            continue
        # trigger: close takes the pullback swing high
        swing = ph.max()
        if not (c[n - 1] > swing):
            continue
        return {
            "impulse_type": "breakout" if (is_break and not is_body) else "body",
            "impulse_bars_ago": n - 1 - imp,
            "pullback_bars": pb,
            "pullback_depth_atr": float(depth / a_now),
            "pullback_low": float(pl.min()),
            "impulse_low": float(imp_low),
            "swing": float(swing),
        }
    return None


def detect_trend_pb(
    df: pd.DataFrame,
    *,
    impulse_atr: float = 0.7,
    max_pullback: int = 8,
    min_pullback: int = 1,
    breakout_lookback: int = 10,
    breakout_atr: float = 0.5,
    depth_atr: float = 1.2,
    stretch_atr: float = 2.0,
    min_rel_vol_tod: float = 1.0,
    stop_buffer_atr: float = 0.1,
    stop_mode: str = "pullback",
) -> Optional[FamilySetup]:
    """stop_mode: "pullback" (v1: pullback extreme -/+ buffer) or "impulse"
    (v2 pre-registered variant: the impulse bar's own extreme -/+ buffer)."""
    need = breakout_lookback + max_pullback + 3
    if df is None or len(df) < need:
        return None
    last = df.iloc[-1]
    atr_now = float(last["atr"])
    if not (atr_now > 0) or np.isnan(atr_now):
        return None

    # trigger-bar vetoes (identical for both sides)
    rv = float(last["rel_vol_tod"]) if "rel_vol_tod" in df.columns else float(last.get("rel_vol", 1.0))
    if np.isnan(rv) or rv < min_rel_vol_tod:
        return None

    o = df["open"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    atr = df["atr"].values.astype(float)
    e8 = df["ema_8"].values.astype(float)
    e21 = df["ema_21"].values.astype(float)
    e50 = df["ema_50"].values.astype(float)
    kw = dict(impulse_atr=impulse_atr, max_pullback=max_pullback, min_pullback=min_pullback,
              breakout_lookback=breakout_lookback, breakout_atr=breakout_atr, depth_atr=depth_atr)

    for side, sgn in (("long", 1.0), ("short", -1.0)):
        # mirror: negate prices for the short side so 'up' is favourable;
        # highs and lows swap roles under negation
        if sgn > 0:
            oo, hh, ll, cc, ee8, ee21, ee50 = o, h, l, c, e8, e21, e50
        else:
            oo, hh, ll, cc, ee8, ee21, ee50 = -o, -l, -h, -c, -e8, -e21, -e50
        # stretched veto: close already > 2 ATR beyond EMA8 in the trade direction
        if cc[-1] - ee8[-1] > stretch_atr * atr_now:
            continue
        g = _detect_long(oo, hh, ll, cc, atr, ee8, ee21, ee50, **kw)
        if g is None:
            continue
        anchor = g["impulse_low"] if stop_mode == "impulse" else g["pullback_low"]
        stop_m = anchor - stop_buffer_atr * atr_now     # mirrored space
        stop = stop_m * sgn
        confs = [f"{side} impulse ({g['impulse_type']}, {g['impulse_bars_ago']} bars ago)",
                 f"{g['pullback_bars']}-bar pullback, depth {g['pullback_depth_atr']:.2f} ATR",
                 "trigger took pullback swing"]
        score = 50
        st = float(last.get("supertrend_dir", 0) or 0)
        if st == sgn:
            score += 10; confs.append("supertrend agrees")
        if (ee8[-1] > ee21[-1] > ee50[-1]):
            score += 10; confs.append("EMA 8>21>50 stack")
        rp = float(last.get("rsi_pct", np.nan)) if "rsi_pct" in df.columns else np.nan
        if not np.isnan(rp):
            if (sgn > 0 and rp >= 0.5) or (sgn < 0 and rp <= 0.5):
                score += 5; confs.append(f"RSI percentile {rp:.2f}")
        if rv >= 1.5:
            score += 10; confs.append(f"volume {rv:.1f}x time-of-day median")
        if g["impulse_type"] == "breakout":
            score += 5
        return FamilySetup(
            family_id=FAMILY_ID, side=side, trigger_close=float(c[-1]), stop=float(stop),
            atr=atr_now, impulse_type=g["impulse_type"], impulse_bars_ago=g["impulse_bars_ago"],
            pullback_bars=g["pullback_bars"], pullback_depth_atr=g["pullback_depth_atr"],
            swing_level=float(g["swing"] * sgn), score=min(score, 100), confirmations=confs,
        )
    return None
