#!/usr/bin/env python3
"""Agent 13 — Backtest Engineer (on-demand + monthly health check)

Two modes:
  1. INVOKE   (on-demand): runs a backtest scenario and produces a result file
  2. STATUS   (monthly cron): reports the backtest engine's capabilities, recent
              runs, and gaps where Edge Validator (Agent 1) is blocked.

The backtest engine itself lives in `backtest/execution_replay/`. This agent
is a thin wrapper + capability registry so Edge Validator (Agent 1) and
Architect can ask "can we backtest X?" without re-reading the code.

USAGE:
  ./backtest_engineer.py status            # default — capability + recent runs
  ./backtest_engineer.py invoke <scenario> # run a named scenario
  ./backtest_engineer.py list-scenarios    # list known scenarios

CRON (monthly status check): 0 8 1 * *
"""
import sys
import datetime
import pathlib
import subprocess

ROOT = pathlib.Path("/home/opc/crypto-trading-bot")
BACKTEST_DIR = ROOT / "backtest" / "execution_replay"
OUT_DIR = ROOT / "storage" / "backtest_engineer"
TS = datetime.datetime.utcnow()
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ─── Capability registry ─────────────────────────────────────
# Updated as the backtest engine adds new replay modes.
CAPABILITIES = {
    "fill_simulation": {
        "status": "✅ available",
        "module": "backtest/execution_replay/calibrate_maker_miss.py",
        "description": "Replay maker order fills against historical L2 (Delta India)",
    },
    "exit_logic_replay": {
        "status": "🔴 GAP",
        "module": "(not implemented)",
        "description": "Replay candles forward through exit cascade (SL/TP/trail/max_age)",
        "needed_for": "Agent 1 Edge Validator can't gate Wave 2 / Lever 3 / mark alignment without this",
    },
    "shadow_vs_real_alignment": {
        "status": "🟡 partial",
        "module": "scripts/paper_vs_shadow_gap.py",
        "description": "Compare paper signals vs shadow exits over historical window",
    },
    "lever3_flip_analysis": {
        "status": "✅ available",
        "module": "scripts/lever3_flip_analysis.py",
        "description": "Detect cases where shadow exit kill triggered while paper held",
    },
    "counterfactual_exit": {
        "status": "✅ available",
        "module": "scripts/counterfactual_exit_analyzer.py",
        "description": "What if we'd held longer / exited earlier on closed trades",
    },
    "venue_replay_bybit": {
        "status": "🔴 GAP",
        "module": "(not implemented)",
        "description": "Replay Delta signals through Bybit L2 historical to confirm Bybit edge "
                       "(today's Agent 15 verdict: Bybit beats Delta by $127/day; we should "
                       "validate via 30-day historical replay before live migration)",
    },
}


SCENARIOS = {
    "maker_miss_calibration": {
        "command": "/home/opc/miniconda3/bin/python3.13 -m backtest.execution_replay.calibrate_maker_miss --days 7",
        "description": "Calibrate maker fill rate against last 7 days of L2",
    },
    "lever3_flips_30d": {
        "command": "/home/opc/miniconda3/bin/python3.13 scripts/lever3_flip_analysis.py --days 30",
        "description": "Find shadow-vs-paper exit divergences in last 30 days",
    },
    "paper_shadow_gap_7d": {
        "command": "/home/opc/miniconda3/bin/python3.13 scripts/paper_vs_shadow_gap.py --days 7",
        "description": "Quantify paper-vs-shadow PnL gap over last 7 days",
    },
}


def cmd_status():
    """Write capability + recent runs report."""
    OUT_FILE = OUT_DIR / f"status_{TS.strftime('%Y%m')}.md"
    lines = [
        f"# Backtest Engineer — Capability Status",
        f"Generated: {TS.isoformat()}Z",
        "",
        "## Engine capabilities",
        "",
        "| Capability | Status | Module | Description |",
        "|---|---|---|---|",
    ]
    n_gaps = 0
    for cap, info in CAPABILITIES.items():
        if "🔴" in info["status"]:
            n_gaps += 1
        lines.append(
            f"| {cap} | {info['status']} | `{info['module']}` | {info['description']} |"
        )
    lines.extend([
        "",
        f"**Gaps:** {n_gaps} of {len(CAPABILITIES)}",
        "",
        "## Available scenarios (invoke via `backtest_engineer.py invoke <name>`)",
        "",
    ])
    for s, info in SCENARIOS.items():
        lines.append(f"- **`{s}`**: {info['description']}")

    # Recent runs
    runs_dir = OUT_DIR.parent / "backtest_runs"
    if runs_dir.exists():
        recent = sorted(runs_dir.glob("*.md"))[-5:]
        lines.append("\n## Last 5 backtest runs")
        for r in recent:
            lines.append(f"- `{r.relative_to(ROOT)}` ({datetime.datetime.fromtimestamp(r.stat().st_mtime).isoformat()})")

    lines.extend([
        "",
        "## Action items",
        "",
        "- 🔴 **Build exit_logic_replay** — Agent 1 Edge Validator is blocked without it",
        "- 🔴 **Build venue_replay_bybit** — needed to validate Bybit migration with 30d historical",
        "",
        "If Agent 1 hits a 'can't simulate this' wall, escalate to Agent 13 (this agent) for engine extension.",
    ])
    OUT_FILE.write_text("\n".join(lines))
    print(f"Wrote: {OUT_FILE}")
    print(f"Gaps: {n_gaps}/{len(CAPABILITIES)}")


def cmd_invoke(scenario):
    if scenario not in SCENARIOS:
        print(f"ERROR: unknown scenario '{scenario}'. Try: {list(SCENARIOS.keys())}")
        sys.exit(2)
    info = SCENARIOS[scenario]
    runs_dir = OUT_DIR.parent / "backtest_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    out_file = runs_dir / f"{scenario}_{TS.strftime('%Y%m%d_%H%M%S')}.md"
    print(f"Running: {info['command']}")
    r = subprocess.run(info["command"], shell=True, cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    out_file.write_text(
        f"# Backtest Run — {scenario}\n"
        f"Generated: {TS.isoformat()}Z\n"
        f"Command: `{info['command']}`\n"
        f"Description: {info['description']}\n"
        f"Exit code: {r.returncode}\n\n"
        f"## stdout\n```\n{r.stdout[:8000]}\n```\n\n"
        f"## stderr\n```\n{r.stderr[:2000]}\n```\n"
    )
    print(f"Wrote: {out_file}")
    sys.exit(r.returncode)


def cmd_list():
    print("Available scenarios:")
    for s, info in SCENARIOS.items():
        print(f"  {s}: {info['description']}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "status":
        cmd_status()
    elif mode == "invoke" and len(sys.argv) > 2:
        cmd_invoke(sys.argv[2])
    elif mode == "list-scenarios":
        cmd_list()
    else:
        print("usage: backtest_engineer.py [status|invoke <scenario>|list-scenarios]")
        sys.exit(1)
