#!/usr/bin/env python3
"""Move trades that were priced on frozen market data out of the paper ledger.

Rule (objective, documented with the archive):
  invalid if slippage_bps > max_entry_slip_bps (the signal was priced off a
  frame that had stopped updating; a live order would never have filled
  there), OR highest_price == lowest_price == fill_price (no price ever
  reached the tracker during the trade).

Run with the bot STOPPED. Rows are moved to
storage/archive/quarantine_<stamp>.json and removed from closed_signals.json
and ml_live_feedback.jsonl. signal_stats.json is deleted so it rebuilds.

Usage: scripts/quarantine_trades.py --reason "feed freeze 2026-09-11" [--max-slip-bps 30] [--dry-run]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "storage"


def is_invalid(t: dict, max_slip: float) -> str:
    slip = float(t.get("slippage_bps") or 0)
    if slip > max_slip:
        return f"entry slippage {slip:.0f}bp > {max_slip:.0f}bp cap (signal priced on a frozen frame)"
    hi, lo, fp = t.get("highest_price"), t.get("lowest_price"), t.get("fill_price")
    if hi and lo and fp and float(hi) == float(lo) == float(fp):
        return "no price update reached the tracker during the trade (high == low == fill)"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reason", required=True)
    ap.add_argument("--max-slip-bps", type=float, default=30.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    pid = ROOT / ".bot.pid"
    if pid.exists() and not a.dry_run:
        try:
            os.kill(int(pid.read_text().strip()), 0)
            print("bot is running — stop it first", file=sys.stderr)
            return 2
        except (ProcessLookupError, ValueError):
            pass

    ledger = STORAGE / "closed_signals.json"
    rows = json.loads(ledger.read_text()) if ledger.exists() else []
    keep, out = [], []
    for t in rows:
        why = is_invalid(t, a.max_slip_bps)
        if why:
            t = dict(t); t["quarantine_reason"] = why; out.append(t)
        else:
            keep.append(t)
    for t in out:
        print(f"  remove {t['exit_time'][:16]} {t['symbol']} {t['side']} pnl {t.get('pnl_usd'):+} — {t['quarantine_reason']}")
    print(f"{len(out)} invalid, {len(keep)} kept")
    if a.dry_run or not out:
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    arch = STORAGE / "archive" / f"quarantine_{stamp}.json"
    arch.parent.mkdir(parents=True, exist_ok=True)
    arch.write_text(json.dumps({"reason": a.reason, "rule": f"slippage_bps > {a.max_slip_bps} or high==low==fill",
                                "trades": out}, indent=1, default=str))
    ledger.write_text(json.dumps(keep, indent=1, default=str))
    bad_ids = {t["trade_id"] for t in out}
    fb = STORAGE / "ml_live_feedback.jsonl"
    if fb.exists():
        lines = [l for l in fb.read_text().splitlines() if l.strip()]
        kept_lines = []
        for l in lines:
            try:
                if json.loads(l).get("trade_id") in bad_ids:
                    continue
            except Exception:
                pass
            kept_lines.append(l)
        fb.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""))
        print(f"ml_live_feedback: {len(lines)} -> {len(kept_lines)} lines")
    stats = STORAGE / "signal_stats.json"
    if stats.exists():
        stats.unlink()
    print(f"archived to {arch.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
