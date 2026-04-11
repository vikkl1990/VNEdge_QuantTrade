"""
SignalLearner — AI-driven adaptive learning system for signal quality improvement.

Learns from closed signal outcomes to:
1. Adjust confidence scores per setup type based on win/loss history
2. Track which market conditions (RSI, volume, trend, time) correlate with wins
3. Detect regime-setup compatibility (e.g., "trend_continuation works in trending, not sideways")
4. Apply smart filters: block historically poor setup+condition combos
5. Provide real-time confidence multipliers to the strategy layer

Uses lightweight Bayesian-style learning — no heavy ML libraries required.
All learned weights persist to disk and survive restarts.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# numpy removed — not used in this module

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
_LEARNER_FILE = _STORAGE_DIR / "signal_learner.json"

# Minimum signals before learner starts adjusting confidence
MIN_SAMPLES = 5
# How fast to adapt (0-1, higher = faster adaptation, more noise)
LEARNING_RATE = 0.15
# Decay factor for older signals (exponential moving average style)
DECAY_FACTOR = 0.95


class SignalLearner:
    """Adaptive learning system that improves signal quality from trade outcomes."""

    def __init__(self) -> None:
        _STORAGE_DIR.mkdir(parents=True, exist_ok=True)

        # ── Per-setup performance tracking ──
        # {setup_type: {wins, losses, total_pnl, avg_pnl, win_rate, confidence_mult, ...}}
        self._setup_stats: Dict[str, Dict[str, Any]] = {}

        # ── Per-condition performance (binned features) ──
        # {feature_bin: {wins, losses, avg_pnl}}
        # e.g., "rsi_zone:oversold", "volume:high", "hour:09", "regime:trending_up"
        self._condition_stats: Dict[str, Dict[str, Any]] = {}

        # ── Setup + Condition combos ──
        # {"setup:condition": {wins, losses, avg_pnl, score}}
        self._combo_stats: Dict[str, Dict[str, Any]] = {}

        # ── Blocked combos (historically terrible) ──
        self._blocked_combos: set = set()

        # ── Global stats ──
        self._total_signals_evaluated: int = 0
        self._total_adjustments_made: int = 0
        self._last_retrain: str = ""

        # ── Loss streak tracking (set by orchestrator) ──
        self._current_streak: int = 0

        self._load()

    # ------------------------------------------------------------------
    # Public API: Called by orchestrator/strategy
    # ------------------------------------------------------------------

    def set_current_streak(self, streak: int) -> None:
        """Set the current win/loss streak (called by orchestrator before adjust_confidence)."""
        self._current_streak = streak

    def adjust_confidence(self, signal_dict: Dict[str, Any]) -> Tuple[int, str]:
        """Apply learned adjustments to a signal's confidence score.

        Returns (adjusted_confidence, adjustment_reason).
        """
        original_conf = int(signal_dict.get("confidence", 0))
        meta = signal_dict.get("metadata", {})
        setup = meta.get("setup_type", "unknown")
        symbol = signal_dict.get("symbol", "")
        side = signal_dict.get("side", "long")

        # Extract features for condition matching
        features = self._extract_features(signal_dict)

        adjustments = []
        total_mult = 1.0

        # 1. Setup-level adjustment (with hard floors for poor setups)
        setup_data = self._setup_stats.get(setup)
        if setup_data and setup_data.get("total", 0) >= MIN_SAMPLES:
            mult = setup_data.get("confidence_mult", 1.0)
            wr = setup_data.get("win_rate", 50)
            total_trades = setup_data.get("total", 0)

            # Hard penalty: if raw WR < 40% with 8+ trades, force harsh multiplier
            # This overrides the slow EMA-based mult which lags reality
            if total_trades >= 8 and wr < 40:
                mult = max(mult, 0.65) if wr >= 30 else 0.55  # 35-45% penalty
                adjustments.append(f"{setup} WEAK ({wr:.0f}% WR over {total_trades})")
            elif abs(mult - 1.0) > 0.01:
                direction = "boost" if mult > 1.0 else "penalize"
                adjustments.append(f"{setup} {direction} ({wr:.0f}% WR)")

            total_mult *= mult

        # 2. Condition-level adjustments (average of relevant conditions)
        condition_mults = []
        for feat_key, feat_val in features.items():
            bin_key = f"{feat_key}:{feat_val}"
            cond_data = self._condition_stats.get(bin_key)
            if cond_data and cond_data.get("total", 0) >= MIN_SAMPLES:
                cond_wr = cond_data.get("win_rate", 50)
                # Convert win rate to multiplier: 80% WR → 1.15x, 30% WR → 0.85x
                cond_mult = 0.7 + (cond_wr / 100.0) * 0.6  # range: 0.7 to 1.3
                condition_mults.append(cond_mult)

        if condition_mults:
            avg_cond_mult = sum(condition_mults) / len(condition_mults)
            total_mult *= avg_cond_mult
            if abs(avg_cond_mult - 1.0) > 0.02:
                direction = "favorable" if avg_cond_mult > 1.0 else "unfavorable"
                adjustments.append(f"conditions {direction}")

        # 2b. Off-hours confidence penalty (01:00-05:00 UTC = low liquidity)
        current_hour = datetime.now(timezone.utc).hour
        if 1 <= current_hour <= 5:
            total_mult *= 0.7
            adjustments.append("off_hours penalty (01-05 UTC)")

        # 2c. Consecutive loss streak penalty
        if self._current_streak <= -5:
            total_mult *= 0.7
            adjustments.append(f"loss streak penalty ({self._current_streak})")
        elif self._current_streak <= -3:
            total_mult *= 0.85
            adjustments.append(f"loss streak penalty ({self._current_streak})")

        # 3. Combo-level check (setup + specific condition)
        for feat_key, feat_val in features.items():
            combo_key = f"{setup}:{feat_key}={feat_val}"
            if combo_key in self._blocked_combos:
                total_mult *= 0.5  # Heavily penalize blocked combos
                adjustments.append(f"BLOCKED combo: {feat_key}={feat_val}")
                break

            combo_data = self._combo_stats.get(combo_key)
            if combo_data and combo_data.get("total", 0) >= 3:
                combo_wr = combo_data.get("win_rate", 50)
                if combo_wr >= 75:
                    total_mult *= 1.1
                elif combo_wr <= 25:
                    total_mult *= 0.8
                    adjustments.append(f"weak combo: {feat_key}={feat_val}")

        # 4. Symbol-specific adjustment
        sym_key = f"symbol:{symbol}"
        sym_data = self._condition_stats.get(sym_key)
        if sym_data and sym_data.get("total", 0) >= MIN_SAMPLES:
            sym_wr = sym_data.get("win_rate", 50)
            sym_mult = 0.8 + (sym_wr / 100.0) * 0.4  # range: 0.8 to 1.2
            total_mult *= sym_mult

        # ── P2 HOTFIX (2026-04-10): Regime-aware boost cap ──
        # The setup-level boost (e.g. "structure_bounce 71% WR") is a regime-agnostic
        # historical average. In counter-HTF conditions, that average does NOT apply —
        # it's dominated by aligned-HTF trades. Applying the boost to counter-HTF trades
        # artificially lifts losers past confidence gates.
        #
        # Fix: if HTF opposes the signal direction, cap total_mult at 1.0 (penalties
        # still apply; boosts are suppressed). Neutral HTF (bias=0) is unaffected.
        # Zero impact on: HTF-aligned trades, neutral-HTF trades, setups that were
        # already being penalized (mult < 1.0).
        try:
            _htf = meta.get("htf_bias")
            if _htf is not None:
                _htf_val = int(_htf) if isinstance(_htf, (int, float)) else int(str(_htf).strip() or 0)
                _side_str = str(side).lower()
                _htf_opposes = (
                    (_htf_val < 0 and _side_str == "long") or
                    (_htf_val > 0 and _side_str == "short")
                )
                if _htf_opposes and total_mult > 1.0:
                    _orig_mult = total_mult
                    total_mult = 1.0
                    adjustments.append(f"[P2_COUNTER_HTF_BOOST_CAPPED: {_orig_mult:.2f}→1.00]")
                    # Phase 3.2: count P2 effectiveness
                    try:
                        from bot import pipeline_metrics as _pm
                        _pm.record_hotfix_veto("p2_counter_htf_boost_cap", f"{symbol}_{_side_str}_htf{_htf_val}_mult{_orig_mult:.2f}")
                    except Exception:
                        pass
        except (ValueError, TypeError):
            pass

        # Apply total multiplier with ceiling cap
        # Cap at 90 to prevent AI from pumping signals to max leverage tier
        # Only the raw strategy signal should reach 90+ (extremely rare, high conviction)
        adjusted = int(round(original_conf * total_mult))
        max_conf = 90 if total_mult > 1.0 else 100  # AI boost capped at 90
        adjusted = max(10, min(max_conf, adjusted))

        self._total_signals_evaluated += 1

        reason = ""
        if adjustments:
            reason = "AI: " + ", ".join(adjustments)
            self._total_adjustments_made += 1

        return adjusted, reason

    def is_signal_blocked(self, signal_dict: Dict[str, Any]) -> Tuple[bool, str]:
        """Check if a signal should be completely blocked based on learned patterns.

        Returns (should_block, reason).
        """
        meta = signal_dict.get("metadata", {})
        setup = meta.get("setup_type", "unknown")

        # Block setups with terrible historical performance
        setup_data = self._setup_stats.get(setup)
        if setup_data and setup_data.get("total", 0) >= 6:
            wr = setup_data.get("win_rate", 50)
            avg_pnl = setup_data.get("avg_pnl", 0)
            # Block if WR < 40% with negative avg PnL (not profitable)
            if wr < 40 and avg_pnl < 0:
                return True, f"AI blocked: {setup} has {wr:.0f}% WR over {setup_data['total']} signals"

        # Block specific setup+side combos with proven negative edge
        side = signal_dict.get("side", "long")
        side_combo = f"{setup}:side={side}"
        if side_combo in self._blocked_combos:
            return True, f"AI blocked: {setup} {side} (blocked combo)"

        # Hard-coded side blocks from 100-trade review
        # ema_momentum SHORT: 33% WR, -$3.08 (LONG is 77% WR, +$26.47)
        if setup == "ema_momentum" and side == "short":
            return True, f"AI blocked: ema_momentum SHORT (33% WR, use rsi_divergence for shorts)"

        # Block specific combos
        features = self._extract_features(signal_dict)
        for feat_key, feat_val in features.items():
            combo_key = f"{setup}:{feat_key}={feat_val}"
            if combo_key in self._blocked_combos:
                return True, f"AI blocked combo: {setup} + {feat_key}={feat_val}"

        return False, ""

    def learn_from_outcome(self, closed_signal: Dict[str, Any]) -> None:
        """Learn from a closed signal outcome (called when signal hits TP/SL/expires)."""
        setup = closed_signal.get("setup_type", "unknown")
        pnl = float(closed_signal.get("pnl_pct", 0))
        is_win = pnl > 0
        symbol = closed_signal.get("symbol", "")
        side = closed_signal.get("side", "long")
        status = closed_signal.get("status", "")
        confidence = int(closed_signal.get("confidence", 0))

        # Extract features from the signal for condition learning
        features = self._extract_features_from_closed(closed_signal)

        # 1. Update setup stats
        self._update_setup_stats(setup, is_win, pnl)

        # 2. Update condition stats
        for feat_key, feat_val in features.items():
            self._update_condition_stats(f"{feat_key}:{feat_val}", is_win, pnl)

        # 3. Update combo stats
        for feat_key, feat_val in features.items():
            combo_key = f"{setup}:{feat_key}={feat_val}"
            self._update_combo_stats(combo_key, is_win, pnl)

        # 4. Update symbol stats
        self._update_condition_stats(f"symbol:{symbol}", is_win, pnl)

        # 5. Check for combos to block
        self._evaluate_blocks()

        # 6. Persist
        self._last_retrain = datetime.now(timezone.utc).isoformat()
        self._save()

        logger.info(
            "Learned: %s %s %s | PnL=%.3f%% | %s | Setup WR now: %.1f%%",
            symbol, setup, "WIN" if is_win else "LOSS", pnl, status,
            self._setup_stats.get(setup, {}).get("win_rate", 0),
        )

    def get_insights(self) -> Dict[str, Any]:
        """Return current learning insights for dashboard display."""
        # Best/worst setups
        setup_rankings = []
        for setup, data in self._setup_stats.items():
            if data.get("total", 0) >= 3:
                setup_rankings.append({
                    "setup": setup,
                    "win_rate": round(data.get("win_rate", 0), 1),
                    "avg_pnl": round(data.get("avg_pnl", 0), 3),
                    "total": data.get("total", 0),
                    "confidence_mult": round(data.get("confidence_mult", 1.0), 2),
                })
        setup_rankings.sort(key=lambda x: x["win_rate"], reverse=True)

        # Best/worst conditions
        condition_rankings = []
        for cond, data in self._condition_stats.items():
            if data.get("total", 0) >= 5 and not cond.startswith("symbol:"):
                condition_rankings.append({
                    "condition": cond,
                    "win_rate": round(data.get("win_rate", 0), 1),
                    "total": data.get("total", 0),
                })
        condition_rankings.sort(key=lambda x: x["win_rate"], reverse=True)

        return {
            "total_evaluated": self._total_signals_evaluated,
            "total_adjustments": self._total_adjustments_made,
            "last_retrain": self._last_retrain,
            "blocked_combos": list(self._blocked_combos),
            "setup_rankings": setup_rankings,
            "best_conditions": condition_rankings[:5],
            "worst_conditions": condition_rankings[-5:] if len(condition_rankings) > 5 else [],
            "learning_active": any(
                d.get("total", 0) >= MIN_SAMPLES for d in self._setup_stats.values()
            ),
        }

    # ------------------------------------------------------------------
    # Feature extraction
    # ------------------------------------------------------------------

    def _extract_features(self, signal_dict: Dict[str, Any]) -> Dict[str, str]:
        """Extract binned features from a signal for condition matching."""
        meta = signal_dict.get("metadata", {})
        features = {}

        # RSI zone
        rsi = meta.get("rsi_value") or meta.get("rsi")
        if rsi is not None:
            rsi = float(rsi)
            if rsi < 30:
                features["rsi_zone"] = "oversold"
            elif rsi < 45:
                features["rsi_zone"] = "low"
            elif rsi < 55:
                features["rsi_zone"] = "neutral"
            elif rsi < 70:
                features["rsi_zone"] = "high"
            else:
                features["rsi_zone"] = "overbought"

        # Volume level
        vol = meta.get("volume_ratio") or meta.get("relative_volume")
        if vol is not None:
            vol = float(vol)
            if vol < 0.5:
                features["volume"] = "very_low"
            elif vol < 1.0:
                features["volume"] = "low"
            elif vol < 2.0:
                features["volume"] = "normal"
            elif vol < 3.0:
                features["volume"] = "high"
            else:
                features["volume"] = "very_high"

        # HTF bias
        htf = meta.get("htf_bias")
        if htf is not None:
            features["htf_bias"] = str(htf)

        # Side
        features["side"] = signal_dict.get("side", "long")

        # Regime
        regime = signal_dict.get("regime") or meta.get("regime")
        if regime:
            features["regime"] = str(regime)

        # Grade
        grade = signal_dict.get("grade", "")
        if grade:
            features["grade"] = str(grade)

        # Time of day (UTC hour bucket)
        ts = signal_dict.get("timestamp", "")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts))
                hour = dt.hour
                if 0 <= hour < 6:
                    features["session"] = "asia_late"
                elif 6 <= hour < 12:
                    features["session"] = "europe"
                elif 12 <= hour < 18:
                    features["session"] = "us"
                else:
                    features["session"] = "asia_early"
            except (ValueError, TypeError):
                pass

        # Confidence bucket
        conf = int(signal_dict.get("confidence", 0))
        if conf < 60:
            features["conf_bucket"] = "low"
        elif conf < 75:
            features["conf_bucket"] = "medium"
        else:
            features["conf_bucket"] = "high"

        # Number of confirmations
        confs = meta.get("confirmations", [])
        if isinstance(confs, list):
            features["num_confirms"] = str(min(len(confs), 7))

        return features

    def _extract_features_from_closed(self, closed: Dict[str, Any]) -> Dict[str, str]:
        """Extract features from a closed signal (may have less metadata)."""
        features = {}

        features["side"] = closed.get("side", "long")

        # Confidence bucket
        conf = int(closed.get("confidence", 0))
        if conf < 60:
            features["conf_bucket"] = "low"
        elif conf < 75:
            features["conf_bucket"] = "medium"
        else:
            features["conf_bucket"] = "high"

        # Grade
        grade = closed.get("grade", "")
        if grade:
            features["grade"] = str(grade)

        # Time of day from entry_time
        ts = closed.get("entry_time", "")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts))
                hour = dt.hour
                if 0 <= hour < 6:
                    features["session"] = "asia_late"
                elif 6 <= hour < 12:
                    features["session"] = "europe"
                elif 12 <= hour < 18:
                    features["session"] = "us"
                else:
                    features["session"] = "asia_early"
            except (ValueError, TypeError):
                pass

        # Strategy type
        st = closed.get("strategy_type", "")
        if st:
            features["strategy"] = st

        return features

    # ------------------------------------------------------------------
    # Statistical update methods
    # ------------------------------------------------------------------

    def _update_setup_stats(self, setup: str, is_win: bool, pnl: float) -> None:
        """Update per-setup performance with exponential decay."""
        if setup not in self._setup_stats:
            self._setup_stats[setup] = {
                "wins": 0, "losses": 0, "total": 0,
                "total_pnl": 0.0, "avg_pnl": 0.0,
                "win_rate": 50.0, "confidence_mult": 1.0,
                "ema_win_rate": 50.0,  # exponential moving average win rate
            }

        s = self._setup_stats[setup]
        s["total"] += 1
        s["total_pnl"] += pnl

        if is_win:
            s["wins"] += 1
        else:
            s["losses"] += 1

        # Simple win rate
        s["win_rate"] = (s["wins"] / s["total"]) * 100

        # EMA win rate (more responsive to recent performance)
        current_wr = 100.0 if is_win else 0.0
        s["ema_win_rate"] = (
            DECAY_FACTOR * s["ema_win_rate"] + (1 - DECAY_FACTOR) * current_wr
        )

        # Average PnL
        s["avg_pnl"] = s["total_pnl"] / s["total"]

        # Confidence multiplier based on EMA win rate
        # 50% WR → 1.0x, 70% WR → 1.15x, 30% WR → 0.85x
        if s["total"] >= MIN_SAMPLES:
            wr = s["ema_win_rate"]
            s["confidence_mult"] = 0.7 + (wr / 100.0) * 0.6
            # Clamp between 0.6 and 1.4
            s["confidence_mult"] = max(0.6, min(1.4, s["confidence_mult"]))

    def _update_condition_stats(self, key: str, is_win: bool, pnl: float) -> None:
        """Update per-condition stats."""
        if key not in self._condition_stats:
            self._condition_stats[key] = {
                "wins": 0, "losses": 0, "total": 0,
                "total_pnl": 0.0, "win_rate": 50.0,
            }

        c = self._condition_stats[key]
        c["total"] += 1
        c["total_pnl"] += pnl
        if is_win:
            c["wins"] += 1
        else:
            c["losses"] += 1
        c["win_rate"] = (c["wins"] / c["total"]) * 100

    def _update_combo_stats(self, key: str, is_win: bool, pnl: float) -> None:
        """Update setup+condition combo stats."""
        if key not in self._combo_stats:
            self._combo_stats[key] = {
                "wins": 0, "losses": 0, "total": 0,
                "total_pnl": 0.0, "win_rate": 50.0,
            }

        c = self._combo_stats[key]
        c["total"] += 1
        c["total_pnl"] += pnl
        if is_win:
            c["wins"] += 1
        else:
            c["losses"] += 1
        c["win_rate"] = (c["wins"] / c["total"]) * 100

    def _evaluate_blocks(self) -> None:
        """Check combo stats and block consistently losing combos."""
        self._blocked_combos = set()
        for combo_key, data in self._combo_stats.items():
            if data.get("total", 0) >= 8:
                wr = data.get("win_rate", 50)
                avg_pnl = data.get("total_pnl", 0) / max(data["total"], 1)
                # Block if <20% WR with negative avg PnL over 8+ signals
                if wr < 30 and avg_pnl < -0.05:
                    self._blocked_combos.add(combo_key)
                    logger.info("AI BLOCKED combo: %s (WR=%.0f%%, avgPnL=%.3f%%)",
                                combo_key, wr, avg_pnl)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load learned state from disk."""
        try:
            if _LEARNER_FILE.exists():
                data = json.loads(_LEARNER_FILE.read_text(encoding="utf-8"))
                self._setup_stats = data.get("setup_stats", {})
                self._condition_stats = data.get("condition_stats", {})
                self._combo_stats = data.get("combo_stats", {})
                self._blocked_combos = set(data.get("blocked_combos", []))
                self._total_signals_evaluated = data.get("total_evaluated", 0)
                self._total_adjustments_made = data.get("total_adjustments", 0)
                self._last_retrain = data.get("last_retrain", "")
                logger.info(
                    "SignalLearner loaded: %d setups, %d conditions, %d combos, %d blocked",
                    len(self._setup_stats),
                    len(self._condition_stats),
                    len(self._combo_stats),
                    len(self._blocked_combos),
                )
        except Exception as exc:
            logger.warning("Failed to load learner state: %s", exc)

    def _save(self) -> None:
        """Persist learned state to disk."""
        try:
            data = {
                "setup_stats": self._setup_stats,
                "condition_stats": self._condition_stats,
                "combo_stats": self._combo_stats,
                "blocked_combos": list(self._blocked_combos),
                "total_evaluated": self._total_signals_evaluated,
                "total_adjustments": self._total_adjustments_made,
                "last_retrain": self._last_retrain,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            _LEARNER_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save learner state: %s", exc)
