#!/usr/bin/env python3
"""
breakout_scalp_lab.py — lab for docs/research/BREAKOUT_SCALP_PREREG_20260917.md.

Fresh-4h-high breakout (cross event) with EMA8 > EMA21, entry next open
(taker), take at +take_pct via a resting stop above entry, protective stop at
-stop_pct, 12h max hold, Scalper Offer credited per exit leg. Rolling-fold
report per side and pooled, plus the same trades with the offer OFF.

  python scripts/breakout_scalp_lab.py --symbol ETH/USDT --tf 5m --days 200
  python scripts/breakout_scalp_lab.py --symbol ETH/USDT --tf 15m --days 500 --judge-days 60 --fit-days 120
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

BARS_4H = {"5m": 48, "15m": 16, "1h": 4}


def rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()
    return 100 - 100 / (1 + g / l)


def simulate(df: pd.DataFrame, tf: str, take_pct: float, stop_pct: float, persist: int,
             max_hold_bars: int, fm, symbol: str, notional: float, ema_gate: bool = True) -> pd.DataFrame:
    from execution.fees import FeeLeg, FeeModel
    fm_off = FeeModel(scalper_offer=False)
    n4 = BARS_4H[tf]
    bar_sec = lab.TF_SECONDS[tf]
    o = df["open"].values.astype(float); h = df["high"].values.astype(float)
    l = df["low"].values.astype(float); c = df["close"].values.astype(float)
    roll_hi = pd.Series(h).rolling(n4).max().shift(1).values
    roll_lo = pd.Series(l).rolling(n4).min().shift(1).values
    e8 = pd.Series(c).ewm(span=8, adjust=False).mean().values
    e21 = pd.Series(c).ewm(span=21, adjust=False).mean().values
    r14 = rsi(pd.Series(c)).values
    times = df["datetime"].astype(str).values
    n = len(c)
    rows = []
    open_until = -1
    up_streak = dn_streak = 0
    for i in range(n4 + 25, n - 1):
        above = c[i] > roll_hi[i] if not np.isnan(roll_hi[i]) else False
        below = c[i] < roll_lo[i] if not np.isnan(roll_lo[i]) else False
        up_streak = up_streak + 1 if above else 0
        dn_streak = dn_streak + 1 if below else 0
        side = None
        if up_streak == persist and (not ema_gate or e8[i] > e21[i]):
            side = "long"
        elif dn_streak == persist and (not ema_gate or e8[i] < e21[i]):
            side = "short"
        if side is None or i <= open_until:
            continue
        sgn = 1.0 if side == "long" else -1.0
        entry = o[i + 1]
        take = entry * (1 + sgn * take_pct / 100)
        stop = entry * (1 - sgn * stop_pct / 100)
        last = min(n - 1, i + max_hold_bars)
        exit_idx, exit_px, reason = last, c[last], "max_hold"
        for k in range(i + 1, last + 1):
            hit_stop = (l[k] <= stop) if sgn > 0 else (h[k] >= stop)
            hit_take = (h[k] >= take) if sgn > 0 else (l[k] <= take)
            if hit_stop:                     # stop first on a bar that touches both
                exit_idx, exit_px, reason = k, stop, "stop"; break
            if hit_take:
                exit_idx, exit_px, reason = k, take, "take"; break
        hold = exit_idx - (i + 1) + 1
        gross_pct = sgn * (exit_px - entry) / entry * 100
        legs = [FeeLeg(1.0, exit_px, "taker", elapsed_sec=hold * bar_sec)]
        fee_on = fm.trade_fees(entry, "taker", legs, symbol=symbol).total_pct
        fee_off = fm_off.trade_fees(entry, "taker", legs, symbol=symbol).total_pct
        rows.append({"signal_time": times[i], "entry_time": times[i + 1], "side": side, "entry": entry,
                     "exit": exit_px, "reason": reason, "hold_bars": hold, "hold_min": hold * bar_sec / 60,
                     "gross_pct": gross_pct, "fee_pct_on": fee_on, "fee_pct_off": fee_off,
                     "net_usd_on": notional * (gross_pct - fee_on) / 100, "net_usd_off": notional * (gross_pct - fee_off) / 100,
                     "net_r_on": (gross_pct - fee_on) / stop_pct, "rsi": r14[i], "hour": int(times[i + 1][11:13])})
        open_until = exit_idx
    out = pd.DataFrame(rows)
    out.attrs["bars"] = n
    return out


def folds_report(t: pd.DataFrame, bars: int, fit_days: int, judge_days: int, label: str) -> dict:
    t = t.copy()
    t["t"] = pd.to_datetime(t["entry_time"])
    t0, t1 = t["t"].min(), t["t"].max()
    edges = []
    s = t0 + pd.Timedelta(days=fit_days)
    while s + pd.Timedelta(days=judge_days) <= t1 + pd.Timedelta(days=1):
        edges.append((s, s + pd.Timedelta(days=judge_days))); s += pd.Timedelta(days=judge_days)
    fires_pct = len(t) / bars * 100
    print(f"\n=== {label} ===  trades={len(t)} ({fires_pct:.2f}% of bars; kill if >3%)  {t0.date()} -> {t1.date()}  folds={len(edges)}")
    res = {}
    for sub_name, sub in (("all", t), ("long", t[t.side == "long"]), ("short", t[t.side == "short"])):
        if len(sub) == 0:
            continue
        for fee_name, col in (("offer ON", "net_usd_on"), ("offer OFF", "net_usd_off")):
            fa = []
            for a, b in edges:
                j = sub[(sub["t"] >= a) & (sub["t"] < b)][col]
                fa.append(j.mean() if len(j) >= 8 else np.nan)
            fa = np.array(fa, dtype=float); v = fa[~np.isnan(fa)]
            top5 = np.sort(sub[col].values)[-5:].sum() / sub[col].sum() if sub[col].sum() > 0 and len(sub) >= 5 else np.nan
            win = (sub[col] > 0).mean() * 100
            print(f"  {sub_name:5s} {fee_name:9s} n={len(sub):5d} win={win:5.1f}% avg$={sub[col].mean():+7.2f} tot$={sub[col].sum():+9.0f} "
                  f"pos_folds={int((v > 0).sum())}/{len(v)} [{' '.join('+' if x > 0 else '-' for x in v)}] top5={top5:.2f} "
                  f"medhold={sub['hold_min'].median():.0f}min take={(sub.reason == 'take').mean() * 100:.0f}% stop={(sub.reason == 'stop').mean() * 100:.0f}%")
            if sub_name == "all" and fee_name == "offer ON":
                res = {"n": len(sub), "avg_usd": sub[col].mean(), "pos_folds": int((v > 0).sum()), "folds": len(v),
                       "top5": top5, "fires_pct": fires_pct,
                       "candidate": bool((v > 0).sum() >= 4 and sub[col].mean() > 0 and (top5 < 0.6) and fires_pct <= 3)}
    print(f"  -> {'CANDIDATE' if res.get('candidate') else 'not a candidate'} (>=4 positive folds, pooled avg>0, top5<0.6, fires<=3%)")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ETH/USDT")
    ap.add_argument("--tf", default="5m", choices=list(BARS_4H))
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--take-pct", type=float, default=0.35)
    ap.add_argument("--stop-pct", type=float, default=1.1)
    ap.add_argument("--persist", type=int, default=1)
    ap.add_argument("--max-hold-hours", type=float, default=12.0)
    ap.add_argument("--margin", type=float, default=1000.0)
    ap.add_argument("--leverage", type=float, default=30.0)
    ap.add_argument("--fit-days", type=int, default=60)
    ap.add_argument("--judge-days", type=int, default=30)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-ema-gate", action="store_true")
    args = ap.parse_args()
    fm, _ = lab._fee_model_from_settings()
    df = lab.load_candles(args.symbol, args.tf, args.days, args.refresh)
    max_hold_bars = int(args.max_hold_hours * 3600 / lab.TF_SECONDS[args.tf])
    t = simulate(df, args.tf, args.take_pct, args.stop_pct, args.persist, max_hold_bars, fm, args.symbol,
                 args.margin * args.leverage, ema_gate=not args.no_ema_gate)
    label = f"{args.symbol} {args.tf} {args.days}d take={args.take_pct}% stop={args.stop_pct}% persist={args.persist} ema_gate={not args.no_ema_gate}"
    folds_report(t, t.attrs["bars"], args.fit_days, args.judge_days, label)
    out = lab.LAB_DIR / "variants" / f"{args.symbol.replace('/', '_')}_breakout_scalp_{args.tf}_{args.days}d_t{args.take_pct:g}_s{args.stop_pct:g}_p{args.persist}_trades.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    t.to_csv(out, index=False)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
