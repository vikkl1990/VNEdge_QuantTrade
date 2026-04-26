# VN Edge — Production-Grade Agent Architecture

> **Purpose:** Operational discipline framework to take VN Edge from research-grade (where silent failures compound and bot bleeds) to production-grade (where every change passes gates, every failure is caught, every decision is auditable).
>
> **Date:** 2026-04-25 (original) · **2026-04-26 RESTRUCTURE** (post bug-bash review)
> **Author:** Architect collaboration with Claude (Chief Quant)
> **Status:** Phase 2 — restructured roster deployed (10 active agents)

---

## 0. CHANGELOG (2026-04-26 restructure)

After today's session uncovered 6 production bugs that **all 6 active agents missed**, the design was reviewed and restructured:

**Fixed:**
- ✅ Agent 4 (Code Review Engineer) — was BROKEN with `ERROR: usage:` daily. **REPURPOSED** from generic AST review to **rollback-diff scanner**: each day, diff every tracked production file against the latest matching `.rollback/*` snapshot. This new mission would have caught Bug 3 (lever3 rollback dropped `shadow_simulated_balance` pass-through).
- ✅ Agent 5 HEAVY (Silent Failure Hunter) — was never on cron despite being "Phase 1 priority". **WIRED** to `0 */6 * * *` cron. Added 7th category (in-memory monitor task health) that would catch Bug O5 class.
- ✅ Agent 1 (Edge Validator / auto_revert) — was blind to "trades stuck open" failure mode. **EXTENDED** with two new triggers: stuck-open >60min + close/open ratio collapse <50%.
- ✅ `agent_watchdog.sh` — bash `[[: 0)` syntax error from `grep -c | echo 0` race. Fixed.

**Added (3 NEW agents to fill blind spots today exposed):**
- ✅ **Agent 15 — Venue Performance Watcher** (every 6h). Compares paired (`delta_shadow`, `bybit_shadow`) PnL on same signals. Today's first run already returned 🔴 ESCALATE matching the manual finding (Bybit PF=3.44 vs Delta PF=0.09).
- ✅ **Agent 16 — Process Heartbeat Watcher** (every 10min). Watches cryptobot, bybit-shadow-daemon, bybit-shadow-monitor systemd activity, plus Mac dispatcher heartbeat (delta_shadow opens / bybit_demo mirrors ratio). Today's first run caught the Mac dispatcher SILENT issue.
- ✅ **Agent 17 — Cohort Pause Surfacer** (daily 09:00 UTC). Surfaces silent operational state (cohort_blacklist_paused_until, live_emergency_halt, kill_switch). Today both users were paused 14h — completely invisible without DB query.

**Demoted/dropped:**
- 🟢 Agent 12 (Architect Briefing) — moved from weekly Sunday → **daily 18:00 UTC** with Sunday "deep" version. Today's session burned 8 hours on bugs a daily briefing would have surfaced.
- 🔄 Agent 13 (Backtest Engineer) — dropped from active roster, on-demand only.

**Active roster: 10 deployed + 4 design-only (Agents 3, 9, 10, 14).**

---

## 0b. CHANGELOG — Phase 3 (2026-04-26 PM, "deploy this" command)

After Phase 2 restructure, architect ordered "deploy this" on the 4 design-only agents + Agent 13 (Backtest Engineer). All 5 deployed.

**Deployed:**
- ✅ **Agent 3 (Deploy Gatekeeper)** — `scripts/deploy_gatekeeper.sh` 7-stage wrapper (syntax → rollback diff context → backup → SCP → install → restart → arm auto-revert). Event-driven (no cron). Replaces ad-hoc `scp; cp; restart` pattern. Smoke-test: DRY_RUN walked through all 7 stages.
- ✅ **Agent 9 (Incident Responder)** — `scripts/incident_responder.py` cron `*/5 * * * *`. Detects 6 trigger classes: kill_switch, auto_revert, SHADOW RECONCILED, MONITOR_FORCE_CLOSE, service restart, position anomaly. CRITICAL writes full diagnostic dump; INFO writes brief log. First run already detected an INFO event (shadow_reconciled from earlier session).
- ✅ **Agent 10 (Compliance Auditor)** — `scripts/compliance_auditor.py` cron `0 7 1 * *` (monthly). 6 checks: signal parity, sizing variance, key safety, balance reconciliation, max-loss enforcement, audit log completeness. First run: 🔴 1 / 🟡 2 / 🟢 7 — actionable findings on day 1.
- ✅ **Agent 13 (Backtest Engineer)** — `scripts/backtest_engineer.py` cron `0 8 1 * *` (monthly status). Modes: `status` (capability registry) / `invoke <scenario>` (run named scenario) / `list-scenarios`. Status report flags: 2/6 capability gaps (`exit_logic_replay`, `venue_replay_bybit`) — both blockers for Edge Validator (Agent 1) and Bybit migration validation.
- ✅ **Agent 14 (UI/UX Designer)** — `scripts/ux_audit.py` cron `0 12 * * 1` (weekly Monday noon UTC). Programmatic scorecard checks: inline styles, ARIA labels, color-only PnL signaling, hardcoded colors, font-size legibility, mobile breakpoints. First run: 68/100 with 32 yellow findings (mostly inline styles).

**ROSTER STATUS NOW: 14 of 14 active.** Zero design-only agents remaining. The 14-member team is fully deployed for the first time since the design doc was created.

---

## 0c. CHANGELOG — Phase 4 (2026-04-26 PM, "i want them to actively work on the code")

After all 14 passive agents went live, architect requested they actually WORK on the code, not just monitor it. Phase 4 introduces an ACTIVE TIER where agents apply remediations.

### Tier A — Deterministic Auto-Fix (plain Python, no Claude reasoning)

| # | Active version | Pattern | Cap | Status |
|---|---|---|---|---|
| **9-A** | `incident_auto_responder.py` (replaces `incident_responder.py` cron) | P1: dead service → `systemctl restart` (whitelist only) · P2: stuck open trades >60min → force-close · P3: monitor crash-loop → restart · P4: 7-day cohort pause → escalate (no auto-clear) | P1: 3/h · P2: 20/h · P3: 5/h | ✅ active. First fire auto-closed 3 stuck SOL trades from Bug O5. |
| **14-A** | `ux_auto_patcher.py` | Inline `style="..."` → CSS class extraction (max 10 files, max 50 extractions/run, skip files modified in last 24h). DRY-PROPOSE only — does not auto-deploy. | 10 files · 50 extractions/run | ✅ active Mon 13:00 UTC. First run proposed 39 extractions across 2 files. |

**Safety mechanisms:**
- Hourly quota cap per pattern stored in `storage/auto_responder/quota.json`
- Every action logged to `storage/auto_responder/actions.log` with TS + pattern + outcome
- Whitelist for `systemctl restart` (cannot restart arbitrary services)
- DRY-PROPOSE for code edits (architect must approve before deploy)

### Tier B — Claude-Reasoning Active Workers (scheduled remote tasks)

These wake up daily, read passive agents' reports, pick the highest-priority issue, INVESTIGATE root cause via SSH/grep/file reads, and ATTEMPT FIXES via the Deploy Gatekeeper (Agent 3).

| Worker | Cadence | Mission | Constraints |
|---|---|---|---|
| **Daily Bug Worker** (`vnedge-daily-bug-worker`) | daily 09:30 IST (cron `30 9 * * *` local) | Reads daily briefing → picks #1 issue → investigates → writes fix → deploys via Gatekeeper → verifies → reports | 60-min budget · 1 file edit per session · 1 cryptobot restart per session · Time-boxed escalate if uncertain |
| **Code Review Auto-Patcher** (`vnedge-code-review-auto-patcher`) | daily 12:30 IST | Reads Agent 4 rollback diff → analyses each 🟡 changed file → classifies OK/REGRESSION/UNCERTAIN → proposes patches (no auto-apply) | 30-min budget · NEVER auto-applies · Only writes to `storage/code_review/` and `.rollback/` |

Claude tool: `mcp__scheduled-tasks` registers them; runs in cloud agent environment.

**Hard rules for Tier B (encoded in each prompt):**
- Cannot push to git main
- Cannot use `git push --force`
- Cannot touch `users.cohort_blacklist_paused_until` (cohort pauses are protective)
- ALWAYS via Agent 3 deploy gatekeeper for any code change
- Time-boxed escalate-or-exit if uncertain
- Document novel patterns in `storage/bug_worker/patterns/` instead of fixing

### Active tier results so far

- Agent 9-A: Bug O5 victims (3 stuck SOL trades) → auto-closed at first fire ✅
- Agent 14-A: 39 inline-style extractions queued for architect review ✅
- Daily Bug Worker: first fire tomorrow morning IST
- Code Review Auto-Patcher: first fire tomorrow noon IST

### Monitoring the active tier

```bash
# Quota usage
cat storage/auto_responder/quota.json

# Action log (auto-fixes)
tail -50 storage/auto_responder/actions.log

# Tier B worker output
ls -lt storage/bug_worker/   # daily fix reports
ls -lt storage/code_review/  # patch proposals
```

**Roster: 14 passive + 2 active deterministic + 2 active Claude workers = 18 capabilities total.**

---

## 1. Executive Summary

Today (2026-04-25) we discovered 10+ silent failures in a single session. Each one cost hours to diagnose. The pattern is consistent: code exists but doesn't run, config exists but isn't read, field exists but isn't populated.

This document defines a 12-agent operational structure to systematically address production readiness. Each agent has:
- A specific mission (the production-grade gap it closes)
- Defined triggers (when it fires)
- Tools (what it uses)
- Deliverables (what it produces)
- KPIs (how we measure success)

**Production readiness target:** 95% within 8 weeks across 10 dimensions.

---

## 2. Production Readiness Scorecard

| Dimension | Now | Target | Gap to close |
|---|---:|---:|---|
| Reliability | 35% | 99% | Silent failures, sync drift, no test coverage |
| Safety | 65% | 99% | Kill_switch + auto_revert exist but never battle-tested |
| Profitability | 5% | 60% | Bot is bleeding; no validated positive cohort |
| Auditability | 50% | 95% | Logs exist; no formal trail / decision log |
| Repeatability | 25% | 95% | Manual deploys, ad-hoc orchestration |
| Recoverability | 70% | 95% | Backups solid; no DR drill |
| Scalability | 40% | 90% | Per-user works for 2; untested for 10+ |
| Compliance | 30% | 85% | Multi-user platform; no audit trail |
| Documentation | 60% | 90% | Many docs; no runbooks |
| Test Coverage | 5% | 70% | 3 tests / 108k LOC |

**Composite production-readiness: 38% → target 95%.**

---

## 3. The Agent Org Chart

```
                ┌──────────────────────────────┐
                │  ARCHITECT (the user)        │
                │  Direction · Approval · Risk │
                └─────────────┬────────────────┘
                              │ weekly briefing + decision points
                ┌─────────────▼────────────────┐
                │  CHIEF QUANT (Claude main)   │
                │  Synthesis · Wave planning   │
                │  Agent orchestration         │
                └────┬────┬────┬────┬──────────┘
                     │    │    │    │
        ┌────────────┘    │    │    └────────────┐
        │    ┌────────────┘    └────────────┐    │
        │    │                              │    │
   ┌────▼────▼────┐    ┌────────────┐  ┌────▼────▼────┐
   │  RESEARCH    │    │   ENGRG    │  │     OPS       │
   │  PILLAR      │    │   PILLAR   │  │     PILLAR    │
   └──┬──────┬────┘    └──┬───┬─────┘  └──┬─────┬──────┘
      │      │            │   │           │     │
   Edge   Execution   Deploy  Code     Risk   Incident
   Vali-  Quality     Gate-   Review   Mon-   Re-
   dator  Engineer    keeper  Engineer itor   sponder
                      │       │        │     │
                      ▼       ▼        ▼     ▼
                Silent      Data     Sync   Compliance
                Failure   Integrity  Recon-  Auditor
                Hunter   Engineer    ciler

ML PILLAR (cross-cutting):  ML Pipeline Operator
FRONTEND/UX PILLAR (cross-cutting): UI/UX Designer (Agent 14)
ARCHITECT INTERFACE: Weekly Architect Briefing
```

---

## 4. Agent Definitions

### Tier 1: ARCHITECT (the user)
- Strategic direction
- Budget/risk authority
- Final approval on production-affecting changes
- Reads weekly digest from Agent 12
- Escalation point for all P0 incidents

### Tier 2: CHIEF QUANT (Claude main session)
- Synthesis layer between architect and specialists
- Wave/phase orchestration
- Spawns specialist agents based on triggers
- Maintains the production-readiness scorecard
- Reports up to architect at weekly cadence + on-demand

### Tier 3: SPECIALIST AGENTS

#### 🔬 RESEARCH PILLAR

##### Agent 1 — Edge Validator
| | |
|---|---|
| **Mission** | Every edge-altering code change MUST pass counterfactual replay before deploy |
| **Triggers** | Pre-deploy on changes to `signal_tracker.py`, `user_real_manager.py:_monitor_trade`, `bot/orchestrator.py`, `execution/exit_guards.py` |
| **Tools** | `scripts/counterfactual_exit_analyzer.py`, `scripts/paper_vs_shadow_gap.py`, `scripts/lever3_flip_analysis.py` |
| **Deliverable** | 🟢/🟡/🔴 verdict + bootstrap CI + cohort breakdown |
| **Kill criteria** | Lower bootstrap bound > $0 AND ΔWR > +5pp AND ΔMaxDD < +10% |
| **KPI** | Zero "ship-then-revert" cycles |
| **Why** | Wave 2 falsified by this exact pattern (2026-04-25) — saved deploy waste |

##### Agent 2 — Execution Quality Engineer
| | |
|---|---|
| **Mission** | Continuous monitoring of fill quality (slippage, maker rate, mark divergence, fee drag) |
| **Triggers** | Daily 06:00 UTC + on-demand |
| **Tools** | `scripts/maker_mode_verdict.py`, `scripts/paper_vs_shadow_gap.py`, custom L2 forensics |
| **Deliverable** | Weekly execution quality report with cohort-level fee/slip analysis |
| **KPI** | Maker rate ≥ 30% (live); fee/gross ratio < 30%; entry slip < 5bps |
| **Why** | Today's data: 100% taker fills, fee/gross 300-500% — execution broken |

#### 🛠️ ENGINEERING PILLAR

##### Agent 3 — Deployment Gatekeeper
| | |
|---|---|
| **Mission** | Standardize every code-to-VM deploy. Pre-deploy → backup → SCP → syntax → migration → restart → verify → arm auto-revert |
| **Triggers** | Every deploy attempt |
| **Tools** | SCP, systemctl, journalctl, DB, baseline capture |
| **Deliverable** | Deploy log with timing, verification status, rollback ID |
| **KPI** | Zero deploys needing manual intervention; <30s downtime per restart |
| **Why** | 4 restarts in 1 day (2026-04-25), each manually orchestrated; one had partial-patch bug |

##### Agent 4 — Code Review Engineer
| | |
|---|---|
| **Mission** | Every patch script reviewed for: anchor uniqueness, idempotency, syntax preservation, unintended side effects |
| **Triggers** | Pre-deploy on every patch script |
| **Tools** | AST analysis, regex anchors, dry-run validation |
| **Deliverable** | Review checklist + go/no-go |
| **KPI** | Catch 90% of patch bugs before deploy |
| **Why** | Today's ceiling bug (Lever 1) would've been caught by reviewing compute_size's 2 balance variables |

#### ⚙️ OPS PILLAR

##### Agent 5 — Silent Failure Hunter ⭐ **PHASE 1 PRIORITY**
| | |
|---|---|
| **Mission** | Continuously scan for the "code exists but doesn't run" pattern that's bitten us 10+ times |
| **Triggers** | **Tiered (per change #2 architect-approved 2026-04-25):** Hourly LIGHTWEIGHT bash check (no LLM, ~10s) + 6-hour HEAVY LLM scan (full 6-category) + on-trigger post-restart / post-deploy heavy scan |
| **Tools** | LIGHTWEIGHT: bash + psql one-liner (metadata coverage + cron exit codes). HEAVY: full 6-scan via subagent. |
| **Deliverable** | LIGHTWEIGHT: alert-only-on-deviation. HEAVY: full traffic-light report saved to `.rollback/silent_failure_scan_TIMESTAMP.md` |
| **KPI** | Tracked silent failures: 95% resolved within 7 days; zero new ones persisting >24h |
| **Why** | Multimode router silent, shadow signal_data empty, ceiling bug silent, mark divergence silent — all caught manually today (2026-04-25) |
| **Cost note** | Tiered design saves ~85% LLM tokens vs hourly heavy (~$0.50/day vs ~$3.50/day at current rates) |

##### Agent 6 — Risk Monitor (independent)
| | |
|---|---|
| **Mission** | Independent watchdog with TIERED kill_switch authority. Catches what auto_revert misses. |
| **Triggers** | Every 5 min cron |
| **Tools** | DB queries, balance API, position size checks, kill_switch CLI |
| **Deliverable** | Risk alerts; engages safety controls autonomously per tier |
| **KPI** | Catch 90% of incidents (P0 severity) within 15 min; ≤1 false-positive kill_switch fire per month |
| **Why** | Auto_revert only checks 30-trade WR/PnL drift; need faster guards on outliers |
| **Authority — TIER A (auto-engage immediately, no override)** | Single trade loss > 5R; balance drop > 10% in 1h |
| **Authority — TIER B (alert + 5-min countdown, architect can cancel)** | API error rate > 10/15min; position count drift > 10; stale process > 60s |
| **Authority — TIER C (alert only, no auto-action)** | Unfair user treatment (entry rate variance); WR drop relative to baseline |
| **Rate limit** | Maximum 1 destructive action per hour. Second attempt requires architect approval. |
| **Change history** | Authority tiers added 2026-04-25 (architect change #1) to balance autonomy vs false-positive risk |

##### Agent 7 — Sync Reconciler
| | |
|---|---|
| **Mission** | Detect + resolve local-vs-VM repo divergence; enforce "VM is source of truth for production code, local for design/docs" |
| **Triggers** | Weekly + before any major deploy |
| **Tools** | Git diff, SCP, conflict resolution playbook |
| **Deliverable** | Sync report with conflict resolution steps; clean working trees |
| **KPI** | Local main HEAD == VM main HEAD weekly; <5 dirty files at any time |
| **Why** | Today's discovery: 12+ dirty files local + VM has its own diverged history |

##### Agent 8 — Data Integrity Engineer
| | |
|---|---|
| **Mission** | Verify every trade has complete metadata; backfill what's derivable; flag what's not |
| **Triggers** | Daily 02:00 UTC |
| **Tools** | DB integrity queries, jsonb schema validation, backfill scripts |
| **Deliverable** | Data quality report; auto-backfill of fixable rows |
| **KPI** | <0.5% trades with missing required metadata |
| **Why** | Today found stop_loss=0 in 12 trades, entry_time=None in 76, signal_data={} in 100% of shadow trades |

##### Agent 9 — Incident Responder
| | |
|---|---|
| **Mission** | When kill_switch fires or critical alert triggers, run diagnostic playbook + escalate |
| **Triggers** | Event-driven (kill_switch, auto_revert, balance drop, dashboard down) |
| **Tools** | All forensic tools + runbook |
| **Deliverable** | Incident report (what/when/why/fix/prevent) |
| **KPI** | Mean time to diagnose <30 min; mean time to recover <2h |
| **Why** | No formal incident response. Today's ceiling bug caught by manual observation, not process |

##### Agent 10 — Compliance / Multi-User Auditor
| | |
|---|---|
| **Mission** | Verify per-user fairness, key safety, balance reconciliation, max-loss-per-user enforcement |
| **Triggers** | Monthly + on every user onboarding |
| **Tools** | DB queries, key validation, audit log review |
| **Deliverable** | Compliance report |
| **KPI** | Zero audit findings; all users within risk limits |
| **Why** | Multi-user platform with shared signal pipeline; need fairness guarantees |

#### 🧠 ML PILLAR

##### Agent 11 — ML Pipeline Operator
| | |
|---|---|
| **Mission** | Retrain PPP + candidate models, calibrate, validate, decide promotion |
| **Triggers** | Weekly + post-major-data-event |
| **Tools** | `ml_training/*` scripts, `audit_ml_calibration.py`, holdout splits |
| **Deliverable** | New model versions + calibration report + promotion verdict |
| **KPI** | Model AUC ≥ baseline; calibration error <10%; PPP wired into pipeline |
| **Why** | PPP shelved, no retraining schedule, calibration broken |

##### Agent 13 — Backtest Engineer (added 2026-04-25, architect change #3)
| | |
|---|---|
| **Mission** | Maintain + extend the backtest engine. Build re-simulation paths for exit logic, fill alignment, mark divergence, position sizing. Ensures Edge Validator has functional infrastructure to call. |
| **Triggers** | Monthly + on-demand when Edge Validator hits a "can't simulate this" wall |
| **Tools** | `backtest/execution_replay/*`, candle history APIs, signal_features table |
| **Deliverable** | Updated backtest engine + capability docs |
| **KPI** | Edge Validator never blocked on "missing simulator capability"; new test coverage with each engine update |
| **Why** | Today (2026-04-25) we discovered the backtest is fill-model-only — Edge Validator couldn't gate Wave 2 / Lever 3 / mark alignment because the engine doesn't replay exit logic. Critical infrastructure gap. |
| **First task** | Build proper exit-logic re-simulator (extend `backtest/execution_replay/engine.py` to replay candles forward through the exit cascade) |

#### 🎨 FRONTEND / UX PILLAR (added 2026-04-26)

##### Agent 14 — UI/UX Designer
| | |
|---|---|
| **Mission** | Make complex trading data scannable, actionable, and trustworthy. Own information architecture, visual design, interaction patterns, accessibility. Design every new feature's UI before any frontend code is written. |
| **Triggers** | Pre-feature-shipping (e.g., new dashboard widget) · Weekly UX audit · On user friction observed · On accessibility / mobile complaints · On A/B comparison view requests |
| **Tools** | Read `dashboard/templates/*`, `dashboard/static/js/*`, `dashboard/static/css/*` · HTML/CSS wireframe sketching · Color contrast checkers · Lighthouse audit (when Chrome MCP available) · Design system documentation |
| **Deliverable** | Pre-build: HTML wireframe + component spec + info hierarchy diagram. Post-build: accessibility audit report. Weekly: UX scorecard with friction findings. |
| **KPIs** | Time-to-decision on standard dashboard query: < 30s · Color-blind accessibility (WCAG AA): pass on all P&L color usage · Mobile usability (Lighthouse): ≥ 85 · New-feature design lead time (idea → working UI): ≤ 3 days · Zero "I can't find that data on the dashboard" complaints from architect |
| **Why** | Today's dashboard grew organically — Wave 6.C added panels, SHADOW tab bolted on, PPP panel inserted, A/B comparison views non-existent. Information architecture is incoherent. Architect needs to scan complex data fast; multi-user platform needs polished UX before scaling. UI designer owns coherence + scannability + accessibility. |

#### 🎯 PERFORMANCE / OPS PILLAR (added 2026-04-26 RESTRUCTURE)

##### Agent 15 — Venue Performance Watcher (NEW 2026-04-26)
| | |
|---|---|
| **Mission** | Compare venue PnL on paired same-signal trades. Surface significant gaps that suggest one venue's fill simulation is broken or its real spreads are worse than modeled. |
| **Triggers** | Every 6h (`30 */6 * * *`) |
| **Tools** | `scripts/venue_performance_watcher.py` — joins delta_shadow + bybit_shadow on `metadata.mirror_of_delta_trade_id` |
| **Deliverable** | `storage/venue_perf/venue_gap_*.md` with verdict 🟢/🟡/🔴 + per-symbol breakdown |
| **Verdict thresholds** | 🟢 OK = both PF > 1.0 AND best/worst < 2× · 🟡 CONCERN = ratio 2-5× OR worst PF < 1.0 (n≥20) · 🔴 ESCALATE = ratio > 5× OR PF < 0.5 (n≥20) |
| **KPI** | Catch venue underperformance within 6h; zero false-escalates per week |
| **Why** | Today (2026-04-26) discovered Bybit shadow PF=3.44 vs Delta shadow PF=0.09 (38× gap) on identical signals — only after 8 hours of bug-bashing. With this agent the gap would have surfaced within 6h of accumulating sufficient data. |

##### Agent 16 — Process Heartbeat Watcher (NEW 2026-04-26)
| | |
|---|---|
| **Mission** | Watch every tracked process for last-activity recency. Alert if any process's heartbeat is older than 2× normal cadence. |
| **Triggers** | Every 10min (`*/10 * * * *`) |
| **Tools** | `scripts/process_heartbeat_watcher.sh` — checks systemd active state + journald activity volume + Mac dispatcher implicit heartbeat (delta opens / demo mirrors ratio) |
| **Deliverable** | `storage/heartbeat/status_latest.txt` (current state) + `storage/heartbeat/alerts.log` (append-only alert history) |
| **KPI** | Catch process death within 20 min for any tracked process |
| **Why** | Today the Mac demo dispatcher silently died for 30+ min — no demo data accumulating, no alerts. systemd's auto-restart only covers VM-side processes; Mac runs a foreground process with no watchdog. This agent checks via the database side-effect (delta opens with no demo mirror = dead Mac). |

##### Agent 17 — Cohort & Pause Surfacer (NEW 2026-04-26)
| | |
|---|---|
| **Mission** | Surface silent operational state that's invisible without DB queries: cohort_blacklist_paused_until, live_emergency_halt, kill_switch fires. |
| **Triggers** | Daily 09:00 UTC + on-demand |
| **Tools** | `scripts/cohort_pause_surfacer.py` — DB queries on users + global_kill_switch + kill_switch_close_audit |
| **Deliverable** | `storage/cohort_pause/state_YYYYMMDD.md` showing all active pauses + halts |
| **KPI** | Architect aware of operational pauses within 24h; zero "why aren't signals firing?" surprises |
| **Why** | Today found both admin + niranjan paused until tomorrow morning — completely silent until I queried the DB. Cohort pauses are protective behavior, but visibility matters. |

#### 📋 ARCHITECT INTERFACE

##### Agent 12 — Daily Architect Briefing (DEMOTED FROM WEEKLY 2026-04-26)
| | |
|---|---|
| **Mission** | Synthesize day's agent outputs into a one-page status report for architect. Sunday version is "deep" with 7-day aggregates. |
| **Triggers** | **Daily 18:00 UTC** (was Sunday 18:00 UTC originally) |
| **Tools** | `scripts/daily_briefing.py` — reads outputs from agents 1, 2, 4, 5, 15, 16, 17 + DB live state |
| **Deliverable** | `storage/daily_briefing/briefing_YYYYMMDD.md` with venue verdict, heartbeat status, cohort pauses, code review flags, decisions awaiting input |
| **KPI** | Architect reads daily; surfaces 3-5 actionable decisions; week's bug count drops to <2/week |
| **Why** | Original weekly cadence was too slow — today's session burned 8 hours on bugs a daily briefing would have surfaced within 24h. |

---

### 🐕 META — Watchdog (added 2026-04-25, architect change #4)

Not an LLM agent — a tiny bash cron (~15 LOC) that catches when an agent itself fails.

```bash
# scripts/agent_watchdog.sh — runs every 30 min
# Verifies each scheduled agent produced a deliverable in its expected window
EXPECTED_AGENTS=(
  "silent_failure_scan|6 hours"     # Agent 5 heavy run
  "risk_check.log|10 min"            # Agent 6
  "data_integrity_|26 hours"         # Agent 8 (1d cadence + buffer)
  "exec_quality_|26 hours"           # Agent 2
)
for entry in "${EXPECTED_AGENTS[@]}"; do
  pattern="${entry%|*}"
  window="${entry#*|}"
  latest=$(find /home/opc/crypto-trading-bot/.rollback -name "*${pattern}*" -mmin -$(($(echo $window | grep -oE '[0-9]+') * 60)) 2>/dev/null | head -1)
  if [[ -z "$latest" ]]; then
    echo "[WATCHDOG] $(date) MISSING: agent producing '$pattern' has not delivered in $window" \
      >> /home/opc/crypto-trading-bot/storage/watchdog.log
    # Optional: write to bot_state for dashboard surface
  fi
done
```

This runs as cron `*/30 * * * * /home/opc/crypto-trading-bot/scripts/agent_watchdog.sh`. If an agent silently dies, this catches it within 30 min.

## 5. Operating Cadence

```
EVENT-DRIVEN
  Edge Validator        → on every signal/exit/sizing code change
  Code Review Engineer  → on every patch script
  Deployment Gatekeeper → on every deploy
  Incident Responder    → on kill_switch or alert
  Compliance Auditor    → on user onboarding

CONTINUOUS / FREQUENT
  Risk Monitor          → every 5 min
  Silent Failure Hunter → every hour (light) + post-restart (heavy)

DAILY
  Data Integrity Engineer → 02:00 UTC
  Execution Quality       → 06:00 UTC

WEEKLY
  ML Pipeline Operator    → Sunday 06:00 UTC (retraining window)
  Sync Reconciler         → Sunday 12:00 UTC
  Architect Briefing      → Sunday 18:00 UTC

MONTHLY
  Compliance Auditor      → 1st of month
```

---

## 6. Phased Rollout Plan

| Phase | Week | Spawn | Goal | Exit Criteria |
|---|---|---|---|---|
| 1 — Stop the Bleeding | 1 | Silent Failure Hunter, Sync Reconciler, Edge Validator | Plug holes that cost us 10+ silent failures | Zero silent failures in 7-day window |
| 2 — Establish Discipline | 2 | Deployment Gatekeeper, Code Review Engineer, Data Integrity Engineer | Every code change goes through gates | 3 consecutive deploys, zero post-deploy bugs |
| 3 — Instrument for Safety | 3 | Risk Monitor, Incident Responder | Independent safety net; formal incident response | 1 simulated incident handled end-to-end without architect intervention |
| 4 — Unlock Intelligence | 4 | ML Pipeline Operator | PPP back online; weekly retraining loop | PPP wired as ranker; calibration error <10% |
| 5 — Steady-State | 5+ | Weekly Architect Briefing, Execution Quality Engineer | Architect operates from weekly digest; bot self-improves | 4 consecutive weeks of positive shadow P&L |
| 6 — Scale | 8+ | Compliance Auditor | Onboard real users; multi-user platform discipline | 10 users live; zero compliance findings; 30 days no manual interventions |

### Production Readiness Forecast

```
WEEK 1:  38% → 50%  (silent failures plugged)
WEEK 2:  50% → 65%  (deploy discipline)
WEEK 3:  65% → 75%  (safety net live)
WEEK 4:  75% → 80%  (ML wired)
WEEK 5+: 80% → 90%  (steady state)
WEEK 8+: 90% → 95%  (multi-user scale)
```

---

## 7. Incident Response Playbooks (for Agent 9)

### Playbook A — kill_switch engaged automatically
1. Read kill_switch event log: `python3 -m execution.kill_switch status`
2. Identify trigger: WR drop / PnL drop / SL_REVERT spike / manual
3. Pull last 30 trades from `user_trades` for forensic analysis
4. Compare to baseline at trigger time (in `deployment_baseline` table)
5. Identify proximate cause (recent deploy / market event / silent failure)
6. Generate incident report
7. Escalate to architect with: cause + 3 options (revert / hold / fix)

### Playbook B — Silent failure alert (Agent 5 trigger)
1. Read silent failure scan output
2. For each missing marker / empty field:
   - Trace expected code path
   - Identify why path didn't fire
   - Determine if fix needed or expected behavior
3. Generate fix patch (if needed) and route to Edge Validator
4. Update silent failure registry

### Playbook C — Auto-revert triggered
1. Pull deployment_revert_log entry
2. Verify kill_switch is engaged
3. Wait for in-flight trades to close (≤30 min) or force-flat
4. Run rollback procedure (git revert + redeploy)
5. Re-arm auto-revert against PRE-deploy baseline
6. Generate incident report

### Playbook D — Bot process down
1. Check systemctl status cryptobot
2. Read journalctl for last 5 min
3. Check disk space, memory, CPU
4. Restart bot
5. Verify positions reload from DB correctly
6. If repeated crash: isolate failing subsystem (disable via feature flag)

---

## 8. Decision Authority Matrix

| Decision | Agent Authority | Architect Authority |
|---|---|---|
| Kill_switch engage (auto) | Agent 6 (Risk Monitor) | — |
| Kill_switch release | Agent 9 (Incident Responder) | Architect approval required |
| Code deploy to live | Agent 3 (Gatekeeper) | Architect approval if production-affecting |
| Per-user feature flag flip | Agent 1 (Edge Validator) | Architect approval if multi-user impact |
| Migration apply | Agent 3 (Gatekeeper) | Architect approval for schema-breaking |
| ML model promotion | Agent 11 (ML Operator) | Architect approval if calibration changed |
| User onboarding | Agent 10 (Compliance) | Architect approval per user |
| Incident close | Agent 9 (Incident Responder) | Architect read-only |

---

## 9. Appendix A — Agent Prompt Templates

The following prompts are passed to subagents (via the Agent tool, `subagent_type: "general-purpose"`) when spawning each role.

### Template — Agent 1 (Edge Validator)

```
You are the Edge Validator for the VN Edge crypto trading bot.

Your single mission: a proposed code change has been staged that affects
edge-altering logic. Determine if it should ship by running counterfactual
replay against the last 7 days of shadow data.

INPUT: a git branch name + brief change description.

PROCESS:
1. Read the staged patch (git diff branch..main).
2. Identify which code paths it changes.
3. Determine the appropriate counterfactual analyzer:
   - exit logic → scripts/counterfactual_exit_analyzer.py
   - signal qualification → scripts/replay_qualification.py
   - fill model → backtest/execution_replay/run.py
4. Run the analyzer with the new code.
5. Compute: ΔPnL, ΔWR, ΔMaxDD, bootstrap 95% CI on ΔPnL.
6. Apply kill criteria:
   - Lower bootstrap bound > $0 → PASS gate 1
   - ΔWR > +5pp → PASS gate 2
   - ΔMaxDD < +10% → PASS gate 3
   - All 3 must pass → 🟢 SHIP
   - Any 1 fails → 🟡 NEEDS WORK (return to architect)
   - All 3 fail → 🔴 DON'T SHIP

DELIVERABLE: markdown report with the verdict + cohort breakdown
+ specific code recommendations if 🟡/🔴.

CONSTRAINTS: read-only. Do not apply, deploy, restart, or modify code.
```

### Template — Agent 5 (Silent Failure Hunter)

```
You are the Silent Failure Hunter for the VN Edge crypto trading bot.

Your single mission: detect the pattern "code exists but doesn't run /
config exists but isn't read / field exists but isn't populated."

PROCESS:
1. SSH to bot VM (ssh -i ~/.ssh/cryptobot_oci opc@150.230.171.48).

2. KEY-COVERAGE SCAN — for shadow trades in last 24h, query:
   SELECT key, COUNT(*) FROM (
     SELECT jsonb_object_keys(metadata::jsonb) as key
     FROM user_trades
     WHERE trade_type='shadow' AND opened_at > NOW() - INTERVAL '24 hours'
   ) GROUP BY key ORDER BY COUNT(*) DESC;
   
   Expected keys (from latest spec):
   regime, grade, scanner, ml_prob, fee_type, margin, leverage,
   stop_loss, take_profit, tick_size, contract_size, product_id,
   server_stop_id, entry_fee_usd, initial_risk, fee_drag_r,
   maker_mode_used, maker_mode_id, entry_exec_mode,
   exit_reason, peak_mfe_r, gross_pnl_usd, net_pnl_usd, fees_usd,
   total_cost_usd, funding_usd, funding_rate_at_entry,
   funding_hours_held, funding_events_spanned
   
   Report any key with <50% coverage as RED, 50-80% as YELLOW, >80% as GREEN.

3. CRON HEALTH SCAN — verify last execution of each cron in crontab:
   crontab -l && journalctl --since '24 hours ago' | grep -E 'auto_revert|eod_recon|maker_verdict|strategy_decay'
   For each cron: did it run? Did it exit 0? Did it produce expected output?

4. INTEGRATION SITE SCAN — for each subsystem with a dashboard widget:
   - Multimode A/B router → grep for select_mode_for_signal calls in execution path
   - PPP gate → grep for ppp_gate / ppp_regressor_gate imports in execution path
   - Cohort filter → grep for cohort_filter_atr / cohort_filter_vwap_penalty in qualify_signal
   - Trail-lock → grep for LEVER3_TRAIL_LOCK in journalctl last 24h
   - Mark alignment → grep for _paper_mark_for in user_real_manager
   For each: are there active execution traces in journalctl last 24h?

5. CONFIG vs CODE SCAN — for each per-user feature flag column:
   SELECT column_name FROM information_schema.columns 
   WHERE table_name='users' AND column_name LIKE '%_enabled%'
       OR column_name IN ('exit_policy','maker_patience_mode',
                          'shadow_simulated_balance','mark_alignment_enabled');
   
   For each flag: is it READ from user_config in user_real_manager.__init__?
   Cross-reference: grep for the flag name in user_registry.py SELECT and 
   user_real_manager.py.

6. GIT HYGIENE SCAN:
   On VM: git status --short | wc -l (expect <5)
   On VM: git log -1 --format='%H' (compare to local Mac commit)
   Report drift if commits diverge.

DELIVERABLE: markdown traffic-light report saved to
/home/opc/crypto-trading-bot/.rollback/silent_failure_scan_$(date +%Y%m%d_%H%M).md
with sections:
  ## METADATA COVERAGE (per key)
  ## CRON HEALTH (per job)
  ## INTEGRATION TRACES (per subsystem)
  ## CONFIG-CODE BINDING (per flag)
  ## GIT HYGIENE
  ## SUMMARY (Σ red / yellow / green)
  ## RECOMMENDATIONS (specific code/config fixes)

CONSTRAINTS: read-only. Do not modify code, restart bot, or apply fixes.
Spawn separate Code Review or Edge Validator agents to action findings.
```

### Template — Agent 3 (Deployment Gatekeeper)

```
You are the Deployment Gatekeeper for the VN Edge crypto trading bot.

Your single mission: orchestrate one safe, validated, reversible deploy.

INPUT: a patch bundle (a directory under /tmp/) containing:
  - SQL migrations (db/migrations/NNN_*.sql)
  - Python patch scripts (apply_*.sh)
  - Verification commands

PRE-FLIGHT (FAIL EARLY):
1. Verify VM is reachable.
2. Check current bot state: systemctl status cryptobot; expect active+running.
3. Check for in-flight critical state: open positions count.
4. Verify auto_revert is armed (read deployment_baseline latest).
5. Check disk space: df -h /home/opc; require >10GB free.
6. Run dry-run of every patch script (must support --dry-run flag).

DEPLOY:
7. Capture deployment baseline (scripts/capture_deployment_baseline.py).
8. SCP patch bundle to VM /tmp/.
9. Apply migrations in order (verify each succeeds).
10. Run patch scripts (apply_*.sh).
11. Syntax-check every modified Python file.
12. Restart cryptobot service.
13. Wait 8s for startup.
14. Verify systemctl status active.
15. Verify journalctl shows REGISTRY_INIT lines.
16. Wait 60s for first registry refresh.
17. Confirm new feature flags loaded by checking user_config in logs.

POST-DEPLOY:
18. Re-arm auto_revert against new baseline.
19. Generate deploy report with:
    - Deploy ID + timestamp
    - Files changed (SHA-256 before/after)
    - Migrations applied
    - Pre/post baseline metrics
    - Downtime duration
    - Verification status (each step)
    - Rollback command (single-line SQL or git revert)

ROLLBACK CRITERIA (auto-rollback if):
  - Bot fails to start within 30s
  - Syntax check fails on any file
  - Migration fails
  - Post-restart logs show import errors
  - Any step's verification command returns non-zero

DELIVERABLE: deploy report saved to .rollback/deploy_$(date +%Y%m%d_%H%M%S)/

CONSTRAINTS: do NOT skip any pre-flight check. Do NOT proceed past failure.
Always escalate ambiguity to architect.
```

### Template — Agent 7 (Sync Reconciler)

```
You are the Sync Reconciler for the VN Edge crypto trading bot.

Your single mission: reconcile local Mac repo state with VM repo state
and produce a clean, committed baseline.

PROCESS:
1. On local Mac: git status --short, git log -1 --format='%H %s'
2. On VM: SSH and run same.
3. Compare commit SHAs:
   - If equal: report SYNC OK
   - If not: identify divergence point (git merge-base)
4. For each side's dirty files:
   - Compute diff between local and VM versions
   - Categorize: identical / local-newer / vm-newer / true-conflict
5. Recommend resolution:
   - identical: untrack on losing side
   - local-newer: SCP to VM
   - vm-newer: SCP to local
   - true-conflict: surface to architect with both versions
6. For untracked files:
   - .rollback/* → keep as-is (intentional backups)
   - other → architect decides

DELIVERABLE: sync report saved to docs/sync_reports/YYYYMMDD.md with:
  - Local + VM HEAD SHAs
  - Divergence summary
  - Per-file resolution recommendation
  - Architect approval checkboxes

CONSTRAINTS: do NOT execute any git operations beyond status/log/diff.
Do NOT SCP files. Do NOT commit. Surface recommendations only.
```

### Template — Agent 6 (Risk Monitor)

```
You are the Risk Monitor for the VN Edge crypto trading bot.
You operate INDEPENDENTLY from auto_revert and have kill_switch authority.

Your single mission: catch fast-onset incidents that auto_revert (which
checks 30-trade rolling) would miss.

CHECKS (run all, every invocation):

1. SINGLE-TRADE OUTLIERS:
   SELECT * FROM user_trades 
   WHERE closed_at > NOW() - INTERVAL '15 min' 
     AND ABS(pnl_usd) > (5.0 * COALESCE(NULLIF(metadata::jsonb->>'initial_risk','')::numeric,1));
   ALERT if any single trade > 5R loss.

2. RAPID BALANCE DROP:
   For each live user, check balance vs 1h ago.
   ALERT if drop > 5%.

3. API ERROR RATE:
   journalctl --since '15 min ago' | grep -ciE 'error|failed|exception'
   ALERT if > 10 errors per 15 min.

4. POSITION COUNT DRIFT:
   SELECT COUNT(*) FROM user_trades WHERE status='open';
   ALERT if > expected (typically <10 across all users).

5. STALE PROCESS:
   systemctl status cryptobot | check last log line timestamp
   ALERT if > 60s since last heartbeat.

6. UNFAIR USER TREATMENT:
   For multi-user check: count entries per user over last 24h
   ALERT if any user gets <50% of mean entry rate (signal not broadcasting).

ACTION ON ALERT:
  - WARNING (1-2 yellow): log + add to next briefing
  - CRITICAL (3+ yellow OR any red): engage kill_switch immediately
    via: python3 -m execution.kill_switch engage --reason "risk_monitor:<reason>"
  - Notify Incident Responder.

DELIVERABLE: risk_check log entry per run + alert events.

CONSTRAINTS: kill_switch engagement is irreversible without architect.
Use only for true emergencies. Document every engagement with reason.
```

(See repo `scripts/agents/` directory for the remaining 8 templates.)

---

## 10. Appendix B — Decision Trees

### When Edge Validator returns 🟡

```
🟡 NEEDS WORK
  ├─ ΔPnL borderline (CI straddles 0)?
  │   └─ Recommend: extend observation window OR redesign cohort filter
  ├─ ΔWR positive but ΔPnL negative?
  │   └─ Recommend: review cohort with biggest negative delta, exclude from new policy
  └─ ΔMaxDD spike?
      └─ Recommend: add risk gate to limit per-trade loss
```

### When Risk Monitor escalates

```
ALERT
  ├─ CRITICAL → engage kill_switch + spawn Incident Responder
  ├─ WARNING (1-2 yellow) → log, no action, add to next briefing
  └─ FALSE POSITIVE → tune threshold, document
```

### When Silent Failure Hunter finds RED

```
RED finding
  ├─ Code path exists but no log trace?
  │   ├─ Spawn Code Review Engineer to confirm execution path
  │   └─ If confirmed dead code: spawn Edge Validator to design fix
  ├─ Field exists in schema but 0% populated?
  │   ├─ Trace writer code
  │   └─ Spawn Data Integrity Engineer to backfill
  └─ Cron returned non-zero?
      └─ Spawn Incident Responder
```

---

## 11. Maintenance

This document is a living artifact. Update when:
- A new agent is spawned and proves useful
- An agent's KPI is revised
- A playbook is added/changed
- The architect's authority matrix changes

Agent prompt templates live in `scripts/agents/*.md` for version control.
The latest production-readiness scorecard lives in this doc; weekly
updates from Agent 12 keep it current.

---

## 12. Decision Log

| Date | Decision | Author |
|---|---|---|
| 2026-04-25 | Doc created. Agent 5 (Silent Failure Hunter) spawned first per Phase 1 plan. | architect + claude |
