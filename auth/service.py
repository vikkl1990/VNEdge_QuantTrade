"""Authentication service — register, login, session management."""
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import bcrypt

logger = logging.getLogger(__name__)


class AuthService:
    """Database-backed authentication service."""

    def __init__(self, db_pool):
        self.pool = db_pool
        self.session_timeout_hours = 24
        self.max_login_attempts = 5  # per minute per IP

    async def register(
        self, email: str, password: str, full_name: str = ""
    ) -> Dict[str, Any]:
        """Create a new user account.

        Returns user dict on success, raises ValueError on failure.
        """
        # Validate email
        if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email):
            raise ValueError("Invalid email format")

        # Validate password strength
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not re.search(r"[A-Z]", password):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"[0-9]", password):
            raise ValueError("Password must contain at least one number")

        # Hash password
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()

        async with self.pool.acquire() as conn:
            # Check if email already exists
            existing = await conn.fetchval(
                "SELECT id FROM users WHERE email = $1", email.lower()
            )
            if existing:
                raise ValueError("Email already registered")

            # Create user
            row = await conn.fetchrow(
                """INSERT INTO users (email, password_hash, full_name)
                   VALUES ($1, $2, $3)
                   RETURNING id, email, role, tier, full_name, created_at""",
                email.lower(),
                password_hash,
                full_name,
            )
            logger.info("New user registered: %s (id=%s)", email, row["id"])
            return dict(row)

    async def login(
        self, email: str, password: str, ip: str = "", user_agent: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Verify credentials and create session.

        Returns {token, user_id, email, role} on success, None on failure.
        """
        async with self.pool.acquire() as conn:
            # Look up user
            row = await conn.fetchrow(
                """SELECT id, email, password_hash, role, tier, is_active, full_name
                   FROM users WHERE email = $1""",
                email.lower(),
            )

            if not row or not row["is_active"]:
                await self._log_login(conn, None, email, ip, False, "user not found or inactive")
                return None

            # Verify password
            if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
                await self._log_login(conn, row["id"], email, ip, False, "wrong password")
                return None

            # Create session token
            token = secrets.token_hex(32)
            expires = datetime.now(timezone.utc) + timedelta(hours=self.session_timeout_hours)

            await conn.execute(
                """INSERT INTO sessions (token, user_id, ip_address, user_agent, expires_at)
                   VALUES ($1, $2, $3, $4, $5)""",
                token, row["id"], ip, user_agent[:500], expires,
            )

            # Update last login
            await conn.execute(
                "UPDATE users SET last_login = NOW() WHERE id = $1", row["id"]
            )

            await self._log_login(conn, row["id"], email, ip, True, None)

            logger.info("User logged in: %s from %s", email, ip)
            return {
                "token": token,
                "user_id": str(row["id"]),
                "email": row["email"],
                "role": row["role"],
                "tier": row["tier"],
                "full_name": row["full_name"] or "",
            }

    async def verify_session(self, token: str) -> Optional[Dict[str, Any]]:
        """Validate session token. Returns user info or None."""
        if not token or len(token) != 64:
            return None

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT s.user_id, s.expires_at, u.email, u.role, u.tier,
                          u.is_active, u.full_name
                   FROM sessions s
                   JOIN users u ON s.user_id = u.id
                   WHERE s.token = $1""",
                token,
            )

            if not row:
                return None
            if row["expires_at"] < datetime.now(timezone.utc):
                await conn.execute("DELETE FROM sessions WHERE token = $1", token)
                return None
            if not row["is_active"]:
                return None

            # Update activity
            await conn.execute(
                """UPDATE sessions
                   SET last_activity = NOW(), request_count = request_count + 1
                   WHERE token = $1""",
                token,
            )

            return {
                "user_id": str(row["user_id"]),
                "email": row["email"],
                "role": row["role"],
                "tier": row["tier"],
                "full_name": row["full_name"] or "",
            }

    async def logout(self, token: str) -> None:
        """Delete session."""
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM sessions WHERE token = $1", token)

    async def get_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get full user profile."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, email, role, tier, full_name, phone,
                          address_line1, address_line2, city, state, country,
                          postal_code, id_verification_status, timezone,
                          telegram_chat_id, notify_on_signal, notify_on_trade,
                          notify_on_tp_hit, notify_on_sl_hit, notify_on_system,
                          trading_pairs, preferred_leverage, max_leverage,
                          risk_per_trade_pct, max_daily_loss_pct,
                          max_open_positions, bot_mode, is_active,
                          email_verified, created_at, updated_at, last_login
                   FROM users WHERE id = $1""",
                user_id,
            )
            if not row:
                return None
            result = dict(row)
            # Convert UUID to string
            result["id"] = str(result["id"])
            return result

    async def update_profile(
        self, user_id: str, fields: Dict[str, Any]
    ) -> bool:
        """Update user profile fields. Returns True on success."""
        # Whitelist of updatable fields
        allowed = {
            "full_name", "phone", "address_line1", "address_line2",
            "city", "state", "country", "postal_code", "timezone",
            "telegram_chat_id", "notify_on_signal", "notify_on_trade",
            "notify_on_tp_hit", "notify_on_sl_hit", "notify_on_system",
            "trading_pairs", "preferred_leverage", "max_leverage",
            "risk_per_trade_pct", "max_daily_loss_pct",
            "max_open_positions", "bot_mode",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False

        # Build SET clause
        set_parts = []
        values = []
        for i, (key, val) in enumerate(updates.items(), 1):
            set_parts.append(f"{key} = ${i}")
            values.append(val)
        values.append(user_id)  # For WHERE clause

        sql = f"UPDATE users SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = ${len(values)}"

        async with self.pool.acquire() as conn:
            await conn.execute(sql, *values)
        return True

    async def cleanup_expired_sessions(self) -> int:
        """Remove expired sessions. Returns count removed."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sessions WHERE expires_at < NOW()"
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info("Cleaned up %d expired sessions", count)
            return count

    async def _log_login(
        self, conn, user_id, email: str, ip: str, success: bool, reason: str
    ) -> None:
        """Record login attempt in audit log."""
        await conn.execute(
            """INSERT INTO login_history (user_id, email, ip_address, success, failure_reason)
               VALUES ($1, $2, $3, $4, $5)""",
            user_id, email.lower(), ip, success, reason,
        )
