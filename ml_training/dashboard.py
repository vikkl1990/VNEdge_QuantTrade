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
            # Phase 4.3: walk-forward stability diagnostics
            "edge_verdict": metrics.get("edge_verdict", "UNCLEAR"),
            "overfit_gap": metrics.get("overfit_gap", 0.0),
            "oos_mean": metrics.get("oos_mean", 0.0),
            "oos_std": metrics.get("oos_std", 0.0),
            "in_sample_mean": metrics.get("in_sample_mean", 0.0),
            "purge_gap_bars": metrics.get("purge_gap_bars", 0),
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
        self._app.router.add_get("/api/ml/health", self._handle_ml_health)  # Phase 4.2
        self._app.router.add_get("/api/ml/live-calibration", self._handle_ml_live_calibration)  # Phase 4.6
        self._app.router.add_get("/api/ml/edge-verdict-trend", self._handle_ml_edge_verdict_trend)  # Phase B.7
        self._app.router.add_get("/api/ml/family-verdict-matrix", self._handle_ml_family_verdict_matrix)  # Phase B.8
        self._app.router.add_get("/api/live-feedback", self._handle_live_feedback)
        self._app.router.add_get("/api/validation", self._handle_validation)
        self._app.router.add_post("/api/validation/run", self._handle_run_validation)
        # NEW endpoints
        self._app.router.add_get("/api/model-trend", self._handle_model_trend)
        self._app.router.add_get("/api/model-health", self._handle_model_health)
        self._app.router.add_get("/api/feature-drift", self._handle_feature_drift)
        self._app.router.add_get("/api/scanner-rankings", self._handle_scanner_rankings)
        self._app.router.add_get("/api/backtest-all", self._handle_backtest_all)

        # Research Center endpoints (2026-04-17) — "ML as innovation lab"
        self._app.router.add_get("/api/research/cohort-health", self._handle_research_cohort_health)
        self._app.router.add_get("/api/research/weakspots", self._handle_research_weakspots)
        self._app.router.add_get("/api/research/policy-variants", self._handle_research_policy_variants)
        self._app.router.add_get("/api/research/edge-trajectory", self._handle_research_edge_trajectory)
        self._app.router.add_get("/api/research/suggestions", self._handle_research_suggestions)
        self._app.router.add_get("/api/research/vetoes", self._handle_research_vetoes)
        self._app.router.add_get("/api/research/timeline", self._handle_research_timeline)
        self._app.router.add_post("/api/research/refresh", self._handle_research_refresh)
        self._app.router.add_get("/api/research/summary", self._handle_research_summary)
        self._app.router.add_get("/research", self._handle_research_page)
        # Mutating endpoints (human-in-loop actions from UI)
        self._app.router.add_post("/api/research/suggestions/approve", self._handle_research_suggestions_approve)
        self._app.router.add_post("/api/research/suggestions/reject", self._handle_research_suggestions_reject)
        self._app.router.add_post("/api/research/vetoes/add", self._handle_research_vetoes_add)
        self._app.router.add_post("/api/research/vetoes/remove", self._handle_research_vetoes_remove)
        # Scanner registry + promotions (2026-04-17)
        self._app.router.add_get("/api/research/scanner-registry", self._handle_research_scanner_registry)
        self._app.router.add_get("/api/research/promotions", self._handle_research_promotions)
        self._app.router.add_get("/api/research/promotion-candidates", self._handle_research_promotion_candidates)
        self._app.router.add_post("/api/research/promotions/approve", self._handle_research_promotions_approve)
        self._app.router.add_post("/api/research/promotions/remove", self._handle_research_promotions_remove)
        # Scanner Attrition Funnel + Variants (Phase 1-3 of scanner research, 2026-04-17)
        self._app.router.add_get("/api/research/scanner-funnel", self._handle_research_scanner_funnel)
        self._app.router.add_get("/api/research/scanner-offline", self._handle_research_scanner_offline)
        self._app.router.add_get("/api/research/scanner-variants", self._handle_research_scanner_variants)
        self._app.router.add_get("/api/research/scanner-variants/active", self._handle_research_scanner_variants_active)
        self._app.router.add_post("/api/research/scanner-variants/approve", self._handle_research_scanner_variants_approve)
        self._app.router.add_post("/api/research/scanner-variants/remove", self._handle_research_scanner_variants_remove)
        # Phase 4: Shadow observation + enforcement
        self._app.router.add_get("/api/research/scanner-variants/observation", self._handle_research_scanner_observation)
        self._app.router.add_post("/api/research/scanner-variants/enforce", self._handle_research_scanner_variants_enforce)

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
        try:
            body = await request.json() if request.content_length else {}
            symbols = body.get("symbols", ["BTC/USDT", "ETH/USDT", "SOL/USDT", "AVAX/USDT", "LINK/USDT", "DOGE/USDT"])
            timeframes = body.get("timeframes", ["5m", "15m", "1h", "4h"])
            if self._trainer:
                asyncio.create_task(self._trainer.run_full_pipeline(symbols, timeframes))
                return web.json_response({"status": "started", "mode": "in_process", "symbols": symbols,
                                           "timeframes": timeframes, "_freshness": _freshness()})
            # The serving dashboard runs in its own process (run_trainer spawns
            # it with trainer=None), so the UI buttons used to answer
            # "No trainer configured". Spawn a training-only process instead —
            # the same command auto_retrainer uses. Models are picked up by
            # this server on mtime change.
            import subprocess, sys as _sys
            cmd = [_sys.executable, "-m", "ml_training.run_trainer", "--train", "--no-dashboard",
                   "--symbols", ",".join(symbols), "--timeframes", ",".join(timeframes),
                   "--port", str(self._port)]
            log_path = PROJECT_ROOT / "logs" / "ml_retrain.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logf = open(log_path, "a")
            logf.write(f"\n=== {datetime.now(timezone.utc).isoformat()} UI-triggered training {symbols} {timeframes} ===\n")
            logf.flush()
            proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), stdout=logf, stderr=subprocess.STDOUT)
            return web.json_response({"status": "started", "mode": "subprocess", "pid": proc.pid,
                                       "symbols": symbols, "timeframes": timeframes,
                                       "log": str(log_path), "_freshness": _freshness()})
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
        """Score a candidate using trained ML model, with mtime-based cache invalidation.

        Phase 4.2 additions:
          - Loud error when model is missing (not a default 0.5 masquerading as prediction)
          - Training-serving schema validation at every score call
          - Drift metrics included in response
          - Explicit ABSTAIN verdict when schema drift is detected
        """
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

            # ── Phase 4.5: FAMILY-AWARE MODEL ROUTING ──
            # When the client supplies a symbol, try the family-scoped model
            # first (e.g. model_structure_bounce_family_liquid_majors.joblib)
            # and fall back to the per-scanner model if that file doesn't
            # exist. This lets pair-family training (Phase 6 of the pipeline)
            # actually be served in production instead of being discarded.
            from ml_training.candidate_trainer import symbol_to_family
            resolved_family = symbol_to_family(symbol) if symbol and symbol != "?" else "other"

            # Build the candidate file_keys in priority order
            candidate_keys: list = []
            if resolved_family != "other":
                candidate_keys.append(f"{scanner}_family_{resolved_family}")
            candidate_keys.append(scanner)  # fallback

            # Find the first key whose model file exists on disk
            file_key = None
            model_path = None
            meta_path = None
            for _k in candidate_keys:
                _mp = MODELS_DIR / f"model_{_k}.joblib"
                if _mp.exists():
                    file_key = _k
                    model_path = _mp
                    meta_path = MODELS_DIR / f"model_{_k}_features.json"
                    break

            if file_key is None:
                # Neither family nor per-scanner model exists
                logger.warning(
                    "MODEL MISSING: scanner=%s symbol=%s side=%s family=%s tried=%s — returning ABSTAIN",
                    scanner, symbol, side, resolved_family, candidate_keys,
                )
                return web.json_response({
                    "probability": None,
                    "scanner": scanner,
                    "symbol": symbol,
                    "side": side,
                    "verdict": "ABSTAIN_NO_MODEL",
                    "error": f"no model file for scanner='{scanner}' family='{resolved_family}'",
                    "tried_keys": candidate_keys,
                    "rank_bucket": "NONE",
                    "model_version": "none",
                    "resolved_scope": None,
                    "resolved_family": None,
                    "features_received": len(features),
                    "features_expected": None,
                    "_freshness": _freshness(),
                }, dumps=_json_dumps, status=503)

            current_mtime = model_path.stat().st_mtime
            resolved_scope = "family" if file_key != scanner else "scanner"

            if file_key in self._loaded_models:
                cached_model, cached_features, cached_meta, cached_mtime = self._loaded_models[file_key]
                if current_mtime != cached_mtime:
                    logger.info("Model %s changed on disk (mtime %s -> %s), reloading",
                                file_key, cached_mtime, current_mtime)
                    del self._loaded_models[file_key]

            if file_key not in self._loaded_models:
                # Reuse CandidateTrainer.load_model with the explicit family override
                if resolved_scope == "family":
                    model, feature_names = CandidateTrainer.load_model(
                        scanner, family_name=resolved_family,
                    )
                else:
                    model, feature_names = CandidateTrainer.load_model(scanner)
                if model is None:
                    # Race: file was deleted between existence-check and load
                    return web.json_response({
                        "probability": None,
                        "scanner": scanner,
                        "symbol": symbol,
                        "side": side,
                        "verdict": "ABSTAIN_NO_MODEL",
                        "error": "model file disappeared mid-load",
                        "rank_bucket": "NONE",
                        "model_version": "none",
                        "_freshness": _freshness(),
                    }, dumps=_json_dumps, status=503)

                meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
                self._loaded_models[file_key] = (model, feature_names, meta, current_mtime)
                logger.info(
                    "Loaded model %s (%d features, mtime=%s, scope=%s, family=%s)",
                    file_key, len(feature_names), current_mtime,
                    resolved_scope, resolved_family,
                )

            model, feature_names, meta, _ = self._loaded_models[file_key]

            # ── Phase 4.2: TRAINING-SERVING SKEW CHECK ──
            # How many of the features the model expects are ACTUALLY in the request?
            # If the caller sends 116 features and model expects 247, we had 131 zero-fills.
            _matched = sum(1 for f in feature_names if f in features)
            _expected = len(feature_names)
            _match_pct = (_matched / _expected) if _expected > 0 else 0.0
            _missing_features = [f for f in feature_names if f not in features][:10]  # first 10

            # Phase 4.2: ABSTAIN if drift is severe (< 80% match)
            if _match_pct < 0.80 and _expected > 0:
                logger.warning(
                    "SCORE SKEW: %s matched %d/%d features (%.1f%%) — ABSTAINING",
                    scanner, _matched, _expected, _match_pct * 100,
                )
                return web.json_response({
                    "probability": None,  # not a real prediction
                    "scanner": scanner,
                    "symbol": symbol,
                    "side": side,
                    "verdict": "ABSTAIN_SKEW",
                    "error": f"training-serving skew: only {_matched}/{_expected} features matched ({_match_pct:.1%})",
                    "features_matched": _matched,
                    "features_expected": _expected,
                    "match_pct": round(_match_pct, 4),
                    "missing_features_sample": _missing_features,
                    "rank_bucket": "NONE",
                    "model_version": meta.get("trained_at", "unknown"),
                    "_freshness": _freshness(model_version=meta.get("trained_at", "unknown")),
                }, dumps=_json_dumps, status=503)

            # Build DataFrame with aligned columns
            row_df = pd.DataFrame([features])
            for col in feature_names:
                if col not in row_df.columns:
                    row_df[col] = 0.0
            row_df = row_df[feature_names].fillna(0)

            # Handle both classifiers (predict_proba) and regressors (predict)
            if hasattr(model, 'predict_proba'):
                prob = float(model.predict_proba(row_df)[0, 1])
            elif hasattr(model, 'predict'):
                # Regressor: normalize prediction to 0-1 range using sigmoid
                raw = float(model.predict(row_df)[0])
                import math
                prob = 1.0 / (1.0 + math.exp(-raw * 2))  # sigmoid scaling
            else:
                prob = 0.5

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
                # Renamed from "WEAK" (2026-09-10): "WEAK" is also an
                # edge_verdict (OOS AUC 0.54-0.58) and a live-calibration
                # verdict; three meanings for one word hid a real bug in P3.6.
                verdict = "MARGINAL"
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
                "feature_set_version": meta.get("feature_schema_version", f"fs_legacy_{len(feature_names)}feat"),
                "feature_schema_hash": meta.get("feature_schema_hash", ""),
                "has_htf_features": meta.get("has_htf_features", False),
                "trainer_phase": meta.get("trainer_phase", "pre_4.2"),
                "label_type": "mfe_binary",
                "calibrated": False,
                # Phase 4.5: surface which model actually scored this candidate
                "resolved_scope": resolved_scope,          # "family" | "scanner"
                "resolved_family": resolved_family if resolved_scope == "family" else None,
                "file_key": file_key,
                # Phase 4.8: surface edge_verdict + overfit so the client can
                # apply selective gating (tighter threshold on HOLDS models,
                # soft-blend only on WEAK/UNCLEAR). NO_EDGE is blocked at save
                # time so shouldn't show up here, but we still return it for
                # visibility.
                "edge_verdict": meta.get("edge_verdict"),
                "oos_mean": meta.get("oos_mean"),
                "overfit_gap": meta.get("overfit_gap"),
                "features_matched": _matched,
                "features_expected": _expected,
                "match_pct": round(_match_pct, 4),
                "_freshness": _freshness(model_version=meta.get("trained_at", "unknown")),
            }, dumps=_json_dumps)
        except Exception as e:
            logger.exception("Score error: %s", e)
            return web.json_response({
                "probability": None,  # Phase 4.2: not a real prediction
                "scanner": body.get("scanner", ""),
                "verdict": "ABSTAIN_ERROR",
                "error": str(e),
                "rank_bucket": "ERROR",
                "model_version": "error",
                "_freshness": _freshness(),
            }, dumps=_json_dumps, status=500)

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
    #  Phase 4.2 — ML health deep inspection
    # -------------------------------------------------------------------
    async def _handle_ml_health(self, request):
        """Phase 4.2: Detailed ML health endpoint.

        Returns:
          - Per-scanner model status (exists, feature count, schema version, age)
          - Global feature_schema.json if present
          - Last N score requests' match_pct (skew telemetry)
          - Any drift/degradation warnings
        """
        try:
            # Expected scanners (keep in sync with SCANNERS in trainer.py)
            EXPECTED_SCANNERS = [
                "structure_bounce",
                "ema_momentum",
                "rsi_divergence",
                "vwap_mean_revert",
                "liquidity_sweep",
                "bos_choch",
                "trend_continuation",
            ]

            scanner_status: Dict[str, Dict[str, Any]] = {}
            now_ts = time.time()

            for scanner in EXPECTED_SCANNERS:
                model_path = MODELS_DIR / f"model_{scanner}.joblib"
                meta_path = MODELS_DIR / f"model_{scanner}_features.json"

                entry: Dict[str, Any] = {
                    "scanner": scanner,
                    "model_file_exists": model_path.exists(),
                    "meta_file_exists": meta_path.exists(),
                }
                if model_path.exists():
                    stat = model_path.stat()
                    entry["model_size_bytes"] = stat.st_size
                    entry["age_hours"] = round((now_ts - stat.st_mtime) / 3600, 1)
                # (2026-09-11) Serving prefers the family models
                # (model_{scanner}_family_*.joblib) and per-symbol runs no
                # longer rewrite the generic file, so age must be the NEWEST
                # of scanner + family files. Otherwise the scanner file's age
                # crosses the bot's 48 h kill switch and every score comes back
                # STALE_MODEL / 0.5 while freshly trained family models sit
                # unused beside it.
                try:
                    newest = max((p.stat().st_mtime for p in MODELS_DIR.glob(f"model_{scanner}*.joblib")), default=None)
                    if newest is not None:
                        entry["age_hours"] = round((now_ts - newest) / 3600, 1)
                        entry["newest_variant_age_hours"] = entry["age_hours"]
                        entry["model_file_exists"] = True
                except Exception:
                    pass
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                        entry["n_features"] = meta.get("n_features")
                        entry["feature_schema_version"] = meta.get("feature_schema_version", "unknown")
                        entry["feature_schema_hash"] = meta.get("feature_schema_hash", "")
                        entry["has_htf_features"] = meta.get("has_htf_features", False)
                        entry["trainer_phase"] = meta.get("trainer_phase", "unknown")
                        entry["trained_at"] = meta.get("trained_at")
                    except Exception as e:
                        entry["meta_error"] = str(e)

                # Classify health state
                if not entry["model_file_exists"]:
                    entry["health"] = "MISSING"
                elif not entry["meta_file_exists"]:
                    entry["health"] = "META_MISSING"
                elif entry.get("age_hours", 0) > 72:
                    entry["health"] = "STALE"
                elif not entry.get("has_htf_features", False):
                    entry["health"] = "PRE_4.1A"  # older model without HTF fusion
                else:
                    entry["health"] = "OK"

                scanner_status[scanner] = entry

            # Global schema file
            global_schema = None
            global_schema_path = MODELS_DIR / "feature_schema.json"
            if global_schema_path.exists():
                try:
                    global_schema = json.loads(global_schema_path.read_text())
                except Exception as e:
                    global_schema = {"error": str(e)}

            # Aggregate summary
            total = len(scanner_status)
            ok_count = sum(1 for s in scanner_status.values() if s.get("health") == "OK")
            missing_count = sum(1 for s in scanner_status.values() if s.get("health") == "MISSING")
            stale_count = sum(1 for s in scanner_status.values() if s.get("health") == "STALE")
            pre_htf_count = sum(1 for s in scanner_status.values() if s.get("health") == "PRE_4.1A")

            overall_health = "OK"
            if missing_count == total:
                overall_health = "CRITICAL_ALL_MISSING"
            elif missing_count > 0:
                overall_health = "DEGRADED_SOME_MISSING"
            elif pre_htf_count > 0:
                overall_health = "NEEDS_RETRAIN_PRE_HTF"
            elif stale_count > total / 2:
                overall_health = "STALE"

            return web.json_response({
                "overall_health": overall_health,
                "summary": {
                    "total_expected": total,
                    "ok": ok_count,
                    "missing": missing_count,
                    "stale": stale_count,
                    "pre_htf_fusion": pre_htf_count,
                },
                "scanners": scanner_status,
                "global_schema": global_schema,
                "models_dir": str(MODELS_DIR),
                "loaded_in_memory": list(self._loaded_models.keys()),
                "_freshness": _freshness(),
            }, dumps=_json_dumps)
        except Exception as e:
            logger.exception("ML health check error: %s", e)
            return _error_response("ml_health", str(e))

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
    #  Phase B.7 — Edge verdict trend over time per scanner
    # -------------------------------------------------------------------
    async def _handle_ml_edge_verdict_trend(self, request):
        """Display-ready series of edge_verdict per scanner over recent retrains.

        Reads ml_model_history.jsonl (written by _ModelHistoryTracker on every
        training run). Groups by scanner + returns a time series of:
          ts, edge_verdict, oos_mean, overfit_gap, in_sample_mean

        Query params:
          scanner  — filter to one scanner (default: return all)
          last_n   — max records per scanner (default: 30)
        """
        scanner_filter = request.query.get("scanner")
        try:
            last_n = int(request.query.get("last_n", "30"))
        except ValueError:
            last_n = 30

        hist_file = MODEL_HISTORY_FILE
        if not hist_file.exists():
            return web.json_response({
                "scanners": {},
                "error": "no history file yet",
                "_freshness": _freshness(),
            }, dumps=_json_dumps)

        from collections import defaultdict
        series: Dict[str, List[Dict]] = defaultdict(list)
        try:
            with open(hist_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    sc = rec.get("scanner", "?")
                    if scanner_filter and sc != scanner_filter:
                        continue
                    series[sc].append({
                        "ts": rec.get("ts"),
                        "edge_verdict": rec.get("edge_verdict", "UNCLEAR"),
                        "oos_mean": rec.get("oos_mean", 0.0),
                        "oos_std": rec.get("oos_std", 0.0),
                        "in_sample_mean": rec.get("in_sample_mean", 0.0),
                        "overfit_gap": rec.get("overfit_gap", 0.0),
                        "auc": rec.get("auc", 0.0),
                        "n_samples": rec.get("n_samples", 0),
                        "purge_gap_bars": rec.get("purge_gap_bars", 0),
                    })
        except Exception as e:
            return _error_response("edge_verdict_trend", f"read failed: {e}")

        # Sort each series by timestamp + truncate to last_n
        for sc in series:
            series[sc].sort(key=lambda r: r.get("ts", ""))
            if last_n > 0:
                series[sc] = series[sc][-last_n:]

        # Per-scanner summary (latest verdict + trend direction)
        summaries = {}
        for sc, rows in series.items():
            if not rows:
                continue
            latest = rows[-1]
            # Trend: compare last 3 vs prior 3 OOS means
            if len(rows) >= 6:
                recent = sum(r.get("oos_mean", 0) for r in rows[-3:]) / 3
                prior = sum(r.get("oos_mean", 0) for r in rows[-6:-3]) / 3
                trend_delta = round(recent - prior, 4)
                if trend_delta > 0.02:
                    trend_dir = "IMPROVING"
                elif trend_delta < -0.02:
                    trend_dir = "DEGRADING"
                else:
                    trend_dir = "STABLE"
            else:
                trend_delta = 0.0
                trend_dir = "INSUFFICIENT_HISTORY"
            summaries[sc] = {
                "latest_verdict": latest.get("edge_verdict"),
                "latest_oos": latest.get("oos_mean"),
                "latest_overfit_gap": latest.get("overfit_gap"),
                "trend_direction": trend_dir,
                "trend_delta": trend_delta,
                "n_runs": len(rows),
            }

        return web.json_response({
            "scanners": dict(series),
            "summaries": summaries,
            "filter": {"scanner": scanner_filter, "last_n": last_n},
            "_freshness": _freshness(),
        }, dumps=_json_dumps)

    # -------------------------------------------------------------------
    #  Phase B.8 — Family × scanner verdict matrix (display-ready grid)
    # -------------------------------------------------------------------
    async def _handle_ml_family_verdict_matrix(self, request):
        """Build a 3×N verdict grid: scanner name × family → verdict/oos/overfit.

        Reads per-family model meta files directly from storage/ml_models/.
        Returns a grid keyed [scanner][family] → {verdict, oos_mean, overfit_gap,
        n_features, trained_at}.

        This is the one-screen "which models are holding, which are drifting"
        visualization the user wants as a dashboard card.
        """
        try:
            from ml_training.candidate_trainer import PAIR_FAMILIES
        except Exception as e:
            return _error_response("family_verdict_matrix", f"PAIR_FAMILIES import failed: {e}")

        families = list(PAIR_FAMILIES.keys())

        # Collect all per-family model meta files
        grid: Dict[str, Dict[str, Dict]] = {}  # scanner -> family -> meta
        row_summary: Dict[str, Dict[str, int]] = {}  # scanner -> verdict counts
        col_summary: Dict[str, Dict[str, int]] = {}  # family -> verdict counts

        if not MODELS_DIR.exists():
            return web.json_response({
                "families": families,
                "grid": {},
                "row_summary": {},
                "col_summary": {},
                "error": "models dir not found",
                "_freshness": _freshness(),
            }, dumps=_json_dumps)

        for meta_path in MODELS_DIR.glob("model_*_family_*_features.json"):
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                continue
            scanner = meta.get("scanner")
            family = meta.get("family")
            if not scanner or not family:
                continue
            grid.setdefault(scanner, {})[family] = {
                "edge_verdict": meta.get("edge_verdict", "UNCLEAR"),
                "oos_mean": meta.get("oos_mean"),
                "overfit_gap": meta.get("overfit_gap"),
                "n_features": meta.get("n_features"),
                "trained_at": meta.get("trained_at"),
                "has_htf_features": meta.get("has_htf_features", False),
                "trainer_phase": meta.get("trainer_phase", "unknown"),
            }
            # Tally row (scanner) + col (family)
            _v = meta.get("edge_verdict", "UNCLEAR")
            row_summary.setdefault(scanner, {}).setdefault(_v, 0)
            row_summary[scanner][_v] += 1
            col_summary.setdefault(family, {}).setdefault(_v, 0)
            col_summary[family][_v] += 1

        # Add per-scanner fallback (family="-") — the scanner-scope model
        for meta_path in MODELS_DIR.glob("model_*_features.json"):
            if "_family_" in meta_path.name:
                continue  # handled above
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                continue
            scanner = meta.get("scanner")
            if not scanner:
                continue
            grid.setdefault(scanner, {})["__scanner__"] = {
                "edge_verdict": meta.get("edge_verdict", "UNCLEAR"),
                "oos_mean": meta.get("oos_mean"),
                "overfit_gap": meta.get("overfit_gap"),
                "n_features": meta.get("n_features"),
                "trained_at": meta.get("trained_at"),
                "has_htf_features": meta.get("has_htf_features", False),
                "trainer_phase": meta.get("trainer_phase", "unknown"),
            }

        # Global tally
        global_tally: Dict[str, int] = {}
        for scanner_row in grid.values():
            for cell in scanner_row.values():
                v = cell.get("edge_verdict", "UNCLEAR")
                global_tally[v] = global_tally.get(v, 0) + 1

        return web.json_response({
            "families": families,
            "scanners": sorted(grid.keys()),
            "grid": grid,
            "row_summary": row_summary,
            "col_summary": col_summary,
            "global_tally": global_tally,
            "total_cells": sum(len(v) for v in grid.values()),
            "_freshness": _freshness(),
        }, dumps=_json_dumps)

    # -------------------------------------------------------------------
    #  Phase 4.6 — Live calibration (closed-loop learning diagnostics)
    # -------------------------------------------------------------------
    async def _handle_ml_live_calibration(self, request):
        """Per-(scanner, family) predicted-vs-realized calibration.

        Reads ml_live_feedback.jsonl and aggregates trades by
        (resolved_scope, resolved_family or "-", scanner). For each bucket:
          - n trades
          - avg ml_probability (what we predicted)
          - realized win rate (mfe_r >= 0.3R or exit_r > 0)
          - realized mean R
          - calibration_error = |avg_pred - realized_wr|
          - edge_delta = realized_wr - 50%  (raw edge)
          - verdict: HOLDS / WEAK / NO_EDGE / DRIFTING

        Query params:
          last_n=200     — window size (default 200 trades)
          min_n=20       — minimum trades for a bucket to be evaluated
        """
        try:
            last_n = int(request.query.get("last_n", "200"))
            min_n = int(request.query.get("min_n", "20"))
        except ValueError:
            return _error_response("ml_live_calibration", "invalid last_n / min_n", 400)

        feedback_file = FEEDBACK_FILE
        if not feedback_file.exists():
            return web.json_response({
                "buckets": [],
                "overall": {},
                "window": last_n,
                "error": "no feedback file yet",
                "_freshness": _freshness(),
            }, dumps=_json_dumps)

        # Read last N lines
        try:
            lines = feedback_file.read_text().splitlines()
        except Exception as e:
            return _error_response("ml_live_calibration", f"read failed: {e}")
        lines = [ln for ln in lines if ln.strip()]
        if last_n > 0:
            lines = lines[-last_n:]

        # Aggregate by (scope, family, scanner)
        from collections import defaultdict
        buckets: Dict[tuple, Dict] = defaultdict(lambda: {
            "n": 0,
            "sum_pred": 0.0,
            "sum_exit_r": 0.0,
            "sum_mfe_r": 0.0,
            "wins": 0,         # mfe_r >= 0.3 (matches Phase 4.4 MFE threshold)
            "exit_wins": 0,    # exit_r > 0 (realized PnL win)
            "sum_match_pct": 0.0,
            "first_ts": None,
            "last_ts": None,
        })

        total = 0
        skipped = 0
        # Phase 4.8: also tally per-verdict (HOLDS/WEAK/UNCLEAR/None)
        verdict_tally: Dict[str, Dict] = defaultdict(lambda: {
            "n": 0, "wins": 0, "sum_pred": 0.0, "sum_exit_r": 0.0, "sum_mfe_r": 0.0,
        })

        for ln in lines:
            try:
                rec = json.loads(ln)
            except Exception:
                skipped += 1
                continue
            if rec.get("exit_price", 0) in (0, None):
                skipped += 1  # not a closed trade
                continue
            scope = rec.get("ml_resolved_scope", "scanner")
            family = rec.get("ml_resolved_family") or "-"
            scanner = rec.get("setup_type") or rec.get("scanner") or "?"
            key = (scope, family, scanner)
            b = buckets[key]
            b["n"] += 1
            b["sum_pred"] += float(rec.get("ml_probability", 0.5) or 0.5)
            b["sum_exit_r"] += float(rec.get("exit_r", 0) or 0)
            b["sum_mfe_r"] += float(rec.get("mfe_r", 0) or 0)
            if float(rec.get("mfe_r", 0) or 0) >= 0.3:
                b["wins"] += 1
            if float(rec.get("exit_r", 0) or 0) > 0:
                b["exit_wins"] += 1
            b["sum_match_pct"] += float(rec.get("ml_match_pct", 1.0) or 1.0)
            ts = rec.get("timestamp")
            if ts:
                if b["first_ts"] is None or ts < b["first_ts"]:
                    b["first_ts"] = ts
                if b["last_ts"] is None or ts > b["last_ts"]:
                    b["last_ts"] = ts
            total += 1

            # Phase 4.8: verdict-level tally
            _v = rec.get("ml_edge_verdict") or "UNKNOWN"
            vb = verdict_tally[_v]
            vb["n"] += 1
            vb["sum_pred"] += float(rec.get("ml_probability", 0.5) or 0.5)
            vb["sum_exit_r"] += float(rec.get("exit_r", 0) or 0)
            vb["sum_mfe_r"] += float(rec.get("mfe_r", 0) or 0)
            if float(rec.get("mfe_r", 0) or 0) >= 0.3:
                vb["wins"] += 1

        # Build response
        out_buckets = []
        for (scope, family, scanner), b in buckets.items():
            n = b["n"]
            if n == 0:
                continue
            avg_pred = b["sum_pred"] / n
            realized_wr = b["wins"] / n
            realized_exit_wr = b["exit_wins"] / n
            avg_exit_r = b["sum_exit_r"] / n
            avg_mfe_r = b["sum_mfe_r"] / n
            avg_match = b["sum_match_pct"] / n
            # calibration error = |pred - realized|, only meaningful if n >= min_n
            calibration_error = abs(avg_pred - realized_wr)
            edge_delta_pct = (realized_wr - 0.5) * 100

            # Live-calibration vocabulary (distinct from the score verdicts
            # STRONG_TAKE/TAKE/MARGINAL/SKIP and the training edge_verdicts
            # HOLDS/WEAK/UNCLEAR/NO_EDGE): CALIBRATED, THIN_EDGE, DRIFTING,
            # NO_EDGE. auto_retrainer keys on DRIFTING / NO_EDGE.
            if n < min_n:
                verdict = "INSUFFICIENT_DATA"
            elif calibration_error <= 0.08 and realized_wr >= 0.55:
                verdict = "CALIBRATED"
            elif realized_wr >= 0.52:
                verdict = "THIN_EDGE"
            elif calibration_error > 0.15:
                verdict = "DRIFTING"
            else:
                verdict = "NO_EDGE"

            out_buckets.append({
                "scope": scope,
                "family": family,
                "scanner": scanner,
                "n": n,
                "avg_ml_probability": round(avg_pred, 4),
                "realized_mfe_wr": round(realized_wr, 4),
                "realized_exit_wr": round(realized_exit_wr, 4),
                "avg_exit_r": round(avg_exit_r, 4),
                "avg_mfe_r": round(avg_mfe_r, 4),
                "calibration_error": round(calibration_error, 4),
                "edge_delta_pct": round(edge_delta_pct, 2),
                "avg_match_pct": round(avg_match, 4),
                "verdict": verdict,
                "first_ts": b["first_ts"],
                "last_ts": b["last_ts"],
            })

        # Sort: family models first, then by n desc
        out_buckets.sort(key=lambda r: (r["scope"] != "family", -r["n"]))

        # Overall aggregate
        if total > 0:
            total_pred = sum(b["sum_pred"] for b in buckets.values())
            total_mfe_wins = sum(b["wins"] for b in buckets.values())
            overall = {
                "n": total,
                "avg_ml_probability": round(total_pred / total, 4),
                "realized_mfe_wr": round(total_mfe_wins / total, 4),
                "calibration_error": round(abs((total_pred / total) - (total_mfe_wins / total)), 4),
                "skipped": skipped,
                "window": last_n,
            }
        else:
            overall = {"n": 0, "skipped": skipped, "window": last_n}

        # Phase 4.8: per-verdict roll-up (HOLDS / WEAK / UNCLEAR / UNKNOWN)
        verdict_rollup = []
        _verdict_order = ["HOLDS", "WEAK", "UNCLEAR", "NO_EDGE", "UNKNOWN"]
        for _v in _verdict_order:
            vb = verdict_tally.get(_v)
            if not vb or vb["n"] == 0:
                continue
            n = vb["n"]
            avg_pred = vb["sum_pred"] / n
            realized_wr = vb["wins"] / n
            avg_exit_r = vb["sum_exit_r"] / n
            avg_mfe_r = vb["sum_mfe_r"] / n
            cal_err = abs(avg_pred - realized_wr)
            verdict_rollup.append({
                "edge_verdict": _v,
                "n": n,
                "avg_ml_probability": round(avg_pred, 4),
                "realized_mfe_wr": round(realized_wr, 4),
                "avg_exit_r": round(avg_exit_r, 4),
                "avg_mfe_r": round(avg_mfe_r, 4),
                "calibration_error": round(cal_err, 4),
            })

        return web.json_response({
            "buckets": out_buckets,
            "overall": overall,
            "by_verdict": verdict_rollup,  # Phase 4.8
            "min_n_for_verdict": min_n,
            "_freshness": _freshness(
                data_through=overall.get("last_ts"),
                record_count=total,
            ),
        }, dumps=_json_dumps)

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
    #  NEW: Backtest All — comprehensive per-pair results
    # -------------------------------------------------------------------
    async def _handle_backtest_all(self, request):
        """Return structured backtest results for ALL pairs with per-pair aggregation.

        Groups results by symbol, scanner, and timeframe with summary stats.
        """
        try:
            # Load all backtest result files
            all_results = []
            errors = []
            if RESULTS_DIR.exists():
                for f in sorted(RESULTS_DIR.glob("*.json")):
                    try:
                        data = json.loads(f.read_text())
                        symbol_raw = data.get("symbol", "")
                        parts = symbol_raw.rsplit("_", 1)
                        symbol = parts[0] if len(parts) > 1 else symbol_raw
                        timeframe = parts[1] if len(parts) > 1 else "?"
                        scanner = data.get("scanner", "?")
                        metrics = data.get("metrics", {})
                        wf = data.get("walk_forward", {})
                        trades_list = data.get("trades", [])

                        all_results.append({
                            "key": f.stem,
                            "scanner": scanner,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "trades": metrics.get("trades", len(trades_list)),
                            "win_rate": metrics.get("win_rate", 0),
                            "expectancy_r": metrics.get("expectancy_r", 0),
                            "profit_factor": metrics.get("profit_factor", 0),
                            "avg_win_r": metrics.get("avg_win_r", 0),
                            "avg_loss_r": metrics.get("avg_loss_r", 0),
                            "max_drawdown_r": metrics.get("max_drawdown_r", 0),
                            "sharpe": metrics.get("sharpe", 0),
                            "wf_verdict": wf.get("verdict", "?") if wf else "?",
                            "wf_edge_holds_pct": wf.get("edge_holds_pct", 0) if wf else 0,
                        })
                    except Exception as e:
                        errors.append(f"{f.name}: {e}")

            if not all_results:
                return web.json_response({
                    "by_pair": {},
                    "by_scanner": {},
                    "summary": {},
                    "all_results": [],
                    "_freshness": _freshness(),
                    "_warnings": errors[:5] if errors else [],
                })

            # Group by symbol
            by_pair = {}
            for r in all_results:
                sym = r["symbol"]
                if sym not in by_pair:
                    by_pair[sym] = {
                        "symbol": sym,
                        "results": [],
                        "total_trades": 0,
                        "avg_wr": 0,
                        "avg_exp": 0,
                        "best_setup": None,
                        "worst_setup": None,
                        "valid_edges": 0,
                        "total_setups": 0,
                    }
                by_pair[sym]["results"].append(r)
                by_pair[sym]["total_trades"] += r["trades"]
                by_pair[sym]["total_setups"] += 1
                if r["wf_verdict"] == "VALID EDGE":
                    by_pair[sym]["valid_edges"] += 1

            # Compute per-pair aggregates
            for sym, pdata in by_pair.items():
                results = pdata["results"]
                n = len(results)
                pdata["avg_wr"] = round(sum(r["win_rate"] for r in results) / n, 1) if n else 0
                pdata["avg_exp"] = round(sum(r["expectancy_r"] for r in results) / n, 3) if n else 0
                # Best & worst by expectancy
                sorted_r = sorted(results, key=lambda x: x["expectancy_r"], reverse=True)
                if sorted_r:
                    best = sorted_r[0]
                    pdata["best_setup"] = f"{best['scanner']} {best['timeframe']} ({best['expectancy_r']:.3f}R)"
                    worst = sorted_r[-1]
                    pdata["worst_setup"] = f"{worst['scanner']} {worst['timeframe']} ({worst['expectancy_r']:.3f}R)"

            # Group by scanner
            by_scanner = {}
            for r in all_results:
                s = r["scanner"]
                if s not in by_scanner:
                    by_scanner[s] = {"scanner": s, "pairs_tested": set(), "total_trades": 0,
                                     "wr_sum": 0, "exp_sum": 0, "count": 0, "valid_edges": 0}
                by_scanner[s]["pairs_tested"].add(r["symbol"])
                by_scanner[s]["total_trades"] += r["trades"]
                by_scanner[s]["wr_sum"] += r["win_rate"]
                by_scanner[s]["exp_sum"] += r["expectancy_r"]
                by_scanner[s]["count"] += 1
                if r["wf_verdict"] == "VALID EDGE":
                    by_scanner[s]["valid_edges"] += 1

            for s, sdata in by_scanner.items():
                n = sdata["count"]
                sdata["pairs_tested"] = len(sdata["pairs_tested"])
                sdata["avg_wr"] = round(sdata["wr_sum"] / n, 1) if n else 0
                sdata["avg_exp"] = round(sdata["exp_sum"] / n, 3) if n else 0
                del sdata["wr_sum"], sdata["exp_sum"]

            # Global summary
            n_total = len(all_results)
            summary = {
                "total_setups": n_total,
                "total_pairs": len(by_pair),
                "total_scanners": len(by_scanner),
                "total_trades": sum(r["trades"] for r in all_results),
                "avg_win_rate": round(sum(r["win_rate"] for r in all_results) / n_total, 1) if n_total else 0,
                "avg_expectancy": round(sum(r["expectancy_r"] for r in all_results) / n_total, 3) if n_total else 0,
                "valid_edges": sum(1 for r in all_results if r["wf_verdict"] == "VALID EDGE"),
                "valid_edge_pct": round(sum(1 for r in all_results if r["wf_verdict"] == "VALID EDGE") / n_total * 100, 1) if n_total else 0,
            }

            # Build pair x scanner heatmap data
            heatmap = {}
            scanners_list = sorted(by_scanner.keys())
            for r in all_results:
                key = f"{r['symbol']}|{r['scanner']}|{r['timeframe']}"
                heatmap[key] = {
                    "symbol": r["symbol"],
                    "scanner": r["scanner"],
                    "timeframe": r["timeframe"],
                    "expectancy_r": r["expectancy_r"],
                    "win_rate": r["win_rate"],
                    "trades": r["trades"],
                    "wf_verdict": r["wf_verdict"],
                }

            return web.json_response(_sanitize_json({
                "by_pair": {k: {**v, "results": v["results"]} for k, v in by_pair.items()},
                "by_scanner": by_scanner,
                "summary": summary,
                "heatmap": list(heatmap.values()),
                "scanners": scanners_list,
                "all_results": all_results,
                "_freshness": _freshness(extra={"result_count": n_total}),
                "_warnings": errors[:5] if errors else [],
            }), dumps=_json_dumps)

        except Exception as e:
            logger.exception("Backtest-all error: %s", e)
            return _error_response("backtest_all", str(e))

    # -------------------------------------------------------------------
    #  Research Center — "ML as innovation lab" (2026-04-17)
    # -------------------------------------------------------------------
    # These handlers delegate to ml_training/research_center.py which
    # runs the analyses in a background scheduler and caches results.
    # User-facing latency is <50ms because handlers only read the cache.

    def _research(self):
        """Lazy-import the research center singleton (avoid import-cycle at module load)."""
        try:
            from ml_training.research_center import get_center
            return get_center()
        except Exception as e:
            logger.warning("research_center unavailable: %s", e)
            return None

    def _cached_or_live(self, key: str, fallback):
        """Return cached if fresh, else compute live (slower path)."""
        rc = self._research()
        if rc is not None:
            cached = rc.get(key)
            if cached is not None:
                return cached
        try:
            return fallback()
        except Exception as e:
            logger.warning("research %s live compute failed: %s", key, e)
            return {"error": str(e)}

    async def _handle_research_cohort_health(self, request):
        from ml_training.research_center import analyze_cohort_health
        return web.json_response(
            self._cached_or_live("cohort_health", analyze_cohort_health),
            dumps=_json_dumps,
        )

    async def _handle_research_weakspots(self, request):
        from ml_training.research_center import mine_weakspots
        days = int(request.query.get("days", 30))
        min_n = int(request.query.get("min_n", 30))
        # Use cache for default params only; live-compute for custom
        if days == 30 and min_n == 30:
            return web.json_response(
                self._cached_or_live("weakspots", lambda: mine_weakspots(days, min_n)),
                dumps=_json_dumps,
            )
        return web.json_response(mine_weakspots(days, min_n), dumps=_json_dumps)

    async def _handle_research_policy_variants(self, request):
        from ml_training.research_center import propose_policy_variants
        return web.json_response(
            self._cached_or_live("policy_variants", lambda: propose_policy_variants(30)),
            dumps=_json_dumps,
        )

    async def _handle_research_edge_trajectory(self, request):
        from ml_training.research_center import edge_trajectory
        days = int(request.query.get("days", 14))
        bucket_hours = int(request.query.get("bucket_hours", 6))
        if days == 14 and bucket_hours == 6:
            return web.json_response(
                self._cached_or_live("edge_trajectory", lambda: edge_trajectory(days, bucket_hours)),
                dumps=_json_dumps,
            )
        return web.json_response(edge_trajectory(days, bucket_hours), dumps=_json_dumps)

    async def _handle_research_suggestions(self, request):
        from ml_training.research_center import scan_suggestions
        return web.json_response(scan_suggestions(), dumps=_json_dumps)

    async def _handle_research_vetoes(self, request):
        from ml_training.research_center import active_vetoes
        return web.json_response(active_vetoes(), dumps=_json_dumps)

    async def _handle_research_timeline(self, request):
        from ml_training.research_center import timeline
        limit = int(request.query.get("limit", 50))
        return web.json_response({"events": timeline(limit), "count": 0}, dumps=_json_dumps)

    async def _handle_research_refresh(self, request):
        """POST — force refresh all research caches. Admin-triggered."""
        rc = self._research()
        if rc is None:
            return web.json_response({"error": "research_center not available"}, status=503)
        try:
            await asyncio.to_thread(rc.refresh_all, True)
            return web.json_response({"status": "ok", "refreshed_at": datetime.now(timezone.utc).isoformat()})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_research_summary(self, request):
        """Headline summary for the Research tab landing view."""
        from ml_training.research_center import (
            analyze_cohort_health, mine_weakspots, scan_suggestions,
            active_vetoes, scanner_registry, active_promotions,
            propose_scanner_promotions,
        )
        health = self._cached_or_live("cohort_health", analyze_cohort_health)
        weak = self._cached_or_live("weakspots", lambda: mine_weakspots(30, 30))
        sug = scan_suggestions()
        vetoes = active_vetoes()
        registry = self._cached_or_live("scanner_registry", scanner_registry)
        promo_candidates = self._cached_or_live("scanner_promotions", propose_scanner_promotions)
        promos = active_promotions()

        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cohort_health": {
                "healthy": (health or {}).get("healthy_count", 0),
                "degraded": (health or {}).get("degraded_count", 0),
                "alerts": (health or {}).get("alerts", [])[:5],
            },
            "weakspots": {
                "count": len((weak or {}).get("weakspots", [])),
                "top_3": (weak or {}).get("weakspots", [])[:3],
                "baseline_wr": (weak or {}).get("baseline_wr", 0),
            },
            "suggestions": {
                "pending": sug.get("count", 0),
            },
            "vetoes": {
                "active": vetoes.get("count", 0),
            },
            "scanner_registry": {
                "loaded": (registry or {}).get("loaded", False),
                "stale_seconds": (registry or {}).get("stale_seconds"),
                "summary": (registry or {}).get("summary", {}),
            },
            "promotions": {
                "candidates": (promo_candidates or {}).get("count", 0),
                "active": promos.get("count", 0),
            },
        }
        return web.json_response(summary, dumps=_json_dumps)

    # -------------------------------------------------------------------
    #  Research UI page + mutating endpoints
    # -------------------------------------------------------------------
    async def _handle_research_page(self, request):
        """GET /research — serve the Research Center HTML page."""
        template_path = PROJECT_ROOT / "ml_training" / "templates" / "research.html"
        if not template_path.exists():
            return web.Response(
                text="<h1>Research page not deployed</h1>",
                content_type="text/html", status=503,
            )
        try:
            return web.Response(
                body=template_path.read_bytes(),
                content_type="text/html",
                headers={"Cache-Control": "no-cache"},
            )
        except Exception as e:
            return web.Response(text=f"Error: {e}", status=500)

    def _research_file(self, name: str):
        """Path helper — storage/research/{name}."""
        from pathlib import Path as _P
        d = PROJECT_ROOT / "storage" / "research"
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    def _read_json_or_default(self, path, default):
        """Safe JSON read — returns default if missing/corrupt."""
        import json as _j
        try:
            if not path.exists():
                return default
            with open(path) as fh:
                return _j.load(fh)
        except Exception:
            return default

    def _write_json_atomic(self, path, data):
        """Atomic write via tmp+rename."""
        import json as _j, os as _os
        tmp = str(path) + ".tmp"
        with open(tmp, "w") as fh:
            _j.dump(data, fh, indent=2, default=str)
        _os.replace(tmp, path)

    def _append_event(self, kind: str, payload: dict):
        """Emit to storage/research/events.jsonl (same format as research_center)."""
        import json as _j
        path = self._research_file("events.jsonl")
        evt = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **payload}
        try:
            with open(path, "a") as fh:
                fh.write(_j.dump(evt, default=str) if False else "")
                # use json.dumps properly
                fh.write(_j.dumps(evt, default=str) + "\n")
        except Exception as e:
            logger.warning("append_event failed: %s", e)

    async def _handle_research_suggestions_approve(self, request):
        """POST /api/research/suggestions/approve — record an approved variant
        for later shadow-mode enforcement by the bot's prefilter adapter.

        Stored in storage/research/approved_variants.json. NEVER auto-enforced.
        Phase-1 contract: bot reads this file and LOGS what it would do
        (vwap_would_veto style), but does not change trading behavior
        until a follow-up commit flips the enforce flag.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        path = self._research_file("approved_variants.json")
        store = self._read_json_or_default(path, {"variants": [], "updated_at": None})
        body.setdefault("approved_at", datetime.now(timezone.utc).isoformat())
        body.setdefault("mode", "shadow")
        body["id"] = f"var_{int(datetime.now(timezone.utc).timestamp())}_{len(store['variants'])}"
        store["variants"].append(body)
        store["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_json_atomic(path, store)
        self._append_event("variant_approved", {
            "id": body["id"],
            "cohort": body.get("cohort"),
            "params": body.get("params"),
            "mode": body.get("mode"),
        })
        return web.json_response({"status": "ok", "id": body["id"], "mode": "shadow"})

    async def _handle_research_suggestions_reject(self, request):
        """POST /api/research/suggestions/reject — record rejection reason."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        path = self._research_file("rejected_variants.jsonl")
        body["rejected_at"] = datetime.now(timezone.utc).isoformat()
        import json as _j
        with open(path, "a") as fh:
            fh.write(_j.dumps(body, default=str) + "\n")
        self._append_event("variant_rejected", {
            "cohort": body.get("cohort"),
            "reason": body.get("reason", "unspecified"),
        })
        return web.json_response({"status": "ok"})

    async def _handle_research_vetoes_add(self, request):
        """POST /api/research/vetoes/add — freeze a cohort.

        Phase-1 SHADOW MODE: writes to storage/research/active_vetoes.json
        where the bot's prefilter adapter reads it. Currently the adapter
        only LOGS would_veto — doesn't actually block. Enforcement
        flip is a separate commit after observation.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        scanner = body.get("scanner")
        regime = body.get("regime")
        side = body.get("side")
        if not (scanner and regime and side):
            return web.json_response({"error": "scanner, regime, side required"}, status=400)

        path = self._research_file("active_vetoes.json")
        store = self._read_json_or_default(path, {"vetoes": [], "last_updated": None})
        # Avoid duplicates
        key = (scanner, regime, side)
        already = any(
            (v.get("scanner"), v.get("regime"), v.get("side")) == key
            for v in store["vetoes"]
        )
        if already:
            return web.json_response({"status": "already_exists"}, status=200)

        store["vetoes"].append({
            "scanner": scanner, "regime": regime, "side": side,
            "reason": body.get("reason", "manual"),
            "mode": body.get("mode", "shadow"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        store["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._write_json_atomic(path, store)
        self._append_event("veto_added", {
            "scanner": scanner, "regime": regime, "side": side,
            "mode": body.get("mode", "shadow"),
        })
        return web.json_response({"status": "ok", "mode": body.get("mode", "shadow")})

    async def _handle_research_vetoes_remove(self, request):
        """POST /api/research/vetoes/remove — unfreeze a cohort by index."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        idx = body.get("index")
        if not isinstance(idx, int):
            return web.json_response({"error": "index (int) required"}, status=400)

        path = self._research_file("active_vetoes.json")
        store = self._read_json_or_default(path, {"vetoes": [], "last_updated": None})
        if idx < 0 or idx >= len(store["vetoes"]):
            return web.json_response({"error": "index out of range"}, status=400)

        removed = store["vetoes"].pop(idx)
        store["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._write_json_atomic(path, store)
        self._append_event("veto_removed", {
            "scanner": removed.get("scanner"),
            "regime": removed.get("regime"),
            "side": removed.get("side"),
        })
        return web.json_response({"status": "ok", "removed": removed})

    # -------------------------------------------------------------------
    #  Scanner Registry + Promotions (2026-04-17)
    # -------------------------------------------------------------------
    async def _handle_research_scanner_registry(self, request):
        """GET /api/research/scanner-registry — live per-scanner status from
        scanner_weights.json (synced from trading VM).  Returns rich table
        data for the UI.  Also triggers emit of scanner_status_change events
        if any statuses have transitioned since the last read."""
        from ml_training.research_center import scanner_registry
        return web.json_response(
            self._cached_or_live("scanner_registry", scanner_registry),
            dumps=_json_dumps,
        )

    async def _handle_research_promotion_candidates(self, request):
        """GET /api/research/promotion-candidates — Research Center's auto
        proposals for cohort-level promotions of globally-penalised scanners."""
        from ml_training.research_center import propose_scanner_promotions
        min_n = int(request.query.get("min_n", 15))
        gap_pp = float(request.query.get("gap_pp", 5.0))
        days = int(request.query.get("days", 30))
        # Default-param path is cached; custom params compute live
        if min_n == 15 and gap_pp == 5.0 and days == 30:
            return web.json_response(
                self._cached_or_live("scanner_promotions", propose_scanner_promotions),
                dumps=_json_dumps,
            )
        return web.json_response(
            propose_scanner_promotions(min_n=min_n, edge_gap_pp=gap_pp, days=days),
            dumps=_json_dumps,
        )

    async def _handle_research_promotions(self, request):
        """GET /api/research/promotions — list of APPROVED cohort-level promotions
        currently in effect (what the bot reads via its mtime-poll adapter).

        Response schema mirrors /api/research/vetoes:
            {"promotions": [...], "count": N, "last_updated": "..."}
        """
        from ml_training.research_center import active_promotions
        return web.json_response(active_promotions(), dumps=_json_dumps)

    async def _handle_research_promotions_approve(self, request):
        """POST /api/research/promotions/approve — human-in-loop approval of a
        cohort-level scanner promotion. Writes to active_promotions.json
        where the bot's scalp_strategy shadow adapter reads.

        Body: {scanner, regime, side, [session], [weight], [mode], [reason]}
        mode defaults to "shadow"; bot logs would_boost only until mode=="enforce".
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        scanner = body.get("scanner")
        regime = body.get("regime")
        side = body.get("side")
        if not (scanner and regime and side):
            return web.json_response(
                {"error": "scanner, regime, side required"}, status=400,
            )

        path = self._research_file("active_promotions.json")
        store = self._read_json_or_default(path, {"promotions": [], "last_updated": None})
        key = (scanner, regime, side, body.get("session") or "")
        already = any(
            (p.get("scanner"), p.get("regime"), p.get("side"), p.get("session") or "") == key
            for p in store["promotions"]
        )
        if already:
            return web.json_response({"status": "already_exists"}, status=200)

        entry = {
            "scanner": scanner,
            "regime": regime,
            "side": side,
            "session": body.get("session"),
            "weight": float(body.get("weight", 1.0)),
            "reason": body.get("reason", "manual approval"),
            "mode": body.get("mode", "shadow"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        store["promotions"].append(entry)
        store["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._write_json_atomic(path, store)
        self._append_event("promotion_approved", {
            "scanner": scanner, "regime": regime, "side": side,
            "session": body.get("session"), "weight": entry["weight"],
            "mode": entry["mode"],
        })
        return web.json_response({"status": "ok", "mode": entry["mode"], "entry": entry})

    async def _handle_research_promotions_remove(self, request):
        """POST /api/research/promotions/remove — unroll a promotion by index."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        idx = body.get("index")
        if not isinstance(idx, int):
            return web.json_response({"error": "index (int) required"}, status=400)

        path = self._research_file("active_promotions.json")
        store = self._read_json_or_default(path, {"promotions": [], "last_updated": None})
        if idx < 0 or idx >= len(store["promotions"]):
            return web.json_response({"error": "index out of range"}, status=400)

        removed = store["promotions"].pop(idx)
        store["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._write_json_atomic(path, store)
        self._append_event("promotion_removed", {
            "scanner": removed.get("scanner"),
            "regime": removed.get("regime"),
            "side": removed.get("side"),
            "session": removed.get("session"),
        })
        return web.json_response({"status": "ok", "removed": removed})

    # -------------------------------------------------------------------
    #  Scanner Attrition Funnel + Variants (Phase 1-3 scanner research, 2026-04-17)
    # -------------------------------------------------------------------
    async def _handle_research_scanner_funnel(self, request):
        """GET /api/research/scanner-funnel — aggregated 7d per-scanner
        attrition report (attempts, triggers, rejection reasons, failure_mode
        classification). Answers: 'for each scanner, why isn't it firing?'"""
        from ml_training.research_center import scanner_funnel_analysis
        days = int(request.query.get("days", 7))
        if days == 7:
            return web.json_response(
                self._cached_or_live("scanner_funnel", lambda: scanner_funnel_analysis(7)),
                dumps=_json_dumps,
            )
        return web.json_response(scanner_funnel_analysis(days=days), dumps=_json_dumps)

    async def _handle_research_scanner_offline(self, request):
        """GET /api/research/scanner-offline — offline diagnostic replay
        from scripts/diagnose_scanners.py. Shows theoretical vs production
        trigger rate, pinpointing where signals die in the filter stack."""
        from ml_training.research_center import scanner_offline_diagnosis
        return web.json_response(scanner_offline_diagnosis(), dumps=_json_dumps)

    async def _handle_research_scanner_variants(self, request):
        """GET /api/research/scanner-variants — Research Center's auto-proposed
        per-scanner tuning variants (threshold relax, filter exemption, etc.)
        based on funnel analysis. Humans approve via the approve endpoint."""
        from ml_training.research_center import propose_scanner_variants
        return web.json_response(
            self._cached_or_live("scanner_variants", propose_scanner_variants),
            dumps=_json_dumps,
        )

    async def _handle_research_scanner_variants_active(self, request):
        """GET /api/research/scanner-variants/active — currently-approved
        scanner tuning variants (what the bot reads via mtime poll)."""
        from ml_training.research_center import active_scanner_variants
        return web.json_response(active_scanner_variants(), dumps=_json_dumps)

    async def _handle_research_scanner_variants_approve(self, request):
        """POST /api/research/scanner-variants/approve — human-in-loop approval
        of a proposed scanner variant. Writes to storage/research/scanner_variants.json
        where the bot's shadow adapter reads. Shadow-first: logs would_fire_with_variant
        for 7d before enforcement flag flips."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        scanner = body.get("scanner")
        proposal = body.get("proposal")
        if not (scanner and proposal):
            return web.json_response({"error": "scanner + proposal required"}, status=400)

        path = self._research_file("scanner_variants.json")
        store = self._read_json_or_default(path, {"variants": [], "last_updated": None})
        entry = {
            "scanner": scanner,
            "proposal": proposal,
            "mode": body.get("mode", "shadow"),
            "reason": body.get("reason", "auto-proposed by Research Lab"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        store["variants"].append(entry)
        store["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._write_json_atomic(path, store)
        self._append_event("scanner_variant_approved", {
            "scanner": scanner,
            "proposal_type": proposal.get("type"),
            "mode": entry["mode"],
        })
        return web.json_response({"status": "ok", "mode": entry["mode"], "entry": entry})

    async def _handle_research_scanner_variants_remove(self, request):
        """POST /api/research/scanner-variants/remove — unroll by index."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        idx = body.get("index")
        if not isinstance(idx, int):
            return web.json_response({"error": "index (int) required"}, status=400)

        path = self._research_file("scanner_variants.json")
        store = self._read_json_or_default(path, {"variants": [], "last_updated": None})
        if idx < 0 or idx >= len(store["variants"]):
            return web.json_response({"error": "index out of range"}, status=400)

        removed = store["variants"].pop(idx)
        store["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._write_json_atomic(path, store)
        self._append_event("scanner_variant_removed", {"scanner": removed.get("scanner")})
        return web.json_response({"status": "ok", "removed": removed})

    async def _handle_research_scanner_observation(self, request):
        """GET /api/research/scanner-variants/observation — per-variant shadow
        observation progress (days elapsed + events count vs readiness
        criteria). Drives the UI's Enforce button state."""
        from ml_training.research_center import shadow_observation_status
        return web.json_response(shadow_observation_status(), dumps=_json_dumps)

    async def _handle_research_scanner_variants_enforce(self, request):
        """POST /api/research/scanner-variants/enforce — flip a variant's
        mode from 'shadow' to 'enforce'. Rejected if the variant isn't ready
        (hasn't accumulated enough shadow observation).

        Body: {"index": N}  -- index into active_scanner_variants list
        Optional: {"force": true}  -- override readiness check (use with care)

        Bot picks up the change within 60s via mtime poll and starts
        honoring the variant (e.g. adds scanner to allowed_scanners for
        regime_whitelist variants).
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        idx = body.get("index")
        if not isinstance(idx, int):
            return web.json_response({"error": "index (int) required"}, status=400)
        force = bool(body.get("force", False))

        # Check readiness first
        from ml_training.research_center import shadow_observation_status
        status = shadow_observation_status()
        variants = status.get("variants", [])
        target = next((v for v in variants if v.get("index") == idx), None)
        if target is None:
            return web.json_response({"error": f"no variant at index {idx}"}, status=400)
        if not target.get("ready_to_enforce") and not force:
            return web.json_response({
                "error": "not ready to enforce",
                "status": target.get("status"),
                "days_elapsed": target.get("days_elapsed"),
                "relevant_events": target.get("relevant_events"),
                "min_days": status.get("min_days"),
                "min_events": status.get("min_events"),
                "hint": "Wait until both days_elapsed and relevant_events meet thresholds, or pass force:true to override",
            }, status=409)

        path = self._research_file("scanner_variants.json")
        store = self._read_json_or_default(path, {"variants": [], "last_updated": None})
        if idx < 0 or idx >= len(store["variants"]):
            return web.json_response({"error": "index out of range"}, status=400)

        store["variants"][idx]["mode"] = "enforce"
        store["variants"][idx]["enforced_at"] = datetime.now(timezone.utc).isoformat()
        if force:
            store["variants"][idx]["force_enforced"] = True
        store["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._write_json_atomic(path, store)
        self._append_event("scanner_variant_enforced", {
            "scanner": store["variants"][idx].get("scanner"),
            "variant_type": (store["variants"][idx].get("proposal") or {}).get("type"),
            "force": force,
            "status_before": target.get("status"),
        })
        return web.json_response({
            "status": "ok",
            "mode": "enforce",
            "variant": store["variants"][idx],
            "force": force,
        })

    # -------------------------------------------------------------------
    #  Start server
    # -------------------------------------------------------------------
    async def start(self):
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._port)
        await site.start()

        # Start the Research Center background scheduler (2026-04-17)
        try:
            from ml_training.research_center import get_center
            rc = get_center()
            await rc.start()
            logger.info("Research Center scheduler started — /api/research/* endpoints live")
        except Exception as e:
            logger.warning("Research Center failed to start (endpoints will fall back to live compute): %s", e)

        logger.info("ML Dashboard v3.0 running on http://0.0.0.0:%d", self._port)
        logger.info("  New endpoints: /api/model-trend, /api/model-health, /api/feature-drift, /api/scanner-rankings")
        logger.info("  Research: /api/research/{cohort-health,weakspots,policy-variants,edge-trajectory,suggestions,vetoes,timeline,summary}")
