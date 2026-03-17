"""
Backtest: New Patch Logic vs Old Logic
=======================================
Simulates all 100 trades (yesterday + today) through the new patch rules:

Patch 2: Lower leverage caps (A+=8x, A=6x, B=4x vs old 10x/8x/6x)
Patch 3: TP1 at 0.8R (vs old 1.5R) — would TP1 have been reached?
Patch 4: Dead-trade time stop (20min, <0.3R) — would stale trades exit earlier?
Patch 5: Near-TP protection (85% of TP1) — would near-winners be saved?
Patch 6: Impulse filter — would chased entries be blocked?

Uses actual trade data: entry, exit, SL, TP levels, highest/lowest prices, duration.
"""

import json
import sys
from datetime import datetime, timezone

# Load trades
with open("/tmp/all_trades.json") as f:
    data = json.load(f)

all_trades = data["yesterday"] + data["today"]
print(f"Total trades to backtest: {len(all_trades)}")
print(f"  Yesterday: {len(data['yesterday'])}")
print(f"  Today: {len(data['today'])}")
print()

# ── Session gate check ──
# IST = UTC + 5:30
# Asia Late = 02:30-09:00 IST = 21:00-03:30 UTC (previous day 21:00 to 03:30)
def get_ist_hour(utc_time_str):
    try:
        dt = datetime.fromisoformat(utc_time_str)
        # Add 5:30 for IST
        ist_hour = dt.hour + 5 + (dt.minute + 30) / 60
        if ist_hour >= 24:
            ist_hour -= 24
        return ist_hour
    except:
        return 12  # default to safe hour

def would_session_block(trade):
    """Check if session gating would have blocked this trade."""
    ist_hour = get_ist_hour(trade.get("entry_time", ""))
    return 2.5 <= ist_hour < 9.0

# ── Leverage calculation ──
def calc_old_leverage(conf, sl_dist_pct):
    """Old leverage rules (10x/8x/6x/3x)"""
    if conf >= 90: lev = 10
    elif conf >= 75: lev = 8
    elif conf >= 60: lev = 6
    else: lev = 3
    if sl_dist_pct > 0.5:
        lev = max(3, lev // 2)
    max_loss = 25.0 * lev * sl_dist_pct / 100
    if max_loss > 15.0:
        lev = max(3, int(15.0 / (25.0 * sl_dist_pct / 100)))
    return lev

def calc_new_leverage(conf, sl_dist_pct):
    """New leverage rules (8x/6x/4x/3x)"""
    if conf >= 90: lev = 8
    elif conf >= 80: lev = 6
    elif conf >= 65: lev = 4
    else: lev = 3
    if sl_dist_pct > 0.5:
        lev = max(3, lev // 2)
    max_loss = 25.0 * lev * sl_dist_pct / 100
    if max_loss > 15.0:
        lev = max(3, int(15.0 / (25.0 * sl_dist_pct / 100)))
    return lev

def calc_stake(conf):
    if conf >= 90: return 50.0
    elif conf >= 80: return 37.5
    return 25.0

# ── Simulate new TP1 at 0.8R ──
def would_new_tp1_hit(trade):
    """Check if the new 0.8R TP1 would have been hit based on highest/lowest prices."""
    entry = trade.get("entry_price", 0)
    sl = trade.get("stop_loss", 0)
    side = trade.get("side", "long")
    highest = trade.get("highest_price", entry)
    lowest = trade.get("lowest_price", entry)

    if entry <= 0 or sl <= 0:
        return False, 0

    risk = abs(entry - sl)
    new_tp1_dist = risk * 0.8  # 0.8R instead of 1.5R

    if side == "long":
        new_tp1 = entry + new_tp1_dist
        hit = highest >= new_tp1
        max_favorable_r = (highest - entry) / risk if risk > 0 else 0
    else:
        new_tp1 = entry - new_tp1_dist
        hit = lowest <= new_tp1
        max_favorable_r = (entry - lowest) / risk if risk > 0 else 0

    return hit, max_favorable_r

# ── Dead trade time stop ──
def would_time_stop(trade):
    """Check if dead-trade time stop would have closed this trade earlier."""
    entry = trade.get("entry_price", 0)
    sl = trade.get("stop_loss", 0)
    highest = trade.get("highest_price", entry)
    lowest = trade.get("lowest_price", entry)
    side = trade.get("side", "long")
    status = trade.get("status", "")

    if entry <= 0 or sl <= 0:
        return False

    risk = abs(entry - sl)
    if risk <= 0:
        return False

    # Calculate max favorable excursion in R
    if side == "long":
        max_fav_r = (highest - entry) / risk
    else:
        max_fav_r = (entry - lowest) / risk

    # Calculate duration
    try:
        e = datetime.fromisoformat(trade["entry_time"])
        x = datetime.fromisoformat(trade["exit_time"])
        duration_min = (x - e).total_seconds() / 60
    except:
        return False

    # Time stop: >20min and never reached 0.3R
    if duration_min >= 20 and max_fav_r < 0.3 and status == "stopped":
        return True
    return False

# ── Near-TP protection ──
def would_near_tp_protect(trade):
    """Check if near-TP protection would have saved this trade."""
    entry = trade.get("entry_price", 0)
    sl = trade.get("stop_loss", 0)
    tp1 = trade.get("tp1", 0)
    highest = trade.get("highest_price", entry)
    lowest = trade.get("lowest_price", entry)
    side = trade.get("side", "long")
    status = trade.get("status", "")
    tp1_hit = trade.get("tp1_hit", False)

    if entry <= 0 or tp1 <= 0 or tp1_hit:
        return False

    tp1_dist = abs(tp1 - entry)
    if tp1_dist <= 0:
        return False

    # Max favorable excursion
    if side == "long":
        max_fav = highest - entry
    else:
        max_fav = entry - lowest

    # Reached 85%+ of TP1 but didn't hit it, and ended as a loss
    reached_pct = max_fav / tp1_dist if tp1_dist > 0 else 0
    if reached_pct >= 0.85 and status == "stopped":
        return True
    return False

# ══════════════════════════════════════════════════════════════
# RUN BACKTEST
# ══════════════════════════════════════════════════════════════

print("=" * 90)
print("BACKTEST: New Patch Logic vs Actual Results")
print("=" * 90)

# Counters
old_pnl_usd = 0
new_pnl_usd = 0
old_wins = 0
new_wins = 0
session_blocked = 0
time_stopped = 0
near_tp_saved = 0
new_tp1_would_hit = 0
old_tp1_hit = 0
impulse_blocked = 0  # can't fully simulate without candle data
total = len(all_trades)

# Detailed results
results = []

for t in all_trades:
    entry = t.get("entry_price", 0)
    sl = t.get("stop_loss", 0)
    conf = t.get("confidence", 0)
    setup = t.get("setup_type", "")
    side = t.get("side", "long")
    status = t.get("status", "")
    old_pnl = t.get("pnl_usd", 0)
    old_pnl_pct = t.get("pnl_pct", 0)
    old_lev = t.get("leverage", 1)
    old_pos = t.get("position_size_usd", 0)
    tp1_hit = t.get("tp1_hit", False)
    highest = t.get("highest_price", entry)
    lowest = t.get("lowest_price", entry)
    gross_pnl_pct = t.get("gross_pnl_pct", old_pnl_pct)

    if entry <= 0:
        continue

    sl_dist_pct = abs(entry - sl) / entry * 100 if entry > 0 else 1.0
    risk = abs(entry - sl)

    # Track old TP1 hits
    if tp1_hit:
        old_tp1_hit += 1

    # ── Check session gate ──
    blocked = would_session_block(t)
    if blocked:
        session_blocked += 1

    # ── Check if disabled setup ──
    setup_blocked = setup in ("momentum_surge", "supertrend_flip")

    # ── New leverage ──
    new_lev = calc_new_leverage(conf, sl_dist_pct)
    stake = calc_stake(conf)
    new_pos = stake * new_lev

    # ── New TP1 check ──
    new_tp1_hit, max_fav_r = would_new_tp1_hit(t)
    if new_tp1_hit:
        new_tp1_would_hit += 1

    # ── Time stop check ──
    ts = would_time_stop(t)
    if ts:
        time_stopped += 1

    # ── Near-TP protection ──
    ntp = would_near_tp_protect(t)
    if ntp:
        near_tp_saved += 1

    # ── Calculate new PnL ──
    if blocked or setup_blocked:
        # Trade would not have been taken
        new_trade_pnl = 0
        new_status = "BLOCKED"
    elif ts:
        # Time stop: exit at roughly 0R (near entry, small loss from fees)
        # Estimate: lose about 0.1R + fees
        fee_cost = new_pos * 0.18 / 100
        if side == "long":
            # Approximate exit near entry (slight loss)
            est_loss = new_pos * 0.05 / 100  # ~0.05% loss + fees
        else:
            est_loss = new_pos * 0.05 / 100
        new_trade_pnl = -(est_loss + fee_cost)
        new_status = "TIME_STOP"
    elif ntp and not tp1_hit:
        # Near-TP protection: exit at ~50% of favorable move
        if side == "long":
            max_fav_pct = (highest - entry) / entry * 100
        else:
            max_fav_pct = (entry - lowest) / entry * 100
        # Capture ~50% of peak favorable move
        captured_pct = max_fav_pct * 0.50
        fee_pct = 0.18
        net_pct = captured_pct - fee_pct
        new_trade_pnl = new_pos * net_pct / 100
        new_status = "NEAR_TP_PROTECT"
    elif new_tp1_hit and not tp1_hit:
        # New TP1 would have hit (0.8R) where old TP1 (1.5R) didn't
        # 70% exits at TP1 (0.8R), 30% continues to actual exit
        tp1_pnl_pct = 0.8 * sl_dist_pct  # 0.8R in %

        # 30% runner uses actual exit price
        if old_pnl_pct > 0:
            runner_pnl_pct = old_pnl_pct  # actual exit was profitable
        else:
            # Runner probably stopped at break-even (fee-aware BE after TP1)
            runner_pnl_pct = -0.18  # lose just fees on the runner

        blended_pnl_pct = 0.70 * tp1_pnl_pct + 0.30 * runner_pnl_pct
        fee_pct = 0.18
        net_pct = blended_pnl_pct - fee_pct
        new_trade_pnl = new_pos * net_pct / 100
        new_status = "NEW_TP1_WIN"
    else:
        # Same outcome, but with new position size
        # Scale PnL by new_pos / old_pos ratio
        if old_pos > 0:
            scale = new_pos / old_pos
        else:
            scale = 1.0
        new_trade_pnl = old_pnl * scale
        new_status = "SAME"

    old_pnl_usd += old_pnl
    new_pnl_usd += new_trade_pnl

    if old_pnl > 0:
        old_wins += 1
    if new_trade_pnl > 0:
        new_wins += 1

    results.append({
        "setup": setup,
        "side": side,
        "conf": conf,
        "old_lev": old_lev,
        "new_lev": new_lev,
        "old_pos": old_pos,
        "new_pos": new_pos,
        "old_pnl": old_pnl,
        "new_pnl": round(new_trade_pnl, 2),
        "new_status": new_status,
        "max_fav_r": round(max_fav_r, 2),
        "old_tp1_hit": tp1_hit,
        "new_tp1_hit": new_tp1_hit,
    })

# ══════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════

print()
print("─" * 90)
print("SUMMARY: Old Logic vs New Patch Logic")
print("─" * 90)
print(f"{'Metric':<40} {'OLD':>12} {'NEW':>12} {'DIFF':>12}")
print("─" * 90)

old_wr = old_wins / total * 100 if total > 0 else 0
# For new WR, exclude blocked trades
new_taken = sum(1 for r in results if r["new_status"] != "BLOCKED")
new_wr = new_wins / new_taken * 100 if new_taken > 0 else 0

print(f"{'Total Trades':<40} {total:>12d} {new_taken:>12d} {new_taken - total:>+12d}")
print(f"{'Wins':<40} {old_wins:>12d} {new_wins:>12d} {new_wins - old_wins:>+12d}")
print(f"{'Win Rate':<40} {old_wr:>11.1f}% {new_wr:>11.1f}% {new_wr - old_wr:>+11.1f}%")
print(f"{'Total PnL ($)':<40} {old_pnl_usd:>+12.2f} {new_pnl_usd:>+12.2f} {new_pnl_usd - old_pnl_usd:>+12.2f}")
print()
print(f"{'Session-gated (blocked)':<40} {0:>12d} {session_blocked:>12d}")
print(f"{'Setup-disabled (blocked)':<40} {0:>12d} {sum(1 for r in results if r['new_status'] == 'BLOCKED') - session_blocked:>12d}")
print(f"{'Time-stopped (dead trades)':<40} {0:>12d} {time_stopped:>12d}")
print(f"{'Near-TP protected':<40} {0:>12d} {near_tp_saved:>12d}")
print(f"{'TP1 hit (old 1.5R)':<40} {old_tp1_hit:>12d}")
print(f"{'TP1 would hit (new 0.8R)':<40} {'':>12} {new_tp1_would_hit:>12d} {new_tp1_would_hit - old_tp1_hit:>+12d}")
print()

# ── Breakdown by action ──
print("─" * 90)
print("BREAKDOWN BY PATCH ACTION")
print("─" * 90)
actions = {}
for r in results:
    s = r["new_status"]
    if s not in actions:
        actions[s] = {"count": 0, "old_pnl": 0, "new_pnl": 0}
    actions[s]["count"] += 1
    actions[s]["old_pnl"] += r["old_pnl"]
    actions[s]["new_pnl"] += r["new_pnl"]

print(f"{'Action':<25} {'Count':>6} {'Old PnL':>12} {'New PnL':>12} {'Saved':>12}")
print("─" * 90)
for action in ["BLOCKED", "TIME_STOP", "NEAR_TP_PROTECT", "NEW_TP1_WIN", "SAME"]:
    if action in actions:
        a = actions[action]
        saved = a["new_pnl"] - a["old_pnl"]
        print(f"{action:<25} {a['count']:>6d} {a['old_pnl']:>+12.2f} {a['new_pnl']:>+12.2f} {saved:>+12.2f}")

total_saved = new_pnl_usd - old_pnl_usd
print("─" * 90)
print(f"{'TOTAL IMPROVEMENT':<25} {'':>6} {'':>12} {'':>12} {total_saved:>+12.2f}")

# ── Leverage comparison ──
print()
print("─" * 90)
print("LEVERAGE COMPARISON")
print("─" * 90)
old_avg_lev = sum(r["old_lev"] for r in results) / len(results)
new_avg_lev = sum(r["new_lev"] for r in results) / len(results)
old_avg_pos = sum(r["old_pos"] for r in results) / len(results)
new_avg_pos = sum(r["new_pos"] for r in results) / len(results)
print(f"{'Avg Leverage':<40} {old_avg_lev:>12.1f}x {new_avg_lev:>12.1f}x")
print(f"{'Avg Position Size':<40} ${old_avg_pos:>11.0f} ${new_avg_pos:>11.0f}")

# ── Per-setup breakdown ──
print()
print("─" * 90)
print("PER-SETUP IMPACT")
print("─" * 90)
setups = {}
for r in results:
    s = r["setup"] or "(unknown)"
    if s not in setups:
        setups[s] = {"count": 0, "old_pnl": 0, "new_pnl": 0, "old_wins": 0, "new_wins": 0, "blocked": 0}
    setups[s]["count"] += 1
    setups[s]["old_pnl"] += r["old_pnl"]
    setups[s]["new_pnl"] += r["new_pnl"]
    if r["old_pnl"] > 0: setups[s]["old_wins"] += 1
    if r["new_pnl"] > 0: setups[s]["new_wins"] += 1
    if r["new_status"] == "BLOCKED": setups[s]["blocked"] += 1

print(f"{'Setup':<22} {'N':>4} {'Blk':>4} {'Old WR':>8} {'New WR':>8} {'Old PnL':>10} {'New PnL':>10} {'Diff':>10}")
print("─" * 90)
for setup in sorted(setups.keys(), key=lambda k: setups[k]["old_pnl"], reverse=True):
    s = setups[setup]
    taken = s["count"] - s["blocked"]
    old_wr_s = s["old_wins"] / s["count"] * 100 if s["count"] > 0 else 0
    new_wr_s = s["new_wins"] / taken * 100 if taken > 0 else 0
    diff = s["new_pnl"] - s["old_pnl"]
    print(f"{setup:<22} {s['count']:>4d} {s['blocked']:>4d} {old_wr_s:>7.1f}% {new_wr_s:>7.1f}% {s['old_pnl']:>+10.2f} {s['new_pnl']:>+10.2f} {diff:>+10.2f}")

# ── Worst trades comparison ──
print()
print("─" * 90)
print("TOP 10 MOST IMPROVED TRADES")
print("─" * 90)
results_sorted = sorted(results, key=lambda r: r["new_pnl"] - r["old_pnl"], reverse=True)
print(f"{'Setup':<20} {'Side':>5} {'Conf':>5} {'Old$':>8} {'New$':>8} {'Saved':>8} {'Action':<20}")
for r in results_sorted[:10]:
    saved = r["new_pnl"] - r["old_pnl"]
    print(f"{r['setup']:<20} {r['side']:>5} {r['conf']:>5d} {r['old_pnl']:>+8.2f} {r['new_pnl']:>+8.2f} {saved:>+8.2f} {r['new_status']:<20}")

print()
print("─" * 90)
print("TOP 10 MOST DEGRADED TRADES")
print("─" * 90)
print(f"{'Setup':<20} {'Side':>5} {'Conf':>5} {'Old$':>8} {'New$':>8} {'Lost':>8} {'Action':<20}")
for r in results_sorted[-10:]:
    saved = r["new_pnl"] - r["old_pnl"]
    print(f"{r['setup']:<20} {r['side']:>5} {r['conf']:>5d} {r['old_pnl']:>+8.2f} {r['new_pnl']:>+8.2f} {saved:>+8.2f} {r['new_status']:<20}")

# ── Final verdict ──
print()
print("=" * 90)
if new_pnl_usd > old_pnl_usd:
    print(f"VERDICT: NEW PATCH IMPROVES PnL by ${new_pnl_usd - old_pnl_usd:+.2f}")
    print(f"  Old: ${old_pnl_usd:+.2f} → New: ${new_pnl_usd:+.2f}")
    print(f"  WR: {old_wins}/{total} ({old_wins/total*100:.1f}%) → {new_wins}/{new_taken} ({new_wins/new_taken*100:.1f}%)")
else:
    print(f"VERDICT: NEW PATCH REDUCES PnL by ${old_pnl_usd - new_pnl_usd:.2f}")
    print(f"  Old: ${old_pnl_usd:+.2f} → New: ${new_pnl_usd:+.2f}")
print("=" * 90)
