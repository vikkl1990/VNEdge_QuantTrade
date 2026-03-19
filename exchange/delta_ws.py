"""
Direct WebSocket client for Delta Exchange India.

Connects to wss://socket.india.delta.exchange for real-time price feeds.
Reduces price latency from ~5000ms (REST polling) to ~100ms (WebSocket push).

Usage:
    ws = DeltaWebSocket(symbols=["BTC/USDT", "ETH/USDT"])
    ws.on_price = my_callback  # called with (symbol, price, bid, ask)
    await ws.connect()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger("bot.delta_ws")  # use bot.* namespace for visibility

# Delta India WebSocket endpoint
WS_URL = "wss://socket.india.delta.exchange"

# Symbol mapping: our format → Delta WS format
SYMBOL_MAP = {
    "BTC/USDT": "BTCUSD",
    "ETH/USDT": "ETHUSD",
    "AVAX/USDT": "AVAXUSD",
}

REVERSE_MAP = {v: k for k, v in SYMBOL_MAP.items()}


class DeltaWebSocket:
    """Real-time price feed via Delta Exchange WebSocket.

    Automatically reconnects on disconnect with exponential backoff.
    Falls back gracefully — if WS fails, the REST polling continues.
    """

    def __init__(
        self,
        symbols: List[str] = None,
        on_price: Optional[Callable] = None,
        ping_interval: int = 25,
        max_reconnects: int = 50,
    ):
        self._symbols = symbols or ["BTC/USDT", "ETH/USDT"]
        self.on_price = on_price  # callback(symbol, last, bid, ask, mark)
        self._ping_interval = ping_interval
        self._max_reconnects = max_reconnects

        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._connected = False
        self._reconnect_count = 0
        self._last_msg_time: float = 0

        # Latest prices (thread-safe via asyncio single-thread)
        self.prices: Dict[str, float] = {}
        self.bids: Dict[str, float] = {}
        self.asks: Dict[str, float] = {}
        self.marks: Dict[str, float] = {}

        # Stats
        self.msg_count: int = 0
        self.connect_time: float = 0
        self.avg_latency_ms: float = 0
        self._latencies: List[float] = []

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        """Start the WebSocket connection loop."""
        self._running = True
        self._session = aiohttp.ClientSession()
        asyncio.create_task(self._connection_loop(), name="delta-ws")
        logger.info("DeltaWebSocket starting for %s", self._symbols)

    async def close(self) -> None:
        """Gracefully shut down."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._connected = False
        logger.info("DeltaWebSocket closed")

    async def _connection_loop(self) -> None:
        """Main loop: connect, subscribe, read messages, reconnect on failure."""
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._connected = False
                self._reconnect_count += 1

                if self._reconnect_count > self._max_reconnects:
                    logger.error(
                        "DeltaWS: exceeded %d reconnect attempts. Giving up.",
                        self._max_reconnects,
                    )
                    break

                delay = min(2 ** min(self._reconnect_count, 6), 60)
                logger.warning(
                    "DeltaWS disconnected (%s). Reconnect %d/%d in %ds...",
                    exc, self._reconnect_count, self._max_reconnects, delay,
                )
                await asyncio.sleep(delay)

    async def _connect_and_listen(self) -> None:
        """Single connection lifecycle: connect → subscribe → read."""
        logger.info("DeltaWS connecting to %s ...", WS_URL)

        async with self._session.ws_connect(
            WS_URL,
            heartbeat=self._ping_interval,
            timeout=30,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._reconnect_count = 0
            self.connect_time = time.time()
            logger.info("DeltaWS connected!")

            # Subscribe to ticker channels
            await self._subscribe(ws)

            # Read messages
            async for msg in ws:
                if not self._running:
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._last_msg_time = time.time()
                    await self._handle_message(msg.data)

                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning("DeltaWS: connection closed/error: %s", msg.data)
                    break

        self._connected = False

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Subscribe to v2_ticker for all configured symbols."""
        delta_symbols = []
        for sym in self._symbols:
            ds = SYMBOL_MAP.get(sym)
            if ds:
                delta_symbols.append(ds)
            else:
                logger.warning("DeltaWS: unknown symbol mapping for %s", sym)

        if not delta_symbols:
            return

        subscribe_msg = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {
                        "name": "v2/ticker",
                        "symbols": delta_symbols,
                    }
                ]
            }
        }

        await ws.send_json(subscribe_msg)
        logger.info("DeltaWS subscribed to ticker: %s", delta_symbols)

    async def _handle_message(self, raw: str) -> None:
        """Parse incoming WebSocket message and update prices."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type", "")

        # Subscription confirmation
        if msg_type == "subscriptions":
            logger.info("DeltaWS subscription confirmed: %s", data)
            return

        # Ticker update — Delta India sends fields at top level, not nested
        if msg_type == "v2/ticker":
            delta_symbol = data.get("symbol", "")
            symbol = REVERSE_MAP.get(delta_symbol, "")

            if not symbol:
                return

            try:
                last = float(data.get("close", 0))
                mark = float(data.get("mark_price", last))
                # Bid/ask are nested under "quotes"
                quotes = data.get("quotes", {})
                bid = float(quotes.get("best_bid", last))
                ask = float(quotes.get("best_ask", last))
            except (ValueError, TypeError):
                return

            if last <= 0:
                return

            # Update price cache
            self.prices[symbol] = last
            self.bids[symbol] = bid
            self.asks[symbol] = ask
            self.marks[symbol] = mark
            self.msg_count += 1

            # Track latency
            ts = data.get("timestamp")
            if ts:
                try:
                    latency = (time.time() * 1000) - float(ts)
                    self._latencies.append(latency)
                    if len(self._latencies) > 100:
                        self._latencies = self._latencies[-100:]
                    self.avg_latency_ms = sum(self._latencies) / len(self._latencies)
                except:
                    pass

            # Fire callback
            if self.on_price:
                try:
                    await self.on_price(symbol, last, bid, ask, mark)
                except Exception as exc:
                    logger.debug("DeltaWS price callback error: %s", exc)

    def get_status(self) -> Dict:
        """Return WebSocket status for dashboard."""
        return {
            "connected": self.is_connected,
            "url": WS_URL,
            "symbols": self._symbols,
            "msg_count": self.msg_count,
            "reconnects": self._reconnect_count,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "last_msg_ago": round(time.time() - self._last_msg_time, 1) if self._last_msg_time else None,
            "uptime_sec": round(time.time() - self.connect_time) if self.connect_time else 0,
            "prices": dict(self.prices),
        }
