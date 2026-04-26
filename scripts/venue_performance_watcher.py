#!/usr/bin/env python3
"""Agent 15 NEW — Venue Performance Watcher (6h cron)

Mission: For every paired (delta_shadow, bybit_shadow) tuple in last 24h,
compute per-venue PnL and surface significant gaps. Today's data:
Bybit shadow PF=3.44 vs Delta shadow PF=0.09 — 38× gap. Without this agent
we discovered it manually after 8 hours of bug-bashing.

Output: storage/venue_perf/venue_gap_YYYYMMDD_HHMM.md
Verdict thresholds:
  🟢 OK         — both venues PF > 1.0 AND best/worst PF ratio < 2.0
  🟡 CONCERN    — best/worst ratio between 2-5, OR worst PF < 1.0 (n>=20)
  🔴 ESCALATE   — best/worst ratio > 5, OR one venue PF < 0.5 with n>=20

Cron: 0 */6 * * *
"""
import sys
import datetime
import pathlib
import subprocess

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
OUT_DIR = ROOT / "storage" / "venue_perf"
TS = datetime.datetime.utcnow()
OUT_FILE = OUT_DIR / f"venue_gap_{TS.strftime('%Y%m%d_%H%M')}.md"

OUT_DIR.mkdir(parents=True, exist_ok=True)


def psql(sql, fmt="-A -F '|' -t"):
    cmd = (
        f"PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge "
        f"{fmt} -c \"{sql}\""
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


# ─── 1. Per-venue 24h aggregate (all venues) ──────────────────────────
def per_venue_aggregate():
    sql = (
        "SELECT exchange||'_'||trade_type, COUNT(*) AS n, "
        "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) AS wins, "
        "ROUND(SUM(pnl_usd)::numeric, 2) AS net, "
        "ROUND((SUM(CASE WHEN pnl_usd>0 THEN pnl_usd ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN pnl_usd<0 THEN -pnl_usd ELSE 0 END), 0))::numeric, 2) AS pf "
        "FROM user_trades WHERE closed_at >= NOW() - INTERVAL '24 hours' "
        "AND COALESCE(metadata::jsonb->>'exit_reason','') NOT LIKE 'force_orphan%' "
        "GROUP BY exchange, trade_type ORDER BY exchange, trade_type;"
    )
    rc, out, _ = psql(sql)
    rows = []
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 5:
                bucket, n, wins, net, pf = parts[0], int(parts[1]), int(parts[2]), parts[3], parts[4]
                rows.append({"bucket": bucket, "n": n, "wins": wins,
                             "net": float(net) if net else 0.0,
                             "pf": float(pf) if pf and pf != "" else None})
    return rows


# ─── 2. Paired same-signal comparison (bybit vs delta) ─────────────────
def paired_gap():
    sql = (
        "WITH paired AS ("
        " SELECT b.symbol, "
        "  ROUND(b.pnl_usd::numeric, 3) AS bybit_pnl, "
        "  ROUND(d.pnl_usd::numeric, 3) AS delta_pnl "
        " FROM user_trades b JOIN user_trades d "
        "  ON d.id::text = (b.metadata::jsonb->>'mirror_of_delta_trade_id') "
        " WHERE b.exchange='bybit' AND b.trade_type='shadow' "
        "   AND b.closed_at IS NOT NULL AND d.closed_at IS NOT NULL "
        "   AND b.closed_at >= NOW() - INTERVAL '24 hours' "
        "   AND COALESCE(b.metadata::jsonb->>'exit_reason','') NOT LIKE 'force_orphan%') "
        "SELECT symbol, COUNT(*) AS n, "
        "ROUND(SUM(bybit_pnl)::numeric, 2) AS bybit_total, "
        "ROUND(SUM(delta_pnl)::numeric, 2) AS delta_total, "
        "ROUND((SUM(bybit_pnl) - SUM(delta_pnl))::numeric, 2) AS gap "
        "FROM paired GROUP BY symbol ORDER BY gap DESC;"
    )
    rc, out, _ = psql(sql)
    rows = []
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 5:
                rows.append({
                    "symbol": parts[0], "n": int(parts[1]),
                    "bybit": float(parts[2]) if parts[2] else 0.0,
                    "delta": float(parts[3]) if parts[3] else 0.0,
                    "gap": float(parts[4]) if parts[4] else 0.0,
                })
    return rows


# ─── 3. Verdict logic ──────────────────────────────────────────────────
def verdict(agg):
    shadow_pfs = [r for r in agg if "shadow" in r["bucket"] and r["n"] >= 20 and r["pf"] is not None]
    if len(shadow_pfs) < 2:
        return "🟡 INSUFFICIENT", "Need ≥2 shadow buckets with n≥20 to compare"
    best = max(shadow_pfs, key=lambda r: r["pf"])
    worst = min(shadow_pfs, key=lambda r: r["pf"])
    if worst["pf"] < 0.5 and worst["n"] >= 20:
        return "🔴 ESCALATE", f"{worst['bucket']} PF={worst['pf']} (catastrophic), {best['bucket']} PF={best['pf']}"
    if best["pf"] / max(0.01, worst["pf"]) > 5:
        return "🔴 ESCALATE", f"{best['bucket']}/PF{best['pf']} vs {worst['bucket']}/PF{worst['pf']} = ratio>{5}"
    if worst["pf"] < 1.0:
        return "🟡 CONCERN", f"{worst['bucket']} PF={worst['pf']} (under-water), {best['bucket']} PF={best['pf']}"
    if best["pf"] / max(0.01, worst["pf"]) > 2:
        return "🟡 CONCERN", f"{best['bucket']}/PF{best['pf']} vs {worst['bucket']}/PF{worst['pf']} = ratio>2"
    return "🟢 OK", f"All shadow venues PF>1, ratio<2"


# ─── Render ────────────────────────────────────────────────────────────
agg = per_venue_aggregate()
pairs = paired_gap()
status, summary = verdict(agg)

lines = [
    f"# Venue Performance Watcher — Agent 15",
    f"Generated: {TS.isoformat()}Z",
    f"",
    f"## Verdict: {status}",
    f"{summary}",
    f"",
    f"## Per-venue 24h aggregate",
    f"| Bucket | n | wins | WR | net | PF |",
    f"|---|---:|---:|---:|---:|---:|",
]
for r in agg:
    wr = (r["wins"] / r["n"] * 100.0) if r["n"] else 0.0
    pf_str = f"{r['pf']}" if r["pf"] is not None else "—"
    lines.append(f"| {r['bucket']} | {r['n']} | {r['wins']} | {wr:.1f}% | ${r['net']} | {pf_str} |")

lines.extend([
    "",
    "## Per-symbol paired gap (bybit minus delta on same signals)",
    "| Symbol | n | bybit | delta | gap | flag |",
    "|---|---:|---:|---:|---:|---|",
])
for r in pairs:
    flag = ""
    if r["n"] >= 5 and r["gap"] > 20:
        flag = "✅ Bybit win"
    elif r["n"] >= 5 and r["gap"] < -10:
        flag = "🔴 Bybit losing"
    lines.append(f"| {r['symbol']} | {r['n']} | ${r['bybit']} | ${r['delta']} | ${r['gap']} | {flag} |")

lines.extend([
    "",
    "## Recommended actions",
])
if status.startswith("🔴"):
    lines.append("- 🔴 Worst-performing venue should be DISABLED for live trading until investigated")
    lines.append("- 🔴 Audit fill simulation in `_close_shadow` for the losing venue")
elif status.startswith("🟡"):
    lines.append("- 🟡 Investigate per-symbol breakdown above for the underperforming symbols")
    lines.append("- 🟡 Consider per-symbol routing — best venue per symbol")
else:
    lines.append("- 🟢 Continue dual-venue shadow trading; no action needed")

OUT_FILE.write_text("\n".join(lines))
print(f"Wrote: {OUT_FILE}")
print(f"Verdict: {status}")
sys.exit(0)
