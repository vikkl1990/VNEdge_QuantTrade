"""Signal Journey — per-signal stage-level tracing (Phase 1).

Attaches a lightweight stage history to every signal dict as it flows
through the pipeline. On close, persists the full journey to a JSONL
file for post-mortem analysis.

Usage at each pipeline stage:
    from bot.signal_journey import SignalJourney
    SignalJourney.begin(sig_dict)
    SignalJourney.stamp(sig_dict, "strategy", passed=True, reason="structure_bounce")
    # ... later ...
    SignalJourney.stamp(sig_dict, "exit", passed=True, reason="trail_profit")
    SignalJourney.close(sig_dict)

All methods are best-effort: failures are swallowed so journey tracking
never blocks trading. Memory footprint: ~500 bytes per active signal.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

logger = logging.getLogger("bot.signal_journey")

_STORAGE_DIR = Path("storage")
_JOURNAL_FILE = _STORAGE_DIR / "signal_journeys.jsonl"
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB rotation threshold

# Internal key on signal dicts — underscore = not displayed
_KEY = "_journey"
_KEY_START = "_journey_start"


class StageResult(NamedTuple):
    """One pipeline stage outcome."""
    stage: str
    passed: bool
    reason: str
    latency_ms: float
    ts: float


class SignalJourney:
    """Static utility class — no instance state, no singleton."""

    @staticmethod
    def begin(sig_dict: dict) -> None:
        """Initialize journey on a signal dict. Idempotent."""
        try:
            if _KEY not in sig_dict:
                sig_dict[_KEY] = []
                sig_dict[_KEY_START] = time.time()
        except Exception:
            pass

    @staticmethod
    def stamp(sig_dict: dict, stage: str, *, passed: bool, reason: str = "ok") -> None:
        """Append a StageResult. Auto-begins if needed."""
        try:
            if not isinstance(sig_dict, dict):
                return
            # Auto-begin
            if _KEY not in sig_dict:
                sig_dict[_KEY] = []
                sig_dict[_KEY_START] = time.time()

            now = time.time()
            stages = sig_dict[_KEY]

            # Latency from previous stamp (or begin)
            if stages:
                prev_ts = stages[-1][4]  # StageResult.ts is index 4
            else:
                prev_ts = sig_dict.get(_KEY_START, now)
            latency_ms = (now - prev_ts) * 1000.0

            stages.append(StageResult(
                stage=stage,
                passed=passed,
                reason=str(reason)[:100],
                latency_ms=round(latency_ms, 2),
                ts=now,
            ))
        except Exception:
            pass

    @staticmethod
    def close(sig_dict: dict) -> None:
        """Persist completed journey to JSONL and remove from dict."""
        try:
            if not isinstance(sig_dict, dict):
                return
            stages = sig_dict.get(_KEY)
            if not stages:
                return
            # Already closed?
            if sig_dict.get("_journey_closed"):
                return
            sig_dict["_journey_closed"] = True

            start = sig_dict.get(_KEY_START, 0)
            total_ms = (time.time() - start) * 1000.0 if start else 0

            record = {
                "trade_id": sig_dict.get("trade_id", ""),
                "symbol": sig_dict.get("symbol", ""),
                "side": sig_dict.get("side", ""),
                "grade": sig_dict.get("grade", ""),
                "stages": [
                    {
                        "stage": s.stage,
                        "passed": s.passed,
                        "reason": s.reason,
                        "latency_ms": s.latency_ms,
                        "ts": s.ts,
                    }
                    for s in stages
                ],
                "stage_count": len(stages),
                "total_ms": round(total_ms, 2),
                "final_stage": stages[-1].stage if stages else "",
                "final_passed": stages[-1].passed if stages else False,
                "closed_at": time.time(),
            }

            # Rotate file if over threshold
            try:
                if _JOURNAL_FILE.exists() and _JOURNAL_FILE.stat().st_size > _MAX_FILE_SIZE:
                    rotated = _JOURNAL_FILE.with_suffix(".jsonl.1")
                    try:
                        if rotated.exists():
                            rotated.unlink()
                    except Exception:
                        pass
                    _JOURNAL_FILE.rename(rotated)
                    logger.info("Rotated signal_journeys.jsonl -> .1 (was >10MB)")
            except Exception:
                pass

            # Append to JSONL
            _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            with open(_JOURNAL_FILE, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")

            # Cleanup from signal dict
            sig_dict.pop(_KEY, None)
            sig_dict.pop(_KEY_START, None)
            sig_dict.pop("_journey_closed", None)

        except Exception as e:
            logger.debug("Journey close failed: %s", e)

    @staticmethod
    def get_journey(sig_dict: dict) -> List[StageResult]:
        """Read current journey from a signal dict."""
        try:
            return list(sig_dict.get(_KEY, []))
        except Exception:
            return []

    @classmethod
    def load_by_trade_id(cls, trade_id: str) -> Optional[Dict[str, Any]]:
        """Search JSONL for a specific trade_id. Returns full record or None."""
        try:
            if not _JOURNAL_FILE.exists():
                return None
            with open(_JOURNAL_FILE, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                chunk = min(size, 500_000)
                f.seek(max(0, size - chunk))
                lines = f.read().decode("utf-8", errors="replace").strip().split("\n")

            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("trade_id") == trade_id:
                        return rec
                except Exception:
                    continue
            return None
        except Exception as e:
            logger.debug("load_by_trade_id failed: %s", e)
            return None

    @classmethod
    def load_recent(cls, limit: int = 100) -> List[Dict[str, Any]]:
        """Load last N journeys from JSONL (tail read)."""
        try:
            if not _JOURNAL_FILE.exists():
                return []
            with open(_JOURNAL_FILE, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                chunk = min(size, 200_000)
                f.seek(max(0, size - chunk))
                lines = f.read().decode("utf-8", errors="replace").strip().split("\n")

            results = []
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except Exception:
                    continue
                if len(results) >= limit:
                    break
            results.reverse()
            return results
        except Exception as e:
            logger.debug("load_recent failed: %s", e)
            return []
