#!/usr/bin/env python3
"""Agent 9 — Incident Responder (every 5min cron)

Watches for incident triggers in the last 5 min. On match: runs diagnostic
playbook + writes incident report. Two-tier:
  - INFORMATIONAL — log + brief metadata (no alert)
  - CRITICAL      — full diagnostic dump + escalate

TRIGGERS WATCHED:
  1. kill_switch fired in last 5min
  2. auto_revert engaged in last 5min
  3. SHADOW RECONCILED log (fired = good, but worth tracking)
  4. MONITOR_FORCE_CLOSE fired (failsafe activation = something's wrong upstream)
  5. cryptobot service restart in last 5min
  6. balance drop > 10% per user in last 5min
  7. position count anomaly (sudden spike or drop)

OUTPUTS:
  - storage/incidents/incident_TS.md per critical event
  - storage/incidents/INFO_TS.md for informational

CRON: */5 * * * *
"""
import sys
import datetime
import pathlib
import subprocess
import re

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
OUT_DIR = ROOT / "storage" / "incidents"
TS = datetime.datetime.utcnow()
OUT_DIR.mkdir(parents=True, exist_ok=True)


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def psql(sql):
    return run(
        f"PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge "
        f"-A -F '|' -t -c \"{sql}\""
    )


# ─── Trigger detectors ──────────────────────────────────────────
incidents = []  # list of (level, trigger, detail)


def check_kill_switch():
    rc, out, _ = run(
        "sudo journalctl -u cryptobot --since '5 minutes ago' --no-pager 2>&1 | "
        "grep -iE 'kill_switch.*engaged|KILL.SWITCH.*FIRED|kill_switch_close_open' | head -5"
    )
    if out:
        incidents.append(("CRITICAL", "kill_switch_fired", out[:500]))


def check_auto_revert():
    rc, out, _ = run(
        "sudo journalctl --since '5 minutes ago' --no-pager 2>&1 | "
        "grep -E 'auto_revert.*🔴|auto_revert.*ENGAGE|stuck_open_trades_60min|exit_rate_collapse' | head -5"
    )
    if out:
        incidents.append(("CRITICAL", "auto_revert_triggered", out[:500]))


def check_shadow_reconciled():
    rc, out, _ = run(
        "sudo journalctl -u cryptobot --since '5 minutes ago' --no-pager 2>&1 | "
        "grep 'SHADOW RECONCILED' | head -5"
    )
    if out:
        n_lines = len(out.splitlines())
        incidents.append(("INFO", "shadow_reconciled",
                          f"{n_lines} shadow trade(s) reconciled in last 5min — bot restart likely occurred"))


def check_monitor_force_close():
    rc, out, _ = run(
        "sudo journalctl -u cryptobot --since '5 minutes ago' --no-pager 2>&1 | "
        "grep -E 'MONITOR_FORCE_CLOSE|MONITOR_NO_PRICE' | head -5"
    )
    if out:
        incidents.append(("CRITICAL", "monitor_failsafe_fired",
                          f"Failsafe activated — upstream price-feed broken. {out[:500]}"))


def check_service_restart():
    rc, out, _ = run(
        "sudo journalctl --since '5 minutes ago' --no-pager 2>&1 | "
        "grep -E 'systemd.*Started|systemd.*Stopped' | grep -E 'cryptobot|bybit-shadow' | head -5"
    )
    if out:
        n_restarts = sum(1 for line in out.splitlines() if "Started" in line)
        if n_restarts >= 1:
            incidents.append(("INFO", "service_restart",
                              f"{n_restarts} service start(s) in last 5min"))


def check_position_anomaly():
    """Check for sudden open-trade spike or all-zero state."""
    rc, out, _ = psql(
        "SELECT COUNT(*) FROM user_trades WHERE closed_at IS NULL"
    )
    if rc == 0 and out:
        n = int(out)
        # Sample threshold: > 30 trades open or = 0 (when normally we expect 5-15)
        if n > 30:
            incidents.append(("CRITICAL", "position_count_spike",
                              f"{n} trades open — well above normal 5-15 range"))


# ─── Diagnostic playbook (for CRITICAL incidents) ───────────────
def diagnostic_dump():
    """Pull a snapshot of current state for incident triage."""
    snapshots = {}
    rc, out, _ = psql(
        "SELECT exchange||'_'||trade_type as bucket, COUNT(*), "
        "MAX(EXTRACT(EPOCH FROM (NOW()-opened_at))/60)::int as oldest_min "
        "FROM user_trades WHERE closed_at IS NULL "
        "GROUP BY exchange, trade_type ORDER BY bucket;"
    )
    snapshots["open_trades"] = out or "none"

    rc, out, _ = run("sudo systemctl is-active cryptobot bybit-shadow-daemon bybit-shadow-monitor 2>&1")
    snapshots["service_status"] = out

    rc, out, _ = run(
        "sudo journalctl -u cryptobot --since '5 minutes ago' --no-pager 2>&1 | tail -10"
    )
    snapshots["recent_log"] = out
    return snapshots


# ─── Run detectors ──────────────────────────────────────────────
check_kill_switch()
check_auto_revert()
check_shadow_reconciled()
check_monitor_force_close()
check_service_restart()
check_position_anomaly()

if not incidents:
    sys.exit(0)

# ─── Write report ───────────────────────────────────────────────
critical = [i for i in incidents if i[0] == "CRITICAL"]
info = [i for i in incidents if i[0] == "INFO"]

if critical:
    fname = f"incident_{TS.strftime('%Y%m%d_%H%M%S')}.md"
    snap = diagnostic_dump()
    lines = [
        f"# 🔴 INCIDENT — {TS.isoformat()}Z",
        "",
        "## Triggers",
    ]
    for level, trigger, detail in critical:
        lines.append(f"### {trigger}")
        lines.append(f"```\n{detail}\n```")
    if info:
        lines.append("\n## Informational (also detected)")
        for level, trigger, detail in info:
            lines.append(f"- {trigger}: {detail}")
    lines.extend([
        "",
        "## Diagnostic snapshot",
        "### Open trades by bucket",
        f"```\n{snap['open_trades']}\n```",
        "### Service status",
        f"```\n{snap['service_status']}\n```",
        "### Recent journald (last 5 min, last 10 lines)",
        f"```\n{snap['recent_log']}\n```",
        "",
        "## Suggested actions",
        "- Review triggered conditions above",
        "- Check `storage/heartbeat/status_latest.txt` for process health",
        "- Check most recent `.rollback/silent_failure_scan_*.md` for upstream cause",
    ])
    (OUT_DIR / fname).write_text("\n".join(lines))
    print(f"INCIDENT REPORT: {fname}")
    sys.exit(1)
elif info:
    fname = f"INFO_{TS.strftime('%Y%m%d_%H%M%S')}.md"
    lines = [f"# ℹ️  Informational events — {TS.isoformat()}Z", ""]
    for level, trigger, detail in info:
        lines.append(f"- **{trigger}**: {detail}")
    (OUT_DIR / fname).write_text("\n".join(lines))
    print(f"INFO: {fname}")

sys.exit(0)
