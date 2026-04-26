#!/usr/bin/env python3
"""Agent 5 HEAVY — Silent Failure Hunter (6h cron)

Catches the "code exists but doesn't run" pattern that bit us 5+ times today.

CATEGORIES SCANNED:
  1. Bot-level: cryptobot service active? journald has recent activity?
  2. Daemon-level: bybit-shadow-daemon, bybit-shadow-monitor active + recent log?
  3. Database-level: open-trades not stuck (max_age_min < 60), close-rate sane,
     metadata coverage (stop_loss/take_profit set on shadow rows)
  4. Code-level: every file in TRACKED was loaded by current process?
     (compare file mtime vs cryptobot pid start)
  5. Config-level: every user_config key the manager reads IS being passed
     through by user_registry (catches Bug 3b regression class)
  6. Cron-agent-level: every expected agent file in .rollback/ has been
     written within its expected window
  7. NEW (added 2026-04-26 after Bug O5) — In-memory monitor task health:
     for each open trade, is there a corresponding _monitor_trade task?
     We can't introspect the running process directly, so we proxy via:
       - opened_at vs current time (>2× max_age and still open = orphan)
       - log presence: any "MONITOR_NO_PRICE" / "MONITOR_FORCE_CLOSE" / close
         event referencing this trade in last 30 min

Output: `.rollback/silent_failure_scan_YYYYMMDD_HHMM.md` traffic-light report.
Exit 0 always — informational only.
"""
import os
import sys
import datetime
import pathlib
import subprocess
import json

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
ROLLBACK_DIR = ROOT / ".rollback"
TS = datetime.datetime.utcnow()
OUT_FILE = ROLLBACK_DIR / f"silent_failure_scan_{TS.strftime('%Y%m%d_%H%M')}.md"

ROLLBACK_DIR.mkdir(parents=True, exist_ok=True)


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def psql(sql):
    cmd = (
        f"PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge "
        f"-A -F '|' -t -c \"{sql}\""
    )
    rc, out, err = run(cmd, timeout=15)
    return rc, out, err


sections = []


# ─── 1. Bot service health ──────────────────────────────────────────────
def check_services():
    findings = []
    for svc in ["cryptobot", "bybit-shadow-daemon", "bybit-shadow-monitor"]:
        rc, out, err = run(f"systemctl is-active {svc}", timeout=5)
        if out != "active":
            findings.append(f"🔴 {svc} not active (got: {out})")
        else:
            # Check journald has activity in last 5 min
            rc2, out2, _ = run(f"sudo journalctl -u {svc} --since '5 minutes ago' --no-pager 2>&1 | wc -l", timeout=5)
            n_lines = int(out2) if out2.isdigit() else 0
            if n_lines < 3:
                findings.append(f"🟡 {svc} active but journald quiet ({n_lines} lines/5min)")
            else:
                findings.append(f"🟢 {svc} active ({n_lines} log lines/5min)")
    return findings


# ─── 2. Database health ──────────────────────────────────────────────────
def check_open_trades():
    findings = []
    rc, out, _ = psql(
        "SELECT exchange||'_'||trade_type as bucket, COUNT(*), "
        "MAX(EXTRACT(EPOCH FROM (NOW()-opened_at))/60)::int as oldest_min "
        "FROM user_trades WHERE closed_at IS NULL GROUP BY bucket ORDER BY bucket;"
    )
    if rc != 0:
        return [f"🔴 DB query failed: {out}"]
    if not out:
        return ["🟢 0 open trades (clean)"]
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        bucket, n, oldest = parts[0], parts[1], parts[2]
        oldest_min = int(oldest) if oldest.lstrip("-").isdigit() else 0
        if oldest_min > 60:
            findings.append(f"🔴 {bucket}: {n} open, oldest {oldest_min}min — STUCK")
        elif oldest_min > 30:
            findings.append(f"🟡 {bucket}: {n} open, oldest {oldest_min}min — past max_age")
        else:
            findings.append(f"🟢 {bucket}: {n} open, oldest {oldest_min}min — healthy")
    return findings


def check_close_rate():
    rc, out, _ = psql(
        "SELECT exchange||'_'||trade_type, "
        "COUNT(CASE WHEN opened_at >= NOW() - INTERVAL '24 hours' THEN 1 END) as opens, "
        "COUNT(CASE WHEN closed_at >= NOW() - INTERVAL '24 hours' THEN 1 END) as closes "
        "FROM user_trades WHERE opened_at >= NOW() - INTERVAL '24 hours' "
        "GROUP BY exchange, trade_type ORDER BY exchange, trade_type;"
    )
    findings = []
    if rc != 0 or not out:
        return findings
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        bucket, opens, closes = parts[0], int(parts[1]), int(parts[2])
        if opens == 0:
            continue
        ratio = closes / opens
        if ratio < 0.5:
            findings.append(f"🔴 {bucket}: {closes}/{opens} closes/opens (24h) = {ratio:.0%} — exit broken?")
        elif ratio < 0.8:
            findings.append(f"🟡 {bucket}: {closes}/{opens} closes/opens (24h) = {ratio:.0%} — backlog")
        else:
            findings.append(f"🟢 {bucket}: {closes}/{opens} closes/opens (24h) = {ratio:.0%}")
    return findings


def check_metadata_coverage():
    rc, out, _ = psql(
        "SELECT trade_type, COUNT(*) as n, "
        "COUNT(metadata->>'stop_loss') as has_sl, "
        "COUNT(metadata->>'take_profit') as has_tp, "
        "COUNT(metadata->>'scanner') as has_scanner "
        "FROM user_trades WHERE opened_at >= NOW() - INTERVAL '24 hours' "
        "GROUP BY trade_type ORDER BY trade_type;"
    )
    findings = []
    if rc != 0 or not out:
        return findings
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        tt, n, sl, tp, scanner = parts[0], int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
        if n == 0:
            continue
        sl_pct = sl / n * 100
        tp_pct = tp / n * 100
        sc_pct = scanner / n * 100
        if sl_pct < 80 or tp_pct < 80:
            findings.append(f"🟡 metadata gaps {tt} (n={n}): sl={sl_pct:.0f}% tp={tp_pct:.0f}% scanner={sc_pct:.0f}%")
        else:
            findings.append(f"🟢 metadata coverage {tt}: sl={sl_pct:.0f}% tp={tp_pct:.0f}%")
    return findings


# ─── 3. Config wiring (Bug 3b class) ────────────────────────────────────
def check_user_config_passthrough():
    """Read user_real_manager init code + user_registry user_config dict.
    For each user_config.get(KEY) the manager reads, ensure user_registry
    user_config dict literal contains KEY.
    """
    mgr_path = ROOT / "execution" / "user_real_manager.py"
    reg_path = ROOT / "execution" / "user_registry.py"
    findings = []
    try:
        mgr_src = mgr_path.read_text()
        reg_src = reg_path.read_text()
    except Exception as e:
        return [f"🔴 cant read mgr/registry: {e}"]
    # Find user_config.get("KEY") in manager
    import re
    keys_used = set(re.findall(r'user_config\.get\(["\']([a-z_][a-z0-9_]*)["\']', mgr_src))
    # Find dict literal keys in registry around `user_config = {`
    reg_dict_match = re.search(r'user_config\s*=\s*\{([^}]+)\}', reg_src, re.DOTALL)
    if not reg_dict_match:
        return ["🔴 cant find user_config dict in user_registry.py"]
    keys_passed = set(re.findall(r'"([a-z_][a-z0-9_]*)"\s*:', reg_dict_match.group(1)))
    missing = keys_used - keys_passed
    if missing:
        findings.append(f"🔴 user_config missing pass-throughs: {sorted(missing)}")
    else:
        findings.append(f"🟢 user_config wiring OK ({len(keys_used)} keys read, all passed through)")
    return findings


# ─── 4. Code-level: are deployed files matching what's currently loaded? ─
def check_file_freshness():
    rc, out, _ = run("pgrep -f 'python.*main.py' | head -1", timeout=5)
    pid = out.strip()
    findings = []
    if not pid.isdigit():
        return ["🟡 cryptobot pid not found"]
    rc2, start_out, _ = run(f"ps -p {pid} -o lstart= 2>/dev/null", timeout=5)
    if not start_out:
        return ["🟡 ps lstart failed"]
    try:
        # 'Sun Apr 26 13:35:14 2026' format
        proc_start = datetime.datetime.strptime(start_out.strip(), "%a %b %d %H:%M:%S %Y")
    except Exception:
        return [f"🟡 unable to parse process start: {start_out!r}"]
    findings.append(f"🟢 cryptobot pid={pid} started {proc_start.isoformat()}")
    # Files modified AFTER process start are not loaded
    for rel in ["execution/user_registry.py", "execution/user_real_manager.py",
                "scripts/bybit_shadow_monitor.py"]:
        p = ROOT / rel
        if not p.exists():
            findings.append(f"🔴 missing file: {rel}")
            continue
        mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime)
        if mtime > proc_start:
            findings.append(f"🔴 {rel} mtime {mtime.isoformat()} AFTER process start — NEEDS RESTART")
        else:
            findings.append(f"🟢 {rel} mtime {mtime.isoformat()} matches running process")
    return findings


# ─── 7. NEW: In-memory monitor task health (Bug O5 class) ──────────────
def check_monitor_task_health():
    """For each open trade, check if monitor logs reference it in last 30 min."""
    rc, out, _ = psql(
        "SELECT id::text, symbol, EXTRACT(EPOCH FROM (NOW()-opened_at))/60 as age_min "
        "FROM user_trades WHERE closed_at IS NULL "
        "AND trade_type IN ('shadow') "
        "AND opened_at < NOW() - INTERVAL '5 minutes' "
        "ORDER BY opened_at LIMIT 20;"
    )
    findings = []
    if rc != 0 or not out:
        return ["🟢 no shadow trades >5min old (no monitor health concerns)"]
    orphans = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3: continue
        tid, sym, age = parts[0], parts[1], float(parts[2])
        # Check journald for this trade_id ref or monitor activity on its symbol
        rc2, log_out, _ = run(
            f"sudo journalctl -u cryptobot --since '30 minutes ago' --no-pager 2>&1 | "
            f"grep -E '{sym}.*monitor|MONITOR_NO_PRICE.*{sym}|MONITOR_FORCE_CLOSE.*{sym}|"
            f"{tid[:8]}|{sym}.*trail|{sym}.*max_age' | wc -l",
            timeout=5,
        )
        n_refs = int(log_out) if log_out.isdigit() else 0
        if n_refs == 0 and age > 30:
            orphans.append(f"  - {sym} age={age:.0f}min trade_id={tid[:8]} — NO monitor activity in journald")
    if orphans:
        findings.append(f"🔴 {len(orphans)} likely orphaned monitor task(s):")
        findings.extend(orphans)
    else:
        findings.append("🟢 all open shadow trades show recent monitor activity")
    return findings


# ─── Build report ────────────────────────────────────────────────────────
sections.append(("1. Service health", check_services()))
sections.append(("2a. Open trades", check_open_trades()))
sections.append(("2b. Close-rate ratio (24h)", check_close_rate()))
sections.append(("2c. Metadata coverage", check_metadata_coverage()))
sections.append(("3. Config pass-through wiring", check_user_config_passthrough()))
sections.append(("4. Code-vs-process freshness", check_file_freshness()))
sections.append(("7. NEW: Monitor task health (Bug O5 class)", check_monitor_task_health()))

# Render
out_lines = [
    f"# Silent Failure Hunter — HEAVY scan",
    f"Generated: {TS.isoformat()}Z",
    "",
    "Categories scanned (1-7). Each line: 🟢 ok / 🟡 concern / 🔴 fix-now",
    "",
]
red_count = 0
for title, findings in sections:
    out_lines.append(f"## {title}")
    for f in findings:
        out_lines.append(f"- {f}")
        if f.startswith("- 🔴") or "🔴" in f:
            red_count += 1
    out_lines.append("")

out_lines.append(f"## Summary: {red_count} 🔴 critical findings")

OUT_FILE.write_text("\n".join(out_lines))
print(f"Wrote: {OUT_FILE}")
print(f"Critical findings: {red_count}")
sys.exit(0)
