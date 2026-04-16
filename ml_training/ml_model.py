"""
ML Probability Model
====================
Trains a classifier to answer:
"Given current market conditions, what is the probability this trade
will reach TP before SL within X minutes?"

Architecture:
- Random Forest classifier (works on small datasets, interpretable)
- Walk-forward validation (train months 1-2, test month 3)
- Feature importance analysis (what actually matters)
- Calibrated probability output (not just 0/1 but actual %)
- Separate model per side (long/short have different dynamics)
"""

import json
import logging
import joblib  # safer than pickle for ML model serialization
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score, classification_report, log_loss, precision_score,
    recall_score, roc_auc_score,
)
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler

from ml_training.feature_builder import (
    build_features, build_labels, build_directional_labels,
    build_regression_labels, compute_indicators,
)

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent / "storage" / "ml_models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)


class MLProbabilityModel:
    """Trains and predicts trade success probability."""

    def __init__(self, symbol: str, side: str, timeframe: str = "5m"):
        self.symbol = symbol
        self.side = side
        self.timeframe = timeframe
        self.model: Optional[Any] = None
        self.scaler: Optional[StandardScaler] = None
        self.feature_names: List[str] = []
        self.feature_importances: Dict[str, float] = {}
        self.training_metrics: Dict = {}
        self.walk_forward_results: List[Dict] = []
        self._is_trained = False

    @property
    def is_trained(self) -> bool:
        return self._is_trained

    def train(self, df: pd.DataFrame, htf_df: Optional[pd.DataFrame] = None,
              tp_r: float = 1.5, sl_r: float = 1.0, max_bars: int = 60,
              signal_indices: Optional[List[int]] = None,
              label_mode: str = "directional",  # default per memory: reduce problem difficulty; call-site can override to binary/mfe
              ) -> Dict:
        """Train the model on historical candle data.

        Parameters
        ----------
        df : Primary timeframe OHLCV data
        htf_df : Higher timeframe data (optional, for multi-TF features)
        tp_r : Take profit in R-multiples
        sl_r : Stop loss in R-multiples
        max_bars : Max bars to look forward for label
        signal_indices : If provided, train only on bars where signals fired
        label_mode : "binary" (TP/SL), "directional" (up/down), "regression" (continuous R)

        Returns
        -------
        Dict with training metrics and feature importances
        """
        logger.info("Training ML model: %s %s %s (label=%s)",
                    self.symbol, self.side, self.timeframe, label_mode)

        # Build features
        features = build_features(df, htf_df)

        # Build labels based on mode
        if label_mode == "directional":
            # Simpler: will price move +0.2 ATR in my direction in next 12 bars?
            labels = build_directional_labels(df, forward_bars=12, threshold_atr=0.2)
            if self.side == "short":
                # Flip: directional_labels gives "up" probability, we want "down"
                labels = 1 - labels
        elif label_mode == "regression":
            labels = build_regression_labels(df, self.side, forward_bars=12)
        else:
            labels = build_labels(df, self.side, tp_r=tp_r, sl_r=sl_r, max_bars=max_bars)

        # Align and clean
        combined = features.join(labels)
        combined.replace([np.inf, -np.inf], np.nan, inplace=True)
        combined.fillna(0, inplace=True)
        combined = combined[combined.index >= features.index[200]]  # skip warmup rows

        if len(combined) < 200:
            return {"error": f"Insufficient data: {len(combined)} rows (need 200+)"}

        # Signal-based sampling: only train on bars where signals fired
        # This dramatically improves signal-to-noise ratio
        if signal_indices and len(signal_indices) >= 50:
            # Map integer indices to DataFrame positions after warmup filtering
            valid_positions = set(range(len(combined)))
            sig_mask = np.zeros(len(combined), dtype=bool)
            for idx in signal_indices:
                # Convert original df index to combined position
                pos = idx - 200  # offset for warmup skip
                if 0 <= pos < len(combined):
                    sig_mask[pos] = True

            signal_rows = combined[sig_mask]
            no_signal_rows = combined[~sig_mask]

            # Take all signal rows + equal number of random no-signal rows
            n_context = min(len(signal_rows), len(no_signal_rows))
            if n_context > 0:
                context_sample = no_signal_rows.sample(
                    n=n_context, random_state=42
                )
                combined = pd.concat([signal_rows, context_sample]).sort_index()
                logger.info("Signal-based sample: %d signal + %d context = %d total",
                            len(signal_rows), n_context, len(combined))

        # Cap for micro VM memory (1GB)
        max_rows = 20000
        if len(combined) > max_rows:
            n = len(combined)
            recent = combined.iloc[int(n * 0.67):]
            mid = combined.iloc[int(n * 0.33):int(n * 0.67)]
            old = combined.iloc[:int(n * 0.33)]
            recent_n = min(len(recent), int(max_rows * 0.50))
            mid_n = min(len(mid), int(max_rows * 0.25))
            old_n = min(len(old), int(max_rows * 0.25))
            combined = pd.concat([
                old.sample(n=old_n, random_state=42) if len(old) > old_n else old,
                mid.sample(n=mid_n, random_state=42) if len(mid) > mid_n else mid,
                recent.iloc[-recent_n:],
            ]).sort_index()

        X = combined.drop(columns=["label"])
        y = combined["label"]

        self.feature_names = list(X.columns)

        # Walk-forward validation
        self.walk_forward_results = self._walk_forward(X, y)

        # Final model: train on all data
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        try:
            import lightgbm as _lgb
            base_rf = _lgb.LGBMClassifier(
                n_estimators=200, max_depth=6, learning_rate=0.05,
                min_child_samples=30, subsample=0.8, colsample_bytree=0.8,
                reg_alpha=0.1, reg_lambda=1.0, is_unbalance=True,
                random_state=42, n_jobs=1, verbose=-1,
            )
            logger.info("Using LightGBM classifier (Upgrade 5)")
        except ImportError:
            logger.warning("LightGBM not available, falling back to RandomForest")
            base_rf = RandomForestClassifier(
            n_estimators=50,
            max_depth=6,
            min_samples_leaf=30,
            min_samples_split=60,
            class_weight="balanced",
            random_state=42,
            n_jobs=1,  # single core to limit memory on micro VM
        )

        # Capture feature importances BEFORE wrapping in the calibrator.
        # Phase 4.6 (2026-04-16): removed the SECOND `base_rf.fit(X_scaled, y)`
        # after CalibratedClassifierCV.fit — it contaminated the reported
        # importances because the calibrator had already seen that data.
        # Now: fit once for importances, then wrap the same fitted estimator
        # with cv='prefit' so the calibrator does NOT refit the base model
        # (saves ~2x training time and keeps importances coherent).
        base_rf.fit(X_scaled, y)
        importances = base_rf.feature_importances_

        # Calibrate probabilities using the already-fit base (cv='prefit').
        # We calibrate on a held-out tail of the training data (last 25%).
        _split = max(len(X_scaled) * 3 // 4, 50)
        try:
            self.model = CalibratedClassifierCV(base_rf, cv='prefit', method='isotonic')
            self.model.fit(X_scaled[_split:], y.iloc[_split:] if hasattr(y, 'iloc') else y[_split:])
        except Exception as _cal_e:
            logger.warning("Calibration (cv='prefit') failed, using uncalibrated base: %s", _cal_e)
            self.model = base_rf
        self.feature_importances = {
            name: round(float(imp), 4)
            for name, imp in sorted(
                zip(self.feature_names, importances),
                key=lambda x: x[1], reverse=True
            )
        }

        # Overall metrics
        y_pred = self.model.predict(X_scaled)
        y_prob = self.model.predict_proba(X_scaled)[:, 1]

        self.training_metrics = {
            "symbol": self.symbol,
            "side": self.side,
            "timeframe": self.timeframe,
            "samples": len(X),
            "positive_rate": round(float(y.mean()) * 100, 1),
            "accuracy": round(accuracy_score(y, y_pred) * 100, 1),
            "precision": round(precision_score(y, y_pred, zero_division=0) * 100, 1),
            "recall": round(recall_score(y, y_pred, zero_division=0) * 100, 1),
            "auc_roc": round(roc_auc_score(y, y_prob), 4) if len(set(y)) > 1 else 0,
            "log_loss": round(log_loss(y, y_prob), 4),
            "top_features": dict(list(self.feature_importances.items())[:15]),
            "walk_forward": self.walk_forward_results,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "tp_r": tp_r,
            "sl_r": sl_r,
            "max_bars": max_bars,
        }

        self._is_trained = True
        self._save()

        logger.info(
            "Model trained: %s %s | Accuracy: %.1f%% | AUC: %.4f | Samples: %d",
            self.symbol, self.side,
            self.training_metrics["accuracy"],
            self.training_metrics["auc_roc"],
            len(X),
        )

        return self.training_metrics

    def _walk_forward(self, X: pd.DataFrame, y: pd.Series,
                       n_splits: int = 5, gap: int = 12) -> List[Dict]:
        """Time-series walk-forward cross-validation.

        Phase 4.6 (2026-04-16): added `gap` parameter to prevent label-window
        leakage between folds. Default 12 bars matches the directional-label
        `forward_bars=12` window used in `build_directional_labels`. Without
        this gap, test bar N shares label context with training bar N-12,
        producing inflated OOS AUC.
        """
        tscv = TimeSeriesSplit(n_splits=n_splits, gap=gap)
        results = []

        for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
            X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
            y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

            if len(set(y_train)) < 2 or len(set(y_test)) < 2:
                continue

            scaler = StandardScaler()
            X_train_s = scaler.fit_transform(X_train)
            X_test_s = scaler.transform(X_test)

            rf = RandomForestClassifier(
                n_estimators=50,
                max_depth=6,
                min_samples_leaf=30,
                min_samples_split=60,
                class_weight="balanced",
                random_state=42,
                n_jobs=1,
            )
            rf.fit(X_train_s, y_train)

            y_pred = rf.predict(X_test_s)
            y_prob = rf.predict_proba(X_test_s)[:, 1]

            results.append({
                "fold": fold + 1,
                "train_size": len(X_train),
                "test_size": len(X_test),
                "train_period": f"{X_train.index[0]} → {X_train.index[-1]}",
                "test_period": f"{X_test.index[0]} → {X_test.index[-1]}",
                "accuracy": round(accuracy_score(y_test, y_pred) * 100, 1),
                "precision": round(precision_score(y_test, y_pred, zero_division=0) * 100, 1),
                "recall": round(recall_score(y_test, y_pred, zero_division=0) * 100, 1),
                "auc_roc": round(roc_auc_score(y_test, y_prob), 4),
                "positive_rate": round(float(y_test.mean()) * 100, 1),
            })

        return results

    def predict_probability(self, df: pd.DataFrame,
                             htf_df: Optional[pd.DataFrame] = None) -> float:
        """Predict probability of trade success for current bar.

        Returns probability 0.0-1.0 that TP will be hit before SL.
        """
        if not self._is_trained or self.model is None:
            return 0.5  # neutral if no model

        features = build_features(df, htf_df)
        if features.empty:
            return 0.5

        last_row = features.iloc[[-1]]

        # Ensure columns match training
        missing = [c for c in self.feature_names if c not in last_row.columns]
        for c in missing:
            last_row[c] = 0
        last_row = last_row[self.feature_names]

        # Handle NaNs and infs
        last_row = last_row.replace([np.inf, -np.inf], np.nan).fillna(0)

        X_scaled = self.scaler.transform(last_row)
        prob = self.model.predict_proba(X_scaled)[0][1]

        return float(prob)

    def _save(self):
        """Save model, scaler, and metadata to disk."""
        safe = f"{self.symbol.replace('/', '_')}_{self.side}_{self.timeframe}"
        model_path = MODEL_DIR / f"{safe}_model.pkl"
        meta_path = MODEL_DIR / f"{safe}_meta.json"

        joblib.dump({
            "model": self.model,
            "scaler": self.scaler,
            "feature_names": self.feature_names,
        }, model_path)

        with open(meta_path, "w") as f:
            json.dump({
                "training_metrics": self.training_metrics,
                "feature_importances": self.feature_importances,
            }, f, indent=2, default=str)

        logger.info("Model saved: %s", model_path)

    def load(self) -> bool:
        """Load a previously trained model."""
        safe = f"{self.symbol.replace('/', '_')}_{self.side}_{self.timeframe}"
        model_path = MODEL_DIR / f"{safe}_model.pkl"
        meta_path = MODEL_DIR / f"{safe}_meta.json"

        if not model_path.exists():
            return False

        try:
            data = joblib.load(model_path)
            self.model = data["model"]
            self.scaler = data["scaler"]
            self.feature_names = data["feature_names"]

            if meta_path.exists():
                with open(meta_path) as f:
                    meta = json.load(f)
                self.training_metrics = meta.get("training_metrics", {})
                self.feature_importances = meta.get("feature_importances", {})

            self._is_trained = True
            return True
        except Exception as e:
            logger.error("Failed to load model %s: %s", model_path, e)
            return False

    def get_status(self) -> Dict:
        """Return model status for dashboard."""
        return {
            "symbol": self.symbol,
            "side": self.side,
            "timeframe": self.timeframe,
            "is_trained": self._is_trained,
            "metrics": self.training_metrics,
            "top_features": dict(list(self.feature_importances.items())[:10]),
            "walk_forward": self.walk_forward_results,
        }
