"""
HeartbeatMonitor - tracks bot liveness and detects stale components.

Periodically logs health status and raises alerts when data feeds go
silent or the exchange connection appears lost.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional


class HeartbeatMonitor:
    """Monitors bot health by tracking activity timestamps.

    Parameters
    ----------
    config : dict
        Application configuration.  Relevant keys::

            bot.heartbeat_interval   - seconds between health-check cycles (default 60)
            bot.stale_data_timeout   - seconds before data is considered stale (default 120)
            bot.max_errors_tracked   - recent errors to keep in memory (default 100)

    logger : logging.Logger
        Logger instance (shared with the orchestrator).
    """

    def __init__(self, *, config: dict, logger: logging.Logger) -> None:
        bot_cfg = config.get("bot", {})
        self._interval: float = float(bot_cfg.get("heartbeat_interval", 60))
        self._stale_timeout: float = float(bot_cfg.get("stale_data_timeout", 120))
        max_errors: int = int(bot_cfg.get("max_errors_tracked", 100))

        self._log = logger

        # Timestamp tracking
        self._activities: Dict[str, float] = {}
        self._start_time: Optional[float] = None

        # Error history (bounded deque)
        self._errors: Deque[Dict[str, Any]] = deque(maxlen=max_errors)

        # Internal task handle
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin periodic health-check loop."""
        if self._running:
            return
        self._running = True
        self._start_time = time.monotonic()
        self._activities["monitor_start"] = time.monotonic()
        self._task = asyncio.create_task(self._loop(), name="heartbeat")
        self._log.info(
            "HeartbeatMonitor started (interval=%ds, stale_timeout=%ds)",
            int(self._interval),
            int(self._stale_timeout),
        )

    async def stop(self) -> None:
        """Stop the health-check loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._log.info("HeartbeatMonitor stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_activity(self, name: str) -> None:
        """Record that *name* was active right now.

        Called by the orchestrator whenever a meaningful event occurs
        (candle close, trade executed, heartbeat log, etc.).
        """
        self._activities[name] = time.monotonic()

    def record_error(self, context: str, message: str) -> None:
        """Record an error for the health report."""
        self._errors.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "context": context,
            "message": message[:500],  # cap length
        })

    # One-time events that should not be checked for staleness
    _IGNORE_STALE = frozenset({"monitor_start"})

    def get_stale_components(self) -> list[str]:
        """Return names of components that have not reported activity
        within the stale timeout window.

        One-time events (like ``monitor_start``) are excluded from
        staleness checks since they are recorded once and never updated.
        """
        now = time.monotonic()
        stale = []
        for name, last_ts in self._activities.items():
            if name in self._IGNORE_STALE:
                continue
            if now - last_ts > self._stale_timeout:
                stale.append(name)
        return stale

    @property
    def uptime(self) -> float:
        """Seconds since the monitor was started."""
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    @property
    def last_activity_age(self) -> float:
        """Seconds since the most recent recorded activity (any component)."""
        if not self._activities:
            return float("inf")
        most_recent = max(self._activities.values())
        return time.monotonic() - most_recent

    @property
    def recent_errors(self) -> list[Dict[str, Any]]:
        """Return a copy of the recent error history."""
        return list(self._errors)

    @property
    def health_report(self) -> Dict[str, Any]:
        """Build a structured health report for logging / dashboards."""
        now = time.monotonic()
        activities_summary = {
            name: round(now - ts, 1) for name, ts in self._activities.items()
        }
        stale = self.get_stale_components()
        return {
            "status": "degraded" if stale else "healthy",
            "uptime_s": round(self.uptime, 1),
            "last_activity_age_s": round(self.last_activity_age, 1),
            "stale_components": stale,
            "activity_ages_s": activities_summary,
            "recent_error_count": len(self._errors),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Periodic health-check cycle."""
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break
                self._check_health()
            except asyncio.CancelledError:
                break
            except Exception:
                self._log.exception("Error in heartbeat loop")

    def _check_health(self) -> None:
        """Run a single health check and log findings."""
        report = self.health_report

        if report["status"] == "healthy":
            self._log.debug(
                "Heartbeat OK | uptime=%ds | last_activity=%ds ago | errors=%d",
                int(report["uptime_s"]),
                int(report["last_activity_age_s"]),
                report["recent_error_count"],
            )
        else:
            stale = report["stale_components"]
            self._log.warning(
                "Heartbeat DEGRADED | stale components: %s | "
                "last_activity=%ds ago | errors=%d",
                ", ".join(stale),
                int(report["last_activity_age_s"]),
                report["recent_error_count"],
            )

        # Detect possible exchange disconnection
        # (candle feeds going stale is a strong indicator)
        candle_activities = [
            name for name in report.get("activity_ages_s", {})
            if name.startswith("candle_close:")
        ]
        if candle_activities:
            ages = [
                report["activity_ages_s"][name] for name in candle_activities
            ]
            max_age = max(ages)
            # If ALL candle feeds are stale, exchange may be disconnected
            if min(ages) > self._stale_timeout:
                self._log.error(
                    "ALL candle feeds stale (oldest=%ds) - "
                    "possible exchange disconnection",
                    int(max_age),
                )
