"""
Per-User Trading API Routes for VN Edge.

All endpoints are scoped to the authenticated user via request["session"]["user_id"].
Users can only see/modify their own trading data. Admins can see all users via admin routes.
"""

from __future__ import annotations

import json
import logging
from aiohttp import web
from typing import Any

logger = logging.getLogger("dashboard.user_trading")


def register_user_trading_routes(app: web.Application, user_registry: Any, db_pool: Any):
    """Register per-user real trading endpoints."""

    async def _get_user_id(request: web.Request) -> str:
        """Extract user_id from session. Returns empty string if not authenticated."""
        session = request.get("session", {})
        return str(session.get("user_id", ""))

    # ── Real Trading Status ──
    async def handle_user_real_status(request: web.Request) -> web.Response:
        """GET /api/user/real/status — User's real trading status."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        mgr = await user_registry.get_manager_for_user(user_id)
        if mgr:
            await mgr.refresh_balance()
            return web.json_response(mgr.get_status())

        return web.json_response({
            "user_id": user_id,
            "enabled": False,
            "balance": 0,
            "open_count": 0,
            "open_positions": [],
            "circuit_breaker": {"is_tripped": False, "consecutive_losses": 0},
            "closed_trades": [],
            "config": {},
            "message": "No real trading configured. Add API keys and set mode to demo/live.",
        })

    # ── Real Trading Toggle ──
    async def handle_user_real_toggle(request: web.Request) -> web.Response:
        """POST /api/user/real/toggle — Enable/disable user's real trading."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        body = await request.json()
        new_mode = body.get("bot_mode", "paper")  # paper / demo / live
        if new_mode not in ("paper", "demo", "live"):
            return web.json_response({"error": "Invalid mode. Use: paper, demo, live"}, status=400)

        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET bot_mode = $1, updated_at = NOW() WHERE id = $2",
                    new_mode, user_id,
                )

            # If switching to paper, remove existing manager
            if new_mode == "paper" and user_id in user_registry._managers:
                del user_registry._managers[user_id]
                logger.info("User %s switched to paper — manager removed", user_id[:8])

            # Force user refresh
            user_registry._last_user_refresh = 0

            return web.json_response({"success": True, "bot_mode": new_mode})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ── User Closed Trades ──
    async def handle_user_real_trades(request: web.Request) -> web.Response:
        """GET /api/user/real/trades — User's closed real trade history."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        limit = int(request.query.get("limit", "100"))

        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, symbol, side, entry_price, exit_price, pnl_usd, fees_usd,
                           status, opened_at, closed_at, metadata
                    FROM user_trades
                    WHERE user_id = $1 AND trade_type = 'real'
                    ORDER BY opened_at DESC
                    LIMIT $2
                """, user_id, limit)
                trades = []
                for r in rows:
                    t = dict(r)
                    t["id"] = str(t["id"])
                    if t.get("metadata"):
                        t["metadata"] = json.loads(t["metadata"]) if isinstance(t["metadata"], str) else t["metadata"]
                    trades.append(t)
                return web.json_response({"trades": trades, "count": len(trades)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ── User Trading Config ──
    async def handle_user_real_config_get(request: web.Request) -> web.Response:
        """GET /api/user/real/config — User's trading configuration."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT max_leverage, max_daily_loss_pct, max_open_positions,
                           trading_pairs, preferred_leverage, bot_mode,
                           risk_per_trade_pct
                    FROM users WHERE id = $1
                """, user_id)
                if not row:
                    return web.json_response({"error": "user not found"}, status=404)
                config = dict(row)
                if config.get("trading_pairs") and isinstance(config["trading_pairs"], str):
                    config["trading_pairs"] = json.loads(config["trading_pairs"])
                return web.json_response(config)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_user_real_config_put(request: web.Request) -> web.Response:
        """PUT /api/user/real/config — Update user's trading configuration."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        body = await request.json()

        # Validate and extract fields
        updates = {}
        if "max_leverage" in body:
            v = int(body["max_leverage"])
            if not 1 <= v <= 50:
                return web.json_response({"error": "max_leverage must be 1-50"}, status=400)
            updates["max_leverage"] = v
        if "max_daily_loss_pct" in body:
            v = float(body["max_daily_loss_pct"])
            if not 0.5 <= v <= 20:
                return web.json_response({"error": "max_daily_loss_pct must be 0.5-20"}, status=400)
            updates["max_daily_loss_pct"] = v
        if "max_open_positions" in body:
            v = int(body["max_open_positions"])
            if not 1 <= v <= 10:
                return web.json_response({"error": "max_open_positions must be 1-10"}, status=400)
            updates["max_open_positions"] = v
        if "trading_pairs" in body:
            pairs = body["trading_pairs"]
            if not isinstance(pairs, list):
                return web.json_response({"error": "trading_pairs must be a list"}, status=400)
            updates["trading_pairs"] = json.dumps(pairs)
        if "preferred_leverage" in body:
            updates["preferred_leverage"] = min(50, max(1, int(body["preferred_leverage"])))

        if not updates:
            return web.json_response({"error": "No valid fields to update"}, status=400)

        try:
            set_clauses = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates.keys()))
            values = [user_id] + list(updates.values())

            async with db_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE users SET {set_clauses}, updated_at = NOW() WHERE id = $1",
                    *values,
                )

            # Invalidate cached manager so it picks up new config
            if user_id in user_registry._managers:
                del user_registry._managers[user_id]

            return web.json_response({"success": True, "updated": list(updates.keys())})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ── User Strategies ──
    async def handle_user_strategies_list(request: web.Request) -> web.Response:
        """GET /api/user/strategies — List user's strategy configurations."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, name, is_active, scanners_enabled, min_confidence,
                           ml_threshold, size_multiplier, regime_overrides,
                           max_daily_trades, allowed_regimes, created_at, updated_at
                    FROM user_strategies
                    WHERE user_id = $1
                    ORDER BY created_at
                """, user_id)
                strategies = []
                for r in rows:
                    s = dict(r)
                    s["id"] = str(s["id"])
                    for field in ("scanners_enabled", "regime_overrides", "allowed_regimes"):
                        if s.get(field) and isinstance(s[field], str):
                            s[field] = json.loads(s[field])
                    strategies.append(s)
                return web.json_response({"strategies": strategies})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_user_strategy_create(request: web.Request) -> web.Response:
        """POST /api/user/strategies — Create a new strategy."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            return web.json_response({"error": "name is required"}, status=400)

        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO user_strategies (user_id, name, scanners_enabled,
                        min_confidence, ml_threshold, size_multiplier, max_daily_trades, allowed_regimes)
                    VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8::jsonb)
                    RETURNING id
                """, user_id, name,
                    json.dumps(body.get("scanners_enabled", [])),
                    float(body.get("min_confidence", 55)),
                    float(body.get("ml_threshold", 0.60)),
                    float(body.get("size_multiplier", 1.0)),
                    int(body.get("max_daily_trades", 15)),
                    json.dumps(body.get("allowed_regimes", ["trending_up", "trending_down", "breakout", "sideways"])),
                )
                return web.json_response({"success": True, "strategy_id": str(row["id"])})
        except Exception as e:
            if "unique" in str(e).lower():
                return web.json_response({"error": f"Strategy '{name}' already exists"}, status=409)
            return web.json_response({"error": str(e)}, status=500)

    # ── CB Reset ──
    async def handle_user_cb_reset(request: web.Request) -> web.Response:
        """POST /api/user/real/cb-reset — Reset user's circuit breaker."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        mgr = await user_registry.get_manager_for_user(user_id)
        if mgr:
            mgr.cb.reset()
            return web.json_response({"success": True, "message": "Circuit breaker reset"})
        return web.json_response({"error": "No real trading manager active"}, status=404)

    # ── Register all routes ──
    app.router.add_get("/api/user/real/status", handle_user_real_status)
    app.router.add_post("/api/user/real/toggle", handle_user_real_toggle)
    app.router.add_get("/api/user/real/trades", handle_user_real_trades)
    app.router.add_get("/api/user/real/config", handle_user_real_config_get)
    app.router.add_put("/api/user/real/config", handle_user_real_config_put)
    app.router.add_get("/api/user/strategies", handle_user_strategies_list)
    app.router.add_post("/api/user/strategies", handle_user_strategy_create)
    app.router.add_post("/api/user/real/cb-reset", handle_user_cb_reset)

    logger.info("Per-user trading routes registered (8 endpoints)")
