#!/usr/bin/env python3
"""
structure_lab.py — does naked market structure carry a forward-return edge?

Builds data.market_structure on 500 days of 1h and 4h (cached Delta candles), then
tests the pre-registered hypotheses in docs/research/NAKED_STRUCTURE_PREREG_20260919.md:
H0 state, H1 CHoCH, H2 BOS, H3 naked touch-reject, H3b touch-through, H4 BOS distance
terciles, H5 range edges, H6 range break. Fit 120d / 6 judge folds of 60d; stable =
same sign as fit in >= 5/6 folds; candidate = stable on BOTH symbols with |judge mean|
> 2x the 0.118% taker round trip. A random-placement null is printed per cell.

  python scripts/structure_lab.py            # both symbols, 1h and 4h, k=2.0
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location("scanner_lab", Path(__file__).resolve().parent / "scanner_lab.py")
lab = importlib.util.module_from_spec(_spec)
sys.modules["scanner_lab"] = lab
_spec.loader.exec_module(lab)
from data.market_structure import build_structure  # noqa: E402

FEE_RT = 0.118
HORIZONS = {"1h": {"f4h": 4, "f12h": 12, "f24h": 24}, "4h": {"f4h": 1, "f12h": 3, "f24h": 6}}
RNG = np.random.default_rng(20260919)


def prepare(sym: str, tf: str, days: int, k: float) -> pd.DataFrame:
    df = lab.load_candles(sym, tf, days, False).reset_index(drop=True)
    st, sw = build_structure(df, k=k)
    p = pd.concat([df, st], axis=1)
    p["t"] = pd.to_datetime(p["timestamp"], unit="ms", utc=True)
    c = p["close"].astype(float)
    for h, nb in HORIZONS[tf].items():
        p[h] = (c.shift(-nb) / c - 1) * 100
    p.attrs["swings"] = sw
    return p


def sanity(p: pd.DataFrame, sym: str, tf: str):
    sw = p.attrs["swings"]
    legs = sw["price"].diff().abs() / p["atr"].reindex(sw["confirmed_idx"]).values
    print(f"\n--- {sym} {tf}: {len(p)} bars, {len(sw)} confirmed swings, median spacing {sw['idx'].diff().median():.0f} bars, "
          f"median confirm lag {sw['lag'].median():.0f} bars, median leg {np.nanmedian(legs):.1f} ATR")
    occ = p["state"].value_counts(normalize=True).mul(100).round(1).to_dict()
    ev = p["event"].value_counts().to_dict(); tc = p["touch"].value_counts().to_dict()
    ev.pop("", None); tc.pop("", None)
    print(f"    state occupancy % {occ}\n    events {ev}\n    touches {tc}")


def hypotheses(p: pd.DataFrame, fit_end) -> dict[str, tuple[pd.Series, pd.Series]]:
    """name -> (mask, direction) with direction +1 long / -1 short. Bins from FIT only."""
    fit = p[p["t"] < fit_end]
    H = {}
    H["H0_state"] = (p["state"].isin(["up", "down"]), p["state"].map({"up": 1, "down": -1}).fillna(0))
    ev = p["event"]
    H["H1_choch"] = (ev.isin(["CHoCH_up", "CHoCH_down"]), ev.map({"CHoCH_up": 1, "CHoCH_down": -1}).fillna(0))
    H["H2_bos"] = (ev.isin(["BOS_up", "BOS_down"]), ev.map({"BOS_up": 1, "BOS_down": -1}).fillna(0))
    H["H6_range_break"] = (ev.isin(["RB_up", "RB_down"]), ev.map({"RB_up": 1, "RB_down": -1}).fillna(0))
    tc = p["touch"]
    rej_dir = tc.map({"high_reject": -1, "low_reject": 1}).fillna(0)
    H["H3_touch_reject"] = (tc.isin(["high_reject", "low_reject"]), rej_dir)
    med_age = fit.loc[fit["touch"].isin(["high_reject", "low_reject"]), "touch_level_age"].median()
    H["H3_touch_reject_old"] = (H["H3_touch_reject"][0] & (p["touch_level_age"] > med_age), rej_dir)
    H["H3_touch_reject_young"] = (H["H3_touch_reject"][0] & (p["touch_level_age"] <= med_age), rej_dir)
    H["H3b_touch_through"] = (tc.isin(["high_through", "low_through"]), tc.map({"high_through": 1, "low_through": -1}).fillna(0))
    bos_fit = fit.loc[fit["event"].isin(["BOS_up", "BOS_down"]), "event_dist_atr"]
    if len(bos_fit) >= 9:
        q1, q2 = bos_fit.quantile([1 / 3, 2 / 3])
        d = H["H2_bos"][1]; m = H["H2_bos"][0]
        H["H4_bos_dist_small"] = (m & (p["event_dist_atr"] <= q1), d)
        H["H4_bos_dist_mid"] = (m & (p["event_dist_atr"] > q1) & (p["event_dist_atr"] <= q2), d)
        H["H4_bos_dist_large"] = (m & (p["event_dist_atr"] > q2), d)
    rng = p["state"].eq("range") & p["pos_in_range"].notna()
    H["H5_range_edge"] = (rng & ((p["pos_in_range"] < 0.2) | (p["pos_in_range"] > 0.8)),
                          np.where(p["pos_in_range"] < 0.2, 1, np.where(p["pos_in_range"] > 0.8, -1, 0)))
    return {k: (m.astype(bool), pd.Series(np.asarray(d, float), index=p.index)) for k, (m, d) in H.items()}


def independent(idx: np.ndarray, gap: int) -> int:
    n, last = 0, -10 ** 9
    for i in idx:
        if i - last >= gap:
            n += 1; last = i
    return n


def judge(p: pd.DataFrame, mask, direction, h: str, nb: int, fit_end, edges, n_null=300):
    y = p[h] * direction
    sel = mask & y.notna() & (direction != 0)
    fitv = y[sel & (p["t"] < fit_end)]
    if len(fitv) < 20:
        return None
    fit_mean = fitv.mean(); sign = np.sign(fit_mean)
    fm = []
    for a, e in edges:
        v = y[sel & (p["t"] >= a) & (p["t"] < e)]
        fm.append(v.mean() if len(v) >= 8 else np.nan)
    fm = np.array(fm); v = fm[~np.isnan(fm)]
    if len(v) < 4:
        return None
    agree = int((np.sign(v) == sign).sum())
    jv = y[sel & (p["t"] >= fit_end)]
    j_idx = np.flatnonzero((sel & (p["t"] >= fit_end)).values)
    # null: same count of judge bars, same direction labels, random placement in the judge period
    judge_pool = np.flatnonzero(((p["t"] >= fit_end) & p[h].notna()).values)
    dirs = direction[sel & (p["t"] >= fit_end)].values
    beat = 0
    fwd = p[h].values; tt = p["t"].values
    for _ in range(n_null):
        pick = RNG.choice(judge_pool, size=len(dirs), replace=False)
        yy = fwd[pick] * dirs
        if yy.mean() * sign < jv.mean() * sign:
            continue
        ag = 0; tp = tt[pick]
        for a, e in edges:
            m = (tp >= np.datetime64(a)) & (tp < np.datetime64(e))
            if m.sum() >= 8 and np.sign(yy[m].mean()) == sign:
                ag += 1
        if ag >= agree:
            beat += 1
    return {"n_fit": len(fitv), "fit%": fit_mean, "n_judge": len(jv), "n_indep": independent(j_idx, nb),
            "judge%": jv.mean(), "win%": (jv > 0).mean() * 100, "folds": f"{agree}/{len(v)}", "agree": agree, "nf": len(v),
            "net%": jv.mean() * sign - FEE_RT, "stable": agree >= 5 and len(v) >= 5 and np.sign(jv.mean()) == sign,
            "p_null": beat / n_null}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTC/USDT,ETH/USDT")
    ap.add_argument("--tfs", default="1h,4h")
    ap.add_argument("--days", type=int, default=500)
    ap.add_argument("--k", type=float, default=2.0)
    ap.add_argument("--fit-days", type=int, default=120)
    ap.add_argument("--judge-days", type=int, default=60)
    args = ap.parse_args()
    pd.set_option("display.width", 250, "display.max_rows", 500, "display.float_format", "{:+.3f}".format)
    out_dir = lab.LAB_DIR / "structure"; out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for tf in args.tfs.split(","):
        for sym in args.symbols.split(","):
            p = prepare(sym, tf, args.days, args.k)
            sanity(p, sym, tf)
            p.drop(columns=["t"]).to_csv(out_dir / f"{sym.replace('/', '_')}_{tf}_structure.csv", index=False)
            t0, t1 = p["t"].min(), p["t"].max()
            fit_end = t0 + pd.Timedelta(days=args.fit_days)
            edges, s = [], fit_end
            while s + pd.Timedelta(days=args.judge_days) <= t1 + pd.Timedelta(days=1):
                edges.append((s, s + pd.Timedelta(days=args.judge_days))); s += pd.Timedelta(days=args.judge_days)
            for name, (mask, direction) in hypotheses(p, fit_end).items():
                for h, nb in HORIZONS[tf].items():
                    r = judge(p, mask, direction, h, nb, fit_end, edges)
                    if r:
                        rows.append({"sym": sym[:3], "tf": tf, "hyp": name, "horizon": h, **r})
    r = pd.DataFrame(rows)
    r.to_csv(out_dir / "structure_hypotheses.csv", index=False)
    cols = ["sym", "tf", "hyp", "horizon", "n_fit", "fit%", "n_judge", "n_indep", "judge%", "win%", "folds", "net%", "p_null", "stable"]
    for tf in args.tfs.split(","):
        print(f"\n===== {tf}: every hypothesis × horizon × symbol (directional forward return, %, gross) =====")
        print(r[r.tf == tf].sort_values(["hyp", "horizon", "sym"])[cols].to_string(index=False))
    st = r[r.stable]
    print(f"\n-- stable cells (fit sign held in >=5/6 judge folds): {len(st)} of {len(r)} --")
    print(st.sort_values("net%", ascending=False)[cols].to_string(index=False) if len(st) else "none")
    both = st.groupby(["tf", "hyp", "horizon"])["sym"].nunique()
    both = both[both == 2].index
    cand = st.set_index(["tf", "hyp", "horizon"]).loc[both].reset_index() if len(both) else st.iloc[0:0]
    cand = cand.groupby(["tf", "hyp", "horizon"]).filter(lambda g: (g["net%"] > FEE_RT).all())
    print(f"\n-- CANDIDATES (stable on BOTH symbols and |judge mean| > 2x fee {FEE_RT:.3f}% on both): {len(cand) // 2} --")
    print(cand.sort_values(["tf", "hyp"])[cols].to_string(index=False) if len(cand) else "none")
    print(f"\nrandom expectation: a cell is 'stable' by chance ~{2 * (6 + 1) / 64 * 100:.0f}% of the time (6 folds, either sign)")


if __name__ == "__main__":
    main()
