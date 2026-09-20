#!/usr/bin/env python3
"""
family_lab.py — walk-forward lab for setup-family detectors (strategies/families/).

Runs a family detector bar-by-bar on the same frames, indicators, regime
detector, fee model and notional as scripts/scanner_lab.py, applies the
family's own risk contract (pattern stop, invalidation, expiry), and reports
fit (Mar-Jun) vs judge (Jul-Sep) results against the pre-registered kill
rules and against the pooled OLD family from the scanner-lab dump.

  python scripts/family_lab.py --family trend_pb --symbol BTC/USDT \
      --timeframes 15m,1h [--impulse-atr 0.7] [--days 200]

Entry variants (both always simulated):
  taker  next bar open, taker fee
  maker  limit at the trigger close; fills only if the next bar trades back
         to it (long: low <= trigger close); maker fee on entry
Exit models (each includes invalidation + expiry; stop checked first):
  r1     fixed 1R target
  r15    fixed 1.5R target
  trail  chandelier: stop ratchets to (peak - 1.0R) once MFE >= 0.3R
Output: storage/research/scanner_lab/variants/{SYM}_{family}{tag}.json / _trades.csv
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location("scanner_lab", Path(__file__).resolve().parent / "scanner_lab.py")
lab = importlib.util.module_from_spec(_spec)
sys.modules["scanner_lab"] = lab
_spec.loader.exec_module(lab)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("family_lab")

SPLIT = "2026-07-01"
MAX_HOLD = 48
OLD_FAMILY = {"trend_pb": ["ema_momentum", "trend_continuation", "momentum_ride", "post_impulse", "supertrend_flip"]}
REGIME_SIDE = {"long": "trending_up", "short": "trending_down"}
EXITS = ("r1", "r15", "trail")


def add_family_features(p: pd.DataFrame) -> pd.DataFrame:
    """rel_vol_tod: volume vs the median volume at the same UTC hour over the
    prior 30 occurrences (current bar excluded). rsi_pct: percentile of RSI
    within the trailing 200 bars."""
    p = p.copy()
    ts = pd.to_datetime(p["timestamp"], utc=True)
    hour = ts.dt.hour
    med = p.groupby(hour)["volume"].transform(lambda s: s.shift(1).rolling(30, min_periods=10).median())
    p["rel_vol_tod"] = p["volume"] / med
    p["rsi_pct"] = p["rsi"].rolling(200, min_periods=50).rank(pct=True)
    return p


def simulate_family_trade(highs, lows, closes, ema21, start_idx, side, entry, stop, exit_model,
                          max_hold=MAX_HOLD, expiry_bars=6, expiry_min_mfe=0.3, trail_mult=1.0,
                          trail_arm_mfe=0.3):
    """One entry variant under one exit model with the family contract.
    Returns (exit_idx, exit_r, reason, mfe_r, mae_r). Order per bar: stop,
    target, invalidation (close through EMA21), expiry, then trail update."""
    risk = abs(entry - stop)
    sgn = 1.0 if side == "long" else -1.0
    n = len(closes)
    last = min(n - 1, start_idx + max_hold - 1)
    target_r = {"r1": 1.0, "r15": 1.5, "trail": None}[exit_model]
    stop_r = -1.0
    peak = 0.0
    mae = 0.0
    for i in range(start_idx, last + 1):
        fav = sgn * ((highs[i] if sgn > 0 else lows[i]) - entry) / risk
        adv = -sgn * ((lows[i] if sgn > 0 else highs[i]) - entry) / risk
        mae = max(mae, adv)
        if adv >= -stop_r:                       # stop (initial or trailed)
            return i, stop_r, ("stop" if stop_r <= -1.0 else "trail_stop"), max(peak, fav), mae
        peak = max(peak, fav)
        if target_r is not None and fav >= target_r:
            return i, target_r, "target", peak, mae
        cr = sgn * (closes[i] - entry) / risk
        if sgn * (closes[i] - ema21[i]) < 0:        # invalidation: close through EMA21 against
            return i, cr, "invalidation", peak, mae
        held = i - start_idx + 1
        if held >= expiry_bars and peak < expiry_min_mfe:
            return i, cr, "expiry", peak, mae
        if exit_model == "trail" and peak >= trail_arm_mfe:
            stop_r = max(stop_r, peak - trail_mult)
    return last, sgn * (closes[last] - entry) / risk, "max_hold", peak, mae


def ema_slope_regime(ema21: np.ndarray, atr: np.ndarray, i: int, lookback: int = 20, thr: float = 1.0) -> str:
    """Pre-registered G3 labeller: (EMA21[t] - EMA21[t-20]) / ATR on the traded TF,
    the ±1.0 ATR/20-bar convention structure_bounce already uses."""
    if i < lookback or not (atr[i] > 0) or np.isnan(atr[i]) or np.isnan(ema21[i]) or np.isnan(ema21[i - lookback]):
        return "sideways"
    slope = (ema21[i] - ema21[i - lookback]) / atr[i]
    if slope > thr:
        return "trending_up"
    if slope < -thr:
        return "trending_down"
    return "sideways"


def run_family(family: str, symbol: str, timeframes: List[str], days: int, margin: float,
               leverage: float, detector_kwargs: Dict[str, Any], tag: str,
               regime_gate: bool = True, regime_source: str = "detector") -> Path:
    from execution.fees import FeeLeg
    from strategies.regime import MarketRegimeDetector
    from strategies.scalp_strategy import ScalpStrategy, _short_allowed

    if family == "trend_pb":
        from strategies.families.trend_pb import detect_trend_pb as detect
    else:
        raise SystemExit(f"unknown family {family}")

    logging.getLogger("strategies.scalp_strategy").setLevel(logging.WARNING)
    logging.getLogger("strategies.regime").setLevel(logging.ERROR)
    notional = margin * leverage
    fm, fee_source = lab._fee_model_from_settings()
    strat = ScalpStrategy({})
    detector = MarketRegimeDetector()

    needed = set(timeframes) | {lab.CONFIRM_TF[tf] for tf in timeframes}
    frames = {tf: lab.load_candles(symbol, tf, days, False) for tf in sorted(needed, key=lambda t: lab.TF_SECONDS[t])}

    all_rows: List[dict] = []
    summary: Dict[str, Any] = {}
    for tf in timeframes:
        praw, craw = frames[tf], frames[lab.CONFIRM_TF[tf]]
        bar_sec = lab.TF_SECONDS[tf]
        p_ts = praw["timestamp"].values.astype(np.int64)
        c_ts = craw["timestamp"].values.astype(np.int64)
        p_in, c_in = praw.copy(), craw.copy()
        p_in["timestamp"] = pd.to_datetime(p_ts, unit="ms", utc=True)
        c_in["timestamp"] = pd.to_datetime(c_ts, unit="ms", utc=True)
        p = add_family_features(strat._compute_indicators(p_in).reset_index(drop=True))
        c = strat._compute_indicators(c_in).reset_index(drop=True)
        n = len(p)
        j_for_i = np.searchsorted(c_ts + lab.TF_SECONDS[lab.CONFIRM_TF[tf]] * 1000, p_ts + bar_sec * 1000, side="right") - 1
        highs, lows, closes, opens = (p[k].values.astype(float) for k in ("high", "low", "close", "open"))
        ema21 = p["ema_21"].values.astype(float)
        atr_p = p["atr"].values.astype(float)
        times = p["datetime"].astype(str).values

        kw = dict(detector_kwargs)
        kw["max_pullback"] = 5 if tf == "5m" else 8
        regime, last_j = "sideways", -1
        fires = 0
        fires_by_regime: Dict[str, int] = {}
        gated = {"regime": 0, "allow_short": 0, "busy": 0}
        open_until = -1
        trades: List[dict] = []
        start = max(60, int(np.argmax(j_for_i >= 100)) if (j_for_i >= 100).any() else 60)
        t0 = time.time()
        for i in range(start, n - 1):
            j = int(j_for_i[i])
            if regime_source == "ema_slope":
                regime = ema_slope_regime(ema21, atr_p, i)
            elif j != last_j and j >= 0:
                cs = c.iloc[max(0, j - lab.CONTEXT_BARS + 1):j + 1]
                try:
                    regime = detector.detect_regime(cs).regime.value if len(cs) >= 100 else "sideways"
                except Exception:
                    regime = "sideways"
                last_j = j
            setup = detect(p.iloc[:i + 1], **kw)
            if setup is None:
                continue
            fires += 1
            fires_by_regime[f"{setup.side}@{regime}"] = fires_by_regime.get(f"{setup.side}@{regime}", 0) + 1
            if regime_gate and regime != REGIME_SIDE[setup.side]:
                gated["regime"] += 1
                continue
            if setup.side == "short" and not _short_allowed(symbol):
                gated["allow_short"] += 1
                continue
            if i <= open_until:
                gated["busy"] += 1
                continue
            sgn = 1.0 if setup.side == "long" else -1.0
            variants = {"taker": float(opens[i + 1])}
            trig = setup.trigger_close
            filled_maker = (lows[i + 1] <= trig) if sgn > 0 else (highs[i + 1] >= trig)
            if filled_maker:
                variants["maker"] = trig
            row_base = {
                "tf": tf, "family": family, "signal_time": times[i], "entry_time": times[i + 1],
                "side": setup.side, "regime": regime, "hour": int(times[i + 1][11:13]),
                "impulse_type": setup.impulse_type, "pullback_bars": setup.pullback_bars,
                "pullback_depth_atr": round(setup.pullback_depth_atr, 3), "score": setup.score,
                "maker_filled": bool(filled_maker),
            }
            longest = i + 1
            for variant, entry in variants.items():
                stop = setup.stop
                risk = abs(entry - stop)
                if risk <= 0:
                    continue
                risk_pct = risk / entry * 100.0
                row = dict(row_base)
                row.update({"variant": variant, "entry": round(entry, 4), "stop": round(stop, 4), "risk_pct": round(risk_pct, 4)})
                for ex in EXITS:
                    xi, r, reason, mfe, mae = simulate_family_trade(highs, lows, closes, ema21, i + 1, setup.side, entry, stop, ex)
                    hold_bars = xi - (i + 1) + 1
                    exit_px = entry + sgn * r * risk
                    fees = fm.trade_fees(entry, entry_liquidity=("taker" if variant == "taker" else "maker"),
                                         exit_legs=[FeeLeg(1.0, exit_px, "taker", elapsed_sec=hold_bars * bar_sec)], symbol=symbol)
                    fee_r = (entry * fees.total_pct / 100.0) / risk
                    net_r = r - fee_r
                    row.update({f"{ex}_r": round(r, 4), f"{ex}_net_r": round(net_r, 4), f"{ex}_reason": reason,
                                f"{ex}_hold": hold_bars, f"{ex}_fee_r": round(fee_r, 4),
                                f"{ex}_net_usd": round(notional * (net_r * risk_pct / 100.0), 2)})
                    longest = max(longest, xi)
                    if ex == "r15":
                        row.update({"mfe_r": round(mfe, 3), "mae_r": round(mae, 3)})
                trades.append(row)
            open_until = longest          # one open trade per family
            all_rows.extend([t for t in trades[-len(variants):]])
        summary[tf] = {"bars": n, "fires": fires, "fires_pct": round(fires / max(1, n - start) * 100, 3),
                       "fires_by_regime": dict(sorted(fires_by_regime.items())),
                       "gated": gated, "regime_gate": regime_gate, "regime_source": regime_source,
                       "taken": sum(1 for t in trades if t["variant"] == "taker"),
                       "elapsed_s": round(time.time() - t0)}
        logger.info("%s %s: %d fires (%.2f%% of bars), gated=%s, taken=%d (%.0fs)",
                    symbol, tf, fires, summary[tf]["fires_pct"], gated, summary[tf]["taken"], time.time() - t0)

    out_dir = lab.LAB_DIR / "variants"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{symbol.replace('/', '_')}_{family}{tag}"
    df = pd.DataFrame(all_rows)
    df.to_csv(out_dir / f"{stem}_trades.csv", index=False)
    meta = {"family": family, "symbol": symbol, "timeframes": timeframes, "days": days, "margin": margin,
            "leverage": leverage, "notional": notional, "detector_kwargs": detector_kwargs, "tag": tag,
            "fee_model": fm.describe(), "fee_source": fee_source, "split": SPLIT,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "summary": summary}
    (out_dir / f"{stem}.json").write_text(json.dumps(meta, indent=1))
    return out_dir / f"{stem}_trades.csv"


def report(csv_path: Path, symbol: str, family: str) -> None:
    df = pd.read_csv(csv_path)
    if df.empty:
        print("no trades"); return
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["oos"] = df["entry_time"] >= SPLIT
    meta = json.loads(csv_path.with_name(csv_path.name.replace("_trades.csv", ".json")).read_text())
    print(f"\n{family} — {symbol}  notional=${meta['notional']:,.0f}  detector={meta['detector_kwargs']}")
    for tf, s in meta["summary"].items():
        print(f"[{tf}] bars={s['bars']} fires={s['fires']} ({s['fires_pct']:.2f}% of bars; kill if >3%)  "
              f"regime_gate={'ON' if s.get('regime_gate', True) else 'OFF (diagnostic)'}  gated={s['gated']}  taken={s['taken']}")
        print(f"      fires by side@regime: {s.get('fires_by_regime', {})}")
    print(f"\n{'tf':4s} {'variant':7s} {'exit':5s} {'side':5s} {'n_is':>5s} {'avg_is':>7s} {'n_oos':>5s} {'avg_oos':>7s} {'tot_oos':>8s} {'win_oos':>7s} {'medMFE':>6s} {'top5':>5s} {'$oos':>9s}  verdict")
    for tf in meta["timeframes"]:
        for variant in ("taker", "maker"):
            for ex in EXITS:
                for side in ("all", "long", "short"):
                    d = df[(df["tf"] == tf) & (df["variant"] == variant)]
                    if side != "all":
                        d = d[d["side"] == side]
                    if len(d) == 0:
                        continue
                    net = d[f"{ex}_net_r"]
                    i, o = net[~d["oos"]], net[d["oos"]]
                    if len(o) == 0:
                        continue
                    mfe_oos = d.loc[d["oos"], "mfe_r"].median()
                    top5 = np.sort(o.values)[-5:].sum() / o.sum() if o.sum() > 0 and len(o) >= 5 else np.nan
                    usd = d.loc[d["oos"], f"{ex}_net_usd"].sum()
                    verdict = ""
                    if side == "all" and variant == "taker":
                        fails = []
                        if o.mean() <= 0: fails.append("avgR<=0")
                        if tf in ("15m", "1h") and mfe_oos < 1.0: fails.append("medMFE<1")
                        if meta["summary"][tf]["fires_pct"] > 3: fails.append("spray")
                        if i.mean() > 0 and o.mean() <= 0: fails.append("IS-only")
                        verdict = "PASS" if not fails else "FAIL(" + ",".join(fails) + ")"
                    print(f"{tf:4s} {variant:7s} {ex:5s} {side:5s} {len(i):5d} {i.mean() if len(i) else float('nan'):7.3f} "
                          f"{len(o):5d} {o.mean():7.3f} {o.sum():8.2f} {(o > 0).mean() * 100:7.1f} {mfe_oos:6.2f} {top5:5.2f} {usd:9,.0f}  {verdict}")
    # old-family bag from the scanner-lab live dump
    old_csv = lab.LAB_DIR / f"{symbol.replace('/', '_')}_trades.csv"
    if old_csv.exists() and family in OLD_FAMILY:
        od = pd.read_csv(old_csv)
        od = od[od["scanner"].isin(OLD_FAMILY[family])]
        od["entry_time"] = pd.to_datetime(od["entry_time"])
        od["oos"] = od["entry_time"] >= SPLIT
        print(f"\nOLD FAMILY BAG ({', '.join(OLD_FAMILY[family])}) from {old_csv.name}, OOS, taker fee:")
        for tf in meta["timeframes"]:
            for label, m in (("raw", np.ones(len(od), bool)), ("routed", od["routed"] == True)):
                d = od[(od["tf"] == tf) & m & od["oos"]]
                if len(d) == 0:
                    continue
                print(f"  [{tf}] {label:6s} n={len(d):5d}  tp1 avg={ (d['alt_tp1'] - d['fee_r']).mean():+.3f} tot={(d['alt_tp1'] - d['fee_r']).sum():8.1f}   "
                      f"trail avg={(d['alt_trail'] - d['fee_r']).mean():+.3f} tot={(d['alt_trail'] - d['fee_r']).sum():8.1f}   medMFE={d['alt_mfe'].median():.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--family", default="trend_pb")
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--timeframes", default="15m,1h")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--margin", type=float, default=1000.0)
    ap.add_argument("--leverage", type=float, default=30.0)
    ap.add_argument("--impulse-atr", type=float, default=0.7)
    ap.add_argument("--tag", default="")
    ap.add_argument("--no-regime-gate", action="store_true",
                    help="DIAGNOSTIC ONLY: take every fire regardless of regime (not a v1 result)")
    ap.add_argument("--stop-mode", choices=("pullback", "impulse"), default="pullback",
                    help="G2 variant: stop at the impulse bar's extreme instead of the pullback extreme")
    ap.add_argument("--regime-source", choices=("detector", "ema_slope"), default="detector",
                    help="G3 variant: EMA21-slope/ATR labeller on the traded TF instead of the confirm-frame detector")
    args = ap.parse_args()
    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    tag = args.tag or (f"_imp{args.impulse_atr:g}" + ("_nogate" if args.no_regime_gate else "")
                       + (f"_stop{args.stop_mode}" if args.stop_mode != "pullback" else "")
                       + (f"_{args.regime_source}" if args.regime_source != "detector" else ""))
    csv_path = run_family(args.family, args.symbol, tfs, args.days, args.margin, args.leverage,
                          {"impulse_atr": args.impulse_atr, "stop_mode": args.stop_mode}, tag,
                          regime_gate=not args.no_regime_gate, regime_source=args.regime_source)
    report(csv_path, args.symbol, args.family)
    print(f"\nwrote {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
