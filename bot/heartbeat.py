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

    # Phase E.7 — per-prefix stale timeout override.
    # Illiquid pairs (LTC, ADA, DOGE, LINK, TAO on Delta India) have sparse 1m
    # candle data and can go 3-5 minutes without a print even when the exchange
    # connection is healthy. The default 120s timeout produced false positive
    # DEGRADED warnings on every heartbeat cycle. Per-prefix override gives
    # candle_close:* events a longer window (300s = 5 minutes) which is still
    # well below the true "exchange is down" threshold but tolerates the
    # natural rate of 1m candle closes on low-volume pairs.
    _PREFIX_STALE_OVERRIDES: Dict[str, float] = {
        "candle_close:": 300.0,  # 5 min for candle_close:{symbol} events
    }

    def get_stale_components(self) -> list[str]:
        """Return names of components that have not reported activity
        within the stale timeout window.

        One-time events (like ``monitor_start``) are excluded from
        staleness checks since they are recorded once and never updated.

        Phase E.7: components matching a prefix in _PREFIX_STALE_OVERRIDES
        use a longer timeout (e.g. candle_close:* gets 300s instead of 120s).
        """
        now = time.monotonic()
        stale = []
        for name, last_ts in self._activities.items():
            if name in self._IGNORE_STALE:
                continue
            # Pick the applicable timeout: longest matching prefix wins
            effective_timeout = self._stale_timeout
            for prefix, override_sec in self._PREFIX_STALE_OVERRIDES.items():
                if name.startswith(prefix):
                    effective_timeout = override_sec
                    break
            if now - last_ts > effective_timeout:
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
            # Phase E.7: only scream if the stale components include something
            # that ISN'T a candle feed OR if ≥3 candle feeds are stale
            # simultaneously. A single stale candle_close:X is almost always
            # an illiquid-pair artifact and shouldn't pollute the log.
            _stale_candle_count = sum(1 for s in stale if s.startswith("candle_close:"))
            _non_candle_stale = [s for s in stale if not s.startswith("candle_close:")]
            _only_sparse_candles = (
                _stale_candle_count <= 2
                and not _non_candle_stale
            )
            log_fn = self._log.info if _only_sparse_candles else self._log.warning
            log_fn(
                "Heartbeat %s | stale components: %s | "
                "last_activity=%ds ago | errors=%d",
                "SPARSE_FEEDS" if _only_sparse_candles else "DEGRADED",
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
