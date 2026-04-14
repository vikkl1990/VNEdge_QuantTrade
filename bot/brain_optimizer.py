"""
AdaptiveOptimizer — Bayesian-style parameter tuning for VN Edge BotBrain.

Continuously adjusts trading parameters based on observed performance,
with hard safety guardrails:
- MIN_SAMPLE_SIZE: won't adjust until enough trades observed
- MAX_ADJUSTMENT: 1 step per optimization cycle
- REVERT_THRESHOLD: auto-reverts if performance drops >15%
- COOLDOWN: minimum trades between adjustments

Phase 7 of BotBrain. Start with real_ml_threshold_min only,
then add others after 100+ trades.

All parameters have explicit min/max bounds and can never exceed them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.brain_memory import BrainMemory

logger = logging.getLogger("bot.brain_optimizer")


@dataclass
class TunableParam:
    """Definition of a parameter that can be auto-tuned."""
    name: str
    default: float
    current: float
    prior: float           # previous value (for revert)
    min_val: float
    max_val: float
    step: float
    metric_at_prior: float = 0.0   # performance metric when prior was set
    metric_at_current: float = 0.0
    trades_at_current: int = 0     # trades observed under current value
    last_adjusted: str = ""
    total_adjustments: int = 0
    direction: int = 0             # +1 = stepping up, -1 = stepping down, 0 = neutral


class AdaptiveOptimizer:
    """Bayesian-style parameter tuner with hard safety rails.

    Observes performance under current parameter values, and steps
    parameters in the direction that improves the primary metric
    (expected R per trade over a rolling window).

    Safety guarantees:
    1. Never adjusts with <MIN_SAMPLE_SIZE trades of data
    2. Max 1 step per optimization cycle (no large jumps)
    3. Auto-reverts if metric drops >REVERT_THRESHOLD from prior
    4. COOLDOWN trades between any two adjustments
    5. All params bounded by [min_val, max_val]
    """

    # ── Safety guardrails ──
    MIN_SAMPLE_SIZE = 20       # trades before adjusting
    MAX_ADJUSTMENT = 1         # max 1 step per cycle
    REVERT_THRESHOLD = -0.15   # if metric drops 15%, revert
    COOLDOWN_TRADES = 10       # min trades between adjustments

    # ── Tunable parameters definition ──
    PARAM_DEFS = {
        "real_ml_threshold_min": {
            "default": 0.65, "min": 0.50, "max": 0.85, "step": 0.02,
        },
        "confidence_floor": {
            "default": 55, "min": 45, "max": 75, "step": 5,
        },
        "sl_multiplier": {
            "default": 1.0, "min": 0.7, "max": 1.5, "step": 0.05,
        },
    }

    def __init__(self, memory: BrainMemory, config: dict = None):
        self._memory = memory
        self._config = config or {}
        self._params: Dict[str, TunableParam] = {}
        self._trades_since_last_adjust: int = 0
        self._total_trades_observed: int = 0
        self._enabled: bool = False  # Phase 7: start disabled, enable after sufficient data

        # Initialize tunable params from config or defaults
        self._init_params()

    def _init_params(self):
        """Initialize tunable parameters from config overrides or defaults."""
        brain_cfg = self._config.get("brain", {}).get("optimizer", {})
        enabled_params = brain_cfg.get("enabled_params", ["real_ml_threshold_min"])

        for name, defn in self.PARAM_DEFS.items():
            # Only activate params that are explicitly enabled
            if name not in enabled_params:
                continue

            # Check if memory has a stored current value
            param_hist = self._memory._param_history.get(name, [])
            current = defn["default"]
            if param_hist:
                current = param_hist[-1].get("value", defn["default"])

            self._params[name] = TunableParam(
                name=name,
                default=defn["default"],
                current=current,
                prior=current,
                min_val=defn["min"],
                max_val=defn["max"],
                step=defn["step"],
            )

        if self._params:
            logger.info(
                "AdaptiveOptimizer: %d params active: %s",
                len(self._params),
                ", ".join(f"{p.name}={p.current}" for p in self._params.values()),
            )

    def enable(self):
        """Enable the optimizer (after sufficient warm-up data)."""
        self._enabled = True
        logger.info("AdaptiveOptimizer ENABLED")

    def disable(self):
        """Disable the optimizer (revert to defaults)."""
        self._enabled = False
        logger.info("AdaptiveOptimizer DISABLED")

    # ══════════════════════════════════════════════════════════════
    # CORE LOOP
    # ══════════════════════════════════════════════════════════════

    def record_trade(self, r_mult: float):
        """Record a trade for optimizer tracking. Called on every trade close."""
        self._total_trades_observed += 1
        self._trades_since_last_adjust += 1
        for p in self._params.values():
            p.trades_at_current += 1

    def step(self, current_metric: float):
        """Run one optimization cycle. Called periodically by BotBrain.

        Args:
            current_metric: rolling expectancy_r (avg R per trade over last N trades).
                           This is the metric we're trying to maximize.
        """
        if not self._enabled:
            return

        if self._trades_since_last_adjust < self.COOLDOWN_TRADES:
            return

        adjusted_any = False
        for name, param in self._params.items():
            if param.trades_at_current < self.MIN_SAMPLE_SIZE:
                continue

            param.metric_at_current = current_metric

            # Check if we should revert (performance dropped)
            if param.metric_at_prior > 0 and param.current != param.prior:
                drop = (current_metric - param.metric_at_prior) / abs(param.metric_at_prior)
                if drop < self.REVERT_THRESHOLD:
                    logger.warning(
                        "OPTIMIZER REVERT: %s %.3f→%.3f (metric dropped %.1f%%)",
                        name, param.current, param.prior, drop * 100,
                    )
                    param.current = param.prior
                    param.direction = 0
                    param.trades_at_current = 0
                    adjusted_any = True
                    self._memory.record_param_change(
                        name, param.current, current_metric,
                        self._total_trades_observed,
                    )
                    continue

            # Try stepping in the current direction (or find a direction)
            if param.direction == 0:
                # First time or after revert — try stepping down (lower threshold = more trades)
                param.direction = -1

            new_val = param.current + param.direction * param.step
            new_val = max(param.min_val, min(param.max_val, new_val))
            new_val = round(new_val, 4)

            if new_val == param.current:
                # Hit boundary, reverse direction
                param.direction *= -1
                continue

            # Apply the adjustment
            param.prior = param.current
            param.metric_at_prior = current_metric
            param.current = new_val
            param.trades_at_current = 0
            param.total_adjustments += 1
            param.last_adjusted = datetime.now(timezone.utc).isoformat()
            adjusted_any = True

            logger.info(
                "OPTIMIZER STEP: %s %.3f→%.3f (metric=%.4f, direction=%+d, adj#%d)",
                name, param.prior, param.current, current_metric,
                param.direction, param.total_adjustments,
            )
            self._memory.record_param_change(
                name, param.current, current_metric,
                self._total_trades_observed,
            )

            break  # Only 1 param per cycle (MAX_ADJUSTMENT)

        if adjusted_any:
            self._trades_since_last_adjust = 0

    # ══════════════════════════════════════════════════════════════
    # GETTERS
    # ══════════════════════════════════════════════════════════════

    def get_current(self, param_name: str) -> Optional[float]:
        """Get current optimized value for a parameter.

        Returns None if param is not being tuned (caller uses its default).
        """
        param = self._params.get(param_name)
        if param and self._enabled:
            return param.current
        return None

    def get_state(self) -> Dict[str, Any]:
        """Return optimizer state for dashboard display."""
        return {
            "enabled": self._enabled,
            "total_trades_observed": self._total_trades_observed,
            "trades_since_last_adjust": self._trades_since_last_adjust,
            "params": {
                name: {
                    "current": p.current,
                    "default": p.default,
                    "prior": p.prior,
                    "min": p.min_val,
                    "max": p.max_val,
                    "step": p.step,
                    "trades_at_current": p.trades_at_current,
                    "metric_at_current": round(p.metric_at_current, 4),
                    "total_adjustments": p.total_adjustments,
                    "direction": p.direction,
                    "last_adjusted": p.last_adjusted,
                }
                for name, p in self._params.items()
            },
            "guardrails": {
                "min_sample_size": self.MIN_SAMPLE_SIZE,
                "cooldown_trades": self.COOLDOWN_TRADES,
                "revert_threshold": self.REVERT_THRESHOLD,
                "max_adjustment_per_cycle": self.MAX_ADJUSTMENT,
            },
        }
