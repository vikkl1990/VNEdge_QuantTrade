"""User profile and API key management endpoints."""
import json
import logging
import time
from aiohttp import web

from auth.crypto import encrypt_api_key, decrypt_api_key, mask_api_key
from auth.middleware import require_role

logger = logging.getLogger(__name__)


def register_user_routes(app: web.Application, auth_service, db_pool):
    """Register all user-related routes."""
    handler = UserRouteHandler(auth_service, db_pool)

    # Auth
    app.router.add_post("/api/register", handler.handle_register)
    app.router.add_post("/api/login", handler.handle_login)
    app.router.add_post("/api/logout", handler.handle_logout)
    app.router.add_get("/api/session", handler.handle_session)

    # Profile
    app.router.add_get("/api/user/profile", handler.handle_get_profile)
    app.router.add_put("/api/user/profile", handler.handle_update_profile)

    # API Keys
    app.router.add_post("/api/user/api-keys", handler.handle_save_api_keys)
    app.router.add_get("/api/user/api-keys", handler.handle_list_api_keys)
    app.router.add_delete("/api/user/api-keys/{key_id}", handler.handle_delete_api_key)

    # Settings
    app.router.add_get("/api/user/settings", handler.handle_get_settings)
    app.router.add_put("/api/user/settings", handler.handle_update_settings)


class UserRouteHandler:
    def __init__(self, auth_service, db_pool):
        self.auth = auth_service
        self.pool = db_pool

    # ── Registration ──────────────────────────────────────

    async def handle_register(self, request: web.Request) -> web.Response:
        """POST /api/register — create new account."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        email = body.get("email", "").strip()
        password = body.get("password", "")
        full_name = body.get("full_name", "").strip()

        if not email or not password:
            return web.json_response({"error": "email and password required"}, status=400)

        try:
            user = await self.auth.register(email, password, full_name)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

        # Auto-login after registration
        ip = request.headers.get("X-Forwarded-For", request.remote or "")
        ua = request.headers.get("User-Agent", "")
        login_result = await self.auth.login(email, password, ip, ua)

        if login_result:
            resp = web.json_response({
                "ok": True,
                "user": {
                    "email": user["email"],
                    "role": user["role"],
                    "tier": user["tier"],
                    "full_name": full_name,
                },
            })
            resp.set_cookie(
                "vn_session", login_result["token"],
                max_age=86400, httponly=True, samesite="Lax",
            )
            return resp

        return web.json_response({"ok": True, "user": user})

    # ── Login / Logout ────────────────────────────────────

    async def handle_login(self, request: web.Request) -> web.Response:
        """POST /api/login — authenticate user."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        email = body.get("email", body.get("username", "")).strip()
        password = body.get("password", "")

        if not email or not password:
            return web.json_response({"error": "email and password required"}, status=400)

        ip = request.headers.get("X-Forwarded-For", request.remote or "")
        ua = request.headers.get("User-Agent", "")

        result = await self.auth.login(email, password, ip, ua)
        if not result:
            return web.json_response({"error": "invalid credentials"}, status=401)

        resp = web.json_response({
            "ok": True,
            "user": {
                "email": result["email"],
                "role": result["role"],
                "tier": result["tier"],
                "full_name": result.get("full_name", ""),
            },
        })
        resp.set_cookie(
            "vn_session", result["token"],
            max_age=86400, httponly=True, samesite="Lax",
        )
        return resp

    async def handle_logout(self, request: web.Request) -> web.Response:
        """POST /api/logout — clear session."""
        cookie = request.cookies.get("vn_session", "")
        if cookie:
            token = cookie.split(":", 1)[0]
            await self.auth.logout(token)

        resp = web.json_response({"ok": True})
        resp.del_cookie("vn_session")
        return resp

    async def handle_session(self, request: web.Request) -> web.Response:
        """GET /api/session — return current user info."""
        user = request.get("user")
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({
            "user": user.get("email", ""),
            "role": user.get("role", ""),
            "tier": user.get("tier", ""),
            "full_name": user.get("full_name", ""),
            "auth_enabled": True,
        })

    # ── Profile ───────────────────────────────────────────

    async def handle_get_profile(self, request: web.Request) -> web.Response:
        """GET /api/user/profile — full profile for current user."""
        user = request.get("user")
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)

        profile = await self.auth.get_user_profile(user["user_id"])
        if not profile:
            return web.json_response({"error": "user not found"}, status=404)

        # Remove sensitive fields
        profile.pop("password_hash", None)

        # Convert datetime objects to ISO strings
        for key in ["created_at", "updated_at", "last_login"]:
            if profile.get(key):
                profile[key] = profile[key].isoformat()

        return web.json_response(profile)

    async def handle_update_profile(self, request: web.Request) -> web.Response:
        """PUT /api/user/profile — update profile fields."""
        user = request.get("user")
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        success = await self.auth.update_profile(user["user_id"], body)
        if success:
            return web.json_response({"ok": True})
        return web.json_response({"error": "no valid fields to update"}, status=400)

    # ── API Keys ──────────────────────────────────────────

    async def handle_save_api_keys(self, request: web.Request) -> web.Response:
        """POST /api/user/api-keys — save/update encrypted API keys."""
        user = request.get("user")
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        exchange = body.get("exchange", "delta")
        label = body.get("label", "")  # "demo" or "live"
        api_key = body.get("api_key", "")
        api_secret = body.get("api_secret", "")
        base_url = body.get("base_url", "")

        if not label or label not in ("demo", "live"):
            return web.json_response({"error": "label must be 'demo' or 'live'"}, status=400)
        if not api_key or not api_secret:
            return web.json_response({"error": "api_key and api_secret required"}, status=400)

        # Encrypt keys
        key_enc = encrypt_api_key(api_key)
        secret_enc = encrypt_api_key(api_secret)

        async with self.pool.acquire() as conn:
            # Upsert
            await conn.execute(
                """INSERT INTO user_api_keys (user_id, exchange, label, api_key_enc, api_secret_enc, base_url)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (user_id, exchange, label)
                   DO UPDATE SET api_key_enc = $4, api_secret_enc = $5, base_url = $6, updated_at = NOW()""",
                user["user_id"], exchange, label, key_enc, secret_enc, base_url,
            )

        logger.info("API key saved: user=%s exchange=%s label=%s", user["email"], exchange, label)
        return web.json_response({"ok": True, "label": label, "exchange": exchange})

    async def handle_list_api_keys(self, request: web.Request) -> web.Response:
        """GET /api/user/api-keys — list API keys (masked)."""
        user = request.get("user")
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, exchange, label, base_url, is_active, last_used, created_at, api_key_enc
                   FROM user_api_keys
                   WHERE user_id = $1
                   ORDER BY exchange, label""",
                user["user_id"],
            )

        keys = []
        for row in rows:
            # Decrypt only to get last 4 chars for masking
            try:
                plain_key = decrypt_api_key(row["api_key_enc"])
                masked = mask_api_key(plain_key)
            except Exception:
                masked = "****"

            keys.append({
                "id": str(row["id"]),
                "exchange": row["exchange"],
                "label": row["label"],
                "api_key_masked": masked,
                "base_url": row["base_url"] or "",
                "is_active": row["is_active"],
                "last_used": row["last_used"].isoformat() if row["last_used"] else None,
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            })

        return web.json_response({"keys": keys})

    async def handle_delete_api_key(self, request: web.Request) -> web.Response:
        """DELETE /api/user/api-keys/{key_id} — remove an API key."""
        user = request.get("user")
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)

        key_id = request.match_info.get("key_id", "")
        if not key_id:
            return web.json_response({"error": "key_id required"}, status=400)

        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM user_api_keys WHERE id = $1 AND user_id = $2",
                key_id, user["user_id"],
            )

        deleted = int(result.split()[-1]) if result else 0
        if deleted:
            logger.info("API key deleted: user=%s key_id=%s", user["email"], key_id)
            return web.json_response({"ok": True})
        return web.json_response({"error": "key not found"}, status=404)

    # ── Settings ──────────────────────────────────────────

    async def handle_get_settings(self, request: web.Request) -> web.Response:
        """GET /api/user/settings — trading preferences."""
        user = request.get("user")
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)

        profile = await self.auth.get_user_profile(user["user_id"])
        if not profile:
            return web.json_response({"error": "user not found"}, status=404)

        return web.json_response({
            "trading_pairs": profile.get("trading_pairs", []),
            "preferred_leverage": profile.get("preferred_leverage", 5),
            "max_leverage": profile.get("max_leverage", 20),
            "risk_per_trade_pct": profile.get("risk_per_trade_pct", 1.0),
            "max_daily_loss_pct": profile.get("max_daily_loss_pct", 3.0),
            "max_open_positions": profile.get("max_open_positions", 3),
            "bot_mode": profile.get("bot_mode", "paper"),
        })

    async def handle_update_settings(self, request: web.Request) -> web.Response:
        """PUT /api/user/settings — update trading preferences."""
        user = request.get("user")
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        # Only allow settings fields
        allowed = {
            "trading_pairs", "preferred_leverage", "max_leverage",
            "risk_per_trade_pct", "max_daily_loss_pct",
            "max_open_positions", "bot_mode",
        }
        updates = {k: v for k, v in body.items() if k in allowed}

        # Validate
        if "preferred_leverage" in updates:
            updates["preferred_leverage"] = min(max(int(updates["preferred_leverage"]), 1), 50)
        if "max_open_positions" in updates:
            updates["max_open_positions"] = min(max(int(updates["max_open_positions"]), 1), 10)
        if "bot_mode" in updates and updates["bot_mode"] not in ("paper", "live", "signal_only"):
            return web.json_response({"error": "invalid bot_mode"}, status=400)

        success = await self.auth.update_profile(user["user_id"], updates)
        if success:
            return web.json_response({"ok": True})
        return web.json_response({"error": "no valid fields"}, status=400)
