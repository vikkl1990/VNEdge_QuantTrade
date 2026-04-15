"""Strategy backtester API — replays signal engine on historical candles."""
import json
import logging
from aiohttp import web

logger = logging.getLogger("dashboard.backtest")


def register_backtest_routes(app: web.Application, db_pool=None):
    """Register backtester endpoints."""

    async def handle_backtest_run(request: web.Request) -> web.Response:
        """POST /api/backtest/run — trigger a backtest with custom params.

        Body: {symbols, start_date, end_date, scanners, ml_threshold, leverage, ...}
        Returns: job_id (results polled separately).
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        # Validate
        symbols = body.get("symbols", ["BTC/USDT"])
        if not isinstance(symbols, list) or not symbols:
            return web.json_response({"error": "symbols required"}, status=400)

        # Stub: real impl would queue a background job
        import uuid
        job_id = str(uuid.uuid4())[:8]
        logger.info("Backtest queued: job=%s symbols=%s", job_id, symbols)
        return web.json_response({
            "job_id": job_id,
            "status": "queued",
            "symbols": symbols,
            "message": "Backtest engine in development. Stub endpoint active.",
        })

    async def handle_backtest_status(request: web.Request) -> web.Response:
        """GET /api/backtest/status/{job_id} — poll backtest progress."""
        job_id = request.match_info.get("job_id", "")
        return web.json_response({
            "job_id": job_id,
            "status": "running",
            "progress": 0,
            "message": "Stub — full backtester implementation pending.",
        })

    async def handle_backtest_history(request: web.Request) -> web.Response:
        """GET /api/backtest/history — list past backtest runs."""
        return web.json_response({"backtests": []})

    app.router.add_post("/api/backtest/run", handle_backtest_run)
    app.router.add_get("/api/backtest/status/{job_id}", handle_backtest_status)
    app.router.add_get("/api/backtest/history", handle_backtest_history)
    logger.info("Backtester routes registered (3 endpoints, stub mode)")
