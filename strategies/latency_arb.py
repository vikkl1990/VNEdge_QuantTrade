"""
Latency Arbitrage Strategy -- Delta India vs Binance
=====================================================
Binance is the price leader for BTC/ETH. Delta India follows with a lag.
When Binance moves significantly and Delta hasn't caught up, we trade on Delta
in the direction of the move, then close when Delta converges.

Edge: Binance price leads Delta by 200ms-2s on volatile moves.
Cost: ~0.14% round-trip (0.05% taker + 0.00% close + 0.06% settlement + ~0.03% slippage)
Minimum dislocation to trade: 0.20% (provides 0.06% net edge minimum)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Dict, List, Optional, Any

import aiohttp
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mapping (matches exchange/delta_ws.py conventions)
# ---------------------------------------------------------------------------

# Delta India WS uses these symbol names on the v2/ticker channel
DELTA_SYMBOL_MAP = {
    "BTC/USDT": "BTCUSD",
    "ETH/USDT": "ETHUSD",
    "SOL/USDT": "SOLUSD",
    "AVAX/USDT": "AVAXUSD",
    "DOGE/USDT": "DOGEUSD",
}

DELTA_REVERSE_MAP = {v: k for k, v in DELTA_SYMBOL_MAP.items()}

# Binance futures bookTicker uses these lowercase symbol names
BINANCE_SYMBOL_MAP = {
    "BTC/USDT": "btcusdt",
    "ETH/USDT": "ethusdt",
    "SOL/USDT": "solusdt",
    "AVAX/USDT": "avaxusdt",
    "DOGE/USDT": "dogeusdt",
}

BINANCE_REVERSE_MAP = {v.upper(): k for k, v in BINANCE_SYMBOL_MAP.items()}


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class PriceSnapshot:
    """Point-in-time price from an exchange."""
    exchange: str          # "binance" or "delta"
    symbol: str            # canonical e.g. "BTC/USDT"
    bid: float
    ask: float
    mid: float             # (bid + ask) / 2
    timestamp: float       # exchange-reported unix seconds (best effort)
    local_recv_ts: float   # time.time() when we received it locally


@dataclass
class Dislocation:
    """Measured price dislocation between two exchanges."""
    symbol: str
    binance_mid: float
    delta_mid: float
    dislocation_pct: float   # (binance_mid - delta_mid) / delta_mid * 100
    dislocation_abs: float   # absolute USD difference
    binance_ts: float
    delta_ts: float
    latency_ms: float        # |local_recv_ts difference| in ms
    direction: str           # "long" if binance > delta, "short" otherwise
    timestamp: float


@dataclass
class LatencyArbSignal:
    """Signal to trade on Delta based on Binance price leadership."""
    symbol: str
    side: str                    # "long" or "short"
    delta_entry_price: float     # current Delta mid (our entry)
    binance_target: float        # where Binance is (Delta should converge here)
    dislocation_pct: float
    expected_profit_pct: float   # dislocation - estimated costs
    confidence: int              # 0-100
    timestamp: float


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class LatencyArbEngine:
    """
    Measures price dislocation between Binance and Delta India.
    Generates signals when dislocation exceeds cost threshold.

    Binance feed: bookTicker (fastest public feed, ~10ms top-of-book updates)
    Delta feed : v2/ticker  (matches existing delta_ws.py channel)
    """

    # WebSocket endpoints
    BINANCE_WS_URL = "wss://fstream.binance.com/ws"
    DELTA_WS_URL = "wss://socket.india.delta.exchange"

    # Thresholds
    MIN_DISLOCATION_PCT = 0.20    # minimum % to consider a trade
    STRONG_DISLOCATION_PCT = 0.35 # high-confidence level
    MAX_DISLOCATION_PCT = 1.5     # too large = probably bad data
    MIN_CONFIDENCE = 60
    CONVERGENCE_TIMEOUT_S = 30

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        on_signal: Optional[Callable[..., Coroutine[Any, Any, None]]] = None,
    ):
        self.symbols = symbols or ["BTC/USDT", "ETH/USDT"]
        self.on_signal = on_signal

        # Latest prices from each exchange
        self._binance_prices: Dict[str, PriceSnapshot] = {}
        self._delta_prices: Dict[str, PriceSnapshot] = {}

        # Dislocation history for analysis (per symbol)
        self._dislocation_history: Dict[str, deque] = {
            s: deque(maxlen=5000) for s in self.symbols
        }

        # Statistics
        self._stats: Dict[str, Any] = {
            "binance_msgs": 0,
            "delta_msgs": 0,
            "dislocations_detected": 0,
            "signals_generated": 0,
            "avg_dislocation_pct": {},
            "max_dislocation_pct": {},
            "avg_latency_ms": {},
            "started_at": None,
        }

        # Measurement mode (no trading, just measuring)
        self._measure_only = True
        self._running = False

        # Cooldown per symbol (prevent rapid-fire signals)
        self._last_signal_time: Dict[str, float] = {}
        self._signal_cooldown_s = 10  # min seconds between signals per symbol

        # aiohttp session (created in start)
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, measure_only: bool = True):
        """Start both WebSocket connections and begin measuring."""
        self._measure_only = measure_only
        self._running = True
        self._stats["started_at"] = time.time()
        self._session = aiohttp.ClientSession()

        logger.info(
            "LatencyArb starting -- symbols=%s, measure_only=%s",
            self.symbols, measure_only,
        )

        # Run both WS connections + dislocation checker concurrently
        await asyncio.gather(
            self._binance_ws_loop(),
            self._delta_ws_loop(),
            self._dislocation_checker(),
        )

    async def stop(self):
        """Signal all loops to stop and close the session."""
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Binance WebSocket (bookTicker -- fastest public feed)
    # ------------------------------------------------------------------

    async def _binance_ws_loop(self):
        """Subscribe to Binance futures bookTicker for all symbols."""
        streams = "/".join(
            f"{BINANCE_SYMBOL_MAP[s]}@bookTicker"
            for s in self.symbols
            if s in BINANCE_SYMBOL_MAP
        )
        url = f"{self.BINANCE_WS_URL}/{streams}"

        while self._running:
            try:
                async with self._session.ws_connect(
                    url, heartbeat=20, timeout=30,
                ) as ws:
                    logger.info("Binance WS connected: %s", url)
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            recv_ts = time.time()
                            data = json.loads(msg.data)
                            self._process_binance_tick(data, recv_ts)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Binance WS error: %s -- reconnecting in 2s", e)
                await asyncio.sleep(2)

    def _process_binance_tick(self, data: dict, recv_ts: float):
        """
        Process Binance bookTicker message.

        Format: {"s": "BTCUSDT", "b": "65000.10", "B": "1.2",
                 "a": "65000.20", "A": "0.8", "T": 1710000000000, ...}
        """
        raw_sym = data.get("s", "")
        bid = float(data.get("b", 0))
        ask = float(data.get("a", 0))

        if bid <= 0 or ask <= 0:
            return

        symbol = BINANCE_REVERSE_MAP.get(raw_sym.upper())
        if not symbol:
            return

        # Binance "T" field is transaction time in milliseconds
        exchange_ts = data.get("T", recv_ts * 1000) / 1000.0

        self._binance_prices[symbol] = PriceSnapshot(
            exchange="binance",
            symbol=symbol,
            bid=bid,
            ask=ask,
            mid=(bid + ask) / 2.0,
            timestamp=exchange_ts,
            local_recv_ts=recv_ts,
        )
        self._stats["binance_msgs"] += 1

    # ------------------------------------------------------------------
    # Delta India WebSocket (v2/ticker -- matches delta_ws.py)
    # ------------------------------------------------------------------

    async def _delta_ws_loop(self):
        """
        Subscribe to Delta India v2/ticker for all symbols.

        Message format (from delta_ws.py):
            {
                "type": "v2/ticker",
                "symbol": "BTCUSD",
                "close": "65000",
                "mark_price": "65001",
                "quotes": {"best_bid": "64999", "best_ask": "65001"},
                "timestamp": <milliseconds>,
                ...
            }
        """
        while self._running:
            try:
                async with self._session.ws_connect(
                    self.DELTA_WS_URL, heartbeat=25, timeout=30,
                ) as ws:
                    logger.info("Delta WS connected: %s", self.DELTA_WS_URL)

                    # Subscribe to v2/ticker for each symbol
                    delta_symbols = [
                        DELTA_SYMBOL_MAP[s]
                        for s in self.symbols
                        if s in DELTA_SYMBOL_MAP
                    ]
                    sub_msg = {
                        "type": "subscribe",
                        "payload": {
                            "channels": [
                                {"name": "v2/ticker", "symbols": delta_symbols}
                            ]
                        },
                    }
                    await ws.send_json(sub_msg)
                    logger.info("Delta WS subscribed to v2/ticker: %s", delta_symbols)

                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            recv_ts = time.time()
                            data = json.loads(msg.data)
                            self._process_delta_tick(data, recv_ts)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Delta WS error: %s -- reconnecting in 2s", e)
                await asyncio.sleep(2)

    def _process_delta_tick(self, data: dict, recv_ts: float):
        """
        Process Delta v2/ticker message.

        Uses the exact same message schema as exchange/delta_ws.py:
        - type == "v2/ticker"
        - symbol in top-level
        - close, mark_price at top level
        - best_bid / best_ask nested under "quotes"
        - timestamp in milliseconds
        """
        if data.get("type") != "v2/ticker":
            return

        delta_sym = data.get("symbol", "")
        symbol = DELTA_REVERSE_MAP.get(delta_sym)
        if not symbol:
            return

        try:
            last = float(data.get("close", 0))
            quotes = data.get("quotes", {})
            bid = float(quotes.get("best_bid", last))
            ask = float(quotes.get("best_ask", last))
        except (ValueError, TypeError):
            return

        if bid <= 0 or ask <= 0:
            return

        # Delta timestamp is in milliseconds
        ts_raw = data.get("timestamp")
        if ts_raw:
            try:
                exchange_ts = float(ts_raw) / 1000.0
            except (ValueError, TypeError):
                exchange_ts = recv_ts
        else:
            exchange_ts = recv_ts

        self._delta_prices[symbol] = PriceSnapshot(
            exchange="delta",
            symbol=symbol,
            bid=bid,
            ask=ask,
            mid=(bid + ask) / 2.0,
            timestamp=exchange_ts,
            local_recv_ts=recv_ts,
        )
        self._stats["delta_msgs"] += 1

    # ------------------------------------------------------------------
    # Dislocation detection
    # ------------------------------------------------------------------

    async def _dislocation_checker(self):
        """Periodically compare prices and detect dislocations."""
        # Wait for initial prices to arrive
        await asyncio.sleep(3)

        while self._running:
            for symbol in self.symbols:
                bp = self._binance_prices.get(symbol)
                dp = self._delta_prices.get(symbol)

                if not bp or not dp:
                    continue

                # Both prices must be fresh (< 2 seconds old)
                now = time.time()
                if now - bp.local_recv_ts > 2.0 or now - dp.local_recv_ts > 2.0:
                    continue

                if dp.mid <= 0:
                    continue

                # Calculate dislocation
                disl_pct = (bp.mid - dp.mid) / dp.mid * 100.0
                disl_abs = bp.mid - dp.mid
                latency_ms = abs(bp.local_recv_ts - dp.local_recv_ts) * 1000.0
                direction = "long" if disl_pct > 0 else "short"

                disl = Dislocation(
                    symbol=symbol,
                    binance_mid=bp.mid,
                    delta_mid=dp.mid,
                    dislocation_pct=disl_pct,
                    dislocation_abs=disl_abs,
                    binance_ts=bp.timestamp,
                    delta_ts=dp.timestamp,
                    latency_ms=latency_ms,
                    direction=direction,
                    timestamp=now,
                )

                self._dislocation_history[symbol].append(disl)

                # Check if dislocation is tradeable
                abs_disl = abs(disl_pct)
                if abs_disl >= self.MIN_DISLOCATION_PCT:
                    self._stats["dislocations_detected"] += 1

                    if abs_disl > self.MAX_DISLOCATION_PCT:
                        logger.warning(
                            "EXTREME dislocation %s: %.3f%% -- skipping (bad data?)",
                            symbol, disl_pct,
                        )
                        continue

                    # Cooldown check
                    last_sig = self._last_signal_time.get(symbol, 0)
                    if now - last_sig < self._signal_cooldown_s:
                        continue

                    # Build confidence
                    confidence = self._calc_confidence(symbol, disl)

                    if confidence >= self.MIN_CONFIDENCE:
                        estimated_cost = 0.14  # % round-trip
                        expected_profit = abs_disl - estimated_cost

                        signal = LatencyArbSignal(
                            symbol=symbol,
                            side=direction,
                            delta_entry_price=dp.mid,
                            binance_target=bp.mid,
                            dislocation_pct=abs_disl,
                            expected_profit_pct=expected_profit,
                            confidence=confidence,
                            timestamp=now,
                        )

                        self._stats["signals_generated"] += 1
                        self._last_signal_time[symbol] = now

                        logger.info(
                            "LATENCY ARB SIGNAL: %s %s | Delta=%.2f Binance=%.2f | "
                            "Disl=%.3f%% | ExpProfit=%.3f%% | Conf=%d",
                            direction.upper(), symbol, dp.mid, bp.mid,
                            abs_disl, expected_profit, confidence,
                        )

                        if not self._measure_only and self.on_signal:
                            await self.on_signal(signal)

            await asyncio.sleep(0.05)  # 50ms check interval

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _calc_confidence(self, symbol: str, disl: Dislocation) -> int:
        """Calculate confidence score (0-100) for a dislocation signal."""
        conf = 50  # base

        abs_disl = abs(disl.dislocation_pct)

        # Dislocation size: bigger = more confident
        if abs_disl >= self.STRONG_DISLOCATION_PCT:
            conf += 20
        elif abs_disl >= 0.25:
            conf += 10

        # Consistency: check if recent dislocations are in the same direction
        history = list(self._dislocation_history.get(symbol, []))[-20:]
        if len(history) >= 5:
            same_dir = sum(
                1 for d in history[-5:] if d.direction == disl.direction
            )
            if same_dir >= 4:
                conf += 15
            elif same_dir >= 3:
                conf += 5

        # Freshness: both prices very recent
        now = time.time()
        bp = self._binance_prices.get(symbol)
        dp = self._delta_prices.get(symbol)
        if bp and dp:
            max_age = max(now - bp.local_recv_ts, now - dp.local_recv_ts)
            if max_age < 0.5:
                conf += 10
            elif max_age < 1.0:
                conf += 5

        # Delta spread: tight spread = better fill
        if dp and dp.ask > 0 and dp.bid > 0:
            spread_pct = (dp.ask - dp.bid) / dp.mid * 100.0
            if spread_pct < 0.05:
                conf += 5
            elif spread_pct > 0.15:
                conf -= 10  # wide spread eats into profit

        return min(100, max(0, conf))

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Return measurement statistics."""
        result: Dict[str, Any] = dict(self._stats)
        result["avg_dislocation_pct"] = {}
        result["max_dislocation_pct"] = {}
        result["avg_latency_ms"] = {}

        for symbol in self.symbols:
            history = list(self._dislocation_history.get(symbol, []))
            if not history:
                continue
            disls = [abs(d.dislocation_pct) for d in history]
            lats = [d.latency_ms for d in history]
            result["avg_dislocation_pct"][symbol] = round(np.mean(disls), 4)
            result["max_dislocation_pct"][symbol] = round(max(disls), 4)
            result[f"p95_dislocation_pct_{symbol}"] = round(
                float(np.percentile(disls, 95)), 4
            )
            result["avg_latency_ms"][symbol] = round(float(np.mean(lats)), 1)

            # How often is dislocation >= threshold?
            tradeable = sum(1 for d in disls if d >= self.MIN_DISLOCATION_PCT)
            result[f"tradeable_pct_{symbol}"] = round(
                tradeable / len(disls) * 100.0, 1
            )

        if self._stats["started_at"]:
            result["uptime_s"] = round(time.time() - self._stats["started_at"], 0)

        return result

    def get_dislocation_history(self, symbol: str) -> List[Dislocation]:
        """Return the full dislocation deque as a list (for analysis scripts)."""
        return list(self._dislocation_history.get(symbol, []))

    def get_recent_dislocations(self, symbol: str, n: int = 20) -> List[Dict]:
        """Return recent dislocations formatted for display."""
        history = list(self._dislocation_history.get(symbol, []))[-n:]
        return [
            {
                "binance": round(d.binance_mid, 2),
                "delta": round(d.delta_mid, 2),
                "disl_pct": round(d.dislocation_pct, 4),
                "direction": d.direction,
                "latency_ms": round(d.latency_ms, 1),
                "time": datetime.fromtimestamp(
                    d.timestamp, tz=timezone.utc
                ).strftime("%H:%M:%S.%f")[:-3],
            }
            for d in history
        ]
