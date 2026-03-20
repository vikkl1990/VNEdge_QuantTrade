"""
Backtest Result Tracker
========================
Stores and compares results across training runs.
Each run is timestamped and stored, so you can track improvements over time.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TRACKER_DIR = Path(__file__).resolve().parent.parent / "storage" / "backtest_history"
TRACKER_DIR.mkdir(parents=True, exist_ok=True)
TRACKER_FILE = TRACKER_DIR / "run_history.json"


class BacktestTracker:
    """Tracks and compares backtest results across runs."""

    def __init__(self):
        self._history: List[Dict] = []
        self._load()

    def _load(self):
        if TRACKER_FILE.exists():
            try:
                self._history = json.loads(TRACKER_FILE.read_text())
            except Exception:
                self._history = []

    def _save(self):
        TRACKER_FILE.write_text(json.dumps(self._history, indent=2, default=str))

    def record_run(self, run_config: Dict, scanner_results: Dict,
                   ml_results: Dict = None, label: str = ""):
        """Record a complete training run."""
        run = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "config": run_config,
            "scanners": {},
            "ml": ml_results or {},
            "summary": {},
        }

        # Extract scanner summaries
        best_scanner = None
        best_exp = -999
        for key, data in scanner_results.items():
            metrics = data.get("metrics", data)
            scanner_name = data.get("scanner", key)
            run["scanners"][key] = {
                "scanner": scanner_name,
                "trades": metrics.get("trades", 0),
                "win_rate": metrics.get("win_rate", 0),
                "expectancy_r": metrics.get("expectancy_r", 0),
                "profit_factor": metrics.get("profit_factor", 0),
                "wf_verdict": data.get("walk_forward", {}).get("verdict", "?"),
            }
            exp = metrics.get("expectancy_r", -999)
            if exp > best_exp:
                best_exp = exp
                best_scanner = scanner_name

        # ML summary
        if ml_results:
            for key, val in ml_results.items():
                if isinstance(val, dict) and "auc_roc" in val:
                    run["ml"][key] = {
                        "auc_roc": val.get("auc_roc", 0),
                        "accuracy": val.get("accuracy", 0),
                        "wf_avg_auc": 0,
                    }
                    wf = val.get("walk_forward", [])
                    if wf:
                        run["ml"][key]["wf_avg_auc"] = round(
                            sum(f.get("auc_roc", 0) for f in wf) / len(wf), 4
                        )

        run["summary"] = {
            "total_scanners_tested": len(scanner_results),
            "best_scanner": best_scanner,
            "best_expectancy": best_exp,
            "has_edge": best_exp > 0,
        }

        self._history.append(run)
        self._save()
        logger.info("Recorded run #%d: %s", len(self._history), label)

    def get_comparison(self, last_n: int = 10) -> List[Dict]:
        """Get comparison of last N runs."""
        runs = self._history[-last_n:]
        comparison = []
        for run in runs:
            comparison.append({
                "timestamp": run["timestamp"],
                "label": run["label"],
                "config": run.get("config", {}),
                "summary": run.get("summary", {}),
                "scanner_count": len(run.get("scanners", {})),
                "ml_models": list(run.get("ml", {}).keys()),
            })
        return comparison

    def get_best_run(self) -> Optional[Dict]:
        """Get the run with the best ML walk-forward AUC."""
        if not self._history:
            return None
        best = None
        best_auc = 0
        for run in self._history:
            for key, ml in run.get("ml", {}).items():
                auc = ml.get("wf_avg_auc", 0)
                if auc > best_auc:
                    best_auc = auc
                    best = run
        return best

    def get_improvement_trend(self) -> List[Dict]:
        """Track key metrics across runs to see if improvements are real."""
        trend = []
        for i, run in enumerate(self._history):
            entry = {
                "run": i + 1,
                "timestamp": run["timestamp"],
                "label": run["label"],
                "best_scanner_exp": run.get("summary", {}).get("best_expectancy", -999),
            }
            # Add ML AUC if available
            ml = run.get("ml", {})
            if ml:
                aucs = [v.get("wf_avg_auc", 0) for v in ml.values() if isinstance(v, dict)]
                entry["best_wf_auc"] = max(aucs) if aucs else 0
            trend.append(entry)
        return trend
