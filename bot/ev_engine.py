"""
Expected Value (EV) Engine — Computes per-scanner EV from empirical R-metrics.

EV Formula:
    EV = (P_win × avg_win_R) - ((1 - P_win) × |avg_loss_R|)

Where:
    P_win = historical win rate for this scanner (from signal_tracker stats)
    avg_win_R = average R-multiple of winning trades
    avg_loss_R = average R-multiple of losing trades (negative number)

Rules:
    - EV > 0.1R → TRADE (positive edge confirmed)
    - EV 0.0-0.1R → REDUCED SIZE (marginal edge)
    - EV < 0.0R → REJECT (negative expectancy)

This replaces rule-based "score >= 65 → trade" with probabilistic edge gating.
No ML needed — purely empirical from your actual trade history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class EVResult:
    """Result of EV computation for a single signal."""
    scanner: str
    ev: float                    # expected value in R-multiples
    p_win: float                 # empirical win probability
    avg_win_r: float             # average winning R
    avg_loss_r: float            # average losing R (negative)
    sample_count: int            # number of historical trades
    verdict: str                 # "TRADE", "REDUCED", "REJECT", "INSUFFICIENT_DATA"
    size_multiplier: float       # 0.0-1.3 based on EV strength
    reason: str                  # human-readable explanation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scanner": self.scanner,
            "ev": round(self.ev, 4),
            "p_win": round(self.p_win, 4),
            "avg_win_r": round(self.avg_win_r, 4),
            "avg_loss_r": round(self.avg_loss_r, 4),
            "sample_count": self.sample_count,
            "verdict": self.verdict,
            "size_multiplier": round(self.size_multiplier, 2),
            "reason": self.reason,
        }


class EVEngine:
    """Computes Expected Value for trade decisions using empirical R-metrics.

    Thresholds:
    - MIN_SAMPLES: Don't gate on EV until scanner has enough history
    - TRADE_EV: Minimum EV to allow full-size trades
    - REDUCED_EV: EV between 0 and TRADE_EV → reduced position size
    - Below 0 → reject (negative expectancy)

    Regime Adjustments:
    - Trending regimes: EV threshold lowered (trend carries edge)
    - Choppy/volatile: EV threshold raised (more noise)
    """

    MIN_SAMPLES = 5              # Need at least 5 trades before gating
    TRADE_EV_THRESHOLD = 0.10    # EV > 0.1R = full trade
    REDUCED_EV_THRESHOLD = 0.0   # EV > 0.0R = reduced size
    MAX_EV_SIZE_MULT = 1.3       # Max size multiplier for high EV

    # Regime-specific EV threshold adjustments
    REGIME_EV_ADJUSTMENTS: Dict[str, float] = {
        "trending_up": -0.05,      # Lower bar in trends (trend adds edge)
        "trending_down": -0.05,
        "breakout": -0.03,
        "sideways": 0.0,
        "ranging": 0.0,
        "mean_reversion": 0.0,
        "volatile": 0.05,          # Higher bar in volatile (more noise)
        "high_volatility": 0.05,
        "quiet": 0.0,
        "low_liquidity": 0.10,     # Much higher bar in thin markets
    }

    def __init__(self) -> None:
        self._last_ev_results: Dict[str, EVResult] = {}

    def compute_ev(
        self,
        scanner_name: str,
        by_setup: Dict[str, Dict[str, Any]],
        regime: str = "",
        side: str = "",
        session: str = "",
    ) -> EVResult:
        """Compute EV for a specific scanner using historical R-metrics.

        Calibrated lookup: tries scanner+side+regime+session first,
        falls back to coarser keys if insufficient data.

        Args:
            scanner_name: Name of the scanner (e.g., "ema_momentum")
            by_setup: Dict from signal_tracker stats
            regime: Current market regime
            side: "long" or "short" for directional calibration
            session: "europe", "us", "asia_early", etc.

        Returns:
            EVResult with verdict and size multiplier
        """
        # Calibrated lookup: regime is the primary context, side/session are optional
        # Changed from over-granular scanner_side_regime_session to scanner_regime primary
        lookup_keys = [
            f"{scanner_name}_{regime}",   # primary: scanner + regime
            scanner_name,                  # fallback: scanner only
        ]

        setup_data = {}
        for key in lookup_keys:
            candidate = by_setup.get(key, {})
            if candidate.get("total", 0) >= self.MIN_SAMPLES:
                setup_data = candidate
                break
        if not setup_data:
            setup_data = by_setup.get(scanner_name, {})
        sample_count = setup_data.get("total", 0)

        # Not enough data — allow trading but flag it
        if sample_count < self.MIN_SAMPLES:
            result = EVResult(
                scanner=scanner_name,
                ev=0.0,
                p_win=0.0,
                avg_win_r=0.0,
                avg_loss_r=0.0,
                sample_count=sample_count,
                verdict="INSUFFICIENT_DATA",
                size_multiplier=0.8,  # slightly reduced — unproven scanner
                reason=f"Only {sample_count}/{self.MIN_SAMPLES} trades — learning",
            )
            self._last_ev_results[scanner_name] = result
            return result

        # Extract empirical metrics
        win_rate_pct = setup_data.get("win_rate", 0)  # as percentage (0-100)
        p_win = win_rate_pct / 100.0
        avg_win_r = setup_data.get("avg_win_r", 0.0)
        avg_loss_r = setup_data.get("avg_loss_r", 0.0)  # already negative

        # EV = (P_win × avg_win_R) + ((1 - P_win) × avg_loss_R)
        # Note: avg_loss_r is already negative, so this naturally subtracts
        ev = (p_win * avg_win_r) + ((1 - p_win) * avg_loss_r)

        # Regime-adjusted thresholds
        regime_adj = self.REGIME_EV_ADJUSTMENTS.get(regime, 0.0)
        trade_threshold = self.TRADE_EV_THRESHOLD + regime_adj
        reduced_threshold = self.REDUCED_EV_THRESHOLD + regime_adj

        # Determine verdict and size multiplier
        if ev >= trade_threshold:
            # Positive edge — trade with EV-scaled size
            # Scale from 1.0 (at threshold) to MAX_EV_SIZE_MULT (at 2x threshold)
            ev_ratio = min(ev / max(trade_threshold, 0.01), 2.0)
            size_mult = min(1.0 + (ev_ratio - 1.0) * 0.3, self.MAX_EV_SIZE_MULT)
            verdict = "TRADE"
            reason = (
                f"EV={ev:+.3f}R > {trade_threshold:.2f}R threshold | "
                f"WR={p_win:.0%} × {avg_win_r:+.2f}R win, "
                f"{1-p_win:.0%} × {avg_loss_r:+.2f}R loss | "
                f"{sample_count} trades"
            )
        elif ev >= reduced_threshold:
            # Marginal edge — trade with reduced size
            size_mult = 0.5 + (ev / max(trade_threshold, 0.01)) * 0.3
            size_mult = max(0.5, min(size_mult, 0.8))
            verdict = "REDUCED"
            reason = (
                f"Marginal EV={ev:+.3f}R (threshold={trade_threshold:.2f}R) | "
                f"Reduced size {size_mult:.0%} | {sample_count} trades"
            )
        else:
            # Negative expectancy — reject
            verdict = "REJECT"
            size_mult = 0.0
            reason = (
                f"Negative EV={ev:+.3f}R | "
                f"WR={p_win:.0%}, avg_win={avg_win_r:+.2f}R, avg_loss={avg_loss_r:+.2f}R | "
                f"{sample_count} trades — REJECTED"
            )

        result = EVResult(
            scanner=scanner_name,
            ev=ev,
            p_win=p_win,
            avg_win_r=avg_win_r,
            avg_loss_r=avg_loss_r,
            sample_count=sample_count,
            verdict=verdict,
            size_multiplier=size_mult,
            reason=reason,
        )
        self._last_ev_results[scanner_name] = result
        return result

    def get_all_ev(self, by_setup: Dict[str, Dict], regime: str = "") -> Dict[str, EVResult]:
        """Compute EV for all scanners at once."""
        results = {}
        for scanner_name in by_setup:
            results[scanner_name] = self.compute_ev(scanner_name, by_setup, regime)
        return results

    def get_last_results(self) -> Dict[str, Dict[str, Any]]:
        """Return last computed EV results for dashboard."""
        return {name: r.to_dict() for name, r in self._last_ev_results.items()}

    def get_dashboard_summary(self) -> Dict[str, Any]:
        """Summary for decision engine / dashboard."""
        if not self._last_ev_results:
            return {"scanners": {}, "best_ev": 0.0, "tradeable_count": 0}

        tradeable = [r for r in self._last_ev_results.values() if r.verdict in ("TRADE", "REDUCED")]
        best = max(self._last_ev_results.values(), key=lambda r: r.ev) if self._last_ev_results else None

        return {
            "scanners": {name: r.to_dict() for name, r in self._last_ev_results.items()},
            "best_ev": round(best.ev, 4) if best else 0.0,
            "best_scanner": best.scanner if best else "",
            "tradeable_count": len(tradeable),
            "total_scanners": len(self._last_ev_results),
        }
