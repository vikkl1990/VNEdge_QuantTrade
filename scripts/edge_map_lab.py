#!/usr/bin/env python3
"""
edge_map_lab.py — where is forward return conditioned on structure?

Instead of testing hand-written entries, condition the forward return of
EVERY closed 1h bar on structural features and ask which conditions carry a
stable, fee-clearing expectancy walk-forward. Features (all computed on data
<= t): UTC hour, weekday, funding-window proximity (Delta funds 00/08/16 UTC),
position in the trailing 24h range, past 1h/4h/24h returns, ATR percentile,
volume vs same-hour median, RSI14, and for ETH the cross-asset lead (BTC's
past 1h / 4h return). Targets: forward 4h / 12h / 24h close-to-close return,
reported gross and net of a taker round trip.

Walk-forward: 6 folds of 60 days after a 120-day fit. A feature bin is
"stable" if its net mean has the same sign in >= 5 of 6 judge folds AND that
sign matches the fit period. Only stable bins whose net mean clears the fee
by 2x are printed as candidates.

Also: the classic Asia-range (00-08 UTC) breakout at London on 15m, as a
concrete rule, same folds.

  python scripts/edge_map_lab.py --symbol ETH/USDT
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

FEE_RT = 0.118   # taker in + taker out, % of notional (Scalper Offer ignored at >30-min holds)
HORIZONS = {"f4h": 4, "f12h": 12, "f24h": 24}


def rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()
    return 100 - 100 / (1 + g / l)


def features_1h(df: pd.DataFrame, btc: pd.DataFrame | None) -> pd.DataFrame:
    p = df.copy()
    p["t"] = pd.to_datetime(p["timestamp"], unit="ms", utc=True)
    c = p["close"].astype(float)
    p["hour"] = p["t"].dt.hour
    p["dow"] = p["t"].dt.dayofweek
    p["to_funding"] = (8 - (p["hour"] % 8)) % 8          # hours until next 00/08/16 UTC funding
    p["ret_1h"] = c.pct_change(1) * 100
    p["ret_4h"] = c.pct_change(4) * 100
    p["ret_24h"] = c.pct_change(24) * 100
    tr = pd.concat([p["high"] - p["low"], (p["high"] - c.shift()).abs(), (p["low"] - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    p["atr_pct"] = atr / c * 100
    p["atr_pctile"] = p["atr_pct"].rolling(200, min_periods=50).rank(pct=True)
    hi24, lo24 = p["high"].rolling(24).max().shift(1), p["low"].rolling(24).min().shift(1)
    p["pos_24h"] = (c - lo24) / (hi24 - lo24).replace(0, np.nan)
    p["range_24h_pct"] = (hi24 - lo24) / c * 100
    med = p.groupby("hour")["volume"].transform(lambda s: s.shift(1).rolling(30, min_periods=10).median())
    p["vol_tod"] = p["volume"] / med
    p["rsi"] = rsi(c)
    for k, h in HORIZONS.items():
        p[k] = (c.shift(-h) / c - 1) * 100
    if btc is not None:
        b = btc[["timestamp", "close"]].copy()
        b["btc_ret_1h"] = b["close"].astype(float).pct_change(1) * 100
        b["btc_ret_4h"] = b["close"].astype(float).pct_change(4) * 100
        p = p.merge(b[["timestamp", "btc_ret_1h", "btc_ret_4h"]], on="timestamp", how="left")
    return p


BINS = {
    "hour": ("cat", None), "dow": ("cat", None), "to_funding": ("cat", None),
    "pos_24h": ("q", 5), "ret_1h": ("q", 5), "ret_4h": ("q", 5), "ret_24h": ("q", 5),
    "atr_pctile": ("q", 5), "vol_tod": ("q", 5), "rsi": ("q", 5), "range_24h_pct": ("q", 5),
    "btc_ret_1h": ("q", 5), "btc_ret_4h": ("q", 5),
}


def edge_map(p: pd.DataFrame, fit_days=120, judge_days=60, label=""):
    t0, t1 = p["t"].min(), p["t"].max()
    fit_end = t0 + pd.Timedelta(days=fit_days)
    edges = []
    s = fit_end
    while s + pd.Timedelta(days=judge_days) <= t1 + pd.Timedelta(days=1):
        edges.append((s, s + pd.Timedelta(days=judge_days))); s += pd.Timedelta(days=judge_days)
    fit = p[p["t"] < fit_end]
    print(f"\n===== EDGE MAP {label}: {len(p)} bars {t0.date()} -> {t1.date()}, fit {fit_days}d, {len(edges)} judge folds of {judge_days}d =====")
    rows = []
    for feat, (kind, q) in BINS.items():
        if feat not in p.columns:
            continue
        if kind == "cat":
            p["_bin"] = p[feat]
        else:
            # bin edges from the FIT period only (no look-ahead into judge folds)
            qs = fit[feat].dropna().quantile(np.linspace(0, 1, q + 1)).values.astype(float).copy()
            qs[0], qs[-1] = -np.inf, np.inf
            p["_bin"] = pd.cut(p[feat], bins=np.unique(qs), labels=False, include_lowest=True)
        for h in HORIZONS:
            for b, g in p.groupby("_bin"):
                fitg = g[g["t"] < fit_end][h].dropna()
                if len(fitg) < 30:
                    continue
                fit_mean = fitg.mean()
                fold_means = []
                for a, e in edges:
                    j = g[(g["t"] >= a) & (g["t"] < e)][h].dropna()
                    fold_means.append(j.mean() if len(j) >= 15 else np.nan)
                fm = np.array(fold_means, dtype=float); v = fm[~np.isnan(fm)]
                if len(v) < 4:
                    continue
                sign = np.sign(fit_mean)
                agree = int((np.sign(v) == sign).sum())
                judge_all = g[g["t"] >= fit_end][h].dropna()
                rows.append({"feature": feat, "bin": b, "horizon": h, "n_fit": len(fitg), "fit_mean%": fit_mean,
                             "n_judge": len(judge_all), "judge_mean%": judge_all.mean(),
                             "folds_agree": f"{agree}/{len(v)}", "agree": agree, "folds": len(v),
                             "net_judge%": abs(judge_all.mean()) - FEE_RT, "dir": "long" if sign > 0 else "short"})
    r = pd.DataFrame(rows)
    pd.set_option("display.width", 220, "display.max_rows", 400, "display.float_format", "{:.3f}".format)
    stable = r[(r["agree"] >= 5) & (r["folds"] >= 5) & (np.sign(r["judge_mean%"]) == np.sign(r["fit_mean%"]))]
    cand = stable[stable["net_judge%"] > FEE_RT]      # clears the round trip by 2x
    print(f"\n-- stable bins (same sign in >=5/6 judge folds and in fit): {len(stable)} of {len(r)} feature-bin-horizon cells --")
    print(stable.sort_values("net_judge%", ascending=False).head(25)[["feature", "bin", "horizon", "dir", "n_fit", "fit_mean%", "n_judge", "judge_mean%", "folds_agree", "net_judge%"]].to_string(index=False) if len(stable) else "none")
    print(f"\n-- CANDIDATES (stable AND |judge mean| > 2x round-trip fee {FEE_RT:.3f}%): {len(cand)} --")
    print(cand.sort_values("net_judge%", ascending=False)[["feature", "bin", "horizon", "dir", "n_fit", "fit_mean%", "n_judge", "judge_mean%", "folds_agree", "net_judge%"]].to_string(index=False) if len(cand) else "none")
    # unconditional baseline for scale
    for h in HORIZONS:
        jj = p[p["t"] >= fit_end][h].dropna()
        print(f"   baseline {h}: judge mean {jj.mean():+.3f}%  |mean| of abs move {jj.abs().mean():.3f}%  n={len(jj)}")
    return r


def asia_range_breakout(df15: pd.DataFrame, symbol: str, fit_days=120, judge_days=60, notional=30000.0):
    """Asia range = 00:00-08:00 UTC high/low. First 15m close beyond it during
    08:00-16:00 UTC -> enter next open; stop = range midpoint; exit at 16:00 UTC
    close or stop. One trade per day per side-direction (first break only)."""
    from execution.fees import FeeLeg
    fm, _ = lab._fee_model_from_settings()
    p = df15.copy()
    p["t"] = pd.to_datetime(p["timestamp"], unit="ms", utc=True)
    p["date"] = p["t"].dt.date
    p["hour"] = p["t"].dt.hour
    rows = []
    for d, g in p.groupby("date"):
        asia = g[(g["hour"] >= 0) & (g["hour"] < 8)]
        lon = g[(g["hour"] >= 8) & (g["hour"] < 16)].reset_index(drop=True)
        if len(asia) < 28 or len(lon) < 28:
            continue
        hi, lo = asia["high"].max(), asia["low"].min()
        mid = (hi + lo) / 2
        rng_pct = (hi - lo) / mid * 100
        traded = False
        for k in range(len(lon) - 1):
            c = lon.loc[k, "close"]
            side = "long" if c > hi else ("short" if c < lo else None)
            if side is None or traded:
                continue
            traded = True
            sgn = 1.0 if side == "long" else -1.0
            entry = lon.loc[k + 1, "open"]
            stop = mid
            exit_px, reason, xi = lon.loc[len(lon) - 1, "close"], "session_end", len(lon) - 1
            for j in range(k + 1, len(lon)):
                if (sgn > 0 and lon.loc[j, "low"] <= stop) or (sgn < 0 and lon.loc[j, "high"] >= stop):
                    exit_px, reason, xi = stop, "stop", j; break
            hold = (xi - (k + 1) + 1) * 900
            gross = sgn * (exit_px - entry) / entry * 100
            fee = fm.trade_fees(entry, "taker", [FeeLeg(1.0, exit_px, "taker", elapsed_sec=hold)], symbol=symbol).total_pct
            rows.append({"date": d, "side": side, "range_pct": rng_pct, "gross%": gross, "net%": gross - fee,
                         "net_usd": notional * (gross - fee) / 100, "reason": reason, "t": lon.loc[k + 1, "t"]})
    t = pd.DataFrame(rows)
    if t.empty:
        print("asia breakout: no trades"); return
    t0 = t["t"].min(); fit_end = t0 + pd.Timedelta(days=fit_days); t1 = t["t"].max()
    edges = []; s = fit_end
    while s + pd.Timedelta(days=judge_days) <= t1 + pd.Timedelta(days=1):
        edges.append((s, s + pd.Timedelta(days=judge_days))); s += pd.Timedelta(days=judge_days)
    print(f"\n===== ASIA-RANGE BREAKOUT @ LONDON, {symbol} 15m: {len(t)} trades, {t0.date()} -> {t1.date()} =====")
    for name, sub in (("all", t), ("long", t[t.side == "long"]), ("short", t[t.side == "short"]),
                      ("range<0.8%", t[t.range_pct < 0.8]), ("range>=0.8%", t[t.range_pct >= 0.8])):
        if len(sub) < 20:
            continue
        i = sub[sub["t"] < fit_end]["net_usd"]; o = sub[sub["t"] >= fit_end]["net_usd"]
        fa = [sub[(sub["t"] >= a) & (sub["t"] < e)]["net_usd"].mean() for a, e in edges]
        fa = np.array([x for x in fa if not np.isnan(x)])
        print(f"  {name:12s} n={len(sub):4d} win={(sub['net%'] > 0).mean() * 100:5.1f}%  fit avg$={i.mean() if len(i) else float('nan'):+7.1f}  "
              f"judge avg$={o.mean() if len(o) else float('nan'):+7.1f} (n={len(o)})  folds {' '.join('+' if x > 0 else '-' for x in fa)}  stop%={(sub.reason == 'stop').mean() * 100:.0f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ETH/USDT")
    ap.add_argument("--days", type=int, default=500)
    args = ap.parse_args()
    df1h = lab.load_candles(args.symbol, "1h", args.days, False)
    btc = lab.load_candles("BTC/USDT", "1h", args.days, False) if args.symbol != "BTC/USDT" else None
    p = features_1h(df1h, btc)
    r = edge_map(p, label=f"{args.symbol} 1h")
    out = lab.LAB_DIR / "duration" / f"{args.symbol.replace('/', '_')}_edge_map.csv"
    r.to_csv(out, index=False)
    df15 = lab.load_candles(args.symbol, "15m", args.days, False)
    asia_range_breakout(df15, args.symbol)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
