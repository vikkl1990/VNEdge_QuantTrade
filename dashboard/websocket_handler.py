"""WebSocket handler for real-time dashboard push (no polling needed)."""
import asyncio
import json
import logging
from aiohttp import web, WSMsgType

logger = logging.getLogger("dashboard.ws")


class WebSocketHub:
    """Broadcasts updates to all connected clients."""
    def __init__(self):
        self._clients: set = set()

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
        msg = json.dumps({"channel": channel, "data": data})
        dead = set()
        for ws in self._clients:
            try:
                if ws.closed:
                    dead.add(ws)
                else:
                    await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._clients.discard(ws)


# Global hub instance
hub = WebSocketHub()


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint: GET /ws — clients subscribe to live updates."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    await hub.add(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Echo ping for keep-alive
                if msg.data == "ping":
                    await ws.send_str("pong")
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        await hub.remove(ws)
    return ws


def register_ws_routes(app: web.Application):
    app.router.add_get("/ws", ws_handler)
    logger.info("WebSocket routes registered (/ws)")
