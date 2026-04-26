#!/usr/bin/env python3
"""Agent 12 — Daily Architect Briefing (daily 18:00 UTC; deep on Sunday)

Synthesizes outputs from all other agents into a single page that the
architect can read in 60 seconds.

Sources:
  - Agent 1: storage/recon/eod_*.txt + auto_revert journald
  - Agent 2: storage/verdicts/maker_verdict_*.md
  - Agent 4: storage/code_review/rollback_diff_*.md
  - Agent 5: .rollback/silent_failure_scan_*.md
  - Agent 8: data_integrity_daily output
  - Agent 15: storage/venue_perf/venue_gap_*.md
  - Agent 16: storage/heartbeat/status_latest.txt
  - Agent 17: storage/cohort_pause/state_*.md
  - DB live state (BOOK, EDGE, MAKER)

Output: storage/daily_briefing/briefing_YYYYMMDD.md
Cron: 0 18 * * *
"""
import sys
import datetime
import pathlib
import subprocess
import re

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
OUT_DIR = ROOT / "storage" / "daily_briefing"
TS = datetime.datetime.utcnow()
OUT_FILE = OUT_DIR / f"briefing_{TS.strftime('%Y%m%d')}.md"

OUT_DIR.mkdir(parents=True, exist_ok=True)


def psql(sql):
    cmd = (
        "PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge "
        f"-A -F '|' -t -c \"{sql}\""
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def latest_file(pattern_dir, pattern_prefix):
    """Find the most recent file in pattern_dir starting with pattern_prefix."""
    if not pattern_dir.exists():
        return None
    files = sorted(pattern_dir.glob(f"{pattern_prefix}*"))
    return files[-1] if files else None


def head_lines(path, n=20):
    if not path or not path.exists():
        return f"_(no file at {path})_"
    return "\n".join(path.read_text().splitlines()[:n])


is_sunday = TS.weekday() == 6
deep = " (deep)" if is_sunday else ""

lines = [
    f"# Daily Architect Briefing{deep}",
    f"Generated: {TS.isoformat()}Z",
    f"",
    f"60-second read. Drill into linked agent reports for detail.",
    f"",
    f"---",
    f"",
    f"## 1. Bot health — last 24h",
]

# ─── Bot KPIs ────────────────────────────────────────────────
rc, out, _ = psql(
    "SELECT exchange||'_'||trade_type as bucket, COUNT(*) as n, "
    "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) as wins, "
    "ROUND(SUM(pnl_usd)::numeric, 2) as net, "
    "ROUND(SUM(fees_usd)::numeric, 2) as fees "
    "FROM user_trades WHERE closed_at >= NOW() - INTERVAL '24 hours' "
    "AND COALESCE(metadata::jsonb->>'exit_reason','') NOT LIKE 'force_orphan%' "
    "GROUP BY exchange, trade_type ORDER BY exchange, trade_type;"
)
lines.append("| Bucket | n | wins | WR | net PnL | fees |")
lines.append("|---|---:|---:|---:|---:|---:|")
if rc == 0 and out:
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 5:
            n = int(parts[1])
            wins = int(parts[2])
            wr = wins / n * 100 if n else 0
            lines.append(f"| {parts[0]} | {n} | {wins} | {wr:.1f}% | ${parts[3]} | ${parts[4]} |")
else:
    lines.append("| _no closed trades_ | | | | | |")
lines.append("")

# ─── Open trades ────────────────────────────────────────────
rc, out, _ = psql(
    "SELECT COUNT(*), MAX(EXTRACT(EPOCH FROM (NOW()-opened_at))/60)::int "
    "FROM user_trades WHERE closed_at IS NULL;"
)
if out:
    parts = out.split("|")
    lines.append(f"**Open: {parts[0]} trade(s)** | oldest age: {parts[1] or 0}min")
    lines.append("")

# ─── 2. Agent reports (most recent each) ────────────────────
lines.append("---")
lines.append("")
lines.append("## 2. Agent reports — most recent each")
lines.append("")

# Agent 15 — Venue Performance
v_file = latest_file(ROOT / "storage" / "venue_perf", "venue_gap_")
lines.append(f"### Agent 15 — Venue Performance")
if v_file:
    txt = v_file.read_text()
    verdict_m = re.search(r"## Verdict: (.+)", txt)
    if verdict_m:
        lines.append(f"- {verdict_m.group(1).strip()}")
    lines.append(f"- file: `{v_file.relative_to(ROOT)}`")
else:
    lines.append("- ⚠️ no run yet")
lines.append("")

# Agent 16 — Heartbeat
h_file = ROOT / "storage" / "heartbeat" / "status_latest.txt"
lines.append(f"### Agent 16 — Process Heartbeat")
if h_file.exists():
    for ln in h_file.read_text().splitlines()[:8]:
        lines.append(f"- {ln}")
else:
    lines.append("- ⚠️ no run yet")
lines.append("")

# Agent 17 — Cohort pauses
c_file = latest_file(ROOT / "storage" / "cohort_pause", "state_")
lines.append(f"### Agent 17 — Cohort & Pause Surfacer")
if c_file:
    txt = c_file.read_text()
    if "🔴" in txt or "Hours remaining" in txt:
        lines.append("- 🟡 active pauses found — see report")
    else:
        lines.append("- 🟢 no active pauses")
    lines.append(f"- file: `{c_file.relative_to(ROOT)}`")
else:
    lines.append("- ⚠️ no run yet")
lines.append("")

# Agent 5 — Silent Failure Hunter HEAVY
s_files = sorted((ROOT / ".rollback").glob("silent_failure_scan_*.md"))
s_file = s_files[-1] if s_files else None
lines.append(f"### Agent 5 — Silent Failure Hunter")
if s_file:
    txt = s_file.read_text()
    summary_m = re.search(r"## Summary: (\d+) 🔴 critical", txt)
    if summary_m:
        n_red = int(summary_m.group(1))
        if n_red > 0:
            lines.append(f"- 🔴 **{n_red} critical findings** in latest scan")
        else:
            lines.append("- 🟢 0 critical findings")
    lines.append(f"- file: `{s_file.relative_to(ROOT)}`")
    lines.append(f"- generated: {datetime.datetime.fromtimestamp(s_file.stat().st_mtime).isoformat()}")
else:
    lines.append("- ⚠️ no scan yet")
lines.append("")

# Agent 4 — Code Review
cr_file = latest_file(ROOT / "storage" / "code_review", "rollback_diff_")
lines.append(f"### Agent 4 — Code Review (Rollback Diff)")
if cr_file:
    txt = cr_file.read_text()
    flag_m = re.search(r"\*\*Files with diffs vs rollback baseline:\*\* (\d+) / (\d+)", txt)
    if flag_m:
        n_changed, n_total = int(flag_m.group(1)), int(flag_m.group(2))
        if n_changed > 0:
            lines.append(f"- 🟡 {n_changed}/{n_total} tracked files changed-since-rollback (review)")
        else:
            lines.append(f"- 🟢 0/{n_total} files diverged from rollback baseline")
    lines.append(f"- file: `{cr_file.relative_to(ROOT)}`")
else:
    lines.append("- ⚠️ no run yet")
lines.append("")

# Agent 2 — Maker mode verdict
m_file = latest_file(ROOT / "storage" / "verdicts", "maker_verdict_")
lines.append(f"### Agent 2 — Execution Quality / Maker Mode")
if m_file:
    lines.append(f"- file: `{m_file.relative_to(ROOT)}`")
else:
    lines.append("- ⚠️ no run yet")
lines.append("")

# ─── 3. Decisions awaiting architect ────────────────────────
lines.append("---")
lines.append("")
lines.append("## 3. Decisions awaiting architect input")
lines.append("")
lines.append("- (Synthesize via reading decision-log; placeholder until decision-log integration)")
lines.append("")

# ─── Sunday deep section ────────────────────────────────────
if is_sunday:
    lines.append("---")
    lines.append("")
    lines.append("## SUNDAY DEEP — week-over-week")
    lines.append("")
    rc, out, _ = psql(
        "SELECT exchange||'_'||trade_type as bucket, COUNT(*) as n, "
        "ROUND(SUM(pnl_usd)::numeric, 2) as net "
        "FROM user_trades WHERE closed_at >= NOW() - INTERVAL '7 days' "
        "AND COALESCE(metadata::jsonb->>'exit_reason','') NOT LIKE 'force_orphan%' "
        "GROUP BY exchange, trade_type ORDER BY exchange, trade_type;"
    )
    lines.append("| Bucket | n (7d) | net |")
    lines.append("|---|---:|---:|")
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 3:
                lines.append(f"| {parts[0]} | {parts[1]} | ${parts[2]} |")

OUT_FILE.write_text("\n".join(lines))
print(f"Wrote: {OUT_FILE}")
sys.exit(0)
