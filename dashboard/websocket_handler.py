"""WebSocket push for the dashboard (2026-09-20 rewrite).

Channels
  tick      {prices:{sym:last}, marks:{sym:mark}, ts}          throttled to TICK_MIN_INTERVAL
  position  {trades:[enriched rows], ts}                       throttled to POS_MIN_INTERVAL
  event     {type, message, trade_id, symbol, signal?, ts}     immediate (opens, fills, closes, stop moves)
  hello     {channels:[...], ts}                                on connect

The route lives under /api/ws so the dashboard's cookie-session auth middleware
covers it (the old /ws path was reachable without a login).
"""
import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

from aiohttp import web, WSMsgType

logger = logging.getLogger("dashboard.ws")

TICK_MIN_INTERVAL = 0.5     # seconds between price frames
POS_MIN_INTERVAL = 1.0      # seconds between position frames
WS_PATH = "/api/ws"


def _default(o):
    try:
        import numpy as np
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
    except Exception:
        pass
    return str(o)


class WebSocketHub:
    """Broadcasts updates to all connected clients."""
    def __init__(self):
        self._clients: set = set()
        self.frames_sent = 0
        self._last_tick = 0.0
        self._last_pos = 0.0
        self._pending_tick: Optional[Dict[str, Any]] = None
        self._flush_task: Optional[asyncio.Task] = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def add(self, ws):
        self._clients.add(ws)
        logger.info("WS client connected (total=%d)", len(self._clients))

    async def remove(self, ws):
        self._clients.discard(ws)
        logger.info("WS client disconnected (total=%d)", len(self._clients))

    async def broadcast(self, channel: str, data: dict):
        """Send {channel, data, ts} to all connected clients."""
        if not self._clients:
            return
        msg = json.dumps({"channel": channel, "data": data, "ts": time.time()}, default=_default)
        dead = set()
        for ws in list(self._clients):
            try:
                if ws.closed:
                    dead.add(ws)
                else:
                    await ws.send_str(msg)
                    self.frames_sent += 1
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._clients.discard(ws)

    # -- throttled helpers used by the orchestrator tick ------------------
    async def push_tick(self, prices: Dict[str, float], marks: Optional[Dict[str, float]] = None) -> None:
        """Coalesce price frames: at most one every TICK_MIN_INTERVAL, latest wins."""
        if not self._clients:
            return
        payload = {"prices": dict(prices), "marks": dict(marks or {})}
        now = time.time()
        if now - self._last_tick >= TICK_MIN_INTERVAL:
            self._last_tick = now
            await self.broadcast("tick", payload)
            return
        if self._pending_tick is not None:
            self._pending_tick["prices"].update(payload["prices"])
            self._pending_tick["marks"].update(payload["marks"])
        else:
            self._pending_tick = payload
        if self._flush_task is None or self._flush_task.done():
            delay = max(0.0, TICK_MIN_INTERVAL - (now - self._last_tick))
            self._flush_task = asyncio.create_task(self._flush_tick(delay))

    async def _flush_tick(self, delay: float) -> None:
        await asyncio.sleep(delay)
        payload, self._pending_tick = self._pending_tick, None
        if payload:
            self._last_tick = time.time()
            await self.broadcast("tick", payload)

    async def push_position(self, trades: list, force: bool = False) -> None:
        if not self._clients:
            return
        now = time.time()
        if not force and now - self._last_pos < POS_MIN_INTERVAL:
            return
        self._last_pos = now
        await self.broadcast("position", {"trades": trades})

    async def push_events(self, events: list) -> None:
        if not self._clients:
            return
        for ev in events or []:
            sig = ev.get("signal") or {}
            await self.broadcast("event", {
                "type": ev.get("type"), "message": ev.get("message"),
                "trade_id": sig.get("trade_id"), "symbol": sig.get("symbol"), "side": sig.get("side"),
                "new_sl": ev.get("new_sl"), "old_sl": ev.get("old_sl"),
                "exit_price": sig.get("exit_price"), "pnl_usd": sig.get("pnl_usd"), "exit_reason": sig.get("exit_reason"),
            })


# Global hub instance
hub = WebSocketHub()


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint: GET /api/ws — clients subscribe to live updates."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    await hub.add(ws)
    try:
        await ws.send_str(json.dumps({"channel": "hello", "data": {"channels": ["tick", "position", "event"]}, "ts": time.time()}))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if msg.data == "ping":
                    await ws.send_str("pong")
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        await hub.remove(ws)
    return ws


def register_ws_routes(app: web.Application):
    app.router.add_get(WS_PATH, ws_handler)
    logger.info("WebSocket routes registered (%s)", WS_PATH)
