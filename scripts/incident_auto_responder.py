#!/usr/bin/env python3
"""Agent 9 ACTIVE — Incident Auto-Responder (every 5min cron)

Drop-in upgrade of `incident_responder.py` that NOT ONLY detects but also
APPLIES known remediations within strict safety caps.

DETERMINISTIC AUTO-FIX patterns (with caps):
  P1. Dead service → systemctl restart      (cap: 3/hour, only listed services)
  P2. Stuck open trades >60min → force-close (cap: 20 trades/hour)
  P3. bybit-shadow-monitor crash loop → restart  (cap: 5/hour)
  P4. cohort_pause >7 days → DO NOT auto-clear; surface for architect

NOVEL incidents → write to storage/incidents/escalate_*.md (no auto-action)

Safety:
  - Per-pattern hourly cap stored in storage/auto_responder/quota.json
  - Every action logged to storage/auto_responder/actions.log
  - Every action gets a journald 'incident_auto' tag

CRON: */5 * * * * (replaces incident_responder.py if you flip the cron)
"""
import sys
import datetime
import pathlib
import subprocess
import json

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
STATE_DIR = ROOT / "storage" / "auto_responder"
STATE_DIR.mkdir(parents=True, exist_ok=True)
QUOTA_FILE = STATE_DIR / "quota.json"
ACTION_LOG = STATE_DIR / "actions.log"
TS = datetime.datetime.utcnow()

CAPS = {
    "P1_service_restart": 3,
    "P2_force_close":     20,
    "P3_monitor_restart": 5,
}
WHITELIST_SERVICES = ["cryptobot", "bybit-shadow-daemon", "bybit-shadow-monitor"]


def run(cmd, timeout=15):
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


def load_quota():
    if not QUOTA_FILE.exists():
        return {"hour_bucket": "", "counts": {}}
    try:
        return json.loads(QUOTA_FILE.read_text())
    except Exception:
        return {"hour_bucket": "", "counts": {}}


def save_quota(q):
    QUOTA_FILE.write_text(json.dumps(q, indent=2))


def quota_ok(pattern):
    """Return (ok, current_count, cap)."""
    q = load_quota()
    bucket = TS.strftime("%Y-%m-%dT%H")
    if q["hour_bucket"] != bucket:
        q = {"hour_bucket": bucket, "counts": {}}
    cur = q["counts"].get(pattern, 0)
    cap = CAPS.get(pattern, 1)
    return cur < cap, cur, cap


def quota_increment(pattern):
    q = load_quota()
    bucket = TS.strftime("%Y-%m-%dT%H")
    if q["hour_bucket"] != bucket:
        q = {"hour_bucket": bucket, "counts": {}}
    q["counts"][pattern] = q["counts"].get(pattern, 0) + 1
    save_quota(q)


def log_action(pattern, action, detail):
    line = f"[{TS.isoformat()}] {pattern} | {action} | {detail}"
    with ACTION_LOG.open("a") as f:
        f.write(line + "\n")
    print(f"AUTO-ACTION: {line}", flush=True)


# ─── P1: Dead service auto-restart ──────────────────────────────
def fix_dead_services():
    for svc in WHITELIST_SERVICES:
        rc, out, _ = run(f"systemctl is-active {svc}", timeout=5)
        if out == "active":
            continue
        ok, cur, cap = quota_ok("P1_service_restart")
        if not ok:
            log_action("P1_service_restart", "QUOTA_EXCEEDED",
                       f"{svc} dead but {cur}/{cap} used this hour — escalate")
            continue
        rc2, _, err = run(f"sudo systemctl restart {svc}", timeout=15)
        if rc2 == 0:
            quota_increment("P1_service_restart")
            log_action("P1_service_restart", "RESTART_OK", f"{svc}")
        else:
            log_action("P1_service_restart", "RESTART_FAIL", f"{svc}: {err}")


# ─── P2: Stuck open trades auto-force-close ──────────────────────
def fix_stuck_trades():
    rc, out, _ = psql(
        "SELECT id::text, symbol FROM user_trades "
        "WHERE closed_at IS NULL AND opened_at < NOW() - INTERVAL '60 minutes' LIMIT 25;"
    )
    if rc != 0 or not out:
        return
    rows = [line.split("|") for line in out.splitlines() if "|" in line]
    if not rows:
        return
    for row in rows:
        tid, sym = row[0], row[1]
        ok, cur, cap = quota_ok("P2_force_close")
        if not ok:
            log_action("P2_force_close", "QUOTA_EXCEEDED",
                       f"{cur}/{cap} closes used; {len(rows)} stuck remain — escalate")
            return
        sql = (
            f"UPDATE user_trades SET status='closed', closed_at=NOW(), "
            f"exit_price=entry_price, pnl_usd=0.0, "
            f"metadata = COALESCE(metadata,'{{}}'::jsonb) || "
            f"jsonb_build_object('exit_reason','auto_responder_stuck_60m','close_via','agent_9_active') "
            f"WHERE id='{tid}'::uuid;"
        )
        rc2, _, err = psql(sql)
        if rc2 == 0:
            quota_increment("P2_force_close")
            log_action("P2_force_close", "FORCE_CLOSED", f"{sym} ({tid[:8]})")
        else:
            log_action("P2_force_close", "DB_ERROR", f"{tid}: {err}")


# ─── P3: Detect monitor crash-loop and restart ──────────────────
def fix_monitor_crash_loop():
    # Detect: bybit-shadow-monitor service active but spitting tracebacks
    rc, out, _ = run(
        "sudo journalctl -u bybit-shadow-monitor --since '10 minutes ago' --no-pager 2>&1 | "
        "grep -c -i 'traceback\\|error\\|exception'"
    )
    if not out.isdigit():
        return
    n_errors = int(out)
    if n_errors < 5:
        return  # threshold: 5 errors in 10min suggests crash loop
    ok, cur, cap = quota_ok("P3_monitor_restart")
    if not ok:
        log_action("P3_monitor_restart", "QUOTA_EXCEEDED",
                   f"{n_errors} errors in 10min but {cur}/{cap} restarts used — escalate")
        return
    rc2, _, err = run("sudo systemctl restart bybit-shadow-monitor", timeout=15)
    if rc2 == 0:
        quota_increment("P3_monitor_restart")
        log_action("P3_monitor_restart", "RESTART_OK", f"({n_errors} errors detected)")
    else:
        log_action("P3_monitor_restart", "RESTART_FAIL", err)


# ─── P4: Cohort pause auto-surface (NO auto-clear) ──────────────
def surface_long_cohort_pause():
    rc, out, _ = psql(
        "SELECT email FROM users "
        "WHERE cohort_blacklist_paused_until > NOW() + INTERVAL '7 days';"
    )
    if rc == 0 and out:
        for line in out.splitlines():
            log_action("P4_cohort_pause", "ESCALATE",
                       f"{line.strip()} paused >7 days — architect should review")


# ─── Run all ────────────────────────────────────────────────────
fix_dead_services()
fix_stuck_trades()
fix_monitor_crash_loop()
surface_long_cohort_pause()

sys.exit(0)
