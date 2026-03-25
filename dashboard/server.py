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
    _PUBLIC_PATHS = {"/api/login", "/api/ping", "/favicon.ico"}
    _PUBLIC_PREFIXES = ("/static/",)

    def __init__(self) -> None:
        cfg = get_config()
        dash_cfg = cfg.get("dashboard", {})
        bot_cfg = cfg.get("bot", {})

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

        # Auth config — ALWAYS enabled, generate random password if not set
        self._auth_user = os.getenv("DASHBOARD_USER", "admin")
        self._auth_password = os.getenv("DASHBOARD_PASSWORD", "")
        self._auth_secret = os.getenv("DASHBOARD_SECRET_KEY", secrets.token_hex(32))
        if not self._auth_password:
            self._auth_password = secrets.token_hex(16)
            logger.warning("DASHBOARD_PASSWORD not set — generated random: %s", self._auth_password)
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
            return None
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
    async def _auth_middleware(self, request: web.Request, handler):
        """Middleware that checks auth on every request except public paths."""
        path = request.path
        # Allow public paths
        if path in self._PUBLIC_PATHS or any(path.startswith(p) for p in self._PUBLIC_PREFIXES):
            return await handler(request)
        # Check session
        session = self._verify_session(request)
        if session:
            request["session"] = session
            return await handler(request)
        # Not authenticated
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
        if self._auth_enabled:
            middlewares.append(self._auth_middleware)
            logger.info("Dashboard auth ENABLED (user=%s)", self._auth_user)
        else:
            logger.warning("Dashboard auth DISABLED — set DASHBOARD_PASSWORD in .env to enable")

        self._app = web.Application(middlewares=middlewares)
        self._register_routes(self._app)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        logger.info("Dashboard server started at http://%s:%s", host, port)

    async def stop(self) -> None:
        """Gracefully shut down the web server."""
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

        # Auth endpoints
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
        return web.Response(text=html, content_type="text/html")

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
        return web.json_response(data, dumps=_safe_dumps)

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
            # Auto-sync: close orphaned dry run positions
            try:
                tracker = getattr(self, '_signal_tracker', None)
                if not tracker:
                    orch = getattr(self, '_orchestrator', None)
                    if orch:
                        tracker = getattr(orch, '_signal_tracker', None)
                if tracker and hasattr(mgr, 'sync_with_paper'):
                    active_ids = set()
                    for sig in tracker.get_active_signals():
                        tid = sig.get("trade_id", "") if isinstance(sig, dict) else getattr(sig, "trade_id", "")
                        if tid:
                            active_ids.add(tid)
                    mgr.sync_with_paper(active_ids)
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
            mgr.dry_run = bool(dry_run)
            logger.warning("REAL TRADING dry_run=%s via dashboard", mgr.dry_run)

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

    async def _handle_ping(self, request: web.Request) -> web.Response:
        """Ultra-fast ping for client-side latency measurement."""
        return web.json_response({"t": time.time() * 1000})

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
