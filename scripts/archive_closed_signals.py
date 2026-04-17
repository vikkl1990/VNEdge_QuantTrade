#!/usr/bin/env python3
"""
Archive & trim storage/closed_signals.json
==========================================
The live closed_signals.json grows unbounded (10MB+ observed, will hit
OOM at several hundred MB over months). Signal tracker only loads the
latest N into memory but writes the full file every save → slow saves
+ eventual startup timeout.

This script:
  1. Reads storage/closed_signals.json
  2. Splits trades older than --keep-days into a dated JSONL archive
     (storage/archive/closed_YYYY_MM_DD.jsonl.gz — compressed)
  3. Rewrites closed_signals.json with ONLY the recent --keep-days
  4. Verifies integrity (round-trip JSON parse)
  5. Logs old/new sizes + trade counts

Safe to run while bot is live — file-write is atomic (tmp+rename), and
the bot's in-memory state is the source of truth for any trade closed
during the archive run.

Usage:
    python scripts/archive_closed_signals.py           # default 14-day retention
    python scripts/archive_closed_signals.py --keep-days 30 --dry-run
    python scripts/archive_closed_signals.py --retention 28
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def parse_ts(x):
    if not x:
        return None
    try:
        return datetime.fromisoformat(x.replace('Z', '+00:00'))
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    default_path = Path(__file__).resolve().parent.parent / "storage" / "closed_signals.json"
    archive_dir = Path(__file__).resolve().parent.parent / "storage" / "archive"
    ap.add_argument("--signals", default=str(default_path))
    ap.add_argument("--archive-dir", default=str(archive_dir))
    ap.add_argument("--keep-days", type=int, default=14,
                    help="Trades newer than this stay in closed_signals.json (default: 14)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    path = Path(args.signals)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 2

    size_before = path.stat().st_size
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        print(f"ERROR: expected JSON list, got {type(data).__name__}", file=sys.stderr)
        return 3

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.keep_days)

    recent, old = [], []
    for t in data:
        ts = parse_ts(t.get("exit_time")) or parse_ts(t.get("entry_time"))
        if ts is None:
            # Keep undated records in recent to avoid silent loss
            recent.append(t)
            continue
        (recent if ts >= cutoff else old).append(t)

    print(f"Total trades: {len(data):,}")
    print(f"  Recent (< {args.keep_days}d old): {len(recent):,}")
    print(f"  Archive candidates: {len(old):,}")
    print(f"  File size: {size_before / 1024 / 1024:.2f} MB")

    if not old:
        print("Nothing to archive. Exiting.")
        return 0

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        # Show what archive path would be created
        oldest_ts = min(
            (parse_ts(t.get("exit_time")) for t in old if parse_ts(t.get("exit_time"))),
            default=None,
        )
        latest_ts = max(
            (parse_ts(t.get("exit_time")) for t in old if parse_ts(t.get("exit_time"))),
            default=None,
        )
        if oldest_ts and latest_ts:
            print(f"Archive window: {oldest_ts.date()} to {latest_ts.date()}")
        return 0

    # Write archive (compressed JSONL)
    arc_dir = Path(args.archive_dir)
    arc_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    arc_path = arc_dir / f"closed_signals_archive_{stamp}.jsonl.gz"

    with gzip.open(arc_path, "wt") as gz:
        for t in old:
            gz.write(json.dumps(t, default=str) + "\n")
    arc_size = arc_path.stat().st_size
    print(f"\nArchive written: {arc_path} ({arc_size / 1024 / 1024:.2f} MB)")

    # Rewrite closed_signals.json atomically
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w") as fh:
        json.dump(recent, fh, indent=2, default=str)
    # Integrity check: parse the temp file
    with open(tmp_path) as fh:
        verify = json.load(fh)
    if len(verify) != len(recent):
        print(f"ERROR: integrity check failed ({len(verify)} vs {len(recent)})", file=sys.stderr)
        tmp_path.unlink()
        return 4

    os.replace(tmp_path, path)
    size_after = path.stat().st_size
    pct_saved = (1 - size_after / size_before) * 100 if size_before else 0
    print(f"Rewrote: {path} ({size_after / 1024 / 1024:.2f} MB, -{pct_saved:.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
