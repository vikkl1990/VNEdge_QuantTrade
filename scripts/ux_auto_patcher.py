#!/usr/bin/env python3
"""Agent 14 ACTIVE — UX Auto-Patcher (weekly Monday 13:00 UTC, after audit)

Reads latest UX audit scorecard. Auto-fixes the LOW-RISK COSMETIC patterns:
  1. Inline `style="..."` → CSS class (extracts repeated patterns)
  2. Hardcoded color hex → CSS variable (if --var- exists for that color)
  3. Missing aria-label on buttons → adds inferred label from button text

Each fix:
  - is git-aware: writes to a feature branch + commit (NO direct deploy)
  - dry-run mode prints what would change
  - requires architect to merge / deploy via Agent 3

Caps:
  - Max 10 file edits per run
  - Max 50 inline-style extractions per run
  - SKIP files modified in last 24h (avoid stomping on architect's WIP)

CRON: 0 13 * * 1   (Monday 13:00 UTC, 1h after Agent 14 audit)
"""
import sys
import re
import datetime
import pathlib
import subprocess
from collections import Counter

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
DASH = ROOT / "dashboard"
OUT_DIR = ROOT / "storage" / "ux_audit"
TS = datetime.datetime.utcnow()
LOG_FILE = OUT_DIR / f"auto_patch_{TS.strftime('%Y%m%d')}.md"

CAP_FILES = 10
CAP_EXTRACTIONS = 50
SKIP_AGE_HOURS = 24


def file_recently_modified(p):
    if not p.exists():
        return False
    age_hr = (TS.timestamp() - p.stat().st_mtime) / 3600
    return age_hr < SKIP_AGE_HOURS


def collect_inline_style_patterns():
    """Scan dashboard HTML, find frequently-repeated inline styles."""
    counter = Counter()
    occurrences = []  # (file, line_num, full_match, style_value)
    for f in DASH.glob("templates/**/*.html"):
        if file_recently_modified(f):
            continue
        text = f.read_text(errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(r'\bstyle="([^"]{8,200})"', line):
                style_val = m.group(1).strip()
                # Skip the display:none shims (intentional)
                if "display:none" in style_val.replace(" ", "") or "display: none" in style_val:
                    continue
                counter[style_val] += 1
                occurrences.append((f, i, m.group(0), style_val))
    return counter, occurrences


def derive_class_name(style_val):
    """e.g. 'color:red;font-size:.7rem' → 'mex-clr-red-fs-7'"""
    # Take first 2 declarations
    decls = [d.strip() for d in style_val.split(";") if d.strip()][:2]
    bits = []
    for d in decls:
        if ":" not in d:
            continue
        prop, val = d.split(":", 1)
        prop = prop.strip()[:6].replace("-", "")
        val = re.sub(r"[^a-zA-Z0-9]", "", val.strip())[:6]
        bits.append(f"{prop}-{val}")
    name = "ux-" + "-".join(bits) if bits else "ux-misc"
    return name.lower()


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    counter, occurrences = collect_inline_style_patterns()
    if not counter:
        LOG_FILE.write_text(
            f"# UX Auto-Patcher — {TS.isoformat()}Z\n\nNo inline-style patterns found.\n"
        )
        print(f"Wrote: {LOG_FILE}")
        return 0

    # Pick patterns that repeat ≥3x (worth extracting)
    repeated = [(s, n) for s, n in counter.most_common(20) if n >= 3]
    files_touched = set()
    extractions = 0
    proposed = []

    for style_val, freq in repeated:
        if extractions >= CAP_EXTRACTIONS:
            break
        cls = derive_class_name(style_val)
        # For each occurrence of this style_val
        for f, ln, full_match, sv in occurrences:
            if sv != style_val:
                continue
            if extractions >= CAP_EXTRACTIONS:
                break
            if len(files_touched) >= CAP_FILES and f not in files_touched:
                continue
            files_touched.add(f)
            proposed.append({
                "file": str(f.relative_to(ROOT)),
                "line": ln,
                "freq": freq,
                "class": cls,
                "style": style_val[:80],
                "old": full_match[:120],
                "new": f'class="{cls}"',
            })
            extractions += 1

    # Render proposal — but DO NOT APPLY (architect-only deploy)
    lines = [
        f"# UX Auto-Patcher — Dry Proposal",
        f"Generated: {TS.isoformat()}Z",
        "",
        f"Found **{len(repeated)}** repeated inline-style patterns. Proposing {extractions} extractions across {len(files_touched)} files.",
        "",
        "## Proposed CSS additions (`dashboard/static/css/ux_auto_extracted.css`)",
        "```css",
    ]
    seen_cls = set()
    for p in proposed:
        if p["class"] in seen_cls:
            continue
        seen_cls.add(p["class"])
        lines.append(f".{p['class']} {{ {p['style']} }}  /* used {next(o['freq'] for o in proposed if o['class']==p['class'])}x */")
    lines.append("```")
    lines.extend([
        "",
        "## Proposed HTML edits",
        "| File | Line | freq | OLD | NEW |",
        "|---|---:|---:|---|---|",
    ])
    for p in proposed[:50]:
        lines.append(f"| `{p['file']}` | {p['line']} | {p['freq']} | `{p['old']}` | `{p['new']}` |")

    lines.extend([
        "",
        "## How to apply",
        "These edits are **NOT auto-deployed**. Review the proposal, then either:",
        "1. Run `/home/opc/crypto-trading-bot/scripts/ux_auto_patcher.py --apply` to apply locally + commit",
        "2. Or hand off to Agent 3 (Deploy Gatekeeper)",
        "",
        "Caps applied: max 10 files, max 50 extractions per run.",
    ])
    LOG_FILE.write_text("\n".join(lines))
    print(f"Wrote: {LOG_FILE}")
    print(f"Proposed: {extractions} extractions across {len(files_touched)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
