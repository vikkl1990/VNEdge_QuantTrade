#!/usr/bin/env python3
"""
orderflow_lab.py — does real order flow condition forward returns?

From storage/research/orderflow/{SYM}_1m.csv.gz (scripts/orderflow_aggregate.py):
resample to 15m and 1h closed bars and build flow features on data <= t:

  delta_ratio    (buy_vol - sell_vol) / vol                      aggressor imbalance
  delta_z        rolling z-score of per-bar delta (96 bars 15m / 48 bars 1h)
  cvd_div_N      sign(price return over N bars) vs sign(sum delta over N bars) — real CVD divergence
  big_imb        (big_buy - big_sell) / (big_buy + big_sell)     large-trade imbalance
  big_share      (big_buy + big_sell) / vol                      how much of the tape is big prints
  n_buy_share    n_buy / n                                       aggressor count share
  intensity      n / same-hour median n (30 occurrences)         activity vs normal
  avg_size_rel   (vol / n) / rolling median                      typical print size vs normal
  absorption     sell-heavy bar (delta_z < -1) that closes >= open, or buy-heavy that closes <= open
  vwap_dev       (close - vwap) / ATR                            where price sits vs the bar's VWAP

Targets: forward close-to-close return, 15m: 1h / 4h; 1h: 4h / 12h / 24h.
Screen (pre-registered, same as edge_map_lab): bins from the FIT period only;
stable = same sign in >= 4/5 judge folds and in fit; candidate = stable AND
|judge mean| > 2 x 0.118% round trip, on BOTH symbols.

  python scripts/orderflow_lab.py --symbol ETHUSD
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FLOW_DIR = PROJECT_ROOT / "storage" / "research" / "orderflow"
FEE_RT = 0.118
HORIZONS = {"15m": {"f1h": 4, "f4h": 16}, "1h": {"f4h": 4, "f12h": 12, "f24h": 24}}
ZWIN = {"15m": 96, "1h": 48}


def load_1m(sym: str) -> pd.DataFrame:
    d = pd.read_csv(FLOW_DIR / f"{sym}_1m.csv.gz", index_col=0, parse_dates=True)
    d.index = d.index.tz_localize("UTC")
    return d


def resample(d: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = {"15m": "15min", "1h": "1h"}[tf]
    g = d.resample(rule, label="left", closed="left")
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
        "vol": g["vol"].sum(), "buy_vol": g["buy_vol"].sum(), "sell_vol": g["sell_vol"].sum(),
        "n": g["n"].sum(), "n_buy": g["n_buy"].sum(), "n_sell": g["n_sell"].sum(),
        "big_buy": g["big_buy_vol"].sum(), "big_sell": g["big_sell_vol"].sum(),
        "pv": (d["vwap"] * d["vol"]).resample(rule, label="left", closed="left").sum(),
        "minutes": g["n"].count(),
    })
    out = out[out["minutes"] > 0].copy()
    out["vwap"] = out["pv"] / out["vol"].replace(0, np.nan)
    return out.drop(columns=["pv"])


def features(p: pd.DataFrame, tf: str) -> pd.DataFrame:
    p = p.copy()
    c = p["close"]
    z = ZWIN[tf]
    tr = pd.concat([p["high"] - p["low"], (p["high"] - c.shift()).abs(), (p["low"] - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    p["ret1"] = c.pct_change() * 100
    p["delta"] = p["buy_vol"] - p["sell_vol"]
    p["delta_ratio"] = p["delta"] / p["vol"].replace(0, np.nan)
    p["delta_z"] = (p["delta"] - p["delta"].rolling(z).mean()) / p["delta"].rolling(z).std()
    for N in (4, 12):
        pr = c.pct_change(N)
        dsum = p["delta"].rolling(N).sum()
        # +1 = bullish divergence (price down, flow up), -1 = bearish, 0 = agreement
        p[f"cvd_div_{N}"] = np.where((pr < 0) & (dsum > 0), 1, np.where((pr > 0) & (dsum < 0), -1, 0))
    p["big_imb"] = (p["big_buy"] - p["big_sell"]) / (p["big_buy"] + p["big_sell"]).replace(0, np.nan)
    p["big_share"] = (p["big_buy"] + p["big_sell"]) / p["vol"].replace(0, np.nan)
    p["n_buy_share"] = p["n_buy"] / p["n"].replace(0, np.nan)
    hour = p.index.hour
    med_n = p.groupby(hour)["n"].transform(lambda s: s.shift(1).rolling(30, min_periods=10).median())
    p["intensity"] = p["n"] / med_n
    avg_size = p["vol"] / p["n"].replace(0, np.nan)
    p["avg_size_rel"] = avg_size / avg_size.rolling(z).median()
    p["absorption"] = np.where((p["delta_z"] < -1) & (c >= p["open"]), 1,
                               np.where((p["delta_z"] > 1) & (c <= p["open"]), -1, 0))
    p["vwap_dev"] = (c - p["vwap"]) / atr
    for k, h in HORIZONS[tf].items():
        p[k] = (c.shift(-h) / c - 1) * 100
    return p


BINS = {
    "delta_ratio": ("q", 5), "delta_z": ("q", 5), "big_imb": ("q", 5), "big_share": ("q", 5),
    "n_buy_share": ("q", 5), "intensity": ("q", 5), "avg_size_rel": ("q", 5), "vwap_dev": ("q", 5),
    "cvd_div_4": ("cat", None), "cvd_div_12": ("cat", None), "absorption": ("cat", None),
}


def screen(p: pd.DataFrame, tf: str, fit_days: int, judge_days: int, label: str) -> pd.DataFrame:
    t = p.index
    t0, t1 = t.min(), t.max()
    fit_end = t0 + pd.Timedelta(days=fit_days)
    edges = []
    s = fit_end
    while s + pd.Timedelta(days=judge_days) <= t1 + pd.Timedelta(days=1):
        edges.append((s, s + pd.Timedelta(days=judge_days))); s += pd.Timedelta(days=judge_days)
    fit = p[t < fit_end]
    print(f"\n===== ORDER FLOW x forward return — {label} {tf}: {len(p)} bars {t0.date()} -> {t1.date()}, fit {fit_days}d, {len(edges)} folds of {judge_days}d =====")
    rows = []
    for feat, (kind, q) in BINS.items():
        if kind == "cat":
            b = p[feat]
        else:
            qs = fit[feat].dropna().quantile(np.linspace(0, 1, q + 1)).values.astype(float).copy()
            qs[0], qs[-1] = -np.inf, np.inf
            b = pd.cut(p[feat], bins=np.unique(qs), labels=False, include_lowest=True)
        for h in HORIZONS[tf]:
            for bv in pd.unique(b.dropna()):
                g = p[b == bv]
                fg = g[g.index < fit_end][h].dropna()
                if len(fg) < 30:
                    continue
                fm = []
                for a, e in edges:
                    j = g[(g.index >= a) & (g.index < e)][h].dropna()
                    fm.append(j.mean() if len(j) >= 15 else np.nan)
                v = np.array([x for x in fm if not np.isnan(x)])
                if len(v) < 3:
                    continue
                jg = g[g.index >= fit_end][h].dropna()
                agree = int((np.sign(v) == np.sign(fg.mean())).sum())
                rows.append({"feature": feat, "bin": bv, "horizon": h, "n_fit": len(fg), "fit_mean%": fg.mean(),
                             "n_judge": len(jg), "judge_mean%": jg.mean(), "agree": agree, "folds": len(v),
                             "folds_str": f"{agree}/{len(v)}", "net%": abs(jg.mean()) - FEE_RT,
                             "dir": "long" if fg.mean() > 0 else "short",
                             "same_sign": np.sign(jg.mean()) == np.sign(fg.mean())})
    r = pd.DataFrame(rows)
    pd.set_option("display.width", 230, "display.max_rows", 300, "display.float_format", "{:.3f}".format)
    need = 4 if r["folds"].max() >= 5 else max(3, int(r["folds"].max()) - 1)
    stable = r[(r["agree"] >= need) & (r["folds"] >= need) & r["same_sign"]]
    exp_rand = len(r) * (0.5 ** need) * 2 if len(r) else 0
    print(f"stable cells: {len(stable)} of {len(r)} (random expectation ≈ {exp_rand:.0f} at >= {need}/{int(r['folds'].max())} agreement)")
    cols = ["feature", "bin", "horizon", "dir", "n_fit", "fit_mean%", "n_judge", "judge_mean%", "folds_str", "net%"]
    print(stable.sort_values("net%", ascending=False).head(20)[cols].to_string(index=False) if len(stable) else "none")
    cand = stable[stable["net%"] > FEE_RT]
    print(f"CANDIDATES (stable and |judge mean| > 2x fee): {len(cand)}")
    print(cand[cols].to_string(index=False) if len(cand) else "none")
    for h in HORIZONS[tf]:
        jj = p[p.index >= fit_end][h].dropna()
        print(f"   baseline {h}: judge mean {jj.mean():+.3f}%   mean |move| {jj.abs().mean():.3f}%")
    r["tf"] = tf
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ETHUSD")
    ap.add_argument("--fit-days", type=int, default=90)
    ap.add_argument("--judge-days", type=int, default=30)
    args = ap.parse_args()
    d = load_1m(args.symbol)
    print(f"{args.symbol}: {len(d)} minutes, {d.index.min()} -> {d.index.max()}, {int(d['n'].sum()):,} trades")
    out = []
    for tf in ("15m", "1h"):
        p = features(resample(d, tf), tf)
        out.append(screen(p, tf, args.fit_days, args.judge_days, args.symbol))
    pd.concat(out).to_csv(FLOW_DIR / f"{args.symbol}_flow_edge_map.csv", index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
