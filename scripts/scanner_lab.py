#!/usr/bin/env python3
"""
scanner_lab.py — per-pair scanner x timeframe replay with dollar P&L.

For ONE symbol, replays every `_scan_*` scanner of ScalpStrategy on each
requested primary timeframe (default 5m, 15m, 1h) over cached history and
turns every fire into a simulated trade sized at --margin x --leverage
notional, net of Delta fees. Output feeds the Scanner Lab page
(/static/scanner_lab.html) via /api/research/scanner-lab.

What is reused from the live bot, on purpose (this is a measurement of the
live code, not a reimplementation of it):
  * indicators           ScalpStrategy._compute_indicators (same as live)
  * structure map        data.structure.build_structure_map from the CONFIRM
                         frame, rebuilt whenever the confirm bar advances —
                         exactly what analyze() does (scalp_strategy ~1786)
  * htf / confirm bias   ScalpStrategy._get_htf_bias
  * regime               strategies.regime.MarketRegimeDetector on the confirm
                         frame (what RegimeFilter delegates to live)
  * routing table        REGIME_SCANNER_ROUTING, read out of analyze()'s source
                         by AST so the lab can't drift from the live dict
  * SL / TP1             ScalpStrategy._build_signal — the live clamp
                         (min/max_sl_pct + 0.1% buffer), per-scanner
                         sl_atr/tp_rr, regime + HTF TP multipliers
  * fees                 execution.fees.FeeModel from settings.yaml, incl. the
                         Scalper Offer free-exit window per leg
  * $ formulas           bot/signal_tracker._calc_pnl: pnl_usd = notional x
                         net_pct, exit_r = entry x net_pct / initial_risk

Exit model (stated in the JSON meta): entry at the NEXT bar's open (closed
bar in, next bar out), exit on the first bar whose high/low touches the
stop or TP1 — stop wins if both are touched on the same bar — else close at
--max-hold-bars. One open trade per scanner at a time; scanners are
measured independently of each other (this is not a portfolio simulation).

Read-only with respect to the bot: touches nothing under storage/ except
storage/research/scanner_lab/.

  python scripts/scanner_lab.py --symbol BTC/USDT [--refresh] [--days 200]
      [--timeframes 5m,15m,1h] [--margin 1000] [--leverage 30] [--max-hold-bars 48]
"""
from __future__ import annotations

import argparse
import ast
import inspect
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scanner_lab")

LAB_DIR = PROJECT_ROOT / "storage" / "research" / "scanner_lab"
CANDLE_DIR = LAB_DIR / "candles"
DELTA_CANDLES_URL = "https://api.india.delta.exchange/v2/history/candles"

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
# Confirm/HTF frame per primary — mirrors the live chain shape
# (ScalpStrategy({}) defaults to primary 5m / confirm 15m / htf 15m).
CONFIRM_TF = {"1m": "5m", "5m": "15m", "15m": "1h", "1h": "4h", "4h": "1d"}
LEARNING_ONLY = frozenset({"simple_bias", "cvd_divergence"})
CHUNK = 1900          # Delta caps candles per request
CONTEXT_BARS = 300    # confirm-frame window handed to structure map / regime / bias
FWD_HORIZONS = {"1h": 12, "4h": 48}  # in primary bars; labels are for 5m, kept for continuity
TRADES_KEPT = 200     # per scanner per tf in the JSON


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def delta_symbol(symbol: str) -> str:
    """BTC/USDT -> BTCUSD, with the 1000x meme prefixes the feed uses."""
    from data.feed import DataFeed
    base = symbol.split("/")[0].upper()
    base = DataFeed._DELTA_BASE_OVERRIDE.get(base, base)
    return f"{base}USD"


def _cache_path(symbol: str, tf: str) -> Path:
    return CANDLE_DIR / f"{symbol.replace('/', '_')}_{tf}.csv"


def fetch_candles(symbol: str, tf: str, days: int) -> pd.DataFrame:
    import requests
    bar_sec = TF_SECONDS[tf]
    now = int(time.time())
    oldest = now - days * 86400
    rows: List[dict] = []
    cursor_end = now
    while cursor_end > oldest:
        cursor_start = max(oldest, cursor_end - CHUNK * bar_sec)
        r = requests.get(
            DELTA_CANDLES_URL,
            params={"resolution": tf, "symbol": delta_symbol(symbol),
                    "start": cursor_start, "end": cursor_end},
            timeout=30,
        )
        r.raise_for_status()
        rows.extend(r.json().get("result", []))
        cursor_end = cursor_start - 1
        time.sleep(0.15)
    if not rows:
        raise RuntimeError(f"Delta returned no {tf} candles for {symbol}")
    df = pd.DataFrame(rows).drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    out = pd.DataFrame({
        "timestamp": df["time"].astype(np.int64) * 1000,
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": df["close"].astype(float),
        "volume": df["volume"].astype(float),
    })
    out.insert(0, "datetime", pd.to_datetime(out["timestamp"], unit="ms", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S"))
    # Never keep a bar that hasn't closed yet.
    out = out[out["timestamp"] + bar_sec * 1000 <= now * 1000].reset_index(drop=True)
    return out


def load_candles(symbol: str, tf: str, days: int, refresh: bool) -> pd.DataFrame:
    path = _cache_path(symbol, tf)
    if path.exists() and not refresh:
        df = pd.read_csv(path)
        logger.info("%s %s: %d cached bars (%s -> %s)", symbol, tf, len(df), df["datetime"].iloc[0], df["datetime"].iloc[-1])
        return df
    logger.info("%s %s: fetching %d days from Delta ...", symbol, tf, days)
    df = fetch_candles(symbol, tf, days)
    CANDLE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    logger.info("%s %s: %d bars fetched (%s -> %s)", symbol, tf, len(df), df["datetime"].iloc[0], df["datetime"].iloc[-1])
    return df


# ---------------------------------------------------------------------------
# Live-code adapters
# ---------------------------------------------------------------------------

def load_regime_routing_names() -> Dict[str, List[str]]:
    """REGIME_SCANNER_ROUTING is a local dict inside ScalpStrategy.analyze();
    pull it out of the source by AST so the lab reads the live table rather
    than a hand-copied one — the same 'single source' rule the dashboard's
    own _regime_routing_names mirror follows."""
    from strategies.scalp_strategy import ScalpStrategy
    # The method source is indented one level; a multi-line string inside it
    # has column-0 content, so dedent() can't strip it — re-wrap in a class.
    tree = ast.parse("class _Src:\n" + inspect.getsource(ScalpStrategy.analyze))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "REGIME_SCANNER_ROUTING" for t in node.targets
        ) and isinstance(node.value, ast.Dict):
            out: Dict[str, List[str]] = {}
            for k, v in zip(node.value.keys, node.value.values):
                if not isinstance(k, ast.Constant) or not isinstance(v, ast.List):
                    continue
                names = []
                for el in v.elts:
                    if isinstance(el, ast.Attribute) and el.attr.startswith("_scan_"):
                        names.append(el.attr[len("_scan_"):])
                out[str(k.value)] = names
            return out
    raise RuntimeError("REGIME_SCANNER_ROUTING not found in ScalpStrategy.analyze()")


def discover_scanners(strat) -> Dict[str, Any]:
    out = {}
    for attr in sorted(dir(strat)):
        if attr.startswith("_scan_") and callable(getattr(strat, attr, None)):
            out[attr[len("_scan_"):]] = getattr(strat, attr)
    return out


def fee_drag_r(fm, symbol: str, notional: float, sl_pct: float,
               within_scalper: bool = True, order_type: str = "maker") -> float:
    """Pure replica of SignalTracker.get_min_viable_move()'s fee_drag_r so the
    lab can report what the live fee gate (hard block at > 0.8) would do,
    without constructing a SignalTracker (which loads storage state)."""
    coin = symbol.split("/")[0].upper() if "/" in symbol else symbol[:3].upper()
    liq = {"BTC": 1.0, "ETH": 1.0, "SOL": 1.5, "AVAX": 1.5, "LINK": 1.5, "XRP": 1.5,
           "ADA": 1.5, "DOGE": 2.5, "SHIB": 2.5, "PEPE": 2.5, "WIF": 2.5, "BONK": 2.5}.get(coin, 1.5)
    entry_base_slip = 0.02 if within_scalper else 0.05
    excess = max(0.0, notional - 500.0)
    entry_slip = min((entry_base_slip + (excess / 1000.0) * 0.01) * liq, 0.15)
    exit_slip = min((0.05 + (excess / 1000.0) * 0.01) * liq, 0.15)
    entry_fee = fm.side_pct("maker" if order_type in ("maker", "auto") else "taker", symbol)
    exit_fee = 0.0 if (fm.free_exit or (within_scalper and fm.scalper_offer)) else fm.side_pct("taker", symbol)
    total = entry_fee + exit_fee + entry_slip + exit_slip
    return total / sl_pct if sl_pct > 0 else 999.0


# ---------------------------------------------------------------------------
# Trade simulation (pure, unit-tested)
# ---------------------------------------------------------------------------

def simulate_trade(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                   start_idx: int, side: str, stop: float, tp: float,
                   max_hold_bars: int) -> Tuple[int, float, str]:
    """Walk bars from start_idx (the entry bar) forward. Returns
    (exit_idx, exit_price, reason) with reason in {stop, tp1, max_hold, eod}.
    Stop is checked before TP on a bar that touches both (conservative)."""
    n = len(closes)
    last = min(n - 1, start_idx + max_hold_bars - 1)
    for i in range(start_idx, last + 1):
        if side == "long":
            if lows[i] <= stop:
                return i, stop, "stop"
            if highs[i] >= tp:
                return i, tp, "tp1"
        else:
            if highs[i] >= stop:
                return i, stop, "stop"
            if lows[i] <= tp:
                return i, tp, "tp1"
    if last < start_idx + max_hold_bars - 1:
        return last, float(closes[last]), "eod"
    return last, float(closes[last]), "max_hold"


def excursions(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, start_idx: int,
               side: str, entry: float, risk: float, max_hold_bars: int
               ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-bar favourable / adverse excursion and close, all in R, over the
    hold window starting at the entry bar. fav[k] is how far the best price
    of bar k got in the trade's favour; adv[k] how far the worst price went
    against it (both >= 0 means beyond entry)."""
    last = min(len(closes) - 1, start_idx + max_hold_bars - 1)
    h = highs[start_idx:last + 1]
    l = lows[start_idx:last + 1]
    c = closes[start_idx:last + 1]
    if side == "long":
        return (h - entry) / risk, (entry - l) / risk, (c - entry) / risk
    return (entry - l) / risk, (h - entry) / risk, (entry - c) / risk


def exit_models(fav: np.ndarray, adv: np.ndarray, close_r: np.ndarray,
                tp1_r: float, tp2_r: float, trail_mult: float = 1.0) -> Dict[str, float]:
    """Outcome in R of the SAME entry under alternative exit rules, each
    with the initial stop at -1R and the stop checked before targets on a
    bar that touches both (conservative, matches simulate_trade):

      tp1     first touch of the live TP1 (what the headline table uses)
      r1/r2/r3 first touch of a fixed 1R / 2R / 3R target
      ladder  live-style: 35% at TP1 (stop -> breakeven), 35% at TP2, the
              30% runner trailed at (peak - trail_mult) R after TP1 — the
              HOLD-profile chandelier shape from bot/signal_tracker.py
              TRADE_TYPE_CONFIG (35/35/30 legs, chandelier_mult ~1.0)
      trail   no target: stop trails at (peak - trail_mult) R once MFE >= 1R
      hold    just the close at the end of the window
    Unresolved paths settle at the window's final close."""
    n = len(fav)
    out: Dict[str, float] = {}

    def first_touch(target: float) -> float:
        for k in range(n):
            if adv[k] >= 1.0:
                return -1.0
            if fav[k] >= target:
                return target
        return float(close_r[-1])

    out["tp1"] = first_touch(tp1_r)
    out["r1"] = first_touch(1.0)
    out["r2"] = first_touch(2.0)
    out["r3"] = first_touch(3.0)

    # ladder
    stop_r, remaining, result, peak = -1.0, 1.0, 0.0, 0.0
    hit1 = hit2 = False
    for k in range(n):
        if adv[k] >= -stop_r:
            result += remaining * stop_r
            remaining = 0.0
            break
        peak = max(peak, float(fav[k]))
        if not hit1 and fav[k] >= tp1_r:
            result += 0.35 * tp1_r
            remaining -= 0.35
            hit1 = True
            stop_r = max(stop_r, 0.0)
        if hit1 and not hit2 and fav[k] >= tp2_r:
            result += 0.35 * tp2_r
            remaining -= 0.35
            hit2 = True
        if hit1:
            stop_r = max(stop_r, peak - trail_mult)
    if remaining > 0:
        result += remaining * float(close_r[-1])
    out["ladder"] = result

    # trail only
    stop_r, peak, res = -1.0, 0.0, None
    for k in range(n):
        if adv[k] >= -stop_r:
            res = stop_r
            break
        peak = max(peak, float(fav[k]))
        if peak >= 1.0:
            stop_r = max(stop_r, peak - trail_mult)
    out["trail"] = float(close_r[-1]) if res is None else res

    out["hold"] = float(close_r[-1])
    out["mfe"] = float(fav.max()) if n else 0.0
    out["mae"] = float(adv.max()) if n else 0.0
    return out


def fee_pnl(fm, symbol: str, side: str, entry: float, exit_price: float,
            hold_sec: float, notional: float, risk: float) -> Dict[str, float]:
    """Same arithmetic as bot/signal_tracker._calc_pnl for a single full exit:
    gross % move, fees as % of entry notional (Scalper Offer credited per leg
    by elapsed time), pnl_usd = notional x net_pct, exit_r vs initial risk."""
    from execution.fees import FeeLeg
    gross_pct = ((exit_price - entry) / entry * 100.0) if side == "long" else ((entry - exit_price) / entry * 100.0)
    fees = fm.trade_fees(entry, entry_liquidity="taker",
                         exit_legs=[FeeLeg(1.0, exit_price, "taker", elapsed_sec=hold_sec)],
                         symbol=symbol)
    net_pct = gross_pct - fees.total_pct
    return {
        "gross_pct": round(gross_pct, 5),
        "fee_pct": round(fees.total_pct, 5),
        "net_pct": round(net_pct, 5),
        "pnl_usd": round(notional * net_pct / 100.0, 2),
        "fees_usd": round(notional * fees.total_pct / 100.0, 2),
        "exit_r": round((entry * net_pct / 100.0) / risk, 4) if risk > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

@dataclass
class _Open:
    name: str
    side: str
    entry_idx: int
    entry: float
    stop: float
    tp: float
    risk: float
    regime: str
    routed: bool
    fee_drag: float
    signal_time: str
    exit_idx: int = -1
    exit_price: float = 0.0
    reason: str = ""
    tp1_r: float = 1.5
    tp2_r: float = 2.5
    alt: Optional[Dict[str, float]] = None


def replay_timeframe(strat, fm, symbol: str, tf: str, primary_raw: pd.DataFrame,
                     confirm_raw: pd.DataFrame, routing: Dict[str, List[str]],
                     notional: float, max_hold_bars: int,
                     sl_mode: str = "live", sl_atr_mult: float = 1.5,
                     ) -> Tuple[Dict[str, Any], Dict[str, list], Dict[str, list]]:
    """Returns (results, trades_capped, trades_full).

    sl_mode "live": stop/TP1/TP2 from ScalpStrategy._build_signal (the live
    0.55-0.95%-of-price clamp + per-scanner tp_rr). "atr": research knob —
    stop = sl_atr_mult x ATR of the traded timeframe, TP1/TP2 at the same
    per-scanner tp_rr multiples of that risk; no % clamp, no liq check.
    """
    from data.structure import build_structure_map
    from strategies.regime import MarketRegimeDetector

    bar_sec = TF_SECONDS[tf]
    ctf = CONFIRM_TF[tf]
    # Live frames carry a datetime `timestamp` column (data/manager.py:66);
    # structure_bounce's UTC session gate does pd.to_datetime(ts).hour on it,
    # which would read a raw ms integer as nanoseconds (1970 -> hour 0 ->
    # the scanner never fires). Keep the ms ints for alignment, hand the
    # scanners the same dtype live gives them.
    p_ts_ms = primary_raw["timestamp"].values.astype(np.int64)
    c_ts_ms = confirm_raw["timestamp"].values.astype(np.int64)
    p_in = primary_raw.copy()
    c_in = confirm_raw.copy()
    p_in["timestamp"] = pd.to_datetime(p_ts_ms, unit="ms", utc=True)
    c_in["timestamp"] = pd.to_datetime(c_ts_ms, unit="ms", utc=True)
    p = strat._compute_indicators(p_in).reset_index(drop=True)
    c = strat._compute_indicators(c_in).reset_index(drop=True)
    n = len(p)

    p_close_ms = p_ts_ms + bar_sec * 1000
    c_close_ms = c_ts_ms + TF_SECONDS[ctf] * 1000
    # confirm index usable at each primary close: last confirm bar already closed
    j_for_i = np.searchsorted(c_close_ms, p_close_ms, side="right") - 1

    highs = p["high"].values.astype(float)
    lows = p["low"].values.astype(float)
    closes = p["close"].values.astype(float)
    opens = p["open"].values.astype(float)
    atrs = p["atr"].values.astype(float)
    times = p["datetime"].astype(str).values
    c_atr = c["atr"].values.astype(float)
    c_atr_sma20 = pd.Series(c_atr).rolling(20).mean().values

    scanners = discover_scanners(strat)
    detector = MarketRegimeDetector()

    stats = {name: {"fires": 0, "routed_fires": 0, "build_rejected": 0, "errors": 0,
                    "fwd": {k: [] for k in FWD_HORIZONS}} for name in scanners}
    trades: Dict[str, list] = {name: [] for name in scanners}
    open_by: Dict[str, _Open] = {}
    pending: Dict[str, _Open] = {}

    last_j = -1
    regime = "sideways"
    htf_bias = confirm_bias = 0
    start = max(60, int(np.argmax(j_for_i >= 100)) if (j_for_i >= 100).any() else 60)
    t0 = time.time()
    logger.info("%s %s: replaying %d bars (from %d), confirm=%s", symbol, tf, n, start, ctf)

    for i in range(start, n):
        # 1) pending entries fill at this bar's open; the exit is resolved
        #    once here (bar-by-bar walk from the entry bar) and the trade
        #    stays "open" — blocking that scanner — until the exit bar.
        for name, po in list(pending.items()):
            po.entry_idx = i
            po.entry = float(opens[i])
            if po.side == "long":
                po.stop, po.tp = po.entry - po.risk, po.entry + po.tp
            else:
                po.stop, po.tp = po.entry + po.risk, po.entry - po.tp
            po.exit_idx, po.exit_price, po.reason = simulate_trade(
                highs, lows, closes, i, po.side, po.stop, po.tp, max_hold_bars)
            fav, adv, close_r = excursions(highs, lows, closes, i, po.side, po.entry, po.risk, max_hold_bars)
            po.alt = exit_models(fav, adv, close_r, po.tp1_r, po.tp2_r)
            open_by[name] = po
            del pending[name]

        # 2) settle trades whose exit bar is this bar
        for name, o in list(open_by.items()):
            if o.exit_idx > i:
                continue
            hold_bars = o.exit_idx - o.entry_idx + 1
            res = fee_pnl(fm, symbol, o.side, o.entry, o.exit_price, hold_bars * bar_sec, notional, o.risk)
            # fee in R for the alternative exits (same round trip, same hold
            # credit as the headline exit — an approximation for the ladder)
            fee_r = (o.entry * res["fee_pct"] / 100.0) / o.risk if o.risk > 0 else 0.0
            # same trade with a maker (resting limit) entry — the fee lever
            from execution.fees import FeeLeg as _FeeLeg
            _mk = fm.trade_fees(o.entry, entry_liquidity="maker",
                                exit_legs=[_FeeLeg(1.0, o.exit_price, "taker", elapsed_sec=hold_bars * bar_sec)],
                                symbol=symbol)
            fee_r_maker = (o.entry * _mk.total_pct / 100.0) / o.risk if o.risk > 0 else 0.0
            alt = {k: round(v, 4) for k, v in (o.alt or {}).items()}
            trades[name].append({
                "signal_time": o.signal_time, "entry_time": times[o.entry_idx], "side": o.side,
                "entry": round(o.entry, 4), "exit": round(o.exit_price, 4), "reason": o.reason,
                "r": res["exit_r"], "pnl_usd": res["pnl_usd"], "fees_usd": res["fees_usd"],
                "hold_bars": hold_bars, "regime": o.regime, "routed": o.routed,
                "fee_drag_r": round(o.fee_drag, 3), "fee_r": round(fee_r, 4),
                "fee_r_maker": round(fee_r_maker, 4),
                "risk_pct": round(o.risk / o.entry * 100.0, 4),
                "hour": int(times[o.entry_idx][11:13]) if len(times[o.entry_idx]) >= 13 else -1,
                "alt": alt,
            })
            del open_by[name]

        # 3) confirm-frame context, refreshed when the confirm bar advances
        j = int(j_for_i[i])
        if j != last_j and j >= 0:
            lo = max(0, j - CONTEXT_BARS + 1)
            cslice = c.iloc[lo:j + 1]
            catr = float(c_atr[j]) if not np.isnan(c_atr[j]) else float(atrs[i])
            strat._confirm_atr = catr if catr > 0 else 0.0
            strat._confirm_atr_series = c["atr"].iloc[:j + 1]
            sma = c_atr_sma20[j]
            strat._atr_ratio = (catr / sma) if (sma and not np.isnan(sma) and sma > 0) else 1.0
            try:
                strat._structure_map = build_structure_map(cslice, float(closes[i]), catr) if catr > 0 else None
            except Exception:
                strat._structure_map = None
            htf_bias = strat._get_htf_bias(cslice)
            confirm_bias = htf_bias
            try:
                regime = detector.detect_regime(cslice).regime.value if len(cslice) >= 100 else "sideways"
            except Exception:
                regime = "sideways"
            last_j = j

        allowed = routing.get(regime, [])
        pslice = p.iloc[:i + 1]

        # 4) run every scanner on the closed bar
        for name, fn in scanners.items():
            if name in open_by or name in pending:
                continue
            try:
                setup = fn(symbol, pslice, htf_bias, confirm_bias)
            except Exception:
                stats[name]["errors"] += 1
                continue
            if setup is None:
                continue
            st = stats[name]
            st["fires"] += 1
            routed = name in allowed
            if routed:
                st["routed_fires"] += 1
            side = "long" if setup.side.value == "long" else "short"
            for k, h in FWD_HORIZONS.items():
                if i + h < n and atrs[i] > 0:
                    move = (closes[i + h] - closes[i]) if side == "long" else (closes[i] - closes[i + h])
                    st["fwd"][k].append(move / atrs[i])
            scanner_exits = strat._calibrated_sl_tp.get(name, {})
            strat._active_scanner_exits = scanner_exits
            if sl_mode == "atr":
                if atrs[i] <= 0 or np.isnan(atrs[i]):
                    st["build_rejected"] += 1
                    continue
                risk = float(atrs[i]) * sl_atr_mult
                tp_dist = risk * float(scanner_exits.get("tp1_rr", strat.tp1_rr))
                tp2_dist = risk * float(scanner_exits.get("tp2_rr", strat.tp2_rr))
                ref_entry = float(closes[i])
            elif sl_mode == "pattern":
                # research: the scanner's own invalidation level, unclamped
                ref_entry = float(setup.entry_price) if setup.entry_price > 0 else float(closes[i])
                sl = float(getattr(setup, "stop_loss", 0.0) or 0.0)
                ok = sl > 0 and ((setup.side.value == "long" and sl < ref_entry) or (setup.side.value != "long" and sl > ref_entry))
                if not ok:
                    st["build_rejected"] += 1
                    continue
                risk = abs(ref_entry - sl)
                tp_dist = risk * float(scanner_exits.get("tp1_rr", strat.tp1_rr))
                tp2_dist = risk * float(scanner_exits.get("tp2_rr", strat.tp2_rr))
            else:
                try:
                    sig = strat._build_signal(symbol, setup, htf_bias, primary_df=pslice, regime=regime)
                except Exception:
                    sig = None
                if sig is None:
                    st["build_rejected"] += 1
                    continue
                risk = abs(float(sig.entry_price) - float(sig.stop_loss))
                if risk <= 0 or sig.entry_price <= 0:
                    st["build_rejected"] += 1
                    continue
                tp_dist = abs(float(sig.take_profits[0]) - float(sig.entry_price)) if sig.take_profits else risk * 1.5
                tp2_dist = abs(float(sig.take_profits[1]) - float(sig.entry_price)) if len(sig.take_profits) > 1 else risk * 2.5
                ref_entry = float(sig.entry_price)
            sl_pct = risk / ref_entry * 100.0
            if i + 1 >= n:
                continue
            pending[name] = _Open(name=name, side=side, entry_idx=i + 1, entry=0.0, stop=0.0,
                                  tp=tp_dist, risk=risk, regime=regime, routed=routed,
                                  fee_drag=fee_drag_r(fm, symbol, notional, sl_pct),
                                  signal_time=times[i], tp1_r=tp_dist / risk, tp2_r=tp2_dist / risk)
        if (i - start) % 5000 == 0 and i > start:
            logger.info("  %s %s: %d/%d bars (%.0fs)", symbol, tf, i, n, time.time() - t0)

    # trades still open at the end of data are dropped (unresolved)
    results = {}
    full_trades = {name: list(tl) for name, tl in trades.items()}
    for name in scanners:
        tl = trades[name]
        st = stats[name]
        rs = np.array([t["r"] for t in tl], dtype=float)
        pnl = np.array([t["pnl_usd"] for t in tl], dtype=float)
        fees = np.array([t["fees_usd"] for t in tl], dtype=float)
        longs = [t for t in tl if t["side"] == "long"]
        shorts = [t for t in tl if t["side"] == "short"]
        total_r = float(rs.sum()) if len(rs) else 0.0
        top5 = float(np.sort(rs)[-5:].sum()) if len(rs) else 0.0
        monthly: Dict[str, Dict[str, float]] = {}
        for t in tl:
            m = t["entry_time"][:7]
            mm = monthly.setdefault(m, {"month": m, "n": 0, "r": 0.0, "pnl_usd": 0.0})
            mm["n"] += 1
            mm["r"] = round(mm["r"] + t["r"], 3)
            mm["pnl_usd"] = round(mm["pnl_usd"] + t["pnl_usd"], 2)
        results[name] = {
            "fires": st["fires"], "routed_fires": st["routed_fires"],
            "build_rejected": st["build_rejected"], "errors": st["errors"],
            "trades": len(tl), "wins": int((rs > 0).sum()) if len(rs) else 0,
            "win_rate": round(float((rs > 0).mean() * 100), 1) if len(rs) else None,
            "avg_r": round(float(rs.mean()), 3) if len(rs) else None,
            "median_r": round(float(np.median(rs)), 3) if len(rs) else None,
            "total_r": round(total_r, 2),
            "pnl_usd": round(float(pnl.sum()), 2) if len(pnl) else 0.0,
            "fees_usd": round(float(fees.sum()), 2) if len(fees) else 0.0,
            "avg_hold_bars": round(float(np.mean([t["hold_bars"] for t in tl])), 1) if tl else None,
            "long": {"n": len(longs), "r": round(sum(t["r"] for t in longs), 2), "pnl_usd": round(sum(t["pnl_usd"] for t in longs), 2)},
            "short": {"n": len(shorts), "r": round(sum(t["r"] for t in shorts), 2), "pnl_usd": round(sum(t["pnl_usd"] for t in shorts), 2)},
            "fee_blocked": int(sum(1 for t in tl if t["fee_drag_r"] > 0.8)),
            "top5_share": round(top5 / total_r, 2) if total_r > 0 and len(rs) >= 5 else None,
            "exit_reasons": {r: int(sum(1 for t in tl if t["reason"] == r)) for r in ("stop", "tp1", "max_hold", "eod")},
            "monthly": sorted(monthly.values(), key=lambda x: x["month"]),
            "fwd_atr": {k: (round(float(np.mean(v)), 3) if v else None) for k, v in st["fwd"].items()},
            "learning_only": name in LEARNING_ONLY,
            "diag": _diagnose(tl, notional),
        }
        trades[name] = tl[-TRADES_KEPT:]
    logger.info("%s %s: done in %.0fs", symbol, tf, time.time() - t0)
    return results, trades, full_trades


EXIT_MODELS = ("tp1", "r1", "r2", "r3", "ladder", "trail", "hold")


def _diagnose(tl: list, notional: float) -> Dict[str, Any]:
    """Per-scanner diagnosis: the same entries under every exit model
    (net of the trade's own fee_r), excursion stats, and net-R splits by
    regime / side / routed / UTC hour bucket (headline tp1 exit)."""
    if not tl:
        return {}
    exits: Dict[str, Any] = {}
    for m in EXIT_MODELS:
        net = np.array([t["alt"].get(m, 0.0) - t["fee_r"] for t in tl if t.get("alt")], dtype=float)
        if len(net) == 0:
            continue
        # $: R -> % of entry via each trade's own risk_pct
        usd = sum((t["alt"].get(m, 0.0) - t["fee_r"]) * t["risk_pct"] / 100.0 * notional
                  for t in tl if t.get("alt"))
        exits[m] = {"total_r": round(float(net.sum()), 2), "avg_r": round(float(net.mean()), 3),
                    "win_rate": round(float((net > 0).mean() * 100), 1), "net_usd": round(usd, 0)}
    mfe = np.array([t["alt"]["mfe"] for t in tl if t.get("alt")], dtype=float)
    mae = np.array([t["alt"]["mae"] for t in tl if t.get("alt")], dtype=float)
    exc = {
        "median_mfe_r": round(float(np.median(mfe)), 3), "mean_mfe_r": round(float(mfe.mean()), 3),
        "p_mfe_ge_1r": round(float((mfe >= 1.0).mean() * 100), 1),
        "p_mfe_ge_1_5r": round(float((mfe >= 1.5).mean() * 100), 1),
        "p_mfe_ge_2r": round(float((mfe >= 2.0).mean() * 100), 1),
        "median_mae_r": round(float(np.median(mae)), 3),
        "p_stopped_first": round(float(np.mean([1.0 if t["alt"]["r1"] == -1.0 else 0.0 for t in tl if t.get("alt")]) * 100), 1),
        "median_fee_r": round(float(np.median([t["fee_r"] for t in tl])), 3),
        "median_risk_pct": round(float(np.median([t["risk_pct"] for t in tl])), 3),
    }

    def split(keyf):
        g: Dict[str, list] = {}
        for t in tl:
            g.setdefault(str(keyf(t)), []).append(t["r"])
        return {k: {"n": len(v), "total_r": round(float(sum(v)), 2), "avg_r": round(float(np.mean(v)), 3)}
                for k, v in sorted(g.items())}

    def hour_bucket(t):
        h = t.get("hour", -1)
        if h < 0:
            return "?"
        return f"{(h // 6) * 6:02d}-{(h // 6) * 6 + 6:02d}"

    best = max(exits.items(), key=lambda kv: kv[1]["total_r"])[0] if exits else None
    return {"exits": exits, "best_exit": best, "excursion": exc,
            "by_regime": split(lambda t: t["regime"]), "by_side": split(lambda t: t["side"]),
            "by_routed": split(lambda t: "routed" if t["routed"] else "unrouted"),
            "by_hour_utc": split(hour_bucket)}


def print_diagnosis(path: Path) -> None:
    data = json.loads(path.read_text())
    for tf, res in data["results"].items():
        print(f"\n[{tf}] DIAGNOSIS — net R per exit model (fees included), excursion stats")
        print(f"{'scanner':22s} {'tr':>5s} {'medMFE':>6s} {'P>=1R':>5s} {'P>=1.5':>6s} {'stop1st':>7s} {'fee_r':>5s} | "
              f"{'tp1':>7s} {'r1':>7s} {'r2':>7s} {'r3':>7s} {'ladder':>7s} {'trail':>7s} {'hold':>7s} | best")
        rows = [(n, r) for n, r in res.items() if r.get("diag") and r["trades"] >= 15 and not r["learning_only"]]
        rows.sort(key=lambda kv: -max(e["total_r"] for e in kv[1]["diag"]["exits"].values()))
        for n, r in rows:
            d = r["diag"]; e = d["exits"]; x = d["excursion"]
            cells = " ".join(f"{e[m]['total_r']:7.1f}" if m in e else f"{'—':>7s}" for m in EXIT_MODELS)
            print(f"{n:22s} {r['trades']:5d} {x['median_mfe_r']:6.2f} {x['p_mfe_ge_1r']:5.0f} {x['p_mfe_ge_1_5r']:6.0f} "
                  f"{x['p_stopped_first']:7.0f} {x['median_fee_r']:5.2f} | {cells} | {d['best_exit']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _fee_model_from_settings():
    """Build the FeeModel from the raw `fees:` block of config/settings.yaml.

    Verified 2026-09-17: the Config dataclass (config/__init__.py) has no
    `fees` field, so get_config() drops that whole block and the bot's
    shared get_fee_model() silently falls back to defaults — Scalper Offer
    OFF — regardless of what settings.yaml says. The lab measures against
    what the config *intends* (offer enabled per yaml) and records which
    source it used; the live discrepancy is logged in
    docs/SCANNER_CLUSTER_ANALYSIS_TODO_20260915.md as a sign-off item.
    """
    from execution.fees import FeeModel, get_fee_model
    try:
        import yaml
        raw = yaml.safe_load((PROJECT_ROOT / "config" / "settings.yaml").read_text()) or {}
        if raw.get("fees"):
            return FeeModel.from_config({"fees": raw["fees"]}), "settings.yaml fees: block"
    except Exception as e:
        logger.warning("could not read settings.yaml fees block (%s); using get_fee_model()", e)
    return get_fee_model(), "get_fee_model() defaults"


def run(symbol: str, timeframes: List[str], days: int, margin: float, leverage: float,
        max_hold_bars: int, refresh: bool, sl_mode: str = "live", sl_atr_mult: float = 1.5,
        tag: str = "") -> Path:
    from strategies.scalp_strategy import ScalpStrategy

    logging.getLogger("strategies.scalp_strategy").setLevel(logging.WARNING)
    logging.getLogger("strategies.regime").setLevel(logging.ERROR)
    logging.getLogger("data.structure").setLevel(logging.WARNING)

    notional = margin * leverage
    fm, fee_source = _fee_model_from_settings()
    logger.info("fee model (%s): %s", fee_source, fm.describe())
    strat = ScalpStrategy({})
    strat._is_learning = False
    # The liquidation-buffer check inside _build_signal uses the live
    # confidence->leverage map; the lab measures at the user's leverage.
    strat.leverage_map = {0: int(leverage)}
    routing = load_regime_routing_names()

    needed = set(timeframes) | {CONFIRM_TF[tf] for tf in timeframes}
    frames = {tf: load_candles(symbol, tf, days, refresh) for tf in sorted(needed, key=lambda t: TF_SECONDS[t])}

    results: Dict[str, Any] = {}
    trades: Dict[str, Any] = {}
    date_range: Dict[str, Any] = {}
    all_rows: List[dict] = []
    for tf in timeframes:
        res, tl, full = replay_timeframe(strat, fm, symbol, tf, frames[tf], frames[CONFIRM_TF[tf]],
                                         routing, notional, max_hold_bars,
                                         sl_mode=sl_mode, sl_atr_mult=sl_atr_mult)
        results[tf] = res
        # full trade dump (the JSON keeps the last TRADES_KEPT per scanner) —
        # every trade, flat, for what-if filtering offline
        for name, lst in full.items():
            for t in lst:
                row = {k: v for k, v in t.items() if k != "alt"}
                row.update({f"alt_{k}": v for k, v in (t.get("alt") or {}).items()})
                row["tf"] = tf
                row["scanner"] = name
                all_rows.append(row)
        trades[tf] = tl
        date_range[tf] = {"from": str(frames[tf]["datetime"].iloc[0]), "to": str(frames[tf]["datetime"].iloc[-1]),
                          "bars": int(len(frames[tf])), "confirm_tf": CONFIRM_TF[tf]}

    out = {
        "meta": {
            "symbol": symbol, "delta_symbol": delta_symbol(symbol), "timeframes": timeframes,
            "days": days, "date_range": date_range, "margin": margin, "leverage": leverage,
            "notional": notional,
            "fee_model": {"description": fm.describe(), "source": fee_source,
                          "taker_pct": fm.side_pct("taker", symbol),
                          "maker_pct": fm.side_pct("maker", symbol), "scalper_offer": fm.scalper_offer,
                          "scalper_window_sec": fm.scalper_window_sec(symbol)},
            "exit_model": (f"entry at next bar open (taker); exit on first touch of SL or TP1 "
                           f"(stop wins ties) else close after {max_hold_bars} bars; one open trade per "
                           f"scanner; scanners independent (not a portfolio sim); "
                           + (f"SL/TP from ScalpStrategy._build_signal at {leverage:.0f}x" if sl_mode == "live"
                              else f"RESEARCH: stop = {sl_atr_mult}x ATR of the traded timeframe, TP at per-scanner tp_rr" if sl_mode == "atr"
                              else "RESEARCH: stop = the scanner's own stop_loss (unclamped), TP at per-scanner tp_rr")),
            "sl_mode": sl_mode, "sl_atr_mult": sl_atr_mult if sl_mode == "atr" else None, "tag": tag,
            "context_bars": CONTEXT_BARS, "fwd_horizons_bars": FWD_HORIZONS,
            "learning_only": sorted(LEARNING_ONLY),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "results": results,
        "trades": trades,
    }
    # tagged (research-variant) runs go under variants/ so the dashboard's
    # symbol list only ever shows the live-SL run per pair
    out_dir = LAB_DIR / "variants" if tag else LAB_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol.replace('/', '_')}{tag}.json"
    path.write_text(json.dumps(out, indent=1, allow_nan=False, default=_json_default))
    if all_rows:
        csv_path = out_dir / f"{symbol.replace('/', '_')}{tag}_trades.csv"
        pd.DataFrame(all_rows).to_csv(csv_path, index=False)
        logger.info("wrote %d trades to %s", len(all_rows), csv_path)
    return path


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not serializable: {type(o)}")


def print_summary(path: Path) -> None:
    data = json.loads(path.read_text())
    meta = data["meta"]
    print(f"\nScanner Lab — {meta['symbol']}  notional=${meta['notional']:,.0f} "
          f"({meta['margin']:.0f} x {meta['leverage']:.0f}x)  {meta['days']}d")
    for tf, res in data["results"].items():
        dr = meta["date_range"][tf]
        print(f"\n[{tf}]  {dr['from']} -> {dr['to']}  ({dr['bars']} bars, confirm {dr['confirm_tf']})")
        print(f"{'scanner':22s} {'fires':>6s} {'routed':>6s} {'trades':>6s} {'win%':>6s} {'avgR':>7s} {'totR':>8s} {'$pnl':>10s} {'$fees':>8s} {'feeblk':>6s} {'top5':>5s}")
        rows = sorted(res.items(), key=lambda kv: -(kv[1]["pnl_usd"] or 0))
        for name, r in rows:
            tag = "*" if r["learning_only"] else " "
            print(f"{tag}{name:21s} {r['fires']:6d} {r['routed_fires']:6d} {r['trades']:6d} "
                  f"{(r['win_rate'] if r['win_rate'] is not None else float('nan')):6.1f} "
                  f"{(r['avg_r'] if r['avg_r'] is not None else float('nan')):7.3f} {r['total_r']:8.2f} "
                  f"{r['pnl_usd']:10,.0f} {r['fees_usd']:8,.0f} {r['fee_blocked']:6d} "
                  f"{(r['top5_share'] if r['top5_share'] is not None else float('nan')):5.2f}")
    print("\n* = learning-only scanner (never trades live)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--timeframes", default="5m,15m,1h")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--margin", type=float, default=1000.0)
    ap.add_argument("--leverage", type=float, default=30.0)
    ap.add_argument("--max-hold-bars", type=int, default=48)
    ap.add_argument("--refresh", action="store_true", help="refetch candles from Delta even if cached")
    ap.add_argument("--sl-mode", choices=("live", "atr", "pattern"), default="live",
                    help="live = ScalpStrategy._build_signal stop/TP; atr = research stop of --sl-atr-mult x ATR(traded tf); "
                         "pattern = the scanner's own stop_loss, unclamped")
    ap.add_argument("--sl-atr-mult", type=float, default=1.5)
    ap.add_argument("--tag", default="", help="suffix for a research-variant run; written under variants/")
    args = ap.parse_args()
    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    for tf in tfs:
        if tf not in CONFIRM_TF:
            ap.error(f"unsupported timeframe {tf}; choose from {sorted(CONFIRM_TF)}")
    if args.sl_mode == "atr" and not args.tag:
        args.tag = f"_atr{args.sl_atr_mult:g}"
    if args.sl_mode == "pattern" and not args.tag:
        args.tag = "_pattern"
    path = run(args.symbol, tfs, args.days, args.margin, args.leverage, args.max_hold_bars, args.refresh,
               sl_mode=args.sl_mode, sl_atr_mult=args.sl_atr_mult, tag=args.tag)
    print_summary(path)
    print_diagnosis(path)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
