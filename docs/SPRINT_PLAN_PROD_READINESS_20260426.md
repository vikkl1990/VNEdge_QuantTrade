# Sprint Plan — Path from 62% → 95% Production-Ready
**Authored:** 2026-04-26 EOD
**Baseline:** Composite production-readiness score 62% (per audit, see scorecard below)
**Target:** 95% composite, live-trading enabled with $500/user cap initially
**Estimated total effort:** 7 weeks of focused work

---

## Operating principle

Each sprint should move ≥ 1 dimension by ≥ 15 points. No sprint ships if its acceptance criteria aren't measurably hit. **Every sprint ends with a demo + sign-off — not just a code drop.**

---

## Sprint 1 (Week 1) — "Strategy Validation + Test Foundations"
**Composite target: 62% → 75% (+13)**
**Effort: 5 days**

### Goals
- Validate strategy edge survives execution friction (FIX 1+2 + Stage 1+2 A/B verdict)
- Lift test coverage from 25% → 45%
- Establish CI to catch regressions before merge

### Deliverables (acceptance criteria)
1. **A/B verdict by EOD day 1**
   - Niranjan delta_shadow PF ≥ 1.5 sustained 24h
   - Or A/B fails → diagnose strategy/exit gap
2. **GitHub Actions CI live**
   - Runs `pytest tests/` on every push to any branch
   - Runs syntax check + ruff lint
   - Required for PR merge to `main`
3. **Integration test suite delivered**
   - Bug 1-5 regression tests (5 tests pinning today's fixes)
   - `compute_size()` table-driven tests (10 cases: shadow_sim_balance / cached / default100, all grades, all multipliers)
   - `qualify_signal()` gate tests (22 gates, ~30 tests)
   - `should_kill_dead_signal()` relaxed_shadow vs standard tests (10 cases)
   - End-to-end smoke test: paper signal → broadcast → 2 user managers fire → close
4. **Test coverage instrumented**
   - `pytest --cov` in CI
   - Coverage badge in README
   - Daily Briefing reports coverage delta

### Sprint 1 dimensions impacted
- Test Coverage: 25% → 45% (+20)
- Repeatability: 70% → 80% (+10)
- Reliability: 75% → 78% (+3)
- Profitability: 15% → 35% (if A/B succeeds)

---

## Sprint 2 (Week 2) — "Scalability + Live Path Hardening"
**Composite target: 75% → 83% (+8)**
**Effort: 5 days**

### Goals
- Stress-test multi-user beyond current 2-user comfort zone
- Harden the LIVE execution path (which has been touched <4h total)
- Build the load test harness so future scaling questions are answerable

### Deliverables
1. **Load test fixture**
   - Synthetic users (10, 50, 100) generated via factory
   - Synthetic signal generator (replay 24h paper signals at 5× speed)
   - Measure: broadcast latency p50/p95/p99, DB connection count, monitor task count, memory
2. **DB connection pool tuning**
   - Right-size `asyncpg.create_pool` based on user count
   - Add metrics: pool exhaustion alerts via Agent 6 (Risk Monitor)
3. **Live execution path tests**
   - Mock Bybit + Delta exchange responses (success / rate-limit / network error)
   - `place_order` retry logic exercised
   - Order-state-machine tests (PENDING → FILLED → MONITORED → CLOSED)
4. **Mac dispatcher launchd watchdog**
   - Plist file that auto-restarts dispatcher on crash
   - Heartbeat published to VM every 60s; Agent 16 alerts if missing > 5 min

### Sprint 2 dimensions impacted
- Scalability: 50% → 75% (+25)
- Reliability: 78% → 82% (+4)
- Test Coverage: 45% → 55% (+10)

---

## Sprint 3 (Weeks 3-6) — "30-Day Bybit Live Demo Validation"
**Composite target: 83% → 88% (+5)**
**Effort: 30 days monitoring**

### Goals
- Real-money validation on Bybit demo for 30 consecutive days
- Generate the EVIDENCE base needed for live-trading sign-off
- Build the on-call discipline that production demands

### Deliverables
1. **Bybit demo $500/user cap continuous trading**
   - 30 days uninterrupted (any 4h+ outage resets the clock)
   - Mac dispatcher uptime ≥ 99% (monitored by Agent 16)
   - Agent 9-A handles all routine incidents autonomously
   - Architect reviews Daily Briefing each morning
2. **Acceptance metrics (must hit ALL)**
   - Sustained PF ≥ 1.5 weekly, ≥ 1.3 daily
   - Max consecutive losers < 5
   - Zero unplanned auto_revert engages
   - Zero stuck-trade pile-ups (>10 open trades for >30 min)
3. **On-call rotation defined**
   - Architect + 1 designated backup
   - Pager via Telegram (existing) + email
   - SLA: P0 ack ≤ 15 min, P1 ack ≤ 1 hour
   - Escalation tree documented
4. **DR drill**
   - Restore from S3 backup to fresh VM
   - Resume trading within 30 min of cold-start
   - Validated quarterly

### Sprint 3 dimensions impacted
- Profitability: 35% → 65% (sustained edge proven)
- Recoverability: 85% → 95% (+10, DR drill done)
- Safety: 80% → 88% (+8, no kill-switch fires for 30d)

---

## Sprint 4 (Weeks 7-8) — "Compliance + Live Multi-User Cap-Up"
**Composite target: 88% → 95% (+7)**
**Effort: 2 weeks**

### Goals
- Legal/compliance framework for paying users
- Cap raise from $500 → $5K per user with safety guards
- Final test coverage push to 70%+

### Deliverables
1. **KYC/AML framework**
   - Per-user identity verification on signup
   - Risk-disclosure agreement (one-time signed acknowledgement)
   - Source-of-funds checks for deposits > $1K
   - Audit log of every consent event
2. **Per-user fee disclosure**
   - Dashboard tab: "What you've paid in fees + bot revenue share"
   - Daily emailed statements
3. **Cap raise to $5K**
   - Gradual: 1 user week 1, 3 users week 2, 5+ thereafter
   - Per-user circuit breaker raised proportionally
   - Auto-revert thresholds re-tuned for higher capital
4. **Final test push**
   - Coverage 55% → 70%+
   - Mutation testing on critical paths (`pytest --mutmut`)
   - Property-based tests for `compute_size()` and exit-guards

### Sprint 4 dimensions impacted
- Compliance: 55% → 90% (+35)
- Test Coverage: 55% → 70% (+15)
- Documentation: 85% → 95% (+10)

---

## Risk register (things that could derail the plan)

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| A/B verdict fails (PF stays < 1) | Medium | High | Already have escape: revert to control + diagnose strategy |
| Bybit demo bans Mac IP | Low | High | Have fallback: Singapore VPS, ~$10/mo, ready in 1 day |
| 30-day demo hits unexpected exchange API change | Medium | Medium | Agent 9 catches via journald scan; rollback in <30 min |
| KYC framework takes longer than 2 weeks (legal review) | High | Medium | Start KYC research in Sprint 2 in parallel |
| Multi-user load test surfaces unfixable architecture limits | Low | High | Plan B: shard by user count, single-process scales to 50 |
| Architect availability constraints | Medium | High | Agents 9-A + 14-A + Daily Bug Worker reduce manual load |

---

## Per-dimension sprint mapping

| Dimension | Today | After S1 | After S2 | After S3 | After S4 (95% target) |
|---|---:|---:|---:|---:|---:|
| Reliability | 75% | 78% | 82% | 88% | 95% |
| Safety | 80% | 80% | 82% | 88% | 95% |
| Profitability | 15% | 35% | 50% | 65% | 80% |
| Auditability | 75% | 80% | 82% | 90% | 95% |
| Repeatability | 70% | 80% | 85% | 90% | 95% |
| Recoverability | 85% | 85% | 88% | 95% | 95% |
| Scalability | 50% | 50% | 75% | 80% | 90% |
| Compliance | 55% | 55% | 60% | 70% | 90% |
| Documentation | 85% | 85% | 87% | 92% | 95% |
| Test Coverage | 25% | 45% | 55% | 60% | 70% |
| **Composite** | **62%** | **67%** | **74%** | **82%** | **91%** |

**Note**: Hitting 91% (not 95%) by end of Sprint 4 is realistic. The remaining 4 points come from sustained operation discipline (no incidents, predictable cadence, mature on-call) which can't be sprinted — needs months of clean operation.

---

## What's NOT in scope of this plan

These are deliberately deferred to a later sprint cycle (post-95%):

- Multi-exchange portfolio rebalancing
- Cross-asset hedging
- Mobile app
- Public marketing site
- Automated tax reporting
- ML model retraining pipeline (Agent 11 handles current)
- Community/affiliate features

These would be Q2 2027 work, after the bot is operationally boring.

---

## Daily cadence during execution

Each weekday during sprints 1-4:

```
09:30 IST   Daily Bug Worker fires (reads briefing, fixes top issue)
12:30 IST   Code Review Auto-Patcher fires (analyses any rollback diffs)
14:30 IST   Wave6c verdict cron fires (during A/B period)
18:00 UTC   Daily Briefing fires
After 18:00 Architect reviews briefing, makes go/no-go calls
```

---

## Sign-off requirement

This plan only proceeds if architect explicitly approves each sprint's deliverables. NO autonomous progression to next sprint without sign-off — even by active agents.

Default state: pause-and-await-architect after each sprint completion.
