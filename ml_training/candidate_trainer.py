"""
Candidate Trainer — Conditional Classifier for Scanner Candidates
===================================================================
Trains ML to rank scanner candidates — NOT predict the market.

Runs for ALL scanners: structure_bounce, ema_momentum, bb_squeeze,
vwap_mean_revert, rsi_divergence, trend_continuation.

Pipeline per scanner:
1. Scan all bars with scanner to find candidates
2. For each candidate, compute:
   - Market state features (from feature_builder)
   - Gate/veto features: regime type (one-hot), regime stability, HTF alignment,
     EMA trend slope, VWAP distance, volume z-score, session, volatility regime,
     distance from swing, impulse freshness, candle body_ratio, range_vs_atr
3. Simulate trade outcome (TP before SL? R-result after fees?)
4. Train classifier: which candidates win?
5. Walk-forward validate
6. Report: raw WR vs rule-filtered WR vs ML top-quartile WR

Key insight: the FEATURES include ALL veto/gate values as inputs — these are
what created the 68% WR. The model learns which combinations matter most
and can potentially improve beyond the hand-coded rules.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score, roc_auc_score,
    classification_report,
)

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ml_training.feature_builder import build_features, compute_indicators, build_mfe_labels

logger = logging.getLogger(__name__)

RESULTS_DIR = PROJECT_ROOT / "storage" / "ml_models"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Fee rate: 0.047% per side with Scalper tier (round trip = 0.094%)
SCALPER_FEE_PER_SIDE = 0.00047
SCALPER_FEE_ROUND_TRIP = SCALPER_FEE_PER_SIDE * 2

# Regimes used in one-hot encoding (matching live strategy regime_filter.py)
REGIME_CATEGORIES = [
    "trending_up", "trending_down", "ranging", "volatile", "quiet", "sideways",
]

# Sessions used in one-hot encoding (matching live strategy)
SESSION_CATEGORIES = ["asia_late", "asia_early", "europe", "us"]


# ──────────────────────────────────────────────────────────────────────
# Gate/Veto feature computation (mirrors live scalp_strategy.py logic)
# ──────────────────────────────────────────────────────────────────────

def _detect_regime(row: pd.Series, df: pd.DataFrame, idx: int) -> str:
    """Detect regime — matches scanner_backtester.py logic."""
    try:
        ema_21 = float(row.get("ema_21", 0))
        ema_50 = float(row.get("ema_50", 0))
        ema_200 = float(row.get("ema_200", 0))
        atr = float(row.get("atr_14", 0))
        avg_atr = df["atr_14"].iloc[max(0, idx - 100):idx].mean() if idx > 100 else atr
        bb_width = float(row.get("bb_width", 0))
        close = float(row.get("close", 0))

        if avg_atr > 0 and atr / avg_atr < 0.7:
            return "quiet"
        if ema_21 > ema_50 > ema_200 and close > ema_21:
            return "trending_up"
        if ema_21 < ema_50 < ema_200 and close < ema_21:
            return "trending_down"
        if bb_width > 0:
            avg_bbw = df["bb_width"].iloc[max(0, idx - 50):idx].mean()
            if avg_bbw > 0 and bb_width / avg_bbw > 1.5:
                return "volatile"
            if avg_bbw > 0 and bb_width / avg_bbw < 0.5:
                return "ranging"
        return "sideways"
    except Exception:
        return "sideways"


def _detect_session(dt) -> str:
    """Detect trading session from UTC hour — matches scanner_backtester.py."""
    if hasattr(dt, "hour"):
        h = dt.hour
    else:
        return "unknown"
    if 0 <= h < 6:
        return "asia_late"
    elif 6 <= h < 12:
        return "asia_early"
    elif 12 <= h < 18:
        return "europe"
    else:
        return "us"


def _compute_regime_stability(df: pd.DataFrame, idx: int, lookback: int = 20) -> float:
    """How stable is the current regime? Ratio of bars in same regime over lookback.

    1.0 = regime unchanged for all lookback bars, 0.0 = constant regime changes.
    Computed by checking EMA alignment consistency.
    """
    if idx < lookback + 50:
        return 0.5

    current_regime = _detect_regime(df.iloc[idx], df, idx)
    same_count = 0
    for k in range(max(50, idx - lookback), idx):
        r = _detect_regime(df.iloc[k], df, k)
        if r == current_regime:
            same_count += 1
    return same_count / lookback


def _get_htf_alignment(df: pd.DataFrame, idx: int, side: str) -> float:
    """HTF alignment score. +1 = aligned, -1 = opposing, 0 = neutral.

    Uses EMA50 slope as HTF proxy (since we only have single-TF data).
    Matches live strategy _get_htf_bias logic (above/below EMA50).
    """
    if idx < 50:
        return 0.0
    close = float(df.iloc[idx]["close"])
    ema50 = float(df.iloc[idx].get("ema_50", 0))
    if ema50 == 0:
        return 0.0

    # HTF bias: bullish if close > ema50, bearish if below
    htf_bullish = close > ema50

    if side == "long":
        return 1.0 if htf_bullish else -1.0
    else:
        return -1.0 if htf_bullish else 1.0


def _compute_gate_veto_features(
    df: pd.DataFrame, idx: int, side: str, symbol: str
) -> Dict[str, float]:
    """Compute all gate/veto feature values for a candidate bar.

    These mirror the live veto checks in scalp_strategy.py (VETO 1-10).
    Each is encoded as a continuous numeric feature for the ML model.
    """
    row = df.iloc[idx]
    c = float(row["close"])
    h = float(row["high"])
    l = float(row["low"])
    o = float(row["open"])
    atr = float(row.get("atr_14", 0))
    if atr <= 0:
        atr = 1e-8  # avoid division by zero

    features = {}

    # ── Regime (one-hot) ──
    regime = _detect_regime(row, df, idx)
    for cat in REGIME_CATEGORIES:
        features[f"regime_{cat}"] = 1.0 if regime == cat else 0.0

    # ── Regime stability (how long has this regime held?) ──
    features["regime_stability"] = _compute_regime_stability(df, idx, lookback=20)

    # ── HTF alignment (does signal agree with higher-TF trend?) ──
    features["htf_alignment"] = _get_htf_alignment(df, idx, side)

    # ── EMA trend slope (rate of trend change — VETO checks trend direction) ──
    ema8 = float(row.get("ema_8", 0))
    ema21 = float(row.get("ema_21", 0))
    ema50 = float(row.get("ema_50", 0))

    # EMA8 slope over 3 bars (normalized by ATR)
    if idx >= 3 and ema8 > 0:
        ema8_prev = float(df.iloc[idx - 3].get("ema_8", ema8))
        features["ema8_slope"] = (ema8 - ema8_prev) / atr
    else:
        features["ema8_slope"] = 0.0

    # EMA21 slope over 5 bars
    if idx >= 5 and ema21 > 0:
        ema21_prev = float(df.iloc[idx - 5].get("ema_21", ema21))
        features["ema21_slope"] = (ema21 - ema21_prev) / atr
    else:
        features["ema21_slope"] = 0.0

    # Trend strength: EMA gap normalized by ATR
    features["trend_strength"] = (ema8 - ema21) / atr if ema21 > 0 else 0.0
    features["trend_strength_long"] = (ema21 - ema50) / atr if ema50 > 0 else 0.0

    # ── VWAP distance (VETO 5-like: stretched from fair value?) ──
    vwap = float(row.get("vwap", 0))
    features["vwap_distance"] = (c - vwap) / atr if vwap > 0 else 0.0

    # ── Volume z-score (VETO 5: rel_vol < 1.0 → veto) ──
    vol_sma = float(row.get("vol_sma_20", 0))
    vol_std = float(row.get("vol_std_20", 0))
    volume = float(row.get("volume", 0))
    features["volume_zscore"] = (volume - vol_sma) / vol_std if vol_std > 0 else 0.0
    features["rel_vol"] = float(row.get("rel_vol", 1.0))

    # ── Session (one-hot — VETO 3: asia_late = hard veto) ──
    session = _detect_session(df.index[idx]) if hasattr(df.index[idx], "hour") else "unknown"
    for cat in SESSION_CATEGORIES:
        features[f"session_{cat}"] = 1.0 if session == cat else 0.0

    # ── Volatility regime (VETO 4: ATR ratio < 0.88 → veto) ──
    avg_atr = df["atr_14"].iloc[max(0, idx - 100):idx].mean()
    features["atr_ratio"] = atr / avg_atr if avg_atr > 0 else 1.0

    # Short-term vs long-term ATR (expansion/contraction)
    atr7 = float(row.get("atr_7", atr))
    features["atr_expansion"] = atr7 / atr if atr > 0 else 1.0

    # ── Distance from swing (how close to support/resistance?) ──
    recent = df.iloc[max(0, idx - 20):idx]
    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())
    features["dist_from_swing_high"] = (c - swing_high) / atr
    features["dist_from_swing_low"] = (c - swing_low) / atr

    # ── Impulse freshness (VETO 8: no-chase gate) ──
    # Large candle body relative to ATR = chasing an impulse
    candle_body = abs(c - o)
    candle_range = h - l
    features["impulse_body_atr"] = candle_body / atr

    # Distance from EMA8 (stretched = chasing)
    features["dist_from_ema8"] = abs(c - ema8) / atr if ema8 > 0 else 0.0

    # ── Candle structure (VETO 7: body_ratio < 0.3 → veto) ──
    features["body_ratio"] = candle_body / candle_range if candle_range > 0 else 0.0
    features["range_vs_atr"] = candle_range / atr

    # Upper/lower wick ratios (rejection quality)
    features["upper_wick_ratio"] = (h - max(c, o)) / candle_range if candle_range > 0 else 0.0
    features["lower_wick_ratio"] = (min(c, o) - l) / candle_range if candle_range > 0 else 0.0

    # ── Regime-side conflict (VETO 10) ──
    # Encode as: +1 aligned with regime, -1 counter-trend, 0 neutral
    if regime in ("trending_up",):
        features["regime_side_alignment"] = 1.0 if side == "long" else -1.0
    elif regime in ("trending_down",):
        features["regime_side_alignment"] = 1.0 if side == "short" else -1.0
    else:
        features["regime_side_alignment"] = 0.0

    # ── RSI zone (not a raw feature — just where in cycle) ──
    rsi = float(row.get("rsi_14", 50))
    features["rsi_zone"] = (rsi - 50) / 50  # -1 to +1

    # ── BB position (where is price in Bollinger range?) ──
    bb_upper = float(row.get("bb_upper", 0))
    bb_lower = float(row.get("bb_lower", 0))
    if bb_upper > bb_lower > 0:
        features["bb_position"] = (c - bb_lower) / (bb_upper - bb_lower)
    else:
        features["bb_position"] = 0.5

    # ── Side encoding ──
    features["side_long"] = 1.0 if side == "long" else 0.0

    # ── Rule gates as continuous + binary features (ML learns thresholds) ──
    # Thresholds are SOFTER than live hard rules so ML can learn the real cutoffs
    features["rule_htf_pass"] = 1.0 if features.get("htf_alignment", 0) >= 0 else 0.0
    features["rule_session_pass"] = 0.0 if features.get("session_asia_late", 0) > 0.5 else 1.0
    features["rule_vol_pass"] = 1.0 if features.get("atr_ratio", 0) >= 0.7 else 0.0
    features["rule_volume_pass"] = 1.0 if features.get("rel_vol", 0) >= 0.8 else 0.0
    features["rule_candle_pass"] = 1.0 if features.get("body_ratio", 0) >= 0.3 else 0.0
    features["rule_chase_pass"] = 1.0 if features.get("impulse_body_atr", 0) <= 1.5 else 0.0
    features["rule_stretch_pass"] = 1.0 if features.get("dist_from_ema8", 0) <= 0.8 else 0.0
    features["rule_regime_pass"] = 1.0 if features.get("regime_side_alignment", 0) >= -0.5 else 0.0
    features["rules_passed_count"] = sum([
        features["rule_htf_pass"], features["rule_session_pass"],
        features["rule_vol_pass"], features["rule_volume_pass"],
        features["rule_candle_pass"], features["rule_chase_pass"],
        features["rule_stretch_pass"], features["rule_regime_pass"],
    ])
    features["rules_passed_pct"] = features["rules_passed_count"] / 8.0

    return features


# ──────────────────────────────────────────────────────────────────────
# Trade simulation (matches scanner_backtester.py _simulate_trade)
# ──────────────────────────────────────────────────────────────────────

def _simulate_trade_outcome(
    df: pd.DataFrame, entry_idx: int, side: str,
    entry_price: float, atr: float, symbol: str,
    fee_rate: float = SCALPER_FEE_ROUND_TRIP,
) -> Dict[str, float]:
    """Simulate trade forward and return outcome dict.

    Uses live-identical exit logic: 0.65% SL, ATR-based TPs,
    dynamic trailing, early kill, scalper timeout.
    """
    sl_pct = 0.0065
    if side == "long":
        sl = entry_price * (1 - sl_pct)
        tp1 = entry_price + 1.5 * atr
    else:
        sl = entry_price * (1 + sl_pct)
        tp1 = entry_price - 1.5 * atr

    initial_risk = abs(entry_price - sl)
    if initial_risk <= 0:
        return {"pnl_r": 0.0, "won": False, "exit_reason": "zero_risk"}

    # Scalper window
    scalper_window_sec = 27 * 60 if "BTC" in symbol else 12 * 60

    # Detect timeframe from index
    tf_seconds = 60
    if len(df) > 1:
        idx_diff = df.index[1] - df.index[0]
        if hasattr(idx_diff, "total_seconds"):
            tf_seconds = max(int(idx_diff.total_seconds()), 1)

    max_bars = max(10, scalper_window_sec // tf_seconds)

    highest = entry_price
    lowest = entry_price
    exit_price = 0.0
    exit_reason = ""

    for j in range(entry_idx + 1, min(entry_idx + max_bars + 1, len(df))):
        h_j = float(df.iloc[j]["high"])
        l_j = float(df.iloc[j]["low"])
        c_j = float(df.iloc[j]["close"])
        age_sec = (j - entry_idx) * tf_seconds

        highest = max(highest, h_j)
        lowest = min(lowest, l_j)

        if side == "long":
            current_r = (c_j - entry_price) / initial_risk
            mfe = (highest - entry_price) / initial_risk
            mae = (entry_price - lowest) / initial_risk
        else:
            current_r = (entry_price - c_j) / initial_risk
            mfe = (entry_price - lowest) / initial_risk
            mae = (highest - entry_price) / initial_risk

        # Dynamic trail
        trail_floor = None
        if mfe >= 1.5:
            trail_floor = mfe * 0.75
        elif mfe >= 1.0:
            trail_floor = mfe * 0.65
        elif mfe >= 0.5:
            trail_floor = mfe * 0.50
        elif mfe >= 0.3:
            trail_floor = 0.15

        if trail_floor is not None and current_r <= trail_floor:
            exit_price = c_j
            exit_reason = "trail"
            break

        # Stop loss
        sl_hit = (l_j <= sl) if side == "long" else (h_j >= sl)
        if sl_hit:
            exit_price = sl
            exit_reason = "stop_loss"
            break

        # Early kill
        if age_sec >= 300 and mfe < 0.15 and current_r < -0.15:
            exit_price = c_j
            exit_reason = "early_kill"
            break

        # Scalper timeout
        if age_sec >= scalper_window_sec:
            exit_price = c_j
            exit_reason = "scalper_timeout"
            break

    # End of data
    if exit_price == 0.0 and entry_idx + 1 < len(df):
        last_j = min(entry_idx + max_bars, len(df) - 1)
        exit_price = float(df.iloc[last_j]["close"])
        exit_reason = "end_of_data"

    # PnL in R (after fees)
    if side == "long":
        raw_r = (exit_price - entry_price) / initial_risk
    else:
        raw_r = (entry_price - exit_price) / initial_risk

    fee_r = (fee_rate * entry_price) / initial_risk
    pnl_r = raw_r - fee_r

    return {
        "pnl_r": pnl_r,
        "won": pnl_r > 0,
        "exit_reason": exit_reason,
        "mfe_r": mfe if "mfe" in dir() else 0.0,
        "mae_r": mae if "mae" in dir() else 0.0,
    }


# ──────────────────────────────────────────────────────────────────────
# Rule-based veto simulation (mirrors live veto logic)
# ──────────────────────────────────────────────────────────────────────

def _would_veto_block(gate_features: Dict[str, float]) -> bool:
    """Simulate the live veto logic using computed gate features.

    Returns True if this candidate would be BLOCKED by the live rules.
    Mirrors VETOs 2-10 from scalp_strategy.py (VETO 1 cooldown is time-based,
    cannot be replicated bar-by-bar).
    """
    # VETO 2: HTF strict alignment
    if gate_features.get("htf_alignment", 0) < 0:
        return True

    # VETO 3: Session — asia_late is hard veto
    if gate_features.get("session_asia_late", 0) > 0.5:
        return True

    # VETO 4: Volatility strict — ATR ratio < 0.88
    if gate_features.get("atr_ratio", 1.0) < 0.88:
        return True

    # VETO 5: Volume — rel_vol < 1.0
    if gate_features.get("rel_vol", 1.0) < 1.0:
        return True

    # VETO 7: Candle quality — body ratio < 0.3
    if gate_features.get("body_ratio", 0.5) < 0.3:
        return True

    # VETO 8: No-chase — impulse body > 1.25 ATR
    if gate_features.get("impulse_body_atr", 0) > 1.25:
        return True

    # VETO 8b: Stretched from EMA8 > 0.7 ATR
    if gate_features.get("dist_from_ema8", 0) > 0.7:
        return True

    # VETO 10: Regime-side conflict
    if gate_features.get("regime_side_alignment", 0) < -0.5:
        return True

    return False


# ──────────────────────────────────────────────────────────────────────
# Main class
# ──────────────────────────────────────────────────────────────────────

class CandidateTrainer:
    """Trains ML to rank scanner candidates — NOT predict the market.

    Works with ANY scanner function. Pipeline per scanner:
    1. Scan all bars to find candidates
    2. Compute gate/veto + market-state features
    3. Simulate trade outcome
    4. Train classifier with walk-forward validation
    5. Report: raw WR vs rule-filtered WR vs ML top-quartile WR
    """

    def __init__(self, config: Optional[dict] = None):
        self._config = config or {}
        self._model: Optional[RandomForestClassifier] = None
        self._feature_names: List[str] = []
        self._results: Dict = {}

    def build_dataset(
        self,
        df: pd.DataFrame,
        symbol: str,
        scanner_func=None,
    ) -> Tuple[pd.DataFrame, pd.Series]:
        """Scan all bars, compute features + labels for each candidate.

        Returns (X, y) where X has gate/veto features + market-state features,
        and y is binary (1 = trade won after fees, 0 = lost).
        """
        if scanner_func is None:
            raise ValueError("scanner_func is required")

        df = compute_indicators(df)

        # Also build market-state features for all bars (we'll select candidate rows)
        market_features = build_features(df)

        rows = []
        labels = []
        start_idx = 200  # indicator warmup

        for i in range(start_idx, len(df) - 30):
            result = scanner_func(i, df, symbol)
            if result is None:
                continue

            side = result["side"]
            entry_price = result["entry_price"]
            atr = float(df.iloc[i].get("atr_14", 0))
            if atr <= 0:
                continue

            # Gate/veto features
            gate_feats = _compute_gate_veto_features(df, i, side, symbol)

            # Market-state features (from feature_builder)
            mkt_row = market_features.iloc[i]
            mkt_dict = {f"mkt_{col}": float(mkt_row[col]) for col in mkt_row.index
                        if not np.isnan(float(mkt_row[col])) if isinstance(mkt_row[col], (int, float, np.number))}

            # Combine
            combined = {**gate_feats, **mkt_dict}
            rows.append(combined)

            # Label: simulate trade outcome
            outcome = _simulate_trade_outcome(df, i, side, entry_price, atr, symbol)
            labels.append(1 if outcome["won"] else 0)

        if not rows:
            logger.warning("No candidates found for %s", symbol)
            return pd.DataFrame(), pd.Series(dtype=int)

        X = pd.DataFrame(rows)
        y = pd.Series(labels, name="label")

        # Fill NaN with 0 (some market features may be NaN at boundaries)
        X = X.fillna(0)

        # Store feature names
        self._feature_names = list(X.columns)

        logger.info(
            "Dataset built: %d candidates, %.1f%% win rate, %d features",
            len(X), y.mean() * 100, len(X.columns),
        )

        return X, y

    def build_dataset_with_veto_labels(
        self,
        df: pd.DataFrame,
        symbol: str,
        scanner_func=None,
        label_mode: str = "mfe",
        mfe_threshold_r: float = 0.2,
        mfe_max_bars: int = 30,
    ) -> Tuple[pd.DataFrame, pd.Series, pd.Series]:
        """Build candidate dataset with veto labels.

        Returns (X, y, veto_blocked) where veto_blocked[i] = True if the
        live rule-based vetos would have blocked this candidate.

        Label modes:
        - "mfe": Will MFE exceed threshold_r within max_bars? (default, generalizes best)
        - "trade": Did full simulated trade win? (original, more coupled to exit logic)
        """
        if scanner_func is None:
            raise ValueError("scanner_func is required")

        df = compute_indicators(df)
        market_features = build_features(df)

        # Pre-compute MFE labels for both sides if using MFE mode
        mfe_labels_long = None
        mfe_labels_short = None
        if label_mode == "mfe":
            mfe_labels_long = build_mfe_labels(df, "long", max_bars=mfe_max_bars,
                                                threshold_r=mfe_threshold_r)
            mfe_labels_short = build_mfe_labels(df, "short", max_bars=mfe_max_bars,
                                                 threshold_r=mfe_threshold_r)

        rows = []
        labels = []
        veto_flags = []
        start_idx = 200

        for i in range(start_idx, len(df) - mfe_max_bars):
            result = scanner_func(i, df, symbol)
            if result is None:
                continue

            side = result["side"]
            entry_price = result["entry_price"]
            atr = float(df.iloc[i].get("atr_14", 0))
            if atr <= 0:
                continue

            gate_feats = _compute_gate_veto_features(df, i, side, symbol)

            mkt_row = market_features.iloc[i]
            mkt_dict = {}
            for col in mkt_row.index:
                try:
                    val = float(mkt_row[col])
                    if not np.isnan(val):
                        mkt_dict[f"mkt_{col}"] = val
                except (TypeError, ValueError):
                    continue

            combined = {**gate_feats, **mkt_dict}
            rows.append(combined)

            # Label based on mode
            if label_mode == "mfe":
                mfe_series = mfe_labels_long if side == "long" else mfe_labels_short
                labels.append(int(mfe_series.iloc[i]))
            else:
                outcome = _simulate_trade_outcome(df, i, side, entry_price, atr, symbol)
                labels.append(1 if outcome["won"] else 0)

            veto_flags.append(_would_veto_block(gate_feats))

        if not rows:
            return pd.DataFrame(), pd.Series(dtype=int), pd.Series(dtype=bool)

        X = pd.DataFrame(rows).fillna(0)
        y = pd.Series(labels, name="label")
        veto_blocked = pd.Series(veto_flags, name="veto_blocked")

        self._feature_names = list(X.columns)

        logger.info(
            "Dataset built (%s labels): %d candidates, %.1f%% positive, %d features",
            label_mode, len(X), y.mean() * 100, len(X.columns),
        )

        return X, y, veto_blocked

    def _select_top_features(self, X: pd.DataFrame, y: pd.Series,
                              max_features: int = 40) -> List[str]:
        """Select top N features by importance. Reduces overfitting."""
        # Quick RF to get importances
        quick_rf = RandomForestClassifier(
            n_estimators=30, max_depth=4, random_state=42,
            class_weight="balanced", n_jobs=1,
        )
        quick_rf.fit(X.fillna(0), y)

        importances = dict(zip(X.columns, quick_rf.feature_importances_))
        sorted_feats = sorted(importances.items(), key=lambda x: x[1], reverse=True)

        # Always keep rule_* features (they encode domain knowledge)
        rule_feats = [f for f in X.columns if f.startswith("rule_")]
        top_feats = [f for f, _ in sorted_feats[:max_features]]

        # Union: top N + all rule features
        selected = list(set(top_feats + rule_feats))

        logger.info("Feature selection: %d -> %d features (top %d + %d rule features)",
                     len(X.columns), len(selected), max_features, len(rule_feats))

        return selected

    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        n_splits: int = 5,
        n_estimators: int = 50,
        max_depth: int = 6,
    ) -> Dict:
        """Train RandomForest with walk-forward TimeSeriesSplit validation.

        Returns metrics dict with per-fold and aggregate results.
        """
        if len(X) < 100:
            return {"error": f"insufficient data: {len(X)} candidates (need 100+)"}

        # Feature selection: reduce overfitting by keeping top features + rule features
        if len(X.columns) > 40:
            selected_features = self._select_top_features(X, y, max_features=40)
            X = X[selected_features]
            self._feature_names = selected_features

        tscv = TimeSeriesSplit(n_splits=n_splits)

        fold_results = []
        all_probs = np.zeros(len(X))
        all_preds = np.zeros(len(X), dtype=int)
        all_mask = np.zeros(len(X), dtype=bool)

        for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(X)):
            X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
            y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

            if len(y_train.unique()) < 2 or len(y_test) < 10:
                continue

            clf = RandomForestClassifier(
                n_estimators=n_estimators,
                max_depth=max_depth,
                n_jobs=1,  # 1GB VM constraint
                random_state=42 + fold_idx,
                class_weight="balanced",
                min_samples_leaf=5,
            )
            clf.fit(X_train, y_train)

            probs = clf.predict_proba(X_test)[:, 1]
            preds = (probs >= 0.5).astype(int)

            all_probs[test_idx] = probs
            all_preds[test_idx] = preds
            all_mask[test_idx] = True

            try:
                auc = roc_auc_score(y_test, probs)
            except ValueError:
                auc = 0.5

            fold_results.append({
                "fold": fold_idx,
                "train_size": len(X_train),
                "test_size": len(X_test),
                "accuracy": round(accuracy_score(y_test, preds) * 100, 1),
                "precision": round(precision_score(y_test, preds, zero_division=0) * 100, 1),
                "recall": round(recall_score(y_test, preds, zero_division=0) * 100, 1),
                "f1": round(f1_score(y_test, preds, zero_division=0) * 100, 1),
                "auc_roc": round(auc, 4),
                "test_win_rate": round(y_test.mean() * 100, 1),
            })

        if not fold_results:
            return {"error": "no valid folds"}

        # Train final model on all data
        self._model = RandomForestClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            n_jobs=1,
            random_state=42,
            class_weight="balanced",
            min_samples_leaf=5,
        )
        self._model.fit(X, y)

        # Feature importances
        importance = dict(zip(
            self._feature_names,
            [round(float(v), 4) for v in self._model.feature_importances_],
        ))
        top_features = dict(sorted(importance.items(), key=lambda x: -x[1])[:20])

        # Aggregate OOS metrics
        oos_mask = all_mask
        if oos_mask.sum() > 0:
            oos_y = y[oos_mask]
            oos_probs = all_probs[oos_mask]
            oos_preds = all_preds[oos_mask]

            try:
                agg_auc = roc_auc_score(oos_y, oos_probs)
            except ValueError:
                agg_auc = 0.5

            agg_metrics = {
                "accuracy": round(accuracy_score(oos_y, oos_preds) * 100, 1),
                "precision": round(precision_score(oos_y, oos_preds, zero_division=0) * 100, 1),
                "recall": round(recall_score(oos_y, oos_preds, zero_division=0) * 100, 1),
                "f1": round(f1_score(oos_y, oos_preds, zero_division=0) * 100, 1),
                "auc_roc": round(agg_auc, 4),
            }
        else:
            agg_metrics = {}

        result = {
            "total_candidates": len(X),
            "base_win_rate": round(y.mean() * 100, 1),
            "folds": fold_results,
            "aggregate_oos": agg_metrics,
            "top_features": top_features,
        }

        self._results = result
        return result

    def compute_stratified_win_rates(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        veto_blocked: pd.Series,
    ) -> Dict:
        """Compute win rates for raw, rule-filtered, ML top-quartile, and ML top-decile.

        This is the key comparison showing whether ML adds value beyond hand-coded rules.
        """
        if self._model is None or len(X) == 0:
            return {"error": "model not trained or empty dataset"}

        probs = self._model.predict_proba(X)[:, 1]

        # Raw: all candidates (no filtering)
        raw_wr = y.mean() * 100

        # Rule-filtered: only candidates NOT blocked by veto logic
        passed_mask = ~veto_blocked
        if passed_mask.sum() > 0:
            rule_filtered_wr = y[passed_mask].mean() * 100
            rule_filtered_count = int(passed_mask.sum())
        else:
            rule_filtered_wr = 0.0
            rule_filtered_count = 0

        # ML top quartile (top 25% by predicted probability)
        q75 = np.percentile(probs, 75)
        top_q_mask = probs >= q75
        if top_q_mask.sum() > 0:
            ml_top_quartile_wr = y[top_q_mask].mean() * 100
            ml_top_quartile_count = int(top_q_mask.sum())
        else:
            ml_top_quartile_wr = 0.0
            ml_top_quartile_count = 0

        # ML top decile (top 10% by predicted probability)
        q90 = np.percentile(probs, 90)
        top_d_mask = probs >= q90
        if top_d_mask.sum() > 0:
            ml_top_decile_wr = y[top_d_mask].mean() * 100
            ml_top_decile_count = int(top_d_mask.sum())
        else:
            ml_top_decile_wr = 0.0
            ml_top_decile_count = 0

        # ML + rules combined (top quartile AND not vetoed)
        ml_plus_rules_mask = top_q_mask & passed_mask
        if ml_plus_rules_mask.sum() > 0:
            ml_plus_rules_wr = y[ml_plus_rules_mask].mean() * 100
            ml_plus_rules_count = int(ml_plus_rules_mask.sum())
        else:
            ml_plus_rules_wr = 0.0
            ml_plus_rules_count = 0

        return {
            "raw_wr": round(raw_wr, 1),
            "raw_count": len(y),
            "rule_filtered_wr": round(rule_filtered_wr, 1),
            "rule_filtered_count": rule_filtered_count,
            "ml_top_quartile_wr": round(ml_top_quartile_wr, 1),
            "ml_top_quartile_count": ml_top_quartile_count,
            "ml_top_decile_wr": round(ml_top_decile_wr, 1),
            "ml_top_decile_count": ml_top_decile_count,
            "ml_plus_rules_wr": round(ml_plus_rules_wr, 1),
            "ml_plus_rules_count": ml_plus_rules_count,
            "q75_threshold": round(float(q75), 4),
            "q90_threshold": round(float(q90), 4),
        }

    def evaluate_probability_buckets(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        n_buckets: int = 5,
    ) -> Dict:
        """Evaluate calibration: do predicted probabilities match actual win rates?

        Splits predictions into N buckets by predicted probability and
        checks if higher buckets genuinely outperform lower buckets.

        Returns bucket stats + monotonicity check.
        """
        if self._model is None or len(X) == 0:
            return {"error": "model not trained or empty dataset"}

        probs = self._model.predict_proba(X)[:, 1]

        # Create buckets by percentile
        bucket_edges = np.percentile(probs, np.linspace(0, 100, n_buckets + 1))
        buckets = []

        for b in range(n_buckets):
            lo, hi = bucket_edges[b], bucket_edges[b + 1]
            if b == n_buckets - 1:
                mask = probs >= lo
            else:
                mask = (probs >= lo) & (probs < hi)

            count = int(mask.sum())
            if count == 0:
                continue

            actual_wr = float(y[mask].mean() * 100)
            avg_pred = float(probs[mask].mean() * 100)

            buckets.append({
                "bucket": b + 1,
                "pred_range": f"{lo:.3f}-{hi:.3f}",
                "avg_predicted": round(avg_pred, 1),
                "actual_win_rate": round(actual_wr, 1),
                "count": count,
                "calibration_error": round(abs(avg_pred - actual_wr), 1),
            })

        # Check monotonicity: do higher buckets have higher actual WR?
        actual_wrs = [b["actual_win_rate"] for b in buckets]
        monotonic = all(actual_wrs[i] <= actual_wrs[i + 1]
                       for i in range(len(actual_wrs) - 1))

        # Rank correlation: how well does predicted rank match actual rank?
        if len(actual_wrs) >= 3:
            pred_ranks = list(range(len(actual_wrs)))
            actual_ranks = [sorted(actual_wrs).index(w) for w in actual_wrs]
            rank_corr = np.corrcoef(pred_ranks, actual_ranks)[0, 1]
        else:
            rank_corr = 0.0

        # Spread: difference between top and bottom bucket WR
        spread = actual_wrs[-1] - actual_wrs[0] if len(actual_wrs) >= 2 else 0.0

        # Compute bucket lift for top-bucket-only enforcement
        bucket_lift = {}
        if buckets:
            top_bucket_wr = buckets[-1]["actual_win_rate"]
            bottom_bucket_wr = buckets[0]["actual_win_rate"]
            avg_wr = sum(b["actual_win_rate"] for b in buckets) / len(buckets)

            top_lift = top_bucket_wr - avg_wr
            top_vs_bottom = top_bucket_wr - bottom_bucket_wr

            bucket_lift = {
                "top_bucket_wr": round(top_bucket_wr, 2),
                "bottom_bucket_wr": round(bottom_bucket_wr, 2),
                "avg_wr": round(avg_wr, 2),
                "top_lift_pct": round(top_lift, 2),
                "top_vs_bottom_pct": round(top_vs_bottom, 2),
                "top_bucket_viable": top_lift > 3.0,  # top bucket must be >3% better than avg
            }

        return {
            "buckets": buckets,
            "monotonic": monotonic,
            "rank_correlation": round(float(rank_corr), 3) if not np.isnan(rank_corr) else 0.0,
            "top_bottom_spread": round(spread, 1),
            "bucket_lift": bucket_lift,
            "verdict": "USEFUL" if spread > 5 and rank_corr > 0.5 else
                       "MARGINAL" if spread > 2 else "NOT_USEFUL",
        }

    def run_full_pipeline(
        self,
        df: pd.DataFrame,
        symbol: str,
        scanner_func=None,
        scanner_name: str = "unknown",
        n_splits: int = 5,
        n_estimators: int = 50,
        max_depth: int = 6,
        label_mode: str = "mfe",
        mfe_threshold_r: float = 0.2,
        mfe_max_bars: int = 30,
    ) -> Dict:
        """Run the complete candidate training pipeline end-to-end.

        1. Build dataset (scan → features → MFE labels)
        2. Train with walk-forward validation
        3. Compute stratified win rates
        4. Evaluate probability bucket calibration
        5. Save results (per scanner)

        Returns full results dict.
        """
        logger.info("=== CandidateTrainer: Starting pipeline for %s / %s (labels=%s, mfe_r=%.2f) ===",
                     scanner_name, symbol, label_mode, mfe_threshold_r)

        # Step 1: Build dataset with veto labels
        X, y, veto_blocked = self.build_dataset_with_veto_labels(
            df, symbol, scanner_func=scanner_func,
            label_mode=label_mode,
            mfe_threshold_r=mfe_threshold_r,
            mfe_max_bars=mfe_max_bars,
        )
        if len(X) < 100:
            result = {
                "error": f"insufficient candidates: {len(X)} (need 100+)",
                "symbol": symbol,
                "scanner": scanner_name,
                "candidates_found": len(X),
            }
            self._save_results(result, scanner_name)
            return result

        logger.info(
            "Dataset: %d candidates, %.1f%% raw WR, %d features",
            len(X), y.mean() * 100, len(X.columns),
        )

        # Step 2: Train
        train_result = self.train(X, y, n_splits=n_splits,
                                  n_estimators=n_estimators, max_depth=max_depth)
        if "error" in train_result:
            train_result["scanner"] = scanner_name
            self._save_results(train_result, scanner_name)
            return train_result

        # Step 3: Stratified win rates
        wr_comparison = self.compute_stratified_win_rates(X, y, veto_blocked)

        # Step 4: Probability bucket calibration check
        bucket_eval = self.evaluate_probability_buckets(X, y, n_buckets=5)

        # Step 5: Assemble final report
        result = {
            "symbol": symbol,
            "scanner": scanner_name,
            "label_mode": label_mode,
            "mfe_threshold_r": mfe_threshold_r,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dataset": {
                "total_candidates": len(X),
                "features": len(X.columns),
                "positive_rate": round(y.mean() * 100, 1),
                "veto_blocked_pct": round(veto_blocked.mean() * 100, 1),
            },
            "training": train_result,
            "win_rate_comparison": wr_comparison,
            "probability_calibration": bucket_eval,
            "feature_importances": train_result.get("top_features", {}),
        }

        self._save_results(result, scanner_name)

        # Save per-scanner model to disk for live scoring
        if self._model is not None:
            self.save_model(scanner_name)
            logger.info("Saved per-scanner model: %s", scanner_name)

        # Record training metrics for trend/drift tracking
        try:
            from ml_training.dashboard import _ModelHistoryTracker
            tracker = _ModelHistoryTracker()
            folds = train_result.get("folds", [])
            avg_auc = sum(f.get("auc_roc", 0) for f in folds) / len(folds) if folds else 0
            avg_acc = sum(f.get("accuracy", 0) for f in folds) / len(folds) if folds else 0
            avg_prec = sum(f.get("precision", 0) for f in folds) / len(folds) if folds else 0
            avg_recall = sum(f.get("recall", 0) for f in folds) / len(folds) if folds else 0
            tracker.record_training(
                scanner=scanner_name,
                metrics={
                    "auc_roc": avg_auc,
                    "accuracy": avg_acc,
                    "precision": avg_prec,
                    "recall": avg_recall,
                    "samples": len(X),
                    "n_features": len(X.columns),
                    "positive_rate": round(y.mean() * 100, 1),
                },
                feature_importances=train_result.get("top_features", {}),
            )
            logger.info("Recorded training history for %s (AUC=%.4f)", scanner_name, avg_auc)
        except Exception as e:
            logger.warning("Failed to record training history: %s", e)

        # Log summary
        logger.info("=== CandidateTrainer: Pipeline complete for %s / %s ===",
                     scanner_name, symbol)
        logger.info(
            "  Labels: %s (threshold=%.2fR) | Positive rate: %.1f%%",
            label_mode, mfe_threshold_r, y.mean() * 100,
        )
        logger.info(
            "  Raw: %.1f%% (%d) | Rules: %.1f%% (%d) | "
            "ML Q75: %.1f%% (%d) | ML D90: %.1f%% (%d)",
            wr_comparison.get("raw_wr", 0), wr_comparison.get("raw_count", 0),
            wr_comparison.get("rule_filtered_wr", 0), wr_comparison.get("rule_filtered_count", 0),
            wr_comparison.get("ml_top_quartile_wr", 0), wr_comparison.get("ml_top_quartile_count", 0),
            wr_comparison.get("ml_top_decile_wr", 0), wr_comparison.get("ml_top_decile_count", 0),
        )
        logger.info(
            "  Calibration: %s (spread=%.1f%%, rank_corr=%.3f)",
            bucket_eval.get("verdict", "?"),
            bucket_eval.get("top_bottom_spread", 0),
            bucket_eval.get("rank_correlation", 0),
        )

        return result

    def run_all_scanners(
        self,
        df: pd.DataFrame,
        symbol: str,
        scanners: Dict,
        n_splits: int = 5,
        n_estimators: int = 50,
        max_depth: int = 6,
        label_mode: str = "mfe",
        mfe_threshold_r: float = 0.2,
        mfe_max_bars: int = 30,
    ) -> Dict:
        """Run candidate training for ALL scanners and produce comparison.

        Args:
            df: OHLCV DataFrame
            symbol: e.g. "BTC/USDT"
            scanners: dict of {name: func} from trainer.py SCANNERS
            label_mode: "mfe" (default) or "trade"
            mfe_threshold_r: MFE threshold in R (default 0.2)

        Returns dict with per-scanner results and comparison table.
        """
        logger.info("=== Running candidate trainer for ALL %d scanners on %s (labels=%s) ===",
                     len(scanners), symbol, label_mode)

        all_results = {}
        comparison_rows = []

        for scanner_name, scanner_func in scanners.items():
            logger.info("--- Scanner: %s ---", scanner_name)
            # Fresh trainer per scanner (separate model)
            trainer = CandidateTrainer(self._config)
            result = trainer.run_full_pipeline(
                df, symbol,
                scanner_func=scanner_func,
                scanner_name=scanner_name,
                n_splits=n_splits,
                n_estimators=n_estimators,
                max_depth=max_depth,
                label_mode=label_mode,
                mfe_threshold_r=mfe_threshold_r,
                mfe_max_bars=mfe_max_bars,
            )
            all_results[scanner_name] = result

            # Build comparison row
            wr = result.get("win_rate_comparison", {})
            cal = result.get("probability_calibration", {})
            comparison_rows.append({
                "scanner": scanner_name,
                "candidates": result.get("dataset", {}).get("total_candidates", 0),
                "raw_wr": wr.get("raw_wr", 0),
                "rule_filtered_wr": wr.get("rule_filtered_wr", 0),
                "rule_filtered_count": wr.get("rule_filtered_count", 0),
                "ml_top_quartile_wr": wr.get("ml_top_quartile_wr", 0),
                "ml_top_decile_wr": wr.get("ml_top_decile_wr", 0),
                "ml_plus_rules_wr": wr.get("ml_plus_rules_wr", 0),
                "oos_auc": result.get("training", {}).get("aggregate_oos", {}).get("auc_roc", 0),
                "calibration": cal.get("verdict", "?"),
                "bucket_spread": cal.get("top_bottom_spread", 0),
                "error": result.get("error", None),
            })

        # Sort comparison by ml_plus_rules_wr descending
        comparison_rows.sort(key=lambda r: r.get("ml_plus_rules_wr", 0), reverse=True)

        # Log comparison table
        logger.info("=== ALL-SCANNER COMPARISON for %s ===", symbol)
        logger.info("%-20s %6s %6s %6s %6s %6s %6s %6s",
                     "Scanner", "Cands", "RawWR", "RuleWR", "MLQ75", "MLD90", "ML+R", "AUC")
        for row in comparison_rows:
            if row.get("error"):
                logger.info("%-20s  ERROR: %s", row["scanner"], row["error"])
            else:
                logger.info("%-20s %6d %5.1f%% %5.1f%% %5.1f%% %5.1f%% %5.1f%% %.4f",
                            row["scanner"], row["candidates"],
                            row["raw_wr"], row["rule_filtered_wr"],
                            row["ml_top_quartile_wr"], row["ml_top_decile_wr"],
                            row["ml_plus_rules_wr"], row["oos_auc"])

        # Save combined comparison
        combined = {
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "comparison": comparison_rows,
            "per_scanner": all_results,
        }
        path = RESULTS_DIR / "candidate_all_scanners.json"
        try:
            path.write_text(json.dumps(combined, default=str, indent=2))
            logger.info("All-scanner results saved to %s", path)
        except Exception as e:
            logger.warning("Failed to save combined results: %s", e)

        return combined

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Score new candidates with the trained model.

        Returns array of win probabilities [0, 1].
        """
        if self._model is None:
            raise RuntimeError("Model not trained. Call train() first.")

        # Ensure feature alignment
        missing = set(self._feature_names) - set(X.columns)
        if missing:
            for col in missing:
                X[col] = 0.0
        X = X[self._feature_names].fillna(0)

        return self._model.predict_proba(X)[:, 1]

    def score_candidate(
        self, df: pd.DataFrame, idx: int, side: str, symbol: str,
    ) -> float:
        """Score a single candidate at bar idx. Returns win probability.

        Can be called from the live strategy to get ML confidence.
        """
        if self._model is None:
            raise RuntimeError("Model not trained.")

        gate_feats = _compute_gate_veto_features(df, idx, side, symbol)

        # Build market features for this bar
        if "ema_8" not in df.columns:
            df = compute_indicators(df)
        mkt_features = build_features(df)
        mkt_row = mkt_features.iloc[idx]
        mkt_dict = {}
        for col in mkt_row.index:
            try:
                val = float(mkt_row[col])
                if not np.isnan(val):
                    mkt_dict[f"mkt_{col}"] = val
            except (TypeError, ValueError):
                continue

        combined = {**gate_feats, **mkt_dict}
        row_df = pd.DataFrame([combined])

        # Align columns
        missing = set(self._feature_names) - set(row_df.columns)
        for col in missing:
            row_df[col] = 0.0
        row_df = row_df[self._feature_names].fillna(0)

        return float(self._model.predict_proba(row_df)[0, 1])

    def get_model(self) -> Optional[RandomForestClassifier]:
        """Return the trained model (for serialization or inspection)."""
        return self._model

    def get_feature_names(self) -> List[str]:
        """Return ordered feature names used by the model."""
        return self._feature_names

    def get_results(self) -> Dict:
        """Return the latest training results."""
        return self._results

    def _save_results(self, results: Dict, scanner_name: str = "unknown"):
        """Save results to storage/ml_models/candidate_{scanner_name}.json."""
        path = RESULTS_DIR / f"candidate_{scanner_name}.json"
        try:
            path.write_text(json.dumps(results, default=str, indent=2))
            logger.info("Results saved to %s", path)
        except Exception as e:
            logger.warning("Failed to save results: %s", e)

    # ──────────────────────────────────────────────────────────────────────
    # Model persistence for live scoring API
    # ──────────────────────────────────────────────────────────────────────

    def save_model(self, scanner_name: str):
        """Persist trained RandomForest model + feature names to disk for live scoring."""
        if self._model is None:
            logger.warning("No model to save for %s", scanner_name)
            return
        import joblib
        model_path = RESULTS_DIR / f"model_{scanner_name}.joblib"
        meta_path = RESULTS_DIR / f"model_{scanner_name}_features.json"

        joblib.dump(self._model, model_path)
        meta = {
            "scanner": scanner_name,
            "feature_names": self._feature_names,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "n_features": len(self._feature_names),
        }
        meta_path.write_text(json.dumps(meta, indent=2))
        logger.info("Saved model for %s: %s (%d features)", scanner_name, model_path, len(self._feature_names))

    @classmethod
    def load_model(cls, scanner_name: str):
        """Load a trained model from disk. Returns (model, feature_names) or (None, [])."""
        import joblib
        model_path = RESULTS_DIR / f"model_{scanner_name}.joblib"
        meta_path = RESULTS_DIR / f"model_{scanner_name}_features.json"
        if not model_path.exists():
            return None, []
        model = joblib.load(model_path)
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        return model, meta.get("feature_names", [])


# ──────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from ml_training.trainer import SCANNERS

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Train candidate classifier")
    parser.add_argument("--csv", type=str, help="Path to OHLCV CSV file")
    parser.add_argument("--symbol", type=str, default="BTC/USDT")
    parser.add_argument("--scanner", type=str, default="all",
                        help="Scanner name or 'all' for all scanners")
    parser.add_argument("--n-estimators", type=int, default=50)
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--n-splits", type=int, default=5)
    args = parser.parse_args()

    if args.csv:
        df = pd.read_csv(args.csv, parse_dates=["timestamp"], index_col="timestamp")
        trainer = CandidateTrainer()

        if args.scanner == "all":
            results = trainer.run_all_scanners(
                df, args.symbol, SCANNERS,
                n_estimators=args.n_estimators,
                max_depth=args.max_depth,
                n_splits=args.n_splits,
            )
        else:
            if args.scanner not in SCANNERS:
                print(f"Unknown scanner: {args.scanner}. Available: {list(SCANNERS.keys())}")
                sys.exit(1)
            results = trainer.run_full_pipeline(
                df, args.symbol,
                scanner_func=SCANNERS[args.scanner],
                scanner_name=args.scanner,
                n_estimators=args.n_estimators,
                max_depth=args.max_depth,
                n_splits=args.n_splits,
            )
        print(json.dumps(results, indent=2, default=str))
    else:
        print("Usage: python candidate_trainer.py --csv candles.csv --symbol BTC/USDT [--scanner all|structure_bounce|...]")
