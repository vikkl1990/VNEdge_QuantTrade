"""
ML Scorer — Live Scoring Client
================================
Sends candidate features to VM2's ML API for probability scoring.
Fail-open design: if VM2 is unreachable, returns neutral score (0.5).

Architecture:
  VM1 (live bot) → POST /api/score → VM2 (ML server) → probability
"""

import logging
import os
import time
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ML server base URL. Override with ML_SERVER_URL (e.g. http://127.0.0.1:8091
# when running ml_training/run_trainer.py locally). Legacy default = VM4.
_ML_SERVER_DEFAULT_BASE = "http://10.0.2.4:8081"


def resolve_ml_server_url() -> str:
    """Scoring endpoint from the *current* environment (call at runtime)."""
    base = os.getenv("ML_SERVER_URL", _ML_SERVER_DEFAULT_BASE).rstrip("/")
    return f"{base}/api/score"


# Import-time snapshot kept for backwards compatibility. Prefer
# resolve_ml_server_url() — .env is usually loaded after this import.
ML_SERVER_BASE = os.getenv("ML_SERVER_URL", _ML_SERVER_DEFAULT_BASE).rstrip("/")
ML_SERVER_URL = f"{ML_SERVER_BASE}/api/score"
SCORE_TIMEOUT = 2.0  # seconds — scalp signals are time-sensitive
_CB_FAILURES_TO_OPEN = 3   # consecutive connection failures before the breaker opens
_CB_OPEN_SECS = 300.0      # how long to skip the ML host once the breaker is open


class MLScorer:
    """Scores scanner candidates via VM2 ML API. Fail-open design."""

    def __init__(self, url: Optional[str] = None, enabled: bool = True,
                 shadow_mode: bool = True):
        """
        Args:
            url: scoring endpoint. None → resolve from ML_SERVER_URL *now*
                 (not at import time: main.py imports this module before
                 config.loader calls load_dotenv, so a module-level default
                 would never see the .env value).
            enabled: Master switch
            shadow_mode: If True, log ML score but never veto trades
        """
        self._url = url or resolve_ml_server_url()
        self._enabled = enabled
        self._shadow_mode = shadow_mode
        self._last_error: Optional[str] = None
        self._scores_log: list = []  # rolling log of recent scores
        self._stats = {"calls": 0, "errors": 0, "avg_latency_ms": 0}

    def score_candidate(
        self,
        scanner_name: str,
        features: Dict[str, float],
        symbol: Optional[str] = None,
        side: Optional[str] = None,
    ) -> Dict:
        """Score a candidate synchronously. Returns score dict.

        Phase 4.5: `symbol` is now passed through to the server so it can
        route the request to a family-specific model (e.g. liquid_majors)
        before falling back to the per-scanner model. `side` is included
        for logging / audit only — it does not affect model selection.

        Always returns a result — never raises.
        """
        if not self._enabled:
            return {"probability": 0.5, "verdict": "DISABLED", "scanner": scanner_name}

        # Circuit breaker (2026-09-09): score_candidate() runs synchronous
        # `requests` calls inside the bot's asyncio loop. When the ML host is
        # unreachable every call blocked the loop for SCORE_TIMEOUT (plus a 3s
        # health probe), starving candle polling ("Stale data", "Trade monitor
        # DELAYED"). After 3 consecutive connection failures, skip the network
        # entirely for _CB_OPEN_SECS and return UNREACHABLE immediately.
        _cb_until = getattr(self, "_cb_open_until", 0.0)
        if time.time() < _cb_until:
            self._stats["errors"] += 1
            return {
                "probability": None,
                "verdict": "UNREACHABLE",
                "scanner": scanner_name,
                "bucket_action": "ABSTAIN",
                "circuit_open": True,
            }

        # Architect review #10: Model staleness kill switch
        # If the ML model on VM4 hasn't been retrained in >48h, degrade to
        # rules-only (return 0.5 probability = neutral). Prevents stale
        # models from confidently misfiring on changed market conditions.
        try:
            _stale_threshold_h = 48
            _last_health = getattr(self, '_last_health_check', {}) or {}
            _health_age = time.time() - float(_last_health.get("ts", 0) or 0)
            # Re-check health every 5 minutes
            if _health_age > 300:
                import requests as _rq
                try:
                    _hr = _rq.get(self._url.replace("/api/score", "/api/ml/health"), timeout=3)
                    if _hr.status_code == 200:
                        _hd = _hr.json()
                        _scanners = _hd.get("scanners", {})
                        _max_age_h = 0
                        for _sn, _sv in _scanners.items():
                            _max_age_h = max(_max_age_h, float(_sv.get("age_hours", 0) or 0))
                        self._last_health_check = {"ts": time.time(), "max_age_h": _max_age_h}
                except Exception:
                    pass
            _model_age_h = float((getattr(self, '_last_health_check', {}) or {}).get("max_age_h", 0) or 0)
            if _model_age_h > _stale_threshold_h:
                logger.warning(
                    "MODEL STALE: oldest model is %.1fh old (threshold=%dh) — returning neutral 0.5",
                    _model_age_h, _stale_threshold_h,
                )
                return {
                    "probability": 0.5, "verdict": "STALE_MODEL",
                    "scanner": scanner_name, "model_age_h": _model_age_h,
                }
        except Exception:
            pass

        import requests  # lazy import — not needed if disabled

        self._stats["calls"] += 1
        t0 = time.time()

        try:
            # Phase 4.5: send symbol + side so server can do family routing
            _payload = {
                "scanner": scanner_name,
                "features": features,
            }
            if symbol:
                _payload["symbol"] = symbol
            if side:
                _payload["side"] = side
            resp = requests.post(
                self._url,
                json=_payload,
                timeout=SCORE_TIMEOUT,
            )
            latency_ms = (time.time() - t0) * 1000
            self._cb_consecutive_failures = 0  # connection succeeded — reset breaker
            self._stats["avg_latency_ms"] = (
                self._stats["avg_latency_ms"] * 0.9 + latency_ms * 0.1
            )

            # Phase 4.2: 503 Service Unavailable = model missing OR schema drift
            # These are LOUD signals that something is broken. They must never
            # be treated as "neutral 0.5" predictions.
            if resp.status_code == 503:
                result = resp.json()
                result["latency_ms"] = round(latency_ms, 1)
                # probability is None (not 0.5) so callers can distinguish from real predictions
                verdict = result.get("verdict", "ABSTAIN_UNKNOWN")
                err = result.get("error", "unknown")
                # Track telemetry on missing/skew — surfaces in get_stats()
                self._stats.setdefault("abstain_counts", {})
                self._stats["abstain_counts"][verdict] = self._stats["abstain_counts"].get(verdict, 0) + 1
                # Log at WARNING level so ops sees it
                logger.warning(
                    "ML ABSTAIN %s: scanner=%s verdict=%s err=%s",
                    "503", scanner_name, verdict, err,
                )
                result["in_top_bucket"] = False
                result["bucket_action"] = "ABSTAIN"
                return result

            if resp.status_code == 200:
                result = resp.json()
                result["latency_ms"] = round(latency_ms, 1)
                self._last_error = None

                # Phase 4.2: check probability is not None (could be from older server)
                prob_raw = result.get("probability")
                if prob_raw is None:
                    # Server returned 200 but no probability — treat as abstain
                    result["in_top_bucket"] = False
                    result["bucket_action"] = "ABSTAIN"
                    logger.warning("ML SCORE 200 but probability=None for %s — treating as ABSTAIN", scanner_name)
                    return result

                prob = float(prob_raw)
                rank_bucket = result.get("rank_bucket", "Q50")
                result["in_top_bucket"] = rank_bucket in ("D90", "Q75")
                result["bucket_action"] = (
                    "TAKE" if rank_bucket in ("D90", "Q75")
                    else "CAUTION" if rank_bucket == "Q50"
                    else "SKIP"
                )

                # Phase 4.5: track which model scope actually scored this (family vs scanner)
                _scope = result.get("resolved_scope", "scanner")
                _family = result.get("resolved_family")
                self._stats.setdefault("scope_counts", {"family": 0, "scanner": 0})
                self._stats["scope_counts"][_scope] = self._stats["scope_counts"].get(_scope, 0) + 1
                if _scope == "family":
                    self._stats.setdefault("family_counts", {})
                    self._stats["family_counts"][_family or "?"] = (
                        self._stats["family_counts"].get(_family or "?", 0) + 1
                    )

                # Phase 4.2 + Architect review #7: concept drift detection
                match_pct = result.get("match_pct", 1.0)
                if match_pct < 0.99 and match_pct >= 0.80:
                    logger.info(
                        "ML SCORE %s: match_pct=%.1f%% (%d/%d features) — minor drift",
                        scanner_name, match_pct * 100,
                        result.get("features_matched", 0),
                        result.get("features_expected", 0),
                    )

                # Architect review #7: Track rolling drift rate
                # If >30% of recent scores had match_pct < 90%, the model is
                # experiencing concept drift and predictions are unreliable.
                try:
                    _drift_window = getattr(self, '_drift_window', [])
                    _drift_window.append(match_pct)
                    if len(_drift_window) > 50:
                        _drift_window = _drift_window[-50:]
                    self._drift_window = _drift_window
                    _drift_rate = sum(1 for m in _drift_window if m < 0.90) / len(_drift_window)
                    if _drift_rate > 0.30 and len(_drift_window) >= 20:
                        logger.warning(
                            "CONCEPT DRIFT DETECTED: %.0f%% of last %d scores had feature skew (match<90%%)",
                            _drift_rate * 100, len(_drift_window),
                        )
                        self._stats["concept_drift_detected"] = True
                        self._stats["concept_drift_rate"] = round(_drift_rate, 2)
                    else:
                        self._stats["concept_drift_detected"] = False
                        self._stats["concept_drift_rate"] = round(_drift_rate, 2)
                except Exception:
                    pass

                # Log for analysis
                self._scores_log.append({
                    "time": time.time(),
                    "scanner": scanner_name,
                    "symbol": symbol,
                    "probability": prob,
                    "verdict": result.get("verdict", "?"),
                    "bucket_action": result["bucket_action"],
                    "match_pct": match_pct,
                    "resolved_scope": _scope,        # Phase 4.5
                    "resolved_family": _family,      # Phase 4.5
                })
                # Keep last 100
                if len(self._scores_log) > 100:
                    self._scores_log = self._scores_log[-100:]

                return result
            else:
                self._stats["errors"] += 1
                self._last_error = f"HTTP {resp.status_code}"
                return {
                    "probability": None,  # Phase 4.2: None not 0.5
                    "verdict": "API_ERROR",
                    "scanner": scanner_name,
                    "error": f"HTTP {resp.status_code}",
                    "bucket_action": "ABSTAIN",
                }

        except Exception as e:
            self._stats["errors"] += 1
            self._last_error = str(e)
            _fails = getattr(self, "_cb_consecutive_failures", 0) + 1
            self._cb_consecutive_failures = _fails
            if _fails >= _CB_FAILURES_TO_OPEN:
                self._cb_open_until = time.time() + _CB_OPEN_SECS
                self._cb_consecutive_failures = 0
                logger.warning(
                    "ML scorer circuit OPEN for %.0fs after %d consecutive failures "
                    "(host %s unreachable): %s",
                    _CB_OPEN_SECS, _fails, self._url, e,
                )
            else:
                logger.warning("ML score error for %s: %s", scanner_name, e)
            return {
                "probability": None,  # Phase 4.2: None not 0.5
                "verdict": "UNREACHABLE",
                "scanner": scanner_name,
                "bucket_action": "ABSTAIN",
            }

    def should_take_trade(self, score_result: Dict, threshold: float = 0.40) -> bool:
        """Decide whether to take trade based on ML score.

        Phase 4.2: ABSTAIN verdicts (NO_MODEL, SKEW, UNREACHABLE, API_ERROR)
        no longer default to 0.5. Behavior depends on shadow_mode:
          - shadow_mode=True  → always return True (never veto)
          - shadow_mode=False → treat ABSTAIN as "cannot evaluate" — return True
                                 (fail-open: don't block just because ML is down)

        This is deliberate: a broken ML scorer should NOT stop trading entirely.
        Other hotfix gates (P0-P4) still protect. ML is advisory.
        """
        if self._shadow_mode:
            return True  # never veto in shadow mode
        prob = score_result.get("probability")
        if prob is None:
            # Phase 4.2: ABSTAIN — fail open (don't block)
            return True
        return float(prob) >= threshold

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
    htf_bias: float = 0.0,
    htf_trend_strength: float = 0.0,
    htf_15m: "pd.DataFrame | None" = None,
    htf_1h: "pd.DataFrame | None" = None,
    htf_4h: "pd.DataFrame | None" = None,
    btc_df: "pd.DataFrame | None" = None,   # Phase 5.0a
    orderbook: "dict | None" = None,         # Phase 5.0c
) -> Dict[str, float]:
    """Build the feature dict for ML scoring from live candle data.

    Phase 4.1b REFACTOR (2026-04-11):
    Delegates to ml_training.unified_features.build_live_row() — the SAME
    function used by candidate_trainer.build_dataset_with_veto_labels() during
    training. This guarantees zero training-serving skew.

    Before Phase 4.1b: this function had a 400-line inline reimplementation
    that drifted from training — produced 73 mkt_ features vs training's 204.
    The missing 131 features were silently zero-filled at serving, corrupting
    every ML prediction.

    Args:
        df: 5m OHLCV DataFrame with indicators (compute_indicators) already run
        idx: bar index (-1 for last bar)
        side: "long" or "short"
        symbol: e.g. "BTC/USDT"
        htf_bias, htf_trend_strength: LEGACY — no longer used (inferred from df)
        htf_15m: 15m HTF candles (optional but recommended)
        htf_1h: 1h HTF candles (Phase 4.1a — macro trend features)
        htf_4h: 4h HTF candles (Phase 4.1a — session/structure features)

    Returns:
        Dict[str, float] with 247 features (stable schema, matches training)
    """
    # Phase 4.1b: single-call to unified builder
    try:
        from ml_training.unified_features import build_live_row
        return build_live_row(
            df=df,
            idx=idx,
            side=side,
            symbol=symbol,
            htf_15m=htf_15m,
            htf_1h=htf_1h,
            htf_4h=htf_4h,
            btc_df=btc_df,      # Phase 5.0a
            orderbook=orderbook, # Phase 5.0c
        )
    except ImportError as e:
        logger.warning(
            "ml_training.unified_features not available (%s) — falling back to legacy inline builder",
            e,
        )
        # Fall through to legacy implementation below
    except Exception as e:
        logger.error("build_live_row failed: %s — falling back to legacy inline", e, exc_info=True)

    # ═══════════════════════════════════════════════════════════════
    # LEGACY FALLBACK — kept for VM1 compatibility if ml_training isn't present
    # Produces 73 mkt_ features (missing ~130 that trained model expects).
    # Not used in normal operation after Phase 4.1b.
    # ═══════════════════════════════════════════════════════════════
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
        features.update(_compute_mkt_features(df, idx, atr, avg_atr,
                                               htf_bias=htf_bias,
                                               htf_trend_strength=htf_trend_strength))
    except Exception as e:
        logger.warning("Failed to build mkt_ features: %s", e)

    return features


def _compute_mkt_features(df: pd.DataFrame, idx: int, atr: float, avg_atr: float,
                          htf_bias: float = 0.0, htf_trend_strength: float = 0.0) -> Dict[str, float]:
    """Compute all mkt_* features inline (mirrors build_features() from ml_training).

    Pure candle math — no external dependencies. Works on VM1 without ml_training.
    Includes: momentum, volatility, candle structure, trend, volume intelligence,
    market context, compression, time, state transitions, FVG, OB proxy, MTF alignment.
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

    # ── 12. VOLUME INTELLIGENCE (buy/sell imbalance, CVD proxy) ──
    # Buy/sell imbalance: close_position * normalized volume
    mkt["mkt_buy_sell_imbalance"] = (mkt["mkt_close_position"] - 0.5) * 2.0 * (vol_val / vs20 if vs20 > 0 else 1.0)
    # CVD proxy: cumulative (close_position - 0.5) * volume over last N bars
    if idx >= 10:
        cvd_raw = sum(
            ((_g(c, k) - _g(l, k)) / max(_g(h, k) - _g(l, k), 1e-8) - 0.5) * _g(v, k)
            for k in range(max(0, idx - 10), idx + 1)
        )
        cvd_norm = cvd_raw / (vs20 * 10) if vs20 > 0 else 0.0
        mkt["mkt_cvd_proxy_10"] = max(min(cvd_norm, 5.0), -5.0)
    else:
        mkt["mkt_cvd_proxy_10"] = 0.0
    # Volume spike ratio (already exists as mkt_volume_spike, add z-score variant)
    mkt["mkt_vol_spike_ratio_3"] = vol_val / (float(v.iloc[max(0, idx - 3):idx + 1].mean()) if idx >= 3 else vol_val + 1e-8)

    # ── 13. VWAP BANDS (normalized distance) ──
    vwap_val = _g(vwap)
    if vwap_val > 0 and idx >= 20:
        vwap_dev = float((c - vwap).iloc[max(0, idx - 20):idx + 1].std())
        if vwap_dev > 0:
            mkt["mkt_vwap_band_distance"] = (c_val - vwap_val) / vwap_dev
            mkt["mkt_vwap_upper_band"] = (vwap_val + 2 * vwap_dev - c_val) / atr_val
            mkt["mkt_vwap_lower_band"] = (c_val - vwap_val + 2 * vwap_dev) / atr_val
        else:
            mkt["mkt_vwap_band_distance"] = 0.0
            mkt["mkt_vwap_upper_band"] = 0.0
            mkt["mkt_vwap_lower_band"] = 0.0
    else:
        mkt["mkt_vwap_band_distance"] = 0.0
        mkt["mkt_vwap_upper_band"] = 0.0
        mkt["mkt_vwap_lower_band"] = 0.0

    # ── 14. ATR EXPANSION RATIO (10-bar lookback) ──
    if idx >= 10:
        atr_10ago = _g(atr_s, idx - 10)
        mkt["mkt_atr_expansion_10"] = atr_val / atr_10ago if atr_10ago > 0 else 1.0
    else:
        mkt["mkt_atr_expansion_10"] = 1.0
    # ATR regime: quiet / normal / expanding / chaotic
    atr_r = mkt.get("mkt_vol_regime", 1.0)
    body_avg_5 = float(body_ratio.iloc[max(0, idx - 5):idx + 1].mean()) if idx >= 5 else _g(body_ratio)
    mkt["mkt_atr_quiet"] = 1.0 if atr_r < 0.5 else 0.0
    mkt["mkt_atr_expanding"] = 1.0 if mkt.get("mkt_atr_expansion_10", 1.0) > 1.2 else 0.0
    mkt["mkt_atr_chaotic"] = 1.0 if atr_r > 1.5 and body_avg_5 < 0.4 else 0.0

    # ── 15. EMA SLOPE ACCELERATION (second derivative) ──
    if idx >= 6:
        slope_now = mkt["mkt_ema_slope_8"]
        slope_3ago = float(ema8.pct_change(3).iloc[idx - 3]) if idx >= 6 else slope_now
        mkt["mkt_ema_slope_accel"] = slope_now - slope_3ago
    else:
        mkt["mkt_ema_slope_accel"] = 0.0

    # ── 16. MTF ALIGNMENT (passed from caller or computed) ──
    mkt["mkt_htf_bias"] = htf_bias  # +1 bullish, -1 bearish, 0 neutral
    mkt["mkt_htf_trend_strength"] = htf_trend_strength
    # EMA alignment score: how many EMAs agree on direction
    ema8_v, ema21_v, ema50_v = _g(ema8), _g(ema21), _g(ema50)
    ema200 = df["ema_200"].astype(float) if "ema_200" in df.columns else c.ewm(span=200, adjust=False).mean()
    ema200_v = _g(ema200)
    bull_count_ema = sum([
        1 if c_val > ema8_v else -1,
        1 if ema8_v > ema21_v else -1,
        1 if ema21_v > ema50_v else -1,
        1 if ema50_v > ema200_v else -1,
    ])
    mkt["mkt_ema_alignment"] = bull_count_ema / 4.0  # -1.0 to +1.0
    # Distance from EMA 200 (normalized by ATR)
    mkt["mkt_dist_from_ema200"] = (c_val - ema200_v) / atr_val if ema200_v > 0 else 0.0

    # ── 17. FVG FEATURES (enhanced) ──
    # Distance to nearest unfilled FVG
    nearest_fvg_dist = 999.0
    fvg_alignment = 0.0
    for k in range(max(2, idx - 20), idx + 1):
        lk, hk = _g(l, k), _g(h, k)
        hk2, lk2 = _g(h, k - 2), _g(l, k - 2)
        if lk > hk2:  # bullish FVG
            fvg_mid = (lk + hk2) / 2
            dist = abs(c_val - fvg_mid) / atr_val
            if dist < nearest_fvg_dist:
                nearest_fvg_dist = dist
                fvg_alignment = 1.0 if mkt["mkt_trend_strength"] > 0 else -0.5
        if hk < lk2:  # bearish FVG
            fvg_mid = (hk + lk2) / 2
            dist = abs(c_val - fvg_mid) / atr_val
            if dist < nearest_fvg_dist:
                nearest_fvg_dist = dist
                fvg_alignment = 1.0 if mkt["mkt_trend_strength"] < 0 else -0.5
    mkt["mkt_fvg_distance"] = min(nearest_fvg_dist, 10.0)
    mkt["mkt_fvg_alignment_score"] = fvg_alignment
    mkt["mkt_fvg_size_atr"] = max(fvg_bull_size, fvg_bear_size)

    # ── 18. ORDER BLOCK PROXY ──
    # Last strong move origin: find last candle with body > 1.5 ATR
    ob_distance = 10.0  # default far
    impulse_strength = 0.0
    consolidation_size = 0.0
    for k in range(idx - 1, max(0, idx - 20) - 1, -1):
        body_k = abs(_g(c, k) - _g(o, k))
        if body_k > 1.5 * _g(atr_s, k):
            ob_distance = (idx - k) / 10.0
            impulse_strength = body_k / _g(atr_s, k)
            # Consolidation before impulse
            if k >= 5:
                cons_range = max(_g(h, j) for j in range(k - 5, k)) - min(_g(l, j) for j in range(k - 5, k))
                consolidation_size = cons_range / _g(atr_s, k)
            break
    mkt["mkt_ob_distance"] = ob_distance
    mkt["mkt_ob_impulse_strength"] = min(impulse_strength, 5.0)
    mkt["mkt_ob_consolidation_size"] = min(consolidation_size, 5.0)

    # ── 19. REGIME FEATURES (for ML regime awareness) ──
    # Regime encoded as continuous features (not one-hot — ML handles better)
    mkt["mkt_regime_trend_score"] = mkt["mkt_ema_alignment"]
    mkt["mkt_regime_vol_score"] = atr_r
    mkt["mkt_regime_range_score"] = mkt["mkt_range_position"]

    # ── 20. REGIME INTERACTION FEATURES ──
    # Let ML learn which features matter in which regime
    mkt["mkt_trend_x_return5"] = mkt["mkt_trend_strength"] * mkt["mkt_return_5"]
    mkt["mkt_trend_x_ema_slope"] = mkt["mkt_trend_strength"] * mkt["mkt_ema_slope_8"]
    mkt["mkt_vol_x_volume"] = mkt["mkt_atr_expansion"] * mkt["mkt_volume_zscore"]
    mkt["mkt_vwap_x_trend"] = mkt["mkt_dist_from_vwap"] * mkt["mkt_trend_strength"]
    mkt["mkt_range_x_regime_vol"] = mkt["mkt_range_position"] * atr_r

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
