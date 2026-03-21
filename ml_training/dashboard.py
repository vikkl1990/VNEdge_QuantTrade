"""
ML Training Dashboard
=====================
Web dashboard for monitoring ML training progress,
viewing backtest results, and visualizing patterns.

Runs on VM2 port 8081.

Fixes & Enhancements (v3.0):
- P0: In-memory JSONL feedback cache (no full-file re-read)
- P0: Model cache invalidation via mtime check
- P0: Error states on all API responses
- P1: Per-pair ML accuracy breakdown
- P1: Model performance trend tracking
- P1: Data sufficiency warnings
- P2: AUC drift monitoring + model health
- P2: Feature importance drift detection
- P2: Inter-scanner ranking trend
- P3: Session-aware performance
- Source freshness metadata on every response
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
        if obj != obj or obj == float('inf') or obj == float('-inf'):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    return obj


def _json_dumps(obj):
    """Standard JSON serializer for all responses."""
    return json.dumps(obj, cls=_NumpyEncoder, default=str)


def _freshness(data_through: str = None, model_version: str = None,
               record_count: int = None, extra: Dict = None) -> Dict:
    """Build source freshness metadata for every API response."""
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if data_through is not None:
        meta["data_through"] = data_through
    if model_version is not None:
        meta["model_version"] = model_version
    if record_count is not None:
        meta["feedback_records_count"] = record_count
    if extra:
        meta.update(extra)
    return meta


def _error_response(endpoint: str, error: str, status: int = 500) -> web.Response:
    """Consistent error response across all endpoints."""
    return web.json_response({
        "error": error,
        "endpoint": endpoint,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, status=status, dumps=_json_dumps)


def _utc_session(hour: int) -> str:
    """Map UTC hour to trading session name."""
    if 0 <= hour < 8:
        return "asia"
    elif 8 <= hour < 14:
        return "europe"
    elif 14 <= hour < 21:
        return "us"
    else:
        return "late_us"


# Minimum samples for reliable ML training
MIN_SAMPLES_WARN = 200
MIN_SAMPLES_BLOCK = 50


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR = PROJECT_ROOT / "storage" / "backtest_results"
STATUS_FILE = PROJECT_ROOT / "storage" / "ml_training_status.json"
FEEDBACK_FILE = PROJECT_ROOT / "storage" / "ml_live_feedback.jsonl"
MODEL_HISTORY_FILE = PROJECT_ROOT / "storage" / "ml_model_history.jsonl"
FEATURE_HISTORY_FILE = PROJECT_ROOT / "storage" / "ml_feature_history.jsonl"
MODELS_DIR = PROJECT_ROOT / "storage" / "ml_models"


# ---------------------------------------------------------------------------
#  P0: In-memory JSONL feedback cache
# ---------------------------------------------------------------------------
class _FeedbackCache:
    """Incrementally reads ml_live_feedback.jsonl and maintains running aggregates.

    Instead of re-reading the full file on every request (O(n) per call),
    this tracks the file offset and only reads new lines appended since last check.
    Aggregates are maintained incrementally.
    """

    def __init__(self, filepath: Path):
        self._filepath = filepath
        self._offset = 0  # byte offset into file
        self._records: List[Dict] = []
        # Running aggregates
        self._by_pair: Dict[str, Dict] = {}
        self._by_scanner: Dict[str, Dict] = {}
        self._by_trade_type: Dict[str, Dict] = {}
        self._by_pair_scanner: Dict[str, Dict] = {}
        self._by_session: Dict[str, Dict] = {}
        self._ml_accuracy: Dict[str, Any] = {
            "total": 0, "ml_correct": 0, "ml_wrong": 0,
            "by_verdict": {},
        }
        self._ml_accuracy_by_pair: Dict[str, Dict] = {}
        self._last_check = 0.0

    def refresh(self) -> int:
        """Read any new lines appended since last check. Returns count of new records."""
        if not self._filepath.exists():
            return 0

        now = time.time()
        # Don't check more than once per second
        if now - self._last_check < 1.0:
            return 0
        self._last_check = now

        try:
            file_size = self._filepath.stat().st_size
            if file_size <= self._offset:
                return 0  # No new data

            new_count = 0
            with open(self._filepath, 'r') as f:
                f.seek(self._offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        self._records.append(record)
                        self._ingest_record(record)
                        new_count += 1
                    except json.JSONDecodeError:
                        continue
                self._offset = f.tell()
            return new_count
        except Exception as e:
            logger.error("FeedbackCache refresh error: %s", e)
            return 0

    def _ingest_record(self, r: Dict):
        """Update all running aggregates with a single new record."""
        pair = r.get("symbol", "?")
        scanner = r.get("setup_type", "?")
        trade_type = r.get("trade_type", "?")
        won = r.get("pnl_pct", 0) > 0
        pnl_usd = r.get("pnl_usd", 0)
        pnl_pct = r.get("pnl_pct", 0)
        exit_r = r.get("exit_r", 0)
        mfe_r = r.get("mfe_r", 0)
        mae_r = r.get("mae_r", 0)
        fees = r.get("total_fees_usd", 0)
        duration = r.get("duration_sec", 0)
        ml_prob = r.get("ml_probability", 0)
        ml_verdict = r.get("ml_verdict", "")

        # Determine session from timestamp
        session = "unknown"
        closed_at = r.get("closed_at", "")
        if closed_at:
            try:
                dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                session = _utc_session(dt.hour)
            except Exception:
                pass

        # --- By Pair ---
        if pair not in self._by_pair:
            self._by_pair[pair] = {"trades": 0, "wins": 0, "pnl_usd": 0.0,
                                   "pnl_pct_sum": 0.0, "r_sum": 0.0,
                                   "mfe_sum": 0.0, "mae_sum": 0.0, "fees_usd": 0.0}
        bp = self._by_pair[pair]
        bp["trades"] += 1
        if won: bp["wins"] += 1
        bp["pnl_usd"] += pnl_usd
        bp["pnl_pct_sum"] += pnl_pct
        bp["r_sum"] += exit_r
        bp["mfe_sum"] += mfe_r
        bp["mae_sum"] += mae_r
        bp["fees_usd"] += fees

        # --- By Scanner ---
        if scanner not in self._by_scanner:
            self._by_scanner[scanner] = {"trades": 0, "wins": 0, "pnl_usd": 0.0, "r_sum": 0.0}
        bs = self._by_scanner[scanner]
        bs["trades"] += 1
        if won: bs["wins"] += 1
        bs["pnl_usd"] += pnl_usd
        bs["r_sum"] += exit_r

        # --- By Trade Type ---
        if trade_type not in self._by_trade_type:
            self._by_trade_type[trade_type] = {"trades": 0, "wins": 0, "pnl_usd": 0.0,
                                                "r_sum": 0.0, "dur_sum": 0}
        bt = self._by_trade_type[trade_type]
        bt["trades"] += 1
        if won: bt["wins"] += 1
        bt["pnl_usd"] += pnl_usd
        bt["r_sum"] += exit_r
        bt["dur_sum"] += duration

        # --- By Pair x Scanner ---
        ps_key = f"{pair}|{scanner}"
        if ps_key not in self._by_pair_scanner:
            self._by_pair_scanner[ps_key] = {"symbol": pair, "scanner": scanner,
                                              "trades": 0, "wins": 0, "pnl_usd": 0.0,
                                              "r_sum": 0.0, "ml_prob_sum": 0.0}
        bps = self._by_pair_scanner[ps_key]
        bps["trades"] += 1
        if won: bps["wins"] += 1
        bps["pnl_usd"] += pnl_usd
        bps["r_sum"] += exit_r
        bps["ml_prob_sum"] += ml_prob

        # --- By Session ---
        if session not in self._by_session:
            self._by_session[session] = {"trades": 0, "wins": 0, "pnl_usd": 0.0, "r_sum": 0.0}
        ss = self._by_session[session]
        ss["trades"] += 1
        if won: ss["wins"] += 1
        ss["pnl_usd"] += pnl_usd
        ss["r_sum"] += exit_r

        # --- ML Accuracy (global) ---
        if ml_prob > 0:
            self._ml_accuracy["total"] += 1
            predicted_win = ml_prob >= 0.50
            if predicted_win == won:
                self._ml_accuracy["ml_correct"] += 1
            else:
                self._ml_accuracy["ml_wrong"] += 1
            if ml_verdict not in self._ml_accuracy["by_verdict"]:
                self._ml_accuracy["by_verdict"][ml_verdict] = {"n": 0, "wins": 0, "pnl_sum": 0}
            bv = self._ml_accuracy["by_verdict"][ml_verdict]
            bv["n"] += 1
            if won: bv["wins"] += 1
            bv["pnl_sum"] += pnl_usd

        # --- ML Accuracy by Pair ---
        if ml_prob > 0:
            if pair not in self._ml_accuracy_by_pair:
                self._ml_accuracy_by_pair[pair] = {"total": 0, "correct": 0, "wrong": 0, "pnl_sum": 0.0}
            mp = self._ml_accuracy_by_pair[pair]
            mp["total"] += 1
            predicted_win = ml_prob >= 0.50
            if predicted_win == won:
                mp["correct"] += 1
            else:
                mp["wrong"] += 1
            mp["pnl_sum"] += pnl_usd

    def get_snapshot(self) -> Dict:
        """Return the full aggregated snapshot for API response."""
        self.refresh()

        # Compute derived fields for by_pair
        by_pair_out = {}
        for k, v in self._by_pair.items():
            n = v["trades"]
            by_pair_out[k] = {
                **v,
                "win_rate": round(v["wins"] / n * 100, 1) if n > 0 else 0,
                "avg_r": round(v["r_sum"] / n, 3) if n > 0 else 0,
                "avg_mfe": round(v["mfe_sum"] / n, 3) if n > 0 else 0,
                "avg_mae": round(v["mae_sum"] / n, 3) if n > 0 else 0,
                "pnl_usd": round(v["pnl_usd"], 2),
                "fees_usd": round(v["fees_usd"], 2),
            }

        # Compute derived fields for by_scanner
        by_scanner_out = {}
        for k, v in self._by_scanner.items():
            n = v["trades"]
            by_scanner_out[k] = {
                **v,
                "win_rate": round(v["wins"] / n * 100, 1) if n > 0 else 0,
                "avg_r": round(v["r_sum"] / n, 3) if n > 0 else 0,
                "pnl_usd": round(v["pnl_usd"], 2),
            }

        # Compute derived fields for by_trade_type
        by_type_out = {}
        for k, v in self._by_trade_type.items():
            n = v["trades"]
            by_type_out[k] = {
                "trades": v["trades"], "wins": v["wins"],
                "pnl_usd": round(v["pnl_usd"], 2),
                "r_sum": v["r_sum"],
                "win_rate": round(v["wins"] / n * 100, 1) if n > 0 else 0,
                "avg_r": round(v["r_sum"] / n, 3) if n > 0 else 0,
                "avg_duration_min": round(v["dur_sum"] / n / 60, 1) if n > 0 else 0,
            }

        # Compute derived fields for by_pair_scanner
        ps_out = []
        for v in self._by_pair_scanner.values():
            n = v["trades"]
            ps_out.append({
                **v,
                "win_rate": round(v["wins"] / n * 100, 1) if n > 0 else 0,
                "avg_r": round(v["r_sum"] / n, 3) if n > 0 else 0,
                "avg_ml_prob": round(v["ml_prob_sum"] / n, 3) if n > 0 else 0,
                "pnl_usd": round(v["pnl_usd"], 2),
            })

        # By session
        by_session_out = {}
        for k, v in self._by_session.items():
            n = v["trades"]
            by_session_out[k] = {
                **v,
                "win_rate": round(v["wins"] / n * 100, 1) if n > 0 else 0,
                "avg_r": round(v["r_sum"] / n, 3) if n > 0 else 0,
                "pnl_usd": round(v["pnl_usd"], 2),
            }

        # ML accuracy
        ml_acc = dict(self._ml_accuracy)
        if ml_acc["total"] > 0:
            ml_acc["accuracy_pct"] = round(ml_acc["ml_correct"] / ml_acc["total"] * 100, 1)
        ml_acc_by_verdict = {}
        for k, v in ml_acc.get("by_verdict", {}).items():
            ml_acc_by_verdict[k] = {
                **v,
                "win_rate": round(v["wins"] / v["n"] * 100, 1) if v["n"] > 0 else 0,
                "pnl_sum": round(v["pnl_sum"], 2),
            }
        ml_acc["by_verdict"] = ml_acc_by_verdict

        # ML accuracy by pair
        ml_acc_pair = {}
        for pair, v in self._ml_accuracy_by_pair.items():
            ml_acc_pair[pair] = {
                **v,
                "accuracy_pct": round(v["correct"] / v["total"] * 100, 1) if v["total"] > 0 else 0,
                "pnl_sum": round(v["pnl_sum"], 2),
            }

        # Data through timestamp
        data_through = None
        if self._records:
            last = self._records[-1]
            data_through = last.get("closed_at", last.get("timestamp", ""))

        recent = self._records[-20:][::-1] if self._records else []

        return {
            "total_trades": len(self._records),
            "by_pair": by_pair_out,
            "by_scanner": by_scanner_out,
            "by_trade_type": by_type_out,
            "by_pair_scanner": ps_out,
            "by_session": by_session_out,
            "ml_accuracy": ml_acc,
            "ml_accuracy_by_pair": ml_acc_pair,
            "recent": recent,
            "_freshness": _freshness(
                data_through=data_through,
                record_count=len(self._records),
            ),
        }


# ---------------------------------------------------------------------------
#  Model History Tracker (for trend + drift)
# ---------------------------------------------------------------------------
class _ModelHistoryTracker:
    """Tracks model metrics over time for trend and drift detection."""

    def __init__(self):
        self._history_file = MODEL_HISTORY_FILE
        self._feature_history_file = FEATURE_HISTORY_FILE

    def record_training(self, scanner: str, metrics: Dict, feature_importances: Dict = None):
        """Append a training record after model is trained."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "scanner": scanner,
            "auc": metrics.get("auc_roc", 0),
            "accuracy": metrics.get("accuracy", 0),
            "precision": metrics.get("precision", 0),
            "recall": metrics.get("recall", 0),
            "n_samples": metrics.get("samples", 0),
            "n_features": metrics.get("n_features", 0),
            "positive_rate": metrics.get("positive_rate", 0),
        }
        self._history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._history_file, 'a') as f:
            f.write(json.dumps(record) + "\n")

        # Record feature importances separately
        if feature_importances:
            feat_record = {
                "ts": record["ts"],
                "scanner": scanner,
                "importances": feature_importances,
            }
            with open(self._feature_history_file, 'a') as f:
                f.write(json.dumps(feat_record) + "\n")

    def get_model_trend(self, scanner: str = None, last_n: int = 20) -> List[Dict]:
        """Return AUC/accuracy trend over last N training runs."""
        if not self._history_file.exists():
            return []
        records = []
        try:
            with open(self._history_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if scanner is None or r.get("scanner") == scanner:
                            records.append(r)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return []
        return records[-last_n:]

    def get_scanner_rankings(self, last_n: int = 10) -> List[Dict]:
        """Return scanner AUC ranking over last N training runs."""
        if not self._history_file.exists():
            return []
        # Group by approximate training batch (within 5 min = same batch)
        records = []
        try:
            with open(self._history_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception:
            return []

        if not records:
            return []

        # Group records into batches by timestamp proximity
        batches = []
        current_batch = [records[0]]
        for r in records[1:]:
            try:
                prev_ts = datetime.fromisoformat(current_batch[-1]["ts"])
                curr_ts = datetime.fromisoformat(r["ts"])
                if (curr_ts - prev_ts).total_seconds() < 300:  # 5 min window
                    current_batch.append(r)
                else:
                    batches.append(current_batch)
                    current_batch = [r]
            except Exception:
                current_batch.append(r)
        batches.append(current_batch)

        # For each batch, rank scanners by AUC
        rankings = []
        for batch in batches[-last_n:]:
            scanners = sorted(batch, key=lambda x: x.get("auc", 0), reverse=True)
            rankings.append({
                "ts": batch[0].get("ts"),
                "ranking": [{"scanner": s["scanner"], "auc": s.get("auc", 0)} for s in scanners],
            })
        return rankings

    def get_feature_drift(self, scanner: str, last_n: int = 5) -> Dict:
        """Compare feature importance rankings across last N training runs."""
        if not self._feature_history_file.exists():
            return {"scanner": scanner, "runs": [], "drift": []}
        records = []
        try:
            with open(self._feature_history_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("scanner") == scanner:
                            records.append(r)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return {"scanner": scanner, "runs": [], "drift": []}

        records = records[-last_n:]
        if len(records) < 2:
            return {"scanner": scanner, "runs": records, "drift": []}

        # Compare first and last run
        first_imp = records[0].get("importances", {})
        last_imp = records[-1].get("importances", {})

        # Rank features
        first_ranked = sorted(first_imp.keys(), key=lambda k: first_imp[k], reverse=True)
        last_ranked = sorted(last_imp.keys(), key=lambda k: last_imp[k], reverse=True)

        first_rank_map = {f: i for i, f in enumerate(first_ranked)}
        last_rank_map = {f: i for i, f in enumerate(last_ranked)}

        # Find features that moved significantly
        drift = []
        all_features = set(list(first_imp.keys()) + list(last_imp.keys()))
        for feat in all_features:
            old_rank = first_rank_map.get(feat, len(first_ranked))
            new_rank = last_rank_map.get(feat, len(last_ranked))
            shift = old_rank - new_rank  # positive = moved up
            if abs(shift) >= 3:  # significant shift
                drift.append({
                    "feature": feat,
                    "old_rank": old_rank + 1,
                    "new_rank": new_rank + 1,
                    "shift": shift,
                    "old_importance": round(first_imp.get(feat, 0), 4),
                    "new_importance": round(last_imp.get(feat, 0), 4),
                })
        drift.sort(key=lambda x: abs(x["shift"]), reverse=True)

        return {
            "scanner": scanner,
            "runs_compared": len(records),
            "first_run_ts": records[0].get("ts"),
            "last_run_ts": records[-1].get("ts"),
            "drift": drift[:20],  # top 20 movers
        }

    def get_model_health(self) -> Dict:
        """Compute health status per scanner: green/yellow/red based on AUC trend."""
        all_records = self.get_model_trend(scanner=None, last_n=100)
        if not all_records:
            return {"scanners": {}, "overall": "NO_DATA"}

        # Group by scanner
        by_scanner = {}
        for r in all_records:
            s = r.get("scanner", "?")
            if s not in by_scanner:
                by_scanner[s] = []
            by_scanner[s].append(r)

        health = {}
        for scanner, runs in by_scanner.items():
            if len(runs) < 2:
                health[scanner] = {
                    "status": "INSUFFICIENT_DATA",
                    "latest_auc": runs[-1].get("auc", 0) if runs else 0,
                    "trend": "unknown",
                    "runs": len(runs),
                }
                continue

            latest_auc = runs[-1].get("auc", 0)
            # Rolling average of last 4
            recent_aucs = [r.get("auc", 0) for r in runs[-4:]]
            avg_auc = sum(recent_aucs) / len(recent_aucs)
            prev_auc = runs[-2].get("auc", 0)

            # Determine trend
            if latest_auc > prev_auc + 0.01:
                trend = "improving"
            elif latest_auc < prev_auc - 0.01:
                trend = "declining"
            else:
                trend = "stable"

            # Health status
            if latest_auc >= 0.58:
                status = "GREEN"
            elif latest_auc >= 0.53:
                status = "YELLOW"
            else:
                status = "RED"

            # Check for AUC drift (>5% drop from rolling avg)
            if len(runs) >= 4 and latest_auc < avg_auc * 0.95:
                status = "RED"
                trend = "drift_detected"

            health[scanner] = {
                "status": status,
                "latest_auc": round(latest_auc, 4),
                "avg_auc_4run": round(avg_auc, 4),
                "trend": trend,
                "runs": len(runs),
                "latest_samples": runs[-1].get("n_samples", 0),
                "latest_ts": runs[-1].get("ts"),
            }

        # Overall
        statuses = [v["status"] for v in health.values() if v["status"] in ("GREEN", "YELLOW", "RED")]
        if not statuses:
            overall = "NO_DATA"
        elif all(s == "GREEN" for s in statuses):
            overall = "HEALTHY"
        elif any(s == "RED" for s in statuses):
            overall = "DEGRADED"
        else:
            overall = "MIXED"

        return {"scanners": health, "overall": overall}


# ---------------------------------------------------------------------------
#  Main Dashboard class
# ---------------------------------------------------------------------------
class MLDashboard:
    """aiohttp dashboard for ML training visualization."""

    def __init__(self, trainer=None, port: int = 8081):
        self._trainer = trainer
        self._port = port
        self._app = web.Application()
        # P0: Model cache with mtime tracking
        self._loaded_models: Dict[str, Tuple] = {}   # scanner -> (model, features, meta, mtime)
        # P0: In-memory feedback cache
        self._feedback_cache = _FeedbackCache(FEEDBACK_FILE)
        # P1/P2: Model history tracker
        self._history_tracker = _ModelHistoryTracker()
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
        # NEW endpoints
        self._app.router.add_get("/api/model-trend", self._handle_model_trend)
        self._app.router.add_get("/api/model-health", self._handle_model_health)
        self._app.router.add_get("/api/feature-drift", self._handle_feature_drift)
        self._app.router.add_get("/api/scanner-rankings", self._handle_scanner_rankings)
        # Serve static files
        static_dir = PROJECT_ROOT / "dashboard" / "static"
        if static_dir.exists():
            self._app.router.add_static("/static", static_dir)

    # -------------------------------------------------------------------
    #  Index
    # -------------------------------------------------------------------
    async def _handle_index(self, request):
        template_path = TEMPLATE_DIR / "ml_dashboard.html"
        if template_path.exists():
            return web.FileResponse(template_path)
        return _error_response("index", "ML Dashboard template not found", 404)

    # -------------------------------------------------------------------
    #  Status  (P0: error states)
    # -------------------------------------------------------------------
    async def _handle_status(self, request):
        if self._trainer:
            try:
                data = self._trainer.get_status()
                data["_freshness"] = _freshness()
                return web.json_response(data, dumps=_json_dumps)
            except Exception as e:
                logger.exception("Status error (trainer): %s", e)
                return _error_response("status", str(e))

        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                # Trim backtest results for performance
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
                data["_freshness"] = _freshness(
                    data_through=data.get("completed_at"),
                    extra={"source": "status_file",
                           "file_age_sec": round(time.time() - STATUS_FILE.stat().st_mtime)},
                )
                return web.json_response(_sanitize_json(data), dumps=_json_dumps)
            except Exception as e:
                logger.exception("Status file parse error: %s", e)
                return _error_response("status", f"Status file corrupt: {e}")
        return web.json_response({"phase": "idle", "results": {},
                                   "_freshness": _freshness()})

    # -------------------------------------------------------------------
    #  Results  (P0: error states)
    # -------------------------------------------------------------------
    async def _handle_results(self, request):
        results = {}
        errors = []
        if RESULTS_DIR.exists():
            for f in RESULTS_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                    symbol_raw = data.get("symbol", "")
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
                except Exception as e:
                    errors.append(f"{f.name}: {e}")
        resp = {"results": results, "_freshness": _freshness(
            extra={"result_count": len(results)}
        )}
        if errors:
            resp["_warnings"] = errors[:5]
        return web.json_response(resp, dumps=_json_dumps)

    # -------------------------------------------------------------------
    #  Scanner Detail  (P0: error states)
    # -------------------------------------------------------------------
    async def _handle_scanner_detail(self, request):
        scanner = request.match_info["scanner"]
        results = {}
        if RESULTS_DIR.exists():
            for f in RESULTS_DIR.glob(f"{scanner}*.json"):
                try:
                    data = json.loads(f.read_text())
                    results[f.stem] = data
                except Exception as e:
                    logger.warning("Scanner detail parse error %s: %s", f.name, e)
        if not results:
            return _error_response("scanner_detail", f"No results for scanner '{scanner}'", 404)
        return web.json_response({"results": results, "_freshness": _freshness()}, dumps=_json_dumps)

    # -------------------------------------------------------------------
    #  Comparison  (P0: error states)
    # -------------------------------------------------------------------
    async def _handle_comparison(self, request):
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                comp = data.get("results", {}).get("comparison", {})
                comp["_freshness"] = _freshness(data_through=data.get("completed_at"))
                return web.json_response(comp, dumps=_json_dumps)
            except Exception as e:
                return _error_response("comparison", f"Parse error: {e}")
        return web.json_response({"_freshness": _freshness()})

    # -------------------------------------------------------------------
    #  Models  (P0: error states)
    # -------------------------------------------------------------------
    async def _handle_models(self, request):
        if self._trainer:
            try:
                models = {k: m.get_status() for k, m in self._trainer._models.items()}
                models["_freshness"] = _freshness()
                return web.json_response(models, dumps=_json_dumps)
            except Exception as e:
                return _error_response("models", str(e))

        result = {}
        errors = []
        if MODELS_DIR.exists():
            # Candidate model files
            for f in MODELS_DIR.glob("candidate_*.json"):
                if f.stem == "candidate_all_scanners":
                    continue
                try:
                    data = json.loads(f.read_text())
                    scanner = data.get("scanner", f.stem.replace("candidate_", ""))
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
                except Exception as e:
                    errors.append(f"{f.name}: {e}")

        # Get model file versions
        model_versions = {}
        if MODELS_DIR.exists():
            for f in MODELS_DIR.glob("model_*_features.json"):
                try:
                    meta = json.loads(f.read_text())
                    name = meta.get("scanner", f.stem)
                    model_versions[name] = meta.get("trained_at", "unknown")
                except Exception:
                    pass

        resp = {**result, "_freshness": _freshness(
            model_version=json.dumps(model_versions) if model_versions else None,
            extra={"model_count": len(result)},
        )}
        if errors:
            resp["_warnings"] = errors[:5]
        return web.json_response(resp, dumps=_json_dumps)

    # -------------------------------------------------------------------
    #  Features  (P0: error states)
    # -------------------------------------------------------------------
    async def _handle_features(self, request):
        result = {}
        if MODELS_DIR.exists():
            for f in MODELS_DIR.glob("candidate_*.json"):
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
                except Exception as e:
                    logger.warning("Feature parse error %s: %s", f.name, e)
        return web.json_response({**result, "_freshness": _freshness()}, dumps=_json_dumps)

    # -------------------------------------------------------------------
    #  Collector
    # -------------------------------------------------------------------
    async def _handle_collector(self, request):
        if self._trainer:
            try:
                data = self._trainer._collector.get_progress()
                data["_freshness"] = _freshness()
                return web.json_response(data, dumps=_json_dumps)
            except Exception as e:
                return _error_response("collector", str(e))
        return web.json_response({"_freshness": _freshness()})

    # -------------------------------------------------------------------
    #  Start Training
    # -------------------------------------------------------------------
    async def _handle_start_training(self, request):
        if not self._trainer:
            return _error_response("train_start", "No trainer configured", 400)
        try:
            body = await request.json() if request.content_length else {}
            symbols = body.get("symbols", ["BTC/USDT", "ETH/USDT", "AVAX/USDT"])
            timeframes = body.get("timeframes", ["1m", "5m", "15m"])
            asyncio.create_task(self._trainer.run_full_pipeline(symbols, timeframes))
            return web.json_response({"status": "started", "symbols": symbols,
                                       "timeframes": timeframes, "_freshness": _freshness()})
        except Exception as e:
            return _error_response("train_start", str(e))

    # -------------------------------------------------------------------
    #  History
    # -------------------------------------------------------------------
    async def _handle_history(self, request):
        try:
            from ml_training.backtest_tracker import BacktestTracker
            tracker = BacktestTracker()
            return web.json_response({
                "runs": tracker.get_comparison(last_n=20),
                "trend": tracker.get_improvement_trend(),
                "best_run": tracker.get_best_run(),
                "_freshness": _freshness(),
            }, dumps=_json_dumps)
        except Exception as e:
            logger.exception("History error: %s", e)
            return _error_response("history", str(e))

    # -------------------------------------------------------------------
    #  Calibration
    # -------------------------------------------------------------------
    async def _handle_calibration(self, request):
        result = {}
        if MODELS_DIR.exists():
            for f in MODELS_DIR.glob("candidate_*.json"):
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
                except Exception as e:
                    logger.warning("Calibration parse error %s: %s", f.name, e)
            # All scanners comparison
            all_file = MODELS_DIR / "candidate_all_scanners.json"
            if all_file.exists():
                try:
                    data = json.loads(all_file.read_text())
                    result["_comparison"] = data.get("comparison", [])
                except Exception:
                    pass
        result["_freshness"] = _freshness()
        return web.json_response(result, dumps=_json_dumps)

    # -------------------------------------------------------------------
    #  Candidates  (P1: data sufficiency warnings)
    # -------------------------------------------------------------------
    async def _handle_candidates(self, request):
        if STATUS_FILE.exists():
            try:
                data = json.loads(STATUS_FILE.read_text())
                ct = data.get('results', {}).get('candidate_training', {})

                # P1: Add data sufficiency warnings per symbol/scanner
                sufficiency_warnings = []
                for sym, sym_data in ct.items():
                    if not isinstance(sym_data, dict):
                        continue
                    comparison = sym_data.get("comparison", [])
                    for row in comparison:
                        n = row.get("candidates", 0)
                        scanner = row.get("scanner", "?")
                        if n < MIN_SAMPLES_BLOCK:
                            sufficiency_warnings.append({
                                "symbol": sym, "scanner": scanner,
                                "samples": n, "severity": "BLOCK",
                                "message": f"{sym} {scanner}: {n} samples — INSUFFICIENT, model unreliable",
                            })
                        elif n < MIN_SAMPLES_WARN:
                            sufficiency_warnings.append({
                                "symbol": sym, "scanner": scanner,
                                "samples": n, "severity": "WARN",
                                "message": f"{sym} {scanner}: {n} samples — LOW, results may be noisy",
                            })

                result = _sanitize_json(ct)
                if isinstance(result, dict):
                    result["_sufficiency_warnings"] = sufficiency_warnings
                    result["_freshness"] = _freshness(
                        data_through=data.get("completed_at"),
                        extra={"warning_count": len(sufficiency_warnings)},
                    )
                return web.json_response(result, dumps=_json_dumps)
            except Exception as e:
                logger.exception("Candidates error: %s", e)
                return _error_response("candidates", str(e))
        return web.json_response({"_freshness": _freshness()})

    # -------------------------------------------------------------------
    #  Score  (P0: model cache invalidation via mtime)
    # -------------------------------------------------------------------
    async def _handle_score(self, request):
        """Score a candidate using trained ML model, with mtime-based cache invalidation."""
        import pandas as pd
        from ml_training.candidate_trainer import CandidateTrainer

        body = {}
        try:
            body = await request.json()
            scanner = body.get("scanner", "")
            symbol = body.get("symbol", "?")
            side = body.get("side", "?")
            features = body.get("features", {})

            if not scanner or not features:
                return _error_response("score", "Missing scanner or features", 400)

            # P0: Model cache with mtime invalidation
            model_path = MODELS_DIR / f"model_{scanner}.joblib"
            meta_path = MODELS_DIR / f"model_{scanner}_features.json"

            current_mtime = model_path.stat().st_mtime if model_path.exists() else 0

            if scanner in self._loaded_models:
                cached_model, cached_features, cached_meta, cached_mtime = self._loaded_models[scanner]
                if current_mtime != cached_mtime:
                    logger.info("Model %s changed on disk (mtime %s -> %s), reloading",
                                scanner, cached_mtime, current_mtime)
                    del self._loaded_models[scanner]

            if scanner not in self._loaded_models:
                model, feature_names = CandidateTrainer.load_model(scanner)
                if model is None:
                    return web.json_response({
                        "probability": 0.5, "scanner": scanner,
                        "verdict": "NO_MODEL", "error": f"no model for {scanner}",
                        "rank_bucket": "NONE", "model_version": "none",
                        "_freshness": _freshness(),
                    }, dumps=_json_dumps)
                meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
                self._loaded_models[scanner] = (model, feature_names, meta, current_mtime)
                logger.info("Loaded model for %s (%d features, mtime=%s)",
                            scanner, len(feature_names), current_mtime)

            model, feature_names, meta, _ = self._loaded_models[scanner]

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
                "_freshness": _freshness(model_version=meta.get("trained_at", "unknown")),
            }, dumps=_json_dumps)
        except Exception as e:
            logger.exception("Score error: %s", e)
            return web.json_response({
                "probability": 0.5, "scanner": body.get("scanner", ""),
                "verdict": "ERROR", "error": str(e),
                "rank_bucket": "ERROR", "model_version": "error",
                "_freshness": _freshness(),
            }, dumps=_json_dumps)

    # -------------------------------------------------------------------
    #  Health
    # -------------------------------------------------------------------
    async def _handle_health(self, request):
        try:
            import psutil
            loaded = list(self._loaded_models.keys())
            available = [f.stem.replace("model_", "").replace("_features", "")
                         for f in MODELS_DIR.glob("model_*_features.json")] if MODELS_DIR.exists() else []

            versions = {}
            if MODELS_DIR.exists():
                for f in MODELS_DIR.glob("model_*_features.json"):
                    try:
                        meta = json.loads(f.read_text())
                        name = meta.get("scanner", f.stem)
                        versions[name] = {
                            "trained_at": meta.get("trained_at"),
                            "n_features": meta.get("n_features"),
                        }
                    except Exception:
                        pass

            mem = psutil.virtual_memory()

            # Include model health assessment
            model_health = self._history_tracker.get_model_health()

            return web.json_response({
                "status": "healthy",
                "server": "vm2-ml",
                "models_loaded": loaded,
                "models_available": available,
                "model_versions": versions,
                "model_health": model_health,
                "memory_used_mb": round(mem.used / 1024 / 1024),
                "memory_total_mb": round(mem.total / 1024 / 1024),
                "memory_pct": mem.percent,
                "feedback_records": len(self._feedback_cache._records),
                "_freshness": _freshness(
                    model_version=json.dumps(versions) if versions else None,
                    record_count=len(self._feedback_cache._records),
                ),
            }, dumps=_json_dumps)
        except Exception as e:
            logger.exception("Health check error: %s", e)
            return _error_response("health", str(e))

    # -------------------------------------------------------------------
    #  Live Feedback  (P0: uses in-memory cache, P1: per-pair accuracy, P3: sessions)
    # -------------------------------------------------------------------
    async def _handle_live_feedback(self, request):
        try:
            snapshot = self._feedback_cache.get_snapshot()
            return web.json_response(_sanitize_json(snapshot), dumps=_json_dumps)
        except Exception as e:
            logger.exception("Live feedback error: %s", e)
            return _error_response("live_feedback", str(e))

    # -------------------------------------------------------------------
    #  Validation
    # -------------------------------------------------------------------
    async def _handle_validation(self, request):
        validation_file = PROJECT_ROOT / "storage" / "ml_validation_results.json"
        if validation_file.exists():
            try:
                data = json.loads(validation_file.read_text())
                data["_freshness"] = _freshness(
                    data_through=data.get("timestamp"),
                    extra={"file_age_sec": round(time.time() - validation_file.stat().st_mtime)},
                )
                return web.json_response(_sanitize_json(data), dumps=_json_dumps)
            except Exception as e:
                return _error_response("validation", str(e))
        return _error_response("validation", "No validation results yet. Run: python -m ml_training.validate_auc", 404)

    # -------------------------------------------------------------------
    #  Run Validation
    # -------------------------------------------------------------------
    async def _handle_run_validation(self, request):
        try:
            from ml_training.validate_auc import run_validation

            body = await request.json() if request.content_length else {}
            symbols = body.get("symbols")

            async def _run():
                try:
                    await run_validation(symbols=symbols, do_fetch=False)
                except Exception as e:
                    logger.error("Validation run failed: %s", e)

            asyncio.create_task(_run())
            return web.json_response({"status": "started", "symbols": symbols or "all",
                                       "_freshness": _freshness()})
        except Exception as e:
            return _error_response("run_validation", str(e))

    # -------------------------------------------------------------------
    #  NEW: Model Trend  (P1)
    # -------------------------------------------------------------------
    async def _handle_model_trend(self, request):
        """Return AUC/accuracy trend over training runs for a given scanner."""
        scanner = request.query.get("scanner")
        last_n = int(request.query.get("last_n", "20"))
        try:
            trend = self._history_tracker.get_model_trend(scanner=scanner, last_n=last_n)
            return web.json_response({
                "scanner": scanner or "all",
                "trend": trend,
                "count": len(trend),
                "_freshness": _freshness(
                    data_through=trend[-1].get("ts") if trend else None,
                    record_count=len(trend),
                ),
            }, dumps=_json_dumps)
        except Exception as e:
            return _error_response("model_trend", str(e))

    # -------------------------------------------------------------------
    #  NEW: Model Health  (P2: AUC drift monitoring)
    # -------------------------------------------------------------------
    async def _handle_model_health(self, request):
        """Return per-scanner model health: green/yellow/red + drift detection."""
        try:
            health = self._history_tracker.get_model_health()
            health["_freshness"] = _freshness()
            return web.json_response(health, dumps=_json_dumps)
        except Exception as e:
            return _error_response("model_health", str(e))

    # -------------------------------------------------------------------
    #  NEW: Feature Drift  (P2)
    # -------------------------------------------------------------------
    async def _handle_feature_drift(self, request):
        """Return feature importance changes across training runs."""
        scanner = request.query.get("scanner", "")
        if not scanner:
            return _error_response("feature_drift", "scanner parameter required", 400)
        try:
            drift = self._history_tracker.get_feature_drift(scanner)
            drift["_freshness"] = _freshness()
            return web.json_response(drift, dumps=_json_dumps)
        except Exception as e:
            return _error_response("feature_drift", str(e))

    # -------------------------------------------------------------------
    #  NEW: Scanner Rankings  (P2)
    # -------------------------------------------------------------------
    async def _handle_scanner_rankings(self, request):
        """Return inter-scanner AUC rankings over time."""
        last_n = int(request.query.get("last_n", "10"))
        try:
            rankings = self._history_tracker.get_scanner_rankings(last_n=last_n)
            return web.json_response({
                "rankings": rankings,
                "count": len(rankings),
                "_freshness": _freshness(),
            }, dumps=_json_dumps)
        except Exception as e:
            return _error_response("scanner_rankings", str(e))

    # -------------------------------------------------------------------
    #  Start server
    # -------------------------------------------------------------------
    async def start(self):
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._port)
        await site.start()
        logger.info("ML Dashboard v3.0 running on http://0.0.0.0:%d", self._port)
        logger.info("  New endpoints: /api/model-trend, /api/model-health, /api/feature-drift, /api/scanner-rankings")
