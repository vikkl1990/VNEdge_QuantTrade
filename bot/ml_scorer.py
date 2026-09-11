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
_ML_SERVER_DEFAULT_BASE = "http://127.0.0.1:8091"  # local ML Lab (scripts/ml_lab.sh)


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
                        _ages = {}
                        _max_age_h = 0
                        for _sn, _sv in _scanners.items():
                            _a = float(_sv.get("age_hours", 0) or 0)
                            _ages[_sn] = _a
                            _max_age_h = max(_max_age_h, _a)
                        self._last_health_check = {"ts": time.time(), "max_age_h": _max_age_h, "ages": _ages}
                except Exception:
                    pass
            # Judge the model that will actually score THIS scanner. Using the
            # max across all scanners made one untrained scanner (bos_choch at
            # 48.9 h) return STALE_MODEL / 0.5 for every other scanner too.
            _hc = getattr(self, '_last_health_check', {}) or {}
            _model_age_h = float((_hc.get("ages") or {}).get(scanner_name, _hc.get("max_age_h", 0)) or 0)
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
    # One code path. unified_features.build_live_row is the same function the
    # trainer assembles rows with (candidate_trainer -> _compute_gate_block +
    # assemble_row). The 600-line inline fallback that used to follow this
    # produced 73 features against a 200+ feature model and was reachable on
    # ANY exception; a failure now propagates and the caller records ERROR.
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
