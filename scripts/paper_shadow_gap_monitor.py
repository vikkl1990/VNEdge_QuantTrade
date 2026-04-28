#!/usr/bin/env python3
"""Paper-vs-Shadow Gap Monitor — Agent 18.

Periodically (cron */30 min) compares closed paper signals vs closed
shadow trades over the last N hours. Identifies WHERE the bleed comes
from by attributing the per-trade R-equivalent gap to:

  - symbol  (BTC short losing more than ETH short on the same regime?)
  - IST hour (some hours have wider gap than others?)
  - paper_exit_reason × shadow_exit_reason  (which transition class
    bleeds the most? e.g. paper trail_profit → shadow time_decay)
  - Phase 2 exit_config_id  (which config widens the gap most?)

Matching: paper signal opened at T → shadow trade opened within ±2 min,
same symbol, same side. Picks the SHADOW with exit_config_id='primary'
as the canonical comparator (or first non-phase2_virtual shadow if no
phase2 fan-out for that signal).

Output: storage/paper_shadow_gap/gap_TS.md
        + appends one line to storage/paper_shadow_gap/gap_history.csv

Cron:
  */30 * * * * cd /home/opc/crypto-trading-bot && \
    /home/opc/miniconda3/bin/python3.13 scripts/paper_shadow_gap_monitor.py \
      --hours 6 2>&1 | logger -t paper_shadow_gap

Author: Architect collaboration with Claude — answer the q
'why does paper print money and shadow bleeds?' in <30 lines.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import asyncpg

ROOT = Path("/home/opc/crypto-trading-bot")
OUT_DIR = ROOT / "storage" / "paper_shadow_gap"
OUT_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_CSV = OUT_DIR / "gap_history.csv"

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


def parse_iso(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def load_paper_in_window(hours: int) -> List[dict]:
    """Load paper signals closed in the last N hours."""
    p = ROOT / "storage" / "closed_signals.json"
    if not p.exists():
        return []
    with open(p) as f:
        sigs = json.load(f)
    cut = TS - dt.timedelta(hours=hours)
    out = []
    for s in sigs:
        et = parse_iso(s.get("exit_time"))
        if et and et >= cut:
            out.append(s)
    return out


async def load_shadow_in_window(hours: int) -> List[dict]:
    """Load shadow trades closed in last N hours, primary config only
    (canonical), clean filter."""
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT
                    symbol, side,
                    entry_price::float AS entry_price,
                    exit_price::float  AS exit_price,
                    pnl_usd::float     AS pnl_usd,
                    fees_usd::float    AS fees_usd,
                    opened_at, closed_at,
                    metadata::jsonb->>'exit_reason'      AS exit_reason,
                    metadata::jsonb->>'exit_config_id'   AS cfg,
                    metadata::jsonb->>'is_phase2_virtual' AS is_p2v,
                    NULLIF(metadata::jsonb->>'peak_mfe_r','')::float AS peak_mfe_r,
                    NULLIF(metadata::jsonb->>'initial_risk','')::float AS initial_risk
                FROM user_trades
                WHERE trade_type='shadow'
                  AND closed_at >= NOW() - INTERVAL '{int(hours)} hours'
                  AND closed_at IS NOT NULL
                  AND COALESCE(metadata::jsonb->>'exit_reason','')
                      NOT IN ('auto_responder_stuck_60m','restart_orphan_cleanup','reconcile_overaged_close')
                  AND (COALESCE(metadata::jsonb->>'is_phase2_virtual','false') != 'true'
                       OR COALESCE(metadata::jsonb->>'exit_config_id','') = 'primary')
                ORDER BY opened_at
            """)
            return [dict(r) for r in rows]
    finally:
        await pool.close()


def to_r(pnl_usd: float, initial_risk: Optional[float], entry_price: float, qty: float, contract_size: float) -> Optional[float]:
    """Convert dollar PnL to R-multiple if we can derive risk in dollars."""
    if initial_risk and initial_risk > 0:
        # initial_risk is stored as the SL distance in price (per metadata)
        risk_usd = abs(initial_risk) * (qty or 1) * (contract_size or 1)
        return pnl_usd / risk_usd if risk_usd > 0 else None
    # Fallback: unknown
    return None


def match_shadow_to_paper(paper: List[dict], shadow: List[dict]) -> List[Tuple[dict, dict]]:
    """Match each paper signal to its corresponding shadow trade by
    (symbol, side, opened_at within ±2 min)."""
    matches = []
    used = set()
    for ps in paper:
        ps_sym  = ps.get("symbol")
        ps_side = (ps.get("side") or "").lower()
        ps_open = parse_iso(ps.get("signal_time")) or parse_iso(ps.get("entry_time"))
        if not (ps_sym and ps_side and ps_open):
            continue
        best = None
        best_dt = dt.timedelta(hours=999)
        for i, sh in enumerate(shadow):
            if i in used:
                continue
            if sh.get("symbol") != ps_sym or (sh.get("side") or "").lower() != ps_side:
                continue
            sh_open = sh.get("opened_at")
            if not sh_open:
                continue
            if not isinstance(sh_open, dt.datetime):
                sh_open = parse_iso(sh_open)
            if not sh_open:
                continue
            if sh_open.tzinfo is None:
                sh_open = sh_open.replace(tzinfo=dt.timezone.utc)
            diff = abs(sh_open - ps_open)
            if diff < best_dt and diff <= dt.timedelta(minutes=2):
                best_dt = diff
                best = i
        if best is not None:
            matches.append((ps, shadow[best]))
            used.add(best)
    return matches


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=6)
    args = ap.parse_args()

    paper = load_paper_in_window(args.hours)
    shadow = asyncio.run(load_shadow_in_window(args.hours))

    matches = match_shadow_to_paper(paper, shadow)

    # ---- Aggregate gap analysis ----
    n_matched = len(matches)
    n_paper_only = len(paper) - n_matched
    n_shadow_only = len(shadow) - n_matched

    # Per-trade gap (paper exit_r vs shadow exit_r-equivalent)
    gaps = []          # (paper_r, shadow_r, gap_r) per matched trade
    sym_gap = defaultdict(list)
    hour_gap = defaultdict(list)
    reason_pair_gap = defaultdict(list)   # (paper_reason, shadow_reason) -> gap list
    cfg_gap = defaultdict(list)

    for ps, sh in matches:
        paper_r = ps.get("exit_r")
        # shadow R-equivalent: if peak_mfe_r is known and exit happened, derive
        # rough exit_r from price move. Simpler: just compare paper_r vs
        # shadow's "would-have-been-r" computed from price delta / initial_risk
        sh_entry = sh.get("entry_price") or 0
        sh_exit  = sh.get("exit_price")  or 0
        sh_risk  = sh.get("initial_risk") or 0
        if sh_risk > 0 and sh_entry > 0:
            move = (sh_entry - sh_exit) if (sh.get("side") or "").lower() == "short" else (sh_exit - sh_entry)
            shadow_r = move / sh_risk
        else:
            shadow_r = None
        if paper_r is None or shadow_r is None:
            continue
        gap = paper_r - shadow_r
        gaps.append((paper_r, shadow_r, gap))
        sym_gap[ps.get("symbol")].append(gap)
        et = parse_iso(ps.get("exit_time"))
        if et:
            hour_ist = et.astimezone(IST).hour
            hour_gap[hour_ist].append(gap)
        reason_pair = (ps.get("exit_reason") or "?", sh.get("exit_reason") or "?")
        reason_pair_gap[reason_pair].append(gap)
        cfg_gap[sh.get("cfg") or "(non-phase2)"].append(gap)

    n_with_r = len(gaps)
    avg_paper_r = sum(p for p, s, g in gaps) / n_with_r if n_with_r else 0
    avg_shadow_r = sum(s for p, s, g in gaps) / n_with_r if n_with_r else 0
    avg_gap = sum(g for p, s, g in gaps) / n_with_r if n_with_r else 0

    # ---- Top contributors ----
    def top_n(d, n=5):
        return sorted(d.items(), key=lambda x: -abs(sum(x[1]) / max(len(x[1]), 1)))[:n]

    # ---- Build report ----
    ts_ist = TS.astimezone(IST).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Paper vs Shadow Gap — last {args.hours}h (UTC; {ts_ist} IST)",
        f"Generated: {TS.isoformat()}",
        "",
        f"**Paper closes**:  {len(paper)}",
        f"**Shadow closes**: {len(shadow)} (primary config canonical)",
        f"**Matched pairs**: {n_matched} (within ±2min same symbol+side)",
        f"  - paper-only (no shadow execution): {n_paper_only}",
        f"  - shadow-only (no paper signal): {n_shadow_only}",
        f"**With R-multiples**: {n_with_r}",
        "",
        "## Headline gap",
        "",
        f"- Avg paper exit_R:  **{avg_paper_r:+.3f}**",
        f"- Avg shadow exit_R: **{avg_shadow_r:+.3f}**",
        f"- **Avg gap (paper - shadow): {avg_gap:+.3f}R per trade**",
        f"- Cumulative R lost to shadow execution: **{sum(g for _,_,g in gaps):+.2f}R** over {n_with_r} matched pairs",
        "",
    ]

    if sym_gap:
        lines += ["## Gap by symbol (worst first)", "",
                  "| Symbol | n | Avg paper R | Avg shadow R | Avg gap |",
                  "|---|---:|---:|---:|---:|"]
        for sym, gs in sorted(sym_gap.items(), key=lambda x: -abs(sum(x[1]) / max(len(x[1]), 1)))[:10]:
            n = len(gs)
            avg_g = sum(gs) / n
            paper_avg = sum(p for p, s, g in gaps if g in gs) / max(1, n)
            shadow_avg = sum(s for p, s, g in gaps if g in gs) / max(1, n)
            flag = " 🔴" if avg_g > 0.15 else (" 🟢" if avg_g < -0.05 else "")
            lines.append(f"| {sym} | {n} | {paper_avg:+.3f} | {shadow_avg:+.3f} | {avg_g:+.3f}{flag} |")

    if hour_gap:
        lines += ["", "## Gap by IST hour (widest first)", "",
                  "| Hour IST | n | Avg gap (R) |",
                  "|---|---:|---:|"]
        for h, gs in sorted(hour_gap.items(), key=lambda x: -abs(sum(x[1]) / max(len(x[1]), 1)))[:10]:
            n = len(gs)
            avg_g = sum(gs) / n
            flag = " 🔴" if avg_g > 0.15 else (" 🟢" if avg_g < -0.05 else "")
            lines.append(f"| {h:02d}:00 IST | {n} | {avg_g:+.3f}{flag} |")

    if reason_pair_gap:
        lines += ["", "## Worst exit-reason transitions (paper → shadow)", "",
                  "These are the actual leak points: when paper exits via X but shadow exits via Y,",
                  "the gap is the alpha lost. Sorted by total R lost.", "",
                  "| Paper exit | → Shadow exit | n | Total gap R | Avg gap R |",
                  "|---|---|---:|---:|---:|"]
        sorted_pairs = sorted(reason_pair_gap.items(), key=lambda x: -sum(x[1]))[:10]
        for (pr, sr), gs in sorted_pairs:
            n = len(gs)
            total = sum(gs)
            avg = total / n
            flag = " 🔴" if total > 1.0 else ""
            lines.append(f"| {pr} | {sr} | {n} | {total:+.2f}{flag} | {avg:+.2f} |")

    if cfg_gap:
        lines += ["", "## Gap by Phase 2 config", "",
                  "| Config | n | Avg gap (R) |",
                  "|---|---:|---:|"]
        for cfg, gs in sorted(cfg_gap.items()):
            n = len(gs)
            avg_g = sum(gs) / n
            lines.append(f"| {cfg} | {n} | {avg_g:+.3f} |")

    # Verdict heuristic
    lines += ["", "## Verdict", ""]
    if n_with_r < 5:
        lines.append("- ⏸ Sample too small (<5 matched pairs with R). No verdict.")
    elif avg_gap > 0.20:
        lines.append(f"- 🔴 SIGNIFICANT shadow degrade: paper outperforming by {avg_gap:+.2f}R/trade. Investigate top symbol + reason-transition.")
    elif avg_gap > 0.05:
        lines.append(f"- 🟡 MILD gap: paper +{avg_gap:.2f}R/trade better. Watch.")
    elif avg_gap < -0.05:
        lines.append(f"- 🟢 Shadow MATCHING or BEATING paper (gap {avg_gap:+.2f}R). Edge translating well.")
    else:
        lines.append(f"- ✅ Shadow ≈ paper (gap {avg_gap:+.3f}R within noise).")

    out_file = OUT_DIR / f"gap_{TS.strftime('%Y%m%d_%H%M%S')}.md"
    out_file.write_text("\n".join(lines))

    # Append history CSV row
    write_header = not HISTORY_CSV.exists()
    with open(HISTORY_CSV, "a") as fp:
        if write_header:
            fp.write("ts_utc,hours,n_matched,n_with_r,avg_paper_r,avg_shadow_r,avg_gap_r,total_gap_r\n")
        fp.write(f"{TS.isoformat()},{args.hours},{n_matched},{n_with_r},"
                 f"{avg_paper_r:.4f},{avg_shadow_r:.4f},{avg_gap:.4f},"
                 f"{sum(g for _,_,g in gaps):.4f}\n")

    print(f"GAP_AGENT: matched={n_matched} with_r={n_with_r} avg_gap={avg_gap:+.3f}R "
          f"total_gap={sum(g for _,_,g in gaps):+.2f}R")
    print(f"Wrote: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
