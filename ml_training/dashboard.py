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
            if np.isnan(obj) or np.isinf(obj):
                return None
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def _sanitize_json(obj):
    """Recursively replace NaN/Infinity with None for JS-safe JSON."""
    if isinstance(obj, float):
        if obj != obj or obj == float('inf') or obj == float('-inf'):  # NaN check
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    return obj

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
        self._loaded_models = {}
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
        self._app.router.add_get("/api/calibration", self._handle_calibration)
        self._app.router.add_get("/api/candidates", self._handle_candidates)
        self._app.router.add_post("/api/score", self._handle_score)
        self._app.router.add_get("/api/health", self._handle_health)
        self._app.router.add_get("/api/live-feedback", self._handle_live_feedback)
        self._app.router.add_get("/api/validation", self._handle_validation)
        self._app.router.add_post("/api/validation/run", self._handle_run_validation)
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

        # Load from file — trim heavy backtest data for dashboard performance
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                # Trim backtest results: keep only summary metrics, drop walk_forward windows
                bt = data.get("results", {}).get("backtest", {})
                if bt:
                    trimmed_bt = {}
                    for key, val in bt.items():
                        trimmed_bt[key] = {
                            "scanner": val.get("scanner"),
                            "symbol": val.get("symbol"),
                            "timeframe": val.get("timeframe"),
                            "metrics": val.get("metrics"),
                            "walk_forward": {
                                "verdict": val.get("walk_forward", {}).get("verdict"),
                                "edge_holds_pct": val.get("walk_forward", {}).get("edge_holds_pct"),
                                "windows": val.get("walk_forward", {}).get("windows", []),
                            } if val.get("walk_forward") else {},
                        }
                    data["results"]["backtest"] = trimmed_bt
                return web.json_response(_sanitize_json(data), dumps=lambda o: json.dumps(o, cls=_NumpyEncoder, default=str))
            except Exception:
                pass
        return web.json_response({"phase": "idle", "results": {}})

    async def _handle_results(self, request):
        results = {}
        if RESULTS_DIR.exists():
            for f in RESULTS_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                    symbol_raw = data.get("symbol", "")
                    # BTC/USDT_1m -> symbol=BTC/USDT, timeframe=1m
                    parts = symbol_raw.rsplit("_", 1)
                    symbol = parts[0] if len(parts) > 1 else symbol_raw
                    timeframe = parts[1] if len(parts) > 1 else "?"
                    results[f.stem] = {
                        "scanner": data.get("scanner"),
                        "symbol": symbol,
                        "timeframe": timeframe,
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
            # Read legacy _meta.json files
            for f in models_dir.glob("*_meta.json"):
                try:
                    data = json.loads(f.read_text())
                    result[f.stem.replace("_meta", "")] = data
                except Exception:
                    pass
            # Read candidate model files
            for f in models_dir.glob("candidate_*.json"):
                if f.stem == "candidate_all_scanners":
                    continue  # skip comparison file
                try:
                    data = json.loads(f.read_text())
                    scanner = data.get("scanner", f.stem.replace("candidate_", ""))
                    # Extract average metrics from folds
                    folds = data.get("training", {}).get("folds", [])
                    if folds:
                        avg_auc = sum(fold.get("auc_roc", 0) for fold in folds) / len(folds)
                        avg_acc = sum(fold.get("accuracy", 0) for fold in folds) / len(folds)
                        avg_prec = sum(fold.get("precision", 0) for fold in folds) / len(folds)
                        avg_recall = sum(fold.get("recall", 0) for fold in folds) / len(folds)
                    else:
                        avg_auc = avg_acc = avg_prec = avg_recall = 0

                    result[f"candidate_{scanner}"] = {
                        "scanner": scanner,
                        "symbol": data.get("symbol", "?"),
                        "label_mode": data.get("label_mode", "?"),
                        "mfe_threshold_r": data.get("mfe_threshold_r"),
                        "training_metrics": {
                            "samples": data.get("training", {}).get("total_candidates", 0),
                            "accuracy": avg_acc,
                            "auc_roc": avg_auc,
                            "precision": avg_prec,
                            "recall": avg_recall,
                            "positive_rate": data.get("training", {}).get("base_win_rate", 0),
                            "n_folds": len(folds),
                        },
                        "feature_importances": data.get("feature_importances", {}),
                        "probability_calibration": data.get("probability_calibration", {}),
                        "win_rate_comparison": data.get("win_rate_comparison", {}),
                    }
                except Exception:
                    pass
        return web.json_response(result, dumps=lambda o: json.dumps(o, cls=_NumpyEncoder, default=str))

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
            # Also read candidate files
            for f in models_dir.glob("candidate_*.json"):
                if f.stem == "candidate_all_scanners":
                    continue
                try:
                    data = json.loads(f.read_text())
                    scanner = data.get("scanner", f.stem.replace("candidate_", ""))
                    result[f"candidate_{scanner}"] = {
                        "importances": data.get("feature_importances", {}),
                        "metrics": {
                            "symbol": data.get("symbol"),
                            "scanner": scanner,
                            "label_mode": data.get("label_mode"),
                        },
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

    async def _handle_calibration(self, request):
        """Return probability calibration data from candidate models."""
        models_dir = PROJECT_ROOT / "storage" / "ml_models"
        result = {}
        if models_dir.exists():
            for f in models_dir.glob("candidate_*.json"):
                if f.stem == "candidate_all_scanners":
                    continue
                try:
                    data = json.loads(f.read_text())
                    scanner = data.get("scanner", f.stem.replace("candidate_", ""))
                    cal = data.get("probability_calibration", {})
                    result[scanner] = {
                        "symbol": data.get("symbol"),
                        "label_mode": data.get("label_mode"),
                        "buckets": cal.get("buckets", []),
                        "is_monotonic": cal.get("is_monotonic", False),
                        "rank_correlation": cal.get("rank_correlation", 0),
                        "top_bottom_spread": cal.get("top_bottom_spread", 0),
                        "verdict": cal.get("verdict", "?"),
                    }
                except Exception:
                    pass
            # Also add all_scanners comparison
            all_file = models_dir / "candidate_all_scanners.json"
            if all_file.exists():
                try:
                    data = json.loads(all_file.read_text())
                    result["_comparison"] = data.get("comparison", [])
                except Exception:
                    pass
        return web.json_response(result, dumps=lambda o: json.dumps(o, cls=_NumpyEncoder, default=str))

    async def _handle_candidates(self, request):
        """Return per-symbol candidate training results."""
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                ct = data.get('results', {}).get('candidate_training', {})
                return web.json_response(_sanitize_json(ct), dumps=lambda o: json.dumps(o, cls=_NumpyEncoder, default=str))
            except Exception:
                pass
        return web.json_response({})

    async def _handle_score(self, request):
        """Score a candidate using trained ML model.

        POST body: {"scanner": "...", "symbol": "...", "side": "...", "features": {...}}
        Returns full metadata for auditability.
        """
        import pandas as pd
        from ml_training.candidate_trainer import CandidateTrainer

        try:
            body = await request.json()
            scanner = body.get("scanner", "")
            symbol = body.get("symbol", "?")
            side = body.get("side", "?")
            features = body.get("features", {})

            if not scanner or not features:
                return web.json_response({"error": "missing scanner or features"}, status=400)

            # Load model with caching
            if scanner not in self._loaded_models:
                model, feature_names = CandidateTrainer.load_model(scanner)
                if model is None:
                    return web.json_response({
                        "probability": 0.5, "scanner": scanner,
                        "verdict": "NO_MODEL", "error": f"no model for {scanner}",
                        "rank_bucket": "NONE", "model_version": "none",
                    })
                # Load model metadata
                meta_path = Path(__file__).resolve().parent.parent / "storage" / "ml_models" / f"model_{scanner}_features.json"
                meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
                self._loaded_models[scanner] = (model, feature_names, meta)
                logger.info("Loaded model for %s (%d features)", scanner, len(feature_names))

            model, feature_names, meta = self._loaded_models[scanner]

            # Build DataFrame with aligned columns
            row_df = pd.DataFrame([features])
            for col in feature_names:
                if col not in row_df.columns:
                    row_df[col] = 0.0
            row_df = row_df[feature_names].fillna(0)

            prob = float(model.predict_proba(row_df)[0, 1])

            # Rank bucket
            if prob >= 0.70:
                rank_bucket = "D90"
            elif prob >= 0.60:
                rank_bucket = "Q75"
            elif prob >= 0.50:
                rank_bucket = "Q50"
            elif prob >= 0.40:
                rank_bucket = "Q25"
            else:
                rank_bucket = "BOTTOM"

            # Verdict
            if prob >= 0.60:
                verdict = "STRONG_TAKE"
            elif prob >= 0.50:
                verdict = "TAKE"
            elif prob >= 0.40:
                verdict = "WEAK"
            else:
                verdict = "SKIP"

            return web.json_response({
                "probability": round(prob, 4),
                "scanner": scanner,
                "symbol": symbol,
                "side": side,
                "verdict": verdict,
                "rank_bucket": rank_bucket,
                "model_version": meta.get("trained_at", "unknown"),
                "feature_set_version": f"fs_v1_{len(feature_names)}feat",
                "label_type": "tp_sl_1.5R_1.0R",
                "calibrated": False,
                "features_matched": len([f for f in feature_names if f in features]),
                "features_expected": len(feature_names),
            })
        except Exception as e:
            logger.exception("Score error: %s", e)
            return web.json_response({
                "probability": 0.5, "scanner": body.get("scanner", ""),
                "verdict": "ERROR", "error": str(e),
                "rank_bucket": "ERROR", "model_version": "error",
            })

    async def _handle_health(self, request):
        """Health check endpoint for VM1 to ping."""
        import psutil
        models_dir = PROJECT_ROOT / "storage" / "ml_models"
        loaded = list(self._loaded_models.keys())
        available = [f.stem.replace("model_", "").replace("_features", "")
                     for f in models_dir.glob("model_*_features.json")] if models_dir.exists() else []

        # Model versions
        versions = {}
        for f in models_dir.glob("model_*_features.json"):
            try:
                meta = json.loads(f.read_text())
                name = meta.get("scanner", f.stem)
                versions[name] = {
                    "trained_at": meta.get("trained_at"),
                    "n_features": meta.get("n_features"),
                }
            except Exception:
                pass

        # Memory
        mem = psutil.virtual_memory()

        return web.json_response({
            "status": "healthy",
            "server": "vm2-ml",
            "models_loaded": loaded,
            "models_available": available,
            "model_versions": versions,
            "memory_used_mb": round(mem.used / 1024 / 1024),
            "memory_total_mb": round(mem.total / 1024 / 1024),
            "memory_pct": mem.percent,
        })

    async def _handle_live_feedback(self, request):
        """Live feedback: per-pair, per-scanner trade outcomes from live bot."""
        feedback_file = PROJECT_ROOT / "storage" / "ml_live_feedback.jsonl"
        if not feedback_file.exists():
            return web.json_response({
                "total_trades": 0,
                "by_pair": {},
                "by_scanner": {},
                "by_trade_type": {},
                "by_pair_scanner": {},
                "recent": [],
            })

        try:
            records = []
            with open(feedback_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

            if not records:
                return web.json_response({"total_trades": 0, "by_pair": {},
                    "by_scanner": {}, "by_trade_type": {}, "by_pair_scanner": {}, "recent": []})

            # Aggregate by pair
            by_pair = {}
            for r in records:
                pair = r.get("symbol", "?")
                if pair not in by_pair:
                    by_pair[pair] = {"trades": 0, "wins": 0, "pnl_usd": 0.0, "pnl_pct_sum": 0.0,
                                    "r_sum": 0.0, "mfe_sum": 0.0, "mae_sum": 0.0, "fees_usd": 0.0}
                bp = by_pair[pair]
                bp["trades"] += 1
                if r.get("pnl_pct", 0) > 0:
                    bp["wins"] += 1
                bp["pnl_usd"] += r.get("pnl_usd", 0)
                bp["pnl_pct_sum"] += r.get("pnl_pct", 0)
                bp["r_sum"] += r.get("exit_r", 0)
                bp["mfe_sum"] += r.get("mfe_r", 0)
                bp["mae_sum"] += r.get("mae_r", 0)
                bp["fees_usd"] += r.get("total_fees_usd", 0)

            for k, v in by_pair.items():
                n = v["trades"]
                v["win_rate"] = round(v["wins"] / n * 100, 1) if n > 0 else 0
                v["avg_r"] = round(v["r_sum"] / n, 3) if n > 0 else 0
                v["avg_mfe"] = round(v["mfe_sum"] / n, 3) if n > 0 else 0
                v["avg_mae"] = round(v["mae_sum"] / n, 3) if n > 0 else 0
                v["pnl_usd"] = round(v["pnl_usd"], 2)
                v["fees_usd"] = round(v["fees_usd"], 2)

            # Aggregate by scanner
            by_scanner = {}
            for r in records:
                scanner = r.get("setup_type", "?")
                if scanner not in by_scanner:
                    by_scanner[scanner] = {"trades": 0, "wins": 0, "pnl_usd": 0.0, "r_sum": 0.0}
                bs = by_scanner[scanner]
                bs["trades"] += 1
                if r.get("pnl_pct", 0) > 0:
                    bs["wins"] += 1
                bs["pnl_usd"] += r.get("pnl_usd", 0)
                bs["r_sum"] += r.get("exit_r", 0)
            for k, v in by_scanner.items():
                n = v["trades"]
                v["win_rate"] = round(v["wins"] / n * 100, 1) if n > 0 else 0
                v["avg_r"] = round(v["r_sum"] / n, 3) if n > 0 else 0
                v["pnl_usd"] = round(v["pnl_usd"], 2)

            # Aggregate by trade type
            by_trade_type = {}
            for r in records:
                tt = r.get("trade_type", "?")
                if tt not in by_trade_type:
                    by_trade_type[tt] = {"trades": 0, "wins": 0, "pnl_usd": 0.0, "r_sum": 0.0,
                                         "avg_duration_min": 0, "dur_sum": 0}
                bt = by_trade_type[tt]
                bt["trades"] += 1
                if r.get("pnl_pct", 0) > 0:
                    bt["wins"] += 1
                bt["pnl_usd"] += r.get("pnl_usd", 0)
                bt["r_sum"] += r.get("exit_r", 0)
                bt["dur_sum"] += r.get("duration_sec", 0)
            for k, v in by_trade_type.items():
                n = v["trades"]
                v["win_rate"] = round(v["wins"] / n * 100, 1) if n > 0 else 0
                v["avg_r"] = round(v["r_sum"] / n, 3) if n > 0 else 0
                v["avg_duration_min"] = round(v["dur_sum"] / n / 60, 1) if n > 0 else 0
                v["pnl_usd"] = round(v["pnl_usd"], 2)
                del v["dur_sum"]

            # Aggregate by pair × scanner (cross matrix)
            by_pair_scanner = {}
            for r in records:
                key = f"{r.get('symbol', '?')}|{r.get('setup_type', '?')}"
                if key not in by_pair_scanner:
                    by_pair_scanner[key] = {"symbol": r.get("symbol", "?"),
                        "scanner": r.get("setup_type", "?"),
                        "trades": 0, "wins": 0, "pnl_usd": 0.0, "r_sum": 0.0,
                        "ml_prob_sum": 0.0}
                bps = by_pair_scanner[key]
                bps["trades"] += 1
                if r.get("pnl_pct", 0) > 0:
                    bps["wins"] += 1
                bps["pnl_usd"] += r.get("pnl_usd", 0)
                bps["r_sum"] += r.get("exit_r", 0)
                bps["ml_prob_sum"] += r.get("ml_probability", 0)
            for k, v in by_pair_scanner.items():
                n = v["trades"]
                v["win_rate"] = round(v["wins"] / n * 100, 1) if n > 0 else 0
                v["avg_r"] = round(v["r_sum"] / n, 3) if n > 0 else 0
                v["avg_ml_prob"] = round(v["ml_prob_sum"] / n, 3) if n > 0 else 0
                v["pnl_usd"] = round(v["pnl_usd"], 2)

            # ML model accuracy: compare ML prediction vs actual outcome
            ml_accuracy = {"total": 0, "ml_correct": 0, "ml_wrong": 0,
                          "by_verdict": {}}
            for r in records:
                prob = r.get("ml_probability", 0)
                verdict = r.get("ml_verdict", "")
                won = r.get("pnl_pct", 0) > 0
                if prob > 0:
                    ml_accuracy["total"] += 1
                    predicted_win = prob >= 0.50
                    if predicted_win == won:
                        ml_accuracy["ml_correct"] += 1
                    else:
                        ml_accuracy["ml_wrong"] += 1
                    if verdict not in ml_accuracy["by_verdict"]:
                        ml_accuracy["by_verdict"][verdict] = {"n": 0, "wins": 0, "pnl_sum": 0}
                    ml_accuracy["by_verdict"][verdict]["n"] += 1
                    if won:
                        ml_accuracy["by_verdict"][verdict]["wins"] += 1
                    ml_accuracy["by_verdict"][verdict]["pnl_sum"] += r.get("pnl_usd", 0)

            if ml_accuracy["total"] > 0:
                ml_accuracy["accuracy_pct"] = round(
                    ml_accuracy["ml_correct"] / ml_accuracy["total"] * 100, 1)
            for k, v in ml_accuracy.get("by_verdict", {}).items():
                if v["n"] > 0:
                    v["win_rate"] = round(v["wins"] / v["n"] * 100, 1)
                    v["pnl_sum"] = round(v["pnl_sum"], 2)

            result = _sanitize_json({
                "total_trades": len(records),
                "by_pair": by_pair,
                "by_scanner": by_scanner,
                "by_trade_type": by_trade_type,
                "by_pair_scanner": list(by_pair_scanner.values()),
                "ml_accuracy": ml_accuracy,
                "recent": records[-20:][::-1],  # last 20, newest first
            })
            return web.json_response(result, dumps=lambda x: json.dumps(x, cls=_NumpyEncoder))
        except Exception as e:
            logger.error("Live feedback error: %s", e)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_validation(self, request):
        """Return latest AUC validation results from saved file."""
        validation_file = PROJECT_ROOT / "storage" / "ml_validation_results.json"
        if validation_file.exists():
            try:
                data = json.loads(validation_file.read_text())
                return web.json_response(
                    _sanitize_json(data),
                    dumps=lambda o: json.dumps(o, cls=_NumpyEncoder, default=str),
                )
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"error": "No validation results yet. Run: python -m ml_training.validate_auc"})

    async def _handle_run_validation(self, request):
        """Trigger AUC validation in background."""
        from ml_training.validate_auc import run_validation

        body = await request.json() if request.content_length else {}
        symbols = body.get("symbols")

        async def _run():
            try:
                await run_validation(symbols=symbols, do_fetch=False)
            except Exception as e:
                logger.error("Validation run failed: %s", e)

        asyncio.create_task(_run())
        return web.json_response({"status": "started", "symbols": symbols or "all"})

    async def start(self):
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._port)
        await site.start()
        logger.info("ML Dashboard running on http://0.0.0.0:%d", self._port)
