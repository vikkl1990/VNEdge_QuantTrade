"""
ML Scorer — Live Scoring Client
================================
Sends candidate features to VM2's ML API for probability scoring.
Fail-open design: if VM2 is unreachable, returns neutral score (0.5).

Architecture:
  VM1 (live bot) → POST /api/score → VM2 (ML server) → probability
"""

import logging
import time
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# VM2 ML server
ML_SERVER_URL = "http://129.80.31.92:8081/api/score"
SCORE_TIMEOUT = 2.0  # seconds — scalp signals are time-sensitive


class MLScorer:
    """Scores scanner candidates via VM2 ML API. Fail-open design."""

    def __init__(self, url: str = ML_SERVER_URL, enabled: bool = True,
                 shadow_mode: bool = True):
        """
        Args:
            url: VM2 scoring endpoint
            enabled: Master switch
            shadow_mode: If True, log ML score but never veto trades
        """
        self._url = url
        self._enabled = enabled
        self._shadow_mode = shadow_mode
        self._last_error: Optional[str] = None
        self._scores_log: list = []  # rolling log of recent scores
        self._stats = {"calls": 0, "errors": 0, "avg_latency_ms": 0}

    def score_candidate(
        self,
        scanner_name: str,
        features: Dict[str, float],
    ) -> Dict:
        """Score a candidate synchronously. Returns score dict.

        Always returns a result — never raises.
        """
        if not self._enabled:
            return {"probability": 0.5, "verdict": "DISABLED", "scanner": scanner_name}

        import requests  # lazy import — not needed if disabled

        self._stats["calls"] += 1
        t0 = time.time()

        try:
            resp = requests.post(
                self._url,
                json={"scanner": scanner_name, "features": features},
                timeout=SCORE_TIMEOUT,
            )
            latency_ms = (time.time() - t0) * 1000
            self._stats["avg_latency_ms"] = (
                self._stats["avg_latency_ms"] * 0.9 + latency_ms * 0.1
            )

            if resp.status_code == 200:
                result = resp.json()
                result["latency_ms"] = round(latency_ms, 1)
                self._last_error = None

                # Log for analysis
                self._scores_log.append({
                    "time": time.time(),
                    "scanner": scanner_name,
                    "probability": result.get("probability", 0.5),
                    "verdict": result.get("verdict", "?"),
                })
                # Keep last 100
                if len(self._scores_log) > 100:
                    self._scores_log = self._scores_log[-100:]

                return result
            else:
                self._stats["errors"] += 1
                self._last_error = f"HTTP {resp.status_code}"
                return {
                    "probability": 0.5, "verdict": "API_ERROR",
                    "scanner": scanner_name, "error": f"HTTP {resp.status_code}"
                }

        except Exception as e:
            self._stats["errors"] += 1
            self._last_error = str(e)
            logger.warning("ML score error for %s: %s", scanner_name, e)
            return {"probability": 0.5, "verdict": "UNREACHABLE", "scanner": scanner_name}

    def should_take_trade(self, score_result: Dict, threshold: float = 0.40) -> bool:
        """Decide whether to take trade based on ML score.

        In shadow_mode, always returns True (log only, never veto).
        """
        if self._shadow_mode:
            return True  # never veto in shadow mode
        prob = score_result.get("probability", 0.5)
        return prob >= threshold

    def get_stats(self) -> Dict:
        return {
            **self._stats,
            "last_error": self._last_error,
            "enabled": self._enabled,
            "shadow_mode": self._shadow_mode,
            "recent_scores": len(self._scores_log),
        }


def build_scoring_features(
    df: pd.DataFrame,
    idx: int,
    side: str,
    symbol: str,
) -> Dict[str, float]:
    """Build the feature dict for ML scoring from live candle data.

    This mirrors _compute_gate_veto_features() + market-state features
    from candidate_trainer.py. Must produce the SAME feature names
    that the model was trained on.

    Args:
        df: DataFrame with OHLCV + indicators (from compute_indicators)
        idx: Current bar index (-1 for last bar)
        side: "long" or "short"
        symbol: Trading symbol
    """
    # No dependency on ml_training — all features computed inline

    if idx < 0:
        idx = len(df) + idx

    row = df.iloc[idx]
    c = float(row["close"])
    h = float(row["high"])
    l = float(row["low"])
    o = float(row["open"])
    # Try atr_14 (training format) or atr (live bot format)
    atr = float(row.get("atr_14", row.get("atr", 0)))
    if atr <= 0:
        atr = 1e-8

    features = {}

    # ── Regime (one-hot) ──
    ema_21 = float(row.get("ema_21", 0))
    ema_50 = float(row.get("ema_50", 0))
    ema_200 = float(row.get("ema_200", 0))
    atr_col = "atr_14" if "atr_14" in df.columns else "atr"
    avg_atr = df[atr_col].iloc[max(0, idx - 100):idx].mean() if idx > 100 and atr_col in df.columns else atr
    bb_width = float(row.get("bb_width", 0))

    regime = "sideways"
    if avg_atr > 0 and atr / avg_atr < 0.7:
        regime = "quiet"
    elif ema_21 > ema_50 > ema_200 and c > ema_21:
        regime = "trending_up"
    elif ema_21 < ema_50 < ema_200 and c < ema_21:
        regime = "trending_down"
    elif bb_width > 0:
        avg_bbw = df["bb_width"].iloc[max(0, idx - 50):idx].mean()
        if avg_bbw > 0 and bb_width / avg_bbw > 1.5:
            regime = "volatile"
        elif avg_bbw > 0 and bb_width / avg_bbw < 0.5:
            regime = "ranging"

    for cat in ["trending_up", "trending_down", "ranging", "volatile", "quiet", "sideways"]:
        features[f"regime_{cat}"] = 1.0 if regime == cat else 0.0

    # ── Regime stability ──
    features["regime_stability"] = 0.5
    if idx >= 70:
        same_count = sum(1 for k in range(max(50, idx - 20), idx)
                        if _quick_regime_match(df.iloc[k], regime))
        features["regime_stability"] = same_count / 20.0

    # ── HTF alignment ──
    htf_bullish = c > ema_50 if ema_50 > 0 else True
    features["htf_alignment"] = (1.0 if htf_bullish else -1.0) * (1 if side == "long" else -1)

    # ── EMA slopes ──
    ema8 = float(row.get("ema_8", 0))
    features["ema8_slope"] = (ema8 - float(df.iloc[idx - 3].get("ema_8", ema8))) / atr if idx >= 3 and ema8 > 0 else 0.0
    features["ema21_slope"] = (ema_21 - float(df.iloc[idx - 5].get("ema_21", ema_21))) / atr if idx >= 5 and ema_21 > 0 else 0.0

    # ── Trend strength ──
    features["trend_strength"] = (ema8 - ema_21) / atr if ema_21 > 0 else 0.0
    features["trend_strength_long"] = (ema_21 - ema_50) / atr if ema_50 > 0 else 0.0

    # ── VWAP distance ──
    vwap = float(row.get("vwap", 0))
    features["vwap_distance"] = (c - vwap) / atr if vwap > 0 else 0.0

    # ── Volume ──
    vol_sma = float(row.get("vol_sma_20", 0))
    vol_std = float(row.get("vol_std_20", 0))
    volume = float(row.get("volume", 0))
    features["volume_zscore"] = (volume - vol_sma) / vol_std if vol_std > 0 else 0.0
    features["rel_vol"] = float(row.get("rel_vol", 1.0))

    # ── Session (one-hot) ──
    session = "unknown"
    if hasattr(df.index[idx], "hour"):
        hour = df.index[idx].hour
        session = "asia_late" if hour < 6 else "asia_early" if hour < 12 else "europe" if hour < 18 else "us"
    for cat in ["asia_late", "asia_early", "europe", "us"]:
        features[f"session_{cat}"] = 1.0 if session == cat else 0.0

    # ── ATR ratio ──
    features["atr_ratio"] = atr / avg_atr if avg_atr > 0 else 1.0
    features["atr_expansion"] = float(row.get("atr_7", atr)) / atr if atr > 0 else 1.0

    # ── Swing distances ──
    recent = df.iloc[max(0, idx - 20):idx]
    if len(recent) > 0:
        features["dist_from_swing_high"] = (c - float(recent["high"].max())) / atr
        features["dist_from_swing_low"] = (c - float(recent["low"].min())) / atr
    else:
        features["dist_from_swing_high"] = 0.0
        features["dist_from_swing_low"] = 0.0

    # ── Candle structure ──
    candle_body = abs(c - o)
    candle_range = h - l
    features["impulse_body_atr"] = candle_body / atr
    features["dist_from_ema8"] = abs(c - ema8) / atr if ema8 > 0 else 0.0
    features["body_ratio"] = candle_body / candle_range if candle_range > 0 else 0.0
    features["range_vs_atr"] = candle_range / atr
    features["upper_wick_ratio"] = (h - max(c, o)) / candle_range if candle_range > 0 else 0.0
    features["lower_wick_ratio"] = (min(c, o) - l) / candle_range if candle_range > 0 else 0.0

    # ── Regime-side alignment ──
    if regime == "trending_up":
        features["regime_side_alignment"] = 1.0 if side == "long" else -1.0
    elif regime == "trending_down":
        features["regime_side_alignment"] = 1.0 if side == "short" else -1.0
    else:
        features["regime_side_alignment"] = 0.0

    # ── RSI / BB ──
    rsi = float(row.get("rsi_14", 50))
    features["rsi_zone"] = (rsi - 50) / 50
    bb_upper = float(row.get("bb_upper", 0))
    bb_lower = float(row.get("bb_lower", 0))
    features["bb_position"] = (c - bb_lower) / (bb_upper - bb_lower) if bb_upper > bb_lower else 0.5

    # ── Side ──
    features["side_long"] = 1.0 if side == "long" else 0.0

    # ── Rule-derived features ──
    features["rule_htf_pass"] = 1.0 if features.get("htf_alignment", 0) >= 0 else 0.0
    features["rule_session_pass"] = 0.0 if features.get("session_asia_late", 0) > 0.5 else 1.0
    features["rule_vol_pass"] = 1.0 if features.get("atr_ratio", 1.0) >= 0.88 else 0.0
    features["rule_volume_pass"] = 1.0 if features.get("rel_vol", 1.0) >= 1.0 else 0.0
    features["rule_candle_pass"] = 1.0 if features.get("body_ratio", 0.5) >= 0.3 else 0.0
    features["rule_chase_pass"] = 1.0 if features.get("impulse_body_atr", 0) <= 1.25 else 0.0
    features["rule_stretch_pass"] = 1.0 if features.get("dist_from_ema8", 0) <= 0.7 else 0.0
    features["rule_regime_pass"] = 1.0 if features.get("regime_side_alignment", 0) >= -0.5 else 0.0
    features["rules_passed_count"] = sum([
        features["rule_htf_pass"], features["rule_session_pass"],
        features["rule_vol_pass"], features["rule_volume_pass"],
        features["rule_candle_pass"], features["rule_chase_pass"],
        features["rule_stretch_pass"], features["rule_regime_pass"],
    ])
    features["rules_passed_pct"] = features["rules_passed_count"] / 8.0

    # ── Market-state features (mkt_ prefix, matches training) ──
    # Computed inline — no dependency on ml_training module.
    # These are pure candle math features from build_features().
    try:
        features.update(_compute_mkt_features(df, idx, atr, avg_atr))
    except Exception as e:
        logger.warning("Failed to build mkt_ features: %s", e)

    return features


def _compute_mkt_features(df: pd.DataFrame, idx: int, atr: float, avg_atr: float) -> Dict[str, float]:
    """Compute all 62 mkt_* features inline (mirrors build_features() from ml_training).

    Pure candle math — no external dependencies. Works on VM1 without ml_training.
    """
    mkt = {}

    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float)

    # ATR column
    atr_col = "atr_14" if "atr_14" in df.columns else "atr"
    atr_s = df[atr_col].astype(float) if atr_col in df.columns else pd.Series(atr, index=df.index)

    # Candle components (compute if not present)
    body = (c - o).abs()
    candle_range = (h - l).replace(0, np.nan)
    body_ratio = body / candle_range
    upper_wick = h - pd.concat([c, o], axis=1).max(axis=1)
    lower_wick = pd.concat([c, o], axis=1).min(axis=1) - l
    is_bullish = (c > o).astype(int)

    # EMA columns (use existing or compute)
    ema8 = df["ema_8"].astype(float) if "ema_8" in df.columns else c.ewm(span=8, adjust=False).mean()
    ema21 = df["ema_21"].astype(float) if "ema_21" in df.columns else c.ewm(span=21, adjust=False).mean()
    ema50 = df["ema_50"].astype(float) if "ema_50" in df.columns else c.ewm(span=50, adjust=False).mean()

    # VWAP
    if "vwap" in df.columns:
        vwap = df["vwap"].astype(float)
    else:
        cum_vol = v.cumsum()
        cum_vp = (c * v).cumsum()
        vwap = cum_vp / cum_vol.replace(0, np.nan)

    # Volume stats
    vol_sma_20 = df["vol_sma_20"].astype(float) if "vol_sma_20" in df.columns else v.rolling(20).mean()
    vol_std_20 = df["vol_std_20"].astype(float) if "vol_std_20" in df.columns else v.rolling(20).std()

    # ATR short
    if "atr_7" in df.columns:
        atr7 = df["atr_7"].astype(float)
    else:
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr7 = tr.rolling(7).mean()

    # Helper: safe get at idx
    def _g(series, i=idx):
        try:
            val = float(series.iloc[i])
            return val if not np.isnan(val) else 0.0
        except Exception:
            return 0.0

    atr_val = _g(atr_s)
    if atr_val <= 0:
        atr_val = 1e-8
    c_val = _g(c)

    # ── 1. MOMENTUM ──
    mkt["mkt_return_1"] = float(c.pct_change(1).iloc[idx]) if idx >= 1 else 0.0
    mkt["mkt_return_3"] = float(c.pct_change(3).iloc[idx]) if idx >= 3 else 0.0
    mkt["mkt_return_5"] = float(c.pct_change(5).iloc[idx]) if idx >= 5 else 0.0
    mkt["mkt_return_10"] = float(c.pct_change(10).iloc[idx]) if idx >= 10 else 0.0
    mkt["mkt_return_20"] = float(c.pct_change(20).iloc[idx]) if idx >= 20 else 0.0
    mkt["mkt_momentum_accel"] = mkt["mkt_return_1"] - mkt["mkt_return_5"]

    # ── 2. VOLATILITY ──
    mkt["mkt_atr_ratio"] = atr_val / c_val if c_val > 0 else 0.0
    mkt["mkt_range_vs_atr"] = _g(candle_range) / atr_val
    mkt["mkt_atr_expansion"] = _g(atr7) / atr_val if atr_val > 0 else 1.0
    atr_100_mean = float(atr_s.iloc[max(0, idx - 100):idx + 1].mean()) if idx > 0 else atr_val
    mkt["mkt_vol_regime"] = atr_val / atr_100_mean if atr_100_mean > 0 else 1.0

    # ── 3. CANDLE STRUCTURE ──
    mkt["mkt_body_ratio"] = _g(body_ratio)
    mkt["mkt_upper_wick_ratio"] = _g(upper_wick) / _g(candle_range) if _g(candle_range) > 0 else 0.0
    mkt["mkt_lower_wick_ratio"] = _g(lower_wick) / _g(candle_range) if _g(candle_range) > 0 else 0.0
    mkt["mkt_body_displacement"] = _g(body) / atr_val
    rng = _g(candle_range)
    mkt["mkt_close_position"] = (c_val - _g(l)) / rng if rng > 0 else 0.5

    # ── 4. TREND STRENGTH ──
    mkt["mkt_trend_strength"] = (_g(ema8) - _g(ema21)) / atr_val
    mkt["mkt_trend_strength_long"] = (_g(ema21) - _g(ema50)) / atr_val
    mkt["mkt_ema_slope_8"] = float(ema8.pct_change(3).iloc[idx]) if idx >= 3 else 0.0
    mkt["mkt_ema_slope_21"] = float(ema21.pct_change(5).iloc[idx]) if idx >= 5 else 0.0
    mkt["mkt_dist_from_ema21"] = (c_val - _g(ema21)) / atr_val

    # ── 5. VOLUME ──
    vs20 = _g(vol_sma_20)
    vstd = _g(vol_std_20)
    vol_val = _g(v)
    mkt["mkt_volume_zscore"] = (vol_val - vs20) / vstd if vstd > 0 else 0.0
    mkt["mkt_volume_spike"] = vol_val / vs20 if vs20 > 0 else 1.0

    # ── 6. MARKET CONTEXT ──
    mkt["mkt_dist_from_vwap"] = (c_val - _g(vwap)) / atr_val
    rolling_high = float(h.iloc[max(0, idx - 20):idx + 1].max()) if idx > 0 else _g(h)
    rolling_low = float(l.iloc[max(0, idx - 20):idx + 1].min()) if idx > 0 else _g(l)
    mkt["mkt_dist_from_high"] = (c_val - rolling_high) / atr_val
    mkt["mkt_dist_from_low"] = (c_val - rolling_low) / atr_val
    rolling_range = rolling_high - rolling_low
    mkt["mkt_range_position"] = (c_val - rolling_low) / rolling_range if rolling_range > 0 else 0.5

    # ── 7. COMPRESSION ──
    range_5 = float(candle_range.iloc[max(0, idx - 5):idx + 1].mean()) if idx >= 5 else _g(candle_range)
    range_20 = float(candle_range.iloc[max(0, idx - 20):idx + 1].mean()) if idx >= 20 else range_5
    mkt["mkt_vol_compression"] = range_5 / range_20 if range_20 > 0 else 1.0

    # ── 8. TIME FEATURES ──
    if hasattr(df.index, 'hour') and len(df.index) > idx:
        try:
            hour = df.index[idx].hour
            mkt["mkt_hour_sin"] = float(np.sin(2 * np.pi * hour / 24))
            mkt["mkt_hour_cos"] = float(np.cos(2 * np.pi * hour / 24))
            mkt["mkt_session"] = 0.0 if hour < 8 else (1.0 if hour < 16 else 2.0)
            mkt["mkt_dow_sin"] = float(np.sin(2 * np.pi * df.index[idx].dayofweek / 7))
        except Exception:
            mkt["mkt_hour_sin"] = 0.0
            mkt["mkt_hour_cos"] = 0.0
            mkt["mkt_session"] = 1.0
            mkt["mkt_dow_sin"] = 0.0
    else:
        from datetime import datetime
        now = datetime.utcnow()
        mkt["mkt_hour_sin"] = float(np.sin(2 * np.pi * now.hour / 24))
        mkt["mkt_hour_cos"] = float(np.cos(2 * np.pi * now.hour / 24))
        mkt["mkt_session"] = 0.0 if now.hour < 8 else (1.0 if now.hour < 16 else 2.0)
        mkt["mkt_dow_sin"] = float(np.sin(2 * np.pi * now.weekday() / 7))

    # ── 9. STATE TRANSITION / DELTA FEATURES ──
    # trend_change: current trend_strength - trend_strength 5 bars ago
    ts_now = mkt["mkt_trend_strength"]
    if idx >= 5:
        atr_5ago = _g(atr_s, idx - 5)
        if atr_5ago <= 0:
            atr_5ago = 1e-8
        ts_5ago = (_g(ema8, idx - 5) - _g(ema21, idx - 5)) / atr_5ago
        mkt["mkt_trend_change"] = ts_now - ts_5ago
    else:
        mkt["mkt_trend_change"] = 0.0

    if idx >= 3:
        atr_3ago = _g(atr_s, idx - 3)
        if atr_3ago <= 0:
            atr_3ago = 1e-8
        ts_3ago = (_g(ema8, idx - 3) - _g(ema21, idx - 3)) / atr_3ago
        mkt["mkt_trend_change_3"] = ts_now - ts_3ago
    else:
        mkt["mkt_trend_change_3"] = 0.0

    tsl_now = mkt["mkt_trend_strength_long"]
    if idx >= 5:
        atr_5ago = _g(atr_s, idx - 5)
        if atr_5ago <= 0:
            atr_5ago = 1e-8
        tsl_5ago = (_g(ema21, idx - 5) - _g(ema50, idx - 5)) / atr_5ago
        mkt["mkt_trend_long_change"] = tsl_now - tsl_5ago
    else:
        mkt["mkt_trend_long_change"] = 0.0

    # Volatility change
    if idx >= 5:
        atr_5ago_val = _g(atr_s, idx - 5)
        mkt["mkt_vol_change"] = atr_val / atr_5ago_val - 1 if atr_5ago_val > 0 else 0.0
    else:
        mkt["mkt_vol_change"] = 0.0

    # ATR ratio change
    if idx >= 103:
        atr_3ago_val = _g(atr_s, idx - 3)
        atr_ratio_prev_mean = float(atr_s.iloc[max(0, idx - 103):idx - 3].mean())
        atr_ratio_prev = atr_3ago_val / atr_ratio_prev_mean if atr_ratio_prev_mean > 0 else 1.0
        mkt["mkt_atr_ratio_change"] = mkt["mkt_vol_regime"] - atr_ratio_prev
    else:
        mkt["mkt_atr_ratio_change"] = 0.0

    # VWAP distance change
    if idx >= 3:
        atr_3ago = _g(atr_s, idx - 3)
        if atr_3ago <= 0:
            atr_3ago = 1e-8
        vwap_dist_prev = (_g(c, idx - 3) - _g(vwap, idx - 3)) / atr_3ago
        mkt["mkt_vwap_dist_change"] = mkt["mkt_dist_from_vwap"] - vwap_dist_prev
    else:
        mkt["mkt_vwap_dist_change"] = 0.0
    mkt["mkt_vwap_reversion_speed"] = mkt["mkt_vwap_dist_change"]

    # Volume change
    vol_5_mean = float(v.iloc[max(0, idx - 5):idx + 1].mean()) if idx >= 5 else vol_val
    mkt["mkt_volume_change"] = vol_val / vol_5_mean - 1 if vol_5_mean > 0 else 0.0

    # Impulse decay
    ret1_abs = abs(mkt["mkt_return_1"])
    ret5_abs = abs(mkt["mkt_return_5"])
    mkt["mkt_impulse_decay"] = min(ret1_abs / ret5_abs if ret5_abs > 0 else 1.0, 5.0)

    # Range change
    range_3 = float(candle_range.iloc[max(0, idx - 3):idx + 1].mean()) if idx >= 3 else _g(candle_range)
    range_10 = float(candle_range.iloc[max(0, idx - 10):idx + 1].mean()) if idx >= 10 else range_3
    mkt["mkt_range_change"] = range_3 / range_10 if range_10 > 0 else 1.0

    # EMA slope change
    if idx >= 6:
        ema_slope_now = mkt["mkt_ema_slope_8"]
        ema_slope_prev = float(ema8.pct_change(3).iloc[idx - 3]) if idx >= 6 else ema_slope_now
        mkt["mkt_ema_slope_change"] = ema_slope_now - ema_slope_prev
    else:
        mkt["mkt_ema_slope_change"] = 0.0

    # ── 9b. RECENT BEHAVIOR MEMORY ──
    mkt["mkt_last_3_return"] = mkt["mkt_return_3"]
    mkt["mkt_last_5_volatility"] = range_5 / atr_val if atr_val > 0 else 1.0
    if idx >= 5:
        bull_count = float(is_bullish.iloc[max(0, idx - 5):idx + 1].sum())
        mkt["mkt_trend_persistence"] = (bull_count - 2.5) / 2.5
    else:
        mkt["mkt_trend_persistence"] = 0.0

    # ── 10. MULTI-TIMEFRAME (rolling windows) ──
    mkt["mkt_return_5bar"] = mkt["mkt_return_5"]
    mkt["mkt_atr_5bar"] = range_5 / c_val if c_val > 0 else 0.0
    mkt["mkt_return_15bar"] = float(c.pct_change(15).iloc[idx]) if idx >= 15 else 0.0
    range_15 = float(candle_range.iloc[max(0, idx - 15):idx + 1].mean()) if idx >= 15 else range_5
    mkt["mkt_atr_15bar"] = range_15 / c_val if c_val > 0 else 0.0

    # TF agreement
    ema8_val = _g(ema8)
    ema21_val = _g(ema21)
    ema50_val = _g(ema50)
    sign_fast_med = 1.0 if ema8_val > ema21_val else (-1.0 if ema8_val < ema21_val else 0.0)
    sign_med_slow = 1.0 if ema21_val > ema50_val else (-1.0 if ema21_val < ema50_val else 0.0)
    mkt["mkt_tf_agreement"] = (sign_fast_med + sign_med_slow) / 2.0

    # ── 11. FVG (Fair Value Gap) ──
    if idx >= 2:
        # Bullish FVG: low[idx] > high[idx-2]
        l_val = _g(l)
        h_2ago = _g(h, idx - 2)
        fvg_bull = 1.0 if l_val > h_2ago else 0.0
        fvg_bull_size = max(l_val - h_2ago, 0) / atr_val

        # Bearish FVG: high[idx] < low[idx-2]
        h_val = _g(h)
        l_2ago = _g(l, idx - 2)
        fvg_bear = 1.0 if h_val < l_2ago else 0.0
        fvg_bear_size = max(l_2ago - h_val, 0) / atr_val
    else:
        fvg_bull = 0.0
        fvg_bull_size = 0.0
        fvg_bear = 0.0
        fvg_bear_size = 0.0

    mkt["mkt_fvg_bullish"] = fvg_bull
    mkt["mkt_fvg_bull_size"] = fvg_bull_size
    mkt["mkt_fvg_bearish"] = fvg_bear
    mkt["mkt_fvg_bear_size"] = fvg_bear_size
    mkt["mkt_fvg_present"] = 1.0 if (fvg_bull + fvg_bear) > 0 else 0.0

    # FVG recent lookbacks (scan last N bars)
    def _fvg_recent(n, bull=True):
        count = 0.0
        max_size = 0.0
        for k in range(max(2, idx - n), idx + 1):
            if k < 2:
                continue
            lk = _g(l, k)
            hk = _g(h, k)
            hk2 = _g(h, k - 2)
            lk2 = _g(l, k - 2)
            if bull:
                if lk > hk2:
                    count = 1.0
                    size = max(lk - hk2, 0) / atr_val
                    max_size = max(max_size, size)
            else:
                if hk < lk2:
                    count = 1.0
                    size = max(lk2 - hk, 0) / atr_val
                    max_size = max(max_size, size)
        return count, max_size

    bull5, _ = _fvg_recent(5, bull=True)
    bear5, _ = _fvg_recent(5, bull=False)
    bull10, bull10_max = _fvg_recent(10, bull=True)
    bear10, bear10_max = _fvg_recent(10, bull=False)

    mkt["mkt_fvg_bull_recent_5"] = bull5
    mkt["mkt_fvg_bear_recent_5"] = bear5
    mkt["mkt_fvg_bull_recent_10"] = bull10
    mkt["mkt_fvg_bear_recent_10"] = bear10
    mkt["mkt_fvg_max_bull_size_10"] = bull10_max
    mkt["mkt_fvg_max_bear_size_10"] = bear10_max

    # FVG trend aligned
    mkt["mkt_fvg_trend_aligned"] = (
        fvg_bull * (1.0 if mkt["mkt_trend_strength"] > 0 else 0.0) +
        fvg_bear * (1.0 if mkt["mkt_trend_strength"] < 0 else 0.0)
    )

    # Clean NaN/inf values
    for k, val in mkt.items():
        if not np.isfinite(val):
            mkt[k] = 0.0

    return mkt


def _quick_regime_match(row, regime: str) -> bool:
    """Quick regime check for stability calculation."""
    try:
        ema21 = float(row.get("ema_21", 0))
        ema50 = float(row.get("ema_50", 0))
        close = float(row["close"])
        if regime == "trending_up":
            return ema21 > ema50 and close > ema21
        elif regime == "trending_down":
            return ema21 < ema50 and close < ema21
        return True  # approximate for other regimes
    except Exception:
        return True
