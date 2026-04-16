#!/usr/bin/env python3
"""
Backfill BotBrain daily_summaries from closed_signals.json
============================================================

BotBrain was added late (created 2026-04-14). It only has a rolling view
from that point forward, but the bot has 25+ days of paper trades in
`storage/closed_signals.json`. This script replays those closed trades
into DailySessionSummary objects and merges them into brain_state.json
so the dashboard can show the full history.

Safe:
  - Atomic write (temp file + rename)
  - Backup of brain_state.json written first
  - Preserves any existing daily_summaries entries by default
  - --overwrite flag to rebuild from scratch

Run:
    # On VM (live brain_state):
    python3 scripts/backfill_brain_sessions.py

    # Dry run (print what would be added):
    python3 scripts/backfill_brain_sessions.py --dry-run

    # Overwrite existing entries (recompute from closed_signals):
    python3 scripts/backfill_brain_sessions.py --overwrite
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.brain_memory import DailySessionSummary  # noqa: E402


def parse_ts(x):
    if not x:
        return None
    try:
        return datetime.fromisoformat(x.replace('Z', '+00:00'))
    except Exception:
        return None


def build_summary_for_date(date: str, trades: list) -> DailySessionSummary:
    """Replicate brain_session._finalize_daily on a list of closed trades."""
    wins = 0
    losses = 0
    total_pnl = 0.0
    total_r = 0.0
    scanner_pnl: dict = defaultdict(float)
    scanner_trades: dict = defaultdict(int)
    scanner_wins: dict = defaultdict(int)
    hour_pnl: dict = defaultdict(float)
    regimes: list = []

    for t in trades:
        pnl_usd = float(t.get('pnl_usd') or 0.0)
        pnl_pct = float(t.get('pnl_pct') or 0.0)
        r_mult = float(t.get('exit_r') or 0.0)
        is_win = pnl_pct > 0

        # Setup/scanner name — prefer explicit setup_type in metadata
        md = t.get('metadata') or {}
        setup = md.get('setup_type') or md.get('scanner') or t.get('scanner') or 'unknown'

        # Hour of ENTRY (matches brain_session.record_trade which uses entry hour)
        entry_ts = parse_ts(t.get('entry_time'))
        hour = entry_ts.hour if entry_ts else -1

        regime = md.get('regime') or t.get('regime') or ''

        if is_win:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl_usd
        total_r += r_mult
        scanner_pnl[setup] += pnl_usd
        scanner_trades[setup] += 1
        if is_win:
            scanner_wins[setup] += 1
        if hour >= 0:
            hour_pnl[hour] += pnl_usd
        if regime:
            regimes.append(regime)

    best_scanner = max(scanner_pnl, key=scanner_pnl.get) if scanner_pnl else ''
    worst_scanner = min(scanner_pnl, key=scanner_pnl.get) if scanner_pnl else ''
    best_hour = max(hour_pnl, key=hour_pnl.get) if hour_pnl else -1
    worst_hour = min(hour_pnl, key=hour_pnl.get) if hour_pnl else -1
    dominant = Counter(regimes).most_common(1)[0][0] if regimes else ''

    breakdown = {}
    for setup, n in scanner_trades.items():
        breakdown[setup] = {
            'trades': n,
            'wins': scanner_wins[setup],
            'wr': round(scanner_wins[setup] / n * 100, 1) if n else 0,
            'pnl': round(scanner_pnl[setup], 2),
        }

    # Count distinct regime transitions across the day
    regime_changes = 0
    last = None
    for r in regimes:
        if last is not None and r != last:
            regime_changes += 1
        last = r

    return DailySessionSummary(
        date=date,
        total_trades=len(trades),
        wins=wins,
        losses=losses,
        total_pnl_usd=round(total_pnl, 2),
        total_r=round(total_r, 3),
        dominant_regime=dominant,
        regime_changes=regime_changes,
        best_scanner=best_scanner,
        worst_scanner=worst_scanner,
        best_hour=best_hour,
        worst_hour=worst_hour,
        scanner_breakdown=breakdown,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--signals', default=str(PROJECT_ROOT / 'storage' / 'closed_signals.json'))
    ap.add_argument('--brain-state', default=str(PROJECT_ROOT / 'storage' / 'brain_state.json'))
    ap.add_argument('--dry-run', action='store_true', help='Print summary, do not write')
    ap.add_argument('--overwrite', action='store_true', help='Recompute existing dates (default: preserve)')
    ap.add_argument('--min-trades', type=int, default=1, help='Minimum trades per day to include')
    ap.add_argument('--group-by-entry', action='store_true',
                    help='Group by entry date (default: exit date — matches brain_session rollover semantics)')
    args = ap.parse_args(argv)

    signals_path = Path(args.signals)
    brain_path = Path(args.brain_state)
    if not signals_path.exists():
        print(f"ERROR: {signals_path} not found", file=sys.stderr)
        return 2
    if not brain_path.exists():
        print(f"ERROR: {brain_path} not found", file=sys.stderr)
        return 2

    print(f"Reading trades: {signals_path}")
    with open(signals_path) as fh:
        closed = json.load(fh)
    print(f"  total trades: {len(closed):,}")

    ts_field = 'entry_time' if args.group_by_entry else 'exit_time'
    print(f"  grouping by: {ts_field} (UTC calendar day)")

    # Group by date
    by_date: dict = defaultdict(list)
    skipped_no_ts = 0
    for t in closed:
        ts = parse_ts(t.get(ts_field))
        if ts is None:
            skipped_no_ts += 1
            continue
        date = ts.astimezone(timezone.utc).strftime('%Y-%m-%d')
        by_date[date].append(t)
    if skipped_no_ts:
        print(f"  skipped (no {ts_field}): {skipped_no_ts}")
    print(f"  unique dates: {len(by_date)}")

    # Read brain_state
    with open(brain_path) as fh:
        brain = json.load(fh)
    existing = brain.get('daily_summaries') or {}
    if isinstance(existing, list):
        print(f"WARN: daily_summaries is a list — converting to dict by date")
        existing = {s.get('date'): s for s in existing if isinstance(s, dict) and s.get('date')}
    print(f"  existing brain daily_summaries: {len(existing)} dates")

    # Build new summaries
    new_summaries = {}
    for date in sorted(by_date.keys()):
        if len(by_date[date]) < args.min_trades:
            continue
        if date in existing and not args.overwrite:
            continue
        summary = build_summary_for_date(date, by_date[date])
        new_summaries[date] = asdict(summary)

    # Merge
    merged = dict(existing)
    merged.update(new_summaries)
    # Keep most recent 90 (same cap as BotBrain)
    MAX = 90
    if len(merged) > MAX:
        keep = sorted(merged.keys())[-MAX:]
        merged = {d: merged[d] for d in keep}

    # Print plan
    print(f"\n=== BACKFILL PLAN ===")
    print(f"  New dates to add:       {len(new_summaries)}")
    print(f"  Existing dates kept:    {len(existing) - (len(existing & new_summaries.keys()) if args.overwrite else 0)}")
    print(f"  Total after merge:      {len(merged)}")
    if new_summaries:
        print(f"\n  First new: {min(new_summaries)}")
        print(f"  Last new:  {max(new_summaries)}")
    sample_n = min(5, len(new_summaries))
    if sample_n:
        print(f"\n  Sample (first {sample_n}):")
        print(f"  {'date':<12} {'trades':>6} {'WR':>6} {'PnL USD':>10} {'best_scanner':>20}")
        for date in sorted(new_summaries.keys())[:sample_n]:
            s = new_summaries[date]
            wr = (s['wins'] / s['total_trades'] * 100) if s['total_trades'] else 0
            print(f"  {date:<12} {s['total_trades']:>6} {wr:>5.1f}% {s['total_pnl_usd']:>+10.2f} {s['best_scanner']:>20}")

    if args.dry_run:
        print("\n--dry-run: no changes written")
        return 0

    # Backup + atomic write
    backup_path = brain_path.with_suffix(
        f".bak.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    )
    shutil.copy2(brain_path, backup_path)
    print(f"\n  backup: {backup_path}")

    brain['daily_summaries'] = merged
    tmp_path = brain_path.with_suffix('.tmp')
    with open(tmp_path, 'w') as fh:
        json.dump(brain, fh, indent=2, default=str)
    os.replace(tmp_path, brain_path)
    print(f"  wrote:  {brain_path}")
    print(f"\n  DONE. daily_summaries now has {len(merged)} dates.")
    print(f"  Note: the running bot has an in-memory copy. Daily summaries will be")
    print(f"  re-read from disk on next restart OR overwritten at next brain save.")
    print(f"  For immediate dashboard refresh without restart, the running bot will")
    print(f"  clobber this file when it next saves. Either restart the bot OR run")
    print(f"  this AFTER stopping the bot.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
