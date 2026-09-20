#!/usr/bin/env python3
"""
duration_lab.py — the time axis for scanner-lab / family-lab trades.

Rebuilds, per trade, from the fill bar (bar 1) over the same 48-bar window
and R units the labs used:

  T_MAE   bars to max adverse excursion        T_MFE   bars to max favourable excursion
  T_1R    bars until +1R first touched (inf)   T_stop  bars until -1R first touched (inf)
  T_exit  bars to actual flat (headline exit)  ratio   T_MFE / T_exit
  mfe_per_bar = MFE / max(T_MFE, 1)

Reports percentiles (p10/p25/p50/p75/p90), never means, split by TF, scanner,
side, regime, UTC hour bucket, exit reason; winners vs losers T_exit; T_MFE
conditional on ever reaching 1R. Applies the pre-registered decision rules
per TF. Writes per-trade and percentile CSVs under
storage/research/scanner_lab/duration/.

  python scripts/duration_lab.py --symbol BTC/USDT                 # 21-scanner dump
  python scripts/duration_lab.py --symbol BTC/USDT --family trend_pb --variant-tag _imp0.7_nogate
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location("scanner_lab", Path(__file__).resolve().parent / "scanner_lab.py")
lab = importlib.util.module_from_spec(_spec)
sys.modules["scanner_lab"] = lab
_spec.loader.exec_module(lab)

H = 48
PCTS = [0.10, 0.25, 0.50, 0.75, 0.90]
OUT_DIR = lab.LAB_DIR / "duration"


def clocks_for(df_trades: pd.DataFrame, symbol: str, tf: str, exit_hold_col: str, net_col: str,
               reason_col: str) -> pd.DataFrame:
    cand = lab.load_candles(symbol, tf, 200, False)
    idx = {str(t): i for i, t in enumerate(cand["datetime"].astype(str).values)}
    highs = cand["high"].values.astype(float)
    lows = cand["low"].values.astype(float)
    closes = cand["close"].values.astype(float)
    n = len(cand)
    rows = []
    for _, t in df_trades.iterrows():
        e = idx.get(str(t["entry_time"]))
        if e is None:
            continue
        entry = float(t["entry"])
        risk = entry * float(t["risk_pct"]) / 100.0
        if risk <= 0:
            continue
        sgn = 1.0 if t["side"] == "long" else -1.0
        last = min(n - 1, e + H - 1)
        h, l, c = highs[e:last + 1], lows[e:last + 1], closes[e:last + 1]
        fav = sgn * ((h if sgn > 0 else l) - entry) / risk
        adv = -sgn * ((l if sgn > 0 else h) - entry) / risk
        t_mfe = int(np.argmax(fav)) + 1
        t_mae = int(np.argmax(adv)) + 1
        hit1 = np.nonzero(fav >= 1.0)[0]
        hits = np.nonzero(adv >= 1.0)[0]
        t_1r = int(hit1[0]) + 1 if len(hit1) else np.inf
        t_stop = int(hits[0]) + 1 if len(hits) else np.inf
        t_exit = int(t[exit_hold_col])
        mfe, mae = float(fav.max()), float(adv.max())
        # MFE realised within the trade's own life
        mfe_in_life = float(fav[:max(1, min(t_exit, len(fav)))].max())
        rows.append({
            "tf": tf, "scanner": t.get("scanner", t.get("family", "?")), "side": t["side"],
            "regime": t["regime"], "hour_b": f"{(int(t['hour']) // 6) * 6:02d}-{(int(t['hour']) // 6) * 6 + 6:02d}",
            "entry_time": t["entry_time"], "reason": t[reason_col], "net_r": float(t[net_col]),
            "winner": float(t[net_col]) > 0,
            "T_MAE": t_mae, "T_MFE": t_mfe, "T_1R": t_1r, "T_stop": t_stop, "T_exit": t_exit,
            "ratio_TMFE_Texit": t_mfe / max(t_exit, 1), "MFE": mfe, "MAE": mae,
            "MFE_in_life": mfe_in_life, "mfe_per_bar": mfe / max(t_mfe, 1),
            "reached_1R": bool(len(hit1)), "stop_before_1R": bool(t_stop < t_1r),
        })
    return pd.DataFrame(rows)


def pct_table(d: pd.DataFrame, keys: List[str]) -> pd.DataFrame:
    cols = ["T_MAE", "T_MFE", "T_1R", "T_stop", "T_exit", "ratio_TMFE_Texit", "MFE", "MAE", "mfe_per_bar"]
    out = []
    for k, g in d.groupby(keys):
        row = {kk: vv for kk, vv in zip(keys, k if isinstance(k, tuple) else (k,))}
        row["n"] = len(g)
        row["reached_1R_pct"] = round(g["reached_1R"].mean() * 100, 1)
        row["stop_before_1R_pct"] = round(g["stop_before_1R"].mean() * 100, 1)
        for c in cols:
            v = g[c].replace(np.inf, np.nan)
            q = v.quantile(PCTS)
            for p, val in zip(PCTS, q.values):
                row[f"{c}_p{int(p * 100)}"] = round(float(val), 3) if pd.notna(val) else np.nan
        out.append(row)
    return pd.DataFrame(out)


def q(s):
    v = pd.Series(s).replace(np.inf, np.nan).dropna()
    if len(v) == 0:
        return "—"
    return " / ".join(f"{x:.1f}" for x in v.quantile(PCTS).values)


def print_report(d: pd.DataFrame, label: str) -> None:
    print(f"\n==================== {label} ====================")
    print("percentiles shown as p10 / p25 / p50 / p75 / p90   (bars from fill, entry bar = 1; inf excluded, share shown)")
    for tf, g in d.groupby("tf"):
        inf1 = np.isinf(g["T_1R"]).mean() * 100
        infs = np.isinf(g["T_stop"]).mean() * 100
        print(f"\n[{tf}] n={len(g)}")
        print(f"  T_exit  {q(g['T_exit'])}      T_MFE  {q(g['T_MFE'])}      T_MAE  {q(g['T_MAE'])}")
        print(f"  T_1R    {q(g['T_1R'])}   (never: {inf1:.0f}%)     T_stop {q(g['T_stop'])}   (never: {infs:.0f}%)")
        print(f"  MFE     {q(g['MFE'])}      MAE    {q(g['MAE'])}      mfe/bar {q(g['mfe_per_bar'])}      T_MFE/T_exit {q(g['ratio_TMFE_Texit'])}")
        w, lo = g[g["winner"]], g[~g["winner"]]
        print(f"  T_exit winners {q(w['T_exit'])}  (n={len(w)})   |   losers {q(lo['T_exit'])}  (n={len(lo)})")
        r1, nr = g[g["reached_1R"]], g[~g["reached_1R"]]
        print(f"  T_MFE  reached 1R {q(r1['T_MFE'])}  (n={len(r1)})   |   never {q(nr['T_MFE'])}  (n={len(nr)})")
        # overlap: share of losers whose T_exit falls inside the winners' IQR
        if len(w) >= 10 and len(lo) >= 10:
            lo_q, hi_q = w["T_exit"].quantile([0.25, 0.75])
            ov = ((lo["T_exit"] >= lo_q) & (lo["T_exit"] <= hi_q)).mean() * 100
            print(f"  losers' T_exit inside winners' IQR [{lo_q:.0f},{hi_q:.0f}]: {ov:.0f}%")
        print(f"  by exit reason: " + "; ".join(f"{r}: n={len(x)} T_exit p50={x['T_exit'].median():.0f}" for r, x in g.groupby("reason")))
        # decision rules
        p50 = lambda s: pd.Series(s).replace(np.inf, np.nan).median()
        t1r_med_inf = np.isinf(g["T_1R"]).mean() >= 0.5
        rules = []
        if tf == "5m" and t1r_med_inf and g["T_exit"].median() <= 6:
            rules.append("5m: p50 T_1R=inf and p50 T_exit<=6 -> do not rebuild on 5m; family lab on 15m/1h")
        if tf in ("15m", "1h") and g["MFE"].median() >= 1.0 and p50(g["T_stop"]) < p50(g["T_1R"]):
            rules.append("15m/1h: p50 MFE>=1R and stop clock beats 1R clock -> keep family, stop=pullback extreme/invalidation, don't raise TP")
        if g["MFE"].median() >= 1.2 and p50(g["T_MFE"]) < 0.5 * g["T_exit"].median():
            rules.append("p50 T_MFE << T_exit with MFE>=1.2R -> trail/CE is the leak; tighten trail or take 1R")
        if g["T_MAE"].median() == 1 and g["MAE"].median() >= 0.7:
            rules.append("p50 T_MAE=1 and MAE>=0.7R -> entry on impulse close is late; trigger on pullback-swing take")
        print("  RULES: " + (" | ".join(rules) if rules else "none triggered"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--family", default="")
    ap.add_argument("--variant-tag", default="_imp0.7_nogate")
    args = ap.parse_args()
    sym = args.symbol.replace("/", "_")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.family:
        src = lab.LAB_DIR / "variants" / f"{sym}_{args.family}{args.variant_tag}_trades.csv"
        df = pd.read_csv(src)
        df = df[df["variant"] == "taker"]
        exit_hold, net_col, reason_col, label = "r15_hold", "r15_net_r", "r15_reason", f"{args.family} {args.variant_tag} (taker, r15 exit)"
        stem = f"{sym}_{args.family}{args.variant_tag}"
    else:
        src = lab.LAB_DIR / f"{sym}_trades.csv"
        df = pd.read_csv(src)
        df = df[~df["scanner"].isin(["simple_bias", "cvd_divergence"])]
        exit_hold, net_col, reason_col, label = "hold_bars", "r", "reason", "21 live scanners (taker, live tp1 exit)"
        stem = sym
    parts = []
    for tf in sorted(df["tf"].unique(), key=lambda t: lab.TF_SECONDS[t]):
        parts.append(clocks_for(df[df["tf"] == tf], args.symbol, tf, exit_hold, net_col, reason_col))
    d = pd.concat(parts, ignore_index=True)
    d.to_csv(OUT_DIR / f"{stem}_duration_trades.csv", index=False)
    tables = []
    for keys in (["tf"], ["tf", "scanner"], ["tf", "side"], ["tf", "regime"], ["tf", "hour_b"], ["tf", "reason"]):
        t = pct_table(d, keys)
        t.insert(0, "split", "+".join(keys))
        tables.append(t)
    pd.concat(tables, ignore_index=True).to_csv(OUT_DIR / f"{stem}_duration_percentiles.csv", index=False)
    print_report(d, f"{args.symbol} — {label}")
    print(f"\nwrote {OUT_DIR / (stem + '_duration_trades.csv')} and {OUT_DIR / (stem + '_duration_percentiles.csv')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
