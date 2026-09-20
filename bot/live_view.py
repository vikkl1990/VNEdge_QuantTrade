"""
Live-view builders for the dashboard (2026-09-20).

Pure functions, no I/O: they turn tracker records, the Delta ticker cache and the
fee model into what a perp terminal shows on a position row, the fee ledger and
the chart overlay. Kept out of dashboard/server.py so they can be unit-tested.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from bot.trade_calculator import calc_liquidation_price

FUNDING_HOURS_UTC = (0, 8, 16)   # Delta settles funding at 00/08/16 UTC


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def seconds_to_next_funding(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    secs_today = now.hour * 3600 + now.minute * 60 + now.second
    for h in FUNDING_HOURS_UTC:
        if h * 3600 > secs_today:
            return h * 3600 - secs_today
    return 24 * 3600 - secs_today


def entry_liquidity_of(t: Dict[str, Any], fm) -> str:
    ft = str(t.get("fee_type") or "")
    if ft.startswith("maker"):
        return "maker"
    if ft.startswith("taker"):
        return "taker"
    try:
        return fm.entry_liquidity(str(t.get("order_type") or "auto"), float(t.get("slippage_bps") or 0.0))
    except Exception:
        return "taker"


def enrich_position(t: Dict[str, Any], mark: Optional[float], meta: Optional[Dict[str, Any]], fm,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything a perp terminal puts on the position row, for one active paper trade."""
    now = now or datetime.now(timezone.utc)
    entry = float(t.get("entry_price") or 0.0)
    side = str(t.get("side") or "long")
    d = 1.0 if side == "long" else -1.0
    price = float(mark or (meta or {}).get("last") or t.get("current_price") or entry)
    notional = float(t.get("position_size_usd") or 0.0)
    margin = float(t.get("paper_stake") or 0.0)
    lev = int(t.get("leverage") or 1) or 1
    remaining = float(t.get("position_remaining_pct") if t.get("position_remaining_pct") is not None else 1.0)
    atr = float(t.get("entry_atr") or t.get("signal_atr") or 0.0)
    risk = float(t.get("initial_risk") or 0.0)
    stop = float(t.get("stop_loss") or 0.0)
    trail = float(t.get("chandelier_stop") or 0.0) if t.get("atr_trail_active") else 0.0
    eff_stop = trail if trail > 0 else stop

    opened = _parse_iso(t.get("entry_time"))
    hold_sec = max(0.0, (now - opened).total_seconds()) if opened else 0.0

    open_leg_pct = d * (price - entry) / entry * 100 if entry > 0 else 0.0
    gross_pct = float(t.get("tp1_pnl_locked") or 0.0) + float(t.get("tp2_pnl_locked") or 0.0) + remaining * open_leg_pct
    gross_usd = notional * gross_pct / 100.0

    entry_liq = entry_liquidity_of(t, fm)
    try:
        entry_fee_usd = notional * fm.side_pct(entry_liq, t.get("symbol")) / 100.0
    except Exception:
        entry_fee_usd = 0.0
    try:
        from execution.fees import FeeLeg
        leg = FeeLeg(remaining, price, liquidity="taker", elapsed_sec=hold_sec)
        exit_free = bool(fm.exit_leg_is_free(leg, t.get("symbol")))
        exit_fee_usd = 0.0 if exit_free else notional * remaining * fm.side_pct("taker", t.get("symbol")) / 100.0
        window = float(fm.scalper_window_sec(t.get("symbol")))
    except Exception:
        exit_free, exit_fee_usd, window = False, 0.0, 0.0
    net_usd = gross_usd - entry_fee_usd - exit_fee_usd

    liq = calc_liquidation_price(entry, lev, side) if entry > 0 else 0.0
    liq_dist_pct = d * (price - liq) / price * 100 if price > 0 and liq > 0 else None
    stop_dist_pct = d * (price - eff_stop) / price * 100 if price > 0 and eff_stop > 0 else None

    fr = (meta or {}).get("funding_rate")
    # Delta reports funding_rate in percent per 8h; a long pays when it is positive.
    funding_est_usd = (-d * notional * float(fr) / 100.0 * hold_sec / (8 * 3600.0)) if fr is not None else None

    return {
        "trade_id": t.get("trade_id"), "symbol": t.get("symbol"), "side": side,
        "mark": price, "index": (meta or {}).get("index"), "basis_pct": (meta or {}).get("basis_pct"),
        "entry": entry, "stop": stop, "trail": trail or None, "effective_stop": eff_stop,
        "tp1": t.get("tp1"), "tp2": t.get("tp2"), "tp3": t.get("tp3"),
        "leverage": lev, "margin_usd": margin, "notional_usd": notional, "remaining_pct": remaining,
        "gross_pct": round(gross_pct, 4), "gross_usd": round(gross_usd, 2),
        "entry_fee_usd": round(entry_fee_usd, 2), "exit_fee_usd": round(exit_fee_usd, 2),
        "net_usd": round(net_usd, 2), "roe_pct": round(net_usd / margin * 100, 2) if margin > 0 else None,
        "r_now": round(d * (price - entry) / risk, 2) if risk > 0 else None,
        "mfe_r": t.get("mfe_r"), "mae_r": t.get("mae_r"),
        "liq_price": liq, "liq_dist_pct": round(liq_dist_pct, 3) if liq_dist_pct is not None else None,
        "liq_dist_atr": round(abs(price - liq) / atr, 2) if atr > 0 and liq > 0 else None,
        "stop_dist_pct": round(stop_dist_pct, 3) if stop_dist_pct is not None else None,
        "stop_dist_atr": round(abs(price - eff_stop) / atr, 2) if atr > 0 and eff_stop > 0 else None,
        "hold_sec": int(hold_sec), "scalper_window_sec": int(window),
        "scalper_remaining_sec": int(max(0.0, window - hold_sec)) if window else 0, "exit_free_now": exit_free,
        "entry_liquidity": entry_liq,
        "funding_rate_8h_pct": fr, "funding_est_usd": round(funding_est_usd, 4) if funding_est_usd is not None else None,
        "funding_charged": False,   # paper account does not debit funding (fees.funding.apply is off)
        "next_funding_sec": seconds_to_next_funding(now),
        "open_interest": (meta or {}).get("open_interest"), "change_24h_pct": (meta or {}).get("change_24h_pct"),
        "breakeven_set": bool(t.get("breakeven_set")), "tp1_hit": bool(t.get("tp1_hit")),
        "setup_type": t.get("setup_type"), "trade_type": t.get("trade_type"), "entry_time": t.get("entry_time"),
        "grade": t.get("grade"), "confidence": t.get("confidence"),
    }


def market_context(meta_by_symbol: Dict[str, Dict[str, Any]], prices: Dict[str, float],
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    rows = []
    for sym in sorted(set(list(meta_by_symbol.keys()) + list(prices.keys()))):
        m = meta_by_symbol.get(sym) or {}
        rows.append({
            "symbol": sym, "last": prices.get(sym, m.get("last")), "mark": m.get("mark"), "index": m.get("index"),
            "basis_pct": m.get("basis_pct"), "funding_rate_8h_pct": m.get("funding_rate"),
            "open_interest": m.get("open_interest"), "oi_value_usd": m.get("oi_value_usd"),
            "turnover_24h": m.get("turnover_24h"), "change_24h_pct": m.get("change_24h_pct"),
            "high_24h": m.get("high_24h"), "low_24h": m.get("low_24h"),
            "age_sec": round(now.timestamp() - m["ts"], 1) if m.get("ts") else None,
        })
    return {"next_funding_sec": seconds_to_next_funding(now), "funding_hours_utc": list(FUNDING_HOURS_UTC),
            "symbols": rows, "ts": now.isoformat()}


def fee_ledger(closed: Iterable[Dict[str, Any]], fm, limit: int = 200) -> Dict[str, Any]:
    """Per-trade cost ledger plus totals. Entry fee is recomputed from the recorded
    entry liquidity; the exit legs are the remainder of the stored total."""
    rows: List[Dict[str, Any]] = []
    for t in closed:
        notional = float(t.get("position_size_usd") or 0.0)
        entry_liq = entry_liquidity_of(t, fm)
        try:
            entry_fee = notional * fm.side_pct(entry_liq, t.get("symbol")) / 100.0
        except Exception:
            entry_fee = 0.0
        total_fee = float(t.get("total_fees_usd") or 0.0)
        gross = float(t.get("gross_pnl_usd") or 0.0)
        net = float(t.get("pnl_usd") or 0.0)
        rows.append({
            "trade_id": t.get("trade_id"), "symbol": t.get("symbol"), "side": t.get("side"),
            "scanner": t.get("setup_type"), "trade_type": t.get("trade_type"),
            "opened": t.get("entry_time"), "closed": t.get("exit_time"),
            "duration_sec": t.get("trade_duration_sec"), "exit_reason": t.get("exit_reason"),
            "notional_usd": notional, "gross_usd": round(gross, 2),
            "entry_fee_usd": round(entry_fee, 2), "exit_fee_usd": round(max(0.0, total_fee - entry_fee), 2),
            "fees_usd": round(total_fee, 2), "fees_pct": t.get("total_fees_pct"),
            "funding_usd": 0.0, "net_usd": round(net, 2),
            "entry_liquidity": entry_liq, "within_scalper": bool(t.get("within_scalper")),
            "fee_r": round(total_fee / float(t["risk_amount_usd"]), 3) if t.get("risk_amount_usd") else None,
        })
    rows.sort(key=lambda r: str(r.get("closed") or ""), reverse=True)
    n = len(rows)
    gross = sum(r["gross_usd"] for r in rows); fees = sum(r["fees_usd"] for r in rows); net = sum(r["net_usd"] for r in rows)
    wins_gross = sum(1 for r in rows if r["gross_usd"] > 0); wins_net = sum(1 for r in rows if r["net_usd"] > 0)
    summary = {
        "n": n, "gross_usd": round(gross, 2), "fees_usd": round(fees, 2), "funding_usd": 0.0, "net_usd": round(net, 2),
        "fee_per_trade_usd": round(fees / n, 2) if n else 0.0,
        "fee_share_of_gross_abs_pct": round(fees / sum(abs(r["gross_usd"]) for r in rows) * 100, 1) if rows and any(r["gross_usd"] for r in rows) else None,
        "maker_entries": sum(1 for r in rows if r["entry_liquidity"] == "maker"),
        "free_exits": sum(1 for r in rows if r["within_scalper"]),
        "gross_win_rate_pct": round(wins_gross / n * 100, 1) if n else 0.0,
        "net_win_rate_pct": round(wins_net / n * 100, 1) if n else 0.0,
        "trades_flipped_by_fees": sum(1 for r in rows if r["gross_usd"] > 0 >= r["net_usd"]),
    }
    return {"summary": summary, "rows": rows[:limit]}


def chart_overlay(df, symbol: str, active: Iterable[Dict[str, Any]], closed: Iterable[Dict[str, Any]],
                  k: float = 2.0, max_levels: int = 6) -> Dict[str, Any]:
    """Trade lines, fill markers and naked structure for the chart page.

    `df` is the candle frame with a millisecond `timestamp` column (RangeIndex).
    """
    import pandas as pd
    from data.market_structure import build_structure

    df = df.reset_index(drop=True)
    times = (df["timestamp"].astype("int64") // 1000).tolist()
    t0, t1 = times[0], times[-1]
    bar_sec = (times[1] - times[0]) if len(times) > 1 else 60
    last_close = float(df["close"].iloc[-1])

    def _snap(iso: str) -> Optional[int]:
        d = _parse_iso(iso)
        if not d:
            return None
        ts = int(d.timestamp())
        ts = ts - (ts - t0) % bar_sec
        return ts if t0 <= ts <= t1 else None

    lines, markers = [], []
    for t in active:
        if t.get("symbol") != symbol:
            continue
        side = t.get("side"); col = "profit" if side == "long" else "loss"
        lines.append({"price": t.get("entry_price"), "label": f"entry {side}", "kind": "entry", "color": col})
        stop = t.get("chandelier_stop") if t.get("atr_trail_active") and t.get("chandelier_stop") else t.get("stop_loss")
        lines.append({"price": stop, "label": "trail" if stop != t.get("stop_loss") else "stop", "kind": "stop", "color": "loss"})
        for i in (1, 2, 3):
            if t.get(f"tp{i}"):
                lines.append({"price": t[f"tp{i}"], "label": f"TP{i}", "kind": "tp", "color": "profit"})
        lev = int(t.get("leverage") or 1) or 1
        if t.get("entry_price"):
            lines.append({"price": calc_liquidation_price(float(t["entry_price"]), lev, side), "label": "liq", "kind": "liq", "color": "warn"})
        ts = _snap(t.get("entry_time"))
        if ts:
            markers.append({"time": ts, "position": "belowBar" if side == "long" else "aboveBar",
                            "shape": "arrowUp" if side == "long" else "arrowDown", "color": col, "text": f"{side} open"})
    for t in closed:
        if t.get("symbol") != symbol:
            continue
        side = t.get("side"); col = "profit" if side == "long" else "loss"
        te, tx = _snap(t.get("entry_time")), _snap(t.get("exit_time"))
        if te:
            markers.append({"time": te, "position": "belowBar" if side == "long" else "aboveBar",
                            "shape": "arrowUp" if side == "long" else "arrowDown", "color": col, "text": str(t.get("setup_type") or "")[:12]})
        if tx:
            pnl = float(t.get("pnl_usd") or 0.0)
            markers.append({"time": tx, "position": "aboveBar" if side == "long" else "belowBar", "shape": "circle",
                            "color": "profit" if pnl > 0 else "loss", "text": f"{pnl:+.2f}"})
    markers.sort(key=lambda m: m["time"])

    structure: Dict[str, Any] = {}
    try:
        st, sw = build_structure(df, k=k)
        zig = [{"time": times[int(r.idx)], "value": float(r.price)} for r in sw.itertuples()]
        naked = []
        for r in sw.itertuples():
            if pd.isna(r.touched_idx) and pd.isna(r.broken_idx):
                naked.append({"price": float(r.price), "kind": "high" if r.kind == "H" else "low",
                              "age_bars": int(len(df) - 1 - r.confirmed_idx), "label": r.label})
        above = sorted([n for n in naked if n["price"] > last_close], key=lambda n: n["price"])[:max_levels]
        below = sorted([n for n in naked if n["price"] < last_close], key=lambda n: -n["price"])[:max_levels]
        ev = st[st["event"] != ""].tail(12)
        events = [{"time": times[int(i)], "event": str(e), "dist_atr": round(float(dd), 2) if not pd.isna(dd) else None}
                  for i, e, dd in zip(ev.index, ev["event"], ev["event_dist_atr"])]
        structure = {"k": k, "state": str(st["state"].iloc[-1]), "zigzag": zig, "naked_levels": above + below,
                     "events": events, "range": {"hi": None if pd.isna(st["range_hi"].iloc[-1]) else float(st["range_hi"].iloc[-1]),
                                                 "lo": None if pd.isna(st["range_lo"].iloc[-1]) else float(st["range_lo"].iloc[-1])},
                     "n_swings": int(len(sw))}
    except Exception as exc:  # structure is decoration; never fail the overlay for it
        structure = {"error": str(exc)[:200]}

    return {"symbol": symbol, "t0": t0, "t1": t1, "bar_sec": bar_sec, "lines": lines, "markers": markers, "structure": structure}
