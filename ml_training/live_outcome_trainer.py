"""
Live Outcome ML Trainer — trains on actual trade outcomes, not backtest simulations.

Key differences from candidate_trainer.py:
1. Uses ml_live_feedback.jsonl as label source (real PnL, not simulated)
2. Uses simplified 20-25 feature set (not 204)
3. Single shared model across all scanners (scanner_name as feature)
4. GradientBoostingClassifier with binary labels (profit vs loss)
5. Proper walk-forward with 10-bar purge gap
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score, accuracy_score
import joblib

logger = logging.getLogger(__name__)

STORAGE_DIR = Path("storage")
ML_MODELS_DIR = STORAGE_DIR / "ml_models"
FEEDBACK_FILE = STORAGE_DIR / "ml_live_feedback.jsonl"

# Canonical feature set — 25 features that actually matter
CANONICAL_FEATURES = [
    # Regime (one-hot, 6)
    "regime_trending_up", "regime_trending_down", "regime_ranging",
    "regime_volatile", "regime_quiet", "regime_sideways",
    # Core market state (8)
    "atr_ratio", "trend_strength", "vwap_distance", "volume_zscore",
    "body_ratio", "htf_alignment", "regime_side_alignment", "ema8_slope",
    # Context (5)
    "dist_from_swing_high", "dist_from_swing_low", "rel_vol",
    "impulse_body_atr", "range_vs_atr",
    # Session (4)
    "session_asia_late", "session_asia_early", "session_europe", "session_us",
    # Scanner (will be added dynamically as one-hot)
    # Rule quality (2)
    "rules_passed_count", "rules_passed_pct",
]


def load_live_feedback() -> pd.DataFrame:
    """Load trade outcomes from ml_live_feedback.jsonl."""
    if not FEEDBACK_FILE.exists():
        logger.warning("No feedback file found at %s", FEEDBACK_FILE)
        return pd.DataFrame()

    records = []
    with open(FEEDBACK_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    logger.info("Loaded %d trade outcomes from feedback file", len(df))
    return df


def build_live_features(trades_df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series]:
    """Build feature matrix and labels from live trade feedback.

    Features come from the trade metadata (what was known at entry time).
    Labels are binary: 1 = profitable (pnl_usd > 0), 0 = loss.
    """
    features = []
    labels = []
    trade_ids = []

    for _, row in trades_df.iterrows():
        feat = {}

        # Regime (from trade metadata)
        regime = row.get("regime", "sideways")
        for cat in ["trending_up", "trending_down", "ranging", "volatile", "quiet", "sideways"]:
            feat["regime_%s" % cat] = 1.0 if regime == cat else 0.0

        # Confidence and grade
        feat["confidence"] = float(row.get("confidence", 50)) / 100.0
        feat["atr_ratio"] = 1.0  # Not available in feedback — use neutral

        # ML probability (meta-feature: what did the old model think?)
        feat["ml_probability"] = float(row.get("ml_probability", 0.5))

        # Scanner (one-hot)
        scanner = row.get("setup_type", "unknown") or "unknown"
        for sc in ["structure_bounce", "bos_choch", "liquidity_sweep", "trend_continuation",
                    "ema_momentum", "vwap_mean_revert", "rsi_divergence", "cvd_divergence"]:
            feat["scanner_%s" % sc] = 1.0 if scanner == sc else 0.0

        # Side
        feat["side_long"] = 1.0 if row.get("side") == "long" else 0.0

        # Session
        session = row.get("session", "unknown")
        for s in ["asia_late", "asia_early", "europe", "us"]:
            feat["session_%s" % s] = 1.0 if session == s else 0.0

        # Trade type
        trade_type = row.get("trade_type", "SCALP")
        feat["is_scalp"] = 1.0 if trade_type == "SCALP" else 0.0
        feat["is_intraday"] = 1.0 if trade_type == "INTRADAY" else 0.0
        feat["is_runner"] = 1.0 if trade_type == "RUNNER" else 0.0

        # Symbol category
        symbol = row.get("symbol", "")
        feat["is_btc"] = 1.0 if "BTC" in symbol else 0.0
        feat["is_eth"] = 1.0 if "ETH" in symbol else 0.0
        feat["is_sol"] = 1.0 if "SOL" in symbol else 0.0

        # Leverage (normalized)
        feat["leverage_norm"] = float(row.get("leverage", 10)) / 20.0

        # Position size (normalized)
        feat["position_norm"] = float(row.get("position_size_usd", 500)) / 2000.0

        features.append(feat)

        # Binary label: profit or loss
        pnl = float(row.get("pnl_usd", 0))
        labels.append(1 if pnl > 0 else 0)
        trade_ids.append(row.get("trade_id", ""))

    X = pd.DataFrame(features)
    y = pd.Series(labels, name="profitable")

    # Fill NaN
    X = X.fillna(0.0)

    logger.info("Built feature matrix: %d trades x %d features, %.1f%% positive",
                len(X), len(X.columns), y.mean() * 100)

    return X, y


def train_live_model(min_trades: int = 100) -> Optional[Dict]:
    """Train a model on live trade outcomes.

    Returns dict with model, metrics, and feature names.
    """
    # Load feedback
    trades_df = load_live_feedback()
    if len(trades_df) < min_trades:
        logger.warning("Insufficient trades: %d < %d minimum", len(trades_df), min_trades)
        return None

    # Build features and labels
    X, y = build_live_features(trades_df)

    # Walk-forward CV with proper purge
    n_splits = 5
    purge_gap = 10  # 10 trades gap between train/test
    tscv = TimeSeriesSplit(n_splits=n_splits, gap=purge_gap)

    fold_aucs = []
    fold_accs = []
    fold_importances = []

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        # Skip fold if too few samples or single class
        if len(y_train.unique()) < 2 or len(y_test) < 10:
            continue

        model = GradientBoostingClassifier(
            n_estimators=50,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            min_samples_leaf=10,
            random_state=42 + fold,
        )
        model.fit(X_train, y_train)

        # Evaluate
        probs = model.predict_proba(X_test)[:, 1]
        try:
            auc = roc_auc_score(y_test, probs)
        except ValueError:
            auc = 0.5
        acc = accuracy_score(y_test, (probs >= 0.5).astype(int))

        fold_aucs.append(auc)
        fold_accs.append(acc)
        fold_importances.append(model.feature_importances_)

        logger.info("Fold %d: AUC=%.4f Acc=%.1f%% (train=%d, test=%d)",
                     fold + 1, auc, acc * 100, len(train_idx), len(test_idx))

    if not fold_aucs:
        logger.warning("No valid folds — cannot train")
        return None

    avg_auc = np.mean(fold_aucs)
    avg_acc = np.mean(fold_accs)
    avg_importance = np.mean(fold_importances, axis=0)

    logger.info("Average OOS: AUC=%.4f Acc=%.1f%%", avg_auc, avg_acc)

    # Train final model on ALL data
    final_model = GradientBoostingClassifier(
        n_estimators=50,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        min_samples_leaf=10,
        random_state=42,
    )
    final_model.fit(X, y)

    # Feature importance ranking
    importance_df = pd.DataFrame({
        "feature": X.columns,
        "importance": final_model.feature_importances_,
    }).sort_values("importance", ascending=False)

    logger.info("Top 10 features:")
    for _, row in importance_df.head(10).iterrows():
        logger.info("  %s: %.4f", row["feature"], row["importance"])

    # Probability bucket analysis
    final_probs = final_model.predict_proba(X)[:, 1]
    bucket_analysis = _analyze_buckets(y.values, final_probs)

    # Save model
    model_path = ML_MODELS_DIR / "model_live_shared.joblib"
    ML_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model": final_model,
        "feature_names": list(X.columns),
        "trained_at": time.time(),
        "n_trades": len(X),
        "avg_auc": avg_auc,
        "avg_acc": avg_acc,
        "label_type": "live_outcome_binary",
    }, model_path)

    logger.info("Model saved to %s", model_path)

    # Save results
    results = {
        "model_type": "live_outcome_shared",
        "n_trades": len(X),
        "n_features": len(X.columns),
        "feature_names": list(X.columns),
        "avg_auc": round(avg_auc, 4),
        "avg_acc": round(avg_acc, 4),
        "fold_aucs": [round(a, 4) for a in fold_aucs],
        "fold_accs": [round(a, 4) for a in fold_accs],
        "top_features": importance_df.head(15).to_dict(orient="records"),
        "bucket_analysis": bucket_analysis,
        "positive_rate": round(y.mean() * 100, 1),
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    results_path = ML_MODELS_DIR / "live_model_results.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    return results


def _analyze_buckets(y_true: np.ndarray, y_prob: np.ndarray) -> Dict:
    """Analyze probability buckets for monotonicity and calibration."""
    n = len(y_true)
    n_buckets = min(5, n // 20)
    if n_buckets < 2:
        return {"monotonic": False, "buckets": []}

    # Sort by predicted probability
    sorted_idx = np.argsort(y_prob)
    bucket_size = n // n_buckets

    buckets = []
    for i in range(n_buckets):
        start = i * bucket_size
        end = start + bucket_size if i < n_buckets - 1 else n
        idx = sorted_idx[start:end]
        actual_wr = y_true[idx].mean() * 100
        avg_prob = y_prob[idx].mean() * 100
        buckets.append({
            "bucket": "Q%d" % (i + 1),
            "n": len(idx),
            "actual_wr": round(actual_wr, 1),
            "avg_prob": round(avg_prob, 1),
        })

    # Check monotonicity
    wrs = [b["actual_wr"] for b in buckets]
    monotonic = all(wrs[i] <= wrs[i + 1] for i in range(len(wrs) - 1))
    spread = wrs[-1] - wrs[0] if wrs else 0

    return {
        "monotonic": monotonic,
        "spread": round(spread, 1),
        "buckets": buckets,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    results = train_live_model(min_trades=50)
    if results:
        print("\n=== LIVE OUTCOME MODEL RESULTS ===")
        print("Trades: %d" % results["n_trades"])
        print("Features: %d" % results["n_features"])
        print("AUC: %.4f" % results["avg_auc"])
        print("Accuracy: %.1f%%" % (results["avg_acc"] * 100))
        print("Positive rate: %.1f%%" % results["positive_rate"])
        print("\nTop features:")
        for f in results["top_features"][:10]:
            print("  %s: %.4f" % (f["feature"], f["importance"]))
        print("\nBucket analysis:")
        ba = results["bucket_analysis"]
        print("  Monotonic: %s | Spread: %.1f%%" % (ba["monotonic"], ba["spread"]))
        for b in ba["buckets"]:
            print("  %s: n=%d actual_wr=%.1f%% avg_prob=%.1f%%" % (
                b["bucket"], b["n"], b["actual_wr"], b["avg_prob"]))
    else:
        print("Training failed — insufficient data")
