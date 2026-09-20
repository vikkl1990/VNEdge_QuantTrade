"""
Naked market structure — price only, no look-ahead.

Swings come from an ATR ZigZag: a running extreme becomes a confirmed swing only
when price has reversed k*ATR14 from it. Every swing carries both the bar where it
printed (`idx`) and the bar where it became knowable (`confirmed_idx`); everything
downstream (labels, state, BOS/CHoCH, naked levels, ranges) uses only swings whose
`confirmed_idx` is at or before the bar being evaluated.

Research module (2026-09-19). Not imported by the live bot. See
docs/research/NAKED_STRUCTURE_PREREG_20260919.md for the definitions and their rationale.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd


@dataclass
class Swing:
    idx: int                 # bar where the extreme printed
    price: float
    kind: str                # "H" or "L"
    confirmed_idx: int       # bar at whose close the swing became knowable
    label: Optional[str] = None   # HH / LH for highs, HL / LL for lows; None for the first of a kind
    broken_idx: Optional[int] = None    # bar whose close first exceeded the swing (BOS/CHoCH/RB)
    touched_idx: Optional[int] = None   # bar whose range first reached the swing after confirmation


def atr_wilder(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.full(n, np.nan)
    if n < period:
        return atr
    atr[period - 1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def zigzag_swings(high: np.ndarray, low: np.ndarray, atr: np.ndarray, k: float = 2.0) -> List[Swing]:
    """ATR-reversal ZigZag. Returns swings in confirmation order (== print order)."""
    n = len(high)
    swings: List[Swing] = []
    direction = 0                    # +1 rising leg (tracking a candidate high), -1 falling leg
    ch_i, ch_p = -1, -np.inf         # candidate high
    cl_i, cl_p = -1, np.inf          # candidate low
    for i in range(n):
        a = atr[i]
        if np.isnan(a) or a <= 0:
            continue
        if ch_i < 0:                 # first valid bar seeds both candidates
            ch_i, ch_p, cl_i, cl_p = i, high[i], i, low[i]
            continue
        if direction == 0:
            if high[i] > ch_p:
                ch_i, ch_p = i, high[i]
            if low[i] < cl_p:
                cl_i, cl_p = i, low[i]
            if high[i] >= cl_p + k * a and cl_i < i:
                swings.append(Swing(cl_i, cl_p, "L", i))
                direction, ch_i, ch_p = 1, i, high[i]
                # the leg up from cl may already include a higher bar than i; running max handles it
                for j in range(cl_i + 1, i + 1):
                    if high[j] > ch_p:
                        ch_i, ch_p = j, high[j]
            elif low[i] <= ch_p - k * a and ch_i < i:
                swings.append(Swing(ch_i, ch_p, "H", i))
                direction, cl_i, cl_p = -1, i, low[i]
                for j in range(ch_i + 1, i + 1):
                    if low[j] < cl_p:
                        cl_i, cl_p = j, low[j]
        elif direction == 1:
            if high[i] > ch_p:
                ch_i, ch_p = i, high[i]
            if low[i] <= ch_p - k * a and ch_i < i:
                swings.append(Swing(ch_i, ch_p, "H", i))
                direction = -1
                cl_i, cl_p = ch_i + 1, low[ch_i + 1]
                for j in range(ch_i + 1, i + 1):
                    if low[j] < cl_p:
                        cl_i, cl_p = j, low[j]
        else:
            if low[i] < cl_p:
                cl_i, cl_p = i, low[i]
            if high[i] >= cl_p + k * a and cl_i < i:
                swings.append(Swing(cl_i, cl_p, "L", i))
                direction = 1
                ch_i, ch_p = cl_i + 1, high[cl_i + 1]
                for j in range(cl_i + 1, i + 1):
                    if high[j] > ch_p:
                        ch_i, ch_p = j, high[j]
    # labels vs previous confirmed swing of the same kind
    last = {"H": None, "L": None}
    for s in swings:
        p = last[s.kind]
        if p is not None:
            if s.kind == "H":
                s.label = "HH" if s.price > p.price else "LH"
            else:
                s.label = "HL" if s.price > p.price else "LL"
        last[s.kind] = s
    return swings


def build_structure(df: pd.DataFrame, k: float = 2.0, atr_period: int = 14) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-bar structure frame + swing table. All per-bar values are knowable at that bar's close."""
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    n = len(df)
    atr = atr_wilder(high, low, close, atr_period)
    swings = zigzag_swings(high, low, atr, k)
    by_conf: dict[int, List[Swing]] = {}
    for s in swings:
        by_conf.setdefault(s.confirmed_idx, []).append(s)

    state = np.array(["none"] * n, dtype=object)
    event = np.array([""] * n, dtype=object)
    event_dist = np.full(n, np.nan)
    event_age = np.full(n, np.nan)
    touch = np.array([""] * n, dtype=object)
    touch_age = np.full(n, np.nan)
    touch_dist = np.full(n, np.nan)
    nk_hi_d = np.full(n, np.nan); nk_hi_age = np.full(n, np.nan)
    nk_lo_d = np.full(n, np.nan); nk_lo_age = np.full(n, np.nan)
    rng_hi = np.full(n, np.nan); rng_lo = np.full(n, np.nan)
    pos_in_range = np.full(n, np.nan); bars_in_range = np.full(n, np.nan)
    n_swings = np.zeros(n, dtype=int)

    last_h: Optional[Swing] = None   # last confirmed high (for labels / range)
    last_l: Optional[Swing] = None
    live_h: Optional[Swing] = None   # last confirmed, unbroken high (break target)
    live_l: Optional[Swing] = None
    naked_h: List[Swing] = []
    naked_l: List[Swing] = []
    cur_state = "none"
    range_since = -1
    count = 0

    for i in range(n):
        # 1) swings confirmed at this bar's close become known
        for s in by_conf.get(i, ()):
            count += 1
            if s.kind == "H":
                last_h, live_h = s, s
                naked_h.append(s)
            else:
                last_l, live_l = s, s
                naked_l.append(s)
            if last_h is not None and last_l is not None and last_h.label and last_l.label:
                if last_h.label == "HH" and last_l.label == "HL":
                    new = "up"
                elif last_h.label == "LH" and last_l.label == "LL":
                    new = "down"
                else:
                    new = "range"
            else:
                new = "none"
            if new != cur_state:
                cur_state = new
                range_since = i if new == "range" else -1
        n_swings[i] = count
        state[i] = cur_state
        if np.isnan(atr[i]) or atr[i] <= 0:
            continue
        a = atr[i]

        # 2) breaks of the live (confirmed, unbroken) swings, evaluated on close
        if live_h is not None and live_h.confirmed_idx < i and close[i] > live_h.price:
            tag = {"up": "BOS_up", "down": "CHoCH_up", "range": "RB_up"}.get(cur_state, "")
            if tag:
                event[i] = tag
                event_dist[i] = (close[i] - live_h.price) / a
                event_age[i] = i - live_h.idx
            live_h.broken_idx = i
            live_h = None
        elif live_l is not None and live_l.confirmed_idx < i and close[i] < live_l.price:
            tag = {"up": "CHoCH_down", "down": "BOS_down", "range": "RB_down"}.get(cur_state, "")
            if tag:
                event[i] = tag
                event_dist[i] = (live_l.price - close[i]) / a
                event_age[i] = i - live_l.idx
            live_l.broken_idx = i
            live_l = None

        # 3) naked levels: first touch after confirmation consumes the level
        hit_h = [s for s in naked_h if s.confirmed_idx < i and high[i] >= s.price]
        hit_l = [s for s in naked_l if s.confirmed_idx < i and low[i] <= s.price]
        if hit_h:
            s = min(hit_h, key=lambda x: x.price)          # nearest one reached
            touch[i] = "high_" + ("through" if close[i] >= s.price else "reject")
            touch_age[i] = i - s.confirmed_idx
            touch_dist[i] = (s.price - close[i]) / a
            for x in hit_h:
                x.touched_idx = i
            naked_h = [x for x in naked_h if x.touched_idx is None]
        elif hit_l:
            s = max(hit_l, key=lambda x: x.price)
            touch[i] = "low_" + ("through" if close[i] <= s.price else "reject")
            touch_age[i] = i - s.confirmed_idx
            touch_dist[i] = (close[i] - s.price) / a
            for x in hit_l:
                x.touched_idx = i
            naked_l = [x for x in naked_l if x.touched_idx is None]
        above = [s for s in naked_h if s.price > close[i]]
        below = [s for s in naked_l if s.price < close[i]]
        if above:
            s = min(above, key=lambda x: x.price)
            nk_hi_d[i] = (s.price - close[i]) / a; nk_hi_age[i] = i - s.confirmed_idx
        if below:
            s = max(below, key=lambda x: x.price)
            nk_lo_d[i] = (close[i] - s.price) / a; nk_lo_age[i] = i - s.confirmed_idx

        # 4) range geometry
        if cur_state == "range" and last_h is not None and last_l is not None and last_h.price > last_l.price:
            rng_hi[i], rng_lo[i] = last_h.price, last_l.price
            pos_in_range[i] = (close[i] - last_l.price) / (last_h.price - last_l.price)
            bars_in_range[i] = i - range_since

    out = pd.DataFrame({
        "atr": atr, "n_swings": n_swings, "state": state,
        "event": event, "event_dist_atr": event_dist, "event_swing_age": event_age,
        "touch": touch, "touch_level_age": touch_age, "touch_dist_atr": touch_dist,
        "naked_high_dist_atr": nk_hi_d, "naked_high_age": nk_hi_age,
        "naked_low_dist_atr": nk_lo_d, "naked_low_age": nk_lo_age,
        "range_hi": rng_hi, "range_lo": rng_lo, "pos_in_range": pos_in_range, "bars_in_range": bars_in_range,
    }, index=df.index)
    sw = pd.DataFrame([{
        "idx": s.idx, "price": s.price, "kind": s.kind, "confirmed_idx": s.confirmed_idx,
        "lag": s.confirmed_idx - s.idx, "label": s.label, "broken_idx": s.broken_idx, "touched_idx": s.touched_idx,
    } for s in swings])
    return out, sw
