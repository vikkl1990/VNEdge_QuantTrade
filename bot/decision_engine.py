"""
Decision Engine — Synthesizes all signals into a single actionable directive.

At any moment, the user sees:
- MARKET_STATE: TREND_STRONG / TREND_WEAK / SIDEWAYS / CHOP / LOW_LIQ
- EDGE_STATUS: STRONG / MEDIUM / WEAK / OFF
- ACTION: LONG / SHORT / WAIT
- BEST_SETUP: symbol + scanner + score + grade
- RISK_STATE: NORMAL / REDUCED / BLOCKED
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    """Single actionable output from the decision engine."""
    # Market assessment
    market_state: str = "UNKNOWN"          # TREND_STRONG, TREND_WEAK, SIDEWAYS, CHOP, LOW_LIQ
    edge_status: str = "OFF"               # STRONG, MEDIUM, WEAK, OFF
    action: str = "WAIT"                   # LONG, SHORT, WAIT

    # Best opportunity
    best_symbol: str = ""
    best_scanner: str = ""
    best_score: float = 0.0
    best_grade: str = ""
    best_tier: str = ""

    # Risk assessment
    risk_state: str = "NORMAL"             # NORMAL, REDUCED, BLOCKED
    risk_reason: str = ""

    # Context
    regime: str = "unknown"
    session: str = ""
    rolling_expectancy: float = 0.0
    drawdown_pct: float = 0.0
    signals_this_hour: int = 0

    # Trade plan (when action is TRADE)
    best_side: str = ""           # LONG/SHORT
    best_entry: float = 0.0
    best_stop: float = 0.0
    best_target: float = 0.0     # TP1
    best_atr: float = 0.0
    blocker: str = ""             # reason for WAIT

    # Reasoning
    reason: str = "Initializing..."
    reasons: List[str] = field(default_factory=list)

    # Timestamp
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DecisionEngine:
    """Computes a real-time TRADE/WAIT directive from all available data.

    Inputs consumed:
    1. Latest scan results (from scalp strategy)
    2. Regime state (from regime filter)
    3. R-metrics (from signal tracker stats)
    4. Risk guard state (from trade monitor)
    5. Scanner health (from weight manager)
    6. Current session info
    """

    # Edge thresholds
    STRONG_EDGE_EXPECTANCY = 0.3      # rolling exp > 0.3R = strong edge
    MEDIUM_EDGE_EXPECTANCY = 0.1      # rolling exp > 0.1R = medium
    WEAK_EDGE_EXPECTANCY = 0.0        # rolling exp > 0 = weak

    # Score thresholds for action
    ACTION_SCORE_THRESHOLD = 65       # weighted score needed for LONG/SHORT

    def __init__(self) -> None:
        self._last_decision = Decision()
        self._last_update: float = 0.0
        self._update_interval: float = 5.0  # re-evaluate every 5s

    @property
    def decision(self) -> Decision:
        return self._last_decision

    def update(
        self,
        *,
        scan_results: Optional[List[Dict[str, Any]]] = None,
        regime_info: Optional[Dict[str, Any]] = None,
        r_metrics: Optional[Dict[str, Any]] = None,
        risk_guard: Optional[Dict[str, Any]] = None,
        scanner_health: Optional[List[Dict[str, Any]]] = None,
        session: str = "",
        signals_this_hour: int = 0,
        funnel: Optional[Dict[str, int]] = None,
    ) -> Decision:
        """Recompute the decision from all available inputs."""
        from datetime import datetime, timezone, timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))

        d = Decision()
        d.session = session
        d.signals_this_hour = signals_this_hour
        d.updated_at = datetime.now(_IST).isoformat()
        d.reasons = []

        # ── 1. Risk State ──
        if risk_guard:
            should_pause = risk_guard.get("should_pause", False)
            pause_reason = risk_guard.get("reason", "")
            dd_pct = risk_guard.get("drawdown", 0)
            d.drawdown_pct = dd_pct

            if should_pause:
                d.risk_state = "BLOCKED"
                d.risk_reason = pause_reason
                d.action = "WAIT"
                d.reason = f"Risk guard: {pause_reason}"
                d.reasons.append(f"BLOCKED: {pause_reason}")
            elif dd_pct >= 4.0:
                d.risk_state = "REDUCED"
                d.risk_reason = f"Drawdown {dd_pct:.1f}%"
                d.reasons.append(f"Reduced risk: DD={dd_pct:.1f}%")
            else:
                d.risk_state = "NORMAL"

        # ── 2. Market State (from regime) ──
        if regime_info:
            regime = regime_info.get("regime", "unknown")
            d.regime = regime

            regime_map = {
                "trending_up": "TREND_STRONG",
                "trending_down": "TREND_STRONG",
                "ranging": "SIDEWAYS",
                "volatile": "CHOP",
                "quiet": "TREND_WEAK",
                "unknown": "UNKNOWN",
            }
            d.market_state = regime_map.get(regime, "UNKNOWN")

            # Refine: check if regime allows trading
            action_info = regime_info.get("action", {})
            if not action_info.get("allow_trade", True):
                d.reasons.append(f"Regime blocks: {action_info.get('reason', regime)}")

        # ── 3. Edge Status (from R-metrics) ──
        if r_metrics:
            global_r = r_metrics if "expectancy_r" in r_metrics else r_metrics.get("global", {})
            exp = global_r.get("expectancy_r", 0)
            d.rolling_expectancy = exp

            if exp >= self.STRONG_EDGE_EXPECTANCY:
                d.edge_status = "STRONG"
                d.reasons.append(f"Edge strong: {exp:+.3f}R expectancy")
            elif exp >= self.MEDIUM_EDGE_EXPECTANCY:
                d.edge_status = "MEDIUM"
                d.reasons.append(f"Edge medium: {exp:+.3f}R")
            elif exp >= self.WEAK_EDGE_EXPECTANCY:
                d.edge_status = "WEAK"
                d.reasons.append(f"Edge weak: {exp:+.3f}R")
            else:
                d.edge_status = "OFF"
                d.reasons.append(f"No edge: {exp:+.3f}R expectancy")

        # ── 4. Best Setup (from scan results) ──
        if scan_results:
            # Find the best triggered scanner result
            triggered = [
                sr for sr in scan_results
                if sr.get("triggered") and sr.get("tier") in ("strong", "valid", "weak")
            ]
            if triggered:
                best = max(triggered, key=lambda x: x.get("weighted_score", 0))
                d.best_symbol = best.get("symbol", "")
                d.best_scanner = best.get("name", best.get("scanner", ""))
                d.best_score = best.get("weighted_score", 0)
                d.best_grade = best.get("grade", "")
                d.best_tier = best.get("tier", "")

                # Trade plan fields
                d.best_side = best.get("side", "")
                d.best_entry = best.get("entry_price", 0) or 0
                d.best_stop = best.get("stop_loss", 0) or 0
                d.best_atr = best.get("atr", 0) or 0
                # Calculate TP1: entry +/- 1.5*ATR
                if d.best_entry and d.best_atr:
                    if d.best_side and d.best_side.upper() in ("BUY", "LONG"):
                        d.best_target = d.best_entry + 1.5 * d.best_atr
                    else:
                        d.best_target = d.best_entry - 1.5 * d.best_atr

                d.reasons.append(f"Best: {d.best_scanner} {d.best_tier} ({d.best_score:.0f})")

        # ── 5. Compute final ACTION ──
        if d.risk_state == "BLOCKED":
            d.action = "WAIT"
            d.reason = f"Risk blocked: {d.risk_reason}"
            d.blocker = f"Risk blocked: {d.risk_reason}"
        elif d.edge_status == "OFF" and d.risk_state != "NORMAL":
            d.action = "WAIT"
            d.reason = "No edge + elevated risk"
            d.blocker = "No edge + elevated risk"
        elif d.best_score >= self.ACTION_SCORE_THRESHOLD and d.best_tier in ("strong", "valid"):
            # We have a tradeable signal
            d.action = "TRADE"
            d.reason = f"{d.best_scanner} {d.best_tier} ({d.best_score:.0f}) in {d.regime}"
        elif d.best_score > 0 and d.best_tier == "weak":
            d.action = "WAIT"
            d.reason = f"Weak signal: {d.best_scanner} ({d.best_score:.0f}) — needs stronger setup"
            d.blocker = f"Weak signal: {d.best_scanner} ({d.best_score:.0f}) — needs stronger setup"
        elif d.market_state == "CHOP":
            d.action = "WAIT"
            d.reason = "Choppy market — waiting for trend"
            d.blocker = "Choppy market — waiting for trend"
        elif d.edge_status in ("STRONG", "MEDIUM"):
            d.action = "WAIT"
            d.reason = f"Edge exists ({d.rolling_expectancy:+.2f}R) — waiting for setup"
            d.blocker = f"Edge exists ({d.rolling_expectancy:+.2f}R) — waiting for setup"
        else:
            d.action = "WAIT"
            d.reason = "No valid setup conditions met"
            d.blocker = "No valid setup conditions met"

        # ── 6. Funnel context ──
        if funnel:
            scanned = funnel.get("scanned", 0)
            strong = funnel.get("strong", 0)
            valid = funnel.get("valid", 0)
            if scanned > 0 and strong == 0 and valid == 0:
                d.reasons.append(f"Signal drought: {scanned} scanned, 0 tradeable this hour")

        # ── 7. Scanner health context ──
        if scanner_health:
            active = sum(1 for s in scanner_health if s.get("status") == "active")
            suppressed = sum(1 for s in scanner_health if s.get("status") in ("suppressed", "shadow"))
            if suppressed > 0:
                d.reasons.append(f"Scanners: {active} active, {suppressed} suppressed")

        # Store
        self._last_decision = d
        self._last_update = time.time()

        return d

    def get_dashboard_data(self) -> Dict[str, Any]:
        """Return decision data formatted for dashboard API."""
        return self._last_decision.to_dict()
