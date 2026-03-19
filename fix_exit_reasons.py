#!/usr/bin/env python3
"""Fix historical closed trades — reclassify stop_loss exits that were actually profitable."""
import json

with open("storage/closed_signals.json") as f:
    closed = json.load(f)

fixed = 0
for t in closed:
    if t.get("exit_reason") != "stop_loss":
        continue

    entry = t.get("entry_price", 0)
    sl = t.get("stop_loss", 0)
    side = t.get("side", "")
    pnl = t.get("pnl_pct", 0)
    exit_r = t.get("exit_r", 0)
    be = t.get("breakeven_set", False)
    tp1_hit = t.get("tp1_hit", False)

    # Check if SL was on the profit side (BE/trail was set)
    is_profit_exit = (
        (side == "long" and sl > entry) or
        (side == "short" and sl < entry)
    )

    if tp1_hit:
        t["exit_reason"] = "partial_win"
        t["status"] = "partial_win"
        fixed += 1
    elif is_profit_exit and be:
        t["exit_reason"] = "trail_profit"
        t["status"] = "trail_win"
        fixed += 1
    elif be and exit_r > -0.1:
        t["exit_reason"] = "breakeven"
        t["status"] = "breakeven"
        fixed += 1

print(f"Fixed {fixed}/{len(closed)} trades")

with open("storage/closed_signals.json", "w") as f:
    json.dump(closed, f, indent=2)

print("Saved.")
