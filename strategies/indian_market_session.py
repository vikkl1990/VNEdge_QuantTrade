"""
Indian Market Session Engine
============================
Detects NSE open/close windows, F&O expiry days, and provides
confidence adjustments + regime overrides for Indian market flow hours.

Data-driven:
  - 9:00-9:30 IST (NSE auction)  → BLOCK (57% WR = whipsaw noise)
  - 9:45-11:00 IST (post-open)   → BOOST +12 (89.5% WR, regime override)
  - 15:00-16:00 IST (NSE close)  → BOOST +15 (91% WR, best hour)
  - F&O expiry Thursdays         → +5 extra during flow hours

All windows are config-driven via settings.yaml → indian_market section.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# IST timezone (UTC+5:30)
_IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class IndianSessionContext:
    """Result of Indian market session analysis."""
    session_name: str                   # e.g. "nse_post_open", "nse_close_rebalance"
    is_indian_flow_hour: bool           # True during boost windows (regime override active)
    confidence_adjustment: int          # Applied to signal confidence
    regime_override: Optional[str]      # None or "ranging_limited"
    block: bool                         # True = hard block (no trading)
    block_reason: str                   # Human-readable reason
    is_fno_expiry_day: bool            # Thursday (or shifted)
    fno_expiry_boost: int              # Extra confidence on expiry days
    session_label: str                  # Human-readable label for logs/dashboard


# Default window definitions (used if config has no windows list)
_DEFAULT_WINDOWS = [
    {"name": "nse_open_auction",    "start_hour": 9.0,  "end_hour": 9.5,  "confidence_adj": 0,   "block": True,  "regime_override": ""},
    {"name": "nse_open_settle",     "start_hour": 9.5,  "end_hour": 9.75, "confidence_adj": -10,  "block": False, "regime_override": ""},
    {"name": "nse_post_open",       "start_hour": 9.75, "end_hour": 11.0, "confidence_adj": 12,   "block": False, "regime_override": "ranging_limited"},
    {"name": "nse_midday",          "start_hour": 11.0, "end_hour": 14.5, "confidence_adj": 0,    "block": False, "regime_override": ""},
    {"name": "nse_pre_close",       "start_hour": 14.5, "end_hour": 15.0, "confidence_adj": 5,    "block": False, "regime_override": ""},
    {"name": "nse_close_rebalance", "start_hour": 15.0, "end_hour": 16.0, "confidence_adj": 15,   "block": False, "regime_override": "ranging_limited"},
]

# Non-Indian fallback context (zero impact)
_NON_INDIAN = IndianSessionContext(
    session_name="non_indian",
    is_indian_flow_hour=False,
    confidence_adjustment=0,
    regime_override=None,
    block=False,
    block_reason="",
    is_fno_expiry_day=False,
    fno_expiry_boost=0,
    session_label="Non-Indian Hours",
)


class IndianMarketSessionEngine:
    """Stateless engine that evaluates Indian market session context.

    Usage:
        engine = IndianMarketSessionEngine(config_dict)
        ctx = engine.evaluate(datetime.now(IST))
        if ctx.block:
            return []  # no trading
        if ctx.regime_override == "ranging_limited":
            # allow ranging scanners even in quiet regime
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self._enabled = config.get("enabled", False)
        self._fno_expiry_day = config.get("fno_expiry_day", "thursday").lower()
        self._fno_expiry_boost = int(config.get("fno_expiry_boost", 5))
        self._holiday_file = config.get("holiday_file", "")
        self._ranging_limited_scanners: List[str] = config.get(
            "ranging_limited_scanners",
            ["liquidity_sweep", "vwap_mean_revert", "structure_bounce", "rsi_divergence"],
        )

        # Parse windows from config (or use defaults)
        raw_windows = config.get("windows", [])
        self._windows = raw_windows if raw_windows else _DEFAULT_WINDOWS

        # Load NSE holidays (fail-open: if file missing, no holidays)
        self._holidays: set = set()
        self._load_holidays()

        if self._enabled:
            logger.info(
                "IndianMarketSessionEngine: ENABLED | windows=%d | fno_day=%s | holidays=%d",
                len(self._windows), self._fno_expiry_day, len(self._holidays),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, dt: datetime) -> IndianSessionContext:
        """Main entry. Given a datetime, return Indian session context."""
        if not self._enabled:
            return _NON_INDIAN

        ist_dt = self._to_ist(dt)
        ist_hour = ist_dt.hour + ist_dt.minute / 60.0
        today = ist_dt.date()

        # Check if NSE is open today (weekday + not holiday)
        is_nse_day = self._is_nse_trading_day(today)
        if not is_nse_day:
            return _NON_INDIAN

        # F&O expiry check
        is_fno = self._is_fno_expiry(today)

        # Classify into a window
        for w in self._windows:
            start = float(w.get("start_hour", 0))
            end = float(w.get("end_hour", 0))
            if start <= ist_hour < end:
                name = w.get("name", "unknown")
                conf_adj = int(w.get("confidence_adj", 0))
                block = bool(w.get("block", False))
                regime_ovr = w.get("regime_override", "") or None

                # Determine if this is a flow hour (has regime override)
                is_flow = regime_ovr == "ranging_limited"

                # F&O expiry boost only during flow hours
                fno_boost = self._fno_expiry_boost if (is_fno and is_flow) else 0

                block_reason = ""
                if block:
                    block_reason = f"NSE opening auction noise (9:00-9:30 IST, 57% WR)"

                # Human label
                labels = {
                    "nse_open_auction": "NSE Open Auction (BLOCKED)",
                    "nse_open_settle": "NSE Open Settle",
                    "nse_post_open": "NSE Post-Open Flow",
                    "nse_midday": "NSE Midday",
                    "nse_pre_close": "NSE Pre-Close",
                    "nse_close_rebalance": "NSE Close Rebalance",
                }
                label = labels.get(name, name)
                if is_fno:
                    label += " [F&O EXPIRY]"

                return IndianSessionContext(
                    session_name=name,
                    is_indian_flow_hour=is_flow,
                    confidence_adjustment=conf_adj,
                    regime_override=regime_ovr,
                    block=block,
                    block_reason=block_reason,
                    is_fno_expiry_day=is_fno,
                    fno_expiry_boost=fno_boost,
                    session_label=label,
                )

        # Outside all windows
        return _NON_INDIAN

    def get_ranging_limited_scanners(self) -> List[str]:
        """Return the list of scanner names allowed during ranging_limited override."""
        return list(self._ranging_limited_scanners)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _to_ist(self, dt: datetime) -> datetime:
        """Convert any datetime to IST."""
        if dt.tzinfo is None:
            # Assume UTC if naive
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_IST)

    def _is_nse_trading_day(self, d: date) -> bool:
        """Check if NSE is open on this date (weekday + not holiday)."""
        # Weekday: Monday=0 ... Friday=4
        if d.weekday() >= 5:
            return False
        if d in self._holidays:
            return False
        return True

    def _is_fno_expiry(self, d: date) -> bool:
        """Check if today is F&O expiry day.

        Standard: last Thursday of the month.
        If Thursday is a holiday → Wednesday becomes expiry.
        Weekly expiry: every Thursday (or Wednesday if holiday).
        """
        day_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2,
            "thursday": 3, "friday": 4,
        }
        target_weekday = day_map.get(self._fno_expiry_day, 3)

        if d.weekday() == target_weekday and d not in self._holidays:
            return True

        # If the normal expiry day is a holiday, check if today is the day before
        if d.weekday() == target_weekday - 1:
            normal_expiry = d + timedelta(days=1)
            if normal_expiry in self._holidays:
                return True

        return False

    def _load_holidays(self) -> None:
        """Load NSE holidays from JSON file.

        Expected format: ["2026-01-26", "2026-03-14", ...]
        Fail-open: if file missing or invalid, no holidays loaded.
        """
        if not self._holiday_file:
            return
        try:
            path = Path(self._holiday_file)
            if path.exists():
                with open(path) as f:
                    dates = json.load(f)
                for ds in dates:
                    try:
                        self._holidays.add(date.fromisoformat(ds))
                    except (ValueError, TypeError):
                        pass
                logger.info("Loaded %d NSE holidays from %s", len(self._holidays), path)
            else:
                logger.warning("NSE holiday file not found: %s (proceeding without holidays)", path)
        except Exception as e:
            logger.warning("Failed to load NSE holidays: %s (proceeding without)", e)
