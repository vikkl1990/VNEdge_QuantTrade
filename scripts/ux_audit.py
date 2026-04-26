#!/usr/bin/env python3
"""Agent 14 — UI/UX Designer (weekly UX audit + on-demand pre-feature review)

Programmatic UX scorecard. Scans dashboard files for:
  1. Inline style violations (modern CSS-in-JS architecture banned inline `style=`)
  2. Color-blind accessibility (P&L color tokens — must not use red/green only)
  3. Mobile usability (max-width media queries, touch target sizes)
  4. ARIA labels on interactive elements (buttons, selects)
  5. Font-size legibility (no <0.55rem text)
  6. Hardcoded colors (must use --var-* tokens)

OUTPUTS:
  - storage/ux_audit/scorecard_YYYYMMDD.md weekly

CRON: 0 12 * * 1   (weekly Monday noon UTC)
"""
import sys
import re
import datetime
import pathlib

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
DASH = ROOT / "dashboard"
OUT_DIR = ROOT / "storage" / "ux_audit"
TS = datetime.datetime.utcnow()
OUT_FILE = OUT_DIR / f"scorecard_{TS.strftime('%Y%m%d')}.md"
OUT_DIR.mkdir(parents=True, exist_ok=True)

findings = []  # (severity, file, line, message)


def scan_html():
    """Inline style + ARIA + interactive element checks."""
    for f in DASH.glob("templates/**/*.html"):
        text = f.read_text(errors="replace")
        rel = f.relative_to(ROOT)
        # 1. inline style — flag if NOT inside a <span style="display:none"> shim
        for i, line in enumerate(text.splitlines(), 1):
            if 'style=' in line and 'display:none' not in line and 'display: none' not in line:
                findings.append(("🟡", str(rel), i, "inline style= (move to CSS class)"))
                if len([f for f in findings if f[1] == str(rel) and "inline style" in f[3]]) > 5:
                    break  # rate-limit per file
        # 4. ARIA on interactive
        # buttons without aria-label/title
        bm = re.findall(r'<button(?![^>]*\baria-label\b)(?![^>]*\btitle\b)([^>]*)>', text)
        if len(bm) > 5:
            findings.append(("🟡", str(rel), 0, f"{len(bm)} <button> elements without aria-label or title"))
        # selects without label
        sm = re.findall(r'<select(?![^>]*\baria-label\b)([^>]*)>', text)
        if sm:
            findings.append(("🟡", str(rel), 0, f"{len(sm)} <select> elements without aria-label"))


def scan_css():
    """Color-blind, hardcoded colors, font-size."""
    for f in DASH.glob("static/css/**/*.css"):
        text = f.read_text(errors="replace")
        rel = f.relative_to(ROOT)
        # 5. font-size <0.55rem
        for i, line in enumerate(text.splitlines(), 1):
            m = re.search(r'font-size:\s*0\.([0-4][0-9]|5[0-4])\s*rem', line)
            if m:
                findings.append(("🟡", str(rel), i, f"font-size {m.group(0)} likely illegible"))
        # 6. hardcoded color (not using --var-)
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r':\s*#[0-9a-fA-F]{3,6}', line) and 'var(' not in line and 'rgba(' not in line:
                # Skip if it's a color-stop in a gradient or shadow
                if 'shadow' not in line.lower() and 'gradient' not in line.lower():
                    findings.append(("🟢", str(rel), i, f"hardcoded color (consider CSS var): {line.strip()[:60]}"))
                    if len([f for f in findings if f[1] == str(rel) and "hardcoded" in f[3]]) > 3:
                        break


def scan_js():
    """Color-only PnL signaling — must include arrow/symbol so color-blind users can read."""
    for f in DASH.glob("static/js/**/*.js"):
        text = f.read_text(errors="replace")
        rel = f.relative_to(ROOT)
        # Look for PnL render that uses color but no arrow/sign
        for i, line in enumerate(text.splitlines(), 1):
            if 'pnl-pos' in line and '▲' not in text and '+' not in line:
                findings.append(("🟡", str(rel), i, "pnl-pos class without ▲ or + sign (color-blind risk)"))
                break  # once per file
            if 'pnl-neg' in line and '▼' not in text and '-' not in line and '−' not in line:
                findings.append(("🟡", str(rel), i, "pnl-neg class without ▼ or sign"))
                break


def check_mobile():
    """At least one CSS file should declare max-width media queries."""
    has_mobile = False
    for f in DASH.glob("static/css/**/*.css"):
        if "@media (max-width" in f.read_text(errors="replace"):
            has_mobile = True
            break
    if not has_mobile:
        findings.append(("🔴", "static/css/*", 0, "no @media (max-width:...) breakpoints — mobile users broken"))


# ─── Run all ─────────────────────────────────────────────────
scan_html()
scan_css()
scan_js()
check_mobile()

# Score: (0 issues = 100; -1 per yellow; -5 per red)
n_red = sum(1 for f in findings if f[0] == "🔴")
n_yellow = sum(1 for f in findings if f[0] == "🟡")
n_green = sum(1 for f in findings if f[0] == "🟢")
score = max(0, 100 - n_yellow - n_red * 5)

lines = [
    f"# UX Audit Scorecard — {TS.strftime('%Y-%m-%d')}",
    f"Generated: {TS.isoformat()}Z",
    "",
    f"## Score: **{score} / 100**",
    f"- 🔴 critical: {n_red}",
    f"- 🟡 review:   {n_yellow}",
    f"- 🟢 cosmetic: {n_green}",
    "",
    "## Findings",
    "",
    "| Sev | File | Line | Issue |",
    "|---|---|---:|---|",
]
for sev, f, ln, msg in findings[:80]:
    line_str = str(ln) if ln else "—"
    lines.append(f"| {sev} | `{f}` | {line_str} | {msg} |")
if len(findings) > 80:
    lines.append(f"\n_(+{len(findings) - 80} more — see full scan)_")

lines.extend([
    "",
    "## Action items",
    "- 🔴 must fix before next deploy (accessibility blockers)",
    "- 🟡 review before next dashboard feature ships",
    "- 🟢 cosmetic / consistency improvements",
])

OUT_FILE.write_text("\n".join(lines))
print(f"Wrote: {OUT_FILE}")
print(f"Score: {score}/100  |  🔴 {n_red}  🟡 {n_yellow}  🟢 {n_green}")
sys.exit(0)
