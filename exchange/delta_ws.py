"""
Direct WebSocket client for Delta Exchange India.

Connects to wss://socket.india.delta.exchange for real-time price feeds
and authenticated private channels (orders, positions).

Public channels: v2/ticker (price feeds)
Private channels: orders (fill events), positions (position changes)

Usage:
    ws = DeltaWebSocket(symbols=["BTC/USDT", "ETH/USDT"],
                        api_key="...", api_secret="...")
    ws.on_price = my_callback
    ws.on_order_fill = my_fill_callback
    await ws.connect()
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger("bot.delta_ws")  # use bot.* namespace for visibility

# Delta India WebSocket endpoints
WS_URL_PROD = "wss://socket.india.delta.exchange"
WS_URL_DEMO = "wss://socket-ind.testnet.deltaex.org"  # India testnet (socket.testnet.delta.exchange has no DNS)
WS_URL = WS_URL_PROD  # Default to production

# Symbol mapping: our format → Delta WS format
# FIX C1: Expanded to all active trading pairs so update_real_trades() sees price ticks
# for every pair that can have a real position (previously missing: XRP/LTC/ADA/LINK/DOT/TAO)
# FIX 2026-04-19: Added meme coins with 1000x multiplier prefix (matches
# _DELTA_BASE_OVERRIDE in exchange/ccxt_client.py and data/feed.py).
# Without these, DeltaWS silently drops meme subscriptions → no live price feed
# → scanners can't evaluate memes in real-time → zero meme signals.
SYMBOL_MAP = {
    "BTC/USDT": "BTCUSD",
    "ETH/USDT": "ETHUSD",
    "AVAX/USDT": "AVAXUSD",
    "SOL/USDT": "SOLUSD",
    "DOGE/USDT": "DOGEUSD",
    "XRP/USDT": "XRPUSD",
    "LTC/USDT": "LTCUSD",
    "ADA/USDT": "ADAUSD",
    "LINK/USDT": "LINKUSD",
    "DOT/USDT": "DOTUSD",
    "TAO/USDT": "TAOUSD",
    # Memes with 1000x multiplier (thin-price contracts on Delta India)
    "PEPE/USDT": "1000PEPEUSD",
    "SHIB/USDT": "1000SHIBUSD",
    "BONK/USDT": "1000BONKUSD",
    "FLOKI/USDT": "1000FLOKIUSD",
    # 1:1 memes (normal contract size)
    "WIF/USDT": "WIFUSD",
    "SUI/USDT": "SUIUSD",
    "NEAR/USDT": "NEARUSD",
    "TRUMP/USDT": "TRUMPUSD",
    "POPCAT/USDT": "POPCATUSD",
    "MEME/USDT": "MEMEUSD",
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
        on_order_fill: Optional[Callable] = None,
        on_position_update: Optional[Callable] = None,
        on_candle: Optional[Callable] = None,
        candle_timeframes: Optional[List[str]] = None,
        api_key: str = "",
        api_secret: str = "",
        mode: str = "live",
        ping_interval: int = 25,
        max_reconnects: int = 50,
    ):
        self._symbols = symbols or ["BTC/USDT", "ETH/USDT"]
        self._mode = mode  # "demo" or "live"
        # Always use production WS for price feeds (testnet has no real prices)
        # Private channels only work in live mode (prod WS + prod API keys)
        # In demo mode: prices from prod WS, order/position sync via REST polling
        self._ws_url = WS_URL_PROD
        self.on_price = on_price  # callback(symbol, last, bid, ask, mark)
        self.on_order_fill = on_order_fill  # callback(symbol, order_id, client_order_id, fill_price, side, size)
        self.on_position_update = on_position_update  # callback(symbol, size, entry_price, pnl)
        # callback(symbol, tf, candle_dict) — Delta candlestick_{tf} channel, REST-shaped dict
        self.on_candle = on_candle
        self._candle_timeframes: List[str] = list(candle_timeframes or ["1m", "5m", "15m", "1h", "4h"])
        self._api_key = api_key
        self._api_secret = api_secret
        self._ping_interval = ping_interval
        self._max_reconnects = max_reconnects

        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._connected = False
        self._authenticated = False
        self._reconnect_count = 0
        self._last_msg_time: float = 0

        # Latest prices (thread-safe via asyncio single-thread)
        self.prices: Dict[str, float] = {}
        self.bids: Dict[str, float] = {}
        self.asks: Dict[str, float] = {}
        self.marks: Dict[str, float] = {}

        # Stats
        self.msg_count: int = 0
        self.private_msg_count: int = 0
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
        """Single connection lifecycle: connect → auth → subscribe → read."""
        logger.info("DeltaWS connecting to %s (mode=%s) ...", self._ws_url, self._mode)

        async with self._session.ws_connect(
            self._ws_url,
            heartbeat=self._ping_interval,
            timeout=30,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._authenticated = False
            self._reconnect_count = 0
            self.connect_time = time.time()
            logger.info("DeltaWS connected!")

            # Authenticate for private channels (orders, positions)
            # Only in LIVE mode — demo keys don't work on production WS
            if self._api_key and self._api_secret and self._mode == "live":
                await self._authenticate(ws)
            elif self._mode == "demo":
                logger.info("DeltaWS: skipping auth (demo mode — private channels via REST)")
                self._authenticated = False

            # Subscribe to ticker channels + private channels
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

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Authenticate for private channels using HMAC-SHA256."""
        try:
            timestamp = str(int(time.time()))
            signature_data = f"GET{timestamp}/live"
            signature = hmac.new(
                self._api_secret.encode("utf-8"),
                signature_data.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

            auth_msg = {
                "type": "auth",
                "payload": {
                    "api-key": self._api_key,
                    "signature": signature,
                    "timestamp": timestamp,
                }
            }
            await ws.send_json(auth_msg)
            self._authenticated = True
            logger.info("DeltaWS: auth message sent (awaiting confirmation)")
        except Exception as e:
            logger.error("DeltaWS: authentication failed: %s", e)
            self._authenticated = False

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Subscribe to public ticker + private order/position channels."""
        delta_symbols = []
        for sym in self._symbols:
            ds = SYMBOL_MAP.get(sym)
            if ds:
                delta_symbols.append(ds)
            else:
                logger.warning("DeltaWS: unknown symbol mapping for %s", sym)

        if not delta_symbols:
            return

        # Public channels: v2/ticker + candlesticks (real-time candle closes;
        # the REST poller is only a backstop when these are subscribed)
        channels = [
            {
                "name": "v2/ticker",
                "symbols": delta_symbols,
            }
        ]
        if self.on_candle:
            for _tf in self._candle_timeframes:
                channels.append({"name": f"candlestick_{_tf}", "symbols": delta_symbols})

        # Private channels (requires auth) — need symbol arrays
        if self._authenticated:
            channels.append({"name": "orders", "symbols": delta_symbols})
            channels.append({"name": "positions", "symbols": delta_symbols})
            logger.info("DeltaWS: subscribing to private channels (orders, positions)")

        subscribe_msg = {
            "type": "subscribe",
            "payload": {"channels": channels}
        }

        await ws.send_json(subscribe_msg)
        logger.info("DeltaWS subscribed to ticker: %s | private=%s",
                    delta_symbols, self._authenticated)

    async def _handle_message(self, raw: str) -> None:
        """Parse incoming WebSocket message and update prices / handle private events."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type", "")

        # Subscription confirmation
        if msg_type == "subscriptions":
            logger.info("DeltaWS subscription confirmed: %s", data)
            return

        # Auth confirmation
        if msg_type == "auth":
            if data.get("success"):
                logger.info("DeltaWS: authenticated successfully")
            else:
                logger.error("DeltaWS: auth failed: %s", data)
                self._authenticated = False
            return

        # ── PUBLIC: Candlestick update (candlestick_1m / _5m / ...) ──
        if msg_type.startswith("candlestick_"):
            await self._handle_candle(data)
            return

        # ── PUBLIC: Ticker update ──
        if msg_type == "v2/ticker":
            await self._handle_ticker(data)
            return

        # ── PRIVATE: Order events ──
        if msg_type == "orders":
            await self._handle_order_event(data)
            return

        # ── PRIVATE: Position events ──
        if msg_type == "positions":
            await self._handle_position_event(data)
            return

    async def _handle_candle(self, data: dict) -> None:
        """candlestick_{tf} message → on_candle(symbol, tf, candle).

        Delta sends the live bar repeatedly as it evolves; timestamps are in
        microseconds (candle_start_time). The feed decides when a bar is
        complete (a newer candle_start_time appears).
        """
        if not self.on_candle:
            return
        try:
            symbol = REVERSE_MAP.get(data.get("symbol", ""))
            tf = data.get("resolution") or data.get("type", "")[len("candlestick_"):]
            start_us = data.get("candle_start_time")
            if not symbol or not tf or not start_us:
                return
            candle = {
                "timestamp": int(int(start_us) // 1000),  # µs → ms
                "open": float(data.get("open", 0) or 0),
                "high": float(data.get("high", 0) or 0),
                "low": float(data.get("low", 0) or 0),
                "close": float(data.get("close", 0) or 0),
                "volume": float(data.get("volume", 0) or 0),
            }
            if candle["close"] <= 0:
                return
            self.candle_msg_count = getattr(self, "candle_msg_count", 0) + 1
            await self.on_candle(symbol, tf, candle)
        except Exception as exc:
            logger.debug("DeltaWS candle callback error: %s", exc)

    async def _handle_ticker(self, data: dict) -> None:
        """Process ticker price update."""
        delta_symbol = data.get("symbol", "")
        symbol = REVERSE_MAP.get(delta_symbol, "")

        if not symbol:
            return

        try:
            last = float(data.get("close", 0))
            mark = float(data.get("mark_price", last))
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
            except Exception:
                pass

        # Fire callback
        if self.on_price:
            try:
                await self.on_price(symbol, last, bid, ask, mark)
            except Exception as exc:
                logger.debug("DeltaWS price callback error: %s", exc)

    async def _handle_order_event(self, data: dict) -> None:
        """Process private order channel events (SL/TP fills).

        Fires on_order_fill callback when a reduce_only order is filled,
        which means SL or TP was hit on the exchange.
        """
        self.private_msg_count += 1

        # Order state: open, pending, closed, cancelled
        state = data.get("state", "")
        if state not in ("closed",):
            return  # Only care about filled orders

        # Only process reduce_only fills (SL/TP exits, not entries)
        if data.get("reduce_only") != True and str(data.get("reduce_only", "")).lower() != "true":
            return

        product_symbol = data.get("product_symbol", "")
        symbol = REVERSE_MAP.get(product_symbol, "")
        order_id = str(data.get("id", ""))
        client_order_id = data.get("client_order_id", "")
        side = data.get("side", "")
        size = int(data.get("size", 0) or 0)

        # Get fill price
        fill_price = float(data.get("average_fill_price", 0) or data.get("limit_price", 0) or 0)

        logger.info(
            "DeltaWS ORDER FILL: %s %s %d lots @ %.4f | order=%s | coid=%s",
            symbol or product_symbol, side, size, fill_price,
            order_id[:12], client_order_id[:12] if client_order_id else "-",
        )

        if self.on_order_fill and fill_price > 0:
            try:
                await self.on_order_fill(
                    symbol=symbol or product_symbol,
                    order_id=order_id,
                    client_order_id=client_order_id,
                    fill_price=fill_price,
                    side=side,
                    size=size,
                )
            except Exception as exc:
                logger.error("DeltaWS order fill callback error: %s", exc)

    async def _handle_position_event(self, data: dict) -> None:
        """Process private position channel events.

        Fires on_position_update callback when a position closes (size=0),
        which catches liquidations and exchange-side closes.
        """
        self.private_msg_count += 1

        product_symbol = data.get("product_symbol", data.get("symbol", ""))
        symbol = REVERSE_MAP.get(product_symbol, "")
        size = int(data.get("size", 0) or 0)
        entry_price = float(data.get("entry_price", 0) or 0)
        pnl = float(data.get("realized_pnl", 0) or data.get("pnl", 0) or 0)

        logger.info(
            "DeltaWS POSITION UPDATE: %s | size=%d entry=%.4f pnl=%.4f",
            symbol or product_symbol, size, entry_price, pnl,
        )

        if self.on_position_update:
            try:
                await self.on_position_update(
                    symbol=symbol or product_symbol,
                    size=size,
                    entry_price=entry_price,
                    pnl=pnl,
                )
            except Exception as exc:
                logger.error("DeltaWS position update callback error: %s", exc)

    def get_status(self) -> Dict:
        """Return WebSocket status for dashboard."""
        return {
            "connected": self.is_connected,
            "authenticated": self._authenticated,
            "url": WS_URL,
            "symbols": self._symbols,
            "msg_count": self.msg_count,
            "private_msg_count": self.private_msg_count,
            "reconnects": self._reconnect_count,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "last_msg_ago": round(time.time() - self._last_msg_time, 1) if self._last_msg_time else None,
            "uptime_sec": round(time.time() - self.connect_time) if self.connect_time else 0,
            "prices": dict(self.prices),
        }
