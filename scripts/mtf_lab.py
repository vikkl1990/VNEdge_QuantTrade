#!/usr/bin/env python3
"""
mtf_lab.py — multi-timeframe plan + 1m execution, pre-registered in
docs/research/MTF_PREREG_20260920.md.

Test A: does 4h / 1h structure agreement improve the 1h scanner book (500 d dumps)?
Test B: does 1m execution (limit-in-zone, pullback-then-resume, close-through stops)
        improve the same 5m/15m/1h entries, with the plan's stop and R unchanged?

  python scripts/mtf_lab.py --symbol BTC/USDT
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
from data.market_structure import atr_wilder, build_structure  # noqa: E402
from execution.fees import FeeLeg  # noqa: E402

NOTIONAL = 30_000.0
TF_MIN = {"5m": 5, "15m": 15, "1h": 60}
W_EXEC = 30            # minutes to get filled
LIMIT_ATR = 0.25       # E1: limit this far inside the zone
PB_ATR = 0.20          # E2: pullback depth before a resume
RESUME_BARS = 3        # E2: close beyond the extreme of the previous N 1m bars
CARD = {"kill": [(4, 0.3), (8, 0.8)], "cap": 24, "window": 48}   # in bars of the signal tf → minutes
OUT = lab.LAB_DIR / "mtf"


def to_ms(series: pd.Series) -> np.ndarray:
    """Epoch milliseconds regardless of the datetime resolution pandas inferred (ns vs us)."""
    return series.dt.tz_convert(None).values.astype("datetime64[ms]").astype("int64")


# ───────────────────────────── Test A ─────────────────────────────
def state_at(struct_df: pd.DataFrame, bar_ts_ms: np.ndarray, bar_sec: int, when: pd.Series) -> pd.Series:
    """State of the last bar that closed at or before each `when` (UTC)."""
    close_ms = bar_ts_ms + bar_sec * 1000
    idx = np.searchsorted(close_ms, to_ms(when), side="right") - 1
    states = struct_df["state"].values
    out = np.where(idx >= 0, states[np.clip(idx, 0, len(states) - 1)], "none")
    return pd.Series(out, index=when.index)


def test_a(sym: str, k: float, fit_days=120, judge_days=60):
    tag = sym.replace("/", "_")
    d = pd.read_csv(lab.LAB_DIR / "variants" / f"{tag}_1h500d_trades.csv")
    d = d[~d.scanner.isin(["simple_bias", "cvd_divergence"])].copy()
    d["t"] = pd.to_datetime(d["entry_time"], utc=True)
    d["net_trail"] = d["alt_trail"] - d["fee_r"]
    for tf, sec in (("4h", 14400), ("1h", 3600)):
        c = lab.load_candles(sym, tf, 500, False).reset_index(drop=True)
        st, _ = build_structure(c, k=k)
        d[f"st_{tf}"] = state_at(st, c["timestamp"].values.astype("int64"), sec, d["t"])
        d[f"al_{tf}"] = np.select(
            [((d.side == "long") & (d[f"st_{tf}"] == "up")) | ((d.side == "short") & (d[f"st_{tf}"] == "down")),
             ((d.side == "long") & (d[f"st_{tf}"] == "down")) | ((d.side == "short") & (d[f"st_{tf}"] == "up"))],
            ["agree", "disagree"], default="range")
    d["al_both"] = np.where((d.al_4h == "agree") & (d.al_1h == "agree"), "agree",
                            np.where((d.al_4h == "disagree") | (d.al_1h == "disagree"), "disagree", "range"))
    t0 = d.t.min(); fit_end = t0 + pd.Timedelta(days=fit_days)
    edges, s = [], fit_end
    while s + pd.Timedelta(days=judge_days) <= d.t.max() + pd.Timedelta(days=1):
        edges.append((s, s + pd.Timedelta(days=judge_days))); s += pd.Timedelta(days=judge_days)
    rows = []
    for frame in ("al_4h", "al_1h", "al_both"):
        for b in ("agree", "disagree", "range"):
            g = d[d[frame] == b]
            j = g[g.t >= fit_end]["net_trail"]; f = g[g.t < fit_end]["net_trail"]
            fm = [g[(g.t >= a) & (g.t < e)]["net_trail"].mean() for a, e in edges]
            fm = np.array([x for x in fm if not np.isnan(x)])
            top5 = np.sort(j.values)[-5:].sum() / j.sum() if len(j) >= 5 and j.sum() > 0 else np.nan
            rows.append({"sym": sym[:3], "frame": frame.replace("al_", ""), "bucket": b, "n": len(g), "n_judge": len(j),
                         "fit_net": f.mean(), "judge_net": j.mean(), "judge_gross": (g[g.t >= fit_end]["alt_trail"]).mean(),
                         "pos_folds": f"{int((fm > 0).sum())}/{len(fm)}", "pos": int((fm > 0).sum()), "nf": len(fm), "top5": top5,
                         "win%": (j > 0).mean() * 100 if len(j) else np.nan})
    r = pd.DataFrame(rows)
    print(f"\n===== TEST A {sym}: 1h book by higher-frame agreement (net trail R; fit {fit_days}d, {len(edges)} folds) =====")
    print(r[["frame", "bucket", "n", "n_judge", "fit_net", "judge_net", "judge_gross", "win%", "pos_folds", "top5"]].to_string(index=False))
    occ = d.groupby("al_4h").size() / len(d) * 100
    print(f"   4h bucket occupancy % {occ.round(1).to_dict()}   1h {(d.groupby('al_1h').size() / len(d) * 100).round(1).to_dict()}")
    return r


# ───────────────────────────── Test B ─────────────────────────────
def load_1m(sym: str) -> pd.DataFrame:
    p = PROJECT_ROOT / "storage" / "research" / "orderflow" / f"{lab.delta_symbol(sym)}_1m.csv.gz"
    m = pd.read_csv(p, usecols=["minute", "open", "high", "low", "close"])
    m["t"] = pd.to_datetime(m["minute"], utc=True)
    return m.sort_values("t").reset_index(drop=True)


def exit_1m(H, L, C, e, side, entry, stop_price, tf_min, close_through=False):
    """Exit on 1m bars with the 1h card in minutes. Returns (exit_idx, exit_price, reason).
    Stop first every bar; trail 1R behind the 1m peak after 1R; kills/cap/window at close."""
    sgn = 1.0 if side == "long" else -1.0
    risk = abs(entry - stop_price)
    n = len(C); last = min(n - 1, e + CARD["window"] * tf_min - 1)
    stop = stop_price; mfe = 0.0; reached = False
    kills = [(b * tf_min, need) for b, need in CARD["kill"]]; cap = CARD["cap"] * tf_min
    for i in range(e, last + 1):
        t = i - e + 1
        if close_through:
            hit = (sgn > 0 and C[i] <= stop) or (sgn < 0 and C[i] >= stop)
            if hit:
                return i, float(C[i]), ("stop" if not reached else "trail_stop")
        else:
            hit = (sgn > 0 and L[i] <= stop) or (sgn < 0 and H[i] >= stop)
            if hit:
                return i, float(stop), ("stop" if not reached else "trail_stop")
        fav = sgn * ((H[i] if sgn > 0 else L[i]) - entry) / risk
        mfe = max(mfe, fav)
        if not reached and fav >= 1.0:
            reached = True
        for mins, need in kills:
            if t == mins and mfe < need:
                return i, float(C[i]), f"kill_{mins}m"
        if not reached and t >= cap:
            return i, float(C[i]), "cap"
        if reached:
            trail = entry + sgn * (mfe - 1.0) * risk
            stop = max(stop, trail) if sgn > 0 else min(stop, trail)
    return last, float(C[last]), "window_end"


def fill_e1(H, L, e, side, entry, atr, w):
    sgn = 1.0 if side == "long" else -1.0
    limit = entry - sgn * LIMIT_ATR * atr
    for i in range(e, min(len(L), e + w)):
        if (sgn > 0 and L[i] <= limit) or (sgn < 0 and H[i] >= limit):
            return i, limit
    return None, None


def fill_e2(H, L, C, e, side, entry, atr, w):
    sgn = 1.0 if side == "long" else -1.0
    pulled = False
    for i in range(e, min(len(C), e + w)):
        if not pulled:
            if (sgn > 0 and L[i] <= entry - PB_ATR * atr) or (sgn < 0 and H[i] >= entry + PB_ATR * atr):
                pulled = True
            continue
        if i - RESUME_BARS < e:
            continue
        ref = H[i - RESUME_BARS:i].max() if sgn > 0 else L[i - RESUME_BARS:i].min()
        if (sgn > 0 and C[i] > ref) or (sgn < 0 and C[i] < ref):
            return i, float(C[i])
    return None, None


def net_usd(fm, sym, side, entry_px, exit_px, entry_liq, hold_sec):
    sgn = 1.0 if side == "long" else -1.0
    gross = sgn * (exit_px - entry_px) / entry_px * NOTIONAL
    fees = fm.trade_fees(entry_px, entry_liq, [FeeLeg(1.0, exit_px, elapsed_sec=hold_sec)], symbol=sym, hold_seconds=hold_sec)
    return gross, gross - fees.usd(NOTIONAL)


def test_b(sym: str, fit_end="2026-06-01", folds=(("2026-06-01", "2026-07-01"), ("2026-07-01", "2026-08-01"), ("2026-08-01", "2026-09-01"))):
    tag = sym.replace("/", "_")
    fm, src = lab._fee_model_from_settings()
    d = pd.read_csv(lab.LAB_DIR / f"{tag}_trades.csv")
    d = d[~d.scanner.isin(["simple_bias", "cvd_divergence"]) & d.tf.isin(TF_MIN)].copy()
    d["t"] = pd.to_datetime(d["entry_time"], utc=True)
    m = load_1m(sym)
    d = d[(d.t >= m.t.min()) & (d.t <= m.t.max() - pd.Timedelta(hours=50))].reset_index(drop=True)
    H, L, C = m["high"].values, m["low"].values, m["close"].values
    mt = to_ms(m["t"])
    c5 = lab.load_candles(sym, "5m", 200, False).reset_index(drop=True)
    atr5 = atr_wilder(c5["high"].values, c5["low"].values, c5["close"].values, 14)
    t5 = c5["timestamp"].values.astype("int64")
    sig_ms = to_ms(pd.to_datetime(d["signal_time"], utc=True))
    d["atr5"] = atr5[np.clip(np.searchsorted(t5, sig_ms, side="right") - 1, 0, len(atr5) - 1)]
    d = d[d.atr5 > 0].reset_index(drop=True)
    e_idx = np.searchsorted(mt, to_ms(d["t"]), side="left")
    # Contiguity guard (2026-09-20): the archives have holes (ETH March is absent). Keep a signal only if
    # the 1m bar at the entry index is within 2 min of the entry time AND the execution+exit window
    # (48 bars of the signal tf + W) is gap-free (<= 5% missing minutes).
    ent_ms = to_ms(d["t"])
    need = (48 * d["tf"].map(TF_MIN).values + W_EXEC).astype(int)
    e_idx = np.clip(e_idx, 0, len(mt) - 1)
    end_idx = np.clip(e_idx + need, 0, len(mt) - 1)
    ok = (np.abs(mt[e_idx] - ent_ms) <= 120_000) & ((mt[end_idx] - mt[e_idx]) <= need * 60_000 * 1.05)
    dropped = int((~ok).sum())
    d = d[ok].reset_index(drop=True); e_idx = e_idx[ok]
    print(f"   contiguity guard: dropped {dropped} signals without gap-free 1m coverage")
    d["sgn"] = np.where(d.side == "long", 1.0, -1.0)
    d["stop_px"] = d["entry"] * (1 - d["sgn"] * d["risk_pct"] / 100)
    d["R"] = d["entry"] * d["risk_pct"] / 100
    print(f"\n===== TEST B {sym}: {len(d)} signals {d.t.min().date()} → {d.t.max().date()} on 1m bars ({len(m)} min), fees {src} =====")

    recs = []
    for r, e in zip(d.itertuples(), e_idx):
        tfm = TF_MIN[r.tf]
        cells = {}
        # B0 / B1: market at the signal close
        for cell, ct in (("B0", False), ("B1", True)):
            xi, xp, why = exit_1m(H, L, C, e, r.side, r.entry, r.stop_px, tfm, close_through=ct)
            g, n_ = net_usd(fm, sym, r.side, r.entry, xp, "taker", (xi - e + 1) * 60)
            cells[cell] = (r.entry, e, xi, xp, why, g, n_)
        # E1 / E2: timed 1m entries, plan stop unchanged
        fi, fp = fill_e1(H, L, e, r.side, r.entry, r.atr5, W_EXEC)
        if fi is not None:
            xi, xp, why = exit_1m(H, L, C, fi, r.side, fp, r.stop_px, tfm)
            g, n_ = net_usd(fm, sym, r.side, fp, xp, "maker", (xi - fi + 1) * 60)
            cells["E1"] = (fp, fi, xi, xp, why, g, n_)
        else:
            cells["E1"] = None
        fi, fp = fill_e2(H, L, C, e, r.side, r.entry, r.atr5, W_EXEC)
        if fi is not None:
            xi, xp, why = exit_1m(H, L, C, fi, r.side, fp, r.stop_px, tfm)
            g, n_ = net_usd(fm, sym, r.side, fp, xp, "taker", (xi - fi + 1) * 60)
            cells["E2"] = (fp, fi, xi, xp, why, g, n_)
        else:
            cells["E2"] = None
        for cell, v in cells.items():
            if v is None:
                recs.append({"t": r.t, "tf": r.tf, "scanner": r.scanner, "cell": cell, "filled": 0, "net": 0.0, "gross": 0.0,
                             "netR": 0.0, "stop": 0, "impr_atr": np.nan, "hold_min": np.nan})
            else:
                fp, fi, xi, xp, why, g, n_ = v
                recs.append({"t": r.t, "tf": r.tf, "scanner": r.scanner, "cell": cell, "filled": 1, "net": n_, "gross": g,
                             "netR": n_ / (NOTIONAL * r.risk_pct / 100), "stop": int(why in ("stop",)),
                             "impr_atr": r.sgn * (r.entry - fp) / r.atr5, "hold_min": xi - fi + 1})
    x = pd.DataFrame(recs)
    # structure agreement at entry (Test C): state of the last CLOSED 4h / 1h bar before the entry
    al = {}
    for tf, sec in (("4h", 14400), ("1h", 3600)):
        c = lab.load_candles(sym, tf, 500, False).reset_index(drop=True)
        st, _ = build_structure(c, k=2.0)
        stt = state_at(st, c["timestamp"].values.astype("int64"), sec, d["t"])
        al[tf] = np.select([((d.side == "long") & (stt == "up")) | ((d.side == "short") & (stt == "down")),
                            ((d.side == "long") & (stt == "down")) | ((d.side == "short") & (stt == "up"))],
                           ["agree", "disagree"], default="range")
    both = np.where((al["4h"] == "agree") & (al["1h"] == "agree"), "agree",
                    np.where((al["4h"] == "disagree") | (al["1h"] == "disagree"), "disagree", "range"))
    x["al_both"] = np.repeat(both, 4)          # four cells per signal, in order
    x.to_csv(OUT / f"{tag}_mtf_exec.csv", index=False)
    fe = pd.Timestamp(fit_end, tz="UTC")
    rows = []
    for tf in ["5m", "15m", "1h", "all"]:
        for cell in ["B0", "B1", "E1", "E2"]:
            g = x[(x.cell == cell) & ((x.tf == tf) if tf != "all" else True)]
            j = g[g.t >= fe]; f = g[g.t < fe]
            fold_net = [g[(g.t >= pd.Timestamp(a, tz="UTC")) & (g.t < pd.Timestamp(b, tz="UTC"))]["net"].mean() for a, b in folds]
            base = x[(x.cell == "B0") & ((x.tf == tf) if tf != "all" else True)]
            base_folds = [base[(base.t >= pd.Timestamp(a, tz="UTC")) & (base.t < pd.Timestamp(b, tz="UTC"))]["net"].mean() for a, b in folds]
            beats = sum(1 for a, b in zip(fold_net, base_folds) if not np.isnan(a) and a > b)
            jf = j[j.filled == 1]
            rows.append({"sym": sym[:3], "tf": tf, "cell": cell, "n": len(g), "fill%": g.filled.mean() * 100,
                         "fit_$/sig": f.net.mean(), "judge_$/sig": j.net.mean(), "judge_$/fill": jf.net.mean() if len(jf) else np.nan,
                         "judge_R/sig": j.netR.mean(), "stop%": jf.stop.mean() * 100 if len(jf) else np.nan,
                         "impr_atr": jf.impr_atr.mean() if len(jf) else np.nan, "hold_min": jf.hold_min.median() if len(jf) else np.nan,
                         "folds>B0": f"{beats}/{len(folds)}" if cell != "B0" else "-",
                         "d_vs_B0_$": j.net.mean() - base[base.t >= fe].net.mean()})
    r = pd.DataFrame(rows)
    print(r.to_string(index=False))
    return r


def test_c(sym: str, fit_end="2026-06-01", folds=(("2026-06-01", "2026-07-01"), ("2026-07-01", "2026-08-01"), ("2026-08-01", "2026-09-01"))):
    tag = sym.replace("/", "_")
    x = pd.read_csv(OUT / f"{tag}_mtf_exec.csv"); x["t"] = pd.to_datetime(x["t"], utc=True)
    fe = pd.Timestamp(fit_end, tz="UTC")
    rows = []
    for tf in ["5m", "15m", "1h", "all"]:
        for b in ["agree", "disagree", "range"]:
            for cell in ["B0", "E1"]:
                g = x[(x.cell == cell) & (x.al_both == b) & ((x.tf == tf) if tf != "all" else True)]
                j = g[g.t >= fe]; f = g[g.t < fe]
                fm = [g[(g.t >= pd.Timestamp(a, tz="UTC")) & (g.t < pd.Timestamp(e, tz="UTC"))]["net"].mean() for a, e in folds]
                fm = np.array([v for v in fm if not np.isnan(v)])
                jf = j[j.filled == 1]
                top5 = np.sort(j.net.values)[-5:].sum() / j.net.sum() if len(j) >= 5 and j.net.sum() > 0 else np.nan
                rows.append({"sym": sym[:3], "tf": tf, "bucket": b, "cell": cell, "n": len(g), "n_judge": len(j),
                             "fit_$/sig": f.net.mean(), "judge_$/sig": j.net.mean(), "judge_R/sig": j.netR.mean(),
                             "win%": (jf.net > 0).mean() * 100 if len(jf) else np.nan, "pos_folds": f"{int((fm > 0).sum())}/{len(fm)}", "top5": top5})
    r = pd.DataFrame(rows)
    print(f"\n===== TEST C {sym}: 4h+1h agreement × execution (net $ per signal at $30k) =====")
    print(r.to_string(index=False))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--k", type=float, default=2.0)
    ap.add_argument("--skip-a", action="store_true")
    ap.add_argument("--skip-b", action="store_true")
    ap.add_argument("--test-c", action="store_true", help="agreement x execution on the Test-B exec dump")
    a = ap.parse_args()
    pd.set_option("display.width", 250, "display.max_rows", 500, "display.float_format", "{:+.3f}".format)
    OUT.mkdir(parents=True, exist_ok=True)
    tag = a.symbol.replace("/", "_")
    if not a.skip_a:
        test_a(a.symbol, a.k).to_csv(OUT / f"{tag}_test_a.csv", index=False)
    if not a.skip_b:
        test_b(a.symbol).to_csv(OUT / f"{tag}_test_b.csv", index=False)
    if a.test_c:
        test_c(a.symbol).to_csv(OUT / f"{tag}_test_c.csv", index=False)


if __name__ == "__main__":
    main()
