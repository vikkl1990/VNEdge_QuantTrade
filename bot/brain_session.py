"""
SessionManager — Daily/weekly performance tracking for VN Edge BotBrain.

Tracks trading sessions (daily rollover at 00:00 UTC), generates
end-of-day summaries, detects warm-up status after restarts, and
provides weekly aggregate reviews.

Phase 6 of BotBrain rollout. Zero risk — observation only.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.brain_memory import BrainMemory, DailySessionSummary

logger = logging.getLogger("bot.brain_session")


class SessionManager:
    """Tracks daily/weekly trading sessions and generates summaries."""

    def __init__(self, memory: BrainMemory):
        self._memory = memory
        self._current_date: str = self._utc_date()
        self._daily_trades: List[Dict[str, Any]] = []
        self._daily_regime_changes: int = 0
        self._daily_regimes: List[str] = []

        # Warm-up tracking
        self._trades_since_restart: int = 0
        self._warm_up_min_trades: int = 3
        self._restart_time: str = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _utc_date() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _utc_hour() -> int:
        return datetime.now(timezone.utc).hour

    # ══════════════════════════════════════════════════════════════
    # TRADE RECORDING
    # ══════════════════════════════════════════════════════════════

    def record_trade(
        self,
        setup: str,
        symbol: str,
        regime: str,
        is_win: bool,
        pnl_usd: float,
        r_mult: float,
        duration_sec: float,
        hour: int,
    ):
        """Record a closed trade for daily tracking."""
        self._daily_trades.append({
            "setup": setup,
            "symbol": symbol,
            "regime": regime,
            "is_win": is_win,
            "pnl_usd": pnl_usd,
            "r_mult": r_mult,
            "duration_sec": duration_sec,
            "hour": hour,
        })
        self._trades_since_restart += 1

    def record_regime_change(self, regime: str):
        """Track a regime change for daily summary."""
        self._daily_regime_changes += 1
        self._daily_regimes.append(regime)

    # ══════════════════════════════════════════════════════════════
    # DAILY ROLLOVER
    # ══════════════════════════════════════════════════════════════

    def check_daily_rollover(self) -> Optional[DailySessionSummary]:
        """Check if a new day has started. If so, finalize yesterday's summary.

        Called at the top of each scan cycle (from BotBrain.tick()).
        Returns the completed summary or None if same day.
        """
        today = self._utc_date()
        if today == self._current_date:
            return None

        # New day — finalize yesterday
        summary = self._finalize_daily(self._current_date)
        self._current_date = today
        self._daily_trades.clear()
        self._daily_regime_changes = 0
        self._daily_regimes.clear()

        if summary:
            self._memory.record_daily_summary(summary)
            self._memory.compact()  # daily compaction
            logger.info(
                "SESSION ROLLOVER %s: %d trades, %d wins, PnL=$%.2f, best=%s",
                summary.date, summary.total_trades, summary.wins,
                summary.total_pnl_usd, summary.best_scanner,
            )
        return summary

    def _finalize_daily(self, date: str) -> Optional[DailySessionSummary]:
        """Compute end-of-day summary from collected trades."""
        if not self._daily_trades:
            return None

        wins = sum(1 for t in self._daily_trades if t["is_win"])
        losses = len(self._daily_trades) - wins
        total_pnl = sum(t["pnl_usd"] for t in self._daily_trades)
        total_r = sum(t["r_mult"] for t in self._daily_trades)

        # Best/worst scanner by PnL
        scanner_pnl: Dict[str, float] = defaultdict(float)
        scanner_trades: Dict[str, int] = defaultdict(int)
        for t in self._daily_trades:
            scanner_pnl[t["setup"]] += t["pnl_usd"]
            scanner_trades[t["setup"]] += 1

        best_scanner = max(scanner_pnl, key=scanner_pnl.get) if scanner_pnl else ""
        worst_scanner = min(scanner_pnl, key=scanner_pnl.get) if scanner_pnl else ""

        # Best/worst hour by PnL
        hour_pnl: Dict[int, float] = defaultdict(float)
        for t in self._daily_trades:
            hour_pnl[t["hour"]] += t["pnl_usd"]
        best_hour = max(hour_pnl, key=hour_pnl.get) if hour_pnl else -1
        worst_hour = min(hour_pnl, key=hour_pnl.get) if hour_pnl else -1

        # Dominant regime
        regime_counts = Counter(self._daily_regimes)
        dominant = regime_counts.most_common(1)[0][0] if regime_counts else ""

        # Scanner breakdown
        breakdown = {}
        for setup in scanner_pnl:
            setup_trades = [t for t in self._daily_trades if t["setup"] == setup]
            setup_wins = sum(1 for t in setup_trades if t["is_win"])
            breakdown[setup] = {
                "trades": scanner_trades[setup],
                "wins": setup_wins,
                "wr": round(setup_wins / len(setup_trades) * 100, 1) if setup_trades else 0,
                "pnl": round(scanner_pnl[setup], 2),
            }

        return DailySessionSummary(
            date=date,
            total_trades=len(self._daily_trades),
            wins=wins,
            losses=losses,
            total_pnl_usd=round(total_pnl, 2),
            total_r=round(total_r, 3),
            dominant_regime=dominant,
            regime_changes=self._daily_regime_changes,
            best_scanner=best_scanner,
            worst_scanner=worst_scanner,
            best_hour=best_hour,
            worst_hour=worst_hour,
            scanner_breakdown=breakdown,
        )

    # ══════════════════════════════════════════════════════════════
    # QUERIES
    # ══════════════════════════════════════════════════════════════

    def is_warm(self) -> bool:
        """True if enough trades collected since last restart for reliable decisions."""
        return self._trades_since_restart >= self._warm_up_min_trades

    def get_today_stats(self) -> Dict[str, Any]:
        """Return running stats for today (for dashboard)."""
        if not self._daily_trades:
            return {"date": self._current_date, "trades": 0, "wins": 0, "pnl": 0.0}
        wins = sum(1 for t in self._daily_trades if t["is_win"])
        pnl = sum(t["pnl_usd"] for t in self._daily_trades)
        return {
            "date": self._current_date,
            "trades": len(self._daily_trades),
            "wins": wins,
            "losses": len(self._daily_trades) - wins,
            "wr": round(wins / len(self._daily_trades) * 100, 1),
            "pnl_usd": round(pnl, 2),
            "trades_since_restart": self._trades_since_restart,
            "is_warm": self.is_warm(),
        }

    def get_weekly_review(self) -> Dict[str, Any]:
        """Aggregate last 7 daily summaries into a weekly review."""
        summaries = sorted(self._memory._daily_summaries.values(), key=lambda s: s.date, reverse=True)[:7]
        if not summaries:
            return {"days": 0, "total_trades": 0, "total_pnl": 0.0}

        total_trades = sum(s.total_trades for s in summaries)
        total_wins = sum(s.wins for s in summaries)
        total_pnl = sum(s.total_pnl_usd for s in summaries)

        # Best/worst day
        best_day = max(summaries, key=lambda s: s.total_pnl_usd)
        worst_day = min(summaries, key=lambda s: s.total_pnl_usd)

        # Scanner frequency
        scanner_freq: Dict[str, int] = defaultdict(int)
        for s in summaries:
            for setup, info in s.scanner_breakdown.items():
                scanner_freq[setup] += info.get("trades", 0)

        return {
            "days": len(summaries),
            "total_trades": total_trades,
            "total_wins": total_wins,
            "total_losses": total_trades - total_wins,
            "wr": round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_daily_pnl": round(total_pnl / len(summaries), 2),
            "best_day": {"date": best_day.date, "pnl": best_day.total_pnl_usd},
            "worst_day": {"date": worst_day.date, "pnl": worst_day.total_pnl_usd},
            "scanner_frequency": dict(scanner_freq),
        }
