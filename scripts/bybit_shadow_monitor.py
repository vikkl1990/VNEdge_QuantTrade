#!/usr/bin/env python3
"""
Bybit Shadow Monitor — Phase 2 independent exit guard.

Each open `exchange='bybit', trade_type='shadow'` trade is monitored against
LIVE Bybit prices (not Delta). When the trade's own SL/TP/trail/time-decay
condition fires based on Bybit price action, this monitor closes it.

Closes:
  - SL hit (price crosses stop_loss)
  - TP1 hit (price reaches first take profit)
  - Trail stop (after peak MFE > 0.4R, ratchet to peak − 0.2R)
  - Time decay (after MAX_HOLD_SEC, close at market)

Delta-source close (Phase 1 cascade) STILL applies as a fallback safety net
via the existing bybit_shadow_simulator daemon — but in practice this monitor
will fire FIRST whenever Bybit price action triggers.

Architecture:
  asyncio.gather(
      ws_task,        # subscribes to Bybit V5 public WS, maintains L2 cache
      monitor_task,   # every POLL_SEC, loops open trades, evaluates exit
  )

systemd: bybit-shadow-monitor.service
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import asyncpg
import websockets

logger = logging.getLogger("bybit_shadow_monitor")

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://vnedge:VnEdge2026db@localhost:5432/vnedge",
)
BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"
POLL_SEC = float(os.environ.get("POLL_SEC", "2"))
MAX_HOLD_SEC = int(os.environ.get("MAX_HOLD_SEC", "1800"))   # 30 min default
TRAIL_TRIGGER_R = float(os.environ.get("TRAIL_TRIGGER_R", "0.40"))
TRAIL_GIVEBACK_R = float(os.environ.get("TRAIL_GIVEBACK_R", "0.20"))
BYBIT_TAKER_FEE_PCT = 0.0006 * 1.18   # 0.0708% incl India GST

SYMBOL_MAP = {
    "BTC/USDT": "BTCUSDT", "ETH/USDT": "ETHUSDT", "SOL/USDT": "SOLUSDT",
    "XRP/USDT": "XRPUSDT", "ADA/USDT": "ADAUSDT", "DOGE/USDT": "DOGEUSDT",
    "DOT/USDT": "DOTUSDT", "LTC/USDT": "LTCUSDT", "AVAX/USDT": "AVAXUSDT",
    "LINK/USDT": "LINKUSDT", "TAO/USDT": "TAOUSDT",
}
INV_SYMBOL_MAP = {v: k for k, v in SYMBOL_MAP.items()}
ACTIVE_BYBIT_SYMS = sorted(set(SYMBOL_MAP.values()))


# ─── Bybit L2 cache ────────────────────────────────────────────
class BybitL2Cache:
    def __init__(self):
        self.bids: Dict[str, float] = {}   # bybit_sym → top bid
        self.asks: Dict[str, float] = {}   # bybit_sym → top ask
        self.last_ts: Dict[str, int] = {}  # bybit_sym → last update ms

    def get(self, internal_sym: str):
        bsym = SYMBOL_MAP.get(internal_sym)
        if not bsym:
            return None, None
        return self.bids.get(bsym), self.asks.get(bsym)

    def is_fresh(self, internal_sym: str, max_age_ms: int = 5000) -> bool:
        bsym = SYMBOL_MAP.get(internal_sym)
        if not bsym or bsym not in self.last_ts:
            return False
        return (time.time() * 1000 - self.last_ts[bsym]) < max_age_ms


async def ws_task(cache: BybitL2Cache, stop: asyncio.Event):
    """Subscribe to Bybit V5 public WS orderbook for ACTIVE_BYBIT_SYMS."""
    backoff = 2
    while not stop.is_set():
        try:
            async with websockets.connect(BYBIT_WS, ping_interval=20, open_timeout=10) as ws:
                await ws.send(json.dumps({
                    "op": "subscribe",
                    "args": [f"orderbook.1.{s}" for s in ACTIVE_BYBIT_SYMS],
                }))
                logger.info("WS connected, subscribed to %d symbols", len(ACTIVE_BYBIT_SYMS))
                backoff = 2
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        d = json.loads(raw)
                        if d.get("op") == "subscribe":
                            continue
                        topic = d.get("topic", "")
                        if not topic.startswith("orderbook"):
                            continue
                        sym = topic.split(".")[-1]
                        data = d.get("data", {})
                        bids = data.get("b") or []
                        asks = data.get("a") or []
                        if bids:
                            try:
                                cache.bids[sym] = float(bids[0][0])
                            except (ValueError, IndexError, TypeError):
                                pass
                        if asks:
                            try:
                                cache.asks[sym] = float(asks[0][0])
                            except (ValueError, IndexError, TypeError):
                                pass
                        cache.last_ts[sym] = int(d.get("ts", time.time() * 1000))
                    except asyncio.TimeoutError:
                        continue
        except Exception as e:
            logger.warning("WS error: %s — reconnecting in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)


# ─── Exit guard ────────────────────────────────────────────────
@dataclass
class TradeState:
    trade_id: str
    symbol: str
    side: str           # 'long' | 'short'
    entry_price: float
    quantity: float
    opened_at_epoch: int
    initial_risk: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    open_fees: float = 0.0
    peak_mfe_r: float = 0.0
    peak_price: float = 0.0
    trail_stop: float = 0.0


@dataclass
class ExitDecision:
    close: bool
    reason: str = ""
    exit_price: float = 0.0


def evaluate_exit(s: TradeState, bid: float, ask: float, now_epoch: int) -> ExitDecision:
    """Pure function — given current Bybit prices + state, decide exit or hold."""
    if bid <= 0 or ask <= 0:
        return ExitDecision(False, "no_l2", 0.0)

    # Long: exit at bid; Short: exit at ask
    is_long = s.side.lower() == "long"
    cur_exit_price = bid if is_long else ask
    risk = s.initial_risk if s.initial_risk > 0 else abs(s.entry_price * 0.005)

    # Update peak MFE in R-multiples
    if is_long:
        mfe_abs = bid - s.entry_price
        if bid > s.peak_price:
            s.peak_price = bid
    else:
        mfe_abs = s.entry_price - ask
        if ask < s.peak_price or s.peak_price == 0:
            s.peak_price = ask
    mfe_r = mfe_abs / risk if risk > 0 else 0
    if mfe_r > s.peak_mfe_r:
        s.peak_mfe_r = mfe_r

    # 1. SL hit
    if s.stop_loss > 0:
        if is_long and bid <= s.stop_loss:
            return ExitDecision(True, "sl_hit", s.stop_loss)
        if not is_long and ask >= s.stop_loss:
            return ExitDecision(True, "sl_hit", s.stop_loss)

    # 2. TP hit
    if s.take_profit > 0:
        if is_long and bid >= s.take_profit:
            return ExitDecision(True, "tp_hit", s.take_profit)
        if not is_long and ask <= s.take_profit:
            return ExitDecision(True, "tp_hit", s.take_profit)

    # 3. Trail stop — after peak MFE > TRAIL_TRIGGER_R, ratchet to peak − giveback
    if s.peak_mfe_r >= TRAIL_TRIGGER_R:
        giveback = TRAIL_GIVEBACK_R * risk
        if is_long:
            new_trail = s.peak_price - giveback
            if new_trail > s.trail_stop:
                s.trail_stop = new_trail
            if s.trail_stop > 0 and bid <= s.trail_stop:
                return ExitDecision(True, "trail_profit", s.trail_stop)
        else:
            new_trail = s.peak_price + giveback
            if s.trail_stop == 0 or new_trail < s.trail_stop:
                s.trail_stop = new_trail
            if s.trail_stop > 0 and ask >= s.trail_stop:
                return ExitDecision(True, "trail_profit", s.trail_stop)

    # 4. Time decay — close after MAX_HOLD_SEC
    age = now_epoch - s.opened_at_epoch
    if age >= MAX_HOLD_SEC:
        return ExitDecision(True, f"time_decay_{int(age/60)}m", cur_exit_price)

    return ExitDecision(False, "hold", 0.0)


# ─── DB helpers ────────────────────────────────────────────────
async def fetch_open_bybit_shadows(pool) -> list:
    """Get OPEN bybit shadow trades + JOIN delta source for SL/TP."""
    async with pool.acquire() as con:
        rows = await con.fetch("""
            SELECT b.id::text AS bid, b.symbol, b.side,
                   b.entry_price, b.quantity, b.fees_usd,
                   EXTRACT(EPOCH FROM b.opened_at)::bigint AS opened_at_epoch,
                   COALESCE((d.metadata::jsonb->>'stop_loss')::float, 0) AS stop_loss,
                   COALESCE((d.metadata::jsonb->>'take_profit')::float, 0) AS take_profit,
                   COALESCE((d.metadata::jsonb->>'initial_risk')::float, 0) AS initial_risk
              FROM user_trades b
              LEFT JOIN user_trades d
                ON d.id::text = (b.metadata::jsonb->>'mirror_of_delta_trade_id')
             WHERE b.exchange = 'bybit'
               AND b.trade_type = 'shadow'
               AND b.closed_at IS NULL
        """)
    return [dict(r) for r in rows]


async def close_bybit_shadow(pool, trade_id: str, exit_price: float, reason: str,
                             entry_price: float, quantity: float, side: str, open_fees: float):
    """Update bybit shadow row with close data."""
    notional_in = entry_price * quantity
    notional_out = exit_price * quantity
    if side.lower() == "long":
        gross = notional_out - notional_in
    else:
        gross = notional_in - notional_out
    close_fee = notional_out * BYBIT_TAKER_FEE_PCT
    new_total_fees = (open_fees or 0) + close_fee
    new_pnl = gross - new_total_fees
    async with pool.acquire() as con:
        await con.execute("""
            UPDATE user_trades
               SET status = 'closed',
                   closed_at = NOW(),
                   exit_price = $1,
                   pnl_usd = $2,
                   fees_usd = $3,
                   metadata = metadata || jsonb_build_object(
                                  'close_via', 'bybit_shadow_monitor_v2',
                                  'close_exit_reason', $4::text
                              )
             WHERE id = $5::uuid
        """, exit_price, new_pnl, new_total_fees, reason, trade_id)
    return new_pnl


# ─── Main monitor loop ─────────────────────────────────────────
async def monitor_task(cache: BybitL2Cache, pool, stop: asyncio.Event):
    states: Dict[str, TradeState] = {}
    while not stop.is_set():
        try:
            opens = await fetch_open_bybit_shadows(pool)
            now_epoch = int(time.time())
            seen_ids = set()
            for r in opens:
                tid = r["bid"]
                seen_ids.add(tid)
                if tid not in states:
                    states[tid] = TradeState(
                        trade_id=tid,
                        symbol=r["symbol"],
                        side=r["side"],
                        entry_price=float(r["entry_price"]),
                        quantity=float(r["quantity"]),
                        opened_at_epoch=int(r["opened_at_epoch"]),
                        initial_risk=float(r["initial_risk"]) if r["initial_risk"] else abs(float(r["entry_price"]) * 0.005),
                        stop_loss=float(r["stop_loss"]) if r["stop_loss"] else 0.0,
                        take_profit=float(r["take_profit"]) if r["take_profit"] else 0.0,
                        open_fees=float(r["fees_usd"] or 0),
                        peak_price=float(r["entry_price"]),
                    )
                s = states[tid]
                if not cache.is_fresh(s.symbol):
                    continue
                bid, ask = cache.get(s.symbol)
                if bid is None or ask is None:
                    continue
                decision = evaluate_exit(s, bid, ask, now_epoch)
                if decision.close:
                    pnl = await close_bybit_shadow(
                        pool, tid, decision.exit_price, decision.reason,
                        s.entry_price, s.quantity, s.side, s.open_fees,
                    )
                    logger.warning(
                        "EXIT %s %s %s reason=%s exit=%.4f pnl=%.4f peak_R=%.2f",
                        s.symbol, s.side, tid[:8], decision.reason, decision.exit_price, pnl, s.peak_mfe_r
                    )
                    states.pop(tid, None)
            # Cleanup stale states
            for k in list(states.keys()):
                if k not in seen_ids:
                    states.pop(k, None)
        except Exception as e:
            logger.error("monitor cycle error: %s", e, exc_info=True)
        await asyncio.sleep(POLL_SEC)


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger.info("BYBIT_SHADOW_MONITOR starting — POLL=%s MAX_HOLD=%ss TRAIL_TRIG=%sR",
                POLL_SEC, MAX_HOLD_SEC, TRAIL_TRIGGER_R)
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)
    cache = BybitL2Cache()
    stop = asyncio.Event()
    try:
        await asyncio.gather(
            ws_task(cache, stop),
            monitor_task(cache, pool, stop),
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
