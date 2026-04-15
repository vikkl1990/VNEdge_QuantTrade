"""Email verification endpoints using DB tokens."""
import logging
import secrets
from datetime import datetime, timezone, timedelta
from aiohttp import web

logger = logging.getLogger("dashboard.email")


def register_email_routes(app: web.Application, db_pool):
    async def _get_user_id(request):
        s = request.get("session", {}) or {}
        u = request.get("user", {}) or {}
        return str(s.get("user_id", "") or u.get("user_id", ""))

    async def handle_email_request(request: web.Request) -> web.Response:
        """POST /api/email/send-verify — generate verification token + return link.

        In production: send an email with the link. For now returns the link directly."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT email FROM users WHERE id = $1", user_id)
            if not row:
                return web.json_response({"error": "user not found"}, status=404)
            await conn.execute("""
                INSERT INTO email_verification (user_id, token, expires_at, verified)
                VALUES ($1, $2, $3, FALSE)
                ON CONFLICT (user_id) DO UPDATE SET token = $2, expires_at = $3, verified = FALSE
            """, user_id, token, expires)
        link = f"/api/email/verify?token={token}"
        logger.info("Email verification link generated for %s: %s", row["email"], token[:8])
        return web.json_response({"ok": True, "link": link, "expires_at": expires.isoformat(), "note": "In production: email this link. For now, click it manually."})

    async def handle_email_verify(request: web.Request) -> web.Response:
        """GET /api/email/verify?token=... — mark email verified."""
        token = request.query.get("token", "").strip()
        if not token:
            return web.json_response({"error": "token required"}, status=400)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT user_id, expires_at, verified FROM email_verification WHERE token = $1
            """, token)
            if not row:
                return web.json_response({"error": "invalid token"}, status=400)
            if row["verified"]:
                return web.json_response({"ok": True, "already_verified": True})
            if row["expires_at"] < datetime.now(timezone.utc):
                return web.json_response({"error": "token expired"}, status=400)
            await conn.execute("UPDATE email_verification SET verified = TRUE WHERE token = $1", token)
            await conn.execute("UPDATE users SET email_verified = TRUE WHERE id = $1", row["user_id"])
        return web.json_response({"ok": True, "verified": True})

    async def handle_email_status(request: web.Request) -> web.Response:
        """GET /api/email/status — check if current user is verified."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT email_verified FROM users WHERE id = $1", user_id)
        return web.json_response({"verified": bool(row["email_verified"]) if row else False})

    app.router.add_post("/api/email/send-verify", handle_email_request)
    app.router.add_get("/api/email/verify", handle_email_verify)
    app.router.add_get("/api/email/status", handle_email_status)
    logger.info("Email verification routes registered (3 endpoints)")
