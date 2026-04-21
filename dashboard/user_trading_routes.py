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
        """Extract user_id from the auth middleware's session.

        SEC FIX (2026-04-19): Previously returned empty string on missing session,
        which could slip through callers that did `if not user_id` but then
        still ran queries with `WHERE user_id = ''` — matching nothing but
        still revealing query structure. Now the middleware is the single
        source of truth — if it set request["user"], we trust it; otherwise
        return "" and the caller MUST 401. Also: prefer request["user"]
        (middleware-set) over request["session"] (transport-set) since the
        former is canonical.
        """
        user = request.get("user") or {}
        user_id = user.get("user_id")
        if user_id:
            return str(user_id)
        # Fallback: request["session"] (some legacy call sites)
        session = request.get("session") or {}
        return str(session.get("user_id", ""))

    # Cache: user_id -> (timestamp, result_dict). 60s TTL to avoid
    # hammering Delta on every page load. Cleared on key changes.
    _conn_status_cache: dict = {}

    async def handle_user_connection_status(request: web.Request) -> web.Response:
        """GET /api/user/connection-status — on-demand check of the user's
        current trading readiness. Drives the login popup that tells the
        user EXACTLY whether they're connected to Delta or not.

        Return shape:
          {
            "bot_mode": "paper"|"demo"|"live",
            "key_required": bool,        # false if paper mode
            "key_present": bool,          # true if a matching-label active key exists
            "key_label_expected": str,    # "demo" or "live" (matching bot_mode)
            "connected": bool,            # Delta accepted the key on a balance probe
            "balance_usdt": float,        # None if not connected
            "status": str,                # "ok" | "no_key" | "key_rejected" | "delta_unreachable" | "paper_only"
            "message": str,               # human-readable
            "severity": str,              # "ok" | "warning" | "critical" | "info"
            "checked_at": iso8601,
            "cache_age_sec": int,         # 0 if freshly fetched
          }
        """
        import time, asyncio, json as _json
        from datetime import datetime, timezone as _tz

        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        force = request.query.get("force", "").lower() in ("1", "true", "yes")
        now = time.time()
        cached = _conn_status_cache.get(user_id)
        if cached and not force and (now - cached[0]) < 60.0:
            out = dict(cached[1])
            out["cache_age_sec"] = int(now - cached[0])
            return web.json_response(out)

        # Load user + (optional) matching key in one query
        async with db_pool.acquire() as conn:
            user_row = await conn.fetchrow(
                "SELECT email, bot_mode FROM users WHERE id = $1", user_id,
            )
            if not user_row:
                return web.json_response({"error": "user not found"}, status=404)
            bot_mode = user_row["bot_mode"] or "paper"

            key_row = None
            expected_label = None
            if bot_mode in ("demo", "live"):
                expected_label = bot_mode
                key_row = await conn.fetchrow(
                    """SELECT api_key_enc, api_secret_enc, base_url
                       FROM user_api_keys
                       WHERE user_id = $1 AND exchange = 'delta'
                         AND label = $2 AND is_active = TRUE""",
                    user_id, expected_label,
                )

        result = {
            "bot_mode": bot_mode,
            "key_required": bot_mode in ("demo", "live"),
            "key_present": key_row is not None,
            "key_label_expected": expected_label,
            "connected": False,
            "balance_usdt": None,
            "checked_at": datetime.now(_tz.utc).isoformat(),
            "cache_age_sec": 0,
        }

        # Paper: no key needed, all good
        if bot_mode == "paper":
            result["status"] = "paper_only"
            result["severity"] = "info"
            result["message"] = "Paper trading mode — no exchange connection needed."
            _conn_status_cache[user_id] = (now, result)
            return web.json_response(result)

        # Demo/Live but no matching key
        if not key_row:
            result["status"] = "no_key"
            result["severity"] = "warning"
            result["message"] = (
                f"Bot mode is '{bot_mode}' but no active '{expected_label}' API key on file. "
                f"Upload one via the Admin panel or /api/user/api-keys."
            )
            _conn_status_cache[user_id] = (now, result)
            return web.json_response(result)

        # Probe Delta with the decrypted key
        try:
            from auth.crypto import decrypt_api_key
            api_key = decrypt_api_key(key_row["api_key_enc"], user_id=user_id)
            api_secret = decrypt_api_key(key_row["api_secret_enc"], user_id=user_id)
            base_url = key_row["base_url"] or (
                "https://cdn-ind.testnet.deltaex.org" if bot_mode == "demo"
                else "https://api.india.delta.exchange"
            )
        except Exception as e:
            result["status"] = "decrypt_failed"
            result["severity"] = "critical"
            result["message"] = f"API key decrypt failed — key may be corrupt or Fernet master rotated: {str(e)[:120]}"
            _conn_status_cache[user_id] = (now, result)
            return web.json_response(result)

        def _probe():
            try:
                from exchange.delta_balance import fetch_usd_balance
                bal = fetch_usd_balance(api_key, api_secret, base_url)
                return {"ok": True, "balance": bal}
            except Exception as e:
                msg = str(e)
                lower = msg.lower()
                if any(s in lower for s in ("unauthorized", "invalid", "forbidden", "signature", "ip_not_allowed", "ip_not_whitelisted", "api_key")):
                    return {"ok": False, "reason": "key_rejected", "error": msg[:200]}
                return {"ok": False, "reason": "delta_unreachable", "error": msg[:200]}

        try:
            probe = await asyncio.to_thread(_probe)
        except Exception as e:
            probe = {"ok": False, "reason": "delta_unreachable", "error": str(e)[:200]}

        if probe.get("ok"):
            result["connected"] = True
            result["balance_usdt"] = round(probe["balance"], 2)
            result["status"] = "ok"
            result["severity"] = "ok"
            result["message"] = (
                f"Connected to Delta {bot_mode.upper()} — balance ${probe['balance']:.2f} USDT."
            )
            # Bump last_used on the key
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE user_api_keys SET last_used = NOW()
                           WHERE user_id = $1 AND label = $2""",
                        user_id, expected_label,
                    )
            except Exception:
                pass
        elif probe.get("reason") == "key_rejected":
            result["status"] = "key_rejected"
            result["severity"] = "critical"
            result["message"] = (
                f"Delta rejected the {expected_label} API key (invalid / revoked / IP not allowed). "
                f"Rotate the key in Delta console and re-upload."
            )
            result["error_detail"] = probe.get("error", "")
        else:
            result["status"] = "delta_unreachable"
            result["severity"] = "warning"
            result["message"] = (
                f"Can't reach Delta right now — network issue or Delta is down. "
                f"Will retry automatically."
            )
            result["error_detail"] = probe.get("error", "")

        _conn_status_cache[user_id] = (now, result)
        return web.json_response(result)

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
        """POST /api/user/real/toggle — Enable/disable user's real trading.

        SEC FIX (2026-04-19): Before allowing a mode flip to 'demo' or 'live',
        verify that the user has at least one ACTIVE API key with the matching
        label in user_api_keys. Previously this endpoint unconditionally wrote
        new bot_mode to the users table, which produced users stuck in a
        broken state (bot_mode=live but no live key → silent fallback to
        demo key, which we just removed in Phase 2.1).
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        body = await request.json()
        new_mode = body.get("bot_mode", "paper")  # paper / demo / live
        if new_mode not in ("paper", "demo", "live"):
            return web.json_response({"error": "Invalid mode. Use: paper, demo, live"}, status=400)

        try:
            # Pre-flight: when switching to demo/live, require a matching-label key
            if new_mode in ("demo", "live"):
                required_label = new_mode  # "demo" or "live"
                async with db_pool.acquire() as conn:
                    key_row = await conn.fetchrow(
                        """
                        SELECT id FROM user_api_keys
                        WHERE user_id = $1 AND exchange = 'delta'
                          AND label = $2 AND is_active = TRUE
                        LIMIT 1
                        """,
                        user_id, required_label,
                    )
                if not key_row:
                    return web.json_response({
                        "error": f"no active '{required_label}' API key on file",
                        "hint": (
                            f"Upload a '{required_label}'-labeled API key via "
                            f"/api/user/api-keys before switching to {new_mode} mode. "
                            f"Shadow trading (paper) always works without keys."
                        ),
                        "current_mode_blocked": True,
                    }, status=400)

            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET bot_mode = $1, updated_at = NOW() WHERE id = $2",
                    new_mode, user_id,
                )

            # If switching to paper, remove existing manager
            if new_mode == "paper" and user_id in user_registry._managers:
                del user_registry._managers[user_id]
                logger.info("User %s switched to paper — manager removed", user_id[:8])

            # Any mode change invalidates cached manager so it rebuilds with new key
            if user_id in user_registry._managers and new_mode != "paper":
                del user_registry._managers[user_id]

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

    async def handle_user_dashboard(request: web.Request) -> web.Response:
        """GET /api/user/dashboard — combined paper (shared) + this user's real trades."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        # Get user's real status
        mgr = await user_registry.get_manager_for_user(user_id)
        real_status = mgr.get_status() if mgr else {"open_count": 0, "open_positions": [], "closed_trades": []}

        # Get user's recent trades from DB
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT email, bot_mode, max_leverage, max_daily_loss_pct
                    FROM users WHERE id = $1
                """, user_id)
                user_info = dict(row) if row else {}

                trades = await conn.fetch("""
                    SELECT id, symbol, side, entry_price, exit_price, pnl_usd,
                           status, opened_at, closed_at, metadata
                    FROM user_trades
                    WHERE user_id = $1 AND trade_type = 'real'
                    ORDER BY opened_at DESC LIMIT 50
                """, user_id)

                user_trades = []
                for t in trades:
                    d = dict(t)
                    d["id"] = str(d["id"])
                    for f in ("opened_at", "closed_at"):
                        if d.get(f): d[f] = d[f].isoformat()
                    user_trades.append(d)

            return web.json_response({
                "user": user_info,
                "real_status": real_status,
                "recent_trades": user_trades,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ── Register all routes ──
    app.router.add_get("/api/user/dashboard", handle_user_dashboard)
    app.router.add_get("/api/user/real/status", handle_user_real_status)
    app.router.add_post("/api/user/real/toggle", handle_user_real_toggle)
    app.router.add_get("/api/user/real/trades", handle_user_real_trades)
    app.router.add_get("/api/user/real/config", handle_user_real_config_get)
    app.router.add_put("/api/user/real/config", handle_user_real_config_put)
    app.router.add_get("/api/user/strategies", handle_user_strategies_list)
    app.router.add_post("/api/user/strategies", handle_user_strategy_create)
    app.router.add_post("/api/user/real/cb-reset", handle_user_cb_reset)
    app.router.add_get("/api/user/connection-status", handle_user_connection_status)

    logger.info("Per-user trading routes registered (9 endpoints)")
