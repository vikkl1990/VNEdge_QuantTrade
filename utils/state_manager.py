"""
Persistent state management for the crypto trading bot.

Saves and loads bot state (open positions, active signals, runtime metadata)
as JSON files so the bot can recover gracefully after restarts or crashes.

Thread-safe: all public methods acquire a lock before touching the
filesystem or in-memory state.

Usage::

    from utils.state_manager import StateManager

    sm = StateManager()
    sm.save_position("BTC/USDT", position_dict)
    positions = sm.load_positions()
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.constants import (
    BOT_STATE_FILE,
    POSITIONS_FILE,
    SIGNALS_FILE,
    STATE_DIR,
)

logger = logging.getLogger(__name__)


class StateManager:
    """
    JSON-file-backed state persistence.

    Parameters
    ----------
    state_dir:
        Directory where state files are stored.  Created on first write
        if it does not exist.
    """

    def __init__(self, state_dir: Optional[str] = None) -> None:
        self._dir = Path(state_dir) if state_dir else Path(STATE_DIR)
        self._lock = threading.Lock()

        # In-memory caches (authoritative between saves)
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._signals: Dict[str, Dict[str, Any]] = {}
        self._bot_state: Dict[str, Any] = {}

        # Bootstrap from disk
        self._ensure_dir()
        self._load_all()

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def save_position(self, symbol: str, position: Dict[str, Any]) -> None:
        """Upsert a position for *symbol* and persist to disk."""
        with self._lock:
            position["updated_at"] = self._now_iso()
            self._positions[symbol] = position
            self._write(POSITIONS_FILE, self._positions)
        logger.debug("Saved position for %s", symbol)

    def remove_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Remove *symbol* from open positions.  Returns the removed entry or ``None``."""
        with self._lock:
            removed = self._positions.pop(symbol, None)
            if removed is not None:
                self._write(POSITIONS_FILE, self._positions)
        if removed:
            logger.debug("Removed position for %s", symbol)
        return removed

    def load_positions(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of all open positions."""
        with self._lock:
            return dict(self._positions)

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the position for *symbol*, or ``None``."""
        with self._lock:
            return self._positions.get(symbol)

    def clear_positions(self) -> None:
        """Remove all open positions."""
        with self._lock:
            self._positions.clear()
            self._write(POSITIONS_FILE, self._positions)
        logger.info("Cleared all positions")

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def save_signal(self, signal_id: str, signal: Dict[str, Any]) -> None:
        """Upsert an active signal and persist."""
        with self._lock:
            signal["updated_at"] = self._now_iso()
            self._signals[signal_id] = signal
            self._write(SIGNALS_FILE, self._signals)
        logger.debug("Saved signal %s", signal_id)

    def remove_signal(self, signal_id: str) -> Optional[Dict[str, Any]]:
        """Remove a signal by ID.  Returns the removed entry or ``None``."""
        with self._lock:
            removed = self._signals.pop(signal_id, None)
            if removed is not None:
                self._write(SIGNALS_FILE, self._signals)
        if removed:
            logger.debug("Removed signal %s", signal_id)
        return removed

    def load_signals(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of all active signals."""
        with self._lock:
            return dict(self._signals)

    def get_signal(self, signal_id: str) -> Optional[Dict[str, Any]]:
        """Return the signal for *signal_id*, or ``None``."""
        with self._lock:
            return self._signals.get(signal_id)

    def clear_signals(self) -> None:
        """Remove all active signals."""
        with self._lock:
            self._signals.clear()
            self._write(SIGNALS_FILE, self._signals)
        logger.info("Cleared all signals")

    # ------------------------------------------------------------------
    # Bot state (generic key/value metadata)
    # ------------------------------------------------------------------

    def save_bot_state(self, state: Dict[str, Any]) -> None:
        """Overwrite the entire bot state dict and persist."""
        with self._lock:
            state["updated_at"] = self._now_iso()
            self._bot_state = state
            self._write(BOT_STATE_FILE, self._bot_state)
        logger.debug("Saved bot state")

    def update_bot_state(self, updates: Dict[str, Any]) -> None:
        """Merge *updates* into the current bot state and persist."""
        with self._lock:
            self._bot_state.update(updates)
            self._bot_state["updated_at"] = self._now_iso()
            self._write(BOT_STATE_FILE, self._bot_state)

    def load_bot_state(self) -> Dict[str, Any]:
        """Return a copy of the bot state."""
        with self._lock:
            return dict(self._bot_state)

    def get_bot_state_value(self, key: str, default: Any = None) -> Any:
        """Return a single value from bot state."""
        with self._lock:
            return self._bot_state.get(key, default)

    def clear_bot_state(self) -> None:
        """Reset bot state to empty."""
        with self._lock:
            self._bot_state.clear()
            self._write(BOT_STATE_FILE, self._bot_state)
        logger.info("Cleared bot state")

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def save_all(self) -> None:
        """Persist every cache to disk in a single locked section."""
        with self._lock:
            ts = self._now_iso()
            self._positions.setdefault("_meta", {})["saved_at"] = ts  # type: ignore[union-attr]
            self._signals.setdefault("_meta", {})["saved_at"] = ts  # type: ignore[union-attr]
            self._bot_state["saved_at"] = ts
            self._write(POSITIONS_FILE, self._positions)
            self._write(SIGNALS_FILE, self._signals)
            self._write(BOT_STATE_FILE, self._bot_state)
        logger.info("Persisted all state to disk")

    def clear_all(self) -> None:
        """Remove all state from memory and disk."""
        with self._lock:
            self._positions.clear()
            self._signals.clear()
            self._bot_state.clear()
            self._write(POSITIONS_FILE, self._positions)
            self._write(SIGNALS_FILE, self._signals)
            self._write(BOT_STATE_FILE, self._bot_state)
        logger.info("Cleared all state")

    def create_backup(self, suffix: Optional[str] = None) -> Path:
        """
        Copy all state files into a timestamped backup directory.

        Returns the path to the backup directory.
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        tag = f"_{suffix}" if suffix else ""
        backup_dir = self._dir / f"backup_{ts}{tag}"
        with self._lock:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for fname in (POSITIONS_FILE, SIGNALS_FILE, BOT_STATE_FILE):
                src = self._dir / fname
                if src.exists():
                    shutil.copy2(src, backup_dir / fname)
        logger.info("State backup created at %s", backup_dir)
        return backup_dir

    def restore_backup(self, backup_dir: str | Path) -> None:
        """Restore state from a previous backup directory."""
        backup_path = Path(backup_dir)
        if not backup_path.is_dir():
            raise FileNotFoundError(f"Backup directory not found: {backup_dir}")
        with self._lock:
            for fname in (POSITIONS_FILE, SIGNALS_FILE, BOT_STATE_FILE):
                src = backup_path / fname
                if src.exists():
                    shutil.copy2(src, self._dir / fname)
            self._load_all_unlocked()
        logger.info("Restored state from %s", backup_dir)

    # ------------------------------------------------------------------
    # Summary / inspection
    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """Return a lightweight summary of the current state."""
        with self._lock:
            return {
                "open_positions": len(
                    {k: v for k, v in self._positions.items() if k != "_meta"}
                ),
                "active_signals": len(
                    {k: v for k, v in self._signals.items() if k != "_meta"}
                ),
                "bot_state_keys": list(self._bot_state.keys()),
                "state_dir": str(self._dir),
            }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def _load_all(self) -> None:
        with self._lock:
            self._load_all_unlocked()

    def _load_all_unlocked(self) -> None:
        """Load every state file from disk (caller must hold lock)."""
        self._positions = self._read(POSITIONS_FILE)
        self._signals = self._read(SIGNALS_FILE)
        self._bot_state = self._read(BOT_STATE_FILE)
        logger.info(
            "Loaded state: %d positions, %d signals",
            len({k for k in self._positions if k != "_meta"}),
            len({k for k in self._signals if k != "_meta"}),
        )

    def _read(self, filename: str) -> Dict[str, Any]:
        """Read a JSON file and return its contents as a dict."""
        path = self._dir / filename
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                logger.warning(
                    "State file %s does not contain a JSON object; resetting.", path,
                )
                return {}
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read state file %s: %s", path, exc)
            # Attempt to preserve the corrupted file for inspection
            corrupted = path.with_suffix(f".corrupted.{int(time.time())}.json")
            try:
                shutil.copy2(path, corrupted)
                logger.info("Corrupted state file copied to %s", corrupted)
            except OSError:
                pass
            return {}

    def _write(self, filename: str, data: Dict[str, Any]) -> None:
        """Atomically write *data* as JSON (caller must hold lock)."""
        path = self._dir / filename
        tmp_path = path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str)
                fh.flush()
            tmp_path.replace(path)
        except OSError as exc:
            logger.error("Failed to write state file %s: %s", path, exc)
            # Clean up the temp file if it still exists
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
