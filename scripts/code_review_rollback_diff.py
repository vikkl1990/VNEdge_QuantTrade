#!/usr/bin/env python3
"""Agent 4 — Code Review Engineer (REPURPOSED 2026-04-26)

Original mission was a generic AST review of patch scripts. That role was
never connected to a real deploy gate (Agent 3 not deployed) — so the daily
cron just errored with `ERROR: usage:`.

NEW MISSION: every day, diff currently-deployed Python files against the
most recent matching .rollback/* snapshot. Surface any function whose body
has CHANGED but signature LOOKS THE SAME — that's the regression class
(Bug 3 today: lever3 rollback dropped `shadow_simulated_balance` pass-through
in `user_registry.py` while keeping the SELECT, producing silent sizing bug).

Output: `storage/code_review/rollback_diff_YYYYMMDD.md`
Exit 0 always — informational only. Architect reviews via Daily Briefing.
"""
import os
import sys
import subprocess
import datetime
import pathlib
import re
from collections import defaultdict

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
ROLLBACK_DIR = ROOT / ".rollback"
OUT_DIR = ROOT / "storage" / "code_review"
TS = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
OUT_FILE = OUT_DIR / f"rollback_diff_{TS}.md"

# Files we care about — the production-affecting code paths
TRACKED = [
    "execution/user_registry.py",
    "execution/user_real_manager.py",
    "execution/exit_guards.py",
    "bot/orchestrator.py",
    "bot/signal_tracker.py",
    "scripts/bybit_shadow_simulator.py",
    "scripts/bybit_shadow_monitor.py",
    "dashboard/server.py",
]


def find_latest_rollback(file_rel: str):
    """Find the most recent rollback snapshot of a file.

    Rollback dirs we know of:
      .rollback/wave2-phase5208-20260425_115515/...
      .rollback/wave6c-20260425_140009/...
      .rollback/lever3-20260425_141308/...
      .rollback/USERFILE.bak.YYYYMMDD_HHMMSS    ← inline backup
    """
    matches = []
    fname = pathlib.Path(file_rel).name
    # Snapshot dirs
    if ROLLBACK_DIR.exists():
        for sub in ROLLBACK_DIR.iterdir():
            if sub.is_dir():
                cand = sub / fname
                if cand.exists():
                    matches.append((cand.stat().st_mtime, cand))
            elif sub.is_file() and fname in sub.name:
                matches.append((sub.stat().st_mtime, sub))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def extract_functions(path: pathlib.Path):
    """Return {fn_name: body_text} for each `def`/`async def` in the file.

    Heuristic — uses indentation to find body. Works for typical Python.
    """
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    lines = src.splitlines()
    fns = {}
    cur_name = None
    cur_body = []
    cur_indent = None
    for line in lines:
        stripped = line.lstrip()
        m = re.match(r"^(async\s+def|def)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", stripped)
        if m and (cur_indent is None or (len(line) - len(stripped)) <= cur_indent):
            # Flush previous
            if cur_name:
                fns[cur_name] = "\n".join(cur_body)
            cur_name = m.group(2)
            cur_indent = len(line) - len(stripped)
            cur_body = [line]
        elif cur_name is not None:
            indent = len(line) - len(stripped) if stripped else 999
            if stripped and indent <= cur_indent:
                # Function ended
                fns[cur_name] = "\n".join(cur_body)
                cur_name = None
                cur_body = []
                cur_indent = None
                # Re-check this line as new function
                m2 = re.match(r"^(async\s+def|def)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", stripped)
                if m2:
                    cur_name = m2.group(2)
                    cur_indent = indent
                    cur_body = [line]
            else:
                cur_body.append(line)
    if cur_name:
        fns[cur_name] = "\n".join(cur_body)
    return fns


def diff_summary(file_rel):
    """Return (status, summary, changed_fns) where status ∈ {'no-rollback', 'identical', 'changed'}"""
    cur = ROOT / file_rel
    if not cur.exists():
        return "missing", f"current file not found: {file_rel}", []
    rollback = find_latest_rollback(file_rel)
    if not rollback:
        return "no-rollback", f"no rollback baseline found for {file_rel}", []
    cur_fns = extract_functions(cur)
    old_fns = extract_functions(rollback)
    changed = []
    removed = []
    added = []
    for name, body in old_fns.items():
        if name not in cur_fns:
            removed.append(name)
        elif cur_fns[name].strip() != body.strip():
            changed.append((name, len(body.splitlines()), len(cur_fns[name].splitlines())))
    for name in cur_fns:
        if name not in old_fns:
            added.append(name)
    if not changed and not removed and not added:
        return "identical", f"identical to {rollback.relative_to(ROOT)}", []
    summary = f"vs {rollback.relative_to(ROOT)}: +{len(added)} -{len(removed)} ~{len(changed)}"
    return "changed", summary, changed + [(n, "removed", "") for n in removed]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    out_lines = [
        f"# Agent 4 — Code Review (Rollback Diff)",
        f"Generated: {datetime.datetime.utcnow().isoformat()}Z",
        f"",
        "Compares each production-affecting Python file against the most recent",
        "matching `.rollback/*` snapshot. Surfaces functions changed-since-rollback.",
        "Today (Bug 3) was caused by a rollback dropping a pass-through; this scan",
        "is designed to catch that pattern before deploy.",
        "",
        "| File | Status | Summary | Changed functions |",
        "|---|---|---|---|",
    ]

    flagged_count = 0
    for f in TRACKED:
        status, summary, changes = diff_summary(f)
        emoji = {
            "identical": "🟢",
            "changed": "🟡",
            "no-rollback": "⚪",
            "missing": "🔴",
        }.get(status, "❓")
        change_list = ", ".join(f"`{c[0]}`" for c in changes[:8]) if changes else "—"
        if len(changes) > 8:
            change_list += f", … (+{len(changes)-8} more)"
        out_lines.append(f"| `{f}` | {emoji} {status} | {summary} | {change_list} |")
        if status == "changed":
            flagged_count += 1

    out_lines.extend([
        "",
        f"**Files with diffs vs rollback baseline:** {flagged_count} / {len(TRACKED)}",
        "",
        "**Notes:**",
        "- 🟢 identical = file matches latest rollback snapshot exactly",
        "- 🟡 changed = file has function-level diffs since last rollback (review)",
        "- ⚪ no-rollback = no baseline snapshot exists yet",
        "- 🔴 missing = production file not found",
        "",
        "Action: review 🟡 entries before next deploy. Cross-check against PR/changelog.",
    ])

    OUT_FILE.write_text("\n".join(out_lines))
    print(f"Wrote: {OUT_FILE}")
    print(f"Flagged (changed-since-rollback): {flagged_count}/{len(TRACKED)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
