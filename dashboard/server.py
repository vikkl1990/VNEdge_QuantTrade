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
    _PUBLIC_PATHS = {
        "/api/login", "/api/ping", "/favicon.ico",
        "/api/real/status", "/api/real/trades",
        "/api/risk-metrics", "/api/session-heatmap",
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
        self._auth_secret = os.getenv("DASHBOARD_SECRET_KEY", secrets.token_hex(32))
        if not self._auth_password:
            self._auth_password = secrets.token_hex(16)
            logger.warning("DASHBOARD_PASSWORD not set — generated random password (check .env to set a permanent one)")
        self._auth_enabled = True  # always enabled
        self._sessions: Dict[str, Dict[str, Any]] = {}  # token -> session data
        self._session_history: List[Dict[str, Any]] = []  # login history
        self._session_timeout = 86400  # 24 hours
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
            return {"user": "admin", "auth_disabled": True}
        cookie = request.cookies.get("vn_session")
        if not cookie:
            logger.info("SESSION: no vn_session cookie found")
            return None
        logger.debug("SESSION: cookie=%s... auth_service=%s has_colon=%s", cookie[:8], bool(self._auth_service), ":" in cookie)
        # Multi-user auth: cookie is a plain JWT/token without ":" separator
        if self._auth_service and ":" not in cookie:
            # Multi-user login already validated credentials and set this cookie
            # Trust it for POST operations (the token was issued by our login handler)
            return {"user": "admin", "role": "admin", "multi_user": True}

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

        # Allow ALL GET/HEAD requests (read-only dashboard data)
        if method in ("GET", "HEAD"):
            session = self._verify_session(request)
            if session:
                request["session"] = session
                # Set user dict for multi-user session handler
                if session.get("multi_user") and "user" not in request:
                    request["user"] = {"email": "admin@vnedge.com", "role": "admin", "tier": "enterprise", "full_name": "VN Edge Admin", "user_id": 1}
            return await handler(request)

        # POST requests: require auth (state-changing operations)
        # But allow login/logout without auth (they ARE the auth)
        if path in ("/api/login", "/api/logout", "/api/register"):
            return await handler(request)
        session = self._verify_session(request)
        if session:
            request["session"] = session
            return await handler(request)

        # Not authenticated for POST
        if path.startswith("/api/"):
            return web.json_response({"error": "unauthorized"}, status=401)

        # For page requests, serve the index (login form will show)
        return await handler(request)

    async def _handle_login(self, request: web.Request) -> web.Response:
        """POST /api/login — validate credentials, set session cookie."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        user = body.get("username", "")
        password = body.get("password", "")
        logger.info("LOGIN DEBUG: received user=[%s] pwd_len=%d, expected user=[%s] pwd_len=%d, match_user=%s match_pwd=%s",
                   user, len(password), self._auth_user, len(self._auth_password),
                   user == self._auth_user, password == self._auth_password)
        if user != self._auth_user or password != self._auth_password:
            logger.warning("Failed login attempt from %s (user=%s)", request.remote, user)
            self._session_history.append({
                "user": user, "ip": request.remote,
                "time": datetime.now(IST).isoformat(),
                "success": False,
            })
            return web.json_response({"error": "invalid credentials"}, status=401)
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
        session = self._verify_session(request)
        if not session:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({
            "user": session.get("user", "admin"),
            "login_time": session.get("login_time", 0),
            "last_activity": session.get("last_activity", 0),
            "requests": session.get("requests", 0),
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
            register_user_routes(self._app, self._auth_service, self._db_pool)
            register_admin_routes(self._app, self._auth_service, self._db_pool)
            logger.info("Multi-user routes registered (user profile, API keys, admin)")

            # Per-user real trading routes
            orch = getattr(self, '_orchestrator', None)
            user_registry = getattr(orch, '_user_registry', None) if orch else None
            if user_registry:
                from dashboard.user_trading_routes import register_user_trading_routes
                register_user_trading_routes(self._app, user_registry, self._db_pool)
                logger.info("Per-user trading routes registered (8 endpoints)")

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

        # JSON API
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/positions", self._handle_positions)
        app.router.add_get("/api/signals", self._handle_signals)
        app.router.add_get("/api/trades", self._handle_trades)
        app.router.add_get("/api/performance", self._handle_performance)
        app.router.add_get("/api/alerts", self._handle_alerts)

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
        app.router.add_get("/api/latency", self._handle_latency)
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
        if True:  # always reload template (cache was serving stale login form)
            self._idx_cache = index_path.read_text(encoding="utf-8")
        return web.Response(
            text=self._idx_cache, content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
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
        """Return current market regime and position sizing info."""
        data = {"regime": "unknown", "action": {}, "early_exit_stats": {}}
        if self._strategy and hasattr(self._strategy, '_scalp'):
            scalp = self._strategy._scalp
            if hasattr(scalp, '_last_regime_info'):
                data = scalp._last_regime_info
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
        """Return real trading manager status for dashboard."""
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
            return web.json_response(mgr.get_status())
        return web.json_response({
            "enabled": False,
            "dry_run": True,
            "mode": "DISABLED",
            "balance": 0,
            "circuit_breaker": {"daily_pnl": 0, "is_tripped": False},
            "open_positions": [],
            "open_count": 0,
            "closed_today": 0,
            "total_closed": 0,
            "recent_trades": [],
        })

    async def _handle_real_toggle(self, request: web.Request) -> web.Response:
        """Toggle real trading on/off from dashboard."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = body.get("enabled")
        dry_run = body.get("dry_run")

        orch = getattr(self, '_orchestrator', None)
        mgr = None
        if hasattr(self, '_real_manager') and self._real_manager:
            mgr = self._real_manager
        elif orch and hasattr(orch, '_real_manager') and orch._real_manager:
            mgr = orch._real_manager

        if not mgr:
            return web.json_response({"error": "Real trading manager not initialized"}, status=400)

        if enabled is not None:
            mgr.enabled = bool(enabled)
            logger.warning("REAL TRADING %s via dashboard", "ENABLED" if mgr.enabled else "DISABLED")
        if dry_run is not None:
            old_dry_run = mgr.dry_run
            mgr.dry_run = bool(dry_run)
            logger.warning("REAL TRADING dry_run=%s via dashboard", mgr.dry_run)
            # Reset circuit breaker when switching modes (dry→live or live→dry)
            if old_dry_run != mgr.dry_run:
                mgr.circuit_breaker.daily_pnl = 0
                mgr.circuit_breaker.total_pnl = 0
                mgr.circuit_breaker.consecutive_losses = 0
                mgr.circuit_breaker.is_tripped = False
                mgr.circuit_breaker.trip_reason = ""
                mgr.circuit_breaker.trade_count_today = 0
                logger.warning("REAL TRADING: Circuit breaker RESET on mode switch (%s → %s)",
                             "dry_run" if old_dry_run else "live",
                             "dry_run" if mgr.dry_run else "live")

        mgr._save_state()
        return web.json_response(mgr.get_status())

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

            # Regime from strategy
            regime_info = {}
            try:
                regime_info = getattr(scalp, '_last_regime_info', {}) or {}
            except Exception:
                pass

            regime = str(regime_info.get("regime", "unknown")).lower()
            regime_conf = float(regime_info.get("confidence", 0) or 0)

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

            # Regime transition detection (compare recent regime vs historical)
            transitions = []
            try:
                orch = getattr(self, '_orchestrator', None)
                strategy = getattr(orch, '_strategy', None) if orch else None
                scalp = getattr(strategy, '_scalp', strategy) if strategy else None
                regime_info = getattr(scalp, '_last_regime_info', {}) or {}
                current_regime = str(regime_info.get("regime", "unknown")).lower()
                # Simple transition: flag if regime changed recently
                transitions.append({
                    "type": "regime",
                    "current": current_regime,
                    "signal": "stable" if regime_info.get("confidence", 0) > 0.7 else "transitioning",
                    "confidence": round(float(regime_info.get("confidence", 0) or 0), 2),
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
