"""
BrainMemory — Unified persistent knowledge store for VN Edge.

The bot's long-term memory. Tracks:
- Setup x Regime x Symbol x Hour performance matrix (4D with wildcard fallback)
- Per-symbol regime history with transition predictions (Markov chain)
- Hourly performance heatmap (UTC)
- Daily session summaries (last 90 days)
- Parameter change history with performance impact

All data persists to storage/brain_state.json via atomic write-then-rename.
Thread-safe for asyncio (no locks needed — single event loop).

Phase 1: observation-only (zero risk). Phases 3+ use lookups to drive decisions.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("bot.brain_memory")

# ── Constants ──
MIN_SAMPLES = 5            # minimum trades before a cell is trusted
EMA_ALPHA = 0.12           # EMA smoothing for win rate (higher = faster adapt)
MAX_REGIME_HISTORY = 200   # per symbol
MAX_DAILY_SUMMARIES = 90   # days
MAX_PARAM_HISTORY = 50     # entries per param
COMPACT_MIN_TRADES = 2     # cells below this AND older than 30d get pruned
COMPACT_MAX_AGE_DAYS = 30


@dataclass
class PerformanceCell:
    """One cell in the setup x regime x symbol x hour matrix."""
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_r: float = 0.0
    ema_win_rate: float = 50.0
    avg_duration_sec: float = 0.0
    sample_count: int = 0
    last_updated: str = ""

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 50.0

    @property
    def avg_r(self) -> float:
        return (self.total_r / self.sample_count) if self.sample_count > 0 else 0.0

    @property
    def avg_pnl(self) -> float:
        return (self.total_pnl / self.sample_count) if self.sample_count > 0 else 0.0

    def record(self, is_win: bool, pnl: float, r_mult: float, duration_sec: float):
        """Record a new trade outcome."""
        if is_win:
            self.wins += 1
        else:
            self.losses += 1
        self.total_pnl += pnl
        self.total_r += r_mult
        self.sample_count += 1
        # EMA win rate update
        win_val = 100.0 if is_win else 0.0
        self.ema_win_rate = EMA_ALPHA * win_val + (1 - EMA_ALPHA) * self.ema_win_rate
        # Running avg duration
        if self.sample_count == 1:
            self.avg_duration_sec = duration_sec
        else:
            self.avg_duration_sec += (duration_sec - self.avg_duration_sec) / self.sample_count
        self.last_updated = datetime.now(timezone.utc).isoformat()


@dataclass
class DailySessionSummary:
    """End-of-day summary for one trading day."""
    date: str = ""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usd: float = 0.0
    total_r: float = 0.0
    dominant_regime: str = ""
    regime_changes: int = 0
    best_scanner: str = ""
    worst_scanner: str = ""
    best_hour: int = -1
    worst_hour: int = -1
    recommendations_acted: int = 0
    param_adjustments: List[Dict] = field(default_factory=list)
    scanner_breakdown: Dict[str, Dict] = field(default_factory=dict)


@dataclass
class RegimeSnapshot:
    """One regime observation for a symbol."""
    timestamp: str
    regime: str
    confidence: float
    duration_bars: int


class BrainMemory:
    """Unified persistent knowledge store — the bot's long-term memory.

    All lookups are in-memory dict accesses (<1ms). Saves are async-safe
    via atomic write-then-rename. Corrupt files are preserved and state
    resets to empty (graceful degradation).
    """

    def __init__(self, storage_dir: Optional[Path] = None):
        self._storage_dir = storage_dir or Path("storage")
        self._state_file = self._storage_dir / "brain_state.json"

        # ── Core: Setup x Regime x Symbol x Hour performance matrix ──
        # Key format: "setup:regime:symbol:hour" (hour = 0-23 or "*")
        # Wildcard levels: "setup:regime:symbol:*", "setup:regime:*:*", "setup:*:*:*"
        self._matrix: Dict[str, PerformanceCell] = {}

        # ── Regime History (per-symbol) ──
        # Tracks regime transitions for each symbol independently
        self._regime_history: Dict[str, Deque[RegimeSnapshot]] = {}

        # ── Regime Transition Matrix ──
        # Key: "from_regime:to_regime" → {count, avg_duration_bars, total_duration}
        self._regime_transitions: Dict[str, Dict[str, Any]] = {}

        # ── Hourly Performance (UTC, aggregated across all setups) ──
        self._hourly_perf: Dict[int, PerformanceCell] = {}

        # ── Daily Session Summaries ──
        self._daily_summaries: Dict[str, DailySessionSummary] = {}

        # ── Parameter History ──
        # Key: param_name → list of {value, metric, timestamp, sample_size}
        self._param_history: Dict[str, List[Dict]] = {}

        # ── Metadata ──
        self._version: int = 1
        self._last_compaction: str = ""
        self._total_observations: int = 0
        self._created_at: str = datetime.now(timezone.utc).isoformat()

        # Load existing state
        self._load()
        logger.info(
            "BrainMemory loaded: %d matrix cells, %d observations, %d daily summaries",
            len(self._matrix), self._total_observations, len(self._daily_summaries),
        )

    # ══════════════════════════════════════════════════════════════
    # RECORD METHODS (write path)
    # ══════════════════════════════════════════════════════════════

    def record_outcome(
        self,
        setup: str,
        regime: str,
        symbol: str,
        hour: int,
        is_win: bool,
        pnl: float,
        r_mult: float,
        duration_sec: float,
    ):
        """Record a trade outcome into the performance matrix.

        Writes to 4 aggregation levels:
        1. Exact:   setup:regime:symbol:hour
        2. Hour-*:  setup:regime:symbol:*
        3. Sym-*:   setup:regime:*:*
        4. Global:  setup:*:*:*
        """
        now = datetime.now(timezone.utc).isoformat()
        keys = [
            f"{setup}:{regime}:{symbol}:{hour}",
            f"{setup}:{regime}:{symbol}:*",
            f"{setup}:{regime}:*:*",
            f"{setup}:*:*:*",
        ]
        for key in keys:
            cell = self._matrix.get(key)
            if cell is None:
                cell = PerformanceCell()
                self._matrix[key] = cell
            cell.record(is_win, pnl, r_mult, duration_sec)

        # Also record into hourly aggregate
        h_cell = self._hourly_perf.get(hour)
        if h_cell is None:
            h_cell = PerformanceCell()
            self._hourly_perf[hour] = h_cell
        h_cell.record(is_win, pnl, r_mult, duration_sec)

        self._total_observations += 1

    def record_regime(self, symbol: str, regime: str, confidence: float, duration_bars: int = 1):
        """Record a regime observation for a symbol."""
        if symbol not in self._regime_history:
            self._regime_history[symbol] = deque(maxlen=MAX_REGIME_HISTORY)

        snap = RegimeSnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            regime=regime,
            confidence=confidence,
            duration_bars=duration_bars,
        )
        self._regime_history[symbol].append(snap)

    def record_regime_transition(self, symbol: str, from_regime: str, to_regime: str, duration_bars: int):
        """Record a regime transition into the Markov transition matrix."""
        key = f"{from_regime}:{to_regime}"
        entry = self._regime_transitions.get(key)
        if entry is None:
            entry = {"count": 0, "total_duration": 0, "avg_duration_bars": 0}
            self._regime_transitions[key] = entry
        entry["count"] += 1
        entry["total_duration"] += duration_bars
        entry["avg_duration_bars"] = entry["total_duration"] / entry["count"]

    def record_daily_summary(self, summary: DailySessionSummary):
        """Store a daily session summary."""
        self._daily_summaries[summary.date] = summary
        # Prune old
        if len(self._daily_summaries) > MAX_DAILY_SUMMARIES:
            sorted_dates = sorted(self._daily_summaries.keys())
            for d in sorted_dates[: len(sorted_dates) - MAX_DAILY_SUMMARIES]:
                del self._daily_summaries[d]

    def record_param_change(self, param_name: str, value: float, metric: float, sample_size: int):
        """Track a parameter change and its performance context."""
        if param_name not in self._param_history:
            self._param_history[param_name] = []
        entry = {
            "value": value,
            "metric": metric,
            "sample_size": sample_size,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._param_history[param_name].append(entry)
        # Prune old
        if len(self._param_history[param_name]) > MAX_PARAM_HISTORY:
            self._param_history[param_name] = self._param_history[param_name][-MAX_PARAM_HISTORY:]

    # ══════════════════════════════════════════════════════════════
    # LOOKUP METHODS (read path — all <1ms, in-memory dict access)
    # ══════════════════════════════════════════════════════════════

    def get_performance(
        self,
        setup: str,
        regime: str = "*",
        symbol: str = "*",
        hour: str = "*",
        min_samples: int = MIN_SAMPLES,
    ) -> Optional[PerformanceCell]:
        """Look up the most specific cell with enough samples.

        Falls back to less specific keys if the exact match has too few trades:
        1. setup:regime:symbol:hour  (exact)
        2. setup:regime:symbol:*     (any hour)
        3. setup:regime:*:*          (any symbol/hour)
        4. setup:*:*:*               (setup-only)
        """
        candidates = [
            f"{setup}:{regime}:{symbol}:{hour}",
            f"{setup}:{regime}:{symbol}:*",
            f"{setup}:{regime}:*:*",
            f"{setup}:*:*:*",
        ]
        for key in candidates:
            cell = self._matrix.get(key)
            if cell and cell.sample_count >= min_samples:
                return cell
        return None

    def get_setup_regime_wr(self, setup: str, regime: str, min_samples: int = MIN_SAMPLES) -> Optional[float]:
        """Get win rate for a specific setup in a specific regime.

        Returns None if insufficient data. This is the key method for
        Phase 3 scanner gating: "should we run this scanner in this regime?"
        """
        cell = self._matrix.get(f"{setup}:{regime}:*:*")
        if cell and cell.sample_count >= min_samples:
            return cell.win_rate
        return None

    def get_bad_hours(self, min_trades: int = 10, max_wr: float = 35.0) -> Set[int]:
        """Return UTC hours where performance is statistically poor.

        Used by Phase 3 to raise confidence_floor during bad hours.
        """
        bad = set()
        for hour, cell in self._hourly_perf.items():
            if cell.sample_count >= min_trades and cell.win_rate < max_wr:
                bad.add(hour)
        return bad

    def get_best_hours(self, min_trades: int = 10, min_wr: float = 65.0) -> Set[int]:
        """Return UTC hours where performance is strong."""
        good = set()
        for hour, cell in self._hourly_perf.items():
            if cell.sample_count >= min_trades and cell.win_rate >= min_wr:
                good.add(hour)
        return good

    def predict_next_regime(self, symbol: str) -> Tuple[str, float]:
        """Predict the most likely next regime for a symbol.

        Uses the Markov transition matrix: given the current regime,
        what regime has historically followed it most often?

        Returns (predicted_regime, probability) or ("unknown", 0.0).
        """
        history = self._regime_history.get(symbol)
        if not history or len(history) < 2:
            return ("unknown", 0.0)

        current = history[-1].regime
        # Find all transitions from current regime
        candidates: Dict[str, int] = {}
        total = 0
        for key, entry in self._regime_transitions.items():
            from_r, to_r = key.split(":", 1)
            if from_r == current:
                candidates[to_r] = entry["count"]
                total += entry["count"]

        if not candidates or total == 0:
            return ("unknown", 0.0)

        best = max(candidates, key=candidates.get)
        prob = candidates[best] / total
        return (best, prob)

    def get_regime_avg_duration(self, regime: str) -> float:
        """Get average duration (in bars) a regime typically lasts.

        Used by Phase 3 to detect "regime is about to change" when
        current duration exceeds the average.
        """
        durations = []
        for key, entry in self._regime_transitions.items():
            from_r, _ = key.split(":", 1)
            if from_r == regime:
                durations.extend([entry["avg_duration_bars"]] * entry["count"])
        return sum(durations) / len(durations) if durations else 0.0

    def get_current_regime(self, symbol: str) -> Optional[str]:
        """Get the most recently recorded regime for a symbol."""
        history = self._regime_history.get(symbol)
        if history and len(history) > 0:
            return history[-1].regime
        return None

    def get_matrix_summary(self) -> Dict[str, Any]:
        """Return a summary of the performance matrix for dashboard display.

        Groups by setup×regime for the heatmap, sorted by sample count.
        """
        summary = {}
        for key, cell in self._matrix.items():
            parts = key.split(":")
            if len(parts) != 4:
                continue
            setup, regime, sym, hour = parts
            # Only include the setup:regime:*:* level for the heatmap
            if sym == "*" and hour == "*" and regime != "*":
                heatmap_key = f"{setup}:{regime}"
                summary[heatmap_key] = {
                    "wins": cell.wins,
                    "losses": cell.losses,
                    "win_rate": round(cell.win_rate, 1),
                    "ema_wr": round(cell.ema_win_rate, 1),
                    "avg_r": round(cell.avg_r, 3),
                    "total_pnl": round(cell.total_pnl, 2),
                    "sample_count": cell.sample_count,
                    "avg_duration_sec": round(cell.avg_duration_sec, 0),
                }
        return summary

    def get_hourly_heatmap(self) -> Dict[int, Dict[str, Any]]:
        """Return hourly performance for dashboard heatmap."""
        return {
            hour: {
                "wins": cell.wins,
                "losses": cell.losses,
                "win_rate": round(cell.win_rate, 1),
                "avg_r": round(cell.avg_r, 3),
                "total_pnl": round(cell.total_pnl, 2),
                "sample_count": cell.sample_count,
            }
            for hour, cell in sorted(self._hourly_perf.items())
        }

    # ══════════════════════════════════════════════════════════════
    # PERSISTENCE (atomic write-then-rename, crash-safe)
    # ══════════════════════════════════════════════════════════════

    def save(self):
        """Persist full state to disk via atomic write."""
        try:
            state = {
                "version": self._version,
                "created_at": self._created_at,
                "last_compaction": self._last_compaction,
                "total_observations": self._total_observations,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "performance_matrix": {
                    k: asdict(v) for k, v in self._matrix.items()
                },
                "regime_history": {
                    sym: [asdict(s) for s in snapshots]
                    for sym, snapshots in self._regime_history.items()
                },
                "regime_transitions": dict(self._regime_transitions),
                "hourly_perf": {
                    str(h): asdict(c) for h, c in self._hourly_perf.items()
                },
                "daily_summaries": {
                    d: asdict(s) for d, s in self._daily_summaries.items()
                },
                "param_history": dict(self._param_history),
            }

            self._storage_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._storage_dir), suffix=".tmp", prefix="brain_"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(state, f, indent=1, default=str)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(self._state_file))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            logger.debug(
                "BrainMemory saved: %d cells, %d observations",
                len(self._matrix), self._total_observations,
            )
        except Exception as e:
            logger.error("BrainMemory save failed: %s", e)

    def _load(self):
        """Load state from disk. On corruption, start fresh (graceful degradation)."""
        if not self._state_file.exists():
            logger.info("BrainMemory: no state file, starting fresh")
            return

        try:
            with open(self._state_file) as f:
                state = json.load(f)

            self._version = state.get("version", 1)
            self._created_at = state.get("created_at", self._created_at)
            self._last_compaction = state.get("last_compaction", "")
            self._total_observations = state.get("total_observations", 0)

            # Restore performance matrix
            for key, cell_dict in state.get("performance_matrix", {}).items():
                self._matrix[key] = PerformanceCell(**cell_dict)

            # Restore regime history
            for sym, snapshots in state.get("regime_history", {}).items():
                dq: Deque[RegimeSnapshot] = deque(maxlen=MAX_REGIME_HISTORY)
                for s in snapshots:
                    dq.append(RegimeSnapshot(**s))
                self._regime_history[sym] = dq

            # Restore regime transitions
            self._regime_transitions = state.get("regime_transitions", {})

            # Restore hourly perf
            for h_str, cell_dict in state.get("hourly_perf", {}).items():
                self._hourly_perf[int(h_str)] = PerformanceCell(**cell_dict)

            # Restore daily summaries
            for d, s_dict in state.get("daily_summaries", {}).items():
                self._daily_summaries[d] = DailySessionSummary(**s_dict)

            # Restore param history
            self._param_history = state.get("param_history", {})

        except Exception as e:
            logger.warning("BrainMemory load failed (starting fresh): %s", e)
            # Preserve corrupt file for debugging
            try:
                corrupt_path = self._state_file.with_suffix(f".corrupted.{int(time.time())}.json")
                os.rename(str(self._state_file), str(corrupt_path))
                logger.warning("Corrupt brain state preserved: %s", corrupt_path)
            except OSError:
                pass

    def compact(self):
        """Prune old/low-value data to keep memory bounded.

        Called daily by SessionManager. Removes:
        - Matrix cells with <COMPACT_MIN_TRADES that are >COMPACT_MAX_AGE_DAYS old
        - Trims regime history deques (already bounded by maxlen)
        - Trims daily summaries to MAX_DAILY_SUMMARIES
        - Trims param history to MAX_PARAM_HISTORY per param
        """
        now = datetime.now(timezone.utc)
        pruned = 0

        keys_to_remove = []
        for key, cell in self._matrix.items():
            if cell.sample_count < COMPACT_MIN_TRADES and cell.last_updated:
                try:
                    last = datetime.fromisoformat(cell.last_updated)
                    if (now - last).days > COMPACT_MAX_AGE_DAYS:
                        keys_to_remove.append(key)
                except (ValueError, TypeError):
                    pass
        for key in keys_to_remove:
            del self._matrix[key]
            pruned += 1

        # Trim daily summaries
        if len(self._daily_summaries) > MAX_DAILY_SUMMARIES:
            sorted_dates = sorted(self._daily_summaries.keys())
            for d in sorted_dates[: len(sorted_dates) - MAX_DAILY_SUMMARIES]:
                del self._daily_summaries[d]

        # Trim param history
        for param, history in self._param_history.items():
            if len(history) > MAX_PARAM_HISTORY:
                self._param_history[param] = history[-MAX_PARAM_HISTORY:]

        self._last_compaction = now.isoformat()
        if pruned > 0:
            logger.info("BrainMemory compacted: pruned %d cells, %d remaining", pruned, len(self._matrix))
