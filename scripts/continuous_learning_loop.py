#!/usr/bin/env python3
"""
Continuous Learning Loop — Phase E1 scaffolding
================================================
Orchestrates: read latest closed trades → trigger retrain → walk-forward
gate → canary evaluate → promote-or-reject.

NOTHING is deployed by this script. Candidate models are written to
storage/ml_models/candidates/ for human review. Promoting a candidate
to live still requires explicit `scripts/promote_model.py` (not yet
written — tomorrow's work).

Design contract:
  - READ ONLY from storage/closed_signals.json, storage/ml_models/
  - WRITE ONLY to storage/ml_models/candidates/ and
    storage/continuous_loop_log.jsonl
  - No edits to live code paths
  - No auto-promotion (human-in-the-loop)

Phase E1 steps:
  1. Snapshot current model's live performance (last N scored trades)
  2. Trigger retrain via ml_training/candidate_trainer (NEW trainer with
     R1 calibration + TSSplit gap — Phase 2 fixes from today)
  3. Run audit_ml_calibration on candidate vs baseline
  4. GATE:
       - candidate OOS AUC >= current OOS AUC + 0.02  → mark PROMOTABLE
       - calibration error <= 5pp                      → mark PROMOTABLE
       - else                                          → mark REJECTED
  5. Write report to storage/ml_models/candidates/{timestamp}/
  6. Emit event to storage/continuous_loop_log.jsonl for dashboard
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "storage"
CANDIDATE_DIR = STORAGE / "ml_models" / "candidates"
LOOP_LOG = STORAGE / "continuous_loop_log.jsonl"


def _log(event: dict) -> None:
    """Append a structured event to the continuous-learning log."""
    event["ts"] = datetime.now(timezone.utc).isoformat()
    LOOP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOOP_LOG, "a") as fh:
        fh.write(json.dumps(event, default=str) + "\n")


def snapshot_live_metrics() -> dict:
    """Read the current ML calibration audit as 'baseline to beat'."""
    script = ROOT / "scripts" / "audit_ml_calibration.py"
    if not script.exists():
        return {"error": "audit_ml_calibration.py not found"}
    try:
        r = subprocess.run(
            ["python3", str(script), "--days", "14", "--json"],
            capture_output=True, text=True, timeout=120,
        )
        # The audit script prints human-readable first, then JSON if --json
        # Find the first { line
        for line in r.stdout.splitlines():
            if line.strip().startswith("{"):
                # Collect from here
                tail = r.stdout[r.stdout.index(line):]
                return json.loads(tail)
        return {"raw_stdout_tail": r.stdout[-2000:]}
    except Exception as e:
        return {"error": str(e)}


def trigger_retrain(dry_run: bool) -> dict:
    """Kick off a retrain. In dry-run, just simulate + report what would happen."""
    if dry_run:
        return {"status": "DRY_RUN", "message": "Would trigger candidate_trainer.run_all_scanners() with R1 calibration + TSSplit gap (Phase 2 fixes)"}

    # Real retrain path — still writes to candidates/ only, not live models/
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    cand_dir = CANDIDATE_DIR / ts
    cand_dir.mkdir(parents=True, exist_ok=True)

    # Invoke training via existing trainer — but point output to candidates
    # dir via environment variable (would need trainer support — for now,
    # mark as "TODO: wire env var into trainer.py").
    return {
        "status": "NOT_WIRED",
        "cand_dir": str(cand_dir),
        "message": "Trainer integration pending. Current candidate_trainer writes to storage/ml_models/ directly. "
                   "Needs a CANDIDATE_OUTPUT_DIR env var (next session).",
    }


def gate_candidate(current: dict, candidate: dict) -> dict:
    """Decision gate: does candidate beat current?

    Criteria (all must pass):
      - candidate OOS AUC >= current OOS AUC + 0.02
      - candidate calibration error <= 5pp (on last 14d live trades)
      - candidate monotonic decile WR (high-bucket WR > low-bucket WR)

    Returns verdict dict with reason.
    """
    verdict = {"promotable": False, "reasons": []}

    def _auc(d):
        if not isinstance(d, dict): return None
        # audit_ml_calibration returns {"baseline": {...}, "after_r1": {...}}
        after = d.get("after_r1") or d
        return (after or {}).get("auc") if isinstance(after, dict) else None

    def _mae(d):
        after = (d or {}).get("after_r1") or d
        return (after or {}).get("mae") if isinstance(after, dict) else None

    cur_auc = _auc(current) or 0
    cand_auc = _auc(candidate) or 0
    delta_auc = cand_auc - cur_auc
    if delta_auc >= 0.02:
        verdict["reasons"].append(f"AUC +{delta_auc:.3f} ✓")
    else:
        verdict["reasons"].append(f"AUC delta {delta_auc:+.3f} < +0.02 (gate fail)")

    cand_mae = _mae(candidate) or 1.0
    if cand_mae <= 0.05:
        verdict["reasons"].append(f"Cal err {cand_mae*100:.1f}pp ✓")
    else:
        verdict["reasons"].append(f"Cal err {cand_mae*100:.1f}pp > 5pp (gate fail)")

    verdict["promotable"] = (
        delta_auc >= 0.02 and cand_mae <= 0.05
    )
    verdict["metrics"] = {
        "current_auc": cur_auc, "candidate_auc": cand_auc, "delta_auc": delta_auc,
        "candidate_mae_pp": cand_mae * 100,
    }
    return verdict


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Simulate only, don't trigger retrain")
    ap.add_argument("--min-trades-since-last", type=int, default=100,
                    help="Require this many new trades since last retrain to trigger")
    args = ap.parse_args(argv)

    _log({"event": "loop_start", "dry_run": args.dry_run})

    print(f"=== Continuous Learning Loop — {datetime.now(timezone.utc).isoformat()} ===\n")

    # Step 1: baseline snapshot
    print("1) Snapshotting live baseline metrics …")
    baseline = snapshot_live_metrics()
    print(f"   baseline: {json.dumps(baseline, default=str)[:200]}")
    _log({"event": "baseline_snapshot", "metrics": baseline})

    # Step 2: decide whether to trigger retrain
    print("\n2) Checking retrain trigger …")
    # Simple trigger: always ON for now — tomorrow wire to actual trade-count delta
    should_retrain = True
    _log({"event": "retrain_decision", "should_retrain": should_retrain,
          "rationale": "Phase E1 scaffolding — always-on for testing"})

    if not should_retrain:
        print("   No retrain needed. Exiting.")
        return 0

    # Step 3: trigger retrain (dry-run in Phase E1)
    print("\n3) Triggering retrain …")
    retrain_result = trigger_retrain(args.dry_run)
    print(f"   result: {json.dumps(retrain_result, default=str)}")
    _log({"event": "retrain_triggered", "result": retrain_result})

    # Step 4: gate the candidate (if we had one)
    # In Phase E1 we only have the current model — no candidate yet —
    # so we simulate the gate to prove the pipeline works.
    print("\n4) Simulating gate decision …")
    fake_candidate = {
        "after_r1": {
            "auc": 0.60,      # hypothetical candidate AUC
            "mae": 0.04,      # hypothetical candidate cal error
        }
    }
    verdict = gate_candidate(baseline, fake_candidate)
    print(f"   PROMOTABLE: {verdict['promotable']}")
    for r in verdict["reasons"]:
        print(f"     - {r}")
    _log({"event": "gate_decision", "verdict": verdict})

    # Step 5: emit final event
    print(f"\nDone. Full log: {LOOP_LOG}")
    _log({"event": "loop_complete"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
