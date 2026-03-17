"""
Scanner Weight Manager — Dynamic weighting based on R-performance data.

Reads per-scanner R-metrics from SignalTracker and computes:
- Weight multipliers (0.0 to 1.5) for each scanner
- Scanner status: active / reduced / suppressed / shadow
- Confidence adjustments based on historical edge

Scanners with proven negative expectancy are automatically suppressed.
Scanners with strong positive expectancy get confidence boosts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
_WEIGHTS_FILE = _STORAGE_DIR / "scanner_weights.json"

# Scanner status levels
STATUS_ACTIVE = "active"           # Full weight, normal operation
STATUS_REDUCED = "reduced"         # Lower weight, fewer signals accepted
STATUS_SUPPRESSED = "suppressed"   # Zero weight, runs in shadow only
STATUS_SHADOW = "shadow"           # Manually disabled, data collection only


@dataclass
class ScannerState:
    """Current state and weight for a single scanner."""
    name: str
    status: str = STATUS_ACTIVE
    weight: float = 1.0             # 0.0-1.5 multiplier on confidence
    expectancy_r: float = 0.0
    avg_r: float = 0.0
    total_r: float = 0.0
    win_rate: float = 0.0
    sample_count: int = 0
    edge_ratio: float = 0.0        # MFE/MAE
    avg_mae_r: float = 0.0
    avg_mfe_r: float = 0.0
    last_updated: str = ""
    reason: str = ""                # Why this status was assigned
    shadow_r_values: list = field(default_factory=list)  # R-multiples of shadow trades
    rolling_expectancy: float = 0.0   # last-20-trade expectancy
    recovery_stage: str = ""          # "", "monitoring", "probation"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Ensure shadow_r_values is serialised as a plain list
        d["shadow_r_values"] = list(d.get("shadow_r_values", []))
        return d


class ScannerWeightManager:
    """Manages dynamic scanner weights based on R-performance data.

    Thresholds:
    - MIN_SAMPLES: Don't adjust weights until scanner has this many trades
    - SUPPRESS below -0.3 expectancy_r
    - REDUCE between -0.3 and -0.1
    - BOOST above +0.3
    - STRONG_BOOST above +0.5
    """

    MIN_SAMPLES = 8                   # Minimum trades before adjusting
    SUPPRESS_EXPECTANCY = -0.3        # Expectancy below this → suppress
    REDUCE_EXPECTANCY = -0.1          # Expectancy below this → reduce
    BOOST_EXPECTANCY = 0.3            # Expectancy above this → boost
    STRONG_BOOST_EXPECTANCY = 0.5     # Strong positive → max boost

    REDUCED_WEIGHT = 0.6             # Weight when reduced
    SUPPRESSED_WEIGHT = 0.0          # Weight when suppressed
    BOOSTED_WEIGHT = 1.2             # Weight when boosted
    STRONG_BOOST_WEIGHT = 1.4        # Weight when strongly boosted

    # Rolling / Shadow recovery parameters
    ROLLING_WINDOW = 20
    SHADOW_MIN_TRADES = 10
    SHADOW_RECOVERY_EXPECTANCY = 0.1   # minimum to start recovery
    SHADOW_FULL_RECOVERY_TRADES = 20
    SHADOW_FULL_RECOVERY_EXPECTANCY = 0.3

    # Manual overrides: scanners forced into specific states
    FORCED_STATES: Dict[str, str] = {
        "supertrend_flip": STATUS_SHADOW,   # 26% WR, -9.62 total R
        "momentum_surge": STATUS_SHADOW,     # 39% WR, -0.41 total R
    }

    def __init__(self) -> None:
        _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._states: Dict[str, ScannerState] = {}
        self._load()

    def update_weights(self, by_setup: Dict[str, Dict[str, Any]]) -> None:
        """Update scanner weights from R-metrics data.

        Args:
            by_setup: Dict from signal_tracker stats, keyed by setup_type name,
                     containing: total, wins, win_rate, avg_r, total_r,
                     expectancy_r, avg_win_r, avg_loss_r, avg_mae_r, avg_mfe_r
        """
        now = datetime.now(timezone.utc).isoformat()

        for scanner_name, metrics in by_setup.items():
            if not scanner_name:
                continue  # Skip empty scanner names

            sample_count = metrics.get("total", 0)
            expectancy = metrics.get("expectancy_r", 0.0)
            avg_r = metrics.get("avg_r", 0.0)
            total_r = metrics.get("total_r", 0.0)
            win_rate = metrics.get("win_rate", 0.0)
            avg_mae = metrics.get("avg_mae_r", 0.0)
            avg_mfe = metrics.get("avg_mfe_r", 0.0)
            edge_ratio = avg_mfe / avg_mae if avg_mae > 0 else 0.0

            state = self._states.get(scanner_name, ScannerState(name=scanner_name))

            # Update metrics
            state.expectancy_r = expectancy
            state.avg_r = avg_r
            state.total_r = total_r
            state.win_rate = win_rate
            state.sample_count = sample_count
            state.edge_ratio = round(edge_ratio, 4)
            state.avg_mae_r = avg_mae
            state.avg_mfe_r = avg_mfe
            state.last_updated = now

            # Check forced overrides first
            if scanner_name in self.FORCED_STATES:
                state.status = self.FORCED_STATES[scanner_name]
                state.weight = self.SUPPRESSED_WEIGHT
                state.reason = "Manually disabled (poor historical performance)"
                self._states[scanner_name] = state
                continue

            # Not enough data → keep active with neutral weight
            if sample_count < self.MIN_SAMPLES:
                state.status = STATUS_ACTIVE
                state.weight = 1.0
                state.reason = f"Learning ({sample_count}/{self.MIN_SAMPLES} trades)"
                self._states[scanner_name] = state
                continue

            # Determine status based on expectancy
            if expectancy <= self.SUPPRESS_EXPECTANCY:
                state.status = STATUS_SUPPRESSED
                state.weight = self.SUPPRESSED_WEIGHT
                state.reason = f"Negative edge: {expectancy:+.3f}R expectancy over {sample_count} trades"
            elif expectancy <= self.REDUCE_EXPECTANCY:
                state.status = STATUS_REDUCED
                state.weight = self.REDUCED_WEIGHT
                state.reason = f"Weak edge: {expectancy:+.3f}R expectancy"
            elif expectancy >= self.STRONG_BOOST_EXPECTANCY:
                state.status = STATUS_ACTIVE
                state.weight = self.STRONG_BOOST_WEIGHT
                state.reason = f"Strong edge: {expectancy:+.3f}R, {win_rate:.0f}% WR"
            elif expectancy >= self.BOOST_EXPECTANCY:
                state.status = STATUS_ACTIVE
                state.weight = self.BOOSTED_WEIGHT
                state.reason = f"Good edge: {expectancy:+.3f}R, {win_rate:.0f}% WR"
            else:
                state.status = STATUS_ACTIVE
                state.weight = 1.0
                state.reason = f"Neutral: {expectancy:+.3f}R expectancy"

            self._states[scanner_name] = state

        self._save()
        logger.info(
            "Scanner weights updated: %s",
            {n: f"{s.status}({s.weight:.1f}x)" for n, s in self._states.items()},
        )

    def get_weight(self, scanner_name: str) -> float:
        """Return current weight multiplier for a scanner."""
        state = self._states.get(scanner_name)
        if state is None:
            # Check forced states for unknown scanners
            if scanner_name in self.FORCED_STATES:
                return self.SUPPRESSED_WEIGHT
            return 1.0
        return state.weight

    def get_status(self, scanner_name: str) -> str:
        """Return active/reduced/suppressed/shadow."""
        state = self._states.get(scanner_name)
        if state is None:
            if scanner_name in self.FORCED_STATES:
                return self.FORCED_STATES[scanner_name]
            return STATUS_ACTIVE
        return state.status

    def is_tradeable(self, scanner_name: str) -> bool:
        """Return True if this scanner is allowed to generate live signals."""
        status = self.get_status(scanner_name)
        return status in (STATUS_ACTIVE, STATUS_REDUCED)

    def get_confidence_adjustment(self, scanner_name: str, base_confidence: int) -> int:
        """Apply weight-based confidence adjustment.

        Returns adjusted confidence. Boosted scanners get +5-10,
        reduced scanners get -10-15.
        """
        weight = self.get_weight(scanner_name)
        if weight >= self.STRONG_BOOST_WEIGHT:
            return min(base_confidence + 10, 100)
        elif weight >= self.BOOSTED_WEIGHT:
            return min(base_confidence + 5, 100)
        elif weight <= self.REDUCED_WEIGHT and weight > 0:
            return max(base_confidence - 10, 0)
        return base_confidence

    # ------------------------------------------------------------------
    # Shadow mode recovery & rolling metrics
    # ------------------------------------------------------------------

    def record_shadow_trade(self, scanner_name: str, r_value: float) -> None:
        """Record a shadow trade result for a suppressed/shadow scanner."""
        state = self._states.get(scanner_name)
        if state is None:
            state = ScannerState(name=scanner_name)
            self._states[scanner_name] = state
        state.shadow_r_values.append(round(r_value, 4))
        # Keep last 50
        state.shadow_r_values = state.shadow_r_values[-50:]
        self._save()

    def check_shadow_recovery(self) -> Dict[str, str]:
        """Check if any suppressed scanners should be promoted based on shadow performance.
        Returns dict of scanner_name -> action taken.
        """
        actions: Dict[str, str] = {}
        for name, state in self._states.items():
            if state.status not in (STATUS_SUPPRESSED, STATUS_SHADOW):
                continue
            if name in self.FORCED_STATES:
                # Check if forced state should be lifted
                shadow = state.shadow_r_values
                if len(shadow) >= self.SHADOW_FULL_RECOVERY_TRADES:
                    avg = sum(shadow[-self.SHADOW_FULL_RECOVERY_TRADES:]) / self.SHADOW_FULL_RECOVERY_TRADES
                    if avg >= self.SHADOW_FULL_RECOVERY_EXPECTANCY:
                        state.status = STATUS_REDUCED
                        state.weight = self.REDUCED_WEIGHT
                        state.reason = f"Shadow recovery: {avg:+.3f}R over {self.SHADOW_FULL_RECOVERY_TRADES} shadow trades"
                        state.recovery_stage = "probation"
                        actions[name] = f"SHADOW→REDUCED (shadow exp={avg:+.3f}R)"
                        logger.info("Scanner %s promoted from shadow to reduced: %s", name, state.reason)
                continue

            shadow = state.shadow_r_values
            if len(shadow) < self.SHADOW_MIN_TRADES:
                continue

            recent = shadow[-self.ROLLING_WINDOW:]
            avg_r = sum(recent) / len(recent)

            if state.status == STATUS_SUPPRESSED:
                if len(shadow) >= self.SHADOW_MIN_TRADES and avg_r >= self.SHADOW_RECOVERY_EXPECTANCY:
                    state.status = STATUS_REDUCED
                    state.weight = self.REDUCED_WEIGHT
                    state.reason = f"Recovery: shadow exp {avg_r:+.3f}R over {len(recent)} trades"
                    state.recovery_stage = "probation"
                    actions[name] = f"SUPPRESSED→REDUCED (shadow exp={avg_r:+.3f}R)"
                    logger.info("Scanner %s promoted: %s", name, state.reason)

            elif state.status == STATUS_REDUCED and state.recovery_stage == "probation":
                if len(shadow) >= self.SHADOW_FULL_RECOVERY_TRADES and avg_r >= self.SHADOW_FULL_RECOVERY_EXPECTANCY:
                    state.status = STATUS_ACTIVE
                    state.weight = 1.0
                    state.reason = f"Full recovery: exp {avg_r:+.3f}R over {len(recent)} trades"
                    state.recovery_stage = ""
                    actions[name] = f"REDUCED→ACTIVE (exp={avg_r:+.3f}R)"
                    logger.info("Scanner %s fully recovered: %s", name, state.reason)

        if actions:
            self._save()
        return actions

    def compute_rolling_metrics(self, by_setup: Dict[str, Dict]) -> None:
        """Compute rolling expectancy for each scanner from recent trades."""
        for scanner_name, metrics in by_setup.items():
            state = self._states.get(scanner_name)
            if state is None:
                continue
            r_vals = metrics.get("r_values_raw", [])
            if len(r_vals) >= self.ROLLING_WINDOW:
                recent = r_vals[-self.ROLLING_WINDOW:]
                wins = [r for r in recent if r > 0]
                losses = [r for r in recent if r < 0]
                wr = len(wins) / len(recent)
                avg_win = sum(wins) / len(wins) if wins else 0
                avg_loss = sum(losses) / len(losses) if losses else 0
                state.rolling_expectancy = round(wr * avg_win + (1 - wr) * avg_loss, 4)

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """Return all scanner states for dashboard display."""
        return {name: state.to_dict() for name, state in self._states.items()}

    def get_dashboard_summary(self) -> List[Dict[str, Any]]:
        """Return sorted list of scanner states for dashboard."""
        result = []
        for name, state in self._states.items():
            result.append({
                "scanner": name,
                "status": state.status,
                "weight": state.weight,
                "expectancy_r": state.expectancy_r,
                "win_rate": state.win_rate,
                "trades": state.sample_count,
                "total_r": state.total_r,
                "edge_ratio": state.edge_ratio,
                "reason": state.reason,
                "rolling_expectancy": state.rolling_expectancy,
                "recovery_stage": state.recovery_stage,
                "shadow_trades": len(state.shadow_r_values),
            })
        # Sort: active first, then by expectancy
        status_order = {STATUS_ACTIVE: 0, STATUS_REDUCED: 1, STATUS_SUPPRESSED: 2, STATUS_SHADOW: 3}
        result.sort(key=lambda x: (status_order.get(x["status"], 9), -x["expectancy_r"]))
        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            if _WEIGHTS_FILE.exists():
                data = json.loads(_WEIGHTS_FILE.read_text(encoding="utf-8"))
                for name, d in data.items():
                    self._states[name] = ScannerState(**{
                        k: v for k, v in d.items()
                        if k in ScannerState.__dataclass_fields__
                    })
                logger.info("Loaded %d scanner weights", len(self._states))
        except Exception as exc:
            logger.warning("Failed to load scanner weights: %s", exc)

    def _save(self) -> None:
        try:
            data = {name: state.to_dict() for name, state in self._states.items()}
            _WEIGHTS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save scanner weights: %s", exc)
