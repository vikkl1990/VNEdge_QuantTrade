"""
Training Dataset — Unified ML-ready trade record schema.

Every trade (executed or skipped) gets recorded here with full features
for later Random Forest / XGBoost training.

Records are stored as JSONL (one JSON per line) for streaming reads.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DATASET_DIR = Path("storage/training_data")
DATASET_DIR.mkdir(parents=True, exist_ok=True)
DATASET_FILE = DATASET_DIR / "trades.jsonl"
MAX_FILE_SIZE_MB = 100


@dataclass
class TrainingRecord:
    """Unified schema for ML training data."""

    # Identity
    trade_id: str = ""
    timestamp: str = ""

    # Market context
    symbol: str = ""
    setup: str = ""
    side: str = ""
    session: str = ""
    regime: str = ""
    regime_stability: float = 0.0

    # Signal quality
    confidence: int = 0
    confidence_breakdown: Dict[str, float] = field(default_factory=dict)
    tier: str = ""
    grade: str = ""

    # Prices
    entry_price: float = 0.0
    stop_loss: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0

    # Execution
    actual_entry: float = 0.0
    slippage_pct: float = 0.0
    entry_delay_ms: float = 0.0

    # Position sizing
    leverage: int = 1
    margin_usd: float = 0.0
    position_usd: float = 0.0
    contracts: int = 0

    # Outcome (filled after trade closes)
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    r_multiple: float = 0.0
    mae_r: float = 0.0
    mfe_r: float = 0.0
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    breakeven_set: bool = False
    duration_sec: int = 0

    # Would-block flags (critical for ML)
    would_block_confidence: bool = False
    would_block_session: bool = False
    would_block_regime: bool = False
    would_block_drawdown: bool = False
    would_block_ai: bool = False
    would_block_cooldown: bool = False
    would_block_rr: bool = False
    would_block_liq: bool = False
    would_block_ev: bool = False

    # Was it actually executed or skipped?
    executed: bool = True
    skip_reason: str = ""

    # Setup features (raw indicators for ML)
    features: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class TrainingDataset:
    """Manages the ML training dataset — append-only JSONL storage."""

    def __init__(self, filepath: Path = DATASET_FILE):
        self._filepath = filepath
        self._count = 0
        # Count existing records
        if self._filepath.exists():
            try:
                with open(self._filepath) as f:
                    self._count = sum(1 for _ in f)
            except Exception:
                self._count = 0
        logger.info("TrainingDataset: %d existing records in %s", self._count, self._filepath.name)

    @property
    def record_count(self) -> int:
        return self._count

    def record_trade(self, record: TrainingRecord) -> None:
        """Append a trade record to the dataset."""
        try:
            # Auto-rotate if file too large
            if self._filepath.exists():
                size_mb = self._filepath.stat().st_size / (1024 * 1024)
                if size_mb > MAX_FILE_SIZE_MB:
                    rotated = self._filepath.with_suffix(f".{int(time.time())}.jsonl")
                    self._filepath.rename(rotated)
                    logger.info("Rotated training data to %s (%.1f MB)", rotated.name, size_mb)

            with open(self._filepath, "a") as f:
                f.write(record.to_json() + "\n")
            self._count += 1
        except Exception as e:
            logger.error("Failed to write training record: %s", e)

    def record_from_signal(
        self,
        signal: Dict[str, Any],
        *,
        would_blocks: Dict[str, bool] = None,
        features: Dict[str, float] = None,
        session: str = "",
        regime: str = "",
    ) -> TrainingRecord:
        """Create a TrainingRecord from a signal dict and save it."""
        meta = signal.get("metadata", {})
        tps = signal.get("take_profits", [0, 0, 0])

        wb = would_blocks or {}
        feat = features or {}

        record = TrainingRecord(
            trade_id=signal.get("trade_id", ""),
            timestamp=signal.get("timestamp", datetime.now(timezone.utc).isoformat()),
            symbol=signal.get("symbol", ""),
            setup=meta.get("setup_type", ""),
            side=signal.get("side", ""),
            session=session or meta.get("session", ""),
            regime=regime or meta.get("regime", ""),
            confidence=signal.get("confidence", 0),
            tier=meta.get("tier", ""),
            grade=signal.get("grade", ""),
            entry_price=signal.get("entry_price", 0),
            stop_loss=signal.get("stop_loss", 0),
            tp1=tps[0] if len(tps) > 0 else 0,
            tp2=tps[1] if len(tps) > 1 else 0,
            tp3=tps[2] if len(tps) > 2 else 0,
            leverage=meta.get("leverage", 1),
            margin_usd=meta.get("margin_usd", 50),
            position_usd=meta.get("position_usd", 0),
            would_block_confidence=wb.get("confidence", False),
            would_block_session=wb.get("session", False),
            would_block_regime=wb.get("regime", False),
            would_block_drawdown=wb.get("drawdown", False),
            would_block_ai=wb.get("ai", False),
            would_block_cooldown=wb.get("cooldown", meta.get("would_block_cooldown", False)),
            would_block_rr=wb.get("rr", meta.get("would_block_rr", False)),
            would_block_liq=wb.get("liq", meta.get("would_block_liq", False)),
            would_block_ev=wb.get("ev", False),
            executed=True,
            features=feat,
        )

        self.record_trade(record)
        return record

    def update_outcome(
        self,
        trade_id: str,
        *,
        exit_price: float = 0,
        exit_reason: str = "",
        pnl_pct: float = 0,
        pnl_usd: float = 0,
        r_multiple: float = 0,
        mae_r: float = 0,
        mfe_r: float = 0,
        tp1_hit: bool = False,
        tp2_hit: bool = False,
        tp3_hit: bool = False,
        breakeven_set: bool = False,
        duration_sec: int = 0,
    ) -> None:
        """Update the last matching record with trade outcome.

        Scans last 1000 lines for the trade_id and updates in place.
        """
        if not self._filepath.exists():
            return

        try:
            lines = []
            with open(self._filepath) as f:
                lines = f.readlines()

            # Find and update the last matching record
            updated = False
            for i in range(len(lines) - 1, max(0, len(lines) - 1000) - 1, -1):
                try:
                    record = json.loads(lines[i])
                    if record.get("trade_id") == trade_id:
                        record["exit_price"] = exit_price
                        record["exit_reason"] = exit_reason
                        record["pnl_pct"] = pnl_pct
                        record["pnl_usd"] = pnl_usd
                        record["r_multiple"] = r_multiple
                        record["mae_r"] = mae_r
                        record["mfe_r"] = mfe_r
                        record["tp1_hit"] = tp1_hit
                        record["tp2_hit"] = tp2_hit
                        record["tp3_hit"] = tp3_hit
                        record["breakeven_set"] = breakeven_set
                        record["duration_sec"] = duration_sec
                        lines[i] = json.dumps(record, default=str) + "\n"
                        updated = True
                        break
                except (json.JSONDecodeError, IndexError):
                    continue

            if updated:
                with open(self._filepath, "w") as f:
                    f.writelines(lines)
        except Exception as e:
            logger.error("Failed to update training record %s: %s", trade_id, e)

    def get_records(self, last_n: int = 500) -> List[Dict]:
        """Read last N records."""
        if not self._filepath.exists():
            return []

        records = []
        try:
            with open(self._filepath) as f:
                lines = f.readlines()
            for line in lines[-last_n:]:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.error("Failed to read training data: %s", e)

        return records

    def get_stats(self) -> Dict[str, Any]:
        """Get dataset statistics."""
        records = self.get_records(last_n=5000)
        if not records:
            return {"count": 0}

        executed = [r for r in records if r.get("executed", True)]
        with_outcome = [r for r in executed if r.get("exit_reason")]

        wins = [r for r in with_outcome if r.get("pnl_pct", 0) > 0]
        losses = [r for r in with_outcome if r.get("pnl_pct", 0) <= 0]

        # Would-block analysis
        wb_counts = {}
        for key in ["confidence", "session", "regime", "drawdown", "ai", "cooldown", "rr", "liq", "ev"]:
            field_name = f"would_block_{key}"
            wb_counts[key] = sum(1 for r in executed if r.get(field_name, False))

        return {
            "total_records": len(records),
            "executed": len(executed),
            "with_outcome": len(with_outcome),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(with_outcome) * 100 if with_outcome else 0,
            "would_block_counts": wb_counts,
            "avg_confidence": sum(r.get("confidence", 0) for r in executed) / len(executed) if executed else 0,
            "setups": list(set(r.get("setup", "") for r in executed)),
        }
