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
    app.router.add_get("/api/admin/users/{user_id}/api-keys", handler.handle_list_api_keys)
    app.router.add_post("/api/admin/users/{user_id}/api-keys", require_role("admin")(handler.handle_add_api_key))
    app.router.add_delete("/api/admin/api-keys/{key_id}", require_role("admin")(handler.handle_delete_api_key))
    app.router.add_get("/api/admin/real-overview", handler.handle_real_overview)
    app.router.add_get("/api/admin/user-trades/{user_id}", handler.handle_user_trades)
    app.router.add_post("/api/admin/users/create", require_role("admin")(handler.handle_create_user))
    app.router.add_delete("/api/admin/users/{user_id}", require_role("admin")(handler.handle_delete_user))
    # Production-grade expansion (2026-04-19): test keys, emergency stop,
    # per-user admin dashboard, broadcast, admin audit log, Fernet rotation.
    app.router.add_post("/api/admin/users/{user_id}/test-api-key", require_role("admin")(handler.handle_test_api_key))
    app.router.add_post("/api/admin/users/{user_id}/emergency-stop", require_role("admin")(handler.handle_emergency_stop))
    app.router.add_post("/api/admin/users/{user_id}/force-mode", require_role("admin")(handler.handle_force_mode))
    app.router.add_get("/api/admin/users/{user_id}/dashboard", require_role("admin")(handler.handle_user_dashboard))
    app.router.add_get("/api/admin/failed-logins", require_role("admin")(handler.handle_failed_logins))
    app.router.add_get("/api/admin/broadcast", handler.handle_broadcast_list)  # public-auth: all users read
    app.router.add_post("/api/admin/broadcast", require_role("admin")(handler.handle_broadcast_create))
    app.router.add_delete("/api/admin/broadcast/{msg_id}", require_role("admin")(handler.handle_broadcast_delete))
    app.router.add_get("/api/admin/admin-audit", require_role("admin")(handler.handle_admin_audit_log))
    app.router.add_post("/api/admin/fernet/rotate-rewrap", require_role("admin")(handler.handle_fernet_rewrap))
    app.router.add_get("/api/admin/system-stats", require_role("admin")(handler.handle_system_stats))


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

    async def handle_create_user(self, request: web.Request) -> web.Response:
        """POST /api/admin/users/create — admin creates a new user."""
        import bcrypt, re as _re
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        email = body.get("email", "").strip().lower()
        password = body.get("password", "")
        role = body.get("role", "trader")
        tier = body.get("tier", "free")
        full_name = body.get("full_name", "").strip()

        if not email or "@" not in email:
            return web.json_response({"error": "valid email required"}, status=400)
        if not password or len(password) < 8:
            return web.json_response({"error": "password must be 8+ chars"}, status=400)
        if role not in ("admin", "trader", "viewer"):
            return web.json_response({"error": "invalid role"}, status=400)
        if tier not in ("free", "pro", "enterprise"):
            return web.json_response({"error": "invalid tier"}, status=400)

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO users (email, password_hash, role, tier, full_name, is_active, email_verified)
                    VALUES ($1, $2, $3, $4, $5, TRUE, FALSE)
                    RETURNING id, email, role, tier
                """, email, pw_hash, role, tier, full_name)
            logger.warning("Admin CREATED user: %s role=%s tier=%s", email, role, tier)
            return web.json_response({"ok": True, "user": {"id": str(row["id"]), "email": row["email"], "role": row["role"], "tier": row["tier"]}})
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                return web.json_response({"error": f"User {email} already exists"}, status=409)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_delete_user(self, request: web.Request) -> web.Response:
        """DELETE /api/admin/users/{user_id} — admin deletes a user (cascades to trades, keys, sessions)."""
        user_id = request.match_info.get("user_id", "")
        # Protect against admin deleting self
        current_user = request.get("user", {})
        if str(current_user.get("user_id", "")) == user_id:
            return web.json_response({"error": "cannot delete your own account"}, status=400)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT email FROM users WHERE id = $1", user_id)
            if not row:
                return web.json_response({"error": "user not found"}, status=404)
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        logger.warning("Admin DELETED user: %s (%s)", user_id, row["email"])
        return web.json_response({"ok": True})

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
            # Decrypt and mask the API key for display.
            # Pass user_id so the per-user cipher is tried first (new scheme);
            # falls back to master cipher for legacy ciphertexts.
            try:
                decrypted = decrypt_api_key(k["api_key_enc"], user_id=user_id)
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

        # SEC FIX (2026-04-19): encrypt under per-user derived key, not master.
        # Compromise of one user's ciphertext no longer exposes other users'.
        from auth.crypto import encrypt_for_user
        key_enc = encrypt_for_user(api_key, user_id)
        secret_enc = encrypt_for_user(api_secret, user_id)

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

    async def handle_real_overview(self, request: web.Request) -> web.Response:
        """GET /api/admin/real-overview — all users' real trading at a glance."""
        async with self.pool.acquire() as conn:
            users = await conn.fetch("""
                SELECT u.id, u.email, u.bot_mode, u.is_active,
                  COUNT(t.id) FILTER (WHERE t.trade_type='real' AND t.status='open') AS open_count,
                  COUNT(t.id) FILTER (WHERE t.trade_type='real' AND t.status='closed') AS closed_count,
                  COALESCE(SUM(t.pnl_usd) FILTER (WHERE t.trade_type='real' AND t.status='closed'), 0) AS total_pnl,
                  COUNT(t.id) FILTER (WHERE t.trade_type='real' AND t.status='closed' AND t.pnl_usd > 0) AS wins,
                  COUNT(k.id) FILTER (WHERE k.is_active) AS active_keys
                FROM users u
                LEFT JOIN user_trades t ON u.id = t.user_id
                LEFT JOIN user_api_keys k ON u.id = k.user_id
                WHERE u.is_active = TRUE
                GROUP BY u.id, u.email, u.bot_mode, u.is_active
                ORDER BY total_pnl DESC
            """)
        out = []
        for u in users:
            d = dict(u)
            d["id"] = str(d["id"])
            total = d["closed_count"] or 0
            d["wr_pct"] = round((d["wins"] / total * 100) if total > 0 else 0, 1)
            d["total_pnl"] = float(d["total_pnl"] or 0)
            out.append(d)
        return web.json_response({"users": out, "total_users": len(out)})

    async def handle_user_trades(self, request: web.Request) -> web.Response:
        """GET /api/admin/user-trades/{user_id} — drill into specific user's trades."""
        user_id = request.match_info.get("user_id", "")
        limit = int(request.query.get("limit", "100"))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, trade_type, symbol, side, entry_price, exit_price,
                  pnl_usd, fees_usd, status, opened_at, closed_at, metadata
                FROM user_trades
                WHERE user_id = $1
                ORDER BY opened_at DESC
                LIMIT $2
            """, user_id, limit)
        trades = []
        for r in rows:
            t = dict(r)
            t["id"] = str(t["id"])
            for f in ("opened_at", "closed_at"):
                if t.get(f):
                    t[f] = t[f].isoformat()
            trades.append(t)
        return web.json_response({"trades": trades, "count": len(trades)})

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

    # ══════════════════════════════════════════════════════════════
    # Production-grade admin capabilities (2026-04-19)
    # ══════════════════════════════════════════════════════════════

    async def _record_audit(
        self, request, action, target_user_id=None, before=None, after=None,
        status=200, result="ok", error=None,
    ):
        """Insert a row into admin_audit_log. Fail-silent — audit must never
        break the actual admin action."""
        try:
            admin_user = (request.get("user") or {})
            admin_id = admin_user.get("user_id")
            admin_email = admin_user.get("email")
            target_email = None
            if target_user_id:
                try:
                    async with self.pool.acquire() as conn:
                        t = await conn.fetchrow("SELECT email FROM users WHERE id = $1", target_user_id)
                    if t:
                        target_email = t["email"]
                except Exception:
                    pass
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO admin_audit_log
                       (admin_user_id, admin_email, action, target_user_id, target_email,
                        method, path, before_json, after_json, ip_address, user_agent,
                        status_code, result, error_message)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,$12,$13,$14)""",
                    admin_id, admin_email, action, target_user_id, target_email,
                    request.method, request.path,
                    json.dumps(before or {}, default=str),
                    json.dumps(after or {}, default=str),
                    request.remote, request.headers.get("User-Agent", "")[:500],
                    status, result, error,
                )
        except Exception as e:
            logger.warning("audit log write failed: %s", e)

    async def handle_test_api_key(self, request: web.Request) -> web.Response:
        """POST /api/admin/users/{user_id}/test-api-key — dry-run Delta balance
        query to validate a user's API key actually works.

        Body: {"label": "demo"|"live"}  — which of the user's keys to test.
        Returns: {"ok": bool, "balance_usdt": float|null, "error": str|null}
        """
        user_id = request.match_info.get("user_id", "")
        try:
            body = await request.json()
        except Exception:
            body = {}
        label = body.get("label", "live")
        if label not in ("demo", "live"):
            return web.json_response({"error": "label must be 'demo' or 'live'"}, status=400)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT api_key_enc, api_secret_enc, base_url
                   FROM user_api_keys
                   WHERE user_id = $1 AND label = $2 AND exchange = 'delta' AND is_active = TRUE""",
                user_id, label,
            )
        if not row:
            return web.json_response({"ok": False, "error": f"no active {label} key"}, status=404)

        try:
            from auth.crypto import decrypt_api_key
            api_key = decrypt_api_key(row["api_key_enc"], user_id=user_id)
            api_secret = decrypt_api_key(row["api_secret_enc"], user_id=user_id)
        except Exception as e:
            await self._record_audit(request, "api_key.test", user_id,
                                     after={"label": label, "ok": False}, status=500,
                                     result="error", error=str(e))
            return web.json_response({"ok": False, "error": f"decrypt failed: {e}"}, status=500)

        # Run the balance probe in a thread so we don't block the event loop
        import asyncio
        def _probe():
            try:
                from delta_rest_client import DeltaRestClient
                base_url = row["base_url"] or (
                    "https://cdn-ind.testnet.deltaex.org" if label == "demo"
                    else "https://api.india.delta.exchange"
                )
                client = DeltaRestClient(base_url=base_url, api_key=api_key, api_secret=api_secret)
                wallets = client.get_balances()
                usdt_bal = 0.0
                for w in (wallets or []):
                    if w.get("asset_symbol") == "USDT" or w.get("asset_id") == 5:
                        usdt_bal = float(w.get("available_balance", 0) or 0)
                        break
                return {"ok": True, "balance_usdt": usdt_bal, "raw_wallets": len(wallets or [])}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}
        result = await asyncio.to_thread(_probe)

        # Update last_used on success
        if result.get("ok"):
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE user_api_keys SET last_used = NOW() WHERE user_id = $1 AND label = $2",
                        user_id, label,
                    )
            except Exception:
                pass

        await self._record_audit(request, "api_key.test", user_id,
                                 after={"label": label, **result}, status=200)
        return web.json_response(result)

    async def handle_emergency_stop(self, request: web.Request) -> web.Response:
        """POST /api/admin/users/{user_id}/emergency-stop — admin override:
        immediately set user's bot_mode=paper, close their cached manager,
        trip their circuit breaker. Their open real positions remain open
        (admin must close them manually via the exchange) — this halts NEW
        entries only.
        """
        user_id = request.match_info.get("user_id", "")
        # Capture "before" snapshot
        async with self.pool.acquire() as conn:
            before = await conn.fetchrow(
                "SELECT bot_mode, is_active FROM users WHERE id = $1", user_id,
            )
        if not before:
            return web.json_response({"error": "user not found"}, status=404)

        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET bot_mode = 'paper', updated_at = NOW() WHERE id = $1",
                user_id,
            )

        # Kill the cached user_registry manager if any
        killed = False
        try:
            # Reach into the orchestrator via the dashboard's registry handle
            app = request.app
            orchestrator = app.get("orchestrator")
            if orchestrator and getattr(orchestrator, "_user_registry", None):
                if user_id in orchestrator._user_registry._managers:
                    del orchestrator._user_registry._managers[user_id]
                    killed = True
                orchestrator._user_registry._last_user_refresh = 0
        except Exception as e:
            logger.warning("emergency_stop: registry cleanup failed: %s", e)

        after = {"bot_mode": "paper", "manager_killed": killed}
        await self._record_audit(request, "user.emergency_stop", user_id,
                                 before=dict(before), after=after, status=200)
        logger.warning("ADMIN EMERGENCY STOP: user %s (previous bot_mode=%s, manager_killed=%s)",
                       user_id[:8], before["bot_mode"], killed)
        return web.json_response({"ok": True, "before": dict(before), "after": after})

    async def handle_force_mode(self, request: web.Request) -> web.Response:
        """POST /api/admin/users/{user_id}/force-mode — admin override of
        bot_mode. Bypasses the user-self pre-flight check. Use cases:
        manually staging a user, testing, emergency re-enable after fix.

        Body: {"bot_mode": "paper"|"demo"|"live"}
        """
        user_id = request.match_info.get("user_id", "")
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        new_mode = body.get("bot_mode", "")
        if new_mode not in ("paper", "demo", "live"):
            return web.json_response({"error": "bot_mode must be paper|demo|live"}, status=400)

        async with self.pool.acquire() as conn:
            before = await conn.fetchrow("SELECT bot_mode FROM users WHERE id = $1", user_id)
            if not before:
                return web.json_response({"error": "user not found"}, status=404)
            await conn.execute(
                "UPDATE users SET bot_mode = $1, updated_at = NOW() WHERE id = $2",
                new_mode, user_id,
            )

        # Invalidate cached manager
        try:
            app = request.app
            orchestrator = app.get("orchestrator")
            if orchestrator and getattr(orchestrator, "_user_registry", None):
                if user_id in orchestrator._user_registry._managers:
                    del orchestrator._user_registry._managers[user_id]
                orchestrator._user_registry._last_user_refresh = 0
        except Exception:
            pass

        await self._record_audit(request, "user.force_mode", user_id,
                                 before=dict(before), after={"bot_mode": new_mode})
        logger.warning("ADMIN FORCE MODE: user %s %s → %s", user_id[:8], before["bot_mode"], new_mode)
        return web.json_response({"ok": True, "before": dict(before), "after": {"bot_mode": new_mode}})

    async def handle_user_dashboard(self, request: web.Request) -> web.Response:
        """GET /api/admin/users/{user_id}/dashboard — combined per-user snapshot
        for the admin panel: user config + key list (masked) + open positions
        + recent trades + today's P&L + CB state."""
        user_id = request.match_info.get("user_id", "")
        from auth.crypto import decrypt_api_key, mask_api_key

        async with self.pool.acquire() as conn:
            user_row = await conn.fetchrow(
                """SELECT id, email, full_name, role, tier, bot_mode, is_active,
                          id_verification_status, max_leverage, max_daily_loss_pct,
                          max_open_positions, trading_pairs, preferred_leverage,
                          risk_per_trade_pct, created_at, last_login
                   FROM users WHERE id = $1""",
                user_id,
            )
            if not user_row:
                return web.json_response({"error": "user not found"}, status=404)
            user = dict(user_row)
            user["id"] = str(user["id"])
            for f in ("created_at", "last_login"):
                if user.get(f):
                    user[f] = user[f].isoformat()

            keys_rows = await conn.fetch(
                """SELECT id, label, is_active, base_url, api_key_enc,
                          last_used, created_at
                   FROM user_api_keys
                   WHERE user_id = $1 AND exchange = 'delta'
                   ORDER BY label""",
                user_id,
            )
            keys = []
            now_ts = None
            from datetime import datetime, timezone as _tz
            now_ts = datetime.now(_tz.utc)
            for k in keys_rows:
                d = dict(k)
                d["id"] = str(d["id"])
                try:
                    pt = decrypt_api_key(d["api_key_enc"], user_id=user_id)
                    d["api_key_masked"] = mask_api_key(pt)
                except Exception:
                    d["api_key_masked"] = "****"
                del d["api_key_enc"]
                for f in ("last_used", "created_at"):
                    if d.get(f):
                        age_days = (now_ts - d[f]).days
                        d[f + "_iso"] = d[f].isoformat()
                        d[f + "_age_days"] = age_days
                        d[f] = d[f].isoformat()
                # Stale warning: key unused > 30d or created > 90d
                d["stale"] = (
                    (d.get("created_at_age_days") or 0) > 90
                    or (d.get("last_used") is None and (d.get("created_at_age_days") or 0) > 7)
                )
                keys.append(d)

            open_rows = await conn.fetch(
                """SELECT id, trade_type, symbol, side, entry_price, quantity,
                          opened_at, metadata
                   FROM user_trades
                   WHERE user_id = $1 AND status = 'open'
                   ORDER BY opened_at DESC
                   LIMIT 20""",
                user_id,
            )
            opens = []
            for r in open_rows:
                o = dict(r)
                o["id"] = str(o["id"])
                if o.get("opened_at"):
                    o["opened_at"] = o["opened_at"].isoformat()
                opens.append(o)

            recent_rows = await conn.fetch(
                """SELECT id, trade_type, symbol, side, entry_price, exit_price,
                          pnl_usd, fees_usd, opened_at, closed_at, metadata
                   FROM user_trades
                   WHERE user_id = $1 AND status = 'closed'
                   ORDER BY closed_at DESC NULLS LAST
                   LIMIT 20""",
                user_id,
            )
            recents = []
            for r in recent_rows:
                t = dict(r)
                t["id"] = str(t["id"])
                for f in ("opened_at", "closed_at"):
                    if t.get(f):
                        t[f] = t[f].isoformat()
                recents.append(t)

            pnl_today_row = await conn.fetchrow(
                """SELECT
                       COALESCE(SUM(pnl_usd), 0) AS pnl_24h,
                       COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins_24h,
                       COUNT(*) AS trades_24h
                   FROM user_trades
                   WHERE user_id = $1 AND status = 'closed'
                     AND closed_at > NOW() - INTERVAL '24 hours'""",
                user_id,
            )
            pnl_24h = {
                "pnl_24h_usd": float(pnl_today_row["pnl_24h"] or 0),
                "wins_24h": int(pnl_today_row["wins_24h"] or 0),
                "trades_24h": int(pnl_today_row["trades_24h"] or 0),
            }
            pnl_24h["wr_24h_pct"] = round(
                (pnl_24h["wins_24h"] / pnl_24h["trades_24h"] * 100) if pnl_24h["trades_24h"] else 0, 1
            )

        return web.json_response({
            "user": user,
            "keys": keys,
            "open_positions": opens,
            "recent_trades": recents,
            "pnl_24h": pnl_24h,
        })

    async def handle_failed_logins(self, request: web.Request) -> web.Response:
        """GET /api/admin/failed-logins — brute-force visibility.
        Aggregates login_history where success=false, grouped by IP."""
        hours = int(request.query.get("hours", 24))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT ip_address, email, COUNT(*) AS attempts,
                          MAX(created_at) AS last_attempt,
                          MIN(created_at) AS first_attempt,
                          array_agg(DISTINCT failure_reason) AS reasons
                   FROM login_history
                   WHERE success = FALSE AND created_at > NOW() - ($1 || ' hours')::interval
                   GROUP BY ip_address, email
                   ORDER BY attempts DESC, last_attempt DESC
                   LIMIT 100""",
                str(hours),
            )
        out = []
        for r in rows:
            d = dict(r)
            for f in ("first_attempt", "last_attempt"):
                if d.get(f):
                    d[f] = d[f].isoformat()
            out.append(d)
        return web.json_response({"failures": out, "window_hours": hours, "total_ips": len(out)})

    async def handle_broadcast_list(self, request: web.Request) -> web.Response:
        """GET /api/admin/broadcast — active broadcast messages (all users can see)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, message, severity, starts_at, ends_at, created_at
                   FROM broadcast_messages
                   WHERE active = TRUE
                     AND starts_at <= NOW()
                     AND (ends_at IS NULL OR ends_at > NOW())
                   ORDER BY created_at DESC
                   LIMIT 5"""
            )
        msgs = []
        for r in rows:
            d = dict(r)
            for f in ("starts_at", "ends_at", "created_at"):
                if d.get(f):
                    d[f] = d[f].isoformat()
            msgs.append(d)
        return web.json_response({"messages": msgs})

    async def handle_broadcast_create(self, request: web.Request) -> web.Response:
        """POST /api/admin/broadcast — create a system-wide message."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        message = (body.get("message") or "").strip()
        if not message:
            return web.json_response({"error": "message required"}, status=400)
        severity = body.get("severity", "info")
        if severity not in ("info", "warning", "critical"):
            severity = "info"
        ends_at = body.get("ends_at")  # optional ISO string

        admin_id = (request.get("user") or {}).get("user_id")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO broadcast_messages (message, severity, ends_at, created_by)
                   VALUES ($1, $2, $3::timestamptz, $4)
                   RETURNING id""",
                message[:2000], severity, ends_at, admin_id,
            )
        await self._record_audit(request, "broadcast.create",
                                 after={"message": message[:120], "severity": severity})
        return web.json_response({"ok": True, "id": row["id"]})

    async def handle_broadcast_delete(self, request: web.Request) -> web.Response:
        """DELETE /api/admin/broadcast/{msg_id} — deactivate a broadcast."""
        msg_id = request.match_info.get("msg_id", "")
        try:
            msg_id_int = int(msg_id)
        except ValueError:
            return web.json_response({"error": "invalid id"}, status=400)
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE broadcast_messages SET active = FALSE WHERE id = $1", msg_id_int,
            )
        await self._record_audit(request, "broadcast.delete", after={"id": msg_id_int})
        return web.json_response({"ok": True})

    async def handle_admin_audit_log(self, request: web.Request) -> web.Response:
        """GET /api/admin/admin-audit — admin action audit log.
        Filters: ?admin_id=<uuid>&target_id=<uuid>&action=<str>&limit=N&offset=N"""
        admin_id = request.query.get("admin_id")
        target_id = request.query.get("target_id")
        action = request.query.get("action")
        limit = min(int(request.query.get("limit", "100")), 500)
        offset = int(request.query.get("offset", "0"))

        wheres = []
        params: list = []
        if admin_id:
            params.append(admin_id)
            wheres.append(f"admin_user_id = ${len(params)}")
        if target_id:
            params.append(target_id)
            wheres.append(f"target_user_id = ${len(params)}")
        if action:
            params.append(action)
            wheres.append(f"action = ${len(params)}")
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.extend([limit, offset])

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT id, admin_email, action, target_email, method, path,
                           status_code, result, error_message, created_at,
                           before_json, after_json, ip_address
                    FROM admin_audit_log
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT ${len(params) - 1} OFFSET ${len(params)}""",
                *params,
            )
        entries = []
        for r in rows:
            e = dict(r)
            if e.get("created_at"):
                e["created_at"] = e["created_at"].isoformat()
            entries.append(e)
        return web.json_response({"entries": entries, "total": len(entries)})

    async def handle_fernet_rewrap(self, request: web.Request) -> web.Response:
        """POST /api/admin/fernet/rotate-rewrap — re-encrypt all user_api_keys
        ciphertexts under the per-user Fernet scheme.

        Idempotent: records already under per-user cipher decrypt identically,
        get re-encrypted to identical ciphertexts. Legacy records (master-only
        ciphers) are upgraded to per-user.

        Body: {"dry_run": bool}  — if true, reports what would happen without writing.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        dry_run = bool(body.get("dry_run", False))

        from auth.crypto import decrypt_api_key, encrypt_for_user

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, user_id, label, api_key_enc, api_secret_enc FROM user_api_keys"
            )
        total = len(rows)
        ok = 0
        failed: list = []
        upgraded = 0

        if dry_run:
            for r in rows:
                try:
                    decrypt_api_key(r["api_key_enc"], user_id=str(r["user_id"]))
                    ok += 1
                except Exception as e:
                    failed.append({"id": str(r["id"]), "error": str(e)[:100]})
            return web.json_response({
                "dry_run": True, "total": total, "decryptable": ok,
                "failed": failed,
            })

        for r in rows:
            user_id = str(r["user_id"])
            try:
                pt_key = decrypt_api_key(r["api_key_enc"], user_id=user_id)
                pt_secret = decrypt_api_key(r["api_secret_enc"], user_id=user_id)
            except Exception as e:
                failed.append({"id": str(r["id"]), "error": str(e)[:100]})
                continue
            new_key = encrypt_for_user(pt_key, user_id)
            new_secret = encrypt_for_user(pt_secret, user_id)
            # Only update if ciphertext differs (indicates legacy record)
            if new_key != r["api_key_enc"] or new_secret != r["api_secret_enc"]:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE user_api_keys
                           SET api_key_enc = $1, api_secret_enc = $2, updated_at = NOW()
                           WHERE id = $3""",
                        new_key, new_secret, r["id"],
                    )
                upgraded += 1
            ok += 1

        await self._record_audit(request, "fernet.rewrap",
                                 after={"total": total, "upgraded": upgraded, "failed": len(failed)})
        return web.json_response({
            "dry_run": False, "total": total, "re_wrapped_ok": ok,
            "legacy_upgraded": upgraded, "failed": failed,
        })

    async def handle_system_stats(self, request: web.Request) -> web.Response:
        """GET /api/admin/system-stats — headline stats for admin home page."""
        async with self.pool.acquire() as conn:
            u = await conn.fetchrow(
                """SELECT
                     COUNT(*) AS total_users,
                     COUNT(*) FILTER (WHERE is_active) AS active_users,
                     COUNT(*) FILTER (WHERE bot_mode = 'live') AS live_users,
                     COUNT(*) FILTER (WHERE bot_mode = 'demo') AS demo_users,
                     COUNT(*) FILTER (WHERE role = 'admin') AS admin_users
                   FROM users"""
            )
            t = await conn.fetchrow(
                """SELECT
                     COUNT(*) FILTER (WHERE trade_type = 'real' AND status = 'open') AS open_real,
                     COUNT(*) FILTER (WHERE trade_type = 'real' AND status = 'closed') AS closed_real,
                     COUNT(*) FILTER (WHERE status = 'closed' AND closed_at > NOW() - INTERVAL '24 hours') AS trades_24h,
                     COALESCE(SUM(pnl_usd) FILTER (WHERE status = 'closed' AND closed_at > NOW() - INTERVAL '24 hours'), 0) AS pnl_24h
                   FROM user_trades"""
            )
            s = await conn.fetchrow(
                "SELECT COUNT(*) AS live_sessions FROM sessions WHERE expires_at > NOW()"
            )
            k = await conn.fetchrow(
                """SELECT
                     COUNT(*) AS total_keys,
                     COUNT(*) FILTER (WHERE is_active) AS active_keys
                   FROM user_api_keys"""
            )
            f = await conn.fetchrow(
                """SELECT COUNT(*) AS failed_logins_1h
                   FROM login_history
                   WHERE success = FALSE AND created_at > NOW() - INTERVAL '1 hour'"""
            )
        return web.json_response({
            "users": dict(u),
            "trades": dict(t),
            "sessions": dict(s),
            "keys": dict(k),
            "security": dict(f),
        })
