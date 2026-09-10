"""
Safety utilities: audit trail, backup rotation, stale price check.
"""
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_STORAGE = Path(__file__).resolve().parent.parent / "storage"
_AUDIT_FILE = _STORAGE / "audit_trail.jsonl"
_BACKUP_DIR = _STORAGE / "backups"


# ──────────────────────────────────────────────────────────
# Audit Trail — append-only immutable trade ledger
# ──────────────────────────────────────────────────────────

def audit_log(event_type: str, data: Dict[str, Any]) -> None:
    """Append an immutable audit entry. Never overwrites."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **data,
    }
    try:
        with open(_AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.error("Audit log failed: %s", e)


# ──────────────────────────────────────────────────────────
# Backup Rotation — keep last N copies of critical files
# ──────────────────────────────────────────────────────────

def backup_state_files(keep: int = 10) -> None:
    """Backup critical state files with rotation."""
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    critical_files = [
        _STORAGE / "signal_stats.json",
        _STORAGE / "active_signals.json",
        _STORAGE / "closed_signals.json",
        _STORAGE / "real_trading_state.json",
        _STORAGE / "ml_live_feedback.jsonl",
    ]

    for src in critical_files:
        if src.exists():
            dst = _BACKUP_DIR / f"{src.name}.{ts}.bak"
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                logger.warning("Backup failed for %s: %s", src.name, e)

    # Rotate: keep only last N backups per file
    for pattern_base in [f.name for f in critical_files]:
        backups = sorted(_BACKUP_DIR.glob(f"{pattern_base}.*.bak"))
        for old in backups[:-keep]:
            try:
                old.unlink()
            except Exception:
                pass

    logger.info("State backup complete (%d files, keep=%d)", len(critical_files), keep)


# ──────────────────────────────────────────────────────────
# Stale Price Check
# ──────────────────────────────────────────────────────────

_STALE_THRESHOLD_SEC = 30  # 30 seconds

def check_price_freshness(signal: Dict[str, Any]) -> tuple:
    """Check if signal data is fresh enough to trade on.

    Returns (is_fresh, age_seconds).
    """
    ts = signal.get("timestamp")
    # A missing, malformed or unsupported timestamp is NOT evidence of
    # freshness. Fail closed: treat it as stale so it cannot pass a safety
    # gate by accident (previously every bad value returned (True, 0)).
    if not ts:
        return False, float("inf")

    try:
        if isinstance(ts, str):
            signal_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        elif isinstance(ts, (int, float)):
            signal_time = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            return False, float("inf")
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)

        age = (datetime.now(timezone.utc) - signal_time).total_seconds()
        return age < _STALE_THRESHOLD_SEC, age
    except Exception:
        return False, float("inf")
