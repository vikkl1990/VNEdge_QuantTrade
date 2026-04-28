#!/usr/bin/env python3
"""Chief Quant Continuous Briefing — Agent 0 (the meta-agent).

Replaces the once-daily Architect Briefing (Agent 12) with a 30-min
continuous-feedback loop. Aggregates EVERY other agent's latest output,
plus live shadow performance, plus paper-vs-shadow gap, plus open
positions — into a single decision-grade briefing the architect can
read in 60 seconds at any time.

Sections:
  1. NOW — current state (open, recent PF, last hour)
  2. EDGE TRACKER — paper vs shadow gap from Agent 18
  3. AGENT FEEDBACK — what every agent said most recently
  4. RECOMMENDED ACTIONS — prioritized list (Chief Quant ranks by impact)
  5. DAILY TASKS — open items the architect should address today

Cron: */30 * * * *

Output:
  storage/chief_quant/briefing_TS.md (one per fire)
  storage/chief_quant/latest.md      (always the most recent — for dashboard)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

import asyncpg

ROOT = Path("/home/opc/crypto-trading-bot")
OUT_DIR = ROOT / "storage" / "chief_quant"
OUT_DIR.mkdir(parents=True, exist_ok=True)
LATEST_PATH = OUT_DIR / "latest.md"

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TS = dt.datetime.now(dt.timezone.utc)


def _load_env(path=ROOT / ".env"):
    try:
        with open(path) as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    os.environ.setdefault(k, v.strip('"').strip("'"))
    except FileNotFoundError:
        pass


def _dsn() -> str:
    _load_env()
    if os.environ.get("DATABASE_URL", "").startswith("postgres"):
        return os.environ["DATABASE_URL"]
    return "postgresql://vnedge:VnEdge2026db@localhost:5432/vnedge"


def latest_file(glob: str) -> Path | None:
    matches = sorted(OUT_DIR.parent.glob(glob), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def first_lines(p: Path, n=8) -> list[str]:
    if not p or not p.exists():
        return []
    try:
        return [ln.rstrip() for ln in p.read_text().splitlines()[:n]]
    except Exception:
        return []


async def shadow_summary(pool, hours: int):
    """Compute clean shadow summary over last N hours (primary canonical)."""
    sql = f"""
        SELECT
            COUNT(*) as n,
            SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl_usd)::float as net,
            SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0 END)::float as gw,
            SUM(CASE WHEN pnl_usd < 0 THEN -pnl_usd ELSE 0 END)::float as gl
        FROM user_trades
        WHERE trade_type='shadow'
          AND closed_at >= NOW() - INTERVAL '{int(hours)} hours'
          AND closed_at IS NOT NULL
          AND COALESCE(metadata::jsonb->>'exit_reason','')
              NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
          AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') != 'true'
               OR COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary')
    """
    async with pool.acquire() as conn:
        r = await conn.fetchrow(sql)
    n = int(r["n"] or 0)
    wins = int(r["wins"] or 0)
    net = float(r["net"] or 0)
    gw = float(r["gw"] or 0)
    gl = float(r["gl"] or 0)
    pf = (gw / gl) if gl > 0 else None
    wr = (wins / n * 100) if n else None
    return {"n": n, "wins": wins, "wr": wr, "net": net, "pf": pf}


async def open_book(pool):
    sql = """
        SELECT trade_type,
               COUNT(*) as n,
               COALESCE(metadata::jsonb->>'is_phase2_virtual','false') as p2v
        FROM user_trades WHERE closed_at IS NULL
        GROUP BY trade_type, p2v
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    out = {}
    for r in rows:
        key = f"{r['trade_type']}{'_p2' if r['p2v']=='true' else ''}"
        out[key] = int(r["n"])
    return out


async def loss_attribution(pool, hours: int):
    """Top loss exit_reasons in last N hours."""
    sql = f"""
        SELECT
            COALESCE(NULLIF(metadata::jsonb->>'exit_reason',''),'(none)') as reason,
            COUNT(*) as n,
            ROUND(SUM(pnl_usd)::numeric,2) as total
        FROM user_trades
        WHERE trade_type='shadow'
          AND closed_at >= NOW() - INTERVAL '{int(hours)} hours'
          AND pnl_usd < 0
          AND COALESCE(metadata::jsonb->>'exit_reason','')
              NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
          AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') != 'true'
               OR COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary')
        GROUP BY reason ORDER BY total ASC LIMIT 5
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [(r["reason"], int(r["n"]), float(r["total"])) for r in rows]


async def main():
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        s_1h = await shadow_summary(pool, 1)
        s_6h = await shadow_summary(pool, 6)
        s_24h = await shadow_summary(pool, 24)
        book = await open_book(pool)
        losses = await loss_attribution(pool, 6)
    finally:
        await pool.close()

    # Pull latest reports from other agents
    gap_md = latest_file("paper_shadow_gap/gap_*.md")
    venue_md = latest_file("venue_perf/venue_gap_*.md")
    cohort_md = latest_file("cohort_pause/state_*.md")
    incidents_md = latest_file("incidents/*.md")
    daily_md = latest_file("daily_briefing/briefing_*.md")
    maker_md = latest_file("verdicts/maker_verdict_*.md")
    auto_log = ROOT / "storage" / "auto_responder" / "actions.log"

    def fmt_pct(v): return f"{v:.1f}%" if v is not None else "—"
    def fmt_pf(v): return f"{v:.2f}" if v is not None else "—"
    def fmt_mny(v): return f"${v:+.2f}"

    ts_ist = TS.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")

    lines = [
        f"# 🧠 Chief Quant Continuous Briefing",
        f"Generated: **{ts_ist}** ({TS.isoformat()})",
        f"",
        f"---",
        f"## 1. NOW — current state",
        f"",
        f"| Window | n | WR | Net | PF |",
        f"|---|---:|---:|---:|---:|",
        f"| Last 1h | {s_1h['n']} | {fmt_pct(s_1h['wr'])} | {fmt_mny(s_1h['net'])} | {fmt_pf(s_1h['pf'])} |",
        f"| Last 6h | {s_6h['n']} | {fmt_pct(s_6h['wr'])} | {fmt_mny(s_6h['net'])} | {fmt_pf(s_6h['pf'])} |",
        f"| Last 24h | {s_24h['n']} | {fmt_pct(s_24h['wr'])} | {fmt_mny(s_24h['net'])} | {fmt_pf(s_24h['pf'])} |",
        f"",
        f"**Open book**: {sum(book.values())} trades — " + ", ".join(f"{k}={v}" for k, v in sorted(book.items())),
        f"",
    ]

    if losses:
        lines += [f"## 2. WHERE THE LOSSES COME FROM (last 6h)", "",
                  "| Exit reason | n | Total loss |",
                  "|---|---:|---:|"]
        for reason, n, total in losses:
            lines.append(f"| {reason} | {n} | {fmt_mny(total)} |")
        lines.append("")

    # Edge tracker
    lines += ["## 3. EDGE TRACKER — paper vs shadow", ""]
    if gap_md:
        gap_lines = first_lines(gap_md, 30)
        # Pull the headline gap line
        for ln in gap_lines:
            if "Avg gap" in ln or "Verdict" in ln:
                lines.append(f"  {ln.lstrip('-').strip()}")
        lines.append(f"  → see `{gap_md.relative_to(ROOT)}`")
    else:
        lines.append("  (no gap report yet — agent 18 fires in next 30min)")
    lines.append("")

    # Agent feedback summaries
    lines += ["## 4. AGENT FEEDBACK (most recent each)", ""]
    if venue_md:
        first = first_lines(venue_md, 4)
        lines.append(f"- **Agent 15 Venue Performance**: {first[0] if first else '?'}")
    if cohort_md:
        first = first_lines(cohort_md, 4)
        lines.append(f"- **Agent 17 Cohort Pause**: {first[0] if first else '?'}")
    if maker_md:
        lines.append(f"- **Maker Verdict**: see `{maker_md.relative_to(ROOT)}`")
    if auto_log.exists():
        try:
            tail = auto_log.read_text().splitlines()[-3:]
            lines.append(f"- **Agent 9-A Auto-Responder** last 3 actions:")
            for t in tail:
                lines.append(f"  - {t.strip()[:180]}")
        except Exception:
            pass
    if incidents_md:
        lines.append(f"- **Last incident**: `{incidents_md.relative_to(ROOT)}`")
    lines.append("")

    # ---- Recommended actions (rule-based ranking) ----
    actions: list[tuple[int, str]] = []  # (priority, line)

    if s_6h["pf"] is not None and s_6h["pf"] < 1.0:
        actions.append((1, f"🔴 P0 — Shadow PF over 6h is {s_6h['pf']:.2f} (<1.0). Review why losses dominate."))
    elif s_6h["pf"] is not None and s_6h["pf"] < 1.5:
        actions.append((2, f"🟡 P1 — Shadow PF 6h is {s_6h['pf']:.2f}. Watch for further degrade."))

    if losses:
        worst = losses[0]
        if abs(worst[2]) > 5.0:
            actions.append((1, f"🔴 P0 — `{worst[0]}` accounts for {fmt_mny(worst[2])} over {worst[1]} trades. Investigate this exit guard."))

    # Open trade overflow
    n_open = sum(book.values())
    if n_open > 50:
        actions.append((2, f"🟡 P1 — {n_open} trades open (Phase 2 fan-out + book). Verify monitor health."))

    # Maker fix still pending
    actions.append((3, "🟢 P2 — Path B live pilot still pending architect go-ahead (validates real maker fill rate)."))
    actions.append((3, "🟢 P2 — `v6_tp_15R` config in fan-out — needs 24h to accumulate sample for Phase 2 verdict."))

    actions.sort(key=lambda x: x[0])
    lines += ["## 5. RECOMMENDED ACTIONS (ranked)", ""]
    for _, action in actions:
        lines.append(f"- {action}")
    lines.append("")

    # ---- Daily tasks (today's open items) ----
    lines += ["## 6. DAILY TASKS — today's open items", "",
              "_Tracked items the architect should resolve today:_", ""]
    open_tasks = []
    if s_24h["pf"] is not None and s_24h["pf"] < 1.5:
        open_tasks.append("Decide: trail_trigger lower? (backtest disproved 0.3R; explore 0.2R or scratch-profit)")
    open_tasks.append("Watch v6_tp_15R Phase 2 leaderboard verdict (need 30+ closes; ETA 12-24h)")
    open_tasks.append("Decide on Path B: live pilot on admin (a/b/c options outstanding)")
    open_tasks.append("Investigate maker patient mode 0% fill rate (needs live pilot to measure)")
    if losses and losses[0][0] == "time_decay_10m" and abs(losses[0][2]) > 5:
        open_tasks.append(f"⚠ time_decay_10m bleeding {fmt_mny(losses[0][2])} in 6h — peak_R distribution suggests trail engagement issue")
    for i, t in enumerate(open_tasks, 1):
        lines.append(f"{i}. {t}")
    lines.append("")
    lines.append(f"---")
    lines.append(f"_Next briefing in 30 min. Live status: `storage/chief_quant/latest.md`_")

    out_text = "\n".join(lines)
    out_file = OUT_DIR / f"briefing_{TS.strftime('%Y%m%d_%H%M%S')}.md"
    out_file.write_text(out_text)
    LATEST_PATH.write_text(out_text)
    print(f"CHIEF_QUANT: 6h_pf={fmt_pf(s_6h['pf'])} 6h_net={fmt_mny(s_6h['net'])} "
          f"actions={len(actions)} tasks={len(open_tasks)}")
    print(f"Wrote: {out_file}")
    print(f"Latest symlink-style: {LATEST_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
