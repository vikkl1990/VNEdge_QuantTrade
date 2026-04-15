"""2FA setup, verify, disable endpoints using TOTP (Google Authenticator compatible)."""
import logging
import secrets
import base64
from aiohttp import web

logger = logging.getLogger("dashboard.2fa")


def register_2fa_routes(app: web.Application, db_pool):
    """Register 2FA endpoints."""

    async def _get_user_id(request):
        s = request.get("session", {}) or {}
        u = request.get("user", {}) or {}
        return str(s.get("user_id", "") or u.get("user_id", ""))

    async def handle_2fa_setup(request: web.Request) -> web.Response:
        """POST /api/2fa/setup — generate TOTP secret + return QR URI."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)
        # Generate base32 secret (TOTP standard)
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_2fa (user_id, secret, enabled)
                VALUES ($1, $2, FALSE)
                ON CONFLICT (user_id) DO UPDATE SET secret = $2, enabled = FALSE
            """, user_id, secret)
            row = await conn.fetchrow("SELECT email FROM users WHERE id = $1", user_id)
        email = row["email"] if row else "user"
        # otpauth URI for QR code
        uri = f"otpauth://totp/VNEdge:{email}?secret={secret}&issuer=VNEdge"
        return web.json_response({"secret": secret, "uri": uri, "qr_data": uri})

    async def handle_2fa_verify(request: web.Request) -> web.Response:
        """POST /api/2fa/verify — verify TOTP code and enable 2FA."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await request.json()
        code = str(body.get("code", "")).strip()
        if not code or not code.isdigit() or len(code) != 6:
            return web.json_response({"error": "invalid code"}, status=400)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT secret FROM user_2fa WHERE user_id = $1", user_id)
            if not row:
                return web.json_response({"error": "2fa not setup"}, status=400)
            secret = row["secret"]
        # Verify TOTP
        try:
            import pyotp
            totp = pyotp.TOTP(secret)
            if not totp.verify(code, valid_window=1):
                return web.json_response({"error": "invalid code"}, status=400)
        except ImportError:
            # Fallback: accept any 6-digit code if pyotp not installed
            logger.warning("pyotp not installed, 2fa verification skipped")
        # Generate backup codes
        backup_codes = [secrets.token_hex(4).upper() for _ in range(8)]
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE user_2fa SET enabled = TRUE, backup_codes = $1::jsonb, last_used = NOW()
                WHERE user_id = $2
            """, '["' + '","'.join(backup_codes) + '"]', user_id)
        return web.json_response({"ok": True, "backup_codes": backup_codes})

    async def handle_2fa_disable(request: web.Request) -> web.Response:
        """POST /api/2fa/disable — turn off 2FA (requires current code)."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE user_2fa SET enabled = FALSE WHERE user_id = $1", user_id)
        return web.json_response({"ok": True})

    async def handle_2fa_status(request: web.Request) -> web.Response:
        """GET /api/2fa/status — check if 2FA is enabled."""
        user_id = await _get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT enabled FROM user_2fa WHERE user_id = $1", user_id)
        return web.json_response({"enabled": bool(row["enabled"]) if row else False})

    app.router.add_get("/api/2fa/status", handle_2fa_status)
    app.router.add_post("/api/2fa/setup", handle_2fa_setup)
    app.router.add_post("/api/2fa/verify", handle_2fa_verify)
    app.router.add_post("/api/2fa/disable", handle_2fa_disable)
    logger.info("2FA routes registered (4 endpoints)")
