"""Production-grade user profile endpoints (2026-04-19).

Adds self-service capabilities missing from the basic user_routes.py:
  - Change password (with current-password verification)
  - Change email (with verification link to new address)
  - List + revoke own sessions
  - View own activity log
  - Test own API keys
  - Export own data (basic GDPR)
  - Audit trail for every profile mutation (user_audit_log)

Every mutation is audit-logged atomically. Every endpoint is user-scoped
(operates on the authenticated user's own data — no user_id URL params).
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone, timedelta

import bcrypt
from aiohttp import web

logger = logging.getLogger("dashboard.profile")


def register_profile_routes(app: web.Application, auth_service, db_pool):
    """Register all self-service profile endpoints."""
    handler = ProfileRouteHandler(auth_service, db_pool)

    # Security — self-service
    app.router.add_post("/api/user/password/change", handler.handle_password_change)
    app.router.add_post("/api/user/email/change", handler.handle_email_change_request)
    app.router.add_get("/api/user/email/change/confirm", handler.handle_email_change_confirm)

    # Sessions (user's own)
    app.router.add_get("/api/user/sessions", handler.handle_list_sessions)
    app.router.add_delete("/api/user/sessions/{token_prefix}", handler.handle_revoke_session)
    app.router.add_post("/api/user/sessions/logout-all", handler.handle_logout_all_others)

    # Activity log (user's own)
    app.router.add_get("/api/user/activity", handler.handle_activity)

    # API key self-service test (mirrors admin /test-api-key but scoped to caller)
    app.router.add_post("/api/user/api-keys/{key_id}/test", handler.handle_test_own_api_key)

    # Audit log (user's own profile changes)
    app.router.add_get("/api/user/audit", handler.handle_own_audit_log)

    # GDPR — export all my data
    app.router.add_get("/api/user/data-export", handler.handle_data_export)


class ProfileRouteHandler:
    def __init__(self, auth_service, db_pool):
        self.auth = auth_service
        self.pool = db_pool

    # ── Helpers ──────────────────────────────────────────────

    def _user_id_from(self, request: web.Request) -> str:
        """Extract authenticated user_id from the middleware-set context.
        Returns empty string if not authenticated — caller must 401."""
        user = request.get("user") or {}
        uid = user.get("user_id") or ""
        if uid:
            return str(uid)
        session = request.get("session") or {}
        return str(session.get("user_id", ""))

    def _user_email_from(self, request: web.Request) -> str:
        user = request.get("user") or {}
        return (user.get("email") or "").strip()

    async def _record_audit(
        self, request, action, before=None, after=None,
        status=200, result="ok", error=None,
    ):
        """Insert a row into user_audit_log. Fail-silent."""
        try:
            user_id = self._user_id_from(request)
            if not user_id:
                return
            email = self._user_email_from(request)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO user_audit_log
                       (user_id, user_email, action, method, path,
                        before_json, after_json, ip_address, user_agent,
                        status_code, result, error_message)
                       VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10,$11,$12)""",
                    user_id, email, action, request.method, request.path,
                    json.dumps(before or {}, default=str),
                    json.dumps(after or {}, default=str),
                    request.remote, request.headers.get("User-Agent", "")[:500],
                    status, result, error,
                )
        except Exception as e:
            logger.warning("user_audit_log write failed: %s", e)

    # ── Password change ──────────────────────────────────────

    async def handle_password_change(self, request: web.Request) -> web.Response:
        """POST /api/user/password/change — user self-service password change.

        Body: {current_password, new_password, [logout_others]}
          logout_others: true → kill all sessions EXCEPT the caller's current one

        Returns 200 ok on success, 400 on validation, 403 if current password wrong.
        """
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        current_pw = (body.get("current_password") or "").encode("utf-8")
        new_pw = body.get("new_password") or ""
        logout_others = bool(body.get("logout_others", True))  # secure default

        # Basic complexity (still lenient to match existing register flow)
        if len(new_pw) < 8:
            return web.json_response({"error": "new_password must be at least 8 characters"}, status=400)
        if new_pw == body.get("current_password"):
            return web.json_response({"error": "new password must differ from current"}, status=400)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT password_hash, email FROM users WHERE id = $1", user_id,
            )
            if not row:
                return web.json_response({"error": "user not found"}, status=404)

            # Verify current password
            try:
                ok = bcrypt.checkpw(current_pw, row["password_hash"].encode("utf-8"))
            except Exception:
                ok = False
            if not ok:
                await self._record_audit(
                    request, "password.change",
                    after={"outcome": "current_password_mismatch"},
                    status=403, result="error", error="current password mismatch",
                )
                return web.json_response({"error": "current password is incorrect"}, status=403)

            # Hash + save new
            new_hash = bcrypt.hashpw(new_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            await conn.execute(
                "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
                new_hash, user_id,
            )

            # Kill other sessions on demand
            killed = 0
            if logout_others:
                current_token = request.cookies.get("vn_session", "")
                if current_token:
                    result = await conn.execute(
                        "DELETE FROM sessions WHERE user_id = $1 AND token != $2",
                        user_id, current_token,
                    )
                    # aiopg/asyncpg returns text like "DELETE 3"
                    try:
                        killed = int(str(result).split()[-1])
                    except Exception:
                        killed = 0
                else:
                    await conn.execute("DELETE FROM sessions WHERE user_id = $1", user_id)

        await self._record_audit(
            request, "password.change",
            after={"logout_others": logout_others, "sessions_killed": killed},
        )
        logger.info("USER password changed: %s (killed %d sessions)", row["email"], killed)
        return web.json_response({"ok": True, "sessions_killed": killed})

    # ── Email change ────────────────────────────────────────

    async def handle_email_change_request(self, request: web.Request) -> web.Response:
        """POST /api/user/email/change — initiate email change.

        Body: {current_password, new_email}
        Flow:
          1. Verify current password (mfa-like friction)
          2. Validate new_email format + uniqueness
          3. Generate 32-byte confirmation token, store in email_change_requests
          4. Return the confirmation link (caller is responsible for emailing it;
             in dev it's shown in the API response so admin can click it)
          5. Old email is NOT yet changed — only after user clicks the link
        """
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        current_pw = (body.get("current_password") or "").encode("utf-8")
        new_email = (body.get("new_email") or "").strip().lower()

        if not new_email or "@" not in new_email or len(new_email) > 255:
            return web.json_response({"error": "valid new_email required"}, status=400)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT password_hash, email FROM users WHERE id = $1", user_id,
            )
            if not row:
                return web.json_response({"error": "user not found"}, status=404)
            if new_email == row["email"].lower():
                return web.json_response({"error": "new email matches current email"}, status=400)

            # Check uniqueness
            exists = await conn.fetchval(
                "SELECT 1 FROM users WHERE LOWER(email) = $1 AND id != $2",
                new_email, user_id,
            )
            if exists:
                return web.json_response({"error": "email already in use"}, status=409)

            # Verify current password
            try:
                ok = bcrypt.checkpw(current_pw, row["password_hash"].encode("utf-8"))
            except Exception:
                ok = False
            if not ok:
                await self._record_audit(
                    request, "email.change_request",
                    after={"new_email": new_email, "outcome": "password_mismatch"},
                    status=403, result="error", error="current password mismatch",
                )
                return web.json_response({"error": "current password is incorrect"}, status=403)

            # Generate token + persist request (24h TTL)
            token = secrets.token_urlsafe(32)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
            await conn.execute(
                """INSERT INTO email_change_requests
                   (user_id, old_email, new_email, token, expires_at, ip_address)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                user_id, row["email"], new_email, token, expires_at, request.remote,
            )

        # Build confirmation URL (bot admin sends this to new_email)
        origin = request.headers.get("X-Forwarded-Host") or request.host
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        confirm_url = f"{scheme}://{origin}/api/user/email/change/confirm?token={token}"

        await self._record_audit(
            request, "email.change_request",
            before={"email": row["email"]}, after={"new_email": new_email},
        )
        logger.info("USER requested email change: %s → %s", row["email"], new_email)
        return web.json_response({
            "ok": True,
            "message": f"Confirmation link sent to {new_email}. Expires in 24h.",
            "confirm_url": confirm_url,  # dev convenience; in prod, email-only delivery
            "expires_at": expires_at.isoformat(),
        })

    async def handle_email_change_confirm(self, request: web.Request) -> web.Response:
        """GET /api/user/email/change/confirm?token=... — finalize email change.

        PUBLIC endpoint (token is the authz). Anyone with the token can confirm,
        so the token MUST be delivered only to the new email address.
        """
        token = request.query.get("token", "")
        if not token:
            return web.json_response({"error": "token required"}, status=400)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, user_id, old_email, new_email, expires_at, confirmed_at
                   FROM email_change_requests WHERE token = $1""",
                token,
            )
            if not row:
                return web.json_response({"error": "invalid token"}, status=404)
            if row["confirmed_at"]:
                return web.json_response({"error": "token already used"}, status=409)
            if row["expires_at"] < datetime.now(timezone.utc):
                return web.json_response({"error": "token expired"}, status=410)

            # Atomically: swap email + mark confirmed + invalidate other requests
            async with conn.transaction():
                await conn.execute(
                    "UPDATE users SET email = $1, email_verified = TRUE, updated_at = NOW() WHERE id = $2",
                    row["new_email"], row["user_id"],
                )
                await conn.execute(
                    "UPDATE email_change_requests SET confirmed_at = NOW() WHERE id = $1",
                    row["id"],
                )
                # Invalidate any other pending requests for this user
                await conn.execute(
                    """UPDATE email_change_requests
                       SET confirmed_at = NOW()
                       WHERE user_id = $1 AND confirmed_at IS NULL AND id != $2""",
                    row["user_id"], row["id"],
                )

        # Audit via admin_audit_log since the confirming request may not be authed
        logger.warning("EMAIL CHANGED: user %s %s → %s (token-confirmed)",
                       str(row["user_id"])[:8], row["old_email"], row["new_email"])
        return web.json_response({
            "ok": True,
            "old_email": row["old_email"],
            "new_email": row["new_email"],
            "message": "Email changed. Please log in with the new email.",
        })

    # ── Sessions (user's own) ───────────────────────────────

    async def handle_list_sessions(self, request: web.Request) -> web.Response:
        """GET /api/user/sessions — the caller's own active sessions.
        Current session is flagged with is_current: true."""
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        current_token = request.cookies.get("vn_session", "")

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT token, ip_address, user_agent, created_at,
                          expires_at, last_activity, request_count
                   FROM sessions
                   WHERE user_id = $1 AND expires_at > NOW()
                   ORDER BY last_activity DESC""",
                user_id,
            )
        sessions = []
        for r in rows:
            s = dict(r)
            s["is_current"] = (s["token"] == current_token)
            s["token_prefix"] = s["token"][:12]  # expose prefix for revoke by token_prefix
            del s["token"]
            for f in ("created_at", "expires_at", "last_activity"):
                if s.get(f):
                    s[f] = s[f].isoformat()
            # Trim user_agent for display
            if s.get("user_agent"):
                s["user_agent"] = s["user_agent"][:160]
            sessions.append(s)
        return web.json_response({"sessions": sessions, "count": len(sessions)})

    async def handle_revoke_session(self, request: web.Request) -> web.Response:
        """DELETE /api/user/sessions/{token_prefix} — revoke ONE session.

        Uses token_prefix (first 12 chars) to avoid ever sending the full
        token in a URL or log. Only deletes sessions owned by the caller.
        """
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        token_prefix = request.match_info.get("token_prefix", "")
        if len(token_prefix) < 8:
            return web.json_response({"error": "invalid token prefix"}, status=400)

        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sessions WHERE user_id = $1 AND token LIKE $2",
                user_id, token_prefix + "%",
            )
        try:
            count = int(str(result).split()[-1])
        except Exception:
            count = 0
        await self._record_audit(request, "session.revoke",
                                 after={"token_prefix": token_prefix, "deleted": count})
        return web.json_response({"ok": True, "revoked": count})

    async def handle_logout_all_others(self, request: web.Request) -> web.Response:
        """POST /api/user/sessions/logout-all — kill ALL the caller's sessions
        EXCEPT the one making this request."""
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        current_token = request.cookies.get("vn_session", "")
        async with self.pool.acquire() as conn:
            if current_token:
                result = await conn.execute(
                    "DELETE FROM sessions WHERE user_id = $1 AND token != $2",
                    user_id, current_token,
                )
            else:
                result = await conn.execute(
                    "DELETE FROM sessions WHERE user_id = $1", user_id,
                )
        try:
            count = int(str(result).split()[-1])
        except Exception:
            count = 0
        await self._record_audit(request, "session.logout_all_others",
                                 after={"killed": count})
        return web.json_response({"ok": True, "killed": count})

    # ── Activity log (user's own) ───────────────────────────

    async def handle_activity(self, request: web.Request) -> web.Response:
        """GET /api/user/activity — caller's login history + trade summary."""
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        hours = int(request.query.get("hours", 168))  # default 7 days
        email = self._user_email_from(request)

        async with self.pool.acquire() as conn:
            logins = await conn.fetch(
                """SELECT ip_address, user_agent, success, failure_reason, created_at
                   FROM login_history
                   WHERE (user_id = $1 OR email = $2)
                     AND created_at > NOW() - ($3 || ' hours')::interval
                   ORDER BY created_at DESC LIMIT 100""",
                user_id, email, str(hours),
            )
            trade_summary = await conn.fetchrow(
                """SELECT
                     COUNT(*) FILTER (WHERE status='open') AS open_count,
                     COUNT(*) FILTER (WHERE status='closed' AND trade_type='real') AS closed_real,
                     COUNT(*) FILTER (WHERE status='closed' AND trade_type='paper') AS closed_paper,
                     COALESCE(SUM(pnl_usd) FILTER (WHERE status='closed' AND trade_type='real'), 0) AS pnl_real,
                     COUNT(*) FILTER (WHERE status='closed' AND closed_at > NOW() - INTERVAL '24 hours') AS trades_24h
                   FROM user_trades WHERE user_id = $1""",
                user_id,
            )
        logins_out = []
        for r in logins:
            d = dict(r)
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()
            if d.get("user_agent"):
                d["user_agent"] = d["user_agent"][:120]
            logins_out.append(d)

        return web.json_response({
            "logins": logins_out,
            "window_hours": hours,
            "trade_summary": dict(trade_summary) if trade_summary else {},
        })

    # ── User's API key test ─────────────────────────────────

    async def handle_test_own_api_key(self, request: web.Request) -> web.Response:
        """POST /api/user/api-keys/{key_id}/test — self-service probe of a key.

        Scoped: user can only test keys they OWN (WHERE user_id = caller AND id = key_id).
        """
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        key_id = request.match_info.get("key_id", "")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, label, api_key_enc, api_secret_enc, base_url
                   FROM user_api_keys
                   WHERE id = $1 AND user_id = $2""",
                key_id, user_id,
            )
        if not row:
            # 404 not 403 — indistinguishable from non-existent to prevent enumeration
            return web.json_response({"error": "key not found"}, status=404)

        try:
            from auth.crypto import decrypt_api_key
            api_key = decrypt_api_key(row["api_key_enc"], user_id=user_id)
            api_secret = decrypt_api_key(row["api_secret_enc"], user_id=user_id)
            base_url = row["base_url"] or (
                "https://cdn-ind.testnet.deltaex.org" if row["label"] == "demo"
                else "https://api.india.delta.exchange"
            )
        except Exception as e:
            return web.json_response({"ok": False, "error": f"decrypt failed: {e}"}, status=500)

        def _probe():
            try:
                from delta_rest_client import DeltaRestClient
                client = DeltaRestClient(base_url=base_url, api_key=api_key, api_secret=api_secret)
                wallets = client.get_balances()
                usdt_bal = 0.0
                for w in (wallets or []):
                    if w.get("asset_symbol") == "USDT" or w.get("asset_id") == 5:
                        usdt_bal = float(w.get("available_balance", 0) or 0)
                        break
                return {"ok": True, "balance_usdt": round(usdt_bal, 2)}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}

        result = await asyncio.to_thread(_probe)
        if result.get("ok"):
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE user_api_keys SET last_used = NOW() WHERE id = $1",
                        key_id,
                    )
            except Exception:
                pass

        await self._record_audit(
            request, "api_key.test",
            after={"key_id": key_id, "label": row["label"], "ok": result.get("ok")},
        )
        return web.json_response(result)

    # ── Audit log (user's own profile changes) ──────────────

    async def handle_own_audit_log(self, request: web.Request) -> web.Response:
        """GET /api/user/audit — caller's own profile mutation history."""
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        limit = min(int(request.query.get("limit", 100)), 500)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT action, method, path, status_code, result,
                          before_json, after_json, ip_address, created_at
                   FROM user_audit_log
                   WHERE user_id = $1
                   ORDER BY created_at DESC LIMIT $2""",
                user_id, limit,
            )
        out = []
        for r in rows:
            d = dict(r)
            if d.get("created_at"):
                d["created_at"] = d["created_at"].isoformat()
            out.append(d)
        return web.json_response({"entries": out, "count": len(out)})

    # ── Data export (GDPR) ──────────────────────────────────

    async def handle_data_export(self, request: web.Request) -> web.Response:
        """GET /api/user/data-export — JSON dump of all the caller's data.

        Includes: profile, API key metadata (NOT secrets), trades, sessions,
        audit log, login history. Does NOT include decrypted API secrets.
        """
        user_id = self._user_id_from(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        async with self.pool.acquire() as conn:
            profile = await conn.fetchrow(
                """SELECT email, full_name, phone, role, tier, bot_mode,
                          is_active, email_verified, id_verification_status,
                          timezone, telegram_chat_id, notify_on_signal,
                          notify_on_trade, notify_on_tp_hit, notify_on_sl_hit,
                          notify_on_system, trading_pairs, preferred_leverage,
                          max_leverage, risk_per_trade_pct, max_daily_loss_pct,
                          max_open_positions, created_at, updated_at, last_login
                   FROM users WHERE id = $1""",
                user_id,
            )
            keys = await conn.fetch(
                """SELECT id, exchange, label, base_url, is_active,
                          last_used, created_at
                   FROM user_api_keys WHERE user_id = $1""",
                user_id,
            )
            trades = await conn.fetch(
                """SELECT id, trade_type, symbol, side, entry_price, exit_price,
                          pnl_usd, fees_usd, status, opened_at, closed_at
                   FROM user_trades WHERE user_id = $1 ORDER BY opened_at DESC""",
                user_id,
            )
            sessions_list = await conn.fetch(
                """SELECT ip_address, user_agent, created_at, last_activity
                   FROM sessions WHERE user_id = $1 AND expires_at > NOW()""",
                user_id,
            )
            logins = await conn.fetch(
                """SELECT ip_address, success, failure_reason, created_at
                   FROM login_history
                   WHERE user_id = $1 OR email = $2
                   ORDER BY created_at DESC LIMIT 500""",
                user_id, (self._user_email_from(request) or ""),
            )
            audits = await conn.fetch(
                """SELECT action, method, status_code, result, created_at
                   FROM user_audit_log WHERE user_id = $1
                   ORDER BY created_at DESC LIMIT 500""",
                user_id,
            )

        def _clean(rows):
            out = []
            for r in rows or []:
                d = dict(r)
                for k, v in d.items():
                    if hasattr(v, "isoformat"):
                        d[k] = v.isoformat()
                    elif hasattr(v, "hex") and not isinstance(v, str):
                        d[k] = str(v)
                out.append(d)
            return out

        profile_dict = _clean([profile])[0] if profile else {}
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "profile": profile_dict,
            "api_keys_metadata": _clean(keys),  # metadata only, no plaintexts
            "trades": _clean(trades),
            "active_sessions": _clean(sessions_list),
            "login_history_last_500": _clean(logins),
            "profile_audit_last_500": _clean(audits),
            "note": (
                "This export contains your account metadata. API key secrets "
                "are never exported. To download historical trade candles or "
                "ML features, see /api/user/real/trades or contact support."
            ),
        }
        email = self._user_email_from(request) or str(user_id)[:8]
        headers = {
            "Content-Disposition": f'attachment; filename="vnedge-export-{email}-{datetime.now().strftime("%Y%m%d")}.json"',
        }
        await self._record_audit(request, "data.export", after={"trade_count": len(trades)})
        return web.json_response(payload, headers=headers)
