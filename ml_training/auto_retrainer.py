"""Phase 4.9 — Auto-Retrain Trigger.

Watches the Phase 4.6 live-calibration endpoint and triggers targeted retrains
when a (scanner, family) bucket's verdict becomes `DRIFTING` or `NO_EDGE` on
enough live samples.

Design:
  - Pure subprocess — spawns `python3 -m ml_training.run_trainer --train ...`
    with the family's symbol set. Does NOT touch CandidateTrainer directly
    (keeps the daemon stateless and the trainer isolated).
  - Cooldown per bucket so a single flapping bucket can't DDoS the retrainer.
  - State file: storage/auto_retrain_state.json with per-bucket `last_retrain`,
    trigger bucket, verdict at trigger time.
  - Runs as a long-lived loop OR one-shot mode for cron use.

Usage:
  python -m ml_training.auto_retrainer --once             # single cycle, exit
  python -m ml_training.auto_retrainer --dry-run --once   # plan only, no spawn
  python -m ml_training.auto_retrainer                    # long-running daemon

Config via flags:
  --check-interval  polling period seconds (default 3600 = 1h)
  --min-n           minimum trades in bucket before evaluating (default 50)
  --cooldown-hours  per-bucket cooldown after a trigger (default 6)
  --dashboard-url   base URL for /api/ml/live-calibration (default localhost:8081)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("ml_training.auto_retrainer")

# ──────────────────────────────────────────────────────────────────────
# Paths & config defaults
# ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "storage" / "auto_retrain_state.json"
RETRAIN_LOG = PROJECT_ROOT / "logs" / "ml_retrain.log"
DEFAULT_DASHBOARD = "http://127.0.0.1:8091"  # local ML Lab (scripts/ml_lab.sh)
DEFAULT_TRAINER_PORT = 8085

# Symbols we'll retrain per family. Keeps symmetry with Phase 4.5
# run_pair_family groupings. Updated here should be kept in sync with the
# PAIR_FAMILIES constant in candidate_trainer.py.
def _family_symbols() -> Dict[str, str]:
    """Single source: candidate_trainer.PAIR_FAMILIES (what serving resolves)."""
    try:
        from ml_training.candidate_trainer import PAIR_FAMILIES
        return {fam: ",".join(syms) for fam, syms in PAIR_FAMILIES.items()}
    except Exception:  # pragma: no cover - import failure means nothing to train
        return {}


FAMILY_SYMBOLS: Dict[str, str] = _family_symbols()

# Verdicts that trigger a retrain. DRIFTING means the model predicts something
# different from what actually happens (cal_err > 0.15). NO_EDGE means the
# model shouldn't be deployed at all.
TRIGGER_VERDICTS = {"DRIFTING", "NO_EDGE"}


class AutoRetrainer:
    """Polls /api/ml/live-calibration and spawns retrain subprocesses when needed."""

    def __init__(
        self,
        dashboard_url: str = DEFAULT_DASHBOARD,
        check_interval_sec: int = 3600,
        min_n_trigger: int = 50,
        cooldown_hours: int = 6,
        dry_run: bool = False,
        trainer_port: int = DEFAULT_TRAINER_PORT,
    ):
        self.dashboard_url = dashboard_url.rstrip("/")
        self.check_interval = check_interval_sec
        self.min_n = min_n_trigger
        self.cooldown = timedelta(hours=cooldown_hours)
        self.dry_run = dry_run
        self.trainer_port = trainer_port
        self._state: Dict[str, Dict] = self._load_state()

    # ── state persistence ────────────────────────────────────────────
    def _load_state(self) -> Dict[str, Dict]:
        if not STATE_FILE.exists():
            return {}
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            logger.warning("failed to load state file: %s — starting fresh", e)
            return {}

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self._state, indent=2, default=str))
        except Exception as e:
            logger.error("failed to save state file: %s", e)

    def _is_cooldown(self, key: str) -> bool:
        entry = self._state.get(key)
        if not entry or "last_retrain" not in entry:
            return False
        try:
            last = datetime.fromisoformat(entry["last_retrain"])
        except Exception:
            return False
        elapsed = datetime.now(timezone.utc) - last
        return elapsed < self.cooldown

    def _record_trigger(self, key: str, bucket: Dict) -> None:
        self._state[key] = {
            "last_retrain": datetime.now(timezone.utc).isoformat(),
            "trigger_n": bucket.get("n"),
            "trigger_verdict": bucket.get("verdict"),
            "trigger_cal_err": bucket.get("calibration_error"),
            "trigger_wr": bucket.get("realized_mfe_wr"),
            "trigger_pred": bucket.get("avg_ml_probability"),
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_state()

    # ── calibration fetch ────────────────────────────────────────────
    def _fetch_calibration(self) -> Optional[Dict]:
        """Pull /api/ml/live-calibration. Returns parsed JSON or None on error."""
        import requests  # lazy import — not needed in dry-run
        url = f"{self.dashboard_url}/api/ml/live-calibration"
        params = {"last_n": 500, "min_n": self.min_n}
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                logger.warning("calibration endpoint returned %s", resp.status_code)
                return None
            return resp.json()
        except Exception as e:
            logger.warning("failed to fetch calibration: %s", e)
            return None

    # ── identify retrainable buckets ─────────────────────────────────
    def _identify_triggers(self, calibration: Dict) -> List[Tuple[str, Dict]]:
        """Return list of (key, bucket) that need a retrain."""
        triggers: List[Tuple[str, Dict]] = []
        for b in calibration.get("buckets", []):
            if not isinstance(b, dict):
                continue
            n = int(b.get("n", 0))
            if n < self.min_n:
                continue
            verdict = str(b.get("verdict", ""))
            if verdict not in TRIGGER_VERDICTS:
                continue
            scope = b.get("scope", "scanner")
            family = b.get("family", "-")
            scanner = b.get("scanner", "?")
            key = f"{scope}__{family}__{scanner}"
            if self._is_cooldown(key):
                logger.info(
                    "cooldown active for %s (last=%s); skipping",
                    key, self._state.get(key, {}).get("last_retrain"),
                )
                continue
            triggers.append((key, b))
        return triggers

    # ── spawn retrain subprocess ─────────────────────────────────────
    def _spawn_retrain(self, family: str) -> Optional[int]:
        """Spawn a retrain for the given family. Returns PID or None on failure.

        Note: v1 retrains ALL scanners for the family (not just the failing one)
        because run_trainer doesn't support per-scanner filtering. More surgical
        variants can be added later via CandidateTrainer API calls.
        """
        symbols = FAMILY_SYMBOLS.get(family)
        if not symbols:
            logger.warning("no symbol set for family '%s' — skipping", family)
            return None

        cmd = [
            sys.executable, "-m", "ml_training.run_trainer",
            "--train",
            "--no-dashboard",  # the serving dashboard already owns the port
            "--symbols", symbols,
            "--timeframes", "5m,15m,1h,4h",
            "--port", str(self.trainer_port),
        ]

        if self.dry_run:
            logger.warning("DRY-RUN: would spawn: %s (cwd=%s)", " ".join(cmd), PROJECT_ROOT)
            return -1  # sentinel

        try:
            RETRAIN_LOG.parent.mkdir(parents=True, exist_ok=True)
            logf = open(RETRAIN_LOG, "a")
            logf.write(f"\n=== {datetime.now(timezone.utc).isoformat()} spawning retrain for {family} ({symbols}) ===\n")
            logf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # detach
            )
            logger.warning(
                "AUTO_RETRAIN: spawned pid=%d family=%s symbols=%s port=%d",
                proc.pid, family, symbols, self.trainer_port,
            )
            return proc.pid
        except Exception as e:
            logger.error("failed to spawn retrain for %s: %s", family, e)
            return None

    # ── single check+act cycle ───────────────────────────────────────
    async def check_and_retrain(self) -> Dict:
        """Run one cycle. Returns a report dict for logging/testing."""
        report = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "calibration_ok": False,
            "buckets_evaluated": 0,
            "triggers": [],
            "spawned": [],
            "skipped_cooldown": [],
        }

        calibration = self._fetch_calibration()
        if calibration is None:
            logger.warning("no calibration data — skipping cycle")
            return report
        report["calibration_ok"] = True
        report["buckets_evaluated"] = len(calibration.get("buckets", []))

        triggers = self._identify_triggers(calibration)
        if not triggers:
            logger.info(
                "cycle clean — %d buckets, %d triggers",
                report["buckets_evaluated"], 0,
            )
            return report

        # Deduplicate by family (avoid spawning the same retrain twice in one cycle)
        families_to_retrain: Dict[str, Tuple[str, Dict]] = {}
        for key, bucket in triggers:
            family = bucket.get("family", "-")
            if family == "-" or family == "other":
                logger.info(
                    "skipping non-family bucket %s (family=%s)", key, family,
                )
                continue
            # Keep the worst bucket per family as the "trigger reason"
            existing = families_to_retrain.get(family)
            if existing is None or bucket.get("calibration_error", 0) > existing[1].get("calibration_error", 0):
                families_to_retrain[family] = (key, bucket)

        for family, (key, bucket) in families_to_retrain.items():
            logger.warning(
                "TRIGGER: family=%s scanner=%s n=%d verdict=%s cal_err=%.3f pred=%.3f wr=%.3f",
                family, bucket.get("scanner"), bucket.get("n"),
                bucket.get("verdict"), bucket.get("calibration_error", 0),
                bucket.get("avg_ml_probability", 0), bucket.get("realized_mfe_wr", 0),
            )
            report["triggers"].append({
                "key": key,
                "family": family,
                "scanner": bucket.get("scanner"),
                "verdict": bucket.get("verdict"),
                "n": bucket.get("n"),
                "cal_err": bucket.get("calibration_error"),
            })
            pid = self._spawn_retrain(family)
            if pid is not None:
                self._record_trigger(key, bucket)
                report["spawned"].append({"family": family, "pid": pid})

        return report

    # ── long-running daemon loop ─────────────────────────────────────
    async def run_loop(self) -> None:
        logger.info(
            "auto_retrainer started — dashboard=%s interval=%ds min_n=%d cooldown=%dh dry_run=%s",
            self.dashboard_url, self.check_interval, self.min_n,
            self.cooldown.total_seconds() / 3600, self.dry_run,
        )
        while True:
            try:
                await self.check_and_retrain()
            except Exception:
                logger.exception("check cycle failed")
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                logger.info("auto_retrainer cancelled")
                return


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 4.9 auto-retrain trigger")
    p.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD,
                   help=f"base URL for /api/ml/live-calibration (default {DEFAULT_DASHBOARD})")
    p.add_argument("--check-interval", type=int, default=3600,
                   help="polling period in seconds (default 3600 = 1h)")
    p.add_argument("--min-n", type=int, default=50,
                   help="minimum trades in bucket before evaluating (default 50)")
    p.add_argument("--cooldown-hours", type=int, default=6,
                   help="per-bucket cooldown after a trigger (default 6)")
    p.add_argument("--trainer-port", type=int, default=DEFAULT_TRAINER_PORT,
                   help="port for the spawned run_trainer process")
    p.add_argument("--dry-run", action="store_true",
                   help="log what would be retrained, don't spawn subprocess")
    p.add_argument("--once", action="store_true",
                   help="run one cycle and exit (for cron use)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    retrainer = AutoRetrainer(
        dashboard_url=args.dashboard_url,
        check_interval_sec=args.check_interval,
        min_n_trigger=args.min_n,
        cooldown_hours=args.cooldown_hours,
        dry_run=args.dry_run,
        trainer_port=args.trainer_port,
    )
    if args.once:
        report = asyncio.run(retrainer.check_and_retrain())
        print(json.dumps(report, indent=2, default=str))
    else:
        try:
            asyncio.run(retrainer.run_loop())
        except KeyboardInterrupt:
            logger.info("interrupted — exiting")


if __name__ == "__main__":
    main()
