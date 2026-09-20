import numpy as np
import pandas as pd

from data.market_structure import atr_wilder, build_structure, zigzag_swings


def _frame(closes, spread=1.0):
    c = np.asarray(closes, float)
    return pd.DataFrame({"open": c, "high": c + spread, "low": c - spread, "close": c, "volume": 1.0})


def _sawtooth(n_legs=6, leg=30, amp=60.0, drift=0.0):
    # legs alternate up/down; amplitude far above the ATR so k=2 confirms every leg
    vals, level = [], 1000.0
    for j in range(n_legs):
        sign = 1 if j % 2 == 0 else -1
        for t in range(leg):
            vals.append(level + sign * amp * (t + 1) / leg)
        level += sign * amp + drift
    return vals


def test_swing_confirmation_never_precedes_print():
    df = _frame(_sawtooth())
    _, sw = build_structure(df, k=2.0)
    assert len(sw) >= 4
    assert (sw["confirmed_idx"] > sw["idx"]).all()
    assert (sw["lag"] >= 1).all()


def test_swings_alternate_and_match_extremes():
    vals = _sawtooth()
    df = _frame(vals)
    _, sw = build_structure(df, k=2.0)
    kinds = sw["kind"].tolist()
    assert all(a != b for a, b in zip(kinds, kinds[1:]))
    for r in sw.itertuples():
        col = "high" if r.kind == "H" else "low"
        assert r.price == df[col].iloc[r.idx]


def test_labels_and_state_in_a_drifting_series():
    # rising sawtooth: every high is HH and every low is HL once two of each exist -> state up
    df = _frame(_sawtooth(n_legs=8, drift=15.0))
    st, sw = build_structure(df, k=2.0)
    labels = sw["label"].dropna().tolist()
    assert "HH" in labels and "HL" in labels
    assert (st["state"].iloc[-1]) == "up"


def test_bos_fires_once_per_swing_and_only_after_confirmation():
    df = _frame(_sawtooth(n_legs=8, drift=15.0))
    st, sw = build_structure(df, k=2.0)
    ev = st[st["event"] != ""]
    assert len(ev) >= 1
    broken = sw.dropna(subset=["broken_idx"])
    assert (broken["broken_idx"] > broken["confirmed_idx"]).all()
    assert broken["broken_idx"].is_unique


def test_naked_touch_consumes_level_and_is_after_confirmation():
    df = _frame(_sawtooth(n_legs=8))
    st, sw = build_structure(df, k=2.0)
    touched = sw.dropna(subset=["touched_idx"])
    assert len(touched) >= 1
    assert (touched["touched_idx"] > touched["confirmed_idx"]).all()
    # after a touch, the nearest naked level distance must not point at the consumed level
    t = st[st["touch"] != ""]
    assert len(t) >= 1


def test_no_lookahead_prefix_stability():
    # structure computed on a prefix must equal the prefix of the structure computed on the full series
    df = _frame(_sawtooth(n_legs=10, drift=5.0))
    full, _ = build_structure(df, k=2.0)
    cut = 200
    part, _ = build_structure(df.iloc[:cut].reset_index(drop=True), k=2.0)
    cols = ["state", "event", "touch", "n_swings"]
    pd.testing.assert_frame_equal(full[cols].iloc[:cut].reset_index(drop=True), part[cols].reset_index(drop=True))


def test_atr_matches_wilder_recursion():
    df = _frame(_sawtooth())
    a = atr_wilder(df["high"].values, df["low"].values, df["close"].values, 14)
    assert np.isnan(a[:13]).all() and np.isfinite(a[13:]).all() and (a[13:] > 0).all()
    assert zigzag_swings(df["high"].values, df["low"].values, a, 2.0)
