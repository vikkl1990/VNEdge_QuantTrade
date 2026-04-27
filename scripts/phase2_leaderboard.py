#!/usr/bin/env python3
"""Phase 2 — Shadow-of-Shadow leaderboard.

Reads phase2 virtual trades from user_trades, groups by exit_config_id,
computes WR/Net/Avg/PF per config, sorts by Net.

USAGE:
  python3 scripts/phase2_leaderboard.py [--hours 24]

OUTPUTS:
  storage/phase2/leaderboard_TS.md
"""
import sys
import argparse
import datetime
import pathlib
import subprocess

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
OUT_DIR = ROOT / "storage" / "phase2"
TS = datetime.datetime.utcnow()
OUT_DIR.mkdir(parents=True, exist_ok=True)


def psql(sql):
    cmd = (
        "PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge "
        f"-A -F '|' -t -c \"{sql}\""
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()

    sql = (
        "SELECT "
        "metadata::jsonb->>'exit_config_id' as cfg_id, "
        "u.email, "
        "COUNT(*) as n, "
        "SUM(CASE WHEN ut.pnl_usd>0 THEN 1 ELSE 0 END) as wins, "
        "ROUND(100.0*SUM(CASE WHEN ut.pnl_usd>0 THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*),0)::numeric, 1) as wr_pct, "
        "ROUND(SUM(ut.pnl_usd)::numeric, 2) as net, "
        "ROUND(AVG(ut.pnl_usd)::numeric, 3) as avg_pnl, "
        "ROUND(SUM(ut.fees_usd)::numeric, 2) as fees, "
        "ROUND((SUM(CASE WHEN ut.pnl_usd>0 THEN ut.pnl_usd ELSE 0 END) / NULLIF(SUM(CASE WHEN ut.pnl_usd<0 THEN -ut.pnl_usd ELSE 0 END),0))::numeric, 2) as pf, "
        "ROUND(AVG(EXTRACT(EPOCH FROM (ut.closed_at-ut.opened_at))/60)::numeric, 1) as avg_dur_min "
        "FROM user_trades ut JOIN users u ON u.id=ut.user_id "
        f"WHERE ut.closed_at >= NOW() - INTERVAL '{args.hours} hours' "
        "AND ut.metadata::jsonb->>'is_phase2_virtual' = 'true' "
        "GROUP BY metadata::jsonb->>'exit_config_id', u.email "
        "ORDER BY metadata::jsonb->>'exit_config_id', u.email"
    )
    rc, out, _ = psql(sql)

    lines = [
        f"# Phase 2 Leaderboard — last {args.hours}h",
        f"Generated: {TS.isoformat()}Z",
        "",
    ]

    if not out:
        lines.append("**No phase 2 virtual trades found in window.**")
        lines.append("")
        lines.append("Check: cryptobot service has `Environment=PHASE2_SOS_ENABLED=true`?")
        lines.append("Check: `sudo systemctl show cryptobot | grep PHASE2`")
        lines.append("")
        out_file = OUT_DIR / f"leaderboard_{TS.strftime('%Y%m%d_%H%M%S')}.md"
        out_file.write_text("\n".join(lines))
        print(f"Wrote (empty): {out_file}")
        return 0

    lines.append("## Per-config × user breakdown")
    lines.append("")
    lines.append("| Config | User | n | wins | WR | Net | Avg | Fees | PF | Avg dur (min) |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 10:
            continue
        cfg, email, n, wins, wr, net, avg, fees, pf, dur = parts[:10]
        lines.append(f"| `{cfg}` | {email.split('@')[0]} | {n} | {wins} | {wr}% | ${net} | ${avg} | ${fees} | {pf} | {dur} |")

    # Aggregate by cfg only (sum across users)
    lines.extend(["", "## Per-config aggregate (admin + niranjan combined)"])
    sql_agg = (
        "SELECT "
        "metadata::jsonb->>'exit_config_id' as cfg_id, "
        "COUNT(*) as n, "
        "SUM(CASE WHEN ut.pnl_usd>0 THEN 1 ELSE 0 END) as wins, "
        "ROUND(100.0*SUM(CASE WHEN ut.pnl_usd>0 THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*),0)::numeric, 1) as wr_pct, "
        "ROUND(SUM(ut.pnl_usd)::numeric, 2) as net, "
        "ROUND(AVG(ut.pnl_usd)::numeric, 3) as avg_pnl, "
        "ROUND((SUM(CASE WHEN ut.pnl_usd>0 THEN ut.pnl_usd ELSE 0 END) / NULLIF(SUM(CASE WHEN ut.pnl_usd<0 THEN -ut.pnl_usd ELSE 0 END),0))::numeric, 2) as pf, "
        "ROUND(AVG(EXTRACT(EPOCH FROM (ut.closed_at-ut.opened_at))/60)::numeric, 1) as avg_dur "
        "FROM user_trades ut "
        f"WHERE ut.closed_at >= NOW() - INTERVAL '{args.hours} hours' "
        "AND ut.metadata::jsonb->>'is_phase2_virtual' = 'true' "
        "GROUP BY metadata::jsonb->>'exit_config_id' "
        "ORDER BY SUM(ut.pnl_usd) DESC"
    )
    rc, out_agg, _ = psql(sql_agg)
    lines.append("")
    lines.append("| Config | n | wins | WR | Net | Avg | PF | Avg dur (min) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    rows = []
    if rc == 0 and out_agg:
        for line in out_agg.splitlines():
            parts = line.split("|")
            if len(parts) < 8: continue
            cfg, n, wins, wr, net, avg, pf, dur = parts[:8]
            rows.append((cfg, n, wins, wr, net, avg, pf, dur))
            lines.append(f"| `{cfg}` | {n} | {wins} | {wr}% | ${net} | ${avg} | {pf} | {dur} |")

    if rows:
        best = rows[0]
        lines.extend([
            "",
            f"## 🏆 Best config: **{best[0]}**  (Net=${best[4]} · PF={best[6]} · WR={best[3]}%)",
            "",
        ])

    out_file = OUT_DIR / f"leaderboard_{TS.strftime('%Y%m%d_%H%M%S')}.md"
    out_file.write_text("\n".join(lines))
    print(f"Wrote: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
