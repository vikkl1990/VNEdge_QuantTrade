"""Phase 4.1b — UNIFIED FEATURE BUILDER.

SINGLE SOURCE OF TRUTH for ML feature computation. Used by BOTH:
  1. ml_training.candidate_trainer — offline training label + feature extraction
  2. bot.ml_scorer — live scoring on VM1 (live bot)

Before Phase 4.1b, these two paths had DUPLICATE implementations that drifted:
  - ml_scorer._compute_mkt_features()  — 400 lines of inline feature math
  - candidate_trainer + feature_builder — different 900 lines of feature math

Result: training used 204 features named `mkt_return_1`, `mkt_h1_trend_bias`, etc.
Live sent 73 features named `mkt_return_1`, `mkt_atr_ratio`, etc. — a SUBSET.
The missing features were silently zero-filled by the dashboard → every ML
prediction was dominated by garbage inputs.

This module fixes that by exposing a single `build_live_row()` function that
produces the EXACT same feature dict that training uses. Same inputs → same
outputs. Zero drift possible.

Design:
  - Pure pandas/numpy, NO sklearn (VM1-safe)
  - `build_live_row(df, idx, side, symbol, htf_15m=None, htf_1h=None, htf_4h=None)`
  - Returns Dict[str, float] with 170+ features (matches training schema)
  - Uses `ml_training.feature_builder.build_features()` for the heavy lifting
  - Adds gate/veto features + rule_* gates on top
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ml_training.feature_builder import build_features, compute_indicators

logger = logging.getLogger("bot.unified_features")


REGIME_CATEGORIES = ("trending_up", "trending_down", "ranging", "volatile", "quiet", "sideways")
SESSION_CATEGORIES = ("asia_late", "asia_early", "europe", "us")


def _detect_regime(row: Any, df: pd.DataFrame, idx: int) -> str:
    """Detect regime from row/df context — matches live scalp_strategy logic."""
    try:
        ema_21 = float(row.get("ema_21", 0))
        ema_50 = float(row.get("ema_50", 0))
        ema_200 = float(row.get("ema_200", 0))
        c = float(row.get("close", 0))
        atr = float(row.get("atr_14", 0))
        bb_width = float(row.get("bb_width", 0))
        avg_atr = df["atr_14"].iloc[max(0, idx - 100):idx].mean() if idx > 100 else atr

        if avg_atr > 0 and atr / avg_atr < 0.7:
            return "quiet"
        if ema_21 > ema_50 > ema_200 and c > ema_21:
            return "trending_up"
        if ema_21 < ema_50 < ema_200 and c < ema_21:
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


def _detect_session(dt: Any) -> str:
    """Detect trading session from UTC hour."""
    try:
        if hasattr(dt, "hour"):
            h = dt.hour
            if 0 <= h < 6:
                return "asia_late"
            elif 6 <= h < 12:
                return "asia_early"
            elif 12 <= h < 18:
                return "europe"
            else:
                return "us"
    except Exception:
        pass
    return "unknown"


def _compute_gate_block(df: pd.DataFrame, idx: int, side: str, symbol: str) -> Dict[str, float]:
    """Compute gate/veto/rule features for a single bar.

    These mirror the live veto checks in scalp_strategy.py and are kept in a
    separate function so candidate_trainer can also use them without recomputing.
    """
    row = df.iloc[idx]
    c = float(row["close"])
    h = float(row["high"])
    l = float(row["low"])
    o = float(row["open"])
    atr = float(row.get("atr_14", 0))
    if atr <= 0:
        atr = 1e-8

    features: Dict[str, float] = {}

    # Regime one-hot
    regime = _detect_regime(row, df, idx)
    for cat in REGIME_CATEGORIES:
        features[f"regime_{cat}"] = 1.0 if regime == cat else 0.0

    # Regime stability
    if idx >= 70:
        same = 0
        for k in range(max(50, idx - 20), idx):
            try:
                if _detect_regime(df.iloc[k], df, k) == regime:
                    same += 1
            except Exception:
                pass
        features["regime_stability"] = same / 20.0
    else:
        features["regime_stability"] = 0.5

    # HTF alignment (basic — uses close vs ema_50 as proxy)
    ema_50 = float(row.get("ema_50", 0))
    htf_bullish = c > ema_50 if ema_50 > 0 else True
    features["htf_alignment"] = (1.0 if htf_bullish else -1.0) * (1 if side == "long" else -1)

    # EMA slopes
    ema8 = float(row.get("ema_8", 0))
    ema21 = float(row.get("ema_21", 0))
    if idx >= 3 and ema8 > 0:
        ema8_prev = float(df.iloc[idx - 3].get("ema_8", ema8))
        features["ema8_slope"] = (ema8 - ema8_prev) / atr
    else:
        features["ema8_slope"] = 0.0
    if idx >= 5 and ema21 > 0:
        ema21_prev = float(df.iloc[idx - 5].get("ema_21", ema21))
        features["ema21_slope"] = (ema21 - ema21_prev) / atr
    else:
        features["ema21_slope"] = 0.0

    features["trend_strength"] = (ema8 - ema21) / atr if ema21 > 0 else 0.0
    features["trend_strength_long"] = (ema21 - ema_50) / atr if ema_50 > 0 else 0.0

    # VWAP distance
    vwap = float(row.get("vwap", 0))
    features["vwap_distance"] = (c - vwap) / atr if vwap > 0 else 0.0

    # Volume
    vol_sma = float(row.get("vol_sma_20", 0))
    vol_std = float(row.get("vol_std_20", 0))
    volume = float(row.get("volume", 0))
    features["volume_zscore"] = (volume - vol_sma) / vol_std if vol_std > 0 else 0.0
    features["rel_vol"] = float(row.get("rel_vol", 1.0))

    # Session one-hot
    session = _detect_session(df.index[idx])
    for cat in SESSION_CATEGORIES:
        features[f"session_{cat}"] = 1.0 if session == cat else 0.0

    # Volatility
    try:
        avg_atr = df["atr_14"].iloc[max(0, idx - 100):idx].mean()
    except Exception:
        avg_atr = atr
    features["atr_ratio"] = atr / avg_atr if avg_atr > 0 else 1.0
    atr7 = float(row.get("atr_7", atr))
    features["atr_expansion"] = atr7 / atr if atr > 0 else 1.0

    # Distance from swing
    try:
        recent = df.iloc[max(0, idx - 20):idx]
        features["dist_from_swing_high"] = (c - float(recent["high"].max())) / atr
        features["dist_from_swing_low"] = (c - float(recent["low"].min())) / atr
    except Exception:
        features["dist_from_swing_high"] = 0.0
        features["dist_from_swing_low"] = 0.0

    # Candle structure
    body = abs(c - o)
    rng = h - l
    features["impulse_body_atr"] = body / atr
    features["dist_from_ema8"] = abs(c - ema8) / atr if ema8 > 0 else 0.0
    features["body_ratio"] = body / rng if rng > 0 else 0.0
    features["range_vs_atr"] = rng / atr
    features["upper_wick_ratio"] = (h - max(c, o)) / rng if rng > 0 else 0.0
    features["lower_wick_ratio"] = (min(c, o) - l) / rng if rng > 0 else 0.0

    # Regime-side alignment
    if regime == "trending_up":
        features["regime_side_alignment"] = 1.0 if side == "long" else -1.0
    elif regime == "trending_down":
        features["regime_side_alignment"] = 1.0 if side == "short" else -1.0
    else:
        features["regime_side_alignment"] = 0.0

    # RSI / BB
    rsi = float(row.get("rsi_14", 50))
    features["rsi_zone"] = (rsi - 50) / 50
    bb_upper = float(row.get("bb_upper", 0))
    bb_lower = float(row.get("bb_lower", 0))
    if bb_upper > bb_lower > 0:
        features["bb_position"] = (c - bb_lower) / (bb_upper - bb_lower)
    else:
        features["bb_position"] = 0.5

    # Side encoding
    features["side_long"] = 1.0 if side == "long" else 0.0

    # Rule gates (each as continuous binary)
    features["rule_htf_pass"] = 1.0 if features.get("htf_alignment", 0) >= 0 else 0.0
    features["rule_session_pass"] = 0.0 if features.get("session_asia_late", 0) > 0.5 else 1.0
    features["rule_vol_pass"] = 1.0 if features.get("atr_ratio", 1.0) >= 0.7 else 0.0
    features["rule_volume_pass"] = 1.0 if features.get("rel_vol", 1.0) >= 0.8 else 0.0
    features["rule_candle_pass"] = 1.0 if features.get("body_ratio", 0.5) >= 0.3 else 0.0
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

    # ══════════════════════════════════════════════════════════════════
    # PHASE 5.0b — HOTFIX DISTILLATION FEATURES
    # ══════════════════════════════════════════════════════════════════
    # The live bot's hotfix stack (P0, P0.8, P3.6, P3.7, P3.11, P3.21,
    # P3.22, P4) encodes expensive hard-won pattern knowledge. ML currently
    # rediscovers these patterns from raw candle features — but loses a lot
    # of signal because the HOTFIXES are COMPOUND rules (e.g. "long + chop
    # regime + counter-HTF + low conf"). Individual features don't capture
    # the interaction directly.
    #
    # Phase 5.0b distills each hotfix into a continuous RISK SCORE that
    # encodes "how close was this signal to tripping this hotfix". ML can
    # then learn to heavily discount setups that look like known losing
    # patterns, without depending on ml_probability (which would be circular).
    #
    # All features use only inputs already computed above. All are
    # side-aware. All are computable at training time from candle features.
    # ══════════════════════════════════════════════════════════════════
    _is_long = 1.0 if side == "long" else 0.0
    _is_short = 1.0 - _is_long
    _htf_align = features.get("htf_alignment", 0.0)
    _trend_str = features.get("trend_strength", 0.0)
    _regime_chop_score = (
        features.get("regime_sideways", 0.0)
        + features.get("regime_ranging", 0.0)
        + features.get("regime_volatile", 0.0)
    )
    _regime_trend_score = (
        features.get("regime_trending_up", 0.0)
        + features.get("regime_trending_down", 0.0)
    )
    _impulse = features.get("impulse_body_atr", 0.0)
    _dist_ema8 = features.get("dist_from_ema8", 0.0)
    _body = features.get("body_ratio", 0.5)
    _upper_wick = features.get("upper_wick_ratio", 0.0)
    _lower_wick = features.get("lower_wick_ratio", 0.0)
    _atr_ratio = features.get("atr_ratio", 1.0)
    _vwap_dist = features.get("vwap_distance", 0.0)  # long positive, short pays

    # P3.11 — chop long trap: long side + chop regime + HTF against
    features["hotfix_chop_long_trap_risk"] = (
        _is_long * _regime_chop_score * max(0.0, -_htf_align)
    )
    # Symmetric for shorts
    features["hotfix_chop_short_trap_risk"] = (
        _is_short * _regime_chop_score * max(0.0, _htf_align)
    )
    # P0/P0.8 — counter-HTF momentum: magnitude of HTF opposition (already side-signed)
    features["hotfix_counter_htf_risk"] = max(0.0, -_htf_align)

    # P3.22 — weak combo: no trend, no HTF, chop regime
    _no_trend = 1.0 - min(1.0, abs(_trend_str) / 2.0)  # 0 when strong trend, 1 when flat
    _no_htf = 1.0 - min(1.0, abs(_htf_align))          # 0 when clear HTF, 1 when neutral
    features["hotfix_weak_combo_risk"] = _no_trend * _no_htf * _regime_chop_score

    # Exhaustion wick — long chasing a spike with upper wick, short chasing dump with lower wick
    features["hotfix_exhaustion_long_risk"] = (
        _is_long * _upper_wick * min(_impulse, 2.0) / 2.0
    )
    features["hotfix_exhaustion_short_risk"] = (
        _is_short * _lower_wick * min(_impulse, 2.0) / 2.0
    )

    # P3.21 — overstretched: far from EMA8 + big impulse = chasing late
    _stretch = min(_dist_ema8, 3.0) / 3.0
    _chase = min(_impulse, 3.0) / 3.0
    features["hotfix_overstretched_risk"] = _stretch * _chase

    # VWAP conflict: long below VWAP (negative vwap_distance) or short above
    _vwap_long_risk = _is_long * max(0.0, -_vwap_dist) / 3.0  # clamp by 3×ATR
    _vwap_short_risk = _is_short * max(0.0, _vwap_dist) / 3.0
    features["hotfix_vwap_conflict_risk"] = min(1.0, _vwap_long_risk + _vwap_short_risk)

    # P4 — fee drag risk: low volatility means SL distance too tight to cover fees
    # When atr_ratio < 0.7, fee_drag_r approaches 1.0. When > 1.0, fee risk is low.
    features["hotfix_fee_drag_risk"] = max(0.0, (0.7 - min(_atr_ratio, 0.7)) / 0.7)

    # P3.7 — sideways SB long (conf proxy via body strength)
    features["hotfix_sideways_weak_body_risk"] = (
        _is_long * features.get("regime_sideways", 0.0) * (1.0 - _body)
    )

    # Compound scores — total and max across all hotfixes
    _all_hotfixes = [
        features["hotfix_chop_long_trap_risk"],
        features["hotfix_chop_short_trap_risk"],
        features["hotfix_counter_htf_risk"],
        features["hotfix_weak_combo_risk"],
        features["hotfix_exhaustion_long_risk"],
        features["hotfix_exhaustion_short_risk"],
        features["hotfix_overstretched_risk"],
        features["hotfix_vwap_conflict_risk"],
        features["hotfix_fee_drag_risk"],
        features["hotfix_sideways_weak_body_risk"],
    ]
    features["hotfix_total_risk"] = float(sum(_all_hotfixes))
    features["hotfix_max_risk"] = float(max(_all_hotfixes)) if _all_hotfixes else 0.0
    # ══════════════════════════════════════════════════════════════════

    return features


def build_live_row(
    df: pd.DataFrame,
    idx: int,
    side: str,
    symbol: str,
    htf_15m: Optional[pd.DataFrame] = None,
    htf_1h: Optional[pd.DataFrame] = None,
    htf_4h: Optional[pd.DataFrame] = None,
    btc_df: Optional[pd.DataFrame] = None,
    skip_indicators: bool = False,
    orderbook: Optional[dict] = None,
) -> Dict[str, float]:
    """Build the complete ML feature dict for a single bar.

    This is the UNIFIED feature builder used by BOTH training and live scoring.
    Guarantees identical feature names and values on both sides.

    Args:
        df: LTF (5m) OHLCV DataFrame with indicators already computed
        idx: bar index (negative indexing allowed, -1 = last bar)
        side: "long" or "short"
        symbol: e.g. "BTC/USDT"
        htf_15m: 15m HTF DataFrame (optional, adds 4 features)
        htf_1h: 1h HTF DataFrame (optional, adds ~14 features)
        htf_4h: 4h HTF DataFrame (optional, adds ~14 features)
        btc_df: BTC/USDT 5m DataFrame (optional, Phase 5.0a — adds ~14 cross-asset features)
        skip_indicators: if True, assume df already has indicator columns
        orderbook: L2 orderbook snapshot dict (optional, Phase 5.0c — adds 12 microstructure features)

    Returns:
        Dict[str, float] with 170+ features (170 base + 34 HTF + 14 BTC + 12 OB when all provided)
        All feature names match what the trained model expects.
    """
    if idx < 0:
        idx = len(df) + idx
    if idx < 0 or idx >= len(df):
        raise ValueError(f"idx {idx} out of range for df of length {len(df)}")

    # Ensure indicators are computed
    if not skip_indicators:
        if "ema_21" not in df.columns or "atr_14" not in df.columns:
            df = compute_indicators(df.copy())

    # Build gate features (rule/regime/session/HTF alignment)
    gate_feats = _compute_gate_block(df, idx, side, symbol)

    # Build market features via the unified feature_builder
    # This is the EXACT same function candidate_trainer uses
    try:
        market_features_df = build_features(
            df,
            htf_df=htf_15m,
            htf_1h_df=htf_1h,
            htf_4h_df=htf_4h,
            btc_df=btc_df,       # Phase 5.0a
            symbol=symbol,       # Phase 5.0a
            orderbook=orderbook, # Phase 5.0c
        )
    except Exception as e:
        logger.error("build_features failed in unified_features: %s", e, exc_info=True)
        # Fallback: return just gate features
        return gate_feats

    # Extract the row at idx and prefix with mkt_
    try:
        mkt_row = market_features_df.iloc[idx]
    except IndexError:
        logger.warning("mkt_row index %d out of range (len=%d)", idx, len(market_features_df))
        return gate_feats

    return assemble_row(gate_feats, mkt_row)


def assemble_row(gate_feats: Dict[str, float], mkt_row: "pd.Series") -> Dict[str, float]:
    """Merge gate features with an `mkt_`-prefixed market row.

    The ONE place the training/serving row is assembled. candidate_trainer
    calls this with a row sliced from its pre-computed build_features()
    frame; build_live_row calls it with the live frame's last row. Any
    change to prefixing or NaN handling therefore reaches both sides.
    """
    mkt_dict: Dict[str, float] = {}
    for col in mkt_row.index:
        try:
            val = float(mkt_row[col])
            if not (np.isnan(val) or np.isinf(val)):
                mkt_dict[f"mkt_{col}"] = val
            else:
                mkt_dict[f"mkt_{col}"] = 0.0
        except (TypeError, ValueError):
            continue
    return {**gate_feats, **mkt_dict}


def get_expected_feature_names(
    with_htf_15m: bool = True,
    with_htf_1h: bool = True,
    with_htf_4h: bool = True,
) -> list:
    """Return the canonical list of feature names the unified builder produces.

    Useful for:
      - Model schema validation at load time
      - Feature alignment in ml_scorer after model load
      - Diagnosing training-serving skew
    """
    # Build with a tiny synthetic DF to extract the exact column order
    import numpy as np
    from datetime import datetime, timedelta
    rows = []
    for i in range(300):
        rows.append({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000})
    idx_range = [datetime(2026, 1, 1) + timedelta(minutes=5 * i) for i in range(300)]
    df_synthetic = pd.DataFrame(rows, index=pd.DatetimeIndex(idx_range))

    # Optional HTFs
    h15 = None
    h1 = None
    h4 = None
    if with_htf_15m:
        h15 = df_synthetic.iloc[::3].copy()
    if with_htf_1h:
        h1 = df_synthetic.iloc[::12].copy()
    if with_htf_4h:
        h4 = df_synthetic.iloc[::48].copy()

    features = build_live_row(
        df_synthetic, idx=250, side="long", symbol="BTC/USDT",
        htf_15m=h15, htf_1h=h1, htf_4h=h4,
    )
    return sorted(features.keys())
