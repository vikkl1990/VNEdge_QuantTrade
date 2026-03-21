"""Async web dashboard server for the crypto trading bot.

Uses aiohttp to serve a single-page dashboard with REST API endpoints
for bot status, positions, signals, trade history, performance, and alerts.
"""

import asyncio
import json
import logging
import os
import platform
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

    def _persist_signals(self) -> None:
        """Save current signals to disk (call inside lock)."""
        try:
            self._SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._SIGNALS_FILE.write_text(
                json.dumps(self._signals[:self._MAX_PERSISTED], cls=_SafeEncoder, indent=1),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to persist signals: %s", exc)

    async def add_signal(self, signal: Dict[str, Any]) -> None:
        """Push a new signal to the front of the signals list and persist."""
        async with self._lock:
            self._signals.insert(0, signal)
            self._signals = self._signals[:self._MAX_PERSISTED]
            self._persist_signals()

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

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Create the aiohttp application, bind, and start serving."""
        self._started_at = time.time()
        self._bot_status = "running"

        self._app = web.Application()
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
        app.router.add_get("/api/ping", self._handle_ping)
        app.router.add_get("/api/latency", self._handle_latency)
        app.router.add_get("/api/latency-arb", self._handle_latency_arb)
        app.router.add_get("/api/latency-arb/dislocations", self._handle_latency_arb_dislocations)

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
