#!/usr/bin/env python3
"""Archive the paper-trading state and restart the paper account from scratch.

Run with the bot STOPPED (the tracker holds the ledger in memory and rewrites
it on every close). Everything is moved, nothing is deleted:

    storage/archive/paper_<stamp>/
        closed_signals.json            the ledger
        closed_signals_archive.jsonl   append-only copy of the ledger
        signal_stats.json              tracker stats snapshot
        scanner_weights.json           scanner grades derived from the ledger
        ml_live_feedback.jsonl         per-trade outcomes fed to ML calibration
        ml_feature_history.jsonl
        active_signals.json            (only if a position was open)
        REASON.txt

Usage:
    scripts/reset_paper_account.py --reason "phantom breakeven fills"
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "storage"
FILES = [
    "closed_signals.json",
    "closed_signals_archive.jsonl",
    "signal_stats.json",
    "scanner_weights.json",
    "ml_live_feedback.jsonl",
    "ml_feature_history.jsonl",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reason", required=True, help="why the account is being reset (kept with the archive)")
    ap.add_argument("--keep-open", action="store_true", help="leave active_signals.json in place (default: archive it too)")
    args = ap.parse_args()

    pid_file = ROOT / ".bot.pid"
    if pid_file.exists():
        try:
            import os
            os.kill(int(pid_file.read_text().strip()), 0)
            print("bot is running (pid file alive) — stop it first: kill -TERM $(cat .bot.pid)", file=sys.stderr)
            return 2
        except (ProcessLookupError, ValueError):
            pass

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = STORAGE / "archive" / f"paper_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)

    moved = []
    for name in FILES + ([] if args.keep_open else ["active_signals.json"]):
        src = STORAGE / name
        if src.exists():
            shutil.move(str(src), str(dest / name))
            moved.append(name)

    # fresh, empty state the tracker understands
    (STORAGE / "closed_signals.json").write_text("[]")
    (STORAGE / "active_signals.json").write_text("[]")
    (STORAGE / "ml_live_feedback.jsonl").write_text("")

    ledger = dest / "closed_signals.json"
    n = 0
    net = 0.0
    if ledger.exists():
        try:
            rows = json.loads(ledger.read_text())
            n = len(rows)
            net = sum(float(r.get("pnl_usd") or 0) for r in rows)
        except Exception:
            pass
    (dest / "REASON.txt").write_text(
        f"reset at {stamp}\nreason: {args.reason}\narchived trades: {n} (net ${net:+.2f})\nfiles: {', '.join(moved)}\n"
    )
    print(f"archived {n} trades (net ${net:+.2f}) and {len(moved)} files to {dest.relative_to(ROOT)}")
    print("paper account restarts at the configured initial_balance on next bot start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
