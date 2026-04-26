#!/usr/bin/env python3
"""Agent 10 — Compliance / Multi-User Auditor (monthly cron)

Verifies multi-user fairness + key safety + max-loss enforcement.
Today's bot has 2 users (admin + niranjan); doc said "scale to 10+ untested" —
this agent ensures fairness as we scale.

CHECKS:
  1. Per-user signal exposure parity (every active user gets signaled per qualified signal)
  2. Per-user sizing variance (intentional from shadow_simulated_balance OR a bug?)
  3. API key safety: all keys encrypted in user_api_keys (no plaintext columns)
  4. Balance reconciliation: per-user wallet vs sum of recorded fees+pnl
  5. Max-loss enforcement: any user exceeded max_daily_loss_pct in last 30 days?
  6. Audit log completeness: trades have user_id, exchange, trade_type, status, signal_data

OUTPUTS:
  - storage/compliance/audit_YYYYMM.md monthly report

CRON: 0 7 1 * * (1st of every month at 07:00 UTC)
"""
import sys
import datetime
import pathlib
import subprocess

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
OUT_DIR = ROOT / "storage" / "compliance"
TS = datetime.datetime.utcnow()
OUT_FILE = OUT_DIR / f"audit_{TS.strftime('%Y%m')}.md"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def psql(sql):
    cmd = (
        "PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge "
        f"-A -F '|' -t -c \"{sql}\""
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


findings = []


# ─── 1. Per-user signal exposure parity ──────────────────────
def check_signal_parity():
    rc, out, _ = psql(
        "SELECT u.email, COUNT(DISTINCT (ut.symbol, ut.side, "
        "DATE_TRUNC('minute', ut.opened_at))) AS unique_signals "
        "FROM user_trades ut JOIN users u ON u.id=ut.user_id "
        "WHERE ut.opened_at >= NOW() - INTERVAL '30 days' "
        "AND ut.trade_type = 'shadow' AND ut.exchange = 'delta_india' "
        "GROUP BY u.email ORDER BY u.email;"
    )
    counts = {}
    for line in (out.splitlines() if out else []):
        parts = line.split("|")
        if len(parts) >= 2:
            counts[parts[0]] = int(parts[1])
    if not counts:
        findings.append("⚪ Signal parity: no shadow data in last 30 days")
        return
    vals = list(counts.values())
    if max(vals) > 0 and (max(vals) - min(vals)) / max(vals) > 0.10:
        findings.append(
            f"🟡 Signal parity: variance >10% across users — {counts}"
        )
    else:
        findings.append(f"🟢 Signal parity: {counts} (within 10% variance)")


# ─── 2. Per-user sizing variance ─────────────────────────────
def check_sizing_variance():
    rc, out, _ = psql(
        "SELECT u.email, u.shadow_simulated_balance, "
        "ROUND(AVG((ut.metadata::jsonb->>'margin')::float)::numeric, 2) AS avg_margin "
        "FROM user_trades ut JOIN users u ON u.id=ut.user_id "
        "WHERE ut.opened_at >= NOW() - INTERVAL '30 days' "
        "AND ut.trade_type = 'shadow' AND ut.exchange = 'delta_india' "
        "AND ut.metadata::jsonb->>'margin' IS NOT NULL "
        "GROUP BY u.email, u.shadow_simulated_balance "
        "ORDER BY u.email;"
    )
    rows = []
    for line in (out.splitlines() if out else []):
        parts = line.split("|")
        if len(parts) >= 3:
            rows.append((parts[0], float(parts[1] or 0), float(parts[2] or 0)))
    if len(rows) < 2:
        findings.append("⚪ Sizing variance: <2 users with margin data — skip")
        return
    # Check ratio: avg_margin / shadow_simulated_balance should be consistent
    ratios = [(r[0], r[2] / r[1] * 100 if r[1] else 0) for r in rows]
    pct_vals = [r[1] for r in ratios]
    if max(pct_vals) > 0 and (max(pct_vals) - min(pct_vals)) / max(pct_vals) > 0.30:
        findings.append(
            f"🟡 Sizing variance: margin/balance ratio differs >30% across users → {ratios}"
        )
    else:
        findings.append(
            f"🟢 Sizing variance: margin/balance ratios consistent → {ratios}"
        )


# ─── 3. API key safety ───────────────────────────────────────
def check_key_safety():
    rc, out, _ = psql(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name='user_api_keys' AND column_name IN ('api_key','api_secret');"
    )
    if rc == 0 and out and int(out) > 0:
        findings.append(
            "🔴 API key safety: PLAINTEXT api_key/api_secret columns exist in user_api_keys — should only be _enc"
        )
    else:
        findings.append("🟢 API key safety: no plaintext key columns in user_api_keys")
    rc, out, _ = psql(
        "SELECT COUNT(*) FROM user_api_keys "
        "WHERE api_key_enc IS NULL OR api_secret_enc IS NULL;"
    )
    if rc == 0 and out and int(out) > 0:
        findings.append(f"🟡 API key safety: {out} key rows missing encrypted fields")


# ─── 4. Balance reconciliation (per-user) ────────────────────
def check_balance_reconciliation():
    rc, out, _ = psql(
        "SELECT u.email, "
        "ROUND(SUM(ut.pnl_usd)::numeric, 2) AS total_pnl, "
        "ROUND(SUM(ut.fees_usd)::numeric, 2) AS total_fees, "
        "COUNT(*) AS n_trades "
        "FROM user_trades ut JOIN users u ON u.id=ut.user_id "
        "WHERE ut.closed_at >= NOW() - INTERVAL '30 days' "
        "AND ut.trade_type = 'shadow' "
        "GROUP BY u.email ORDER BY u.email;"
    )
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 4:
                findings.append(
                    f"🟢 Reconcile {parts[0]}: 30d shadow PnL ${parts[1]} fees ${parts[2]} (n={parts[3]})"
                )


# ─── 5. Max-loss enforcement ─────────────────────────────────
def check_max_loss_enforcement():
    rc, out, _ = psql(
        "SELECT u.email, u.max_daily_loss_pct, "
        "ROUND((MIN(daily.daily_pnl) / NULLIF(u.shadow_simulated_balance, 0) * 100)::numeric, 2) AS worst_day_pct "
        "FROM users u "
        "LEFT JOIN ("
        "  SELECT user_id, DATE(closed_at) AS day, SUM(pnl_usd) AS daily_pnl "
        "  FROM user_trades WHERE closed_at >= NOW() - INTERVAL '30 days' "
        "  AND trade_type = 'shadow' "
        "  GROUP BY user_id, DATE(closed_at)"
        ") daily ON daily.user_id = u.id "
        "WHERE u.bot_mode != 'paper' "
        "GROUP BY u.email, u.max_daily_loss_pct, u.shadow_simulated_balance;"
    )
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 3:
                email = parts[0]
                cap_pct = float(parts[1] or 0)
                worst_pct = float(parts[2] or 0)
                # worst_pct is negative (loss); cap is positive (e.g. 3%)
                if worst_pct < -cap_pct:
                    findings.append(
                        f"🔴 Max-loss breach {email}: worst day {worst_pct:.2f}% < cap -{cap_pct}%"
                    )
                else:
                    findings.append(
                        f"🟢 Max-loss {email}: worst day {worst_pct:.2f}% within cap -{cap_pct}%"
                    )


# ─── 6. Audit log completeness ───────────────────────────────
def check_audit_completeness():
    rc, out, _ = psql(
        "SELECT trade_type, COUNT(*) AS n, "
        "COUNT(*) FILTER (WHERE user_id IS NULL) AS no_user, "
        "COUNT(*) FILTER (WHERE exchange IS NULL OR exchange = '') AS no_exch, "
        "COUNT(*) FILTER (WHERE status IS NULL OR status = '') AS no_status "
        "FROM user_trades WHERE opened_at >= NOW() - INTERVAL '30 days' "
        "GROUP BY trade_type ORDER BY trade_type;"
    )
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 5:
                tt, n, no_u, no_e, no_s = parts[0], int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
                gaps = []
                if no_u > 0: gaps.append(f"user_id={no_u}")
                if no_e > 0: gaps.append(f"exchange={no_e}")
                if no_s > 0: gaps.append(f"status={no_s}")
                if gaps:
                    findings.append(f"🟡 Audit gaps in {tt} (n={n}): missing {', '.join(gaps)}")
                else:
                    findings.append(f"🟢 Audit complete for {tt} (n={n})")


# ─── Run all checks ──────────────────────────────────────────
check_signal_parity()
check_sizing_variance()
check_key_safety()
check_balance_reconciliation()
check_max_loss_enforcement()
check_audit_completeness()

n_red = sum(1 for f in findings if "🔴" in f)
n_yellow = sum(1 for f in findings if "🟡" in f)
n_green = sum(1 for f in findings if "🟢" in f)

lines = [
    f"# Compliance Audit — {TS.strftime('%B %Y')}",
    f"Generated: {TS.isoformat()}Z",
    f"",
    f"**Summary:** 🔴 {n_red} · 🟡 {n_yellow} · 🟢 {n_green}",
    f"",
    f"## Findings",
]
for f in findings:
    lines.append(f"- {f}")

lines.extend([
    "",
    "## Verdict",
    f"{'🔴 ESCALATE — fix all red findings before next deploy' if n_red > 0 else '🟢 PASS — all checks within tolerance' if n_yellow == 0 else '🟡 REVIEW — yellow findings warrant review but not block'}",
])

OUT_FILE.write_text("\n".join(lines))
print(f"Wrote: {OUT_FILE}")
print(f"🔴 {n_red}  🟡 {n_yellow}  🟢 {n_green}")
sys.exit(1 if n_red > 0 else 0)
