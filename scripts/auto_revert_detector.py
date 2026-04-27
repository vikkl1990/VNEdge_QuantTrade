#!/usr/bin/env python3
"""
Phase 5.20-D — Auto-revert detector.

Compares current bot performance against the latest deployment_baseline.
If degradation crosses a threshold, engages the global kill switch and
logs to deployment_revert_log.

Cron:
    # Every 30 min — sufficient cadence for a slow-moving bot
    */30 * * * * cd /home/opc/crypto-trading-bot && python3 scripts/auto_revert_detector.py

Triggers (any one):
    1. WR drop:           current_30trade_WR < baseline_WR - 10pp
    2. PnL drop:          current_30trade_avg_pnl < baseline_avg_pnl - 0.50
    3. SL_REVERT spike:   current_hr > 5 (signals execution storm)
    4. Latency spike:     current p95 > baseline + 200ms (if measured)

Action:
    1. Write deployment_revert_log row with trigger details
    2. Engage kill switch (with auto-engaged_by='auto_revert_detector')
    3. Telegram alert (if configured)

To resume after revert:
    python3 -m execution.kill_switch release --by ops
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))


def _load_env(path=".env"):
    try:
        with open(path) as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    os.environ.setdefault(k, v.strip('"').strip("'"))
    except FileNotFoundError:
        pass


def _dsn():
    _load_env()
    if os.environ.get("DATABASE_URL", "").startswith("postgres"):
        return os.environ["DATABASE_URL"]
    return "postgresql://vnedge:VnEdge2026db@localhost:5432/vnedge"


# --- Trigger thresholds ---
THRESHOLD_WR_DROP_PP = 10.0          # 10 percentage points
THRESHOLD_PNL_DROP_USD = 0.50        # avg P&L per trade
THRESHOLD_SL_REVERT_PER_HR = 5.0     # SL_REVERT events per hour
THRESHOLD_LATENCY_SPIKE_MS = 200.0   # additional p95 latency
MIN_CURRENT_SAMPLE = 30              # need this many recent trades for comparison


async def fetch_active_baseline(conn):
    return await conn.fetchrow("""
        SELECT * FROM deployment_baseline
        WHERE is_active = TRUE
        ORDER BY captured_at DESC
        LIMIT 1
    """)


async def fetch_recent_metrics(conn, n: int = 30):
    """Last N closed trades — measure current performance.

    2026-04-26 fix (false-positive at 07:00 UTC):
    EXCLUDE force-closed trades (status='force_closed_*' or exit_reason='kill_switch_*').
    These represent operational decisions (manual force-flat, kill_switch listener),
    not strategy performance. Including them poisoned the baseline comparison —
    13 force-closes from morning made avg_pnl look catastrophic, triggering false
    auto-revert on the next 30-trade window.
    """
    # 2026-04-27 second-tier fix — same false-positive class:
    # auto_responder_stuck_60m closes are 60-minute admin force-closes
    # caused by the pre-fix max_age=3600 on INTRADAY/RUNNER shadow
    # trades. All exit at PnL=$0 (entry=exit) -> drag WR from ~70%
    # true to ~25% contaminated -> wr_drop kill switch trips on FALSE
    # bad performance. Triggered at 2026-04-27 08:00 UTC after the
    # morning's ~50 stuck-60m rows piled into the recent-30 window.
    # Also exclude is_phase2_virtual fan-out trades (5x write
    # amplification would skew the n=30 window unfairly toward
    # whichever config opens first).
    rows = await conn.fetch(f"""
        SELECT pnl_usd
        FROM user_trades
        WHERE pnl_usd IS NOT NULL
          AND trade_type IN ('real', 'shadow')
          AND closed_at >= NOW() - INTERVAL '24 hours'
          AND COALESCE(status, '') NOT LIKE 'force_closed%'
          AND COALESCE(metadata::jsonb->>'exit_reason', '') NOT IN
              ('kill_switch_close_open', 'force_flat', 'restart_reconcile_flat',
               'execution_refactor_v2_FORCE_FLAT', 'auto_responder_stuck_60m', 'restart_orphan_cleanup')
          AND COALESCE(metadata::jsonb->>'is_phase2_virtual', 'false') != 'true'
        ORDER BY closed_at DESC
        LIMIT {int(n)}
    """)
    if not rows:
        return None
    pnls = [float(r["pnl_usd"] or 0) for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": len(pnls),
        "wr_pct": (wins / len(pnls)) * 100,
        "avg_pnl": sum(pnls) / len(pnls),
        "sum_pnl": sum(pnls),
    }


async def detect_triggers(conn, baseline, current):
    """Return list of trigger reasons + details."""
    triggers = []
    if not baseline or not current:
        return triggers

    if current["n"] < MIN_CURRENT_SAMPLE:
        return triggers  # not enough data yet

    base_wr = float(baseline["baseline_wr_pct"] or 0)
    base_avg = float(baseline["baseline_avg_pnl_usd"] or 0)

    # Trigger 1: WR drop
    wr_delta = base_wr - current["wr_pct"]
    if wr_delta > THRESHOLD_WR_DROP_PP:
        triggers.append({
            "reason": "wr_drop",
            "detail": {
                "baseline_wr": base_wr, "current_wr": current["wr_pct"],
                "drop_pp": wr_delta, "threshold_pp": THRESHOLD_WR_DROP_PP,
                "n_current": current["n"],
            },
        })

    # Trigger 2: avg PnL drop
    pnl_delta = base_avg - current["avg_pnl"]
    if pnl_delta > THRESHOLD_PNL_DROP_USD:
        triggers.append({
            "reason": "pnl_drop",
            "detail": {
                "baseline_avg_pnl": base_avg, "current_avg_pnl": current["avg_pnl"],
                "drop_usd": pnl_delta, "threshold_usd": THRESHOLD_PNL_DROP_USD,
                "n_current": current["n"],
            },
        })

    # Trigger 3: SL_REVERT spike (count last hour from journalctl is hard;
    # use a proxy: count user_trades with metadata.n2_sl_retries > 0 in last hr)
    revert_row = await conn.fetchrow("""
        SELECT COUNT(*) AS n
        FROM user_trades
        WHERE closed_at >= NOW() - INTERVAL '1 hour'
          AND (metadata::jsonb->>'n2_sl_retries')::int > 0
    """)
    sl_revert_per_hr = float(revert_row["n"] or 0)
    if sl_revert_per_hr > THRESHOLD_SL_REVERT_PER_HR:
        triggers.append({
            "reason": "sl_revert_spike",
            "detail": {
                "current_per_hr": sl_revert_per_hr,
                "threshold_per_hr": THRESHOLD_SL_REVERT_PER_HR,
            },
        })

    return triggers


async def engage_kill_switch(conn, baseline_id: int, triggers: list):
    """Set bot_state.kill_switch_engaged + log revert action."""
    reason = "; ".join(t["reason"] for t in triggers)
    detail_summary = " | ".join(
        f"{t['reason']}: {list(t['detail'].items())[:2]}"
        for t in triggers
    )[:255]

    await conn.execute("""
        UPDATE bot_state
        SET kill_switch_engaged = TRUE,
            kill_switch_reason = $1,
            kill_switch_engaged_at = NOW(),
            kill_switch_engaged_by = 'auto_revert_detector',
            kill_switch_close_open = FALSE,
            updated_at = NOW()
        WHERE id = 1
    """, f"AUTO-REVERT: {reason}"[:255])

    import json as _json
    for t in triggers:
        await conn.execute("""
            INSERT INTO deployment_revert_log
              (baseline_id, trigger_reason, trigger_detail, revert_action,
               reverted_at, notes)
            VALUES ($1, $2, $3, 'engaged_kill_switch', NOW(), $4)
        """, baseline_id, t["reason"], _json.dumps(t["detail"]),
             "auto-engaged kill switch; manual release required")

    print(f"🛑 AUTO-REVERT TRIGGERED — kill switch engaged")
    print(f"   Triggers: {reason}")
    print(f"   Detail: {detail_summary}")


async def main():
    conn = await asyncpg.connect(_dsn())
    try:
        baseline = await fetch_active_baseline(conn)
        current = await fetch_recent_metrics(conn)

        if not baseline:
            print("(no active baseline — skipping detection)")
            return
        if not current:
            print("(no recent trades — skipping detection)")
            return

        print(f"\n=== Auto-revert check — {datetime.now(timezone.utc).isoformat()} ===")
        print(f"  Baseline (id={baseline['id']}, captured={baseline['captured_at'].isoformat()})")
        print(f"    WR: {float(baseline['baseline_wr_pct'] or 0):.1f}%  "
              f"avg_pnl: ${float(baseline['baseline_avg_pnl_usd'] or 0):+.3f}")
        print(f"  Current (n={current['n']})")
        print(f"    WR: {current['wr_pct']:.1f}%  avg_pnl: ${current['avg_pnl']:+.3f}")
        print()

        triggers = await detect_triggers(conn, baseline, current)

        # 2026-04-26 EXTENSION (Agent 1 v2):
        # Original checks WR/PnL drift on CLOSED trades only — blind to bugs
        # like today's where trades sat OPEN forever (excluded from WR calc).
        # Add two new triggers:
        #   (a) stuck-open: any open trade older than 2× SCALP max_age (= 60min)
        #   (b) close/open ratio: 24h closes < 50% of 24h opens → exits broken
        try:
            stuck_row = await conn.fetchrow(
                "SELECT COUNT(*) AS n_stuck FROM user_trades "
                "WHERE closed_at IS NULL AND opened_at < NOW() - INTERVAL '60 minutes'"
            )
            n_stuck = int(stuck_row["n_stuck"] or 0)
            if n_stuck > 0:
                triggers.append({
                    "reason": "stuck_open_trades_60min",
                    "detail": f"{n_stuck} trade(s) open >60min — exit guards likely broken",
                })

            ratio_rows = await conn.fetch(
                "SELECT exchange, trade_type, "
                "COUNT(CASE WHEN opened_at >= NOW() - INTERVAL '24 hours' THEN 1 END) AS opens, "
                "COUNT(CASE WHEN closed_at >= NOW() - INTERVAL '24 hours' THEN 1 END) AS closes "
                "FROM user_trades WHERE opened_at >= NOW() - INTERVAL '24 hours' "
                "GROUP BY exchange, trade_type"
            )
            for r in ratio_rows:
                opens = int(r["opens"] or 0)
                closes = int(r["closes"] or 0)
                if opens >= 5 and closes / opens < 0.5:
                    triggers.append({
                        "reason": "exit_rate_collapse",
                        "detail": f"{r['exchange']}|{r['trade_type']}: {closes}/{opens} closes/opens (24h) < 50%",
                    })
        except Exception as e:
            print(f"  (extension check failed: {e})")

        if not triggers:
            print("✅ No degradation detected.")
            return

        # Check if kill switch already engaged — don't double-engage
        ks_row = await conn.fetchrow(
            "SELECT kill_switch_engaged FROM bot_state WHERE id = 1"
        )
        if ks_row and ks_row["kill_switch_engaged"]:
            print("⚠️ Triggers detected but kill switch already engaged — logging only")
            for t in triggers:
                print(f"   - {t['reason']}: {t['detail']}")
            return

        await engage_kill_switch(conn, baseline["id"], triggers)

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
