"""Strategy backtester API — replays signal engine on historical candles."""
import json
import logging
from aiohttp import web

logger = logging.getLogger("dashboard.backtest")


def register_backtest_routes(app: web.Application, db_pool=None):
    """Register backtester endpoints."""

    async def handle_backtest_run(request: web.Request) -> web.Response:
        """POST /api/backtest/run — run backtest synchronously on historical data."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        try:
            from ml_training.backtest_engine import run_backtest
            result = run_backtest(body)
            logger.info("Backtest: %d trades, WR=%.1f%%, PnL=$%.2f",
                        result["total_trades"], result["win_rate"], result["total_pnl"])
            return web.json_response({"status": "complete", **result})
        except Exception as e:
            logger.error("Backtest error: %s", e)
            return web.json_response({"error": str(e)}, status=500)

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
