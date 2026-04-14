"""
BotBrain — Central Nervous System for VN Edge.

The unified coordinator that reads from all agents (SignalLearner, TradeMonitor,
RegimeFilter, DecisionEngine, ScannerWeightManager) and injects decisions via
return values at two critical pipeline junctures:

  A. consult_pre_scan()  — before scanner activation (which scanners to run)
  B. consult_pre_qualify() — before signal qualification (dynamic thresholds)

Design principles:
  1. Read-many, write-few: observes all agents, only injects via return values
  2. Never mutates another agent's state directly
  3. Every integration point is try/except wrapped — if Brain crashes, pipeline continues
  4. All lookups are in-memory (<1ms) — no disk I/O in the hot path
  5. Saves happen after trade closes, not in the signal path
  6. Dry-run mode logs decisions without affecting behavior

Phased rollout:
  Phase 1-2: Passive (on_trade_closed, on_regime_change only)
  Phase 3:   Scanner gating (consult_pre_scan)
  Phase 4:   Dynamic thresholds (consult_pre_qualify)
  Phase 5:   Auto-act TradeMonitor recommendations
  Phase 6:   Session manager (daily/weekly reviews)
  Phase 7:   Adaptive optimizer (Bayesian parameter tuning)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from bot.brain_memory import BrainMemory
from bot.brain_session import SessionManager
from bot.brain_optimizer import AdaptiveOptimizer

logger = logging.getLogger("bot.brain")

# ── Return value dataclasses ──


@dataclass
class BrainDirective:
    """Returned by consult_pre_scan() — controls scanner activation.

    If allow_trading is False, the strategy skips this symbol entirely.
    scanner_overrides maps scanner names to actions:
      "block"  = don't run this scanner (even if regime routing allows it)
      "allow"  = run this scanner (even if regime routing would block it)
      "shadow" = run but don't emit signal (for data collection)
    """
    allow_trading: bool = True
    pause_reason: str = ""
    scanner_overrides: Dict[str, str] = field(default_factory=dict)
    confidence_floor: int = 55
    size_multiplier: float = 1.0
    sl_multiplier: float = 1.0
    regime_note: str = ""


@dataclass
class QualifyOverrides:
    """Returned by consult_pre_qualify() — adjusts real qualification gates.

    Any field set to None means "use the default" (no override).
    """
    ml_threshold_override: Optional[float] = None
    confidence_override: Optional[int] = None
    size_override: Optional[float] = None
    reason: str = ""


class BotBrain:
    """Central nervous system — reads all agents, coordinates decisions.

    Instantiated by the orchestrator at startup. References to sub-agents
    (signal_learner, trade_monitor, etc.) are set after construction.
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        brain_cfg = self._config.get("brain", {})

        # Core subsystems
        self._memory = BrainMemory()
        self._session = SessionManager(self._memory)
        self._optimizer = AdaptiveOptimizer(self._memory, self._config)

        # Dry-run mode: log decisions but return neutral directives
        self._dry_run = brain_cfg.get("dry_run", True)  # Default: dry_run ON for safety

        # Agent references (set by orchestrator after init)
        self._signal_learner = None
        self._trade_monitor = None
        self._weight_manager = None

        # Active state (ephemeral — not persisted)
        self._suppressed_scanners: Set[str] = set()
        self._suppressed_hours: Set[int] = set()
        self._trading_paused: bool = False
        self._pause_reason: str = ""
        self._pause_until: float = 0.0
        self._size_mult_override: float = 1.0
        self._size_mult_trades_remaining: int = 0
        self._side_penalty: Dict[str, int] = {}  # "long"/"short" → confidence penalty

        # Counters
        self._trades_processed: int = 0
        self._recommendations_acted: int = 0
        self._directives_issued: int = 0
        self._last_save: float = 0.0

        logger.info(
            "BotBrain initialized: dry_run=%s, memory=%d cells, %d observations",
            self._dry_run, len(self._memory._matrix), self._memory._total_observations,
        )

    # ══════════════════════════════════════════════════════════════
    # PHASE 1-2: PASSIVE OBSERVATION (zero risk)
    # ══════════════════════════════════════════════════════════════

    def on_trade_closed(self, closed_signal: dict, regime: str):
        """Called when a paper or real trade closes. Records to unified memory.

        This is the PRIMARY learning ingest point. Every closed trade flows
        through here to update the setup x regime x symbol x hour matrix.

        Integration: orchestrator.py lines 663, 904 (after signal_learner + trade_monitor)
        """
        try:
            # Extract dimensions
            meta = closed_signal.get("metadata", {}) or {}
            setup = (
                meta.get("scanner", "")
                or meta.get("setup_type", "")
                or closed_signal.get("scanner", "")
                or closed_signal.get("reason", "unknown")
            )
            symbol = closed_signal.get("symbol", "unknown")
            is_win = closed_signal.get("pnl_pct", 0) > 0 or closed_signal.get("win", False)
            pnl = float(meta.get("pnl_usd", 0) or closed_signal.get("pnl_usd", 0) or 0)
            r_mult = float(
                closed_signal.get("mfe_r", 0)
                or meta.get("exit_r", 0)
                or closed_signal.get("pnl_pct", 0) * 100  # fallback: pct → rough R
            )
            duration = float(closed_signal.get("duration_sec", 0) or meta.get("duration_sec", 0) or 0)
            hour = datetime.now(timezone.utc).hour

            if not regime:
                regime = "unknown"

            # Record into unified memory
            self._memory.record_outcome(
                setup=setup, regime=regime, symbol=symbol, hour=hour,
                is_win=is_win, pnl=pnl, r_mult=r_mult, duration_sec=duration,
            )

            # Record into session manager
            self._session.record_trade(
                setup=setup, symbol=symbol, regime=regime,
                is_win=is_win, pnl_usd=pnl, r_mult=r_mult,
                duration_sec=duration, hour=hour,
            )

            # Optimizer tracking
            self._optimizer.record_trade(r_mult)

            self._trades_processed += 1

            # Phase 5: auto-act TradeMonitor recommendations (if not dry_run)
            if not self._dry_run and self._trade_monitor:
                self._process_recommendations()

            # Periodic optimizer step (every 10 trades)
            if self._trades_processed % 10 == 0 and self._optimizer._enabled:
                # Compute rolling expectancy from memory
                global_cell = self._memory._matrix.get(f"{setup}:*:*:*")
                metric = global_cell.avg_r if global_cell else 0.0
                self._optimizer.step(metric)

            # Periodic save (every 5 trades or every 60s)
            now = time.time()
            if self._trades_processed % 5 == 0 or (now - self._last_save) > 60:
                self._memory.save()
                self._last_save = now

            # Daily rollover check
            summary = self._session.check_daily_rollover()
            if summary:
                self._memory.save()

        except Exception as e:
            logger.error("BotBrain on_trade_closed error: %s", e)

    def on_regime_change(self, symbol: str, new_regime: str, confidence: float):
        """Called when strategy detects a regime change for a symbol.

        Updates the regime history and transition matrix.
        Integration: scalp_strategy.py line 705-707 (after regime detection)
        """
        try:
            # Get previous regime from memory
            prev = self._memory.get_current_regime(symbol)
            prev_history = self._memory._regime_history.get(symbol)
            duration_bars = 1
            if prev_history and len(prev_history) > 0:
                duration_bars = prev_history[-1].duration_bars

            # Record the new regime snapshot
            self._memory.record_regime(symbol, new_regime, confidence, 1)

            # Record transition if there was a previous regime
            if prev and prev != new_regime:
                self._memory.record_regime_transition(symbol, prev, new_regime, duration_bars)
                self._session.record_regime_change(new_regime)
                logger.debug(
                    "BRAIN regime change: %s %s→%s (conf=%.2f, prev_duration=%d bars)",
                    symbol, prev, new_regime, confidence, duration_bars,
                )
        except Exception as e:
            logger.error("BotBrain on_regime_change error: %s", e)

    # ══════════════════════════════════════════════════════════════
    # PHASE 3: SCANNER GATING (first behavior change)
    # ══════════════════════════════════════════════════════════════

    def consult_pre_scan(
        self, symbol: str, regime: str, regime_confidence: float
    ) -> BrainDirective:
        """Called before REGIME_SCANNER_ROUTING — the meta-controller hook.

        Returns a BrainDirective that can:
        - Pause trading entirely for this symbol
        - Override scanner allow/block per regime-performance data
        - Raise confidence floor during bad hours
        - Adjust position size and SL multipliers

        Integration: scalp_strategy.py, before scanner routing (~line 1289)
        """
        directive = BrainDirective()

        try:
            now_hour = datetime.now(timezone.utc).hour

            # 1. Check trading pause
            if self._trading_paused:
                if time.time() < self._pause_until:
                    directive.allow_trading = False
                    directive.pause_reason = self._pause_reason
                    if not self._dry_run:
                        return directive
                    else:
                        directive.regime_note += f"[DRY] Would pause: {self._pause_reason}. "
                        directive.allow_trading = True  # dry_run: don't actually pause
                else:
                    self._trading_paused = False
                    self._pause_reason = ""

            # 2. Scanner gating based on setup x regime performance
            scanners_to_check = [
                "structure_bounce", "ema_momentum", "vwap_bounce",
                "rsi_divergence", "liquidity_sweep", "bos_choch",
                "cvd_divergence", "trend_continuation", "order_block_entry",
            ]
            for scanner in scanners_to_check:
                wr = self._memory.get_setup_regime_wr(scanner, regime)
                if wr is not None:
                    if wr < 30.0:
                        directive.scanner_overrides[scanner] = "block"
                        directive.regime_note += f"{scanner} blocked (WR={wr:.0f}% in {regime}). "
                    elif wr >= 75.0:
                        directive.scanner_overrides[scanner] = "allow"

            # 3. Explicitly suppressed scanners (from Phase 5 auto-act)
            for scanner in self._suppressed_scanners:
                if scanner not in directive.scanner_overrides:
                    directive.scanner_overrides[scanner] = "block"

            # 4. Bad hour detection
            bad_hours = self._memory.get_bad_hours()
            if now_hour in bad_hours or now_hour in self._suppressed_hours:
                directive.confidence_floor = max(directive.confidence_floor, 75)
                directive.regime_note += f"Bad hour ({now_hour}h) — floor raised to 75. "

            # 5. Size multiplier from Phase 5 auto-act
            if self._size_mult_trades_remaining > 0:
                directive.size_multiplier = self._size_mult_override
                self._size_mult_trades_remaining -= 1

            # 6. Side penalty
            # (applied downstream in consult_pre_qualify, just note it here)

            # 7. Regime transition warning
            avg_dur = self._memory.get_regime_avg_duration(regime)
            history = self._memory._regime_history.get(symbol)
            if history and len(history) > 0 and avg_dur > 0:
                current_dur = history[-1].duration_bars
                if current_dur > avg_dur * 1.5:
                    pred_regime, pred_prob = self._memory.predict_next_regime(symbol)
                    directive.regime_note += (
                        f"Regime {regime} held {current_dur} bars (avg={avg_dur:.0f}). "
                        f"Likely shift → {pred_regime} ({pred_prob:.0f}%). "
                    )

            self._directives_issued += 1

            # Dry-run: log but return neutral
            if self._dry_run:
                overrides = directive.scanner_overrides
                if overrides or directive.confidence_floor > 55:
                    logger.info(
                        "BRAIN DRY_RUN [%s/%s]: overrides=%s, floor=%d, note=%s",
                        symbol, regime, overrides, directive.confidence_floor,
                        directive.regime_note.strip(),
                    )
                # Reset overrides in dry_run — don't actually block anything
                directive.scanner_overrides = {}
                directive.confidence_floor = 55
                directive.size_multiplier = 1.0
                directive.sl_multiplier = 1.0

        except Exception as e:
            logger.error("BotBrain consult_pre_scan error: %s", e)

        return directive

    # ══════════════════════════════════════════════════════════════
    # PHASE 4: DYNAMIC QUALIFICATION THRESHOLDS
    # ══════════════════════════════════════════════════════════════

    def consult_pre_qualify(self, signal: dict) -> QualifyOverrides:
        """Called before _smart_qualify() and during _process_signal().

        Returns dynamic threshold overrides based on:
        - Setup x regime performance from memory
        - Optimizer's current parameter values
        - Side-specific penalties

        Integration:
          - real_manager.py line 479 (_smart_qualify)
          - orchestrator.py line 1596 (_process_signal)
        """
        overrides = QualifyOverrides()

        try:
            if self._dry_run:
                return overrides  # Phase 4 inactive in dry_run

            meta = signal.get("metadata", {}) or {}
            setup = (
                meta.get("scanner", "")
                or meta.get("setup_type", "")
                or signal.get("scanner", "")
                or ""
            )
            regime = meta.get("regime", "") or signal.get("regime", "")
            symbol = signal.get("symbol", "")
            side = signal.get("side", "long")

            # Lookup setup x regime performance
            cell = self._memory.get_performance(setup, regime, symbol)
            if cell:
                if cell.win_rate > 65.0 and cell.sample_count >= 10:
                    # Strong edge — lower ML threshold slightly
                    overrides.ml_threshold_override = max(0.50, 0.65 - 0.05)
                    overrides.reason = f"Strong edge: {setup} in {regime} WR={cell.win_rate:.0f}% (n={cell.sample_count})"
                elif cell.win_rate < 40.0 and cell.sample_count >= 8:
                    # Weak edge — raise ML threshold
                    overrides.ml_threshold_override = min(0.85, 0.65 + 0.10)
                    overrides.reason = f"Weak edge: {setup} in {regime} WR={cell.win_rate:.0f}% (n={cell.sample_count})"

            # Side penalty from Phase 5 auto-act
            side_pen = self._side_penalty.get(side, 0)
            if side_pen > 0:
                current_conf = signal.get("confidence", 50)
                overrides.confidence_override = current_conf + side_pen
                overrides.reason += f" | Side penalty: {side} +{side_pen}"

            # Optimizer overrides
            opt_ml = self._optimizer.get_current("real_ml_threshold_min")
            if opt_ml is not None and overrides.ml_threshold_override is None:
                overrides.ml_threshold_override = opt_ml
                overrides.reason += f" | Optimizer ML={opt_ml:.3f}"

        except Exception as e:
            logger.error("BotBrain consult_pre_qualify error: %s", e)

        return overrides

    # ══════════════════════════════════════════════════════════════
    # PHASE 5: AUTO-ACT TRADE MONITOR RECOMMENDATIONS
    # ══════════════════════════════════════════════════════════════

    def _process_recommendations(self):
        """Read and auto-act on TradeMonitor recommendations.

        Called after every trade close (in on_trade_closed).
        Only active when dry_run=False.
        """
        if not self._trade_monitor:
            return

        try:
            recs = getattr(self._trade_monitor, "_recommendations", [])
            if not recs:
                return

            for rec in recs:
                action = rec.get("action", "") if isinstance(rec, dict) else str(rec)
                severity = rec.get("severity", "LOW") if isinstance(rec, dict) else "LOW"

                if "pause" in action.lower() and severity in ("HIGH", "CRITICAL"):
                    self._trading_paused = True
                    self._pause_reason = f"TradeMonitor: {action}"
                    self._pause_until = time.time() + 600  # 10 min
                    self._recommendations_acted += 1
                    logger.warning("BRAIN AUTO-ACT: pause trading 10m — %s", action)

                elif "reduce_size" in action.lower():
                    self._size_mult_override = 0.7
                    self._size_mult_trades_remaining = 5
                    self._recommendations_acted += 1
                    logger.warning("BRAIN AUTO-ACT: reduce size to 0.7x for 5 trades")

                elif "avoid_hour" in action.lower():
                    # Extract hour from recommendation
                    try:
                        hour = int("".join(c for c in action if c.isdigit())[:2])
                        self._suppressed_hours.add(hour)
                        self._recommendations_acted += 1
                        logger.warning("BRAIN AUTO-ACT: suppress hour %d", hour)
                    except (ValueError, IndexError):
                        pass

                elif "disable" in action.lower() or "kill" in action.lower():
                    # Extract setup name from recommendation
                    for scanner in [
                        "structure_bounce", "ema_momentum", "vwap_bounce",
                        "rsi_divergence", "liquidity_sweep", "bos_choch",
                        "cvd_divergence", "trend_continuation", "order_block_entry",
                    ]:
                        if scanner in action.lower():
                            self._suppressed_scanners.add(scanner)
                            self._recommendations_acted += 1
                            logger.warning("BRAIN AUTO-ACT: suppress scanner %s for 1h", scanner)
                            break

                elif "reduce_shorts" in action.lower():
                    self._side_penalty["short"] = 15
                    self._recommendations_acted += 1
                    logger.warning("BRAIN AUTO-ACT: short confidence penalty +15")

                elif "reduce_longs" in action.lower():
                    self._side_penalty["long"] = 15
                    self._recommendations_acted += 1
                    logger.warning("BRAIN AUTO-ACT: long confidence penalty +15")

        except Exception as e:
            logger.error("BotBrain _process_recommendations error: %s", e)

    # ══════════════════════════════════════════════════════════════
    # DASHBOARD STATE
    # ══════════════════════════════════════════════════════════════

    def get_dashboard_state(self) -> Dict[str, Any]:
        """Return full BotBrain state for the /api/brain/state endpoint."""
        try:
            return {
                "enabled": True,
                "dry_run": self._dry_run,
                "trades_processed": self._trades_processed,
                "total_observations": self._memory._total_observations,
                "matrix_cells": len(self._memory._matrix),
                "recommendations_acted": self._recommendations_acted,
                "directives_issued": self._directives_issued,
                "active_directives": {
                    "trading_paused": self._trading_paused,
                    "pause_reason": self._pause_reason,
                    "suppressed_scanners": list(self._suppressed_scanners),
                    "suppressed_hours": sorted(self._suppressed_hours),
                    "size_mult_override": self._size_mult_override if self._size_mult_trades_remaining > 0 else 1.0,
                    "side_penalties": dict(self._side_penalty),
                },
                "bad_hours": sorted(self._memory.get_bad_hours()),
                "best_hours": sorted(self._memory.get_best_hours()),
                "session": self._session.get_today_stats(),
                "weekly": self._session.get_weekly_review(),
                "optimizer": self._optimizer.get_state(),
            }
        except Exception as e:
            logger.error("BotBrain get_dashboard_state error: %s", e)
            return {"enabled": True, "error": str(e)}

    def get_matrix_data(self) -> Dict[str, Any]:
        """Return performance matrix for /api/brain/matrix endpoint."""
        return self._memory.get_matrix_summary()

    def get_regime_data(self) -> Dict[str, Any]:
        """Return regime history for /api/brain/regime-history endpoint."""
        try:
            result = {}
            for sym, history in self._memory._regime_history.items():
                result[sym] = {
                    "current": history[-1].regime if history else "unknown",
                    "current_confidence": history[-1].confidence if history else 0,
                    "history_count": len(history),
                    "last_5": [
                        {"regime": s.regime, "confidence": s.confidence, "ts": s.timestamp}
                        for s in list(history)[-5:]
                    ],
                }

            # Add transition predictions
            predictions = {}
            for sym in result:
                pred, prob = self._memory.predict_next_regime(sym)
                predictions[sym] = {"predicted": pred, "probability": round(prob, 2)}

            return {
                "symbols": result,
                "predictions": predictions,
                "transitions": self._memory._regime_transitions,
            }
        except Exception as e:
            logger.error("BotBrain get_regime_data error: %s", e)
            return {}

    def get_hourly_data(self) -> Dict[str, Any]:
        """Return hourly heatmap for /api/brain/hourly-heatmap endpoint."""
        return self._memory.get_hourly_heatmap()

    def get_sessions_data(self) -> Dict[str, Any]:
        """Return session summaries for /api/brain/sessions endpoint."""
        try:
            summaries = sorted(
                self._memory._daily_summaries.values(),
                key=lambda s: s.date, reverse=True,
            )[:30]
            return {
                "today": self._session.get_today_stats(),
                "weekly_review": self._session.get_weekly_review(),
                "daily_summaries": [
                    {
                        "date": s.date,
                        "trades": s.total_trades,
                        "wins": s.wins,
                        "wr": round(s.wins / s.total_trades * 100, 1) if s.total_trades > 0 else 0,
                        "pnl": s.total_pnl_usd,
                        "best_scanner": s.best_scanner,
                        "worst_scanner": s.worst_scanner,
                        "dominant_regime": s.dominant_regime,
                        "regime_changes": s.regime_changes,
                    }
                    for s in summaries
                ],
            }
        except Exception as e:
            logger.error("BotBrain get_sessions_data error: %s", e)
            return {}
