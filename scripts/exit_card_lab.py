#!/usr/bin/env python3
"""
exit_card_lab.py — re-exit the scanner-lab dumps under the per-TF exit card
(docs/research/EXIT_CARD_PREREG_20260917.md) and compare, walk-forward,
against the same entries under the old 48-bar tp1 / trail exits.

  python scripts/exit_card_lab.py --symbol BTC/USDT
  python scripts/exit_card_lab.py --symbol BTC/USDT --family trend_pb --variant-tag _imp0.7_nogate
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location("scanner_lab", Path(__file__).resolve().parent / "scanner_lab.py")
lab = importlib.util.module_from_spec(_spec)
sys.modules["scanner_lab"] = lab
_spec.loader.exec_module(lab)

SPLIT = "2026-07-01"
WINDOW = 48
CARD = {
    "5m":  {"kill": [(6, 0.3)], "late_fill_mae": 0.7, "cap": 12, "after_1r": "take_1r", "ema21_inval": False},
    "15m": {"kill": [(4, 0.3)], "late_fill_mae": None, "cap": 16, "after_1r": "trail", "ema21_inval": True},
    "1h":  {"kill": [(4, 0.3), (8, 0.8)], "late_fill_mae": None, "cap": 24, "after_1r": "trail", "ema21_inval": False},
}
TRAIL_MULT = 1.0


SCALPER = {"take_pct": 0.35, "stop_pct": 0.60, "max_bars": 6}      # 5m: flat inside 30 min -> exit leg free
SWING = {"4h": {"kill": [(6, 0.3)], "late_fill_mae": None, "cap": 30, "after_1r": "trail", "ema21_inval": False},
         "1h": {"kill": [(8, 0.3)], "late_fill_mae": None, "cap": 48, "after_1r": "trail", "ema21_inval": False}}


def scalper_exit(highs, lows, closes, e, side, entry, take_pct, stop_pct, max_bars) -> Tuple[int, float, str]:
    """Price-based scalp: take at +take_pct, stop at -stop_pct, force flat at
    max_bars (so the exit stays inside the Scalper Offer window). Stop first."""
    sgn = 1.0 if side == "long" else -1.0
    take = entry * (1 + sgn * take_pct / 100)
    stop = entry * (1 - sgn * stop_pct / 100)
    last = min(len(closes) - 1, e + max_bars - 1)
    for i in range(e, last + 1):
        if (sgn > 0 and lows[i] <= stop) or (sgn < 0 and highs[i] >= stop):
            return i, stop, "stop"
        if (sgn > 0 and highs[i] >= take) or (sgn < 0 and lows[i] <= take):
            return i, take, "take"
    return last, float(closes[last]), "flat_30m"


def card_exit(highs, lows, closes, ema21, e, side, entry, risk, card) -> Tuple[int, float, str]:
    """Returns (exit_idx, exit_r, reason). Stop first on every bar; kills and
    caps exit at close; after 1R: take 1R, or CE trail (+EMA21 invalidation)."""
    sgn = 1.0 if side == "long" else -1.0
    n = len(closes)
    last = min(n - 1, e + WINDOW - 1)
    stop_r = -1.0
    mfe = 0.0
    mae = 0.0
    reached = False
    for i in range(e, last + 1):
        t = i - e + 1
        fav = sgn * ((highs[i] if sgn > 0 else lows[i]) - entry) / risk
        adv = -sgn * ((lows[i] if sgn > 0 else highs[i]) - entry) / risk
        if adv >= -stop_r:
            return i, stop_r, ("stop" if stop_r <= -1.0 else "trail_stop")
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        cr = sgn * (closes[i] - entry) / risk
        if not reached and fav >= 1.0:
            reached = True
            if card["after_1r"] == "take_1r":
                return i, 1.0, "take_1r"
        if card["late_fill_mae"] is not None and t <= 2 and mae >= card["late_fill_mae"]:
            return i, cr, "late_fill"
        for bar, need in card["kill"]:
            if t == bar and mfe < need:
                return i, cr, f"kill_b{bar}"
        if not reached and t >= card["cap"]:
            return i, cr, "cap"
        if reached:
            if card["ema21_inval"] and sgn * (closes[i] - ema21[i]) < 0:
                return i, cr, "ema21_inval"
            stop_r = max(stop_r, mfe - TRAIL_MULT)
    return last, sgn * (closes[last] - entry) / risk, "window_end"


def main() -> int:
    from execution.fees import FeeLeg
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--family", default="")
    ap.add_argument("--variant-tag", default="_imp0.7_nogate")
    ap.add_argument("--dump", default="", help="explicit scanner-lab trades CSV (e.g. a variants/ run) instead of the live dump")
    ap.add_argument("--mode", choices=("card", "scalper", "swing"), default="card",
                    help="card = per-TF exit card; scalper = +0.35%%/-0.6%% price take/stop, flat at 6 bars (5m only); swing = 4h/1h trail card")
    ap.add_argument("--take-pct", type=float, default=SCALPER["take_pct"])
    ap.add_argument("--stop-pct", type=float, default=SCALPER["stop_pct"])
    ap.add_argument("--entry", choices=("taker", "maker"), default="taker", help="entry liquidity assumed for fees")
    args = ap.parse_args()
    sym = args.symbol.replace("/", "_")
    fm, _ = lab._fee_model_from_settings()

    if args.dump:
        df = pd.read_csv(args.dump)
        df = df[~df["scanner"].isin(["simple_bias", "cvd_divergence"])].copy()
        base_cols = {"old_tp1": "r", "old_trail": None}
        label = f"21 live scanners [{Path(args.dump).name}]"
        out_tag = "exit_card_" + Path(args.dump).stem.replace(f"{sym}_", "").replace("_trades", "")
    elif args.family:
        df = pd.read_csv(lab.LAB_DIR / "variants" / f"{sym}_{args.family}{args.variant_tag}_trades.csv")
        df = df[df["variant"] == "taker"].copy()
        df["scanner"] = args.family
        base_cols = {"old_tp1": None, "old_r15": "r15_net_r", "old_trail": "trail_net_r"}
        label = f"{args.family}{args.variant_tag}"
        out_tag = "exit_card_" + args.family
    else:
        df = pd.read_csv(lab.LAB_DIR / f"{sym}_trades.csv")
        df = df[~df["scanner"].isin(["simple_bias", "cvd_divergence"])].copy()
        base_cols = {"old_tp1": "r", "old_trail": None}
        label = "21 live scanners"
        out_tag = "exit_card"
    df["entry_time_dt"] = pd.to_datetime(df["entry_time"])
    df["oos"] = df["entry_time_dt"] >= SPLIT

    rows = []
    cards = {"card": CARD, "scalper": {"5m": None}, "swing": SWING}[args.mode]
    for tf in sorted(df["tf"].unique(), key=lambda t: lab.TF_SECONDS[t]):
        if tf not in cards:
            continue
        cand = lab.load_candles(args.symbol, tf, 200, False)
        idx = {str(t): i for i, t in enumerate(cand["datetime"].astype(str).values)}
        highs = cand["high"].values.astype(float)
        lows = cand["low"].values.astype(float)
        closes = cand["close"].values.astype(float)
        ema21 = cand["close"].ewm(span=21, adjust=False).mean().values
        bar_sec = lab.TF_SECONDS[tf]
        for _, t in df[df["tf"] == tf].iterrows():
            e = idx.get(str(t["entry_time"]))
            if e is None:
                continue
            entry = float(t["entry"])
            risk = entry * float(t["risk_pct"]) / 100.0
            if risk <= 0:
                continue
            sgn = 1.0 if t["side"] == "long" else -1.0
            if args.mode == "scalper":
                xi, exit_px, reason = scalper_exit(highs, lows, closes, e, t["side"], entry, args.take_pct, args.stop_pct, SCALPER["max_bars"])
                r = sgn * (exit_px - entry) / risk
            else:
                xi, r, reason = card_exit(highs, lows, closes, ema21, e, t["side"], entry, risk, cards[tf])
                exit_px = entry + sgn * r * risk
            hold = xi - e + 1
            fees = fm.trade_fees(entry, entry_liquidity=args.entry,
                                 exit_legs=[FeeLeg(1.0, exit_px, "taker", elapsed_sec=hold * bar_sec)], symbol=args.symbol)
            fee_r = (entry * fees.total_pct / 100.0) / risk
            gross_pct = sgn * (exit_px - entry) / entry * 100.0
            row = {"tf": tf, "scanner": t["scanner"], "side": t["side"], "oos": bool(t["oos"]),
                   "entry_time": t["entry_time"], "regime": t.get("regime", ""),
                   "card_r": r - fee_r, "card_hold": hold, "card_reason": reason,
                   "card_net_pct": gross_pct - fees.total_pct, "card_net_usd": 30000.0 * (gross_pct - fees.total_pct) / 100.0}
            if args.family:
                row["old_tp1"] = float(t["r15_net_r"])       # the family's headline (1.5R) exit
                row["old_trail"] = float(t["trail_net_r"])
            else:
                row["old_tp1"] = float(t["r"])
                row["old_trail"] = float(t["alt_trail"]) - float(t["fee_r"])
            rows.append(row)
    d = pd.DataFrame(rows)
    if args.mode != "card":
        out_tag = f"{out_tag}_{args.mode}_{args.entry}"
    out = lab.LAB_DIR / "duration" / f"{sym}_{out_tag}_trades.csv"
    if args.mode == "scalper":
        # $ view is the honest one for a scalp (R against a 0.6% stop is not the point)
        print(f"\n==================== {args.symbol} — SCALPER exit (+{args.take_pct}% / -{args.stop_pct}%, flat at 30 min, {args.entry} entry, exit free inside the offer) ====================")
        for tf, g in d.groupby("tf"):
            i, o = g[~g["oos"]], g[g["oos"]]
            print(f"[{tf}] IS n={len(i)} avg$={i['card_net_usd'].mean():+.1f} | OOS n={len(o)} avg$={o['card_net_usd'].mean():+.1f} tot$={o['card_net_usd'].sum():+.0f} "
                  f"win={(o['card_net_usd'] > 0).mean() * 100:.0f}%  reasons: " + ", ".join(f"{k}={v}" for k, v in g["card_reason"].value_counts().items()))
            per = []
            for sc, s in g.groupby("scanner"):
                si, so = s[~s["oos"]], s[s["oos"]]
                if len(so) >= 30 and len(si) >= 30:
                    per.append((sc, len(si), si["card_net_usd"].mean(), len(so), so["card_net_usd"].mean(), so["card_net_usd"].sum(),
                                (so["card_reason"] == "take").mean() * 100, (so["card_reason"] == "stop").mean() * 100))
            per.sort(key=lambda x: -x[4])
            print(f"     {'scanner':22s} {'n_is':>5s} {'avg$_is':>8s} {'n_oos':>5s} {'avg$_oos':>8s} {'tot$_oos':>9s} {'take%':>6s} {'stop%':>6s}  both>0")
            for sc, ni, ai, no, ao, to, tk, st in per:
                print(f"     {sc:22s} {ni:5d} {ai:+8.1f} {no:5d} {ao:+8.1f} {to:+9.0f} {tk:6.0f} {st:6.0f}  {'YES' if (ai > 0 and ao > 0) else ''}")
        d.to_csv(out, index=False)
        print(f"\nwrote {out}")
        return 0
    d.to_csv(out, index=False)

    pd.set_option("display.width", 200, "display.float_format", "{:.3f}".format)
    print(f"\n==================== {args.symbol} — {label}: old exits vs card, same entries ====================")
    for tf, g in d.groupby("tf"):
        i, o = g[~g["oos"]], g[g["oos"]]
        def line(name, col):
            return (f"  {name:10s} IS n={len(i):5d} avg={i[col].mean():+.3f} | OOS n={len(o):5d} avg={o[col].mean():+.3f} "
                    f"tot={o[col].sum():8.1f} win={(o[col] > 0).mean() * 100:5.1f}%")
        print(f"[{tf}]")
        print(line("old_tp1", "old_tp1"))
        print(line("old_trail", "old_trail"))
        print(line("CARD", "card_r"))
        delta_tp1 = o["card_r"].mean() - o["old_tp1"].mean()
        delta_trail = o["card_r"].mean() - o["old_trail"].mean()
        verdict = "PASS" if (delta_tp1 > 0 and delta_trail > 0) else "FAIL"
        print(f"  OOS delta vs tp1 {delta_tp1:+.3f}, vs trail {delta_trail:+.3f}  -> {verdict}   "
              f"card hold p50={g['card_hold'].median():.0f}  reasons: " +
              ", ".join(f"{k}={v}" for k, v in g["card_reason"].value_counts().items()))
        # per scanner OOS (n>=20)
        per = []
        for sc, s in o.groupby("scanner"):
            if len(s) >= 20:
                per.append((sc, len(s), s["old_tp1"].mean(), s["old_trail"].mean(), s["card_r"].mean()))
        per.sort(key=lambda x: -(x[4] - x[2]))
        for sc, n, a, b, c in per:
            print(f"     {sc:22s} n={n:4d}  old_tp1={a:+.3f} old_trail={b:+.3f} card={c:+.3f}  d_tp1={c - a:+.3f}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
