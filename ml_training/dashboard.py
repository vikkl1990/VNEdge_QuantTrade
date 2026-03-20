"""
ML Training Dashboard
=====================
Web dashboard for monitoring ML training progress,
viewing backtest results, and visualizing patterns.

Runs on VM2 port 8081.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import numpy as np
from aiohttp import web


class _NumpyEncoder(json.JSONEncoder):
    """Handle numpy types in JSON serialization."""
    def default(self, obj):
        if isinstance(obj, (np.bool_, np.integer)):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR = PROJECT_ROOT / "storage" / "backtest_results"
STATUS_FILE = PROJECT_ROOT / "storage" / "ml_training_status.json"


class MLDashboard:
    """aiohttp dashboard for ML training visualization."""

    def __init__(self, trainer=None, port: int = 8081):
        self._trainer = trainer
        self._port = port
        self._app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/api/status", self._handle_status)
        self._app.router.add_get("/api/results", self._handle_results)
        self._app.router.add_get("/api/scanner/{scanner}", self._handle_scanner_detail)
        self._app.router.add_get("/api/comparison", self._handle_comparison)
        self._app.router.add_get("/api/models", self._handle_models)
        self._app.router.add_get("/api/features", self._handle_features)
        self._app.router.add_get("/api/collector", self._handle_collector)
        self._app.router.add_post("/api/train/start", self._handle_start_training)
        self._app.router.add_get("/api/history", self._handle_history)
        # Serve static files (logo) from main dashboard's static dir
        static_dir = PROJECT_ROOT / "dashboard" / "static"
        if static_dir.exists():
            self._app.router.add_static("/static", static_dir)

    async def _handle_index(self, request):
        template_path = TEMPLATE_DIR / "ml_dashboard.html"
        if template_path.exists():
            return web.FileResponse(template_path)
        return web.Response(text="ML Dashboard - template not found", status=404)

    async def _handle_status(self, request):
        if self._trainer:
            return web.json_response(self._trainer.get_status(), dumps=lambda o: json.dumps(o, cls=_NumpyEncoder, default=str))

        # Load from file
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                return web.json_response(data)
            except Exception:
                pass
        return web.json_response({"phase": "idle", "results": {}})

    async def _handle_results(self, request):
        results = {}
        if RESULTS_DIR.exists():
            for f in RESULTS_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                    results[f.stem] = {
                        "scanner": data.get("scanner"),
                        "symbol": data.get("symbol"),
                        "total_trades": data.get("total_trades"),
                        "metrics": data.get("metrics"),
                    }
                except Exception:
                    pass
        return web.json_response(results)

    async def _handle_scanner_detail(self, request):
        scanner = request.match_info["scanner"]
        results = {}
        if RESULTS_DIR.exists():
            for f in RESULTS_DIR.glob(f"{scanner}*.json"):
                try:
                    data = json.loads(f.read_text())
                    results[f.stem] = data
                except Exception:
                    pass
        return web.json_response(results)

    async def _handle_comparison(self, request):
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                return web.json_response(data.get("results", {}).get("comparison", {}))
            except Exception:
                pass
        return web.json_response({})

    async def _handle_models(self, request):
        if self._trainer:
            models = {k: m.get_status() for k, m in self._trainer._models.items()}
            return web.json_response(models, dumps=lambda o: json.dumps(o, cls=_NumpyEncoder, default=str))

        # Load from disk
        models_dir = PROJECT_ROOT / "storage" / "ml_models"
        result = {}
        if models_dir.exists():
            for f in models_dir.glob("*_meta.json"):
                try:
                    data = json.loads(f.read_text())
                    result[f.stem.replace("_meta", "")] = data
                except Exception:
                    pass
        return web.json_response(result)

    async def _handle_features(self, request):
        """Return feature importances from trained models."""
        models_dir = PROJECT_ROOT / "storage" / "ml_models"
        result = {}
        if models_dir.exists():
            for f in models_dir.glob("*_meta.json"):
                try:
                    data = json.loads(f.read_text())
                    result[f.stem.replace("_meta", "")] = {
                        "importances": data.get("feature_importances", {}),
                        "metrics": data.get("training_metrics", {}),
                    }
                except Exception:
                    pass
        return web.json_response(result)

    async def _handle_collector(self, request):
        if self._trainer:
            return web.json_response(self._trainer._collector.get_progress())
        return web.json_response({})

    async def _handle_start_training(self, request):
        if not self._trainer:
            return web.json_response({"error": "no trainer configured"}, status=400)

        body = await request.json() if request.content_length else {}
        symbols = body.get("symbols", ["BTC/USDT", "ETH/USDT", "AVAX/USDT"])
        timeframes = body.get("timeframes", ["1m", "5m", "15m"])

        # Start training in background
        asyncio.create_task(self._trainer.run_full_pipeline(symbols, timeframes))
        return web.json_response({"status": "started", "symbols": symbols, "timeframes": timeframes})

    async def _handle_history(self, request):
        """Return backtest run history for comparison."""
        from ml_training.backtest_tracker import BacktestTracker
        tracker = BacktestTracker()
        return web.json_response({
            "runs": tracker.get_comparison(last_n=20),
            "trend": tracker.get_improvement_trend(),
            "best_run": tracker.get_best_run(),
        }, dumps=lambda o: json.dumps(o, cls=_NumpyEncoder, default=str))

    async def start(self):
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._port)
        await site.start()
        logger.info("ML Dashboard running on http://0.0.0.0:%d", self._port)
