"""Supervisor — 60-second async watchdog for the trading pipeline (Phase 1).

Runs as an asyncio.Task in the orchestrator. Every 60 seconds it checks:
1. Zombie positions (real_trades vs actual Delta positions)
2. Agent heartbeat staleness
3. Paper/real R-multiple drift
4. Stuck trades past max_age
5. WAL consistency (opened events without matching close)

Read-only: never touches exit logic, never places orders.
On anomaly: logs CRITICAL + records exec event for dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bot.supervisor")


class Supervisor:
    """60-second async watchdog task."""

    INTERVAL = 60  # seconds between checks
    STARTUP_GRACE = 120  # skip first 120s after boot (let systems stabilize)
    MAX_TRADE_AGE_SEC = 4 * 3600  # 4h hard backstop
    HEARTBEAT_WARN_SEC = 30  # agent stale warning threshold
    HEARTBEAT_CRIT_SEC = 120  # agent dead threshold
    R_DRIFT_THRESHOLD = 0.5  # paper/real R divergence alert

    def __init__(
        self,
        *,
        signal_tracker=None,
        real_manager=None,
        heartbeat=None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self._signal_tracker = signal_tracker
        self._real_manager = real_manager
        self._heartbeat = heartbeat
        self._log = log or logger
        self._task: Optional[asyncio.Task] = None
        self._started_at: float = time.time()
        self._last_run: float = 0
        self._anomaly_count: int = 0
        self._recent_alerts: List[Dict[str, Any]] = []  # last 20 alerts

    async def start(self) -> None:
        """Create the supervisor asyncio.Task."""
        if self._task is not None:
            return
        self._started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="supervisor")
        self._log.info("SUPERVISOR: started (interval=%ds, grace=%ds)",
                       self.INTERVAL, self.STARTUP_GRACE)

    async def stop(self) -> None:
        """Cancel the task."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            self._log.info("SUPERVISOR: stopped")

    async def _loop(self) -> None:
        """Main 60s cycle."""
        try:
            # Wait for startup grace period
            await asyncio.sleep(self.STARTUP_GRACE)
            self._log.info("SUPERVISOR: grace period over, starting checks")

            while True:
                try:
                    await self._run_checks()
                except Exception as e:
                    self._log.debug("SUPERVISOR: check cycle failed: %s", e)
                await asyncio.sleep(self.INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _run_checks(self) -> None:
        """Execute all checks and aggregate anomalies."""
        self._last_run = time.time()
        anomalies: List[Dict[str, Any]] = []

        # 1. Agent heartbeats
        anomalies.extend(self._check_agent_heartbeats())

        # 2. Stuck trades
        anomalies.extend(self._check_stuck_trades())

        # 3. WAL consistency
        anomalies.extend(self._check_wal_consistency())

        # 4. Paper/real drift (only if both trackers available)
        anomalies.extend(self._check_paper_real_drift())

        # 5. Orphan position reconciliation (#2/#8)
        anomalies.extend(self._check_orphan_positions())

        # 6. External uptime ping (#3)
        self._ping_uptime_monitor()

        # 7. Stale feed auto-restart
        await self._check_stale_feed_restart()

        # Record anomalies
        if anomalies:
            self._anomaly_count += len(anomalies)
            for a in anomalies:
                self._log.critical("SUPERVISOR ALERT: [%s] %s", a.get("check", "?"), a.get("detail", "?"))
                self._recent_alerts.append(a)
            # Trim alerts to last 20
            self._recent_alerts = self._recent_alerts[-20:]
            # Record to pipeline metrics
            try:
                from bot import pipeline_metrics as _pm
                for a in anomalies:
                    _pm.record_exec_event("supervisor_alert")
            except Exception:
                pass
        else:
            self._log.debug("SUPERVISOR: all checks passed (%d total anomalies since start)",
                           self._anomaly_count)

    def _check_agent_heartbeats(self) -> List[Dict[str, Any]]:
        """Check pipeline_metrics heartbeats for staleness."""
        alerts = []
        try:
            from bot import pipeline_metrics as _pm
            now = time.time()
            for comp, ts in _pm._heartbeats.items():
                age = now - ts
                if age > self.HEARTBEAT_CRIT_SEC:
                    alerts.append({
                        "check": "agent_heartbeat",
                        "severity": "critical",
                        "detail": f"{comp} DEAD — last seen {age:.0f}s ago (threshold={self.HEARTBEAT_CRIT_SEC}s)",
                        "component": comp,
                        "age_sec": round(age, 1),
                    })
                elif age > self.HEARTBEAT_WARN_SEC:
                    alerts.append({
                        "check": "agent_heartbeat",
                        "severity": "warning",
                        "detail": f"{comp} STALE — last seen {age:.0f}s ago",
                        "component": comp,
                        "age_sec": round(age, 1),
                    })
        except Exception:
            pass
        return alerts

    def _check_stuck_trades(self) -> List[Dict[str, Any]]:
        """Any active trades older than MAX_TRADE_AGE_SEC."""
        alerts = []
        now = time.time()
        try:
            # Check paper trades
            if self._signal_tracker:
                for tid, ts in list(getattr(self._signal_tracker, '_active', {}).items()):
                    opened = 0
                    try:
                        from datetime import datetime, timezone
                        et = getattr(ts, 'entry_time', '')
                        if isinstance(et, str) and et:
                            opened = datetime.fromisoformat(et.replace('Z', '+00:00')).timestamp()
                    except Exception:
                        pass
                    if opened > 0 and (now - opened) > self.MAX_TRADE_AGE_SEC:
                        age_h = (now - opened) / 3600
                        alerts.append({
                            "check": "stuck_trade",
                            "severity": "warning",
                            "detail": f"Paper {getattr(ts, 'symbol', '?')} {tid[:12]} stuck for {age_h:.1f}h (max={self.MAX_TRADE_AGE_SEC/3600:.0f}h)",
                            "trade_id": tid,
                            "age_hours": round(age_h, 1),
                        })

            # Check real trades
            if self._real_manager:
                for tid, t in list(getattr(self._real_manager, 'real_trades', {}).items()):
                    opened = getattr(t, 'opened_at', 0) or 0
                    if opened > 0 and (now - opened) > self.MAX_TRADE_AGE_SEC:
                        age_h = (now - opened) / 3600
                        alerts.append({
                            "check": "stuck_trade",
                            "severity": "critical",
                            "detail": f"REAL {getattr(t, 'symbol', '?')} {tid[:12]} stuck for {age_h:.1f}h (max={self.MAX_TRADE_AGE_SEC/3600:.0f}h)",
                            "trade_id": tid,
                            "age_hours": round(age_h, 1),
                        })
        except Exception:
            pass
        return alerts

    def _check_wal_consistency(self) -> List[Dict[str, Any]]:
        """Check WAL for opened events without matching close.

        Ignores pre-link garbage rows where trade_id is literally "pending"
        — these come from a historical code path that wrote the WAL row at
        order-send time, before the exchange response provided a real id.
        The supervisor should not CRITICAL-alert on these forever, so we
        treat them as structural noise (filter out before orphan check).
        """
        alerts = []
        try:
            wal = Path("storage/real_trades.wal.jsonl")
            if not wal.exists():
                return []
            events = {}
            junk_rows = 0
            with open(wal) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        tid = rec.get("trade_id", "")
                        ev = rec.get("event", "")
                        # Filter pre-link garbage: trade_id=="pending" is a
                        # pre-confirm placeholder that never got rewritten.
                        if not tid or tid == "pending":
                            junk_rows += 1
                            continue
                        events[tid] = ev
                    except Exception:
                        continue

            # Find opens without closes that aren't in current real_trades
            tracked_ids = set()
            if self._real_manager:
                tracked_ids = set(getattr(self._real_manager, 'real_trades', {}).keys())

            orphans = [tid for tid, ev in events.items()
                       if ev == "opened" and tid not in tracked_ids]

            if orphans:
                alerts.append({
                    "check": "wal_consistency",
                    "severity": "warning",
                    "detail": f"{len(orphans)} WAL orphan(s): {orphans[:3]}",
                    "orphan_count": len(orphans),
                })
            # One-time informational log if we filtered junk — helps catch
            # unexpected garbage quantity but never alerts the user.
            if junk_rows > 0 and not getattr(self, "_wal_junk_logged", False):
                self._log.info(
                    "SUPERVISOR: filtered %d pre-link 'pending' WAL row(s) (structural, not an alert)",
                    junk_rows,
                )
                self._wal_junk_logged = True
        except Exception:
            pass
        return alerts

    def _check_paper_real_drift(self) -> List[Dict[str, Any]]:
        """Compare paper/real P&L for mirrored trades."""
        alerts = []
        try:
            if not self._real_manager or not self._signal_tracker:
                return []

            p2r = getattr(self._real_manager, 'paper_to_real', {})
            active_paper = getattr(self._signal_tracker, '_active', {})
            real_trades = getattr(self._real_manager, 'real_trades', {})

            for paper_id, real_id in p2r.items():
                paper_ts = active_paper.get(paper_id)
                real_t = real_trades.get(real_id)
                if not paper_ts or not real_t:
                    continue

                paper_r = getattr(paper_ts, 'mfe_r', 0) or 0
                real_r = getattr(real_t, 'peak_mfe_r', 0) or 0
                drift = abs(paper_r - real_r)

                if drift > self.R_DRIFT_THRESHOLD:
                    alerts.append({
                        "check": "paper_real_drift",
                        "severity": "warning",
                        "detail": f"{getattr(paper_ts, 'symbol', '?')} paper_R={paper_r:.2f} real_R={real_r:.2f} drift={drift:.2f}R",
                        "symbol": getattr(paper_ts, 'symbol', '?'),
                        "paper_r": round(paper_r, 2),
                        "real_r": round(real_r, 2),
                        "drift_r": round(drift, 2),
                    })
        except Exception:
            pass
        return alerts

    STALE_FEED_RESTART_SEC = 300  # 5 minutes of stale feeds → auto-restart
    STALE_FEED_MAX_RESTARTS = 3  # max restarts per hour to prevent restart loop

    async def _check_stale_feed_restart(self) -> None:
        """Auto-restart the bot if ALL candle feeds are stale for >5 minutes.

        The 2026-04-12 incident: Delta India WS dropped, REST fallback failed,
        bot sat with zero candle data for 2h 9m while appearing "active (running)".
        This check detects the condition and triggers systemctl restart.

        Safety: max 3 restarts per hour to prevent infinite restart loops.
        Only triggers if the heartbeat system reports ALL feeds stale — a single
        stale symbol (e.g., DOT on weekends) won't trigger restart.
        """
        try:
            from bot import pipeline_metrics as _pm

            # Check if candle_close heartbeat is stale
            now = time.time()
            candle_ts = _pm._heartbeats.get("candle_close", 0)
            stale_sec = now - candle_ts if candle_ts > 0 else 0

            # Also check if ANY individual symbol has recent activity
            any_recent = False
            for comp, ts in _pm._heartbeats.items():
                if comp.startswith("candle_close:") and (now - ts) < self.STALE_FEED_RESTART_SEC:
                    any_recent = True
                    break

            if stale_sec < self.STALE_FEED_RESTART_SEC or any_recent:
                # Reset consecutive stale counter on recovery
                if hasattr(self, '_stale_restart_count_reset_at'):
                    if now - self._stale_restart_count_reset_at > 3600:
                        self._stale_restart_count = 0
                        self._stale_restart_count_reset_at = now
                return

            # ALL feeds stale for >5 min — consider restart
            restart_count = getattr(self, '_stale_restart_count', 0)
            if not hasattr(self, '_stale_restart_count_reset_at'):
                self._stale_restart_count_reset_at = now

            if restart_count >= self.STALE_FEED_MAX_RESTARTS:
                self._log.critical(
                    "STALE FEED: ALL feeds dead for %.0fs but already restarted %d times this hour — "
                    "NOT restarting (possible exchange outage, manual intervention needed)",
                    stale_sec, restart_count,
                )
                return

            self._log.critical(
                "STALE FEED AUTO-RESTART: ALL candle feeds dead for %.0fs (>%ds threshold) — "
                "triggering systemctl restart (attempt %d/%d this hour)",
                stale_sec, self.STALE_FEED_RESTART_SEC,
                restart_count + 1, self.STALE_FEED_MAX_RESTARTS,
            )

            self._stale_restart_count = restart_count + 1

            # Trigger restart via subprocess (non-blocking)
            import subprocess
            subprocess.Popen(
                ["sudo", "systemctl", "restart", "cryptobot"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._log.debug("Stale feed restart check failed: %s", e)

    def _check_orphan_positions(self) -> List[Dict[str, Any]]:
        """Architect review #2/#8: Periodic reconciliation — bot state vs exchange.

        Fetches open positions from Delta exchange and compares to bot's
        real_trades dict. If exchange has a position the bot doesn't know
        about, alert as orphan. If bot thinks a position is open but
        exchange says it's closed, alert as ghost.

        Read-only: never closes positions. Just alerts for manual review.
        """
        alerts = []
        try:
            if not self._real_manager:
                return []
            # Only check every 5th cycle (~5 min) to avoid rate limits
            _cycle = getattr(self, '_recon_cycle', 0)
            self._recon_cycle = _cycle + 1
            if _cycle % 5 != 0:
                return []

            delta = getattr(self._real_manager, '_delta_live', None)
            if not delta:
                return []

            # Fetch exchange positions
            try:
                exchange_positions = {}
                for sym in ("BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"):
                    try:
                        pos = delta.get_position(sym)
                        if pos and abs(float(pos.get("size", 0) or 0)) > 0:
                            exchange_positions[sym] = pos
                    except Exception:
                        pass

                bot_symbols = set()
                for tid, t in list(getattr(self._real_manager, 'real_trades', {}).items()):
                    bot_symbols.add(getattr(t, 'symbol', ''))

                # Orphan: on exchange but not in bot
                for sym, pos in exchange_positions.items():
                    if sym not in bot_symbols:
                        size = float(pos.get("size", 0) or 0)
                        alerts.append({
                            "check": "orphan_position",
                            "severity": "critical",
                            "detail": f"ORPHAN on exchange: {sym} size={size} — bot has no record!",
                            "symbol": sym,
                        })

                # Ghost: in bot but not on exchange (only if bot has >0 real trades)
                if len(getattr(self._real_manager, 'real_trades', {})) > 0 and not exchange_positions:
                    for sym in bot_symbols:
                        if sym and sym not in exchange_positions:
                            alerts.append({
                                "check": "ghost_position",
                                "severity": "warning",
                                "detail": f"GHOST in bot: {sym} tracked but not on exchange (may have been closed externally)",
                                "symbol": sym,
                            })
            except Exception as e:
                self._log.debug("Reconciliation fetch failed: %s", e)
        except Exception:
            pass
        return alerts

    def _ping_uptime_monitor(self) -> None:
        """Architect review #3: External uptime heartbeat.

        Pings an external monitoring URL every supervisor cycle (60s).
        If the bot crashes, the monitor detects missed pings and alerts.
        Uses a simple HTTP GET to healthchecks.io or similar service.
        """
        try:
            _url = getattr(self, '_uptime_monitor_url', None)
            if not _url:
                # Default: log-only (no external URL configured)
                # User can set via: supervisor._uptime_monitor_url = "https://hc-ping.com/UUID"
                return
            import urllib.request
            urllib.request.urlopen(_url, timeout=5)
        except Exception:
            pass  # Never let uptime ping failure affect the bot

    @property
    def status(self) -> Dict[str, Any]:
        """Summary for dashboard: last_run, anomaly_count, alerts."""
        return {
            "running": self._task is not None and not self._task.done(),
            "last_run": self._last_run,
            "last_run_ago_sec": round(time.time() - self._last_run, 1) if self._last_run else None,
            "anomaly_count": self._anomaly_count,
            "recent_alerts": self._recent_alerts[-5:],  # last 5 for dashboard
            "uptime_sec": round(time.time() - self._started_at, 0),
        }
