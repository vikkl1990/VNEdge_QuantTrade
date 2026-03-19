#!/usr/bin/env python3
"""Remove duplicate trades and dead trades from closed_signals.json.

Duplicates: Multiple trades with same symbol+side+entry_price within 5 minutes.
Dead trades: MFE=0.00R (price never moved in signal direction).

Keeps only the FIRST trade in each duplicate group.
"""
import json
from datetime import datetime

with open("storage/closed_signals.json") as f:
    closed = json.load(f)

original_count = len(closed)
print(f"Original trades: {original_count}")

# Pass 1: Remove duplicates (same symbol+side+entry within 5 min)
seen = {}  # key: "symbol_side_entry" -> entry_time
kept = []
dupes_removed = 0
dead_removed = 0

for t in closed:
    key = f"{t.get('symbol', '')}_{t.get('side', '')}_{t.get('entry_price', 0)}"
    entry_time = t.get("entry_time", "")

    # Check if this is a duplicate (same key within 5 minutes)
    is_dupe = False
    if key in seen:
        try:
            prev_time = datetime.fromisoformat(seen[key])
            curr_time = datetime.fromisoformat(entry_time)
            if abs((curr_time - prev_time).total_seconds()) < 300:  # 5 minutes
                is_dupe = True
                dupes_removed += 1
        except:
            pass

    if is_dupe:
        continue

    # Check if dead trade (MFE = 0, negative PnL)
    mfe = t.get("mfe_r", 0)
    pnl = t.get("pnl_pct", 0)
    exit_reason = t.get("exit_reason", "")

    if mfe <= 0.01 and pnl < 0 and exit_reason == "time_stop_dead_trade":
        dead_removed += 1
        continue

    seen[key] = entry_time
    kept.append(t)

print(f"Duplicates removed: {dupes_removed}")
print(f"Dead trades removed: {dead_removed}")
print(f"Remaining trades: {len(kept)}")

# Recalculate stats
wins = sum(1 for t in kept if t.get("pnl_pct", 0) > 0)
losses = sum(1 for t in kept if t.get("pnl_pct", 0) <= 0)
total_pnl = sum(t.get("pnl_pct", 0) for t in kept)
total_usd = sum(t.get("pnl_usd", 0) for t in kept)

print(f"\nCleaned stats: {wins}W/{losses}L | WR={wins/max(wins+losses,1)*100:.0f}% | PnL={total_pnl:+.3f}% | ${total_usd:+.2f}")

# Save
with open("storage/closed_signals.json", "w") as f:
    json.dump(kept, f, indent=2)

print("Saved cleaned data.")
