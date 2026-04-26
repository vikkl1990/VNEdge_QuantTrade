#!/usr/bin/env python3
"""Agent 17 NEW — Cohort Pause Surfacer (daily 09:00 UTC cron)

Surfaces silent operational state that's invisible without a DB query:
  - users.cohort_blacklist_paused_until in the future (signals blocked)
  - users.live_emergency_halt active
  - kill_switch table state
  - any unusual user state

Today (2026-04-26) found both admin and niranjan paused until tomorrow
morning — completely silent. This agent makes that visible.

Output: storage/cohort_pause/state_YYYYMMDD.md
Cron: 0 9 * * *
"""
import sys
import datetime
import pathlib
import subprocess

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
OUT_DIR = ROOT / "storage" / "cohort_pause"
TS = datetime.datetime.utcnow()
OUT_FILE = OUT_DIR / f"state_{TS.strftime('%Y%m%d')}.md"

OUT_DIR.mkdir(parents=True, exist_ok=True)


def psql(sql):
    cmd = (
        "PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge "
        f"-A -F '|' -t -c \"{sql}\""
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


lines = [
    f"# Cohort & Pause Surfacer — Agent 17",
    f"Generated: {TS.isoformat()}Z",
    "",
]

# ─── 1. Users with cohort_blacklist_paused_until in future ──────────
rc, out, _ = psql(
    "SELECT email, bot_mode, cohort_blacklist_paused_until, "
    "ROUND(EXTRACT(EPOCH FROM (cohort_blacklist_paused_until - NOW()))/3600) as hours_remaining "
    "FROM users WHERE cohort_blacklist_paused_until > NOW() ORDER BY hours_remaining DESC;"
)
lines.append("## Cohort blacklist pauses (future)")
if rc == 0 and out:
    lines.append("| User | Mode | Paused until | Hours remaining |")
    lines.append("|---|---|---|---:|")
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            lines.append(f"| {parts[0]} | {parts[1]} | {parts[2]} | {parts[3]}h |")
    lines.append("")
    lines.append("⚠️  Trading is BLOCKED for these users until pause expires.")
else:
    lines.append("✅ No users currently paused")
lines.append("")

# ─── 2. Live emergency halt ─────────────────────────────────────────
rc, out, _ = psql(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name='users' AND column_name='live_emergency_halt';"
)
if out:
    rc2, out2, _ = psql("SELECT email, bot_mode FROM users WHERE live_emergency_halt = TRUE;")
    lines.append("## Live emergency halt")
    if rc2 == 0 and out2:
        for line in out2.splitlines():
            parts = line.split("|")
            if len(parts) >= 2:
                lines.append(f"- 🔴 {parts[0]} ({parts[1]}) — LIVE HALT ON")
    else:
        lines.append("✅ No live halts active")
    lines.append("")

# ─── 3. Global kill switch ─────────────────────────────────────────
rc, out, _ = psql(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name='global_kill_switch';"
)
if out:
    rc2, out2, _ = psql(
        "SELECT engaged, reason, engaged_at FROM global_kill_switch ORDER BY id DESC LIMIT 1;"
    )
    lines.append("## Global kill switch")
    if rc2 == 0 and out2:
        parts = out2.split("|")
        if len(parts) >= 3:
            engaged = parts[0].strip().lower() == "t"
            if engaged:
                lines.append(f"- 🔴 ENGAGED: {parts[1]} (since {parts[2]})")
            else:
                lines.append(f"- ✅ CLEAR")
    lines.append("")

# ─── 4. User capital & sizing state ─────────────────────────────────
rc, out, _ = psql(
    "SELECT email, bot_mode, max_leverage, preferred_leverage, "
    "shadow_simulated_balance, max_daily_loss_pct "
    "FROM users WHERE bot_mode != 'paper' ORDER BY email;"
)
lines.append("## Active user sizing config")
if rc == 0 and out:
    lines.append("| User | Mode | maxLev | prefLev | shadow_sim_bal | maxDailyLoss% |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 6:
            lines.append(f"| {parts[0]} | {parts[1]} | {parts[2]} | {parts[3]} | ${parts[4]} | {parts[5]}% |")
lines.append("")

# ─── 5. Recent kill_switch fires (last 24h) ────────────────────────
rc, out, _ = psql(
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='kill_switch_close_audit';"
)
if out and out.strip() not in ("0", ""):
    rc2, out2, _ = psql(
        "SELECT engaged_at, reason, COUNT(*) FROM kill_switch_close_audit "
        "WHERE engaged_at >= NOW() - INTERVAL '24 hours' "
        "GROUP BY engaged_at, reason ORDER BY engaged_at DESC LIMIT 5;"
    )
    if out2:
        lines.append("## Kill-switch fires (24h)")
        for line in out2.splitlines():
            lines.append(f"- {line}")
        lines.append("")

# ─── 6. Action items ───────────────────────────────────────────────
lines.append("## Suggested actions")
lines.append("- Verify cohort pauses are intentional (most are auto-fired by 5/5 last-cohort-loss rule)")
lines.append("- If pause was triggered by today's bug-induced losses, consider clearing manually:")
lines.append("  ```sql")
lines.append("  UPDATE users SET cohort_blacklist_paused_until = NULL")
lines.append("    WHERE email IN ('admin@vnedge.com', 'niranjan_139@yahoo.co.in');")
lines.append("  ```")

OUT_FILE.write_text("\n".join(lines))
print(f"Wrote: {OUT_FILE}")
sys.exit(0)
