"""Admin management endpoints — user list, role changes, audit log, API key management."""
import json
import logging
from aiohttp import web
from auth.middleware import require_role

logger = logging.getLogger(__name__)


def register_admin_routes(app: web.Application, auth_service, db_pool):
    """Register admin-only routes."""
    handler = AdminRouteHandler(auth_service, db_pool)

    app.router.add_get("/api/admin/users", require_role("admin")(handler.handle_list_users))
    app.router.add_put("/api/admin/users/{user_id}", require_role("admin")(handler.handle_update_user))
    app.router.add_get("/api/admin/sessions", require_role("admin")(handler.handle_list_sessions))
    app.router.add_delete("/api/admin/sessions/{token}", require_role("admin")(handler.handle_force_logout))
    app.router.add_get("/api/admin/audit", require_role("admin")(handler.handle_audit_log))
    app.router.add_post("/api/admin/users/{user_id}/reset-password", require_role("admin")(handler.handle_reset_password))
    app.router.add_get("/api/admin/users/{user_id}/api-keys", require_role("admin")(handler.handle_list_api_keys))
    app.router.add_post("/api/admin/users/{user_id}/api-keys", require_role("admin")(handler.handle_add_api_key))
    app.router.add_delete("/api/admin/api-keys/{key_id}", require_role("admin")(handler.handle_delete_api_key))


class AdminRouteHandler:
    def __init__(self, auth_service, db_pool):
        self.auth = auth_service
        self.pool = db_pool

    async def handle_list_users(self, request: web.Request) -> web.Response:
        """GET /api/admin/users — list all users with full config."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, email, role, tier, full_name, is_active,
                          email_verified, id_verification_status,
                          bot_mode, created_at, last_login,
                          max_leverage, max_daily_loss_pct, max_open_positions,
                          trading_pairs, preferred_leverage, risk_per_trade_pct,
                          timezone, telegram_chat_id, phone
                   FROM users ORDER BY created_at DESC"""
            )
        users = []
        for row in rows:
            u = dict(row)
            u["id"] = str(u["id"])
            for k in ["created_at", "last_login"]:
                if u.get(k):
                    u[k] = u[k].isoformat()
            users.append(u)
        return web.json_response({"users": users, "total": len(users)})

    async def handle_update_user(self, request: web.Request) -> web.Response:
        """PUT /api/admin/users/{user_id} — update role, tier, active status."""
        user_id = request.match_info.get("user_id", "")
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        allowed = {"role", "tier", "is_active", "id_verification_status",
                   "bot_mode", "max_leverage", "max_daily_loss_pct",
                   "max_open_positions", "preferred_leverage", "risk_per_trade_pct",
                   "trading_pairs", "timezone", "telegram_chat_id"}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return web.json_response({"error": "no valid fields"}, status=400)

        # Validate fields
        if "role" in updates and updates["role"] not in ("admin", "trader", "viewer"):
            return web.json_response({"error": "invalid role"}, status=400)
        if "tier" in updates and updates["tier"] not in ("free", "pro", "enterprise"):
            return web.json_response({"error": "invalid tier"}, status=400)
        if "bot_mode" in updates and updates["bot_mode"] not in ("paper", "demo", "live"):
            return web.json_response({"error": "invalid bot_mode"}, status=400)
        if "max_leverage" in updates:
            updates["max_leverage"] = max(1, min(50, int(updates["max_leverage"])))
        if "max_daily_loss_pct" in updates:
            updates["max_daily_loss_pct"] = max(0.5, min(20, float(updates["max_daily_loss_pct"])))
        if "max_open_positions" in updates:
            updates["max_open_positions"] = max(1, min(10, int(updates["max_open_positions"])))
        if "preferred_leverage" in updates:
            updates["preferred_leverage"] = max(1, min(50, int(updates["preferred_leverage"])))
        if "risk_per_trade_pct" in updates:
            updates["risk_per_trade_pct"] = max(0.1, min(10, float(updates["risk_per_trade_pct"])))
        if "trading_pairs" in updates:
            import json
            if isinstance(updates["trading_pairs"], list):
                updates["trading_pairs"] = json.dumps(updates["trading_pairs"])

        set_parts = []
        values = []
        for i, (key, val) in enumerate(updates.items(), 1):
            set_parts.append(f"{key} = ${i}")
            values.append(val)
        values.append(user_id)

        sql = f"UPDATE users SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = ${len(values)}"

        async with self.pool.acquire() as conn:
            await conn.execute(sql, *values)

        logger.info("Admin updated user %s: %s", user_id, updates)
        return web.json_response({"ok": True})

    async def handle_reset_password(self, request: web.Request) -> web.Response:
        """POST /api/admin/users/{user_id}/reset-password — admin resets a user's password."""
        import bcrypt
        user_id = request.match_info.get("user_id", "")
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        new_password = body.get("new_password", "")
        if not new_password or len(new_password) < 8:
            return web.json_response({"error": "Password must be at least 8 characters"}, status=400)

        password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(12)).decode()

        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
                password_hash, user_id,
            )
            # Also kill all their sessions to force re-login
            await conn.execute(
                "DELETE FROM sessions WHERE user_id = $1",
                user_id,
            )

        logger.warning("Admin RESET PASSWORD for user %s (sessions killed)", user_id)
        return web.json_response({"ok": True, "message": "Password reset. User will need to login again."})

    async def handle_list_api_keys(self, request: web.Request) -> web.Response:
        """GET /api/admin/users/{user_id}/api-keys — list user's API keys (masked)."""
        user_id = request.match_info.get("user_id", "")
        from auth.crypto import mask_api_key, decrypt_api_key
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT k.id, k.exchange, k.label, k.api_key_enc, k.base_url,
                          k.is_active, k.last_used, k.created_at, u.email
                   FROM user_api_keys k
                   JOIN users u ON k.user_id = u.id
                   WHERE k.user_id = $1
                   ORDER BY k.label""",
                user_id,
            )
        keys = []
        for row in rows:
            k = dict(row)
            k["id"] = str(k["id"])
            # Decrypt and mask the API key for display
            try:
                decrypted = decrypt_api_key(k["api_key_enc"])
                k["api_key_masked"] = mask_api_key(decrypted)
            except Exception:
                k["api_key_masked"] = "****"
            del k["api_key_enc"]  # Never send encrypted blob to frontend
            for field in ["last_used", "created_at"]:
                if k.get(field):
                    k[field] = k[field].isoformat()
            keys.append(k)
        return web.json_response({"keys": keys, "user_id": user_id})

    async def handle_add_api_key(self, request: web.Request) -> web.Response:
        """POST /api/admin/users/{user_id}/api-keys — add/update API key for user."""
        user_id = request.match_info.get("user_id", "")
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        api_key = body.get("api_key", "").strip()
        api_secret = body.get("api_secret", "").strip()
        label = body.get("label", "demo").strip()
        base_url = body.get("base_url", "").strip()

        if not api_key or not api_secret:
            return web.json_response({"error": "api_key and api_secret are required"}, status=400)
        if label not in ("demo", "live"):
            return web.json_response({"error": "label must be 'demo' or 'live'"}, status=400)

        from auth.crypto import encrypt_api_key
        key_enc = encrypt_api_key(api_key)
        secret_enc = encrypt_api_key(api_secret)

        async with self.pool.acquire() as conn:
            # Upsert: update if exists, insert if not
            await conn.execute("""
                INSERT INTO user_api_keys (user_id, exchange, label, api_key_enc, api_secret_enc, base_url)
                VALUES ($1, 'delta', $2, $3, $4, $5)
                ON CONFLICT (user_id, exchange, label)
                DO UPDATE SET api_key_enc = $3, api_secret_enc = $4, base_url = $5, updated_at = NOW()
            """, user_id, label, key_enc, secret_enc, base_url)

        logger.warning("Admin added %s API key for user %s", label, user_id[:8])
        return web.json_response({"ok": True, "message": f"{label} API key saved"})

    async def handle_delete_api_key(self, request: web.Request) -> web.Response:
        """DELETE /api/admin/api-keys/{key_id} — remove an API key."""
        key_id = request.match_info.get("key_id", "")
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM user_api_keys WHERE id = $1", key_id)
        logger.warning("Admin deleted API key %s", key_id[:8])
        return web.json_response({"ok": True})

    async def handle_list_sessions(self, request: web.Request) -> web.Response:
        """GET /api/admin/sessions — all active sessions."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT s.token, s.ip_address, s.created_at, s.expires_at,
                          s.last_activity, s.request_count, u.email, u.role
                   FROM sessions s
                   JOIN users u ON s.user_id = u.id
                   WHERE s.expires_at > NOW()
                   ORDER BY s.last_activity DESC"""
            )
        sessions = []
        for row in rows:
            s = dict(row)
            s["token"] = s["token"][:8] + "..."  # Mask token
            for k in ["created_at", "expires_at", "last_activity"]:
                if s.get(k):
                    s[k] = s[k].isoformat()
            sessions.append(s)
        return web.json_response({"sessions": sessions})

    async def handle_force_logout(self, request: web.Request) -> web.Response:
        """DELETE /api/admin/sessions/{token} — force logout a user."""
        token_prefix = request.match_info.get("token", "")
        async with self.pool.acquire() as conn:
            # Match by prefix (admin sees masked tokens)
            result = await conn.execute(
                "DELETE FROM sessions WHERE token LIKE $1",
                token_prefix + "%",
            )
        return web.json_response({"ok": True})

    async def handle_audit_log(self, request: web.Request) -> web.Response:
        """GET /api/admin/audit — login history."""
        limit = int(request.query.get("limit", "100"))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT lh.email, lh.ip_address, lh.success, lh.failure_reason,
                          lh.created_at
                   FROM login_history lh
                   ORDER BY lh.created_at DESC
                   LIMIT $1""",
                min(limit, 500),
            )
        entries = []
        for row in rows:
            e = dict(row)
            if e.get("created_at"):
                e["created_at"] = e["created_at"].isoformat()
            entries.append(e)
        return web.json_response({"entries": entries, "total": len(entries)})
