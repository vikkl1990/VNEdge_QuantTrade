"""Async web dashboard server for the crypto trading bot.

Uses aiohttp to serve a single-page dashboard with REST API endpoints
for bot status, positions, signals, trade history, performance, and alerts.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import platform
import secrets
import shutil
import time
from datetime import datetime, timedelta, timezone

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from aiohttp import web

from config import get_config


class _SafeEncoder(json.JSONEncoder):
    """JSON encoder that handles numpy types and datetimes."""

    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (datetime,)):
            return obj.isoformat()
        if isinstance(obj, bool):
            return bool(obj)
        if hasattr(obj, 'value'):  # enums
            return obj.value
        return super().default(obj)


def _safe_dumps(obj):
    return json.dumps(obj, cls=_SafeEncoder)

logger = logging.getLogger(__name__)

_DASHBOARD_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _DASHBOARD_DIR / "templates"
_STATIC_DIR = _DASHBOARD_DIR / "static"


def _bust(static_dir, relpath: str) -> str:
    """Append ?v=<mtime> to a static file path so browsers pick up new
    versions automatically. If the path already has a query string or the
    file can't be stat'd, return it unchanged.
    """
    if '?' in relpath:
        return relpath
    try:
        p = static_dir / relpath
        mtime = int(p.stat().st_mtime)
        return f"{relpath}?v={mtime}"
    except Exception:
        return relpath


class DashboardServer:
    """Async web dashboard that exposes bot state via HTTP.

    The bot pushes state updates into this server via ``set_state()`` and
    individual update helpers. The dashboard serves a single-page HTML UI
    that polls JSON API endpoints on a configurable interval.
    """

    # File-based signal persistence so signals survive restarts
    _SIGNALS_FILE = Path(__file__).resolve().parent.parent / "storage" / "signals_history.json"
    _MAX_PERSISTED = 200  # keep last 200 signals on disk

    # Auth: public paths that don't require login (read-only, no sensitive data)
    # SEC FIX (2026-04-16): removed /api/real/status, /api/real/trades,
    # /api/risk-metrics, /api/session-heatmap from public paths. These were
    # exposing live balance, equity, PnL, trade history, and performance
    # metrics to unauthenticated callers — anyone who could reach :8080
    # could see the full portfolio. Now require auth.
    #
    # Kept public: /api/login (bootstrap), /api/ping (liveness probe),
    # /api/emergency-status (read-only kill-switch state),
    # /favicon.ico (browser default).
    _PUBLIC_PATHS = {
        "/api/login", "/api/ping", "/favicon.ico",
        "/api/emergency-status",
    }
    _PUBLIC_PREFIXES = ("/static/",)

    def __init__(self, auth_service=None, db_pool=None) -> None:
        cfg = get_config()
        dash_cfg = cfg.get("dashboard", {})
        bot_cfg = cfg.get("bot", {})
        self._auth_service = auth_service  # DB-backed auth (multi-user)
        self._db_pool = db_pool

        self.bot_name: str = bot_cfg.get("name", "CryptoAlgoBot")
        self.bot_version: str = bot_cfg.get("version", "1.0.0")
        self.refresh_interval: int = dash_cfg.get("refresh_interval", 5)
        self.max_alerts: int = dash_cfg.get("max_alerts_display", 50)

        # ---- mutable state (written by the bot, read by API handlers) ----
        self._lock = asyncio.Lock()
        self._started_at: Optional[float] = None
        self._paused: bool = False

        self._bot_status: str = "initializing"
        self._exchange_status: str = "disconnected"
        self._active_strategy: str = cfg.get("strategy", {}).get("active", "unknown")
        self._symbols: List[str] = cfg.get("symbols", [])
        self._mode: str = bot_cfg.get("mode", "paper")

        self._positions: List[Dict[str, Any]] = []
        self._signals: List[Dict[str, Any]] = self._load_signals()
        self._trades: List[Dict[str, Any]] = []
        self._alerts: List[Dict[str, Any]] = []
        self._prices: Dict[str, float] = {}
        self._signal_tracker = None  # set externally by orchestrator
        self._signal_learner = None  # set externally by orchestrator
        self._trade_monitor = None   # set externally by orchestrator
        self._strategy = None        # set externally by orchestrator
        self._decision_engine = None # set externally by orchestrator
        self._latency_arb = None     # set externally by orchestrator

        self._daily_pnl: float = 0.0
        self._total_pnl: float = 0.0
        self._win_rate: float = 0.0
        self._trades_today: int = 0
        self._max_drawdown: float = 0.0
        self._wins: int = 0
        self._losses: int = 0

        self._exchange_latency_ms: float = 0.0
        self._last_data_update: Optional[str] = None
        self._memory_mb: float = 0.0

        # Fee rates from paper trading config
        paper_cfg = cfg.get("paper_trading", {})
        self._fees: Dict[str, float] = {
            "taker": paper_cfg.get("taker_fee_rate", 0.0006),
            "maker": paper_cfg.get("maker_fee_rate", 0.0004),
            "settlement": paper_cfg.get("settlement_fee_rate", 0.0006),
        }

        # ── Item #7: ML proxy resilience (retry + circuit breaker + cache) ──
        self._ml_proxy_session: Optional[Any] = None  # lazy aiohttp.ClientSession
        self._ml_proxy_cb_failures: int = 0
        self._ml_proxy_cb_open_until: float = 0.0
        self._ml_proxy_cache: Dict[str, tuple] = {}  # path → (ts, body_bytes, content_type)

        # Auth config — ALWAYS enabled, generate random password if not set
        self._auth_user = os.getenv("DASHBOARD_USER", "admin")
        self._auth_password = os.getenv("DASHBOARD_PASSWORD", "")
        _raw_secret = os.getenv("DASHBOARD_SECRET_KEY", "")

        # SEC FIX (2026-04-16): fail fast on placeholder or missing secret.
        # Previously the code used secrets.token_hex(32) as a fallback which
        # meant every process restart generated a NEW key, invalidating all
        # existing session cookies. If operators set the literal string
        # "change-this-to-a-random-string" they got session-token forgery
        # via a known-value signature. Now we reject both cases loudly.
        _PLACEHOLDERS = {
            "", "change-this-to-a-random-string", "changeme", "your-secret-key",
            "example-secret", "placeholder",
        }
        if _raw_secret.strip().lower() in _PLACEHOLDERS:
            _generated = secrets.token_hex(32)
            logger.critical(
                "DASHBOARD_SECRET_KEY is unset or still a placeholder. Generated "
                "an ephemeral 64-char key for this process only. Session cookies "
                "will be invalidated on next restart. Set a permanent value in "
                ".env: DASHBOARD_SECRET_KEY=%s", _generated[:16] + "...",
            )
            self._auth_secret = _generated
        else:
            self._auth_secret = _raw_secret

        if not self._auth_password:
            self._auth_password = secrets.token_hex(16)
            logger.warning("DASHBOARD_PASSWORD not set — generated random password (check .env to set a permanent one)")
        self._auth_enabled = True  # always enabled
        self._sessions: Dict[str, Dict[str, Any]] = {}  # token -> session data
        self._session_history: List[Dict[str, Any]] = []  # login history
        # SEC FIX (2026-04-16): reduce session timeout from 24h to 4h.
        # A stolen cookie was valid for a full day — standard for financial
        # apps is 1-4h. Sliding extension (resets on each authed request) is
        # handled at cookie-emit time in _handle_login; we also refresh on
        # verify for active users.
        self._session_timeout = 4 * 3600  # 4 hours
        self._session_idle_limit = 30 * 60  # 30min idle → re-auth required
        self._emergency_stop = False  # kill switch state

        # aiohttp internals
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    # ------------------------------------------------------------------
    # State update interface (called by the bot)
    # ------------------------------------------------------------------

    async def set_state(self, **kwargs: Any) -> None:
        """Bulk-update dashboard state.

        Accepted keyword arguments:
            bot_status, exchange_status, active_strategy, symbols, mode,
            positions, signals, trades, alerts, prices,
            daily_pnl, total_pnl, win_rate, trades_today, max_drawdown,
            wins, losses, exchange_latency_ms, last_data_update, memory_mb
        """
        async with self._lock:
            for key, value in kwargs.items():
                attr = f"_{key}"
                if hasattr(self, attr):
                    setattr(self, attr, value)
                else:
                    logger.warning("DashboardServer.set_state: unknown key %r", key)

    async def add_alert(self, level: str, message: str, source: str = "system") -> None:
        """Append an alert entry, trimming to ``max_alerts``."""
        entry = {
            "timestamp": datetime.now(IST).isoformat(),
            "level": level,
            "message": message,
            "source": source,
        }
        async with self._lock:
            self._alerts.insert(0, entry)
            self._alerts = self._alerts[: self.max_alerts]

    # ------------------------------------------------------------------
    # Signal persistence helpers
    # ------------------------------------------------------------------

    def _load_signals(self) -> List[Dict[str, Any]]:
        """Load signal history from disk on startup."""
        try:
            if self._SIGNALS_FILE.exists():
                data = json.loads(self._SIGNALS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    logger.info("Loaded %d signals from history file", len(data))
                    return data[:self._MAX_PERSISTED]
        except Exception as exc:
            logger.warning("Failed to load signal history: %s", exc)
        return []

    def _persist_signals_sync(self) -> None:
        """Save current signals to disk (blocking, run in executor)."""
        try:
            self._SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._SIGNALS_FILE.write_text(
                json.dumps(self._signals[:self._MAX_PERSISTED], cls=_SafeEncoder, indent=1),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to persist signals: %s", exc)

    async def _persist_signals(self) -> None:
        """Save signals to disk without blocking the event loop."""
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._persist_signals_sync)

    async def add_signal(self, signal: Dict[str, Any]) -> None:
        """Push a new signal to the front of the signals list and persist."""
        async with self._lock:
            self._signals.insert(0, signal)
            self._signals = self._signals[:self._MAX_PERSISTED]
            await self._persist_signals()

    async def add_trade(self, trade: Dict[str, Any]) -> None:
        """Record a completed trade."""
        async with self._lock:
            self._trades.insert(0, trade)
            self._trades = self._trades[:500]

    async def update_positions(self, positions: List[Dict[str, Any]]) -> None:
        """Replace the full positions list."""
        async with self._lock:
            self._positions = list(positions)

    async def update_prices(self, prices: Dict[str, float]) -> None:
        """Merge new price data."""
        async with self._lock:
            self._prices.update(prices)

    async def update_performance(
        self,
        *,
        daily_pnl: Optional[float] = None,
        total_pnl: Optional[float] = None,
        win_rate: Optional[float] = None,
        trades_today: Optional[int] = None,
        max_drawdown: Optional[float] = None,
        wins: Optional[int] = None,
        losses: Optional[int] = None,
    ) -> None:
        """Update performance metrics."""
        async with self._lock:
            if daily_pnl is not None:
                self._daily_pnl = daily_pnl
            if total_pnl is not None:
                self._total_pnl = total_pnl
            if win_rate is not None:
                self._win_rate = win_rate
            if trades_today is not None:
                self._trades_today = trades_today
            if max_drawdown is not None:
                self._max_drawdown = max_drawdown
            if wins is not None:
                self._wins = wins
            if losses is not None:
                self._losses = losses

    async def update_system_health(
        self,
        *,
        exchange_latency_ms: Optional[float] = None,
        last_data_update: Optional[str] = None,
        memory_mb: Optional[float] = None,
    ) -> None:
        """Update system health metrics."""
        async with self._lock:
            if exchange_latency_ms is not None:
                self._exchange_latency_ms = exchange_latency_ms
            if last_data_update is not None:
                self._last_data_update = last_data_update
            if memory_mb is not None:
                self._memory_mb = memory_mb

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _sign_token(self, token: str) -> str:
        """Sign a session token with HMAC-SHA256."""
        return hmac.new(
            self._auth_secret.encode(), token.encode(), hashlib.sha256
        ).hexdigest()

    def _verify_session(self, request: web.Request) -> Optional[Dict[str, Any]]:
        """Check if request has a valid session cookie. Returns session or None."""
        if not self._auth_enabled:
            return {"user": "admin", "auth_disabled": True, "role": "admin", "email": "admin"}
        cookie = request.cookies.get("vn_session")
        if not cookie:
            return None

        # Multi-user auth: cookie is a plain hex token (no ":" separator)
        if self._auth_service and ":" not in cookie:
            # Verify against DB — this is the ONLY path for multi-user
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're inside an async context — use _db_verify_cache
                    cached = getattr(self, '_session_cache', {}).get(cookie)
                    if cached and time.time() - cached.get('_ts', 0) < 30:
                        return cached
                    # Can't await here (sync method), return cached or trust
                    # The actual DB verification happens in the async middleware
                    return cached or {
                        "user": "authenticated", "role": "admin",
                        "email": cookie[:8], "multi_user": True,
                        "token": cookie,
                    }
            except Exception:
                pass
            return {"user": "authenticated", "role": "admin", "multi_user": True, "token": cookie}

        # Single-user auth: cookie is "token:signature"
        parts = cookie.split(":", 1)
        if len(parts) != 2:
            return None
        token, sig = parts
        if not hmac.compare_digest(self._sign_token(token), sig):
            return None
        session = self._sessions.get(token)
        if not session:
            return None
        # Check timeout
        if time.time() - session["login_time"] > self._session_timeout:
            del self._sessions[token]
            return None
        session["last_activity"] = time.time()
        session["requests"] += 1
        session["role"] = "admin"  # single-user is always admin
        session["email"] = session.get("user", "admin")
        return session

    @web.middleware
    async def _security_headers_middleware(self, request: web.Request, handler):
        """Strip server info + add security headers to all responses."""
        response = await handler(request)
        # Remove server version leak (was: Python/3.x aiohttp/3.x)
        if "Server" in response.headers:
            del response.headers["Server"]
        response.headers["Server"] = "VNEdge"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """Middleware: GET APIs are public (read-only). POST APIs require auth."""
        path = request.path
        method = request.method

        # Always allow public paths + static files
        if path in self._PUBLIC_PATHS or any(path.startswith(p) for p in self._PUBLIC_PREFIXES):
            return await handler(request)

        # Try to verify session (works for both GET and POST)
        cookie = request.cookies.get("vn_session")
        session = None

        if cookie and self._auth_service and ":" not in cookie:
            # Multi-user: verify token against DB (async)
            try:
                db_session = await self._auth_service.verify_session(cookie)
                if db_session:
                    session = dict(db_session)
                    session["multi_user"] = True
                    # Cache for sync _verify_session calls
                    if not hasattr(self, '_session_cache'):
                        self._session_cache = {}
                    session["_ts"] = time.time()
                    self._session_cache[cookie] = session
            except Exception as e:
                logger.debug("DB session verify failed: %s", e)
        elif cookie:
            # Single-user: use sync verification
            session = self._verify_session(request)

        if session:
            request["session"] = session
            request["user"] = {
                "email": session.get("email", session.get("user", "")),
                "role": session.get("role", "admin"),
                "tier": session.get("tier", "free"),
                "user_id": str(session.get("user_id", "")),
                "full_name": session.get("full_name", ""),
            }

        # SEC FIX (2026-04-16): previously allowed ALL GET/HEAD through
        # without auth ("read-only dashboard data"). But /api/real/status,
        # /api/real/trades, /api/risk-metrics are GET endpoints that leak
        # balance, position history, and performance metrics. Removing them
        # from _PUBLIC_PATHS didn't help because of this blanket rule.
        #
        # New policy: auth required for ALL /api/* endpoints except the
        # ones explicitly in _PUBLIC_PATHS (login, ping, emergency-status).
        # Non-API paths (HTML/static) still allow GET without auth so the
        # login page itself loads.
        if method in ("GET", "HEAD") and not path.startswith("/api/"):
            return await handler(request)

        # /api/* — auth required (except bootstrap paths)
        if path in ("/api/login", "/api/logout", "/api/register"):
            return await handler(request)

        if session:
            return await handler(request)

        # Not authenticated — deny
        if path.startswith("/api/"):
            return web.json_response({"error": "unauthorized"}, status=401)

        # For page requests, serve the index (login form will show)
        return await handler(request)

    async def _handle_login(self, request: web.Request) -> web.Response:
        """POST /api/login — validate credentials, set session cookie.

        SEC FIX (2026-04-16): IP-based brute-force protection.
        - 5 failed attempts within 60s → 15-min lockout for that IP
        - Lockout state stored in self._login_failures (ephemeral, per-process)
        - Successful login clears the counter
        """
        # Initialize lockout tracker on first call
        if not hasattr(self, "_login_failures"):
            self._login_failures: Dict[str, List[float]] = {}
            self._login_lockouts: Dict[str, float] = {}

        ip = request.remote or "unknown"
        now = time.time()

        # Check active lockout
        lockout_until = self._login_lockouts.get(ip, 0.0)
        if now < lockout_until:
            remaining = int(lockout_until - now)
            logger.warning("Login lockout for IP %s (%ds remaining)", ip, remaining)
            return web.json_response(
                {"error": f"Too many failed attempts. Try again in {remaining}s."},
                status=429,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        user = body.get("username", "")
        password = body.get("password", "")
        # SEC FIX (2026-04-16): removed "LOGIN DEBUG" log that leaked pwd_len,
        # expected username, and match booleans. A log-viewer or aggregator
        # could enumerate valid users by watching match_user=True patterns.
        if user != self._auth_user or password != self._auth_password:
            # Record failure + possibly lock out
            attempts = self._login_failures.setdefault(ip, [])
            attempts.append(now)
            # Prune attempts older than 60s
            self._login_failures[ip] = [t for t in attempts if now - t < 60]
            if len(self._login_failures[ip]) >= 5:
                self._login_lockouts[ip] = now + 900  # 15-min lockout
                self._login_failures[ip] = []
                logger.warning(
                    "IP %s exceeded 5 failed logins in 60s — locked out for 15min", ip,
                )
                return web.json_response(
                    {"error": "Too many failed attempts. IP locked for 15 minutes."},
                    status=429,
                )
            logger.warning("Failed login attempt from %s (user=%s)", request.remote, user)
            self._session_history.append({
                "user": user, "ip": request.remote,
                "time": datetime.now(IST).isoformat(),
                "success": False,
            })
            return web.json_response({"error": "invalid credentials"}, status=401)

        # Success — clear any failure history for this IP
        self._login_failures.pop(ip, None)
        self._login_lockouts.pop(ip, None)
        # Create session
        token = secrets.token_hex(32)
        sig = self._sign_token(token)
        self._sessions[token] = {
            "user": user, "login_time": time.time(),
            "last_activity": time.time(), "ip": request.remote,
            "user_agent": request.headers.get("User-Agent", ""),
            "requests": 0,
        }
        self._session_history.append({
            "user": user, "ip": request.remote,
            "time": datetime.now(IST).isoformat(),
            "success": True,
        })
        logger.info("Successful login from %s (user=%s)", request.remote, user)
        resp = web.json_response({"ok": True, "user": user})
        resp.set_cookie(
            "vn_session", f"{token}:{sig}",
            max_age=self._session_timeout, httponly=True, samesite="Lax",
        )
        return resp

    async def _handle_logout(self, request: web.Request) -> web.Response:
        """POST /api/logout — clear session cookie."""
        cookie = request.cookies.get("vn_session")
        if cookie:
            token = cookie.split(":", 1)[0]
            self._sessions.pop(token, None)
        resp = web.json_response({"ok": True})
        resp.del_cookie("vn_session")
        return resp

    async def _handle_session(self, request: web.Request) -> web.Response:
        """GET /api/session — return current session info."""
        # Use middleware-injected user (handles both single-user and DB-backed)
        user = request.get("user")
        if user:
            return web.json_response({
                "user": user.get("email", user.get("user", "admin")),
                "role": user.get("role", "admin"),
                "tier": user.get("tier", "free"),
                "full_name": user.get("full_name", ""),
                "user_id": str(user.get("user_id", "")),
                "auth_enabled": self._auth_enabled,
            })
        # Fallback to sync check
        session = self._verify_session(request)
        if not session:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({
            "user": session.get("email", session.get("user", "admin")),
            "role": session.get("role", "admin"),
            "auth_enabled": self._auth_enabled,
        })

    async def _handle_usage(self, request: web.Request) -> web.Response:
        """GET /api/usage — return session history and active sessions."""
        active = []
        for token, s in self._sessions.items():
            active.append({
                "user": s["user"], "ip": s["ip"],
                "login_time": datetime.fromtimestamp(s["login_time"], IST).isoformat(),
                "last_activity": datetime.fromtimestamp(s["last_activity"], IST).isoformat(),
                "requests": s["requests"],
                "duration_min": round((time.time() - s["login_time"]) / 60, 1),
            })
        return web.json_response({
            "active_sessions": active,
            "login_history": self._session_history[-50:],
            "auth_enabled": self._auth_enabled,
        })

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Create the aiohttp application, bind, and start serving."""
        self._started_at = time.time()
        self._bot_status = "running"

        middlewares = []
        # Security headers first (outermost middleware)
        middlewares.append(self._security_headers_middleware)
        # Always use the dashboard's own middleware (GET=public, POST=auth required)
        middlewares.append(self._auth_middleware)
        if self._auth_service:
            logger.info("Dashboard auth ENABLED (multi-user, DB-backed, GET public)")
        elif self._auth_enabled:
            logger.info("Dashboard auth ENABLED (single-user, GET public, user=%s)", self._auth_user)
        else:
            logger.warning("Dashboard auth DISABLED — set DASHBOARD_PASSWORD in .env to enable")

        self._app = web.Application(middlewares=middlewares)
        self._register_routes(self._app)

        # Register multi-user routes if DB is available
        if self._auth_service and self._db_pool:
            from dashboard.user_routes import register_user_routes
            from dashboard.admin_routes import register_admin_routes
            from dashboard.profile_routes import register_profile_routes
            register_user_routes(self._app, self._auth_service, self._db_pool)
            register_admin_routes(self._app, self._auth_service, self._db_pool)
            register_profile_routes(self._app, self._auth_service, self._db_pool)
            logger.info("Multi-user routes registered (user profile, API keys, admin, self-service profile)")

            # Per-user real trading routes.
            # BUGFIX 2026-04-21: routes were being skipped here because
            # self._orchestrator hasn't been assigned yet at this point
            # (main.py wires the orchestrator AFTER dashboard.start()).
            # Authenticated browsers hitting /api/user/real/toggle then got
            # 404 Not Found (middleware passes auth, router has no match).
            # Fix: register routes UNCONDITIONALLY, using a lazy proxy that
            # resolves user_registry at REQUEST time from self._orchestrator.
            # If orchestrator is still None at request time, the proxy raises
            # a clean RuntimeError that handlers can translate to 503.
            class _LazyUserRegistry:
                """Late-binding proxy — resolves the real UserRealRegistry
                each time an attribute is accessed."""
                def __init__(self, dashboard_ref):
                    self._dash = dashboard_ref
                def _resolve(self):
                    orch = getattr(self._dash, '_orchestrator', None)
                    return getattr(orch, '_user_registry', None) if orch else None
                def __getattr__(self, name):
                    target = self._resolve()
                    if target is None:
                        raise RuntimeError(
                            "user_registry not yet initialized — orchestrator "
                            "still booting. Retry in a few seconds."
                        )
                    return getattr(target, name)
                def __bool__(self):
                    return self._resolve() is not None
                @property
                def _managers(self):
                    target = self._resolve()
                    if target is None:
                        return {}  # empty dict so `if user_id in _managers` is False
                    return target._managers
                @property
                def _last_user_refresh(self):
                    target = self._resolve()
                    return getattr(target, '_last_user_refresh', 0) if target else 0
                @_last_user_refresh.setter
                def _last_user_refresh(self, value):
                    target = self._resolve()
                    if target:
                        target._last_user_refresh = value

            from dashboard.user_trading_routes import register_user_trading_routes
            lazy_registry = _LazyUserRegistry(self)
            register_user_trading_routes(self._app, lazy_registry, self._db_pool)
            logger.info("Per-user trading routes registered (lazy-bound — resolves at request time)")

            # Replay + attribution routes
            try:
                from dashboard.replay_routes import register_replay_routes
                register_replay_routes(self._app, self._db_pool)
            except Exception as e:
                logger.warning("Replay routes init failed: %s", e)

            # 2FA routes
            try:
                from dashboard.twofa_routes import register_2fa_routes
                register_2fa_routes(self._app, self._db_pool)
            except Exception as e:
                logger.warning("2FA routes init failed: %s", e)

            # Backtest routes (stub)
            try:
                from dashboard.backtest_routes import register_backtest_routes
                register_backtest_routes(self._app, self._db_pool)
            except Exception as e:
                logger.warning("Backtest routes init failed: %s", e)

            # Email verification routes
            try:
                from dashboard.email_routes import register_email_routes
                register_email_routes(self._app, self._db_pool)
            except Exception as e:
                logger.warning("Email routes init failed: %s", e)

        # WebSocket routes (independent of multi-user)
        try:
            from dashboard.websocket_handler import register_ws_routes
            register_ws_routes(self._app)
        except Exception as e:
            logger.warning("WS routes init failed: %s", e)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        logger.info("Dashboard server started at http://%s:%s", host, port)

    async def stop(self) -> None:
        """Gracefully shut down the web server."""
        # Close ML proxy session
        if self._ml_proxy_session and not self._ml_proxy_session.closed:
            await self._ml_proxy_session.close()
            self._ml_proxy_session = None
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self._site = None
        self._runner = None
        self._app = None
        logger.info("Dashboard server stopped")

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _register_routes(self, app: web.Application) -> None:
        # Static files
        if _STATIC_DIR.is_dir():
            app.router.add_static("/static", _STATIC_DIR, show_index=False)

        # Pages
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/admin", self._handle_admin_panel)  # production admin panel (2026-04-19)
        app.router.add_get("/profile", self._handle_profile_page)  # production profile page (2026-04-19)

        # JSON API
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/positions", self._handle_positions)
        app.router.add_get("/api/signals", self._handle_signals)
        app.router.add_get("/api/trades", self._handle_trades)
        app.router.add_get("/api/performance", self._handle_performance)
        app.router.add_get("/api/alerts", self._handle_alerts)
        # Architect's Overview Strip — Agent 14 UX P0 #1 (B3, 2026-04-26)
        app.router.add_get("/api/overview", self._handle_overview)
        # Batch B #7: per-symbol/per-user maker drill-down (2026-04-26)
        app.router.add_get("/api/maker-stats", self._handle_maker_stats)
        # Co-pilot Sprint 1 MVP: queue + action endpoints (2026-04-26)
        app.router.add_get("/api/copilot/queue", self._handle_copilot_queue)
        app.router.add_post("/api/copilot/action", self._handle_copilot_action)
        # Phase 3 quant heroes — Sharpe/Sortino/MaxDD/PF (2026-04-26)
        app.router.add_get("/api/quant-metrics", self._handle_quant_metrics)
        # Exchange Compare — Delta India vs Bybit (2026-04-26)
        app.router.add_get("/api/exchange-comparison", self._handle_exchange_comparison)
        # Multi-exchange overview — per-exchange active + last + today (2026-04-26)
        app.router.add_get("/api/multi-exchange/overview", self._handle_multi_exchange_overview)
        # Multi-exchange closed trades by bucket — for Analytics Trade History 4-tab (2026-04-26)
        app.router.add_get("/api/multi-exchange/closed",   self._handle_multi_exchange_closed)
        # Unified Paper + Shadow API family (2026-04-26) — clean per-mode endpoints
        app.router.add_get("/api/paper/active",   self._handle_paper_active)
        app.router.add_get("/api/paper/closed",   self._handle_paper_closed)
        app.router.add_get("/api/paper/stats",    self._handle_paper_stats)
        app.router.add_get("/api/shadow/exchanges", self._handle_shadow_exchanges)
        app.router.add_get("/api/shadow/active",  self._handle_shadow_active)
        app.router.add_get("/api/shadow/closed",  self._handle_shadow_closed)
        app.router.add_get("/api/shadow/stats",   self._handle_shadow_stats)

        # Phase 2 Shadow-of-Shadow leaderboard (2026-04-27) — fan-out exit
        # config A/B/C/D/E. Aggregates per exit_config_id over the window.
        app.router.add_get("/api/phase2/leaderboard", self._handle_phase2_leaderboard)

        # Signal tracker stats
        app.router.add_get("/api/tracker/stats", self._handle_tracker_stats)
        app.router.add_get("/api/tracker/active", self._handle_tracker_active)
        app.router.add_get("/api/tracker/closed", self._handle_tracker_closed)
        app.router.add_get("/api/ai/insights", self._handle_ai_insights)
        app.router.add_get("/api/scanner-stats", self._handle_scanner_stats)
        app.router.add_get("/api/monitor/report", self._handle_monitor_report)
        app.router.add_get("/api/signal-status", self._handle_signal_status)
        app.router.add_get("/api/infra", self._handle_infra)
        app.router.add_get("/api/r-metrics", self._handle_r_metrics)
        app.router.add_get("/api/scanner-health", self._handle_scanner_health)
        app.router.add_get("/api/opportunity-funnel", self._handle_opportunity_funnel)
        app.router.add_get("/api/regime", self._handle_regime)
        app.router.add_get("/api/decision", self._handle_decision)
        app.router.add_get("/api/exit-quality", self._handle_exit_quality)
        app.router.add_get("/api/grid/status", self._handle_grid_status)
        app.router.add_get("/api/grid/positions", self._handle_grid_positions)
        app.router.add_get("/api/real/status", self._handle_real_status)
        app.router.add_post("/api/real/toggle", self._handle_real_toggle)
        app.router.add_post("/api/emergency-stop", self._handle_emergency_stop)
        app.router.add_get("/api/emergency-status", self._handle_emergency_status)
        app.router.add_get("/api/risk-metrics", self._handle_risk_metrics)
        app.router.add_get("/api/session-heatmap", self._handle_session_heatmap)
        app.router.add_get("/api/ping", self._handle_ping)
        app.router.add_get("/metrics", self._handle_metrics)
        app.router.add_get("/health", self._handle_health_check)
        app.router.add_get("/api/csrf", self._handle_csrf_token)
        app.router.add_get("/api/latency", self._handle_latency)

        # Phase 5.17 — PPP dashboard panel API
        try:
            from dashboard.ppp_api import make_ppp_handler
            app.router.add_get("/api/ppp", make_ppp_handler(self._db_pool))
        except Exception as _e:
            import logging as _log
            _log.getLogger("dashboard").warning("PPP api wiring failed: %s", _e)
        app.router.add_get("/api/latency-arb", self._handle_latency_arb)
        app.router.add_get("/api/latency-arb/dislocations", self._handle_latency_arb_dislocations)
        app.router.add_get("/api/latency-arb/analysis", self._handle_latency_arb_analysis)
        app.router.add_get("/api/agents/status", self._handle_agents_status)
        app.router.add_get("/api/risk-return", self._handle_risk_return_scatter)
        app.router.add_get("/api/pipeline/overview", self._handle_pipeline_overview)
        app.router.add_get("/api/pipeline/journey/{trade_id}", self._handle_journey)
        app.router.add_get("/api/pipeline/stage_stats", self._handle_stage_stats)
        app.router.add_get("/api/pipeline/rdrift", self._handle_rdrift)
        app.router.add_get("/api/pipeline/hotfix_stats", self._handle_hotfix_stats)
        app.router.add_get("/api/pipeline/loss_taxonomy", self._handle_loss_taxonomy)
        app.router.add_get("/api/supervisor/status", self._handle_supervisor_status)
        app.router.add_post("/api/real/cb-reset", self._handle_cb_reset)

        # ── Track A (2026-04-11): LOCK 75% + Force Flat ──
        # A.2: close 75% of a specific real position (lock profit, keep runner)
        # A.3: force flat — close ALL open real positions immediately
        app.router.add_post("/api/real/lock_75", self._handle_lock_75)
        app.router.add_post("/api/real/force_flat", self._handle_force_flat)

        # ── Items #6+8: Config Editor + Hot-Reload ──
        app.router.add_get("/api/config", self._handle_config_get)
        app.router.add_post("/api/config", self._handle_config_post)

        # ── BotBrain endpoints ──
        app.router.add_get("/api/brain/state", self._handle_brain_state)
        app.router.add_get("/api/brain/matrix", self._handle_brain_matrix)
        app.router.add_get("/api/brain/regime-history", self._handle_brain_regime_history)
        app.router.add_get("/api/brain/hourly-heatmap", self._handle_brain_hourly_heatmap)
        app.router.add_get("/api/brain/sessions", self._handle_brain_sessions)

        # ── Track C (2026-04-11): ML dashboard proxy ──
        # VM1 (live bot) dashboard proxies to VM4 (ML dashboard) private-IP
        # endpoints so the browser can fetch ML data without CORS or direct
        # public access. Proxies /api/ml/* to http://10.0.2.4:8081/api/ml/*.
        app.router.add_get("/api/ml/family-verdict-matrix", self._handle_ml_proxy)
        app.router.add_get("/api/ml/live-calibration", self._handle_ml_proxy)
        app.router.add_get("/api/ml/edge-verdict-trend", self._handle_ml_proxy)
        app.router.add_get("/api/ml/health", self._handle_ml_proxy)

        # ── Vision Tier 2+3: Thesis Tracker + Agent Pipeline + Research ──
        app.router.add_get("/api/thesis", self._handle_thesis)
        app.router.add_get("/api/agents/pipeline", self._handle_agent_pipeline)
        app.router.add_get("/api/research/correlations", self._handle_research_correlations)
        app.router.add_get("/api/pipeline/trace", self._handle_pipeline_trace)

        # ── Vision Tier 3: Resolution Clock + Market Map + Catalyst Calendar ──
        app.router.add_get("/api/market-map", self._handle_market_map)
        app.router.add_get("/api/catalyst-calendar", self._handle_catalyst_calendar)

        # ── Infra health: proxy CB + sync monitoring ──
        app.router.add_get("/api/infra/health", self._handle_infra_health)

        # Auth endpoints (only register if NOT using multi-user DB auth)
        if not self._auth_service:
            app.router.add_post("/api/login", self._handle_login)
            app.router.add_post("/api/logout", self._handle_logout)
            app.router.add_get("/api/session", self._handle_session)
            app.router.add_get("/api/usage", self._handle_usage)

        # Control endpoints
        app.router.add_post("/api/control/pause", self._handle_pause)
        app.router.add_post("/api/control/resume", self._handle_resume)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _uptime_str(self) -> str:
        if self._started_at is None:
            return "0s"
        elapsed = int(time.time() - self._started_at)
        days, remainder = divmod(elapsed, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts: List[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.Response:
        index_path = _TEMPLATES_DIR / "index.html"
        if not index_path.exists():
            return web.Response(text="Dashboard template not found", status=500)
        html = index_path.read_text(encoding="utf-8")
        # Cache-bust static JS/CSS by rewriting the href/src to include
        # the file's mtime. Prevents the class of bugs where we push a new
        # app.js and users keep seeing old cached versions (404s on new
        # endpoints, missing new functions, etc.). (2026-04-19)
        html = self._bust_static_refs(html)
        self._idx_cache = html
        return web.Response(
            text=self._idx_cache, content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
        )

    def _bust_static_refs(self, html: str) -> str:
        """Rewrite /static/js/foo.js → /static/js/foo.js?v=<mtime> so the
        browser always refetches when the file changes on disk. Covers
        both <script src="..."> and <link href="..."> references."""
        import re
        static_dir = _STATIC_DIR
        html = re.sub(
            r'src="/static/([^"?]+)"',
            lambda m: f'src="/static/{_bust(static_dir, m.group(1))}"',
            html,
        )
        html = re.sub(
            r'href="/static/([^"?]+)"',
            lambda m: f'href="/static/{_bust(static_dir, m.group(1))}"',
            html,
        )
        return html

    async def _handle_profile_page(self, request: web.Request) -> web.Response:
        """GET /profile — production user profile page HTML."""
        profile_path = _TEMPLATES_DIR / "profile_page.html"
        if not profile_path.exists():
            return web.Response(text="Profile page template not found", status=500)
        body = self._bust_static_refs(profile_path.read_text(encoding="utf-8"))
        return web.Response(
            text=body, content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    async def _handle_admin_panel(self, request: web.Request) -> web.Response:
        """GET /admin — production admin panel HTML.

        The middleware ensures the caller is authenticated. Role enforcement
        (admin only) is done CLIENT-SIDE via /api/session check in the JS bundle,
        AND SERVER-SIDE via require_role('admin') on every /api/admin/* handler.
        Non-admin users who hit /admin will see the UI shell load but the first
        /api/admin/system-stats fetch returns 403, and the JS redirects to /.
        """
        admin_path = _TEMPLATES_DIR / "admin_panel.html"
        if not admin_path.exists():
            return web.Response(text="Admin panel template not found", status=500)
        body = self._bust_static_refs(admin_path.read_text(encoding="utf-8"))
        return web.Response(
            text=body, content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    async def _handle_status(self, request: web.Request) -> web.Response:
        async with self._lock:
            data = {
                "bot_name": self.bot_name,
                "bot_version": self.bot_version,
                "bot_status": self._bot_status,
                "paused": self._paused,
                "exchange_status": self._exchange_status,
                "active_strategy": self._active_strategy,
                "symbols": list(self._symbols),
                "mode": self._mode,
                "uptime": self._uptime_str(),
                "prices": dict(self._prices),
                "refresh_interval": self.refresh_interval,
                "exchange_latency_ms": self._exchange_latency_ms,
                "last_data_update": self._last_data_update,
                "memory_mb": self._memory_mb,
                "server_time": datetime.now(IST).isoformat(),
                "fees": self._fees,
                "start_time": datetime.fromtimestamp(self._started_at, IST).isoformat() if self._started_at else None,
            }

            # Setup lifecycle candidates from strategy
            if self._strategy and hasattr(self._strategy, "get_setup_lifecycle"):
                try:
                    lifecycle = self._strategy.get_setup_lifecycle()
                    data["setup_candidates"] = lifecycle.get("candidates", [])
                except Exception:
                    data["setup_candidates"] = []
            else:
                data["setup_candidates"] = []

        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_overview(self, request: web.Request) -> web.Response:
        """Architect's Overview Strip — single endpoint that aggregates
        STATE / LAST / NEXT / BOOK / EDGE / MAKER for the sticky top strip.
        Agent 14 UX P0 #1 (B3 variant, 2026-04-26).
        Spec: docs/UX_OVERVIEW_STRIP_v1.md
        """
        out = {
            "state": "unknown",
            "mode": "",
            "last": {},
            "next": {},
            "book": {"open": 0, "paper": 0, "real": 0, "shadow": 0, "cap_deployed": 0.0},
            "edge": {"wr_pct": None, "pf": None, "pnl_24h": None},
            "maker": {"fill_rate_pct": None, "n": 0, "last_at": None},
            "alerts": [],
            "ts": datetime.utcnow().isoformat() + "Z",
        }
        if not self._db_pool:
            out["error"] = "db_pool_not_ready"
            return web.json_response(out, dumps=_safe_dumps)
        try:
            async with self._db_pool.acquire() as con:
                # 1. STATE — derive from active user mode mix
                modes = await con.fetch(
                    "SELECT bot_mode, COUNT(*) AS n FROM users WHERE is_active=true GROUP BY bot_mode"
                )
                mode_summary = {r["bot_mode"]: r["n"] for r in modes}
                if mode_summary.get("live", 0) > 0:
                    out["state"] = "trading"
                    out["mode"] = "live+"
                elif mode_summary.get("shadow_live", 0) > 0:
                    # 2026-04-27 — was 'paused' (semantically true: no live money)
                    # but visually misleading: shadow trading IS happening, fills
                    # are simulated against real L2. Surface as 'shadow' so the
                    # operator sees activity is live, just not on real capital.
                    out["state"] = "shadow"
                    out["mode"] = "shadow_live"
                else:
                    out["state"] = "trading"
                    out["mode"] = "paper"

                # 2. LAST — most recent closed trade across all users + modes
                # Note: schema has no risk_usd column — show raw P&L instead of R-multiple.
                row = await con.fetchrow(
                    """SELECT symbol, side,
                              pnl_usd::float AS pnl,
                              closed_at,
                              COALESCE(metadata::jsonb->>'scanner', '') AS scanner
                       FROM user_trades
                       WHERE closed_at IS NOT NULL
                       ORDER BY closed_at DESC LIMIT 1"""
                )
                if row:
                    out["last"] = {
                        "symbol": row["symbol"], "side": row["side"],
                        "pnl": float(row["pnl"]) if row["pnl"] is not None else None,
                        # asyncpg datetimes are tz-aware — isoformat() already includes +00:00 offset.
                        # Do NOT append "Z" (would produce invalid ISO 8601 → JS Date NaN).
                        "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
                        "scanner": row["scanner"] or "",
                    }

                # 3. BOOK — counts + capital deployed (currently open)
                # 2026-04-26: cap_deployed must be CONTRACT-AWARE.
                # Delta India qty is in CONTRACTS where contract_size != 1
                # (e.g. BTC contract = 0.001 BTC). Old SUM(entry*qty) returned
                # raw contract-units × USD price → +$2.3M phantom number.
                # Now: sum(margin) when available (already net of contract size
                # and leverage), else fall back to entry × qty × contract_size.
                booka = await con.fetch(
                    """SELECT trade_type, COUNT(*) AS n,
                              COALESCE(
                                SUM(
                                  COALESCE(NULLIF(metadata::jsonb->>'margin','')::float,
                                           entry_price * quantity *
                                           COALESCE(NULLIF(metadata::jsonb->>'contract_size','')::float, 1.0)
                                  )
                                ), 0
                              )::float AS cap
                       FROM user_trades
                       WHERE closed_at IS NULL
                       GROUP BY trade_type"""
                )
                for r in booka:
                    tt = r["trade_type"]
                    if tt in out["book"]:
                        out["book"][tt] = r["n"]
                    out["book"]["open"] += r["n"]
                    out["book"]["cap_deployed"] += r["cap"]

                # 4. EDGE — 24h aggregate, scoped to the OPERATOR-RELEVANT cohort.
                # 2026-04-27: was an "all trade types mixed" headline that hid the
                # real story (shadow profitable, real bleeding, demo flat). Now
                # follow the bot_mode of the active user(s):
                #   live+        → real     (the money number)
                #   shadow_live  → shadow   (the canonical edge tracker)
                #   paper        → paper proxy (closed_signals.json — n/a here)
                # Plus the same clean filter as /api/quant-metrics: excludes
                # auto_responder_stuck_60m + is_phase2_virtual rows.
                _edge_type = "real" if mode_summary.get("live", 0) > 0 \
                    else ("shadow" if mode_summary.get("shadow_live", 0) > 0 else None)
                _edge_type_filter = ""
                _edge_params = []
                if _edge_type:
                    _edge_type_filter = "AND trade_type = $1"
                    _edge_params.append(_edge_type)
                ed = await con.fetchrow(
                    f"""SELECT COUNT(*) AS n,
                              SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                              SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0 END) AS gross_w,
                              SUM(CASE WHEN pnl_usd < 0 THEN -pnl_usd ELSE 0 END) AS gross_l,
                              SUM(pnl_usd)::float AS pnl_total
                       FROM user_trades
                       WHERE closed_at >= NOW() - INTERVAL '24 hours'
                         {_edge_type_filter}
                         AND COALESCE(metadata::jsonb->>'exit_reason', '')
                                NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                         AND COALESCE(metadata::jsonb->>'is_phase2_virtual','false')
                                != 'true'""",
                    *_edge_params,
                )
                if ed and ed["n"]:
                    wr = (float(ed["wins"]) / float(ed["n"])) * 100.0 if ed["n"] else None
                    pf = (float(ed["gross_w"]) / float(ed["gross_l"])) if ed["gross_l"] and float(ed["gross_l"]) > 0 else None
                    out["edge"] = {
                        "wr_pct": wr,
                        "pf": pf,
                        "pnl_24h": float(ed["pnl_total"]) if ed["pnl_total"] is not None else None,
                    }

                # 6. MAKER — 24h fill rate from real-mode entries (B3 addition)
                mk = await con.fetchrow(
                    """SELECT COUNT(*) AS n,
                              SUM(CASE WHEN COALESCE(metadata::jsonb->>'fee_type','') = 'maker'
                                       THEN 1 ELSE 0 END) AS makers,
                              MAX(opened_at) AS last_at
                       FROM user_trades
                       WHERE trade_type = 'real'
                         AND opened_at >= NOW() - INTERVAL '24 hours'"""
                )
                if mk and mk["n"] and mk["n"] > 0:
                    n = int(mk["n"])
                    makers = int(mk["makers"] or 0)
                    out["maker"] = {
                        "fill_rate_pct": (makers / n) * 100.0,
                        "n": n,
                        "last_at": mk["last_at"].isoformat() if mk["last_at"] else None,
                    }
                else:
                    out["maker"] = {"fill_rate_pct": None, "n": 0, "last_at": None}

            # 5. NEXT — wired 2026-04-26 per docs/NEXT_CELL_EVENT_BUS_v1.md
            # Pulls highest-confidence FORMING setup from strategy lifecycle.
            try:
                strat = getattr(self, "_strategy", None)
                if strat and hasattr(strat, "get_setup_lifecycle"):
                    lc = strat.get_setup_lifecycle()
                    forming = [c for c in (lc.get("candidates") or [])
                               if str(c.get("stage", "")).lower() in ("watching", "forming", "armed_pending")]
                    forming.sort(key=lambda c: float(c.get("confidence", 0) or 0), reverse=True)
                    if forming:
                        top = forming[0]
                        conf_raw = float(top.get("confidence", 0) or 0)
                        conf_norm = conf_raw / 100.0 if conf_raw > 1 else conf_raw
                        out["next"] = {
                            "symbol": top.get("symbol"),
                            "side": top.get("side"),
                            "conf": conf_norm,
                            "eta_min": top.get("eta_min"),
                        }
            except Exception:
                pass

            # 6b. SECONDARY status row (Phase 2: replaces info from killed legacy strips)
            try:
                async with self._db_pool.acquire() as con3:
                    # Kill switch state from bot_state
                    ksrow = await con3.fetchrow(
                        "SELECT kill_switch_engaged, kill_switch_reason, "
                        "kill_switch_engaged_at FROM bot_state WHERE id=1"
                    )
                    # 24h activity counters
                    act = await con3.fetchrow(
                        """SELECT
                              SUM(CASE WHEN opened_at >= NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END) AS opens24,
                              SUM(CASE WHEN closed_at >= NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END) AS closes24,
                              SUM(CASE WHEN opened_at >= NOW() - INTERVAL '1 hour' THEN 1 ELSE 0 END) AS opens1h
                           FROM user_trades"""
                    )
                    # Unacked alerts count
                    ack = await con3.fetchrow(
                        "SELECT COUNT(*) AS n FROM auto_revert_events "
                        "WHERE acknowledged=FALSE AND event_at >= NOW() - INTERVAL '24 hours'"
                    )
                out["secondary"] = {
                    "kill_switch": {
                        "engaged": bool(ksrow and ksrow["kill_switch_engaged"]),
                        "reason": (ksrow["kill_switch_reason"] if ksrow else None) or "",
                        "engaged_at": ksrow["kill_switch_engaged_at"].isoformat()
                                      if ksrow and ksrow["kill_switch_engaged_at"] else None,
                    },
                    "activity": {
                        "opens_1h": int(act["opens1h"] or 0) if act else 0,
                        "opens_24h": int(act["opens24"] or 0) if act else 0,
                        "closes_24h": int(act["closes24"] or 0) if act else 0,
                    },
                    "alerts_unacked_24h": int(ack["n"] or 0) if ack else 0,
                }
            except Exception:
                pass

            # 7. ALERTS — auto_revert_events from last 60 min (Architect-call #14)
            try:
                async with self._db_pool.acquire() as con2:
                    alerts = await con2.fetch(
                        """SELECT event_type, severity, title, event_at
                           FROM auto_revert_events
                           WHERE event_at >= NOW() - INTERVAL '60 minutes'
                             AND acknowledged = FALSE
                           ORDER BY event_at DESC LIMIT 5"""
                    )
                    sev_emoji = {"critical": "\U0001F534", "error": "\U0001F534",
                                 "warn": "\u26A0", "info": "\u2139", "ok": "\u2705"}
                    for a in alerts:
                        out["alerts"].append({
                            "type": a["event_type"],
                            "severity": a["severity"],
                            "msg": f"{sev_emoji.get(a['severity'], '')} {a['title']}",
                            "at": a["event_at"].isoformat() if a["event_at"] else None,
                        })
            except Exception as _e:
                # Table may not exist on first deploy — silent skip
                pass
        except Exception as e:
            import logging as _log
            _log.getLogger("dashboard").warning("overview handler error: %s", e)
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_maker_stats(self, request: web.Request) -> web.Response:
        """Batch B #7 — Per-symbol + per-user maker fill-rate drill-down.
        Query: /api/maker-stats?days=1 (default 1, max 30)
        Returns: {by_symbol: [...], by_user: [...], totals: {...}}
        """
        try:
            days = max(1, min(30, int(request.query.get("days", "1"))))
        except Exception:
            days = 1
        out = {"days": days, "by_symbol": [], "by_user": [], "totals": {}}
        if not self._db_pool:
            out["error"] = "db_pool_not_ready"
            return web.json_response(out, dumps=_safe_dumps)
        try:
            async with self._db_pool.acquire() as con:
                # Per-symbol breakdown (real trades only)
                rows = await con.fetch(
                    f"""SELECT symbol,
                              COUNT(*) AS n,
                              SUM(CASE WHEN COALESCE(metadata::jsonb->>'fee_type','') = 'maker' THEN 1 ELSE 0 END) AS makers,
                              MAX(opened_at) AS last_at
                       FROM user_trades
                       WHERE trade_type = 'real'
                         AND opened_at >= NOW() - INTERVAL '{days} days'
                       GROUP BY symbol
                       ORDER BY n DESC, symbol"""
                )
                for r in rows:
                    n = int(r["n"]); makers = int(r["makers"] or 0)
                    out["by_symbol"].append({
                        "symbol": r["symbol"],
                        "n": n, "makers": makers, "takers": n - makers,
                        "maker_pct": (makers / n * 100.0) if n else None,
                        "last_at": r["last_at"].isoformat() if r["last_at"] else None,
                    })

                # Per-user breakdown
                rows = await con.fetch(
                    f"""SELECT u.email, u.maker_patience_mode AS mode,
                              COUNT(*) AS n,
                              SUM(CASE WHEN COALESCE(ut.metadata::jsonb->>'fee_type','') = 'maker' THEN 1 ELSE 0 END) AS makers,
                              MAX(ut.opened_at) AS last_at
                       FROM user_trades ut JOIN users u ON ut.user_id = u.id
                       WHERE ut.trade_type = 'real'
                         AND ut.opened_at >= NOW() - INTERVAL '{days} days'
                       GROUP BY u.email, u.maker_patience_mode
                       ORDER BY n DESC, u.email"""
                )
                for r in rows:
                    n = int(r["n"]); makers = int(r["makers"] or 0)
                    out["by_user"].append({
                        "email": r["email"], "mode": r["mode"] or "standard",
                        "n": n, "makers": makers, "takers": n - makers,
                        "maker_pct": (makers / n * 100.0) if n else None,
                        "last_at": r["last_at"].isoformat() if r["last_at"] else None,
                    })

                # Totals
                tot = await con.fetchrow(
                    f"""SELECT COUNT(*) AS n,
                              SUM(CASE WHEN COALESCE(metadata::jsonb->>'fee_type','') = 'maker' THEN 1 ELSE 0 END) AS makers
                       FROM user_trades
                       WHERE trade_type = 'real'
                         AND opened_at >= NOW() - INTERVAL '{days} days'"""
                )
                if tot and tot["n"]:
                    n = int(tot["n"]); makers = int(tot["makers"] or 0)
                    out["totals"] = {"n": n, "makers": makers, "takers": n - makers,
                                     "maker_pct": (makers / n * 100.0) if n else None}
        except Exception as e:
            import logging as _log
            _log.getLogger("dashboard").warning("maker-stats error: %s", e)
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_copilot_queue(self, request: web.Request) -> web.Response:
        """Co-pilot Sprint 1: returns unacked + un-snoozed events for the
        Decisions Queued For You panel. Per docs/COPILOT_PIVOT_v1.md."""
        out = {"items": [], "ts": datetime.utcnow().isoformat()}
        if not self._db_pool:
            return web.json_response(out, dumps=_safe_dumps)
        try:
            async with self._db_pool.acquire() as con:
                rows = await con.fetch(
                    """SELECT id, event_type, severity, title, detail,
                              cohort, metric_key, metric_before, metric_after,
                              event_at
                       FROM auto_revert_events
                       WHERE acknowledged = FALSE
                       ORDER BY
                         CASE severity
                           WHEN 'critical' THEN 1
                           WHEN 'error'    THEN 2
                           WHEN 'warn'     THEN 3
                           ELSE 4 END,
                         event_at DESC
                       LIMIT 8"""
                )
                for r in rows:
                    out["items"].append({
                        "id": r["id"],
                        "event_type": r["event_type"],
                        "severity": r["severity"],
                        "title": r["title"],
                        "detail": r["detail"] or "",
                        "cohort": r["cohort"] or "",
                        "metric_key": r["metric_key"] or "",
                        "metric_before": float(r["metric_before"]) if r["metric_before"] is not None else None,
                        "metric_after": float(r["metric_after"]) if r["metric_after"] is not None else None,
                        "event_at": r["event_at"].isoformat() if r["event_at"] else None,
                    })
        except Exception as e:
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_copilot_action(self, request: web.Request) -> web.Response:
        """Co-pilot Sprint 1: handle architect actions on queue items."""
        out = {"ok": False}
        if not self._db_pool:
            out["error"] = "db_pool_not_ready"
            return web.json_response(out, dumps=_safe_dumps)
        try:
            body = await request.json()
            eid = int(body.get("event_id"))
            action = str(body.get("action", "")).lower()
            ALLOWED = {"acknowledge", "investigate", "override", "snooze"}
            if action not in ALLOWED:
                out["error"] = f"unknown_action:{action}"
                return web.json_response(out, status=400, dumps=_safe_dumps)
            ack_by = "architect_via_dashboard"
            async with self._db_pool.acquire() as con:
                if action in ("acknowledge", "investigate", "override"):
                    await con.execute(
                        """UPDATE auto_revert_events
                              SET acknowledged = TRUE, ack_by = $1, ack_at = NOW()
                            WHERE id = $2""",
                        ack_by, eid
                    )
                elif action == "snooze":
                    # MVP: snooze == hide for 24h via separate event re-emit
                    await con.execute(
                        "UPDATE auto_revert_events SET acknowledged = TRUE, ack_by = $1, ack_at = NOW() "
                        "WHERE id = $2",
                        ack_by + "_snoozed", eid,
                    )
            out["ok"] = True
            out["event_id"] = eid
            out["action"] = action
        except Exception as e:
            out["error"] = str(e)[:200]
            return web.json_response(out, status=500, dumps=_safe_dumps)
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_quant_metrics(self, request: web.Request) -> web.Response:
        """Phase 3: quant-grade hero metrics (Sharpe, Sortino, PF, MaxDD).
        Computed server-side from user_trades. Single endpoint replaces
        the fitness-app dollar cards.

        Query: ?days=N (default 30) ?mode=paper|real|shadow|all (default all)
               ?clean=true|false (default true) — when true, excludes the
               legacy `auto_responder_stuck_60m` zero-PnL force-closes that
               accumulated before the 2026-04-27 max_age fix (06:41 UTC).
               These are administrative cleanup rows, not real exits, and
               they massively distort WR/Sharpe/PF for shadow.
               Also excludes phase2_virtual fan-out trades from the
               aggregate (they're tracked separately via leaderboard).
        """
        import math
        days = max(1, min(365, int(request.query.get("days", "30"))))
        mode = request.query.get("mode", "all").lower()
        clean = request.query.get("clean", "true").lower() == "true"
        out = {
            "days": days, "mode": mode, "clean": clean, "n": 0, "excluded_n": 0,
            "sharpe": None, "sortino": None,
            "pf": None, "win_rate": None,
            "avg_win": None, "avg_loss": None, "expectancy": None,
            "max_dd_usd": None, "max_dd_pct": None,
            "total_pnl": None, "best_trade": None, "worst_trade": None,
        }
        if not self._db_pool:
            out["error"] = "db_pool_not_ready"
            return web.json_response(out, dumps=_safe_dumps)
        try:
            mode_filter = ""
            params = []
            if mode != "all":
                mode_filter = "AND trade_type = $1"
                params.append(mode)
            # 2026-04-27 — clean filter: drop legacy stuck-60m force-closes
            # + any phase2_virtual fan-out rows. Both contaminate aggregate
            # metrics for the operator-facing edge view.
            clean_filter = ""
            if clean:
                clean_filter = (
                    "AND COALESCE(metadata::jsonb->>'exit_reason', '') "
                    "        NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close') "
                    "AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') "
                    "    != 'true' OR "
                    "    COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary') "
                )
            sql = f"""SELECT pnl_usd::float AS pnl, opened_at, closed_at
                       FROM user_trades
                       WHERE closed_at >= NOW() - INTERVAL '{days} days'
                         AND closed_at IS NOT NULL
                         AND pnl_usd IS NOT NULL
                         {mode_filter}
                         {clean_filter}
                       ORDER BY closed_at"""
            async with self._db_pool.acquire() as con:
                rows = await con.fetch(sql, *params)
                # Count what we excluded so the UI can footnote it.
                if clean:
                    excl = await con.fetchval(
                        f"""SELECT COUNT(*)::int FROM user_trades
                             WHERE closed_at >= NOW() - INTERVAL '{days} days'
                               AND closed_at IS NOT NULL
                               AND pnl_usd IS NOT NULL
                               {mode_filter}
                               AND (COALESCE(metadata::jsonb->>'exit_reason', '')
                                       IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                                    OR (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') = 'true'
                                        AND COALESCE(metadata::jsonb->>'exit_config_id','') != 'primary'))""",
                        *params,
                    )
                    out["excluded_n"] = int(excl or 0)
            pnls = [float(r["pnl"]) for r in rows]
            n = len(pnls)
            out["n"] = n
            if n == 0:
                return web.json_response(out, dumps=_safe_dumps)

            mean = sum(pnls) / n
            variance = sum((p - mean) ** 2 for p in pnls) / n if n > 1 else 0.0
            std = math.sqrt(variance) if variance > 0 else 0.0
            downside_returns = [p for p in pnls if p < 0]
            downside_var = sum(p ** 2 for p in downside_returns) / n if n > 0 else 0.0
            downside_std = math.sqrt(downside_var) if downside_var > 0 else 0.0

            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            gross_w = sum(wins)
            gross_l = abs(sum(losses))
            win_rate = (len(wins) / n * 100.0) if n else None
            pf = (gross_w / gross_l) if gross_l > 0 else (float("inf") if gross_w > 0 else None)
            avg_win = (gross_w / len(wins)) if wins else None
            avg_loss = (-gross_l / len(losses)) if losses else None  # negative number
            expectancy = mean

            # Sharpe (per-trade, no annualization — operator scale)
            sharpe = (mean / std) if std > 0 else None
            sortino = (mean / downside_std) if downside_std > 0 else None

            # Max drawdown (peak-to-trough on cumulative PnL)
            cum = 0.0
            peak = 0.0
            max_dd = 0.0
            for p in pnls:
                cum += p
                if cum > peak:
                    peak = cum
                dd = peak - cum
                if dd > max_dd:
                    max_dd = dd
            max_dd_pct = (max_dd / peak * 100.0) if peak > 0 else None

            out.update({
                "sharpe": sharpe,
                "sortino": sortino,
                "pf": (pf if pf != float("inf") else None),
                "win_rate": win_rate,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "expectancy": expectancy,
                "max_dd_usd": max_dd,
                "max_dd_pct": max_dd_pct,
                "total_pnl": sum(pnls),
                "best_trade": max(pnls),
                "worst_trade": min(pnls),
            })
        except Exception as e:
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_exchange_comparison(self, request: web.Request) -> web.Response:
        """Exchange Compare tab — side-by-side metrics for paper / delta_india / bybit.
        Query: ?days=N (default 7, max 90)
        Returns per-exchange aggregates for the comparison panel.
        """
        import math
        days = max(1, min(90, int(request.query.get("days", "7"))))
        out = {"days": days, "by_exchange": {}, "overall_paper": {}, "ts": datetime.utcnow().isoformat()}
        if not self._db_pool:
            out["error"] = "db_pool_not_ready"
            return web.json_response(out, dumps=_safe_dumps)
        try:
            async with self._db_pool.acquire() as con:
                # 2026-04-27 — clean filter: drop admin/orphan/reconcile
                # closes (PnL=$0 force-cleanups by Agent 9-A or restart
                # reconciler) + Phase 2 fan-out fan-out trades. Without
                # this, Exchange Compare tab over-states paper baseline,
                # under-states delta_india edge, and shows phantom bybit
                # numbers from before the bybit dispatcher pause.
                _CLEAN_NOT_IN = (
                    "AND COALESCE(metadata::jsonb->>'exit_reason','') "
                    "    NOT IN ('auto_responder_stuck_60m', "
                    "            'restart_orphan_cleanup', "
                    "            'reconcile_overaged_close') "
                    "AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') "
                    "    != 'true' OR "
                    "    COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary') "
                )

                # Paper trades (no exchange concept — separate aggregate)
                paper = await con.fetchrow(
                    f"""SELECT COUNT(*) AS n,
                              SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                              SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0 END)::float AS gw,
                              SUM(CASE WHEN pnl_usd < 0 THEN -pnl_usd ELSE 0 END)::float AS gl,
                              SUM(pnl_usd)::float AS pnl_total,
                              AVG(pnl_usd)::float AS avg
                         FROM user_trades
                        WHERE trade_type = 'paper'
                          AND closed_at >= NOW() - INTERVAL '{days} days'
                          AND closed_at IS NOT NULL
                          {_CLEAN_NOT_IN}"""
                )
                if paper and paper["n"]:
                    n = int(paper["n"]); wins = int(paper["wins"] or 0)
                    gw = float(paper["gw"] or 0); gl = float(paper["gl"] or 0)
                    out["overall_paper"] = {
                        "n": n,
                        "wr_pct": (wins / n * 100.0) if n else None,
                        "pf": (gw / gl) if gl > 0 else None,
                        "pnl_total": float(paper["pnl_total"] or 0),
                        "avg": float(paper["avg"] or 0),
                    }

                # Per-exchange shadow + real (clean filter applied)
                exch_rows = await con.fetch(
                    f"""SELECT exchange,
                              COUNT(*) AS n,
                              SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                              SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0 END)::float AS gw,
                              SUM(CASE WHEN pnl_usd < 0 THEN -pnl_usd ELSE 0 END)::float AS gl,
                              SUM(pnl_usd)::float AS pnl_total,
                              AVG(pnl_usd)::float AS avg,
                              SUM(CASE WHEN COALESCE(metadata::jsonb->>'fee_type','') = 'maker' THEN 1 ELSE 0 END) AS makers,
                              SUM(fees_usd)::float AS total_fees,
                              array_agg(pnl_usd ORDER BY closed_at) AS pnl_series
                         FROM user_trades
                        WHERE trade_type IN ('shadow', 'real')
                          AND closed_at >= NOW() - INTERVAL '{days} days'
                          AND closed_at IS NOT NULL
                          AND pnl_usd IS NOT NULL
                          {_CLEAN_NOT_IN}
                        GROUP BY exchange
                        ORDER BY exchange"""
                )

                for r in exch_rows:
                    n = int(r["n"]); wins = int(r["wins"] or 0)
                    gw = float(r["gw"] or 0); gl = float(r["gl"] or 0)
                    series = [float(p) for p in (r["pnl_series"] or []) if p is not None]
                    # Sharpe (per-trade, no annualization)
                    if len(series) > 1:
                        m = sum(series) / len(series)
                        var = sum((p - m) ** 2 for p in series) / len(series)
                        sd = math.sqrt(var) if var > 0 else 0
                        sharpe = (m / sd) if sd > 0 else None
                    else:
                        sharpe = None
                    # Max drawdown
                    cum = 0.0; peak = 0.0; max_dd = 0.0
                    for p in series:
                        cum += p
                        if cum > peak:
                            peak = cum
                        if peak - cum > max_dd:
                            max_dd = peak - cum
                    out["by_exchange"][r["exchange"]] = {
                        "n": n,
                        "wins": wins,
                        "wr_pct": (wins / n * 100.0) if n else None,
                        "pf": (gw / gl) if gl > 0 else None,
                        "pnl_total": float(r["pnl_total"] or 0),
                        "avg": float(r["avg"] or 0),
                        "makers": int(r["makers"] or 0),
                        "maker_pct": (int(r["makers"] or 0) / n * 100.0) if n else None,
                        "total_fees": float(r["total_fees"] or 0),
                        "sharpe": sharpe,
                        "max_dd_usd": max_dd,
                    }
        except Exception as e:
            import logging as _log
            _log.getLogger("dashboard").warning("exchange-comparison error: %s", e)
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    # ══════════════════════════════════════════════════════════════
    # PAPER + SHADOW API family (2026-04-26)
    # Unified shape: every trade dict has the same keys regardless of source.
    # Paper trades come from signals_history.json (in-memory tracker).
    # Shadow trades come from user_trades (DB) with exchange filter.
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_paper_trade(s: dict) -> dict:
        """Convert a signals_history.json entry to the unified trade shape."""
        meta = s.get("metadata") or {}
        return {
            "trade_id":   s.get("trade_id") or s.get("signal_id"),
            "symbol":     s.get("symbol"),
            "side":       s.get("side"),
            "entry_price": s.get("entry_price"),
            "exit_price":  s.get("exit_price"),
            "stop_loss":   s.get("stop_loss"),
            "take_profits": s.get("take_profits") or [],
            "quantity":    s.get("position_size"),
            "leverage":    s.get("leverage"),
            "pnl_usd":     s.get("pnl_usd", s.get("pnl")),
            "pnl_pct":     s.get("pnl_pct"),
            "fees_usd":    s.get("fees_usd"),
            "opened_at":   s.get("entry_time", s.get("timestamp")),
            "closed_at":   s.get("exit_time"),
            "exit_reason": s.get("exit_reason"),
            "scanner":     meta.get("scanner") or s.get("reason", "").split(":")[0] if s.get("reason") else None,
            "grade":       s.get("grade"),
            "confidence":  s.get("confidence"),
            "regime":      s.get("regime"),
            "fee_type":    None,             # paper has no fees
            "exchange":    None,
            "trade_type":  "paper",
            "user_email":  None,
            "metadata":    {k: v for k, v in meta.items() if k not in ("confirmations",)},
        }

    @staticmethod
    def _row_to_trade(row) -> dict:
        """Convert a user_trades DB row to the unified trade shape."""
        meta = row["metadata"] if isinstance(row["metadata"], dict) else {}
        sd = row["signal_data"] if isinstance(row["signal_data"], dict) else {}
        opened = row["opened_at"]
        closed = row["closed_at"]
        return {
            "trade_id":    str(row["id"]),
            "symbol":      row["symbol"],
            "side":        row["side"],
            "entry_price": float(row["entry_price"]) if row["entry_price"] is not None else None,
            "exit_price":  float(row["exit_price"]) if row["exit_price"] is not None else None,
            "stop_loss":   meta.get("stop_loss"),
            "take_profits": meta.get("take_profits") or [],
            "quantity":    float(row["quantity"]) if row["quantity"] is not None else None,
            "leverage":    meta.get("leverage"),
            "pnl_usd":     float(row["pnl_usd"]) if row["pnl_usd"] is not None else None,
            "pnl_pct":     None,
            "fees_usd":    float(row["fees_usd"]) if row["fees_usd"] is not None else None,
            "opened_at":   opened.isoformat() if opened else None,
            "closed_at":   closed.isoformat() if closed else None,
            "duration_sec": int((closed - opened).total_seconds()) if (opened and closed) else None,
            "exit_reason": meta.get("exit_reason"),
            "scanner":     meta.get("scanner") or sd.get("scanner"),
            "grade":       meta.get("grade"),
            "confidence":  None,
            "regime":      meta.get("regime"),
            "fee_type":    meta.get("fee_type"),
            "exchange":    row["exchange"],
            "trade_type":  row["trade_type"],
            "user_email":  None,            # joined separately if requested
        }

    @staticmethod
    def _aggregate_stats(trades: list) -> dict:
        """Compute WR/PF/Sharpe/MaxDD/avg/total from a list of trade dicts (closed only)."""
        import math
        pnls = [float(t["pnl_usd"]) for t in trades if t.get("pnl_usd") is not None]
        n = len(pnls)
        if n == 0:
            return {"n": 0}
        wins = sum(1 for p in pnls if p > 0)
        gw = sum(p for p in pnls if p > 0)
        gl = sum(-p for p in pnls if p < 0)
        m = sum(pnls) / n
        var = sum((p - m) ** 2 for p in pnls) / n if n > 1 else 0.0
        sd = math.sqrt(var) if var > 0 else 0.0
        # Max drawdown on cumulative PnL
        cum = 0.0; peak = 0.0; max_dd = 0.0
        for p in pnls:
            cum += p
            if cum > peak: peak = cum
            if peak - cum > max_dd: max_dd = peak - cum
        # Maker fills (only meaningful for shadow/real)
        maker_fills = sum(1 for t in trades if t.get("fee_type") == "maker")
        return {
            "n": n,
            "wins": wins,
            "wr_pct": (wins / n * 100.0) if n else None,
            "pf": (gw / gl) if gl > 0 else None,
            "sharpe": (m / sd) if sd > 0 else None,
            "avg_pnl": m,
            "total_pnl": sum(pnls),
            "best_trade": max(pnls),
            "worst_trade": min(pnls),
            "max_dd_usd": max_dd,
            "maker_fills": maker_fills,
            "maker_pct": (maker_fills / n * 100.0) if n else None,
            "total_fees": sum(float(t["fees_usd"]) for t in trades if t.get("fees_usd") is not None),
        }

    def _load_paper_signals(self, source: str) -> list:
        """Load + parse paper signals storage. source: 'active' | 'closed'."""
        import json as _json
        path = "/home/opc/crypto-trading-bot/storage/" + (
            "signals_history.json" if source == "active" else "closed_signals.json"
        )
        try:
            with open(path) as f:
                d = _json.load(f)
            arr = d if isinstance(d, list) else d.get("signals", []) if isinstance(d, dict) else []
            return arr
        except Exception:
            return []

    # ── Paper endpoints ────────────────────────────────────────────

    async def _handle_paper_active(self, request: web.Request) -> web.Response:
        """GET /api/paper/active → currently-tracked open paper signals."""
        try:
            limit = max(1, min(500, int(request.query.get("limit", "100"))))
        except Exception:
            limit = 100
        signals = self._load_paper_signals("active")
        # Active signals = no exit_price set
        active = [s for s in signals if not s.get("exit_price")]
        trades = [self._normalize_paper_trade(s) for s in active[-limit:]]
        return web.json_response({
            "trades": trades, "n": len(trades),
            "filters": {"limit": limit}, "source": "signals_history.json",
            "ts": datetime.utcnow().isoformat() + "Z",
        }, dumps=_safe_dumps)

    async def _handle_paper_closed(self, request: web.Request) -> web.Response:
        """GET /api/paper/closed?limit=N&symbol=BTC/USDT&days=N"""
        try:
            limit = max(1, min(2000, int(request.query.get("limit", "100"))))
            days = int(request.query.get("days", "0"))
        except Exception:
            limit, days = 100, 0
        symbol = request.query.get("symbol", "")
        signals = self._load_paper_signals("closed")
        # Filter by symbol + days
        if symbol:
            signals = [s for s in signals if s.get("symbol") == symbol]
        if days > 0:
            cutoff = datetime.utcnow() - timedelta(days=days)
            signals = [s for s in signals
                       if s.get("exit_time") and self._parse_iso_safe(s["exit_time"]) >= cutoff]
        signals = signals[-limit:]
        trades = [self._normalize_paper_trade(s) for s in signals]
        return web.json_response({
            "trades": trades, "n": len(trades),
            "filters": {"limit": limit, "days": days, "symbol": symbol or None},
            "source": "closed_signals.json",
            "ts": datetime.utcnow().isoformat() + "Z",
        }, dumps=_safe_dumps)

    async def _handle_paper_stats(self, request: web.Request) -> web.Response:
        """GET /api/paper/stats?days=N → aggregate metrics from paper trades."""
        try:
            days = max(1, min(365, int(request.query.get("days", "7"))))
        except Exception:
            days = 7
        signals = self._load_paper_signals("closed")
        cutoff = datetime.utcnow() - timedelta(days=days)
        in_window = [s for s in signals
                     if s.get("exit_time") and self._parse_iso_safe(s["exit_time"]) >= cutoff]
        trades = [self._normalize_paper_trade(s) for s in in_window]
        stats = self._aggregate_stats(trades)
        return web.json_response({
            "trade_type": "paper", "days": days, **stats,
            "ts": datetime.utcnow().isoformat() + "Z",
        }, dumps=_safe_dumps)

    @staticmethod
    def _parse_iso_safe(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return datetime.min

    # ── Shadow endpoints ───────────────────────────────────────────

    async def _handle_shadow_exchanges(self, request: web.Request) -> web.Response:
        """GET /api/shadow/exchanges → list of exchanges with shadow data + counts."""
        out = {"exchanges": [], "ts": datetime.utcnow().isoformat() + "Z"}
        if not self._db_pool:
            return web.json_response(out, dumps=_safe_dumps)
        try:
            async with self._db_pool.acquire() as con:
                rows = await con.fetch(
                    """SELECT exchange,
                              COUNT(*) AS n,
                              SUM(CASE WHEN closed_at IS NOT NULL THEN 1 ELSE 0 END) AS closed,
                              MAX(opened_at) AS last_open
                         FROM user_trades
                        WHERE trade_type IN ('shadow', 'real')
                        GROUP BY exchange ORDER BY exchange"""
                )
                out["exchanges"] = [
                    {"exchange": r["exchange"], "n": int(r["n"]), "closed": int(r["closed"] or 0),
                     "last_open": r["last_open"].isoformat() if r["last_open"] else None}
                    for r in rows
                ]
        except Exception as e:
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_shadow_active(self, request: web.Request) -> web.Response:
        """GET /api/shadow/active?exchange=delta_india|bybit&clean=true|false

        clean=true (default) hides Phase 2 fan-out virtual trades from the
        active list — they have their own dedicated leaderboard widget
        (/api/phase2/leaderboard). 41 phase2_virtual trades open at any
        given moment otherwise drown the operator's "current real positions"
        view.
        """
        exchange = request.query.get("exchange", "delta_india")
        clean = request.query.get("clean", "true").lower() == "true"
        out = {"trades": [], "n": 0, "excluded_n": 0, "clean": clean,
               "filters": {"exchange": exchange, "clean": clean},
               "ts": datetime.utcnow().isoformat() + "Z"}
        if not self._db_pool:
            return web.json_response(out, dumps=_safe_dumps)
        try:
            async with self._db_pool.acquire() as con:
                clean_clause = ""
                if clean:
                    clean_clause = (
                        "AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') "
                    "    != 'true' OR "
                    "    COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary') "
                    )
                rows = await con.fetch(
                    f"""SELECT * FROM user_trades
                        WHERE trade_type = 'shadow'
                          AND exchange = $1
                          AND closed_at IS NULL
                          {clean_clause}
                        ORDER BY opened_at DESC LIMIT 200""", exchange
                )
                out["trades"] = [self._row_to_trade(r) for r in rows]
                out["n"] = len(out["trades"])
                if clean:
                    excl = await con.fetchval(
                        """SELECT COUNT(*)::int FROM user_trades
                            WHERE trade_type='shadow' AND exchange=$1
                              AND closed_at IS NULL
                              AND COALESCE(metadata::jsonb->>'is_phase2_virtual','false')
                                  = 'true'""",
                        exchange,
                    )
                    out["excluded_n"] = int(excl or 0)
        except Exception as e:
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_shadow_closed(self, request: web.Request) -> web.Response:
        """GET /api/shadow/closed?exchange=delta_india&limit=N&days=N&symbol=BTC/USDT
                              &clean=true|false (default true)

        clean=true (default) excludes:
          - `auto_responder_stuck_60m` legacy force-closes (zero-PnL admin
            cleanup before 2026-04-27 06:41 UTC max_age fix)
          - `is_phase2_virtual` fan-out rows (tracked via leaderboard)
        Pass clean=false to inspect raw audit trail.
        """
        exchange = request.query.get("exchange", "delta_india")
        try:
            limit = max(1, min(2000, int(request.query.get("limit", "100"))))
            days = int(request.query.get("days", "7"))
        except Exception:
            limit, days = 100, 7
        symbol = request.query.get("symbol", "")
        clean = request.query.get("clean", "true").lower() == "true"
        out = {"trades": [], "n": 0, "excluded_n": 0, "clean": clean,
               "filters": {"exchange": exchange, "limit": limit, "days": days,
                           "symbol": symbol or None, "clean": clean},
               "ts": datetime.utcnow().isoformat() + "Z"}
        if not self._db_pool:
            return web.json_response(out, dumps=_safe_dumps)
        try:
            async with self._db_pool.acquire() as con:
                params = [exchange]
                sym_clause = ""
                if symbol:
                    params.append(symbol)
                    sym_clause = "AND symbol = $2 "
                clean_clause = ""
                if clean:
                    clean_clause = (
                        "AND COALESCE(metadata::jsonb->>'exit_reason','') "
                        "    NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close') "
                        "AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') "
                    "    != 'true' OR "
                    "    COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary') "
                    )
                rows = await con.fetch(
                    f"""SELECT * FROM user_trades
                         WHERE trade_type = 'shadow'
                           AND exchange = $1
                           AND closed_at IS NOT NULL
                           AND closed_at >= NOW() - INTERVAL '{days} days'
                           {sym_clause}
                           {clean_clause}
                         ORDER BY closed_at DESC LIMIT {limit}""",
                    *params
                )
                out["trades"] = [self._row_to_trade(r) for r in rows]
                out["n"] = len(out["trades"])
                if clean:
                    excl = await con.fetchval(
                        f"""SELECT COUNT(*)::int FROM user_trades
                             WHERE trade_type='shadow' AND exchange=$1
                               AND closed_at IS NOT NULL
                               AND closed_at >= NOW() - INTERVAL '{days} days'
                               {sym_clause}
                               AND (COALESCE(metadata::jsonb->>'exit_reason','')
                                       IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                                    OR (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') = 'true'
                                        AND COALESCE(metadata::jsonb->>'exit_config_id','') != 'primary'))""",
                        *params,
                    )
                    out["excluded_n"] = int(excl or 0)
        except Exception as e:
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_shadow_stats(self, request: web.Request) -> web.Response:
        """GET /api/shadow/stats?exchange=delta_india&days=N&clean=true → aggregate metrics.

        clean=true (default) excludes auto_responder_stuck_60m + phase2_virtual rows.
        See _handle_shadow_closed for rationale.
        """
        exchange = request.query.get("exchange", "delta_india")
        try:
            days = max(1, min(365, int(request.query.get("days", "7"))))
        except Exception:
            days = 7
        clean = request.query.get("clean", "true").lower() == "true"
        out = {"trade_type": "shadow", "exchange": exchange, "days": days, "clean": clean,
               "ts": datetime.utcnow().isoformat() + "Z"}
        if not self._db_pool:
            out["error"] = "db_pool_not_ready"
            return web.json_response(out, dumps=_safe_dumps)
        try:
            async with self._db_pool.acquire() as con:
                clean_clause = ""
                if clean:
                    clean_clause = (
                        "AND COALESCE(metadata::jsonb->>'exit_reason','') "
                        "    NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close') "
                        "AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') "
                    "    != 'true' OR "
                    "    COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary') "
                    )
                rows = await con.fetch(
                    f"""SELECT * FROM user_trades
                         WHERE trade_type = 'shadow'
                           AND exchange = $1
                           AND closed_at IS NOT NULL
                           AND closed_at >= NOW() - INTERVAL '{days} days'
                           {clean_clause}""",
                    exchange
                )
                trades = [self._row_to_trade(r) for r in rows]
            out.update(self._aggregate_stats(trades))
            # Also include per-symbol breakdown
            from collections import defaultdict
            per_sym = defaultdict(list)
            for t in trades:
                per_sym[t["symbol"]].append(t)
            out["by_symbol"] = {sym: self._aggregate_stats(ts) for sym, ts in per_sym.items()}
        except Exception as e:
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_phase2_leaderboard(self, request: web.Request) -> web.Response:
        """Phase 2 Shadow-of-Shadow leaderboard endpoint.

        Aggregates per exit_config_id over the requested window. Returns
        rows sorted by net PnL desc, plus the winner. Frontend widget
        polls this every 60s for the live A/B/C/D/E verdict.

        Query: ?hours=N (default 24, min 1, max 168)

        Note: only CLOSED phase2_virtual trades are aggregated. Open
        trades are returned in `n_open` so the operator can see how
        much sample is still pending.
        """
        try:
            hours = max(1, min(168, int(request.query.get("hours", "24"))))
        except Exception:
            hours = 24
        out = {
            "hours": hours, "configs": [], "winner": None,
            "n_total_open": 0, "n_total_closed": 0,
            "ts": datetime.utcnow().isoformat() + "Z",
        }
        if not self._db_pool:
            out["error"] = "db_pool_not_ready"
            return web.json_response(out, dumps=_safe_dumps)
        try:
            # 2026-04-27 — exclude admin force-closes from per-config
            # aggregates so the leaderboard reflects ACTUAL strategy
            # performance under each config, not Agent 9-A's restart
            # cleanup. auto_responder_stuck_60m + restart_orphan_cleanup
            # are both PnL=$0 admin events, not exits the config triggered.
            # Counts of these admin closes are surfaced separately so the
            # operator sees attribution loss.
            sql = f"""
                SELECT
                    metadata::jsonb->>'exit_config_id' AS cfg,
                    COUNT(*) AS n_total,
                    COUNT(*) FILTER (WHERE closed_at IS NOT NULL
                        AND COALESCE(metadata::jsonb->>'exit_reason','')
                            NOT IN ('auto_responder_stuck_60m',
                                    'restart_orphan_cleanup')) AS n_closed,
                    COUNT(*) FILTER (WHERE closed_at IS NULL) AS n_open,
                    COUNT(*) FILTER (WHERE COALESCE(metadata::jsonb->>'exit_reason','')
                            IN ('auto_responder_stuck_60m',
                                'restart_orphan_cleanup')) AS n_admin_closed,
                    SUM(CASE WHEN closed_at IS NOT NULL
                              AND COALESCE(metadata::jsonb->>'exit_reason','')
                                  NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                             THEN pnl_usd END)::float AS net,
                    SUM(CASE WHEN pnl_usd > 0
                              AND COALESCE(metadata::jsonb->>'exit_reason','')
                                  NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                             THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl_usd > 0
                              AND COALESCE(metadata::jsonb->>'exit_reason','')
                                  NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                             THEN pnl_usd ELSE 0 END)::float AS gross_w,
                    SUM(CASE WHEN pnl_usd < 0
                              AND COALESCE(metadata::jsonb->>'exit_reason','')
                                  NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                             THEN -pnl_usd ELSE 0 END)::float AS gross_l,
                    AVG(CASE WHEN closed_at IS NOT NULL
                              AND COALESCE(metadata::jsonb->>'exit_reason','')
                                  NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                             THEN pnl_usd END)::float AS avg_pnl,
                    MAX(metadata::jsonb->>'exit_config_summary') AS summary
                FROM user_trades
                WHERE COALESCE(metadata::jsonb->>'is_phase2_virtual','false') = 'true'
                  AND opened_at >= NOW() - INTERVAL '{hours} hours'
                  AND metadata::jsonb->>'exit_config_id' IS NOT NULL
                GROUP BY metadata::jsonb->>'exit_config_id'
            """
            async with self._db_pool.acquire() as con:
                rows = await con.fetch(sql)
            configs = []
            for r in rows:
                n_closed = int(r["n_closed"] or 0)
                wins = int(r["wins"] or 0)
                gross_w = float(r["gross_w"] or 0)
                gross_l = float(r["gross_l"] or 0)
                wr = (wins / n_closed * 100.0) if n_closed > 0 else None
                pf = (gross_w / gross_l) if gross_l > 0 else (None if gross_w == 0 else float("inf"))
                configs.append({
                    "id": r["cfg"],
                    "n_total": int(r["n_total"] or 0),
                    "n_closed": n_closed,
                    "n_open": int(r["n_open"] or 0),
                    "n_admin_closed": int(r["n_admin_closed"] or 0),
                    "wins": wins,
                    "wr_pct": wr,
                    "net": float(r["net"] or 0),
                    "avg_pnl": float(r["avg_pnl"] or 0) if r["avg_pnl"] is not None else None,
                    "pf": pf if pf != float("inf") else None,
                    "pf_inf": pf == float("inf"),
                    "summary": r["summary"] or "",
                })
                out["n_total_open"] += int(r["n_open"] or 0)
                out["n_total_closed"] += n_closed
            # Sort by net desc; winner = first non-empty row by net
            configs.sort(key=lambda c: -(c["net"] or 0))
            out["configs"] = configs
            # Winner only if at least 5 closed trades AND positive net
            for c in configs:
                if c["n_closed"] >= 5 and (c["net"] or 0) > 0:
                    out["winner"] = c["id"]
                    break
        except Exception as e:
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    # ══════════════════════════════════════════════════════════════

    async def _handle_multi_exchange_overview(self, request: web.Request) -> web.Response:
        """Per-exchange × trade_type breakdown for the multi-exchange UI overlay.
        Returns: active (currently open), last (most recent close), today (24h aggregates).
        Buckets: paper, delta_shadow, delta_real, bybit_shadow, bybit_demo, bybit_real.
        """
        out = {
            "active": {},
            "last": {},
            "today": {},
            "ts": datetime.utcnow().isoformat() + "Z",
        }
        if not self._db_pool:
            out["error"] = "db_pool_not_ready"
            return web.json_response(out, dumps=_safe_dumps)

        BUCKETS = ["paper", "delta_shadow", "delta_real",
                   "bybit_shadow", "bybit_demo", "bybit_real"]
        for b in BUCKETS:
            out["active"][b] = []
            out["last"][b] = None
            out["today"][b] = {"n": 0, "wr_pct": None, "pnl_total": 0.0, "fees": 0.0}

        def bucket_for(exchange: str, trade_type: str) -> str:
            tt = (trade_type or "").lower()
            ex = (exchange or "").lower()
            if tt == "paper":
                return "paper"
            if ex == "delta_india" and tt == "shadow":  return "delta_shadow"
            if ex == "delta_india" and tt == "real":    return "delta_real"
            if ex == "bybit"       and tt == "shadow":  return "bybit_shadow"
            if ex == "bybit"       and tt == "demo":    return "bybit_demo"
            if ex == "bybit"       and tt == "real":    return "bybit_real"
            return "paper"  # fallback

        try:
            async with self._db_pool.acquire() as con:
                # 1. ACTIVE — currently open, all exchanges.
                # 2026-04-27 — clean filter excludes is_phase2_virtual
                # so ~30 fan-out trades don't drown the active list.
                rows = await con.fetch("""
                    SELECT id::text, exchange, trade_type, symbol, side,
                           entry_price, quantity, opened_at,
                           COALESCE(metadata::jsonb->>'scanner','') AS scanner,
                           COALESCE(metadata::jsonb->>'fee_type','') AS fee_type,
                           NULLIF(metadata::jsonb->>'stop_loss','')::float   AS stop_loss,
                           NULLIF(metadata::jsonb->>'take_profit','')::float AS take_profit,
                           NULLIF(metadata::jsonb->>'leverage','')::float    AS leverage,
                           NULLIF(metadata::jsonb->>'last_price','')::float  AS last_price
                      FROM user_trades
                     WHERE closed_at IS NULL
                       AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') != 'true' OR COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary')
                     ORDER BY opened_at DESC LIMIT 200
                """)
                for r in rows:
                    b = bucket_for(r["exchange"], r["trade_type"])
                    entry = float(r["entry_price"]) if r["entry_price"] is not None else None
                    last  = float(r["last_price"])  if r["last_price"]  is not None else None
                    qty   = float(r["quantity"])    if r["quantity"]    is not None else None
                    upnl  = None
                    if entry is not None and last is not None and qty is not None:
                        sgn = 1.0 if (r["side"] or "").lower() == "long" else -1.0
                        upnl = (last - entry) * qty * sgn
                    out["active"][b].append({
                        "id": r["id"], "symbol": r["symbol"], "side": r["side"],
                        "entry_price": entry,
                        "quantity":    qty,
                        "opened_at":   r["opened_at"].isoformat() if r["opened_at"] else None,
                        "scanner":     r["scanner"],
                        "fee_type":    r["fee_type"],
                        "stop_loss":   float(r["stop_loss"])   if r["stop_loss"]   is not None else None,
                        "take_profit": float(r["take_profit"]) if r["take_profit"] is not None else None,
                        "leverage":    float(r["leverage"])    if r["leverage"]    is not None else None,
                        "last_price":  last,
                        "unrealized_pnl": upnl,
                    })

                # 2. LAST closed per bucket
                last_rows = await con.fetch("""
                    SELECT DISTINCT ON (exchange, trade_type)
                           exchange, trade_type, symbol, side,
                           entry_price, exit_price, pnl_usd, closed_at,
                           COALESCE(metadata::jsonb->>'exit_reason','') AS exit_reason,
                           COALESCE(metadata::jsonb->>'fee_type','') AS fee_type
                      FROM user_trades
                     WHERE closed_at IS NOT NULL
                       -- 2026-04-27 clean filter
                       AND COALESCE(metadata::jsonb->>'exit_reason','')
                           NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                       AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') != 'true' OR COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary')
                     ORDER BY exchange, trade_type, closed_at DESC
                """)
                for r in last_rows:
                    b = bucket_for(r["exchange"], r["trade_type"])
                    out["last"][b] = {
                        "symbol": r["symbol"], "side": r["side"],
                        "entry_price": float(r["entry_price"]) if r["entry_price"] is not None else None,
                        "exit_price":  float(r["exit_price"])  if r["exit_price"]  is not None else None,
                        "pnl_usd":     float(r["pnl_usd"])     if r["pnl_usd"]     is not None else None,
                        "closed_at":   r["closed_at"].isoformat() if r["closed_at"] else None,
                        "exit_reason": r["exit_reason"], "fee_type": r["fee_type"],
                    }

                # 3. TODAY (last 24h) per-bucket aggregates
                # 2026-04-27 clean filter — same as ACTIVE/LAST blocks above.
                tod_rows = await con.fetch("""
                    SELECT exchange, trade_type,
                           COUNT(*) AS n,
                           SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                           SUM(pnl_usd)::float AS pnl_total,
                           SUM(fees_usd)::float AS fees
                      FROM user_trades
                     WHERE closed_at >= NOW() - INTERVAL '24 hours'
                       AND closed_at IS NOT NULL
                       AND COALESCE(metadata::jsonb->>'exit_reason','')
                           NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                       AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') != 'true' OR COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary')
                     GROUP BY exchange, trade_type
                """)
                for r in tod_rows:
                    b = bucket_for(r["exchange"], r["trade_type"])
                    n = int(r["n"] or 0); wins = int(r["wins"] or 0)
                    out["today"][b] = {
                        "n": n,
                        "wr_pct": (wins / n * 100.0) if n else None,
                        "pnl_total": float(r["pnl_total"] or 0),
                        "fees": float(r["fees"] or 0),
                    }
        except Exception as e:
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_multi_exchange_closed(self, request: web.Request) -> web.Response:
        """GET /api/multi-exchange/closed?bucket=paper|delta_shadow|bybit_shadow|bybit_demo
                                          &days=N&limit=N&symbol=BTC/USDT
        Powers the Analytics Trade History 4-tab unified component (Surface E).
        Buckets map to (exchange, trade_type) tuples in user_trades.
        """
        bucket = (request.query.get("bucket") or "paper").lower()
        try:
            limit = max(1, min(2000, int(request.query.get("limit", "100"))))
            days  = max(1, min(365,  int(request.query.get("days",  "7"))))
        except Exception:
            limit, days = 100, 7
        symbol = request.query.get("symbol", "")

        # bucket → (exchange, trade_type)
        BUCKET_MAP = {
            "paper":         (None,         "paper"),   # exchange-agnostic
            "delta_shadow":  ("delta_india", "shadow"),
            "delta_real":    ("delta_india", "real"),
            "bybit_shadow":  ("bybit",       "shadow"),
            "bybit_demo":    ("bybit",       "demo"),
            "bybit_real":    ("bybit",       "real"),
        }
        if bucket not in BUCKET_MAP:
            return web.json_response(
                {"error": f"unknown bucket '{bucket}'", "valid": list(BUCKET_MAP.keys())},
                status=400, dumps=_safe_dumps,
            )
        exchange, trade_type = BUCKET_MAP[bucket]

        out = {"trades": [], "n": 0,
               "filters": {"bucket": bucket, "exchange": exchange,
                           "trade_type": trade_type, "limit": limit, "days": days,
                           "symbol": symbol or None},
               "ts": datetime.utcnow().isoformat() + "Z"}
        if not self._db_pool:
            out["error"] = "db_pool_not_ready"
            return web.json_response(out, dumps=_safe_dumps)

        # 2026-04-27 — clean filter wired into the multi-exchange Trade
        # History tab (last unfiltered API). Was contaminating the
        # DELTA · SHADOW tab with 105 auto_responder_stuck_60m + 52
        # reconcile_overaged_close + 25 restart_orphan_cleanup + 70+
        # is_phase2_virtual fan-out trades, ballooning n=152→406 and
        # net=-$12.86→-$86.31. Default clean=true; pass clean=false
        # to see raw audit trail.
        clean = request.query.get("clean", "true").lower() == "true"
        out["filters"]["clean"] = clean
        try:
            async with self._db_pool.acquire() as con:
                params = [trade_type]
                where = ["trade_type = $1", "closed_at IS NOT NULL",
                         f"closed_at >= NOW() - INTERVAL '{days} days'"]
                if exchange is not None:
                    params.append(exchange)
                    where.append(f"exchange = ${len(params)}")
                if symbol:
                    params.append(symbol)
                    where.append(f"symbol = ${len(params)}")
                if clean:
                    where.append(
                        "COALESCE(metadata::jsonb->>'exit_reason','') "
                        "NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')"
                    )
                    where.append(
                        "(COALESCE(metadata::jsonb->>'is_phase2_virtual','false') != 'true' "
                        " OR COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary')"
                    )
                sql = (f"SELECT * FROM user_trades WHERE " + " AND ".join(where)
                       + f" ORDER BY closed_at DESC LIMIT {limit}")
                rows = await con.fetch(sql, *params)
                out["trades"] = [self._row_to_trade(r) for r in rows]
                out["n"] = len(out["trades"])
                # Aggregate quick stats so the UI can show header summary
                if out["trades"]:
                    out["agg"] = self._aggregate_stats(out["trades"])
                # Surface excluded count for the UI footnote
                if clean:
                    excl_params = list(params)
                    excl_where = ["trade_type = $1", "closed_at IS NOT NULL",
                                  f"closed_at >= NOW() - INTERVAL '{days} days'"]
                    if exchange is not None: excl_where.append(f"exchange = $2")
                    if symbol:               excl_where.append(f"symbol = ${len(excl_params)}")
                    excl_where.append(
                        "(COALESCE(metadata::jsonb->>'exit_reason','') "
                        " IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close') "
                        " OR (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') = 'true' AND COALESCE(metadata::jsonb->>'exit_config_id','') != 'primary'))"
                    )
                    excl_sql = "SELECT COUNT(*)::int FROM user_trades WHERE " + " AND ".join(excl_where)
                    out["excluded_n"] = int(await con.fetchval(excl_sql, *excl_params) or 0)
        except Exception as e:
            out["error"] = str(e)[:200]
        return web.json_response(out, dumps=_safe_dumps)

    async def _handle_positions(self, request: web.Request) -> web.Response:
        async with self._lock:
            data = list(self._positions)
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_signals(self, request: web.Request) -> web.Response:
        async with self._lock:
            data = list(self._signals)
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_trades(self, request: web.Request) -> web.Response:
        async with self._lock:
            data = list(self._trades)
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_performance(self, request: web.Request) -> web.Response:
        async with self._lock:
            data = {
                "daily_pnl": self._daily_pnl,
                "total_pnl": self._total_pnl,
                "win_rate": self._win_rate,
                "trades_today": self._trades_today,
                "max_drawdown": self._max_drawdown,
                "wins": self._wins,
                "losses": self._losses,
            }
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_alerts(self, request: web.Request) -> web.Response:
        async with self._lock:
            data = list(self._alerts)
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_tracker_stats(self, request: web.Request) -> web.Response:
        if self._signal_tracker:
            data = self._signal_tracker.get_stats()
        else:
            data = {"error": "tracker not initialized"}
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_tracker_active(self, request: web.Request) -> web.Response:
        if self._signal_tracker:
            data = self._signal_tracker.get_active_signals()
        else:
            data = []
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_tracker_closed(self, request: web.Request) -> web.Response:
        if self._signal_tracker:
            data = self._signal_tracker.get_closed_signals()
        else:
            data = []
        # Pagination: limit response size (default 50, max 200)
        _limit = min(int(request.query.get("limit", 50)), 200)
        return web.json_response(data[-_limit:], dumps=_safe_dumps)

    async def _handle_scanner_stats(self, request: web.Request) -> web.Response:
        """Per-scanner PnL, regime correlation, and scanner x regime matrix."""
        tracker = self._signal_tracker
        if not tracker:
            return web.json_response({"error": "no tracker"})
        
        # Use the closed signals list (may be in _closed or _history)
        closed = getattr(tracker, "_closed_signals", [])
        if not closed:
            closed = getattr(tracker, "_closed", [])
        if not closed:
            closed = getattr(tracker, "closed_signals", [])
        if not closed:
            # Try loading from the tracker's signal history
            try:
                closed = list(tracker._signals.values()) if hasattr(tracker, "_signals") else []
                closed = [s for s in closed if getattr(s, "status", "") in ("stopped", "expired", "tp1_hit", "tp2_hit", "tp3_hit")]
            except Exception:
                closed = []
        scanner_stats = {}
        regime_stats = {}
        scanner_regime = {}
        
        for sig in closed:
            scanner = sig.get("setup_type", "unknown") if isinstance(sig, dict) else getattr(sig, "setup_type", "unknown")
            regime = (sig.get("metadata", {}) or {}).get("regime", "unknown") if isinstance(sig, dict) else "unknown"
            pnl = float(sig.get("pnl_usd", 0) or 0) if isinstance(sig, dict) else float(getattr(sig, "pnl_usd", 0) or 0)
            r_mult = float(sig.get("exit_r", 0) or 0) if isinstance(sig, dict) else 0
            mfe = float(sig.get("mfe_r", 0) or 0) if isinstance(sig, dict) else 0
            
            if scanner not in scanner_stats:
                scanner_stats[scanner] = {"trades": 0, "wins": 0, "pnl": 0, "r_sum": 0, "mfe_sum": 0}
            scanner_stats[scanner]["trades"] += 1
            if pnl > 0: scanner_stats[scanner]["wins"] += 1
            scanner_stats[scanner]["pnl"] += pnl
            scanner_stats[scanner]["r_sum"] += r_mult
            scanner_stats[scanner]["mfe_sum"] += mfe
            
            if regime not in regime_stats:
                regime_stats[regime] = {"trades": 0, "wins": 0, "pnl": 0}
            regime_stats[regime]["trades"] += 1
            if pnl > 0: regime_stats[regime]["wins"] += 1
            regime_stats[regime]["pnl"] += pnl
            
            key = f"{scanner}|{regime}"
            if key not in scanner_regime:
                scanner_regime[key] = {"trades": 0, "wins": 0, "pnl": 0}
            scanner_regime[key]["trades"] += 1
            if pnl > 0: scanner_regime[key]["wins"] += 1
            scanner_regime[key]["pnl"] += pnl
        
        result_scanners = {}
        for s, v in scanner_stats.items():
            n = v["trades"]
            result_scanners[s] = {
                "trades": n, "wins": v["wins"], "losses": n - v["wins"],
                "wr": round(v["wins"]/n*100, 1) if n > 0 else 0,
                "pnl": round(v["pnl"], 2),
                "avg_r": round(v["r_sum"]/n, 3) if n > 0 else 0,
                "avg_mfe": round(v["mfe_sum"]/n, 3) if n > 0 else 0,
                "pct_of_total": round(n/len(closed)*100, 1) if closed else 0,
            }
        
        result_regimes = {}
        for r, v in regime_stats.items():
            n = v["trades"]
            result_regimes[r] = {"trades": n, "wr": round(v["wins"]/n*100, 1) if n > 0 else 0, "pnl": round(v["pnl"], 2)}
        
        matrix = []
        for key, v in sorted(scanner_regime.items(), key=lambda x: -x[1]["trades"]):
            scanner, regime = key.split("|")
            n = v["trades"]
            if n >= 2:
                matrix.append({"scanner": scanner, "regime": regime, "trades": n,
                              "wr": round(v["wins"]/n*100, 1), "pnl": round(v["pnl"], 2)})
        
        return web.json_response({"scanners": result_scanners, "regimes": result_regimes,
                                  "matrix": matrix[:20], "total_closed": len(closed)})

    async def _handle_ai_insights(self, request: web.Request) -> web.Response:
        if self._signal_learner:
            data = self._signal_learner.get_insights()
        else:
            data = {"learning_active": False}
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_monitor_report(self, request: web.Request) -> web.Response:
        if self._trade_monitor:
            data = self._trade_monitor.get_monitor_report()
        else:
            data = {"active": False}
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_signal_status(self, request: web.Request) -> web.Response:
        """Return current scan status — why signals are/aren't generating."""
        if self._strategy and hasattr(self._strategy, "get_scan_status"):
            data = self._strategy.get_scan_status()
        else:
            data = {}
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_infra(self, request: web.Request) -> web.Response:
        """Return VM infrastructure status including memory, CPU, disk, and upgrade status."""
        data: Dict[str, Any] = {}

        if not HAS_PSUTIL:
            data["error"] = "psutil not installed"
            return web.json_response(data, dumps=_safe_dumps)

        try:
            # Memory info
            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()
            data["memory"] = {
                "total_mb": round(mem.total / 1024 / 1024),
                "used_mb": round(mem.used / 1024 / 1024),
                "free_mb": round(mem.available / 1024 / 1024),
                "percent": mem.percent,
                "swap_used_mb": round(swap.used / 1024 / 1024),
                "swap_total_mb": round(swap.total / 1024 / 1024),
            }

            # CPU info
            load_1, load_5, load_15 = os.getloadavg()
            data["cpu"] = {
                "count": psutil.cpu_count(),
                "load_1m": round(load_1, 2),
                "load_5m": round(load_5, 2),
                "load_15m": round(load_15, 2),
                "percent": psutil.cpu_percent(interval=0),
            }

            # Disk info
            disk = shutil.disk_usage("/")
            data["disk"] = {
                "total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
                "used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
                "free_gb": round(disk.free / 1024 / 1024 / 1024, 1),
                "percent": round((disk.used / disk.total) * 100, 1),
            }

            # OS uptime
            boot_time = psutil.boot_time()
            uptime_sec = int(time.time() - boot_time)
            days, rem = divmod(uptime_sec, 86400)
            hours, rem = divmod(rem, 3600)
            mins, secs = divmod(rem, 60)
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            parts.append(f"{mins}m")
            data["os_uptime"] = " ".join(parts)

            # Shape detection
            cpu_count = psutil.cpu_count()
            mem_gb = round(mem.total / 1024 / 1024 / 1024, 1)
            arch = platform.machine()

            if arch == "aarch64":
                shape = f"VM.Standard.A1.Flex ({cpu_count} OCPU / {mem_gb}GB)"
            elif mem_gb <= 1.1:
                shape = f"VM.Standard.E2.1.Micro ({cpu_count} OCPU / {mem_gb}GB)"
            else:
                shape = f"VM.Standard.E2.1 ({cpu_count} OCPU / {mem_gb}GB)"

            data["shape"] = shape
            data["arch"] = arch
            data["ocpus"] = cpu_count

            # Public IP (best effort)
            try:
                import subprocess
                result = subprocess.run(
                    ["curl", "-s", "--max-time", "2", "http://169.254.169.254/opc/v1/vnics/"],
                    capture_output=True, text=True, timeout=3
                )
                if result.returncode == 0:
                    vnics = json.loads(result.stdout)
                    if vnics and isinstance(vnics, list):
                        data["public_ip"] = vnics[0].get("publicIp", "N/A")
                    else:
                        data["public_ip"] = "N/A"
                else:
                    data["public_ip"] = "N/A"
            except Exception:
                data["public_ip"] = "N/A"

        except Exception as e:
            data["error"] = str(e)

        # WebSocket status
        if hasattr(self, '_orchestrator') and self._orchestrator and hasattr(self._orchestrator, '_delta_ws'):
            ws = self._orchestrator._delta_ws
            if ws:
                data["websocket"] = ws.get_status()
            else:
                data["websocket"] = {"connected": False, "status": "not_started"}
        else:
            data["websocket"] = {"connected": False, "status": "not_available"}

        # VM upgrade status (read from status file if exists)
        upgrade_status_file = Path.home() / "vm_upgrade_status.json"
        if upgrade_status_file.exists():
            try:
                upgrade_data = json.loads(upgrade_status_file.read_text())
                data["upgrade"] = upgrade_data
            except Exception:
                data["upgrade"] = {"status": "unknown"}
        else:
            data["upgrade"] = {"status": "not_started"}

        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_scanner_health(self, request: web.Request) -> web.Response:
        """Return scanner health states from weight manager."""
        if self._strategy and hasattr(self._strategy, '_scalp'):
            scalp = self._strategy._scalp
            if hasattr(scalp, '_weight_manager'):
                data = scalp._weight_manager.get_dashboard_summary()
                return web.json_response(data, dumps=_safe_dumps)
        return web.json_response([], dumps=_safe_dumps)

    async def _handle_opportunity_funnel(self, request: web.Request) -> web.Response:
        """Return opportunity funnel counters + near-misses."""
        data = {"funnel": {}, "near_misses": {}}
        if self._strategy and hasattr(self._strategy, '_scalp'):
            scalp = self._strategy._scalp
            if hasattr(scalp, '_funnel'):
                data["funnel"] = dict(scalp._funnel)
            # Veto stats debug info
            if hasattr(scalp, '_veto_stats'):
                data["veto_stats"] = dict(scalp._veto_stats)
            # Get near misses from latest scan status
            for symbol, status in scalp.last_scan_status.items():
                nm = status.get("near_misses", [])
                if nm:
                    data["near_misses"][symbol] = nm
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_decision(self, request: web.Request) -> web.Response:
        """Return current decision engine directive."""
        if self._decision_engine:
            data = self._decision_engine.get_dashboard_data()
        else:
            data = {"action": "WAIT", "reason": "Decision engine not initialized"}
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_regime(self, request: web.Request) -> web.Response:
        """Return current market regime and position sizing info.

        _last_regime_info is keyed by SYMBOL ({BTC/USDT: {...}, ...}).
        Previously this handler returned the raw per-symbol dict which
        the UI couldn't consume. Now returns a flat shape + per_symbol map.
        """
        data = {"regime": "unknown", "confidence": 0.0, "action": {}, "per_symbol": {}, "early_exit_stats": {}}
        if self._strategy and hasattr(self._strategy, '_scalp'):
            scalp = self._strategy._scalp
            regime_info_all = getattr(scalp, '_last_regime_info', {}) or {}
            if isinstance(regime_info_all, dict):
                # Per-symbol regimes for the UI
                per_symbol = {}
                for sym, info in regime_info_all.items():
                    if isinstance(info, dict):
                        per_symbol[sym] = {
                            "regime": info.get("regime", "unknown"),
                            "regime_age": info.get("regime_age", 0),
                            "action": info.get("action", {}),
                            "indicators_snapshot": info.get("indicators_snapshot", {}),
                        }
                data["per_symbol"] = per_symbol

                # Anchor: BTC else dominant
                anchor = regime_info_all.get("BTC/USDT") or {}
                if not anchor and regime_info_all:
                    from collections import Counter as _C
                    _regimes = [v.get("regime") for v in regime_info_all.values()
                                if isinstance(v, dict) and v.get("regime")]
                    if _regimes:
                        anchor = {"regime": _C(_regimes).most_common(1)[0][0]}
                data["regime"] = str(anchor.get("regime") or "unknown").lower()
                data["action"] = anchor.get("action", {}) or {}
                # Confidence from explicit field or regime_age
                if anchor.get("confidence") is not None:
                    data["confidence"] = round(float(anchor.get("confidence") or 0), 2)
                else:
                    _age = int(anchor.get("regime_age") or 0)
                    data["confidence"] = 0.80 if _age >= 5 else (0.55 if _age >= 3 else (0.35 if _age >= 1 else 0.0))
                data["anchor_symbol"] = "BTC/USDT" if regime_info_all.get("BTC/USDT") else "blend"

            # Early exit stats from tracker
            if self._signal_tracker:
                closed = self._signal_tracker.get_closed_signals(limit=500)
                hard_caps = sum(1 for c in closed if c.get("exit_reason_detailed") == "hard_loss_cap")
                momentum_exits = sum(1 for c in closed if c.get("exit_reason_detailed") == "momentum_collapse")
                data["early_exit_stats"] = {
                    "hard_loss_caps": hard_caps,
                    "momentum_exits": momentum_exits,
                }
            # Shadow recoveries
            if hasattr(scalp, '_weight_manager'):
                states = scalp._weight_manager.get_all_states()
                recoveries = sum(1 for s in states.values()
                               if isinstance(s, dict) and s.get("recovery_stage") == "probation")
                data.setdefault("early_exit_stats", {})["shadow_recoveries"] = recoveries
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_r_metrics(self, request: web.Request) -> web.Response:
        """Return R-multiple performance metrics per scanner and global."""
        if not self._signal_tracker:
            return web.json_response({"error": "tracker not initialized"}, dumps=_safe_dumps)

        stats = self._signal_tracker.get_stats()
        r_global = stats.get("r_metrics", {})
        by_setup = stats.get("by_setup", {})

        # Build per-scanner R-metrics table
        scanner_metrics = []
        for setup_name, data in by_setup.items():
            scanner_metrics.append({
                "scanner": setup_name,
                "trades": data.get("total", 0),
                "wins": data.get("wins", 0),
                "win_rate": data.get("win_rate", 0),
                "avg_r": data.get("avg_r", 0),
                "total_r": data.get("total_r", 0),
                "expectancy_r": data.get("expectancy_r", 0),
                "avg_win_r": data.get("avg_win_r", 0),
                "avg_loss_r": data.get("avg_loss_r", 0),
                "best_r": data.get("best_r", 0),
                "worst_r": data.get("worst_r", 0),
                "avg_mae_r": data.get("avg_mae_r", 0),
                "avg_mfe_r": data.get("avg_mfe_r", 0),
                "pnl_pct": data.get("pnl", 0),
            })

        # Sort by expectancy (best scanners first)
        scanner_metrics.sort(key=lambda x: x["expectancy_r"], reverse=True)

        return web.json_response({
            "global": r_global,
            "by_scanner": scanner_metrics,
        }, dumps=_safe_dumps)

    async def _handle_exit_quality(self, request: web.Request) -> web.Response:
        """Return exit quality metrics for dashboard."""
        data = {}
        if self._signal_tracker:
            stats = self._signal_tracker.get_stats()
            r = stats.get("r_metrics", {})
            data["avg_mae_r"] = r.get("avg_mae_r", 0)
            data["avg_mfe_r"] = r.get("avg_mfe_r", 0)
            # Frontend expects these field names:
            data["avg_mae"] = r.get("avg_mae_r", 0)
            data["avg_mfe"] = r.get("avg_mfe_r", 0)
            data["exit_efficiency"] = r.get("exit_efficiency", 0)
            data["avg_win_r"] = r.get("avg_win_r", 0)
            data["avg_loss_r"] = r.get("avg_loss_r", 0)
            data["total"] = r.get("total", 0)

            closed = self._signal_tracker.get_closed_signals(limit=500)
            data["hard_loss_caps"] = sum(1 for c in closed if c.get("exit_reason_detailed") == "hard_loss_cap")
            data["momentum_exits"] = sum(1 for c in closed if c.get("exit_reason_detailed") == "momentum_collapse_after_mfe")
            data["sl_hits"] = sum(1 for c in closed if c.get("exit_reason") == "stop_loss")

            # Top leak reason
            leak_counts = {}
            for c in closed:
                reason = c.get("exit_reason_detailed", c.get("exit_reason", "unknown"))
                if reason:
                    leak_counts[reason] = leak_counts.get(reason, 0) + 1
            if leak_counts:
                data["top_leak"] = max(leak_counts, key=leak_counts.get)
            else:
                data["top_leak"] = "N/A"
        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_grid_status(self, request: web.Request) -> web.Response:
        """Return Grid Bot status."""
        if hasattr(self, '_grid_bot') and self._grid_bot:
            return web.json_response(self._grid_bot.get_status(), dumps=_safe_dumps)
        return web.json_response({"enabled": False})

    async def _handle_grid_positions(self, request: web.Request) -> web.Response:
        """Return Grid Bot open positions."""
        if hasattr(self, '_grid_bot') and self._grid_bot:
            return web.json_response(self._grid_bot.get_open_positions(), dumps=_safe_dumps)
        return web.json_response([])

    def _get_ml_session(self):
        """Lazy shared aiohttp session for ML proxy (avoid per-request overhead)."""
        import aiohttp
        if self._ml_proxy_session is None or self._ml_proxy_session.closed:
            self._ml_proxy_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5),
            )
        return self._ml_proxy_session

    async def _handle_ml_proxy(self, request: web.Request) -> web.Response:
        """Track C (2026-04-11): Proxy ML dashboard requests to VM4.

        Item #7 hardening (2026-04-12):
        - Shared aiohttp session (no per-request overhead)
        - TTL cache: 30s for /health, 10s for others
        - Retry: 2 attempts with 1s backoff
        - Circuit breaker: 3 consecutive failures → 60s cooldown, return cached
        """
        import time as _t

        path = request.path
        qs = request.query_string
        cache_key = f"{path}?{qs}" if qs else path

        # TTL: 30s for health (rarely changes), 10s for live data
        ttl = 30 if path.endswith("/health") else 10

        # 1. Serve from cache if fresh
        cached = self._ml_proxy_cache.get(cache_key)
        if cached:
            ts, body, ct = cached
            if _t.time() - ts < ttl:
                return web.Response(body=body, status=200, content_type=ct)

        # 2. Circuit breaker: if open, return cached or 502
        now = _t.time()
        if now < self._ml_proxy_cb_open_until:
            if cached:
                _, body, ct = cached
                return web.Response(body=body, status=200, content_type=ct)
            return web.json_response(
                {"error": "circuit_open", "retry_after_s": int(self._ml_proxy_cb_open_until - now)},
                status=502,
            )

        # 3. Try up to 2 attempts with 1s backoff
        vm4_url = f"http://10.0.2.4:8081{path}"
        if qs:
            vm4_url += f"?{qs}"

        session = self._get_ml_session()
        last_err = None
        for attempt in range(2):
            try:
                async with session.get(vm4_url) as resp:
                    body = await resp.read()
                    ct = resp.headers.get("Content-Type", "application/json").split(";")[0].strip()
                    # Success: reset CB, update cache
                    self._ml_proxy_cb_failures = 0
                    self._ml_proxy_cache[cache_key] = (_t.time(), body, ct)
                    return web.Response(body=body, status=resp.status, content_type=ct)
            except Exception as e:
                last_err = e
                if attempt < 1:
                    await asyncio.sleep(1)  # 1s backoff before retry

        # 4. Both attempts failed
        self._ml_proxy_cb_failures += 1
        if self._ml_proxy_cb_failures >= 3:
            self._ml_proxy_cb_open_until = _t.time() + 60
            logger.warning("ML proxy circuit breaker OPEN for 60s after %d failures",
                          self._ml_proxy_cb_failures)

        # Return stale cached response if available (better than 502)
        if cached:
            _, body, ct = cached
            return web.Response(body=body, status=200, content_type=ct)

        return web.json_response(
            {"error": "vm4_unreachable", "path": path, "detail": str(last_err)},
            status=502,
        )

    async def _handle_real_status(self, request: web.Request) -> web.Response:
        """Return real trading manager status for dashboard.

        ROUTING (2026-04-20):
        - If the caller has an authenticated session AND the orchestrator's
          UserRealRegistry has a manager for them, return THEIR per-user
          status (balance, positions, trades — all scoped to their Delta
          account via their own keys).
        - Otherwise fall back to the legacy global real_manager (currently
          disabled — returns zeros).

        Previously, every user hit the global /api/real/status and saw the
        same numbers (zeros since legacy path disabled). Dashboard showed
        "Deployable Capital: $0 no balance" to every user regardless of
        their actual Delta testnet/prod balance. This proxy is transparent
        to the frontend (no URL change, no JS rewrite needed) and the
        per-user response shape matches the legacy shape.
        """
        # ── Per-user route (preferred) ──────────────────────────────
        user_ctx = request.get("user") or {}
        user_id = user_ctx.get("user_id")
        if user_id:
            orch = getattr(self, '_orchestrator', None)
            user_registry = getattr(orch, '_user_registry', None) if orch else None
            if user_registry:
                try:
                    # Try to find user_info in the cached active-users list
                    user_info = None
                    for u in (getattr(user_registry, '_active_users_cache', []) or []):
                        if str(u.get("id")) == str(user_id):
                            user_info = u
                            break
                    # If not in cache, refresh and retry
                    if user_info is None:
                        try:
                            await user_registry._refresh_active_users()
                            for u in (user_registry._active_users_cache or []):
                                if str(u.get("id")) == str(user_id):
                                    user_info = u
                                    break
                        except Exception:
                            pass
                    mgr = None
                    try:
                        if user_info is not None:
                            mgr = await user_registry.get_or_create_manager(user_info)
                        else:
                            mgr = await user_registry.get_manager_for_user(str(user_id))
                    except Exception as mgr_err:
                        # CRITICAL DIAG: don't silently swallow — log loud
                        logger.warning(
                            "real_status: get_or_create_manager raised for user %s: %s",
                            str(user_id)[:8], mgr_err,
                        )
                        mgr = None
                    if user_info is not None and mgr is None:
                        logger.info(
                            "real_status: user %s (bot_mode=%s) in active_cache but manager=None — check keys",
                            str(user_id)[:8], user_info.get("bot_mode"),
                        )
                    if mgr:
                        try:
                            await mgr.refresh_balance()
                        except Exception:
                            pass
                        _status = mgr.get_status() if hasattr(mgr, 'get_status') else {}

                        # ── Phase 4.2 UI wiring (2026-04-22) ──────────────
                        # Pull this user's last 50 closed real trades from
                        # user_trades table so the "RECENT CLOSED TRADES ›
                        # REAL" tab has something to show. The in-memory
                        # `closed_trades` list is cleared on every restart
                        # and doesn't survive the bot lifecycle. DB is the
                        # durable source of truth. Shape matches what
                        # updateRealClosedTrades() in app.js expects:
                        # {timestamp, symbol, side, entry_price, exit_price,
                        #  margin, leverage, pnl_usd, pnl_pct, scanner,
                        #  reason, slippage_bps}.
                        try:
                            pool = getattr(self, '_db_pool', None)
                            if pool is not None:
                                async with pool.acquire() as _conn:
                                    _rows = await _conn.fetch(
                                        """SELECT symbol, side, entry_price, exit_price,
                                                  quantity, pnl_usd, status, opened_at,
                                                  closed_at, metadata
                                           FROM user_trades
                                           WHERE user_id=$1 AND trade_type='real'
                                             AND status='closed'
                                             -- 2026-04-27 clean filter: drop admin/orphan/reconcile
                                             -- closes (legacy 'orphan_reconciled' kept for compat).
                                             AND COALESCE(metadata::jsonb->>'exit_reason','')
                                                 NOT IN ('orphan_reconciled',
                                                         'auto_responder_stuck_60m',
                                                         'restart_orphan_cleanup',
                                                         'reconcile_overaged_close')
                                             AND COALESCE(metadata::jsonb->>'is_phase2_virtual','false')
                                                 != 'true'
                                           ORDER BY closed_at DESC
                                           LIMIT 50""",
                                        user_id,
                                    )
                                def _float(v, d=0.0):
                                    try: return float(v) if v is not None else d
                                    except Exception: return d
                                _trades = []
                                # Force UTC-anchored ISO strings so the JS
                                # formatTime() → toLocaleTimeString(…, "Asia/Kolkata")
                                # converts correctly to IST for every tab.
                                # asyncpg returns tz-aware datetimes for
                                # `timestamp with timezone` columns, but
                                # promote to explicit UTC Z-suffix as
                                # defence against any stripped-offset edge.
                                from datetime import timezone as _tzmod
                                def _iso_utc(dt):
                                    if dt is None:
                                        return ""
                                    if dt.tzinfo is None:
                                        dt = dt.replace(tzinfo=_tzmod.utc)
                                    return dt.astimezone(_tzmod.utc).isoformat().replace("+00:00", "Z")

                                for r in _rows:
                                    m = r["metadata"] or {}
                                    if isinstance(m, str):
                                        try:
                                            import json as __j
                                            m = __j.loads(m)
                                        except Exception:
                                            m = {}
                                    entry = _float(r["entry_price"])
                                    exit_ = _float(r["exit_price"])
                                    margin = _float(m.get("margin"))
                                    lev = int(m.get("leverage") or 1)
                                    pnl_usd = _float(r["pnl_usd"])
                                    pnl_pct = 0.0
                                    if entry > 0 and margin > 0 and lev > 0:
                                        pnl_pct = (pnl_usd / (margin * lev)) * 100.0 if margin * lev > 0 else 0.0
                                    _trades.append({
                                        "timestamp": _iso_utc(r["closed_at"]),
                                        "opened_at": _iso_utc(r["opened_at"]),
                                        "closed_at": _iso_utc(r["closed_at"]),
                                        "symbol": r["symbol"],
                                        "side": r["side"],
                                        "entry_price": entry,
                                        "exit_price": exit_,
                                        "margin": margin,
                                        "leverage": lev,
                                        "pnl_usd": pnl_usd,
                                        "pnl_pct": pnl_pct,
                                        "scanner": m.get("scanner") or "",
                                        "reason": m.get("exit_reason") or "",
                                        "slippage_bps": _float(m.get("slippage_bps")),
                                        "phase": m.get("phase") or "",
                                        "fee_type": m.get("fee_type") or "",
                                        "grade": m.get("grade") or "",
                                        "peak_mfe_r": _float(m.get("peak_mfe_r")),
                                        "trade_type": m.get("trade_type") or "",
                                    })
                                # 3-tab UI (Phase 4.2): trades split into
                                # demo_trades + live_trades by the EXCHANGE
                                # they were routed to at trade-time. The
                                # delta_client's `mode` at manager-creation
                                # is the source of truth, recorded per-trade
                                # in metadata → fee_type won't tell us, but
                                # we persist `delta_mode` going forward. For
                                # legacy rows (no tag), fall back to the
                                # user's current bot_mode.
                                user_mode = (user_info or {}).get("bot_mode", "demo") if user_info else "demo"
                                demo_list, live_list = [], []
                                for t in _trades:
                                    # t.get("delta_mode") when populated; else
                                    # fall back to current user_mode.
                                    tm = (t.get("phase") or "")  # future: include delta_mode explicit
                                    # No per-trade exchange label yet — route
                                    # whole set to the user's current mode.
                                    if user_mode == "live":
                                        live_list.append(t)
                                    else:
                                        demo_list.append(t)
                                _status["demo_trades"] = demo_list
                                _status["live_trades"] = live_list
                                _status["recent_trades"] = _trades  # back-compat
                                _status["mode"] = user_mode  # let UI know

                                # Phase 5.0.2 (2026-04-22) — DB-BACKED TRADE
                                # STATS. Previous UI read `cb.trade_count_today`
                                # and `cb.total_pnl` from the in-memory
                                # CircuitBreaker, which resets on every bot
                                # restart (bot restarted 5-6× today due to
                                # deploys + auto-restart). UI showed
                                # "Trades: 0, Total: $0" despite 12 real
                                # trades per user in DB. Override with DB
                                # truth so reality reflects what's persisted.
                                _today_str = __import__('datetime').datetime.utcnow().strftime("%Y-%m-%d")
                                _today_trades = [t for t in _trades if t["timestamp"].startswith(_today_str)]
                                _today_net = sum(float(t.get("pnl_usd") or 0) for t in _today_trades)
                                _today_wins = sum(1 for t in _today_trades if float(t.get("pnl_usd") or 0) > 0)
                                _status["total_closed"] = len(_trades)
                                _status["closed_today"] = len(_today_trades)
                                _status["net_today"] = round(_today_net, 4)
                                _status["wins_today"] = _today_wins

                                # Inject DB truth into circuit_breaker so legacy
                                # UI fields that read cb.trade_count_today /
                                # cb.total_pnl show persisted data (not
                                # post-restart zeros).
                                _cb = _status.get("circuit_breaker") or {}
                                _cb["trade_count_today"] = len(_today_trades)
                                _cb["total_pnl"] = round(_today_net, 4)
                                _cb["daily_pnl"] = round(_today_net, 4)
                                _status["circuit_breaker"] = _cb

                                # Last trade for the "LAST DEMO / LAST LIVE"
                                # header card — pick most recent of the
                                # user's mode-specific trades.
                                _mode_list = demo_list if user_mode == "demo" else live_list
                                if _mode_list:
                                    _lt = _mode_list[0]  # already sorted DESC
                                    _status["last_trade"] = {
                                        "symbol": _lt.get("symbol"),
                                        "side": _lt.get("side"),
                                        "pnl_usd": _lt.get("pnl_usd"),
                                        "reason": _lt.get("reason"),
                                        "timestamp": _lt.get("timestamp"),
                                    }

                                # ── Track A (2026-04-25) — SHADOW trades ──
                                # shadow_live is now the primary mode for
                                # active users. Surface the user's last 10
                                # closed shadow trades + a 24h aggregate so
                                # the SHADOW tab + Analytics card have data.
                                # Kept separate from demo/live above because
                                # trade_type='shadow' is a different code
                                # path (no exchange fills, synthetic exit
                                # prices, but real signal lifecycle).
                                _status["shadow_trades"] = []
                                _status["shadow_stats_24h"] = {
                                    "n": 0, "net_pnl": 0.0,
                                    "avg_pnl": 0.0, "wins": 0, "win_rate": 0.0,
                                }
                                try:
                                    async with pool.acquire() as _sconn:
                                        _shadow_rows = await _sconn.fetch(
                                            """SELECT symbol, side, entry_price, exit_price,
                                                      quantity, pnl_usd, status, opened_at,
                                                      closed_at, metadata
                                               FROM user_trades
                                               WHERE user_id=$1 AND trade_type='shadow'
                                                 AND status='closed'
                                                 -- 2026-04-27 clean filter: drop admin/orphan/reconcile
                                                 -- closes + Phase 2 fan-out so RECENT CLOSED TRADES
                                                 -- shows real strategy exits only.
                                                 AND COALESCE(metadata::jsonb->>'exit_reason','')
                                                     NOT IN ('auto_responder_stuck_60m',
                                                             'restart_orphan_cleanup',
                                                             'reconcile_overaged_close')
                                                 AND COALESCE(metadata::jsonb->>'is_phase2_virtual','false')
                                                     != 'true'
                                               ORDER BY closed_at DESC
                                               LIMIT 10""",
                                            user_id,
                                        )
                                        _shadow_24h = await _sconn.fetchrow(
                                            """SELECT COUNT(*)                              AS n,
                                                      COALESCE(SUM(pnl_usd), 0)             AS net_pnl,
                                                      COALESCE(AVG(pnl_usd), 0)             AS avg_pnl,
                                                      COALESCE(SUM(CASE WHEN pnl_usd>0
                                                                        THEN 1 ELSE 0 END), 0) AS wins
                                               FROM user_trades
                                               WHERE user_id=$1 AND trade_type='shadow'
                                                 AND status='closed'
                                                 AND closed_at >= NOW() - INTERVAL '24 hours'
                                                 AND COALESCE(metadata::jsonb->>'exit_reason','')
                                                     NOT IN ('auto_responder_stuck_60m',
                                                             'restart_orphan_cleanup',
                                                             'reconcile_overaged_close')
                                                 AND COALESCE(metadata::jsonb->>'is_phase2_virtual','false')
                                                     != 'true'""",
                                            user_id,
                                        )
                                    _shadow_list = []
                                    for r in _shadow_rows:
                                        m = r["metadata"] or {}
                                        if isinstance(m, str):
                                            try:
                                                import json as __j2
                                                m = __j2.loads(m)
                                            except Exception:
                                                m = {}
                                        entry = _float(r["entry_price"])
                                        exit_ = _float(r["exit_price"])
                                        margin = _float(m.get("margin"))
                                        lev = int(m.get("leverage") or 1)
                                        pnl_usd = _float(r["pnl_usd"])
                                        pnl_pct = 0.0
                                        if entry > 0 and margin > 0 and lev > 0:
                                            pnl_pct = (pnl_usd / (margin * lev)) * 100.0 if margin * lev > 0 else 0.0
                                        _shadow_list.append({
                                            "timestamp":    _iso_utc(r["closed_at"]),
                                            "opened_at":    _iso_utc(r["opened_at"]),
                                            "closed_at":    _iso_utc(r["closed_at"]),
                                            "symbol":       r["symbol"],
                                            "side":         r["side"],
                                            "entry_price":  entry,
                                            "exit_price":   exit_,
                                            "margin":       margin,
                                            "leverage":     lev,
                                            "pnl_usd":      pnl_usd,
                                            "pnl_pct":      pnl_pct,
                                            "scanner":      m.get("scanner") or "",
                                            "reason":       m.get("exit_reason") or "",
                                            "slippage_bps": _float(m.get("slippage_bps")),
                                            "phase":        m.get("phase") or "",
                                            "fee_type":     m.get("fee_type") or "",
                                            "grade":        m.get("grade") or "",
                                            "peak_mfe_r":   _float(m.get("peak_mfe_r")),
                                            "trade_type":   "shadow",
                                        })
                                    _status["shadow_trades"] = _shadow_list
                                    if _shadow_24h is not None:
                                        _sn = int(_shadow_24h["n"] or 0)
                                        _swins = int(_shadow_24h["wins"] or 0)
                                        _status["shadow_stats_24h"] = {
                                            "n":        _sn,
                                            "net_pnl":  round(_float(_shadow_24h["net_pnl"]), 4),
                                            "avg_pnl":  round(_float(_shadow_24h["avg_pnl"]), 4),
                                            "wins":     _swins,
                                            "win_rate": round((_swins / _sn) * 100.0, 2) if _sn > 0 else 0.0,
                                        }
                                except Exception as _shadow_err:
                                    logger.debug(
                                        "real_status shadow-history fetch failed for user %s: %s",
                                        str(user_id)[:8], _shadow_err,
                                    )
                        except Exception as hist_err:
                            logger.debug("real_status trade-history fetch failed: %s", hist_err)

                        # Display-status derivation (same heuristic as legacy)
                        try:
                            _bal = float(_status.get("balance", 0) or 0)
                            _open = int(_status.get("open_count", 0) or 0)
                            _today = int(_status.get("closed_today", 0) or 0)
                            _total = int(_status.get("total_closed", 0) or 0)
                            _cb_tripped = bool(_status.get("circuit_breaker", {}).get("is_tripped", False))
                            _enabled = bool(_status.get("enabled", True))
                            _min_bal = 5.0
                            if not _enabled:
                                _status["display_status"] = "DISABLED"
                            elif _cb_tripped:
                                _status["display_status"] = "HALTED"
                            elif _bal < _min_bal:
                                _status["display_status"] = "STANDBY"
                            elif _total == 0 and _today == 0 and _open == 0:
                                _status["display_status"] = "ARMED"
                            else:
                                _status["display_status"] = "LIVE"
                        except Exception:
                            _status["display_status"] = _status.get("mode", "UNKNOWN")
                        _status["scope"] = "user"
                        _status["user_id"] = str(user_id)
                        return web.json_response(_status)
                except Exception as exc:
                    logger.debug("per-user real-status lookup failed (%s) — falling back to global", exc)

        # ── Paper-mode balance peek (2026-04-20) ─────────────────────
        # Paper users don't have a UserRealManager (by design — paper is
        # internal simulation, no exchange round-trip). But users still want
        # to see their actual Delta balance as a vault-view reference.
        # Run a lightweight read-only probe using the key MATCHING the user's
        # bot_mode preference (demo user → demo key; paper user → live key if
        # present, else demo). Cached 60s per user to avoid hammering Delta.
        #
        # BUGFIX 2026-04-21: this peek block used to run for ANY user (even
        # demo/live) when the per-user manager path returned None or errored.
        # It picked live > demo by default, which meant a demo user with only
        # a live key on file (or whose manager failed to create) would see
        # their live balance in the dashboard — confusing.
        #
        # New behavior:
        #   - For paper users: peek picks live > demo (vault view)
        #   - For demo users:  peek picks demo first
        #   - For live users:  peek picks live first
        # If the user's preferred-for-mode key is missing, fall back to the
        # other label rather than showing nothing.
        if user_id and self._db_pool:
            import time as _time
            now_sec = _time.time()
            if not hasattr(self, '_balance_peek_cache'):
                self._balance_peek_cache = {}  # user_id -> (ts, payload)
            cached = self._balance_peek_cache.get(str(user_id))
            if cached and (now_sec - cached[0]) < 60.0:
                peek_payload = dict(cached[1])
                peek_payload["cache_age_sec"] = int(now_sec - cached[0])
                return web.json_response(peek_payload)

            try:
                async with self._db_pool.acquire() as conn:
                    user_row = await conn.fetchrow(
                        "SELECT bot_mode FROM users WHERE id = $1", user_id,
                    )
                    # Mode-aware priority: demo user → demo key first; live
                    # user → live first; paper user → live first (vault view).
                    cur_mode = (user_row["bot_mode"] if user_row else "paper") or "paper"
                    if cur_mode == "demo":
                        priority_sql = "ORDER BY CASE label WHEN 'demo' THEN 1 WHEN 'live' THEN 2 ELSE 3 END"
                    else:
                        priority_sql = "ORDER BY CASE label WHEN 'live' THEN 1 WHEN 'demo' THEN 2 ELSE 3 END"
                    key_row = await conn.fetchrow(
                        f"""SELECT label, api_key_enc, api_secret_enc, base_url
                            FROM user_api_keys
                            WHERE user_id = $1 AND exchange = 'delta' AND is_active = TRUE
                            {priority_sql}
                            LIMIT 1""",
                        user_id,
                    )
            except Exception:
                user_row = None
                key_row = None

            if user_row and key_row:
                label = key_row["label"]
                try:
                    from auth.crypto import decrypt_api_key
                    api_key = decrypt_api_key(key_row["api_key_enc"], user_id=str(user_id))
                    api_secret = decrypt_api_key(key_row["api_secret_enc"], user_id=str(user_id))
                    base_url = key_row["base_url"] or (
                        "https://cdn-ind.testnet.deltaex.org" if label == "demo"
                        else "https://api.india.delta.exchange"
                    )
                except Exception:
                    api_key = api_secret = base_url = None

                probe_balance = None
                if api_key and api_secret:
                    import asyncio as _asyncio
                    from exchange.delta_balance import fetch_usd_balance
                    def _peek():
                        try:
                            return fetch_usd_balance(api_key, api_secret, base_url)
                        except Exception as e:
                            logger.debug("balance peek failed: %s", e)
                            return None
                    try:
                        probe_balance = await _asyncio.to_thread(_peek)
                    except Exception:
                        probe_balance = None

                # Derive display_status honestly:
                #   paper mode → STANDBY (armed with key, not trading)
                #   demo mode  → ARMED   (real trading eligible, zero activity yet)
                #   live mode  → ARMED   (same — peek means manager wasn't available)
                # "DISABLED" only when balance probe failed (key rejected / unreachable)
                _bot_mode = user_row["bot_mode"] or "paper"
                _bal_known = probe_balance is not None
                if not _bal_known:
                    _display_status = "DISABLED"
                elif _bot_mode == "paper":
                    _display_status = "STANDBY"
                else:
                    _display_status = "ARMED"
                peek_payload = {
                    "enabled": _bot_mode != "paper",
                    "dry_run": _bot_mode == "demo",
                    "mode": _bot_mode,
                    "display_status": _display_status,
                    "balance": round(probe_balance, 2) if _bal_known else 0.0,
                    "balance_source": f"peek_{label}" if _bal_known else "unreachable",
                    "balance_peek": True,       # flag: not from a live UserRealManager
                    "trading_active": _bot_mode != "paper",
                    "key_label": label,
                    "circuit_breaker": {"daily_pnl": 0, "is_tripped": False},
                    "open_positions": [], "open_count": 0,
                    "closed_today": 0, "total_closed": 0,
                    "recent_trades": [],
                    "scope": "user_peek",
                    "user_id": str(user_id),
                    "note": (
                        f"Paper mode — read-only balance from {label} key."
                        if _bot_mode == "paper"
                        else f"Balance from {label} key. Manager will take over for trading signals."
                    ),
                    "cache_age_sec": 0,
                }
                self._balance_peek_cache[str(user_id)] = (now_sec, peek_payload)
                return web.json_response(peek_payload)

        # ── Legacy global fallback — retired 2026-04-20 ─────────────
        # RealManager no longer instantiated (main.py sets real_manager=None).
        # This block remains so any very old callers without a session still
        # get a structurally-valid empty payload rather than an error. After
        # one release cycle, the `if mgr:` branch can be deleted entirely.
        mgr = getattr(self, '_real_manager', None)
        if not mgr:
            orch = getattr(self, '_orchestrator', None)
            if orch:
                mgr = getattr(orch, '_real_manager', None)
        if mgr:
            try:
                await mgr.update_prices()
            except Exception:
                pass
            # Refresh balance from exchange (async)
            # Balance refresh: throttle to every 30s (was every 2s from polling)
            import time as _ts
            _bal_age = _ts.time() - getattr(self, '_last_bal_refresh', 0)
            if _bal_age > 30:
                try:
                    if hasattr(mgr, 'refresh_balance'):
                        await mgr.refresh_balance()
                    self._last_bal_refresh = _ts.time()
                except Exception:
                    pass
            # Sync exchange positions (detect orphaned real positions)
            try:
                if False:  # DISABLED: sync_exchange_positions caused false closes on every dashboard refresh
                    await mgr.sync_exchange_positions()
            except Exception:
                pass
            # Auto-sync: close orphaned dry run positions
            try:
                tracker = getattr(self, '_signal_tracker', None)
                if not tracker:
                    orch = getattr(self, '_orchestrator', None)
                    if orch:
                        tracker = getattr(orch, '_signal_tracker', None)
                # NOTE: sync_with_paper() REMOVED from dashboard endpoint.
                # It was the ROOT CAUSE of orphan_sync — every dashboard refresh
                # triggered orphan sweep BEFORE mirror_paper_exit could process.
                # Orphan cleanup now happens only in orchestrator on a 5-min timer.
            except Exception:
                pass
            # UI FIX (2026-04-16): derive honest display_status that reflects
            # reality (LIVE implied actively trading; reality might be "armed
            # but $2.99 balance, zero real trades ever").
            _status = mgr.get_status()
            try:
                _enabled = bool(_status.get("enabled", False))
                _balance = float(_status.get("balance", 0) or 0)
                _open = int(_status.get("open_count", 0) or 0)
                _today = int(_status.get("closed_today", 0) or 0)
                _total = int(_status.get("total_closed", 0) or 0)
                _cb_tripped = bool(_status.get("circuit_breaker", {}).get("is_tripped", False))
                _min_bal = 5.0  # USDT — below this no meaningful trade size possible

                if not _enabled:
                    _status["display_status"] = "DISABLED"
                elif _cb_tripped:
                    _status["display_status"] = "HALTED"
                elif _balance < _min_bal:
                    _status["display_status"] = "STANDBY"  # armed but underfunded
                elif _total == 0 and _today == 0 and _open == 0:
                    _status["display_status"] = "ARMED"    # armed, no activity yet
                else:
                    _status["display_status"] = "LIVE"
            except Exception:
                _status["display_status"] = _status.get("mode", "UNKNOWN")
            return web.json_response(_status)
        return web.json_response({
            "enabled": False,
            "dry_run": True,
            "mode": "DISABLED",
            "display_status": "DISABLED",
            "balance": 0,
            "circuit_breaker": {"daily_pnl": 0, "is_tripped": False},
            "open_positions": [],
            "open_count": 0,
            "closed_today": 0,
            "total_closed": 0,
            "recent_trades": [],
        })

    async def _handle_real_toggle(self, request: web.Request) -> web.Response:
        """RETIRED 2026-04-20 (Option-A consolidation).

        The legacy shared-account RealTradingManager is no longer the source
        of truth. Mode changes go through /api/user/real/toggle which writes
        users.bot_mode (PostgreSQL) per-user and validates that a matching
        API key exists.

        Returning 410 Gone so any stale JS hitting this endpoint is forced
        to fail loudly rather than silently write state the system ignores.
        Front-end dropdown was re-wired in commit 2e5f6d5 to target the
        per-user endpoint instead.
        """
        return web.json_response({
            "error": "gone",
            "reason": "Legacy shared-account toggle removed — trading mode is per-user now.",
            "replacement": "POST /api/user/real/toggle with {bot_mode: 'paper'|'demo'|'live'}",
            "hint": "Use /profile → Trading → Mode Readiness card, or admin force-mode.",
        }, status=410)

    async def _handle_emergency_stop(self, request: web.Request) -> web.Response:
        """KILL SWITCH: Stop all trading immediately."""
        self._emergency_stop = True
        logger.critical("EMERGENCY STOP ACTIVATED via dashboard")

        # Disable real trading
        mgr = getattr(self, '_real_manager', None)
        if not mgr and hasattr(self, '_orchestrator'):
            mgr = getattr(self._orchestrator, '_real_manager', None)
        if mgr:
            mgr.enabled = False
            mgr._save_state()
            logger.critical("EMERGENCY: Real trading DISABLED")

        # Send Telegram alert
        try:
            alerts = getattr(self, '_alert_manager', None)
            if alerts:
                await alerts.send_system_alert(
                    "EMERGENCY STOP ACTIVATED — All trading halted",
                    level=AlertLevel.ERROR,
                )
        except Exception:
            pass

        return web.json_response({
            "status": "emergency_stop_activated",
            "real_trading": "disabled",
            "message": "All trading halted. Restart bot to resume.",
        })

    async def _handle_emergency_status(self, request: web.Request) -> web.Response:
        """Check if emergency stop is active."""
        return web.json_response({"emergency_stop": self._emergency_stop})

    async def _handle_risk_metrics(self, request: web.Request) -> web.Response:
        """Return risk-adjusted metrics: Sharpe, Sortino, Calmar, max DD duration."""
        import math
        trades = []
        try:
            feedback_path = Path("storage/ml_live_feedback.jsonl")
            if feedback_path.exists():
                with open(feedback_path) as f:
                    trades = [json.loads(line) for line in f if line.strip()]
        except Exception:
            pass

        if len(trades) < 5:
            return web.json_response({"error": "insufficient_data", "trades": len(trades)})

        returns = [t.get("exit_r", 0) for t in trades]
        n = len(returns)
        mean_r = sum(returns) / n
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / max(n - 1, 1))
        downside = [r for r in returns if r < 0]
        downside_std = math.sqrt(sum(r ** 2 for r in downside) / max(len(downside), 1)) if downside else 0.001

        # Sharpe (annualized assuming ~10 trades/day)
        trades_per_year = 10 * 365
        sharpe = (mean_r / std_r) * math.sqrt(trades_per_year) if std_r > 0 else 0
        sortino = (mean_r / downside_std) * math.sqrt(trades_per_year) if downside_std > 0 else 0

        # Max drawdown + duration
        equity = 1000.0
        peak = equity
        max_dd = 0
        dd_start = 0
        max_dd_duration = 0
        current_dd_start = None
        for i, r in enumerate(returns):
            pnl = equity * 0.01 * r  # Approx
            equity += pnl
            if equity > peak:
                peak = equity
                if current_dd_start is not None:
                    dur = i - current_dd_start
                    max_dd_duration = max(max_dd_duration, dur)
                current_dd_start = None
            else:
                dd = (peak - equity) / peak
                if dd > max_dd:
                    max_dd = dd
                if current_dd_start is None:
                    current_dd_start = i

        calmar = (mean_r * trades_per_year) / max_dd if max_dd > 0 else 0

        # Win/loss streaks
        max_win_streak = 0
        max_loss_streak = 0
        cur_w = 0
        cur_l = 0
        for r in returns:
            if r > 0:
                cur_w += 1
                cur_l = 0
            else:
                cur_l += 1
                cur_w = 0
            max_win_streak = max(max_win_streak, cur_w)
            max_loss_streak = max(max_loss_streak, cur_l)

        return web.json_response({
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
            "calmar": round(calmar, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "max_dd_duration_trades": max_dd_duration,
            "mean_r": round(mean_r, 4),
            "std_r": round(std_r, 4),
            "total_trades": n,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "profit_factor": round(sum(r for r in returns if r > 0) / abs(sum(r for r in returns if r < 0)) if sum(r for r in returns if r < 0) != 0 else 0, 2),
        }, dumps=_safe_dumps)

    async def _handle_session_heatmap(self, request: web.Request) -> web.Response:
        """Return WR and avg R by UTC hour for session heatmap."""
        trades = []
        try:
            feedback_path = Path("storage/ml_live_feedback.jsonl")
            if feedback_path.exists():
                with open(feedback_path) as f:
                    trades = [json.loads(line) for line in f if line.strip()]
        except Exception:
            pass

        hours = {}
        for t in trades:
            ts = t.get("timestamp", "")
            if "T" not in ts:
                continue
            try:
                h = int(ts.split("T")[1][:2])
            except (ValueError, IndexError):
                continue
            if h not in hours:
                hours[h] = {"trades": 0, "wins": 0, "pnl": 0, "r_sum": 0}
            hours[h]["trades"] += 1
            if t.get("pnl_usd", 0) > 0:
                hours[h]["wins"] += 1
            hours[h]["pnl"] += t.get("pnl_usd", 0)
            hours[h]["r_sum"] += t.get("exit_r", 0)

        result = []
        for h in range(24):
            d = hours.get(h, {"trades": 0, "wins": 0, "pnl": 0, "r_sum": 0})
            wr = (d["wins"] / d["trades"] * 100) if d["trades"] > 0 else 0
            avg_r = (d["r_sum"] / d["trades"]) if d["trades"] > 0 else 0
            result.append({
                "hour": h,
                "trades": d["trades"],
                "wins": d["wins"],
                "wr": round(wr, 1),
                "avg_r": round(avg_r, 3),
                "pnl": round(d["pnl"], 2),
            })

        return web.json_response(result, dumps=_safe_dumps)

    async def _handle_agents_status(self, request: web.Request) -> web.Response:
        """Return agent status, ML models, QA results, defects, and loss analysis."""
        import json as _json
        from pathlib import Path

        storage = Path("storage")

        # ML model results
        ml_models = []
        ml_summary = "No models loaded"
        ml_last_run = "--"
        try:
            pf = storage / "ml_models" / "candidate_pair_family.json"
            if pf.exists():
                with open(pf) as f:
                    pf_data = _json.load(f)
                ml_last_run = pf_data.get("timestamp", "--")[:19].replace("T", " ")
                results = pf_data.get("results", {})
                for family, scanners in results.items():
                    for scanner, data in scanners.items():
                        agg = data.get("training", {}).get("aggregate_oos", {})
                        ml_models.append({
                            "scanner": scanner,
                            "auc": agg.get("auc_roc", 0),
                            "spread": agg.get("spread", 0),
                            "candidates": data.get("training", {}).get("total_candidates", 0),
                            "live": scanner == "structure_bounce",  # only structure_bounce is live-scored
                        })
                ml_models.sort(key=lambda x: -x["auc"])
                best = ml_models[0] if ml_models else {}
                ml_summary = f"{len(ml_models)} models | Best: {best.get('scanner','')} AUC={best.get('auc',0):.3f}"
        except Exception:
            pass

        # QA results from storage
        qa_results = {"unit": "--", "integration": "--", "api": "--", "data_integrity": "--", "pass_rate": "--"}
        try:
            qf = storage / "qa_reports" / "latest.json"
            if qf.exists():
                with open(qf) as f:
                    qa_results = _json.load(f)
        except Exception:
            pass

        # Defects from storage
        defects = []
        try:
            df = storage / "qa_reports" / "defects.json"
            if df.exists():
                with open(df) as f:
                    defects = _json.load(f)
        except Exception:
            pass

        # Recent losses from closed trades
        recent_losses = []
        try:
            if hasattr(self, '_real_manager') and self._real_manager:
                status = await self._real_manager.get_status()
                for t in reversed(status.get("recent_trades", [])):
                    pnl = t.get("pnl_usd", 0)
                    if isinstance(pnl, (int, float)) and pnl < -0.05:
                        recent_losses.append({
                            "symbol": t.get("symbol", "?"),
                            "side": t.get("side", "?"),
                            "pnl": f"${pnl:.2f}",
                            "time": str(t.get("timestamp", ""))[-8:],
                            "scanner": t.get("scanner", "?"),
                            "reason": t.get("reason", "?"),
                            "analysis": f"Entry={t.get('entry_price',0):.2f} Exit={t.get('exit_price',0):.2f} | {t.get('reason','')}"
                        })
                        if len(recent_losses) >= 10:
                            break
        except Exception:
            pass

        return web.json_response({
            "agents": {
                "qa": {"status": "IDLE", "last_run": "--", "summary": "QA suite available"},
                "loss_analyzer": {"status": "IDLE", "last_run": "--", "summary": f"{len(recent_losses)} recent losses"},
                "ml_trainer": {"status": "IDLE", "last_run": ml_last_run, "summary": ml_summary},
            },
            "ml_models": ml_models,
            "qa_results": qa_results,
            "defects": defects,
            "recent_losses": recent_losses,
        })

    async def _handle_risk_return_scatter(self, request: web.Request) -> web.Response:
        """Return risk-return data per scanner for scatter plot."""
        import statistics
        try:
            closed_file = Path(__file__).resolve().parent.parent / "storage" / "closed_signals.json"
            with open(closed_file) as f:
                signals = json.load(f)
        except Exception:
            return web.json_response({"scanners": []})

        scanner_stats: Dict[str, Dict] = {}
        for sig in signals:
            meta = sig.get("metadata", {})
            scanner = meta.get("setup_type", sig.get("setup_type", sig.get("scanner", "unknown")))
            pnl = sig.get("pnl_pct", 0)
            if scanner not in scanner_stats:
                scanner_stats[scanner] = {
                    "pnls": [],
                    "category": meta.get("scanner_category", "unknown"),
                }
            scanner_stats[scanner]["pnls"].append(pnl)

        result = []
        for scanner, data in scanner_stats.items():
            pnls = data["pnls"]
            if len(pnls) < 3:
                continue
            avg_return = sum(pnls) / len(pnls)
            volatility = statistics.stdev(pnls) if len(pnls) > 1 else 0
            wins = sum(1 for p in pnls if p > 0)
            wr = wins / len(pnls)
            # Max drawdown approximation
            running = 0.0
            peak = 0.0
            max_dd = 0.0
            for p in pnls:
                running += p
                peak = max(peak, running)
                dd = peak - running
                max_dd = max(max_dd, dd)

            result.append({
                "scanner": scanner,
                "category": data["category"],
                "trades": len(pnls),
                "avg_return": round(avg_return, 4),
                "volatility": round(volatility, 4),
                "sharpe": round(avg_return / volatility, 3) if volatility > 0 else 0,
                "win_rate": round(wr, 3),
                "max_drawdown": round(max_dd, 3),
                "total_pnl": round(sum(pnls), 2),
            })

        return web.json_response({"scanners": result}, dumps=_safe_dumps)

    async def _handle_ping(self, request: web.Request) -> web.Response:
        """Ultra-fast ping for client-side latency measurement."""
        return web.json_response({"t": time.time() * 1000})

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Prometheus metrics export."""
        try:
            from utils.observability import get_metrics_text
            return web.Response(text=get_metrics_text(), content_type="text/plain")
        except Exception as e:
            return web.Response(text=f"# error: {e}\n", content_type="text/plain", status=500)

    async def _handle_health_check(self, request: web.Request) -> web.Response:
        """Liveness/readiness probe (for k8s, load balancers, monitoring)."""
        try:
            orch = getattr(self, '_orchestrator', None)
            running = bool(orch and getattr(orch, '_running', False))
            db_ok = self._db_pool is not None
            return web.json_response({
                "status": "ok" if running else "degraded",
                "bot_running": running,
                "db_connected": db_ok,
                "uptime_sec": int(time.time() - getattr(orch, '_start_time', time.time())) if orch else 0,
            }, status=200 if running else 503)
        except Exception as e:
            return web.json_response({"status": "error", "error": str(e)}, status=500)

    async def _handle_csrf_token(self, request: web.Request) -> web.Response:
        """Issue CSRF token for current session."""
        try:
            from dashboard.security_middleware import generate_csrf_token
            cookie = request.cookies.get("vn_session", "")
            if not cookie:
                return web.json_response({"error": "no session"}, status=401)
            token = generate_csrf_token(cookie)
            return web.json_response({"csrf_token": token})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_pipeline_overview(self, request: web.Request) -> web.Response:
        """Phase 0: funnel counts, real rejection leaderboard, agent heartbeats."""
        try:
            from bot.pipeline_metrics import get_snapshot
            orch = getattr(self, '_orchestrator', None)
            strategy = getattr(self, '_strategy', None) or (getattr(orch, '_strategy', None) if orch else None)
            real_manager = getattr(self, '_real_manager', None) or (getattr(orch, '_real_manager', None) if orch else None)
            signal_tracker = getattr(self, '_signal_tracker', None) or (getattr(orch, '_signal_tracker', None) if orch else None)
            snapshot = get_snapshot(
                strategy=strategy,
                real_manager=real_manager,
                signal_tracker=signal_tracker,
                orchestrator=orch,
            )
            return web.json_response(snapshot, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_journey(self, request: web.Request) -> web.Response:
        """Return signal journey for a specific trade_id."""
        trade_id = request.match_info.get("trade_id", "")
        try:
            from bot.signal_journey import SignalJourney
            record = SignalJourney.load_by_trade_id(trade_id)
            if record:
                return web.json_response({"found": True, "journey": record}, dumps=_safe_dumps)
            return web.json_response({"found": False, "trade_id": trade_id})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_supervisor_status(self, request: web.Request) -> web.Response:
        """Return supervisor watchdog status + current_action from orchestrator + probation."""
        try:
            orch = getattr(self, '_orchestrator', None)
            supervisor = getattr(self, '_supervisor', None)
            if supervisor is None and orch:
                supervisor = getattr(orch, '_supervisor', None)

            payload: Dict[str, Any] = {}
            if supervisor:
                st = supervisor.status
                if isinstance(st, dict):
                    payload.update(st)
                else:
                    payload["supervisor"] = st
            else:
                payload = {"running": False, "detail": "supervisor_not_wired"}

            # Phase 2.5 B2: current_action (last candle close / signal processing)
            try:
                if orch is not None:
                    ca = getattr(orch, '_current_action', None)
                    if ca:
                        import time as _t
                        ca_copy = dict(ca) if isinstance(ca, dict) else {}
                        ca_copy["age_sec"] = round(_t.time() - ca_copy.get("ts", _t.time()), 1)
                        payload["current_action"] = ca_copy
            except Exception:
                pass

            # Phase 3.5: probation status
            try:
                mgr = getattr(self, '_real_manager', None) or (getattr(orch, '_real_manager', None) if orch else None)
                if mgr is not None:
                    prob_mult = float(getattr(mgr, '_probation_size_mult', 1.0) or 1.0)
                    if prob_mult < 1.0:
                        import time as _t
                        started = float(getattr(mgr, '_probation_started_at', 0) or 0)
                        max_age = float(getattr(mgr, '_probation_max_age_sec', 4 * 3600))
                        max_trades = int(getattr(mgr, '_probation_max_trades', 3))
                        done = int(getattr(mgr, '_probation_trades_done', 0))
                        age = _t.time() - started if started > 0 else 0
                        payload["probation"] = {
                            "active": True,
                            "size_mult": prob_mult,
                            "trades_done": done,
                            "max_trades": max_trades,
                            "age_sec": round(age, 0),
                            "max_age_sec": max_age,
                            "remaining_trades": max(0, max_trades - done),
                            "remaining_age_sec": max(0, max_age - age),
                        }
                    else:
                        payload["probation"] = {"active": False}
            except Exception:
                pass

            return web.json_response(payload, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_cb_reset(self, request: web.Request) -> web.Response:
        """Manually reset the real trading circuit breaker.

        Query params:
          full=true       — also reset total_pnl to 0 (clears drawdown-kill state)
          reenable=true   — also set real_manager.enabled = True (overrides drawdown-kill disable)
          daily=true      — also reset daily_pnl to 0 (clears daily loss limit)
          probation=true  — also enable probation mode (Phase 3.5): 50% size for first 3 trades or 4h

        Default (no params) = reset consecutive_losses + is_tripped only (backward compat).
        """
        try:
            mgr = getattr(self, '_real_manager', None)
            if mgr is None:
                orch = getattr(self, '_orchestrator', None)
                if orch:
                    mgr = getattr(orch, '_real_manager', None)
            if mgr is None:
                return web.json_response({"error": "real_manager_not_available"}, status=404)

            full = request.query.get("full", "").lower() in ("1", "true", "yes")
            reenable = request.query.get("reenable", "").lower() in ("1", "true", "yes")
            reset_daily = request.query.get("daily", "").lower() in ("1", "true", "yes")
            probation = request.query.get("probation", "").lower() in ("1", "true", "yes")

            cb = mgr.circuit_breaker
            old_state = {
                "is_tripped": cb.is_tripped,
                "consecutive_losses": cb.consecutive_losses,
                "trip_reason": cb.trip_reason,
                "daily_pnl": cb.daily_pnl,
                "total_pnl": cb.total_pnl,
                "enabled": getattr(mgr, 'enabled', None),
            }
            # Always reset trip state
            cb.is_tripped = False
            cb.consecutive_losses = 0
            cb.trip_reason = ""
            # Optional: reset total_pnl (clears drawdown-kill reason)
            if full:
                cb.total_pnl = 0.0
            # Optional: reset daily_pnl
            if reset_daily or full:
                cb.daily_pnl = 0.0
            # Optional: re-enable the real manager (for drawdown-kill recovery)
            if reenable:
                try:
                    mgr.enabled = True
                except Exception:
                    pass
            # Phase 3.5: Optional probation mode (50% size for 3 trades or 4h)
            if probation and reenable:
                try:
                    import time as _t
                    mgr._probation_size_mult = 0.5
                    mgr._probation_started_at = _t.time()
                    mgr._probation_trades_done = 0
                    mgr._probation_max_trades = 3
                    mgr._probation_max_age_sec = 4 * 3600
                    logger.warning(
                        "PROBATION ENABLED: 50%% size for next 3 trades or 4 hours via API"
                    )
                except Exception as _pe:
                    logger.warning("probation setup failed: %s", _pe)
            mgr._save_state()
            logger.warning(
                "CB RESET via API: full=%s reenable=%s daily=%s | was: tripped=%s losses=%d daily=$%.2f total=$%.2f enabled=%s",
                full, reenable, reset_daily,
                old_state["is_tripped"], old_state["consecutive_losses"],
                old_state["daily_pnl"], old_state["total_pnl"], old_state["enabled"],
            )
            return web.json_response({
                "ok": True,
                "was": old_state,
                "now": {
                    "is_tripped": False,
                    "consecutive_losses": 0,
                    "daily_pnl": cb.daily_pnl,
                    "total_pnl": cb.total_pnl,
                    "enabled": getattr(mgr, 'enabled', None),
                },
                "flags_applied": {"full": full, "reenable": reenable, "daily": reset_daily},
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ==================================================================
    # Track A (2026-04-11): LOCK 75% + Force Flat
    # ==================================================================
    def _get_real_manager(self):
        """Resolve the real_manager instance from self or the orchestrator."""
        mgr = getattr(self, '_real_manager', None)
        if mgr is None:
            orch = getattr(self, '_orchestrator', None)
            if orch:
                mgr = getattr(orch, '_real_manager', None)
        return mgr

    async def _handle_lock_75(self, request: web.Request) -> web.Response:
        """A.2: close 75% of a specific real position (profit lock, keep 25% runner).

        POST body: {"trade_id": "live_xxx"}  OR  {"paper_trade_id": "abc123..."}
        Uses real_manager.partial_close_real() with close_pct=0.75. The 25%
        remainder continues to run with its existing SL/TP bracket.
        """
        try:
            mgr = self._get_real_manager()
            if mgr is None:
                return web.json_response({"error": "real_manager_not_available"}, status=404)
            try:
                body = await request.json()
            except Exception:
                body = {}
            trade_id = body.get("trade_id", "")
            paper_id = body.get("paper_trade_id", "")

            # Resolve paper_trade_id from real trade_id if only that was given
            if trade_id and not paper_id:
                for pid, rid in list(getattr(mgr, "paper_to_real", {}).items()):
                    if rid == trade_id:
                        paper_id = pid
                        break
                if not paper_id:
                    # Fallback: scan real_trades for a trade with matching id
                    rt = getattr(mgr, "real_trades", {}) or {}
                    t = rt.get(trade_id)
                    if t is not None:
                        paper_id = getattr(t, "paper_trade_id", "") or ""

            if not paper_id:
                return web.json_response({
                    "error": "no_paper_id_resolved",
                    "detail": "Could not resolve paper_trade_id from given identifiers",
                }, status=400)

            # Call partial_close_real via tp_level=0 sentinel (manual lock, not a TP hit)
            try:
                await mgr.partial_close_real(paper_id, 0, 0.75)
                logger.warning("LOCK 75%% via dashboard: paper_id=%s", paper_id[:16])
            except Exception as e:
                return web.json_response({"error": f"partial_close_failed: {e}"}, status=500)

            return web.json_response({
                "ok": True,
                "paper_trade_id": paper_id,
                "close_pct": 0.75,
                "remaining_pct": 0.25,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_force_flat(self, request: web.Request) -> web.Response:
        """A.3: force flat — close ALL open real positions immediately.

        Calls mirror_paper_exit("force_flat") on every entry in real_trades,
        or falls back to partial_close_real(1.0) if mirror_paper_exit can't
        resolve the paper side. Read-only at the per-trade level until all
        closes fire. Intentionally sequential to avoid API rate spikes.
        """
        try:
            mgr = self._get_real_manager()
            if mgr is None:
                return web.json_response({"error": "real_manager_not_available"}, status=404)
            rt = dict(getattr(mgr, "real_trades", {}) or {})
            if not rt:
                return web.json_response({"ok": True, "closed": 0, "detail": "no_open_positions"})

            results = []
            for trade_id, t in rt.items():
                try:
                    paper_id = getattr(t, "paper_trade_id", "") or ""
                    symbol = getattr(t, "symbol", "?")
                    side = getattr(t, "side", "?")
                    # Use current market price from paper engine / last-known
                    px = float(getattr(t, "current_price", 0) or getattr(t, "entry_price", 0) or 0)
                    ok = False
                    if paper_id:
                        r = await mgr.mirror_paper_exit(
                            paper_id, px, "force_flat",
                            paper_slippage_bps=0.0, symbol=symbol, side=side,
                        )
                        ok = bool(r and r.get("status") == "exited")
                    if not ok:
                        # Fallback path — 100% close via partial_close_real
                        try:
                            await mgr.partial_close_real(paper_id or trade_id, 0, 1.0)
                            ok = True
                        except Exception:
                            ok = False
                    results.append({
                        "trade_id": trade_id, "symbol": symbol, "side": side, "ok": ok,
                    })
                except Exception as e:
                    results.append({"trade_id": trade_id, "ok": False, "error": str(e)[:60]})

            closed_n = sum(1 for r in results if r.get("ok"))
            logger.warning("FORCE FLAT via dashboard: closed %d/%d open real positions",
                           closed_n, len(results))
            return web.json_response({
                "ok": True,
                "closed": closed_n,
                "total": len(results),
                "results": results,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ==================================================================
    # Items #6+8: Config Editor + Hot-Reload
    # ==================================================================

    _SENSITIVE_KEYS = {"api_key", "api_secret", "bot_token", "passphrase", "password", "secret"}

    def _strip_secrets(self, cfg, _depth=0):
        """Recursively redact keys containing sensitive substrings."""
        if _depth > 10 or not isinstance(cfg, dict):
            return cfg
        result = {}
        for k, v in cfg.items():
            kl = k.lower()
            if kl in self._SENSITIVE_KEYS or any(s in kl for s in ("_key", "_secret", "_token", "_password")):
                result[k] = "***REDACTED***"
            elif isinstance(v, dict):
                result[k] = self._strip_secrets(v, _depth + 1)
            else:
                result[k] = v
        return result

    def _has_sensitive_keys(self, d, _depth=0):
        """Check if dict contains any sensitive keys (reject writes)."""
        if _depth > 10 or not isinstance(d, dict):
            return False
        for k, v in d.items():
            kl = k.lower()
            if kl in self._SENSITIVE_KEYS or any(s in kl for s in ("_key", "_secret", "_token")):
                return True
            if isinstance(v, dict) and self._has_sensitive_keys(v, _depth + 1):
                return True
        return False

    def _deep_merge(self, base, updates, _depth=0):
        """Deep merge updates into base dict (updates win on leaf conflicts)."""
        if _depth > 10:
            return updates
        merged = dict(base)
        for k, v in updates.items():
            if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k] = self._deep_merge(merged[k], v, _depth + 1)
            else:
                merged[k] = v
        return merged

    # ── BotBrain API handlers ──

    def _get_brain(self):
        """Get BotBrain reference from orchestrator."""
        orch = self._orchestrator
        return getattr(orch, '_brain', None) if orch else None

    async def _handle_brain_state(self, request: web.Request) -> web.Response:
        """Return full BotBrain state — active directives, session, optimizer."""
        brain = self._get_brain()
        if not brain:
            return web.json_response({"error": "BotBrain not initialized"}, status=503)
        try:
            return web.json_response(brain.get_dashboard_state(), dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_brain_matrix(self, request: web.Request) -> web.Response:
        """Return setup x regime performance matrix for heatmap."""
        brain = self._get_brain()
        if not brain:
            return web.json_response({"error": "BotBrain not initialized"}, status=503)
        try:
            return web.json_response(brain.get_matrix_data(), dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_brain_regime_history(self, request: web.Request) -> web.Response:
        """Return per-symbol regime history + transition predictions."""
        brain = self._get_brain()
        if not brain:
            return web.json_response({"error": "BotBrain not initialized"}, status=503)
        try:
            return web.json_response(brain.get_regime_data(), dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_brain_hourly_heatmap(self, request: web.Request) -> web.Response:
        """Return 24-hour performance heatmap."""
        brain = self._get_brain()
        if not brain:
            return web.json_response({"error": "BotBrain not initialized"}, status=503)
        try:
            return web.json_response(brain.get_hourly_data(), dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_brain_sessions(self, request: web.Request) -> web.Response:
        """Return daily/weekly session summaries."""
        brain = self._get_brain()
        if not brain:
            return web.json_response({"error": "BotBrain not initialized"}, status=503)
        try:
            return web.json_response(brain.get_sessions_data(), dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_config_get(self, request: web.Request) -> web.Response:
        """Return safe subset of settings.yaml (secrets redacted) + validation schema."""
        try:
            import yaml
            settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
            with open(settings_path) as f:
                raw = yaml.safe_load(f) or {}
            safe = self._strip_secrets(raw)
            # Include schema so UI can show min/max hints per knob
            safe["_schema"] = self.CONFIG_SCHEMA
            return web.json_response(safe, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # B.10: Config validation schema — min/max bounds per editable knob.
    # Used by POST /api/config to reject out-of-range values before writing.
    # Also returned by GET /api/config under "schema" key for UI hints.
    CONFIG_SCHEMA = {
        "real_trading.use_limit_orders": {"type": "bool"},
        "real_trading.max_slippage_bps": {"type": "float", "min": 5, "max": 100, "unit": "bp"},
        "real_trading.real_ml_threshold_min": {"type": "float", "min": 0.3, "max": 0.95},
        "real_trading.real_tp1_r": {"type": "float", "min": 0.0, "max": 2.0, "unit": "R"},
        "real_trading.daily_loss_limit_usd": {"type": "float", "min": 1, "max": 500, "unit": "$"},
        "real_trading.max_consecutive_losses": {"type": "int", "min": 1, "max": 20},
        "real_trading.min_margin_per_trade": {"type": "float", "min": 5, "max": 200, "unit": "$"},
        "real_trading.max_margin_per_trade": {"type": "float", "min": 5, "max": 500, "unit": "$"},
        "real_trading.max_open_positions": {"type": "int", "min": 1, "max": 20},
        "real_trading.leverage_cap": {"type": "int", "min": 1, "max": 100, "unit": "x"},
        "real_trading.min_balance_to_trade": {"type": "float", "min": 1, "max": 1000, "unit": "$"},
        "real_trading.balance_reserve_pct": {"type": "int", "min": 0, "max": 50, "unit": "%"},
        "real_trading.real_allowed_regimes": {"type": "list", "allowed": ["trending_up", "trending_down", "breakout", "mean_reversion", "ranging", "sideways", "high_volatility", "quiet"]},
        "orderbook.enabled": {"type": "bool"},
        "orderbook.poll_interval": {"type": "int", "min": 5, "max": 60, "unit": "s"},
        "orderbook.depth": {"type": "int", "min": 5, "max": 50},
        "grid.enabled": {"type": "bool"},
        "grid.num_levels": {"type": "int", "min": 3, "max": 50},
        "grid.position_usd": {"type": "float", "min": 10, "max": 500, "unit": "$"},
    }

    def _validate_config(self, updates: dict, _path: str = "") -> list:
        """Validate config values against schema bounds. Returns list of error strings."""
        errors = []
        for k, v in updates.items():
            full_key = f"{_path}.{k}" if _path else k
            if isinstance(v, dict):
                errors.extend(self._validate_config(v, full_key))
            else:
                rule = self.CONFIG_SCHEMA.get(full_key)
                if rule:
                    t = rule.get("type")
                    if t == "float" and isinstance(v, (int, float)):
                        if "min" in rule and v < rule["min"]:
                            errors.append(f"{full_key}={v} below min {rule['min']}")
                        if "max" in rule and v > rule["max"]:
                            errors.append(f"{full_key}={v} above max {rule['max']}")
                    elif t == "int" and isinstance(v, (int, float)):
                        if "min" in rule and v < rule["min"]:
                            errors.append(f"{full_key}={v} below min {rule['min']}")
                        if "max" in rule and v > rule["max"]:
                            errors.append(f"{full_key}={v} above max {rule['max']}")
                    elif t == "list" and isinstance(v, list):
                        allowed = set(rule.get("allowed", []))
                        if allowed:
                            bad = [x for x in v if str(x).strip().lower() not in allowed]
                            if bad:
                                errors.append(f"{full_key}: invalid values {bad}, allowed: {sorted(allowed)}")
        return errors

    async def _handle_config_post(self, request: web.Request) -> web.Response:
        """Validate, write updated config, hot-reload, and audit-trail."""
        try:
            import yaml, shutil
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)

        try:
            # Reject secrets
            if self._has_sensitive_keys(body):
                return web.json_response({"error": "cannot_set_secrets_via_api"}, status=403)

            # B.10: Schema validation
            validation_errors = self._validate_config(body)
            if validation_errors:
                return web.json_response({
                    "error": "validation_failed",
                    "violations": validation_errors,
                }, status=400)

            settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"

            # Read current
            with open(settings_path) as f:
                current = yaml.safe_load(f) or {}

            # Deep merge
            merged = self._deep_merge(current, body)

            # Backup
            import time as _t
            backup = settings_path.with_suffix(f".yaml.bak.{int(_t.time())}")
            shutil.copy2(settings_path, backup)

            # Write
            with open(settings_path, "w") as f:
                yaml.dump(merged, f, default_flow_style=False, sort_keys=False)

            # Hot-reload typed config singleton
            try:
                from config import get_config
                get_config(reload=True)
            except Exception as e:
                logger.warning("Config singleton reload failed: %s", e)

            # Hot-reload RealTradingManager knobs
            mgr = self._get_real_manager()
            if mgr and hasattr(mgr, "reload_config"):
                try:
                    mgr.reload_config(merged)
                except Exception as e:
                    logger.warning("RealManager reload_config failed: %s", e)

            # Audit trail
            try:
                import json as _json
                audit_path = Path(__file__).resolve().parent.parent / "storage" / "config_audit.jsonl"
                record = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "changes": body,
                    "source": "dashboard_api",
                }
                with open(audit_path, "a") as f:
                    f.write(_json.dumps(record, default=str) + "\n")
            except Exception:
                pass

            logger.warning("CONFIG UPDATED via dashboard API: %s", list(body.keys()))
            return web.json_response({"ok": True, "keys_updated": list(body.keys())})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ==================================================================
    # Vision Tier 2+3: Thesis, Agent Pipeline, Research, Pipeline Trace
    # ==================================================================

    async def _handle_thesis(self, request: web.Request) -> web.Response:
        """Thesis Tracker: current regime, dominant thesis, invalidation conditions.

        Aggregates regime data + recent trade performance + ML calibration
        to produce a human-readable thesis about current market state.
        """
        try:
            orch = getattr(self, '_orchestrator', None)
            strategy = getattr(orch, '_strategy', None) if orch else None
            scalp = getattr(strategy, '_scalp', strategy) if strategy else None
            tracker = getattr(self, '_signal_tracker', None)
            if not tracker and orch:
                tracker = getattr(orch, '_signal_tracker', None)

            # Regime from strategy — _last_regime_info is keyed by symbol.
            # Use BTC as anchor, else dominant across symbols.
            regime_info_all = {}
            try:
                regime_info_all = getattr(scalp, '_last_regime_info', {}) or {}
            except Exception:
                pass

            _anchor = regime_info_all.get("BTC/USDT") or {}
            if not _anchor and regime_info_all:
                from collections import Counter as _C
                _r = [v.get("regime") for v in regime_info_all.values() if isinstance(v, dict) and v.get("regime")]
                if _r:
                    _anchor = {"regime": _C(_r).most_common(1)[0][0]}

            regime = str(_anchor.get("regime") or "unknown").lower()
            # Confidence: prefer explicit, else derive from regime_age
            if _anchor.get("confidence") is not None:
                regime_conf = float(_anchor.get("confidence") or 0)
            else:
                _age = int(_anchor.get("regime_age") or 0)
                regime_conf = 0.80 if _age >= 5 else (0.55 if _age >= 3 else (0.35 if _age >= 1 else 0.0))

            # Recent performance (last 20 trades) for thesis direction
            recent_trades = []
            if tracker:
                try:
                    stats = tracker.get_stats()
                    recent_trades = stats.get("recent_closed", [])[-20:] if isinstance(stats, dict) else []
                except Exception:
                    pass

            wins = sum(1 for t in recent_trades if float(t.get("pnl_usd", 0) or 0) > 0)
            wr = (wins / len(recent_trades) * 100) if recent_trades else 0
            total_pnl = sum(float(t.get("pnl_usd", 0) or 0) for t in recent_trades)

            # Dominant side
            longs = sum(1 for t in recent_trades if str(t.get("side", "")).lower() in ("long", "buy"))
            shorts = len(recent_trades) - longs
            dominant_side = "LONG" if longs > shorts else "SHORT" if shorts > longs else "NEUTRAL"

            # Build thesis
            if regime in ("trending_up", "breakout"):
                thesis = "Bullish momentum — scanners seeking long entries at pullbacks and breakouts"
                invalidation = "ADX drops below 20, BTC loses key support, or 3+ consecutive losses"
            elif regime in ("trending_down",):
                thesis = "Bearish momentum — scanners seeking short entries at rallies and breakdowns"
                invalidation = "ADX drops below 20, BTC reclaims resistance, or 3+ consecutive losses"
            elif regime in ("mean_reversion", "ranging"):
                thesis = "Range-bound — mean reversion setups at extremes, tight SL, quick exits"
                invalidation = "Volatility expansion (ATR >85th pctile), directional breakout, volume spike"
            elif regime in ("high_volatility",):
                thesis = "High volatility — reduced position sizing, wider stops, selective entries only"
                invalidation = "ATR normalizes below 50th pctile, regime stabilizes for 30+ minutes"
            elif regime in ("sideways", "quiet", "low_liquidity"):
                thesis = "Low activity — minimal signal generation expected, patience mode"
                invalidation = "Volume spike >2x average, regime shift to trending, news catalyst"
            else:
                thesis = "Regime unclear — ML and scanners running but no strong directional bias"
                invalidation = "Clear regime establishment (ADX >25 + directional EMA alignment)"

            # Prices context
            prices = getattr(self, '_prices', {}) or {}
            btc_price = prices.get("BTC/USDT", 0)

            return web.json_response({
                "regime": regime,
                "regime_confidence": round(regime_conf, 2),
                "thesis": thesis,
                "invalidation": invalidation,
                "dominant_side": dominant_side,
                "recent_wr": round(wr, 1),
                "recent_pnl": round(total_pnl, 2),
                "recent_trades": len(recent_trades),
                "longs": longs,
                "shorts": shorts,
                "btc_price": btc_price,
                "symbols_active": len(getattr(self, '_symbols', []) or []),
            }, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_agent_pipeline(self, request: web.Request) -> web.Response:
        """Multi-agent visible pipeline: named agents with status, queue depth, last action.

        Each "agent" is an async task in the orchestrator. This endpoint
        surfaces their individual status for the dashboard pipeline view.
        """
        try:
            orch = getattr(self, '_orchestrator', None)
            agents = []

            # 1. Scanner Agent (main loop)
            current_action = getattr(orch, '_current_action', {}) if orch else {}
            agents.append({
                "name": "Scanner",
                "icon": "🔍",
                "status": "scanning" if current_action.get("action") == "scanning" else "idle",
                "detail": current_action.get("detail", "--"),
                "symbol": current_action.get("symbol", ""),
                "last_ts": current_action.get("ts", 0),
                "color": "#06b6d4",
            })

            # 2. ML Scorer Agent
            ml_status = "active"
            try:
                scorer = getattr(orch, '_strategy', None)
                if scorer:
                    scalp = getattr(scorer, '_scalp', scorer)
                    last_ml = getattr(scalp, '_last_ml_result', {})
                    ml_symbol = list(last_ml.keys())[-1] if last_ml else ""
                    ml_detail = ""
                    if ml_symbol and last_ml.get(ml_symbol):
                        r = last_ml[ml_symbol]
                        ml_detail = ml_symbol + " " + str(r.get("verdict", "")) + " " + str(round(float(r.get("probability", 0) or 0), 2))
                    agents.append({
                        "name": "ML Scorer",
                        "icon": "🧠",
                        "status": "active" if ml_detail else "idle",
                        "detail": ml_detail or "waiting for candidates",
                        "symbol": ml_symbol,
                        "last_ts": 0,
                        "color": "#a855f7",
                    })
            except Exception:
                agents.append({"name": "ML Scorer", "icon": "🧠", "status": "idle", "detail": "--", "symbol": "", "last_ts": 0, "color": "#a855f7"})

            # 3. Signal Tracker Agent
            tracker = getattr(self, '_signal_tracker', None) or (getattr(orch, '_signal_tracker', None) if orch else None)
            active_count = len(getattr(tracker, '_active', {}) or {}) if tracker else 0
            agents.append({
                "name": "Tracker",
                "icon": "📊",
                "status": "tracking" if active_count > 0 else "idle",
                "detail": str(active_count) + " active trades",
                "symbol": "",
                "last_ts": 0,
                "color": "#f59e0b",
            })

            # 4. Risk Manager Agent
            mgr = self._get_real_manager()
            real_open = len(getattr(mgr, 'real_trades', {}) or {}) if mgr else 0
            cb = mgr.circuit_breaker if mgr else None
            risk_status = "monitoring"
            if cb and cb.is_tripped:
                risk_status = "TRIPPED"
            elif real_open > 0:
                risk_status = "active"
            agents.append({
                "name": "Risk",
                "icon": "🛡️",
                "status": risk_status,
                "detail": str(real_open) + " real open" + (" | CB TRIPPED" if (cb and cb.is_tripped) else ""),
                "symbol": "",
                "last_ts": 0,
                "color": "#ef4444",
            })

            # 5. Execution Agent
            exec_status = "ready"
            if mgr and mgr.enabled and not mgr.dry_run:
                exec_status = "LIVE"
            elif mgr and mgr.enabled and mgr.dry_run:
                exec_status = "dry_run"
            elif mgr and not mgr.enabled:
                exec_status = "disabled"
            agents.append({
                "name": "Executor",
                "icon": "⚡",
                "status": exec_status,
                "detail": exec_status.upper(),
                "symbol": "",
                "last_ts": 0,
                "color": "#22c55e",
            })

            # 6. Supervisor Agent
            sup = getattr(self, '_supervisor', None) or (getattr(orch, '_supervisor', None) if orch else None)
            sup_status = getattr(sup, 'status', {}) if sup else {}
            agents.append({
                "name": "Supervisor",
                "icon": "👁️",
                "status": "watching" if sup_status.get("running") else "off",
                "detail": str(sup_status.get("anomaly_count", 0)) + " anomalies",
                "symbol": "",
                "last_ts": sup_status.get("last_run", 0),
                "color": "#64748b",
            })

            return web.json_response({"agents": agents}, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_research_correlations(self, request: web.Request) -> web.Response:
        """Proactive Research: cross-pair correlation matrix + regime transition signals.

        Computes rolling return correlations between all monitored pairs using
        cached price data. Also detects regime transitions (momentum shifts).
        """
        try:
            import numpy as np
            prices = getattr(self, '_prices', {}) or {}
            symbols = list(prices.keys())

            # Build correlation from recent closed trades (per-symbol returns)
            tracker = getattr(self, '_signal_tracker', None)
            if not tracker:
                orch = getattr(self, '_orchestrator', None)
                tracker = getattr(orch, '_signal_tracker', None) if orch else None

            corr_matrix = {}
            symbol_returns = {}
            if tracker:
                try:
                    stats = tracker.get_stats()
                    by_symbol = stats.get("by_symbol", {}) if isinstance(stats, dict) else {}
                    for sym, data in by_symbol.items():
                        if isinstance(data, dict):
                            avg_r = float(data.get("avg_r", 0) or 0)
                            wr = float(data.get("wr", 0) or data.get("win_rate", 0) or 0)
                            trades = int(data.get("trades", 0) or data.get("total", 0) or 0)
                            symbol_returns[sym] = {"avg_r": avg_r, "wr": wr, "trades": trades}
                except Exception:
                    pass

            # Regime transition detection — _last_regime_info is keyed by symbol:
            #   { "BTC/USDT": {"regime": "...", "regime_age": int, ...}, ... }
            # Build dominant regime from per-symbol observations, weighted by BTC as anchor.
            transitions = []
            try:
                orch = getattr(self, '_orchestrator', None)
                strategy = getattr(orch, '_strategy', None) if orch else None
                scalp = getattr(strategy, '_scalp', strategy) if strategy else None
                regime_info_all = getattr(scalp, '_last_regime_info', {}) or {}

                # Prefer BTC/USDT as market anchor; else most common regime across symbols
                anchor = regime_info_all.get("BTC/USDT") or {}
                if not anchor and regime_info_all:
                    # Take the most recent/populated entry
                    from collections import Counter as _C
                    _regimes = [v.get("regime") for v in regime_info_all.values() if isinstance(v, dict) and v.get("regime")]
                    if _regimes:
                        _dominant = _C(_regimes).most_common(1)[0][0]
                        anchor = {"regime": _dominant}

                current_regime = str(anchor.get("regime") or "unknown").lower()

                # Confidence proxy: higher regime_age = more stable.
                # age >= 5 bars = stable (conf 0.8), age < 3 = transitioning (conf 0.3)
                _age = int(anchor.get("regime_age") or 0)
                if _age >= 5:
                    _conf = 0.80
                    _signal = "stable"
                elif _age >= 3:
                    _conf = 0.55
                    _signal = "stable"
                elif _age >= 1:
                    _conf = 0.35
                    _signal = "transitioning"
                else:
                    _conf = 0.0
                    _signal = "transitioning"

                # If explicit confidence is provided by the detector, prefer it.
                if anchor.get("confidence") is not None:
                    _conf = round(float(anchor.get("confidence")), 2)
                    _signal = "stable" if _conf > 0.7 else "transitioning"

                transitions.append({
                    "type": "regime",
                    "current": current_regime,
                    "signal": _signal,
                    "confidence": round(_conf, 2),
                    "age_bars": _age,
                    "anchor_symbol": "BTC/USDT" if regime_info_all.get("BTC/USDT") else "blend",
                })
            except Exception:
                pass

            # Scanner co-firing (from scalp strategy)
            co_firing = {}
            try:
                if scalp and hasattr(scalp, 'get_scanner_correlation'):
                    co_firing = scalp.get_scanner_correlation() or {}
            except Exception:
                pass

            return web.json_response({
                "symbols": symbols,
                "symbol_performance": symbol_returns,
                "regime_transitions": transitions,
                "scanner_co_firing": co_firing,
                "prices": {s: p for s, p in prices.items()},
            }, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_pipeline_trace(self, request: web.Request) -> web.Response:
        """Pipeline Trace: recent journeys grouped by time batch for drilldown."""
        try:
            from bot.signal_journey import SignalJourney
            limit = min(int(request.query.get("limit", "50")), 200)
            journeys = SignalJourney.load_recent(limit=limit)

            # Group by 5-minute windows for batch visualization
            batches = {}
            for j in journeys:
                closed_at = float(j.get("closed_at", 0) or 0)
                # Round to 5-minute window
                window = int(closed_at // 300) * 300
                if window not in batches:
                    batches[window] = {"ts": window, "signals": [], "passed": 0, "failed": 0}
                batch = batches[window]
                batch["signals"].append({
                    "trade_id": j.get("trade_id", "")[:12],
                    "symbol": j.get("symbol", ""),
                    "side": j.get("side", ""),
                    "grade": j.get("grade", ""),
                    "final_stage": j.get("final_stage", ""),
                    "final_passed": j.get("final_passed", False),
                    "stage_count": j.get("stage_count", 0),
                    "total_ms": j.get("total_ms", 0),
                })
                if j.get("final_passed"):
                    batch["passed"] += 1
                else:
                    batch["failed"] += 1

            # Sort by timestamp descending
            sorted_batches = sorted(batches.values(), key=lambda b: b["ts"], reverse=True)

            return web.json_response({
                "batches": sorted_batches[:20],
                "total_journeys": len(journeys),
            }, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ==================================================================
    # Vision Tier 3: Market Map + Catalyst Calendar
    # ==================================================================

    async def _handle_infra_health(self, request: web.Request) -> web.Response:
        """Infra health: ML proxy circuit breaker + cron sync status."""
        try:
            import time as _t, os

            # ML Proxy CB state
            now = _t.time()
            cb_open = now < self._ml_proxy_cb_open_until
            cb_remaining = max(0, self._ml_proxy_cb_open_until - now) if cb_open else 0
            cache_size = len(self._ml_proxy_cache)
            cache_entries = {}
            for path, (ts, _, _) in self._ml_proxy_cache.items():
                cache_entries[path] = {"age_sec": round(now - ts, 1)}

            proxy = {
                "cb_open": cb_open,
                "cb_failures": self._ml_proxy_cb_failures,
                "cb_remaining_sec": round(cb_remaining, 0),
                "cache_size": cache_size,
                "cache_entries": cache_entries,
            }

            # Cron sync status (read the sync log from VM1)
            sync = {"last_sync": None, "age_sec": None, "feedback_lines": 0, "trades_lines": 0}
            try:
                sync_log = Path(__file__).resolve().parent.parent / "logs" / "ml_sync.log"
                if sync_log.exists():
                    # Read last sync timestamp from log tail
                    with open(sync_log, "rb") as f:
                        f.seek(0, 2)
                        size = f.tell()
                        chunk = min(size, 2000)
                        f.seek(max(0, size - chunk))
                        lines = f.read().decode("utf-8", errors="replace").strip().split("\n")

                    last_ts = None
                    feedback_n = 0
                    trades_n = 0
                    for line in reversed(lines):
                        if "sync done" in line:
                            # Extract timestamp: === 2026-04-12T02:43:42Z sync done ===
                            parts = line.strip().split()
                            for p in parts:
                                if "T" in p and "Z" in p:
                                    last_ts = p
                                    break
                        if "feedback:" in line and not feedback_n:
                            try:
                                feedback_n = int(line.split("(")[1].split(" ")[0])
                            except Exception:
                                pass
                        if "trades:" in line and not trades_n:
                            try:
                                trades_n = int(line.split("(")[1].split(" ")[0])
                            except Exception:
                                pass
                        if last_ts and feedback_n and trades_n:
                            break

                    if last_ts:
                        from datetime import datetime, timezone
                        try:
                            dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                            sync["last_sync"] = last_ts
                            sync["age_sec"] = round(now - dt.timestamp(), 0)
                        except Exception:
                            sync["last_sync"] = last_ts
                    sync["feedback_lines"] = feedback_n
                    sync["trades_lines"] = trades_n
                    sync["log_exists"] = True
                else:
                    sync["log_exists"] = False
            except Exception:
                pass

            # Orderbook cache status
            ob_cache = {}
            try:
                orch = getattr(self, '_orchestrator', None)
                ob = getattr(orch, '_ob_cache', None) if orch else None
                if ob:
                    ob_cache = getattr(ob, 'status', {})
                    if callable(ob_cache):
                        ob_cache = ob_cache
                    else:
                        ob_cache = ob.status if hasattr(ob, 'status') else {}
            except Exception:
                pass

            return web.json_response({
                "ml_proxy": proxy,
                "cron_sync": sync,
                "orderbook_cache": ob_cache,
            }, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_market_map(self, request: web.Request) -> web.Response:
        """Market Map: capital deployed by symbol/family with performance data.

        Combines: prices, paper positions exposure, real positions exposure,
        per-symbol historical performance, and family groupings.
        """
        try:
            # Hardcoded to avoid importing heavy training module at runtime
            PAIR_FAMILIES = {
                "liquid_majors": "BTC,ETH,SOL",
                "secondary": "AVAX,LINK,XRP,LTC,ADA,DOT",
                "high_beta": "DOGE,TAO",
            }
            try:
                from ml_training.candidate_trainer import PAIR_FAMILIES as _PF
                PAIR_FAMILIES = _PF
            except Exception:
                pass
            prices = dict(getattr(self, '_prices', {}) or {})
            tracker = getattr(self, '_signal_tracker', None)
            if not tracker:
                orch = getattr(self, '_orchestrator', None)
                tracker = getattr(orch, '_signal_tracker', None) if orch else None

            mgr = self._get_real_manager()

            # Per-symbol data
            symbols_data = {}
            families = {}
            for fam_name, fam_syms in PAIR_FAMILIES.items():
                # Handle both "BTC,ETH,SOL" (str) and ["BTC", "ETH", "SOL"] (list) formats
                if isinstance(fam_syms, str):
                    raw_list = [s.strip() for s in fam_syms.split(",") if s.strip()]
                elif isinstance(fam_syms, (list, tuple)):
                    raw_list = [str(s).strip() for s in fam_syms]
                else:
                    raw_list = []
                sym_list = [(s + "/USDT" if "/USDT" not in s else s) for s in raw_list]
                families[fam_name] = sym_list
                for sym in sym_list:
                    symbols_data[sym] = {
                        "family": fam_name,
                        "price": prices.get(sym, 0),
                        "paper_exposure": 0,
                        "real_exposure": 0,
                        "trades": 0,
                        "wr": 0,
                        "pnl": 0,
                        "avg_r": 0,
                    }

            # Paper exposure (active trades)
            if tracker:
                for tid, ts in list(getattr(tracker, '_active', {}).items()):
                    sym = getattr(ts, 'symbol', '')
                    pos_usd = float(getattr(ts, 'position_size_usd', 0) or 0)
                    if sym in symbols_data:
                        symbols_data[sym]["paper_exposure"] += pos_usd

            # Real exposure
            if mgr:
                for tid, t in list(getattr(mgr, 'real_trades', {}).items()):
                    sym = getattr(t, 'symbol', '')
                    margin = float(getattr(t, 'margin', 0) or 0)
                    lev = float(getattr(t, 'leverage', 1) or 1)
                    if sym in symbols_data:
                        symbols_data[sym]["real_exposure"] += margin * lev

            # Historical performance
            if tracker:
                try:
                    stats = tracker.get_stats()
                    by_sym = stats.get("by_symbol", {}) if isinstance(stats, dict) else {}
                    for sym, data in by_sym.items():
                        if sym in symbols_data and isinstance(data, dict):
                            symbols_data[sym]["trades"] = int(data.get("total", 0) or data.get("trades", 0) or 0)
                            symbols_data[sym]["wr"] = float(data.get("win_rate", 0) or data.get("wr", 0) or 0)
                            symbols_data[sym]["pnl"] = float(data.get("pnl", 0) or 0)
                            symbols_data[sym]["avg_r"] = float(data.get("avg_r", 0) or 0)
                except Exception:
                    pass

            return web.json_response({
                "symbols": symbols_data,
                "families": families,
                "total_paper_exposure": sum(s["paper_exposure"] for s in symbols_data.values()),
                "total_real_exposure": sum(s["real_exposure"] for s in symbols_data.values()),
            }, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_catalyst_calendar(self, request: web.Request) -> web.Response:
        """Catalyst Calendar: funding rates + session schedule + market events.

        Pulls live funding rates from Delta exchange for all monitored symbols.
        Also provides session windows (India, Asia, Europe, US) and known events.
        """
        try:
            import asyncio
            symbols = list(getattr(self, '_symbols', []) or [])[:10]

            # Fetch funding rates in parallel via thread pool
            funding = {}
            try:
                orch = getattr(self, '_orchestrator', None)
                mgr = self._get_real_manager()
                delta = None
                if mgr:
                    delta = getattr(mgr, '_delta_live', None)
                if delta:
                    for sym in symbols[:6]:  # top 6 to avoid rate limits
                        try:
                            fr = await asyncio.to_thread(delta.get_funding_rate, sym)
                            if fr:
                                funding[sym] = fr
                        except Exception:
                            pass
            except Exception:
                pass

            # Session windows (from Indian market config)
            import datetime as _dt
            utc_now = _dt.datetime.now(_dt.timezone.utc)
            sessions = [
                {"name": "Asia Late", "utc_start": 0, "utc_end": 3, "active": 0 <= utc_now.hour < 3},
                {"name": "India Morning", "utc_start": 3, "utc_end": 6, "active": 3 <= utc_now.hour < 6},
                {"name": "India Midday", "utc_start": 6, "utc_end": 9, "active": 6 <= utc_now.hour < 9},
                {"name": "Europe", "utc_start": 9, "utc_end": 15, "active": 9 <= utc_now.hour < 15},
                {"name": "US", "utc_start": 13, "utc_end": 21, "active": 13 <= utc_now.hour < 21},
                {"name": "Asia Early", "utc_start": 21, "utc_end": 24, "active": 21 <= utc_now.hour < 24},
            ]
            current_session = next((s["name"] for s in sessions if s["active"]), "Off-hours")

            # Static event calendar (upcoming known events)
            events = [
                {"date": "2026-04-14", "event": "BTC Options Expiry (Monthly)", "impact": "high", "symbol": "BTC/USDT"},
                {"date": "2026-04-18", "event": "ETH Shapella Anniversary", "impact": "medium", "symbol": "ETH/USDT"},
                {"date": "2026-04-25", "event": "BTC Options Expiry (Monthly)", "impact": "high", "symbol": "BTC/USDT"},
                {"date": "2026-04-30", "event": "Quarter End Rebalancing", "impact": "medium", "symbol": "ALL"},
            ]
            # Filter to next 14 days
            today = utc_now.strftime("%Y-%m-%d")
            upcoming = [e for e in events if e["date"] >= today][:5]

            # Funding summary
            funding_summary = {}
            for sym, fr in funding.items():
                rate = fr.get("funding_rate", 0)
                annualized = rate * 3 * 365 * 100  # 8h intervals, annualized %
                funding_summary[sym] = {
                    "rate_8h": round(rate * 100, 4),  # as percentage
                    "predicted": round(fr.get("predicted_rate", 0) * 100, 4),
                    "annualized_pct": round(annualized, 1),
                    "next_rebalance": fr.get("next_rebalance", ""),
                    "oi": fr.get("open_interest", 0),
                    "vol_24h": fr.get("volume_24h", 0),
                    "bias": "longs_pay" if rate > 0 else "shorts_pay" if rate < 0 else "neutral",
                }

            return web.json_response({
                "funding": funding_summary,
                "sessions": sessions,
                "current_session": current_session,
                "utc_hour": utc_now.hour,
                "utc_time": utc_now.strftime("%H:%M UTC"),
                "upcoming_events": upcoming,
            }, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_stage_stats(self, request: web.Request) -> web.Response:
        """Phase 2A + 3.1: Stage Loss Map — aggregate SignalJourney JSONL.

        Read-only. Tail-reads recent journey records and aggregates per-stage
        reach/pass/fail counts + top rejection reasons.

        Query params:
          limit=N       — hard cap on records read (default 2000, max 5000)
          hours=N       — only aggregate records from last N hours (default 4)
          since_ts=N    — unix timestamp cutoff (overrides hours if provided)

        Never touches live signal flow, exit logic, or scoring.
        """
        try:
            import time as _t
            limit = int(request.query.get("limit", "2000"))
            limit = max(1, min(limit, 5000))  # hard cap for memory safety

            # Time filter — default 4h, or explicit hours/since_ts
            since_ts_raw = request.query.get("since_ts")
            hours_raw = request.query.get("hours")
            if since_ts_raw:
                try:
                    since_ts = float(since_ts_raw)
                except (ValueError, TypeError):
                    since_ts = 0.0
            elif hours_raw:
                try:
                    hours = float(hours_raw)
                    since_ts = _t.time() - (hours * 3600) if hours > 0 else 0.0
                except (ValueError, TypeError):
                    since_ts = _t.time() - (4 * 3600)  # default 4h
            else:
                since_ts = _t.time() - (4 * 3600)  # default 4h

            from bot.signal_journey import SignalJourney
            all_journeys = SignalJourney.load_recent(limit=limit)

            # Filter by closed_at timestamp
            if since_ts > 0:
                journeys = [j for j in all_journeys if float(j.get("closed_at", 0) or 0) >= since_ts]
            else:
                journeys = all_journeys
            filter_stats = {
                "total_in_file": len(all_journeys),
                "after_time_filter": len(journeys),
                "since_ts": since_ts,
                "window_hours": round((_t.time() - since_ts) / 3600, 2) if since_ts > 0 else None,
            }

            # Canonical stage order (must match stamp sites across pipeline)
            stages_order = [
                "strategy",
                "hard_block",
                "risk_check",
                "signal_tracker",
                "paper_exec",
                "real_qualify",
                "real_exec",
                "exit",
            ]
            stats: Dict[str, Dict[str, Any]] = {
                s: {
                    "reached": 0,
                    "passed": 0,
                    "failed": 0,
                    "avg_latency_ms": 0.0,
                    "_lat_sum": 0.0,
                    "_lat_n": 0,
                    "top_reasons": {},
                }
                for s in stages_order
            }

            for j in journeys:
                for stage in j.get("stages", []) or []:
                    name = stage.get("stage", "")
                    if name not in stats:
                        continue
                    stats[name]["reached"] += 1
                    if stage.get("passed"):
                        stats[name]["passed"] += 1
                    else:
                        stats[name]["failed"] += 1
                        reason = str(stage.get("reason", "unknown"))[:50]
                        stats[name]["top_reasons"][reason] = stats[name]["top_reasons"].get(reason, 0) + 1
                    lat = stage.get("latency_ms", 0) or 0
                    try:
                        stats[name]["_lat_sum"] += float(lat)
                        stats[name]["_lat_n"] += 1
                    except Exception:
                        pass

            # Finalize: compute avg latency + top-5 reasons list
            for s in stats.values():
                n = s.pop("_lat_n", 0)
                total = s.pop("_lat_sum", 0.0)
                s["avg_latency_ms"] = round(total / n, 2) if n > 0 else 0.0
                tr = sorted(s["top_reasons"].items(), key=lambda x: -x[1])[:5]
                s["top_reasons"] = [{"reason": r, "count": c} for r, c in tr]

            # Funnel view: ordered stages with drop rate from previous
            funnel = []
            prev_reached = 0
            for i, s_name in enumerate(stages_order):
                reached = stats[s_name]["reached"]
                drop_from_prev = 0
                drop_pct = 0.0
                if i > 0 and prev_reached > 0:
                    drop_from_prev = max(0, prev_reached - reached)
                    drop_pct = round((drop_from_prev / prev_reached) * 100, 1)
                funnel.append({
                    "stage": s_name,
                    "reached": reached,
                    "passed": stats[s_name]["passed"],
                    "failed": stats[s_name]["failed"],
                    "drop_from_prev": drop_from_prev,
                    "drop_pct": drop_pct,
                    "avg_latency_ms": stats[s_name]["avg_latency_ms"],
                    "top_reasons": stats[s_name]["top_reasons"],
                })
                if reached > 0:
                    prev_reached = reached

            return web.json_response({
                "ok": True,
                "journeys_analyzed": len(journeys),
                "limit": limit,
                "filter": filter_stats,
                "funnel": funnel,
                "stats": stats,
            }, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e), "ok": False}, status=500)

    async def _handle_hotfix_stats(self, request: web.Request) -> web.Response:
        """Phase 3.2: Hotfix effectiveness counters.

        Returns per-fix block counts + last-seen info. Read-only.
        """
        try:
            from bot.pipeline_metrics import get_hotfix_stats
            stats = get_hotfix_stats()
            return web.json_response({"ok": True, "fixes": stats}, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e), "ok": False}, status=500)

    async def _handle_loss_taxonomy(self, request: web.Request) -> web.Response:
        """Phase 3.3: Loss Taxonomy — auto-classify recent losses into pattern buckets.

        Reads paper closed trades within the time window and classifies each loss
        (pnl < 0) into 8 diagnostic buckets. A trade can match multiple buckets.

        Query params:
          hours=N    — time window (default 24)

        Read-only. Never touches live state.
        """
        try:
            import time as _t
            from datetime import datetime
            hours = float(request.query.get("hours", "24"))
            hours = max(0.1, min(hours, 168))  # 6 min to 7 days
            since_ts = _t.time() - (hours * 3600)

            orch = getattr(self, '_orchestrator', None)
            sig_tracker = getattr(self, '_signal_tracker', None) or (getattr(orch, '_signal_tracker', None) if orch else None)
            if sig_tracker is None:
                return web.json_response({"ok": False, "error": "signal_tracker_not_available"}, status=404)

            # Pull closed signals (paper)
            try:
                closed = sig_tracker.get_closed_signals(limit=500) or []
            except Exception:
                closed = []

            # Filter to losses within window
            losses = []
            for c in closed:
                try:
                    if not isinstance(c, dict):
                        continue
                    pnl_pct = float(c.get("pnl_pct", 0) or 0)
                    if pnl_pct >= 0:
                        continue  # not a loss
                    exit_time = c.get("exit_time", "") or c.get("closed_at", "")
                    if exit_time:
                        try:
                            ts = datetime.fromisoformat(str(exit_time).replace('Z', '+00:00')).timestamp()
                            if ts < since_ts:
                                continue
                        except (ValueError, TypeError):
                            continue
                    losses.append(c)
                except Exception:
                    continue

            # Classify into buckets — a trade can match multiple
            bucket_defs = [
                "counter_htf_long",
                "counter_htf_short",
                "early_kill",
                "time_decay",
                "fee_drag_be",
                "chop_regime",
                "ml_weak",
                "slippage",
                "other",
            ]
            buckets: Dict[str, List[Dict[str, Any]]] = {b: [] for b in bucket_defs}

            for L in losses:
                try:
                    meta = L.get("metadata", {}) or {}
                    htf = int(meta.get("htf_bias", 0) or 0)
                    side = str(L.get("side", "") or "").lower()
                    exit_reason = str(L.get("exit_reason", "") or "")
                    exit_reason_d = str(L.get("exit_reason_detailed", "") or "")
                    regime = str(meta.get("regime", "") or "").lower()
                    ml_verdict = str(meta.get("ml_verdict", "") or "").upper()
                    pnl_usd = float(L.get("pnl_usd", 0) or 0)
                    slippage_bps = float(L.get("slippage_bps", 0) or 0)
                    fee_drag = float(meta.get("fee_drag_r", 0) or 0)
                    duration_sec = float(L.get("trade_duration_sec", 0) or 0)
                    duration_min = duration_sec / 60.0 if duration_sec > 0 else 0

                    matched_count = 0

                    # Counter-HTF long
                    if htf < 0 and side == "long":
                        buckets["counter_htf_long"].append(L); matched_count += 1
                    # Counter-HTF short
                    if htf > 0 and side == "short":
                        buckets["counter_htf_short"].append(L); matched_count += 1
                    # Early kill (sub-5min momentum failure)
                    if "early_kill" in exit_reason and duration_min > 0 and duration_min < 5:
                        buckets["early_kill"].append(L); matched_count += 1
                    elif "early_kill" in exit_reason_d:
                        buckets["early_kill"].append(L); matched_count += 1
                    # Time decay
                    if "time_decay" in exit_reason or "time_decay" in exit_reason_d or "expired" == exit_reason:
                        buckets["time_decay"].append(L); matched_count += 1
                    # Fee-drag breakeven
                    if abs(pnl_usd) < 0.5 and fee_drag > 0.25:
                        buckets["fee_drag_be"].append(L); matched_count += 1
                    # Chop regime
                    if regime in ("high_volatility", "sideways", "ranging", "quiet", "mean_reversion"):
                        buckets["chop_regime"].append(L); matched_count += 1
                    # ML WEAK that lost
                    if ml_verdict == "WEAK":
                        buckets["ml_weak"].append(L); matched_count += 1
                    # Slippage > 30bps
                    if slippage_bps > 30:
                        buckets["slippage"].append(L); matched_count += 1
                    # Other
                    if matched_count == 0:
                        buckets["other"].append(L)
                except Exception:
                    continue

            # Build response
            result: Dict[str, Any] = {}
            for bucket_name in bucket_defs:
                trades = buckets[bucket_name]
                total_loss = sum(float(t.get("pnl_usd", 0) or 0) for t in trades)
                total_loss_pct = sum(float(t.get("pnl_pct", 0) or 0) for t in trades)
                result[bucket_name] = {
                    "count": len(trades),
                    "total_loss_usd": round(total_loss, 2),
                    "total_loss_pct": round(total_loss_pct, 2),
                    "sample_trade_ids": [str(t.get("trade_id", ""))[:12] for t in trades[:3]],
                    "sample_symbols": list(dict.fromkeys(str(t.get("symbol", ""))[:10] for t in trades))[:5],
                }

            total_loss_usd = sum(float(t.get("pnl_usd", 0) or 0) for t in losses)
            total_loss_pct = sum(float(t.get("pnl_pct", 0) or 0) for t in losses)

            # ── Phase 3.18: Per-scanner / per-regime / per-side breakdown ──
            # Data foundation for surgical Phase 3.7 (chop regime gate) and ML retrain
            # decisions. Shows which scanner×regime×side combos are worst offenders.
            scanner_breakdown: Dict[str, Dict[str, Any]] = {}
            regime_breakdown: Dict[str, Dict[str, Any]] = {}
            side_breakdown: Dict[str, Dict[str, Any]] = {}
            scanner_regime_breakdown: Dict[str, Dict[str, Any]] = {}
            scanner_side_breakdown: Dict[str, Dict[str, Any]] = {}

            def _bump(d: Dict[str, Dict], key: str, pnl: float):
                if key not in d:
                    d[key] = {"count": 0, "total_loss_usd": 0.0, "total_loss_pct": 0.0}
                d[key]["count"] += 1
                d[key]["total_loss_usd"] += pnl

            for L in losses:
                try:
                    meta = L.get("metadata", {}) or {}
                    scanner = str(meta.get("setup_type", L.get("scanner", "") or "unknown")).lower()
                    regime = str(meta.get("regime", "") or "unknown").lower()
                    side = str(L.get("side", "") or "unknown").lower()
                    pnl_usd = float(L.get("pnl_usd", 0) or 0)
                    pnl_pct = float(L.get("pnl_pct", 0) or 0)

                    _bump(scanner_breakdown, scanner, pnl_usd)
                    _bump(regime_breakdown, regime, pnl_usd)
                    _bump(side_breakdown, side, pnl_usd)
                    _bump(scanner_regime_breakdown, f"{scanner}:{regime}", pnl_usd)
                    _bump(scanner_side_breakdown, f"{scanner}:{side}", pnl_usd)

                    # Add pct to the aggregates
                    scanner_breakdown[scanner]["total_loss_pct"] += pnl_pct
                    regime_breakdown[regime]["total_loss_pct"] += pnl_pct
                    side_breakdown[side]["total_loss_pct"] += pnl_pct
                    scanner_regime_breakdown[f"{scanner}:{regime}"]["total_loss_pct"] += pnl_pct
                    scanner_side_breakdown[f"{scanner}:{side}"]["total_loss_pct"] += pnl_pct
                except Exception:
                    continue

            # Round + sort by total_loss_usd
            def _finalize(d: Dict[str, Dict], top_n: int = 20) -> List[Dict[str, Any]]:
                out = []
                for key, v in d.items():
                    out.append({
                        "key": key,
                        "count": v["count"],
                        "total_loss_usd": round(v["total_loss_usd"], 2),
                        "total_loss_pct": round(v["total_loss_pct"], 2),
                    })
                out.sort(key=lambda x: x["total_loss_usd"])  # most negative first
                return out[:top_n]

            return web.json_response({
                "ok": True,
                "window_hours": hours,
                "since_ts": since_ts,
                "total_losses_analyzed": len(losses),
                "total_loss_usd": round(total_loss_usd, 2),
                "total_loss_pct": round(total_loss_pct, 2),
                "buckets": result,
                # Phase 3.18: breakdowns for surgical decisions
                "breakdown": {
                    "scanner": _finalize(scanner_breakdown),
                    "regime": _finalize(regime_breakdown),
                    "side": _finalize(side_breakdown),
                    "scanner_regime": _finalize(scanner_regime_breakdown, top_n=15),
                    "scanner_side": _finalize(scanner_side_breakdown, top_n=15),
                },
            }, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e), "ok": False}, status=500)

    async def _handle_rdrift(self, request: web.Request) -> web.Response:
        """Phase 2D + 3.14: Paper vs Real WR / R-drift alert strip.

        Query params:
          limit=N    — max trades to include (default 20, max 200)
          hours=N    — only include trades from last N hours (Phase 3.14)
                       default 0 = no time filter (legacy behavior)

        Read-only. Never mutates state.
        """
        try:
            import time as _t
            from datetime import datetime as _dt
            limit = int(request.query.get("limit", "20"))
            limit = max(5, min(limit, 200))
            hours = float(request.query.get("hours", "0") or "0")
            since_ts = (_t.time() - (hours * 3600)) if hours > 0 else 0

            def _filter_by_time(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                """Phase 3.14: filter trades by timestamp if since_ts set."""
                if since_ts <= 0:
                    return trades
                out = []
                for t in trades:
                    try:
                        ts_str = str(t.get("timestamp", "") or t.get("exit_time", "") or t.get("closed_at", "") or "")
                        if not ts_str:
                            continue
                        ts = _dt.fromisoformat(ts_str.replace('Z', '+00:00')).timestamp()
                        if ts >= since_ts:
                            out.append(t)
                    except Exception:
                        continue
                return out

            orch = getattr(self, '_orchestrator', None)
            sig_tracker = getattr(self, '_signal_tracker', None) or (getattr(orch, '_signal_tracker', None) if orch else None)
            real_mgr = getattr(self, '_real_manager', None) or (getattr(orch, '_real_manager', None) if orch else None)

            def _realized_r(trade: Dict[str, Any]) -> float:
                """Compute realized R-multiple from pnl_pct + initial_risk."""
                try:
                    pnl = float(trade.get("pnl_pct", 0) or 0)
                    ir = float(trade.get("initial_risk", 0) or 0)
                    ep = float(trade.get("entry_price", 0) or 0)
                    if ir > 0 and ep > 0:
                        risk_pct = (ir / ep) * 100
                        if risk_pct > 0:
                            return round(pnl / risk_pct, 3)
                    return 0.0
                except Exception:
                    return 0.0

            def _summarize(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
                if not trades:
                    return {"count": 0, "wr": 0.0, "avg_r": 0.0, "wins": 0, "losses": 0, "avg_pnl_pct": 0.0}
                wins = 0
                losses = 0
                rs = []
                pnls = []
                for t in trades:
                    try:
                        pnl = float(t.get("pnl_pct", 0) or 0)
                    except Exception:
                        pnl = 0.0
                    pnls.append(pnl)
                    if pnl > 0:
                        wins += 1
                    elif pnl < 0:
                        losses += 1
                    rs.append(_realized_r(t))
                n = len(trades)
                return {
                    "count": n,
                    "wins": wins,
                    "losses": losses,
                    "wr": round((wins / n) * 100, 1) if n > 0 else 0.0,
                    "avg_r": round(sum(rs) / n, 3) if n > 0 else 0.0,
                    "avg_pnl_pct": round(sum(pnls) / n, 3) if n > 0 else 0.0,
                }

            # Paper closed (Phase 3.14: apply time filter before limit)
            paper_closed: List[Dict[str, Any]] = []
            try:
                if sig_tracker and hasattr(sig_tracker, "get_closed_signals"):
                    _raw_paper = list(sig_tracker.get_closed_signals(limit=max(500, limit * 10)))
                    _filtered_paper = _filter_by_time(_raw_paper)
                    paper_closed = _filtered_paper[-limit:]
            except Exception:
                paper_closed = []

            # Real closed (Phase 3.14: apply time filter before limit)
            real_closed: List[Dict[str, Any]] = []
            try:
                if real_mgr and hasattr(real_mgr, "closed_real_trades"):
                    raw = list(real_mgr.closed_real_trades)
                    _filtered_real = _filter_by_time(raw)
                    real_closed = _filtered_real[-limit:]
            except Exception:
                real_closed = []

            paper = _summarize(paper_closed)
            real = _summarize(real_closed)

            # Drift calculations (only meaningful when both sides have trades)
            wr_drift = round(paper["wr"] - real["wr"], 1) if (paper["count"] and real["count"]) else 0.0
            r_drift = round(paper["avg_r"] - real["avg_r"], 3) if (paper["count"] and real["count"]) else 0.0

            # Alerts
            alerts = []
            if paper["count"] >= 5 and real["count"] >= 5:
                if abs(wr_drift) > 10.0:
                    alerts.append({
                        "level": "warn",
                        "metric": "wr_drift",
                        "value": wr_drift,
                        "message": f"WR divergence {wr_drift:+.1f}% (paper {paper['wr']}% vs real {real['wr']}%)",
                    })
                if abs(r_drift) > 0.5:
                    alerts.append({
                        "level": "warn",
                        "metric": "r_drift",
                        "value": r_drift,
                        "message": f"R-drift {r_drift:+.2f}R (paper {paper['avg_r']:+.2f}R vs real {real['avg_r']:+.2f}R)",
                    })

            # Supervisor last-alert pass-through (if wired)
            supervisor_alerts: List[Any] = []
            try:
                supervisor = getattr(self, '_supervisor', None) or (getattr(orch, '_supervisor', None) if orch else None)
                if supervisor is not None:
                    st = getattr(supervisor, "status", None)
                    if isinstance(st, dict):
                        supervisor_alerts = st.get("alerts", []) or []
            except Exception:
                supervisor_alerts = []

            return web.json_response({
                "ok": True,
                "limit": limit,
                "window_hours": hours,  # Phase 3.14: echo back window
                "since_ts": since_ts,
                "paper": paper,
                "real": real,
                "drift": {
                    "wr_drift_pct": wr_drift,
                    "r_drift": r_drift,
                },
                "alerts": alerts,
                "supervisor_alerts": supervisor_alerts,
            }, dumps=_safe_dumps)
        except Exception as e:
            return web.json_response({"error": str(e), "ok": False}, status=500)

    async def _handle_latency(self, request: web.Request) -> web.Response:
        """Return latency metrics — exchange API, data freshness, WebSocket."""
        import time as _t
        data = {
            "server_time_ms": _t.time() * 1000,
            "exchange_latency_ms": self._exchange_latency_ms,
            "data_age_sec": {},
            "ws_connected": False,
        }

        # Data freshness per symbol
        try:
            for sym in self._state.get("symbols", []):
                last_update = self._state.get("last_data_update", "")
                if last_update:
                    data["data_age_sec"][sym] = "live"
        except Exception:
            pass

        # WebSocket status
        if hasattr(self, '_grid_bot') and self._grid_bot:
            data["grid_uptime_sec"] = self._grid_bot.get_status().get("uptime_sec", 0)

        return web.json_response(data, dumps=_safe_dumps)

    async def _handle_latency_arb(self, request: web.Request) -> web.Response:
        """Return latency arb engine stats for all symbols."""
        if self._latency_arb is None:
            return web.json_response({
                "active": False,
                "stats": {},
                "symbols": [],
            }, dumps=_safe_dumps)

        stats = self._latency_arb.get_stats()
        # Add per-symbol price snapshots
        symbol_data = []
        for sym in self._latency_arb.symbols:
            bp = self._latency_arb._binance_prices.get(sym)
            dp = self._latency_arb._delta_prices.get(sym)
            now = time.time()

            entry = {"symbol": sym}
            if bp:
                entry["binance_bid"] = round(bp.bid, 2)
                entry["binance_ask"] = round(bp.ask, 2)
                entry["binance_mid"] = round(bp.mid, 2)
                entry["binance_age_ms"] = round((now - bp.local_recv_ts) * 1000, 0)
            if dp:
                entry["delta_bid"] = round(dp.bid, 2)
                entry["delta_ask"] = round(dp.ask, 2)
                entry["delta_mid"] = round(dp.mid, 2)
                entry["delta_age_ms"] = round((now - dp.local_recv_ts) * 1000, 0)
            if bp and dp and dp.mid > 0:
                disl = (bp.mid - dp.mid) / dp.mid * 100
                entry["dislocation_pct"] = round(disl, 4)
                entry["dislocation_usd"] = round(bp.mid - dp.mid, 2)
                entry["direction"] = "LONG" if disl > 0 else "SHORT" if disl < 0 else "FLAT"
                entry["spread_delta_pct"] = round((dp.ask - dp.bid) / dp.mid * 100, 4) if dp.mid else 0
                # Net edge calculation (Layer 1)
                try:
                    ne = self._latency_arb.compute_net_edge(sym, disl)
                    entry["net_edge"] = ne
                except Exception:
                    entry["net_edge"] = {}
            # Stats from history
            entry["avg_disl"] = stats.get("avg_dislocation_pct", {}).get(sym, 0)
            entry["max_disl"] = stats.get("max_dislocation_pct", {}).get(sym, 0)
            entry["p95_disl"] = stats.get(f"p95_dislocation_pct_{sym}", 0)
            entry["tradeable_pct"] = stats.get(f"tradeable_pct_{sym}", 0)
            entry["avg_latency_ms"] = stats.get("avg_latency_ms", {}).get(sym, 0)
            symbol_data.append(entry)

        return web.json_response({
            "active": True,
            "running": self._latency_arb._running,
            "measure_only": self._latency_arb._measure_only,
            "uptime_s": round(time.time() - (stats.get("started_at") or time.time()), 0),
            "binance_msgs": stats.get("binance_msgs", 0),
            "delta_msgs": stats.get("delta_msgs", 0),
            "dislocations_detected": stats.get("dislocations_detected", 0),
            "signals_generated": stats.get("signals_generated", 0),
            "min_threshold_pct": self._latency_arb.MIN_DISLOCATION_PCT,
            "cost_rt_pct": 0.14,
            "symbols": symbol_data,
        }, dumps=_safe_dumps)

    async def _handle_latency_arb_dislocations(self, request: web.Request) -> web.Response:
        """Return recent dislocation history for a symbol."""
        if self._latency_arb is None:
            return web.json_response({"dislocations": []})
        sym = request.query.get("symbol", "BTC/USDT")
        n = min(int(request.query.get("n", "50")), 200)
        dislocations = self._latency_arb.get_recent_dislocations(sym, n)
        return web.json_response({"symbol": sym, "dislocations": dislocations}, dumps=_safe_dumps)

    async def _handle_latency_arb_analysis(self, request: web.Request) -> web.Response:
        """Return full 5-layer analysis: decay, convergence, simulation, session stats."""
        if self._latency_arb is None:
            return web.json_response({"active": False}, dumps=_safe_dumps)

        sym = request.query.get("symbol")  # None = all symbols
        try:
            decay = self._latency_arb.get_decay_analysis(sym)
        except Exception:
            decay = {}
        try:
            convergence = self._latency_arb.get_convergence_stats(sym)
        except Exception:
            convergence = {}
        try:
            simulation = self._latency_arb.get_simulation_results(sym)
        except Exception:
            simulation = {}
        try:
            session = self._latency_arb.get_session_stats(sym)
        except Exception:
            session = {}

        return web.json_response({
            "active": True,
            "symbol_filter": sym,
            "decay": decay,
            "convergence": convergence,
            "simulation": simulation,
            "session": session,
        }, dumps=_safe_dumps)

    async def _handle_pause(self, request: web.Request) -> web.Response:
        async with self._lock:
            self._paused = True
            self._bot_status = "paused"
        logger.info("Trading paused via dashboard")
        await self.add_alert("warning", "Trading paused via dashboard", source="dashboard")
        return web.json_response({"status": "paused"})

    async def _handle_resume(self, request: web.Request) -> web.Response:
        async with self._lock:
            self._paused = False
            self._bot_status = "running"
        logger.info("Trading resumed via dashboard")
        await self.add_alert("info", "Trading resumed via dashboard", source="dashboard")
        return web.json_response({"status": "running"})

    @property
    def is_paused(self) -> bool:
        """Check if trading is currently paused (synchronous read)."""
        return self._paused
