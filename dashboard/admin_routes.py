"""Admin management endpoints — user list, role changes, audit log."""
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
