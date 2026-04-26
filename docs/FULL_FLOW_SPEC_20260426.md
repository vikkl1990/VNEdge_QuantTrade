# VN Edge — Full System Flow Spec
**Status:** Architect Review · 2026-04-26 · revision after today's bug bash

---

## 1. Problem Statement

VN Edge is a multi-user crypto perp trading bot whose **core thesis** is that
a paper-only "edge engine" produces signals with measurable alpha, and we
mirror those signals into multiple execution venues simultaneously to
**isolate edge from execution friction**:

```
PAPER (oracle, no friction)
   │
   └──► DELTA India SHADOW (per-user, sim taker fills @ Delta L2)
   │                ─────────────► comparison: how much edge survives Delta?
   │
   └──► BYBIT SHADOW (per-user, sim taker fills @ Bybit L2)
   │                ─────────────► comparison: how much edge survives Bybit?
   │
   └──► BYBIT DEMO (per-user, real Bybit demo orders, real exchange clock)
                    ─────────────► validates: does real-exchange execution
                                              reproduce shadow's PnL?
```

**Goal:** Pick the venue that preserves the most paper edge after slippage,
fee, and latency friction. **Today's data says Bybit beats Delta India by
$127/day on identical signals (PF 3.44 vs 0.09).**

Ultimate state: PAPER continues as oracle; ONE shadow venue continues as
control; the winning venue (currently Bybit) becomes `real`.

---

## 2. Topology

```
┌────────────────────────────────────────────────────────────────────────┐
│                  ORACLE CLOUD VM (Oracle Cloud India)                  │
│                          150.230.171.48                                │
│                                                                        │
│   ┌─────────────────────────┐   ┌──────────────────────────────────┐   │
│   │  cryptobot.service      │   │  bybit-shadow-daemon.service     │   │
│   │  (main trading engine)  │   │  (mirrors delta_shadow OPENS     │   │
│   │  - paper signals        │   │   AND CLOSES into bybit_shadow   │   │
│   │  - per-user mgrs        │   │   via Bybit public WS L2)        │   │
│   │  - delta_shadow exec    │   └──────────────────────────────────┘   │
│   │  - dashboard (8080)     │                                          │
│   └─────────────────────────┘   ┌──────────────────────────────────┐   │
│                                 │  bybit-shadow-monitor.service    │   │
│                                 │  (Phase 2 independent exit guard │   │
│                                 │   for bybit_shadow trades —      │   │
│                                 │   own L2 cache, own SL/TP/trail) │   │
│                                 └──────────────────────────────────┘   │
│                                                                        │
│   ┌─────────────────────────────────────────────────────────────┐      │
│   │  PostgreSQL (vnedge db)                                     │      │
│   │  - users          — per-user config, ssb, max_lev           │      │
│   │  - user_trades    — every trade (paper/shadow/demo/real)    │      │
│   │  - user_api_keys  — Fernet-encrypted per-exchange keys      │      │
│   └─────────────────────────────────────────────────────────────┘      │
└────────────────────────────────────────────────────────────────────────┘
                                    ▲
                                    │ ssh+psql, REST API
                                    │
┌─────────────────────────────────── ┴ ────────────────────────────────┐
│                  ARCHITECT'S MAC (residential IP)                    │
│                                                                      │
│   bybit_demo_dispatcher.py (foreground process)                      │
│   - polls VM for new delta_shadow opens                              │
│   - places real Market IOC orders on api-demo.bybit.com              │
│   - records back to VM as exchange='bybit', trade_type='demo'        │
│   - polls for delta closes → mirrors close as bybit_demo close       │
│                                                                      │
│   Why Mac, not VM? VM is geo-blocked from Bybit REST endpoints       │
│   (CloudFront India block). Mac residential IP is whitelisted.       │
│   Bybit DEMO supports order placement; Bybit LIVE would require      │
│   non-VM infra too (or VM via Trade-WS bypass — built but not live). │
└──────────────────────────────────────────────────────────────────────┘

EXCHANGE ENDPOINTS
- Delta India    REST:  api.india.delta.exchange    (VM uses)
                 WS:    socket.india.delta.exchange (VM uses, demo mode)
- Bybit Public   WS:    stream.bybit.com/v5/public/linear (VM + Mac use)
- Bybit Demo     REST:  api-demo.bybit.com          (Mac uses)
- Bybit Live     REST:  api.bybit.com               (geo-blocked from VM;
                                                    Trade-WS workaround built)
```

---

## 3. Trade Lifecycle (parallel 4-path)

A single qualified paper signal fans out to **6 trade rows per user** (2 users
× 3 mirrors = 6 rows in user_trades, plus the paper oracle row):

```
[scanner fires → ML rank → grade gate → qualify_signal] (in cryptobot.service)
                          │
                          ▼
                ┌─── PAPER (oracle, 1 row) ────────────┐
                │   trade_type='paper'                 │
                │   exchange=NULL (or 'delta_india')   │
                │   exec via signal_tracker (no fees)  │
                └──────────────────────────────────────┘
                          │
                          │  (UserRealRegistry.broadcast_signal)
                          │  (per active user with bot_mode=shadow_live)
                          ▼
              ┌─── PER USER (admin + niranjan) ────┐
              │                                    │
              ▼                                    ▼
     ┌─ ADMIN ──────────────────────┐    ┌─ NIRANJAN ─────────────────┐
     │                              │    │                            │
     │  delta_shadow row (1)        │    │  delta_shadow row (1)      │
     │  exchange='delta_india'      │    │  exchange='delta_india'    │
     │  trade_type='shadow'         │    │  trade_type='shadow'       │
     │  fill = Delta L2 taker       │    │  fill = Delta L2 taker     │
     │  margin: ssb=1000 path       │    │  margin: ssb=443 path      │
     │      ▼                       │    │      ▼                     │
     │  ┌─────────────────┐         │    │  ┌─────────────────┐       │
     │  │ _monitor_trade  │         │    │  │ _monitor_trade  │       │
     │  │  500ms loop     │         │    │  │  500ms loop     │       │
     │  │  reads          │         │    │  │  reads          │       │
     │  │ orchestrator.   │         │    │  │ orchestrator.   │       │
     │  │ _ws_prices      │         │    │  │ _ws_prices      │       │
     │  │  fires SL/TP/   │         │    │  │  fires SL/TP/   │       │
     │  │  trail/max_age  │         │    │  │  trail/max_age  │       │
     │  │  → _close_shadow│         │    │  │  → _close_shadow│       │
     │  └─────────────────┘         │    │  └─────────────────┘       │
     └──────────────────────────────┘    └────────────────────────────┘
                  │                                   │
                  │   delta_shadow OPEN persisted     │
                  ▼                                   ▼
     ┌──────────────────────────────────────────────────────┐
     │  bybit-shadow-daemon polls user_trades every 3s for: │
     │  (a) delta_shadow OPENS not yet mirrored             │
     │       → simulates Bybit fill at Bybit L2 top-of-book │
     │       → INSERT bybit_shadow row with                 │
     │          metadata.mirror_of_delta_trade_id           │
     │  (b) delta_shadow CLOSES with orphan bybit mirror    │
     │       → UPDATE bybit_shadow row with close fill      │
     └──────────────────────────────────────────────────────┘
                  │
                  ▼
     ┌────────── bybit_shadow rows (2: admin + niranjan) ─────────────┐
     │  Two parallel exit guards:                                     │
     │   1. daemon "close cascade"  — closes when delta source closes │
     │   2. bybit-shadow-monitor    — independent SL/TP/trail/decay   │
     │                                using own L2 cache              │
     │     ┌─ TODAY'S DATA: ────────────────────────────────────┐     │
     │     │ daemon cascade (mirrored_from_delta): +$1.57/trade │     │
     │     │ monitor's own time_decay exits:       -$0.76/trade │     │
     │     │ ⇒ daemon cascade is the alpha; monitor             │     │
     │     │   eats some of it on tail trades.                  │     │
     │     └────────────────────────────────────────────────────┘     │
     └────────────────────────────────────────────────────────────────┘
                  │
                  ▼ (Mac side, separate process)
     ┌──────────────────────────────────────────────────────┐
     │  bybit_demo_dispatcher.py (Mac foreground process)   │
     │  Every 10s polls VM for unrecorded delta_shadow      │
     │  opens (exchange='delta_india', trade_type='shadow'),│
     │  places Market IOC on api-demo.bybit.com,            │
     │  records back as exchange='bybit', trade_type='demo' │
     │  Also polls for delta closes → places reduce_only    │
     │  market order to close demo position.                │
     └──────────────────────────────────────────────────────┘
                  │
                  ▼
     ┌──────────── bybit_demo rows (2: admin + niranjan) ─────────────┐
     │  Real Bybit demo orders, real exchange ms-clock.               │
     │  Independent fill prices from bybit_shadow (separate L2 read,  │
     │  separate slippage model).                                     │
     │  Currently 7 clean trades in 24h (rest were force-cleanup):    │
     │     n=7, WR=28.6%, net +$0.89, avg +$0.13/trade after $4 fees  │
     │  Sample too small for verdict; needs 30+ clean trades.         │
     └────────────────────────────────────────────────────────────────┘
```

**Per qualified signal, the row count is:**
```
1 paper
+ 2 delta_shadow (admin, niranjan)
+ 2 bybit_shadow (mirrored from each delta_shadow)
+ 2 bybit_demo   (Mac dispatch from each delta_shadow)
= 7 rows in user_trades per signal
```

---

## 4. Service Inventory

| service | host | role | restart cadence |
|---|---|---|---|
| `cryptobot.service` | VM | scanner + ML + per-user shadow exec + dashboard | manual |
| `bybit-shadow-daemon.service` | VM | mirrors delta_shadow opens AND cascades closes to bybit_shadow rows | manual; 5s sleep loop |
| `bybit-shadow-monitor.service` | VM | Phase 2 independent exit guard for bybit_shadow (own L2 cache) | manual; 2s poll loop |
| `dashboard-whitelist-sync.service` | VM | syncs `dashboard_whitelist` table → iptables INPUT chain | systemd-timer hourly |
| `bybit_demo_dispatcher.py` | Mac | mirrors delta_shadow → real Bybit demo orders | foreground; 10s poll |
| (24 cron jobs) | VM | cohort detection, lever verdicts, eod reconcile, etc. | varies (15min – daily) |

---

## 5. Sizing Flow (Per User Per Signal)

```
qualified signal arrives at user manager
                │
                ▼
   compute_size(signal):
   ┌───────────────────────────────────────────────────────────┐
   │ 1. RESOLVE BALANCE                                        │
   │    if _is_shadow_live AND shadow_simulated_balance set:   │
   │       balance = ssb              ← admin=1000, niranjan=443 │
   │    else:                                                  │
   │       balance = _cached_balance or 100.0  ← fallback       │
   │                                                           │
   │ 2. usable = balance × 0.85       (15% reserve)             │
   │                                                           │
   │ 3. Grade-based base_margin:                               │
   │      A+: min(75, usable × 0.20)                           │
   │      A : min(65, usable × 0.15)                           │
   │      B : min(55, usable × 0.12)                           │
   │      C : min(45, usable × 0.10)  (C blocked at qualify)   │
   │                                                           │
   │ 4. Multipliers (compounding):                             │
   │      conviction = 0.80 + max(0, ml-0.50)×1.25, capped 1.30│
   │      grade_mult = 1.0 (B/A/A+) | 0.70 (C, dead path)      │
   │      regime_mult = 0.50–0.90 per regime tilt              │
   │      ladder_mult = 0.50 if 2nd same-side same-symbol      │
   │                                                           │
   │ 5. margin = base × size_mult × conviction × grade × regime × ladder │
   │                                                           │
   │ 6. Floor + Ceiling:                                       │
   │      ceiling = max(balance × 0.22, 10.0)                  │
   │      ←—— uses SAME balance source as base (not _cached)   │
   │      margin = max(10.0, min(margin, ceiling))             │
   │                                                           │
   │ 7. leverage = min(max_leverage, 50)                       │
   │                                                           │
   │ 8. lots = int(margin × lev / (entry × contract_size))     │
   └───────────────────────────────────────────────────────────┘
                │
                ▼
        (margin, leverage, lots)
                │
                ▼
   record_shadow_trade()  →  asyncio.create_task(_monitor_trade(id))
```

**Pre-fix (Bug 3) sizing inversion:**
- balance source was always `_cached_balance` (real Delta wallet)
- admin had wallet ~$0.68 → size capped at $10
- niranjan had wallet ~$200 → size at $22
- Net result: niranjan sized 2-7× admin per signal despite admin's larger
  intended sim bankroll ($1000 vs $443)

**Post-fix (today):** both balance and ceiling now read from
`shadow_simulated_balance` for shadow_live users → admin sizes larger than
niranjan, proportional to sim bankroll. **Awaiting verification trace from
the latest restart.**

---

## 6. Exit Guard Flow

There are **THREE** independent exit guards per signal:

```
                   ┌──── Delta Shadow (per-user) ─────┐
                   │  Runs in cryptobot.service       │
                   │  _monitor_trade(trade_id):       │
                   │   • polls orchestrator._ws_prices│
                   │     every 500ms                  │
                   │   • fires:                       │
                   │       - SL hit (price ≤/≥ sl)    │
                   │       - TP hit                   │
                   │       - chandelier trail         │
                   │       - smart_max_age (15-30min) │
                   │       - early kill (<1min DOA)   │
                   │   • calls _close_shadow → DB     │
                   │   • NEW: failsafe force-close at │
                   │     2× max_age if no price 60s+  │
                   └──────────────────────────────────┘

                   ┌── Bybit Shadow Daemon Cascade ──┐
                   │  Runs in bybit-shadow-daemon    │
                   │  fetch_orphaned_open_mirrors():  │
                   │   • finds bybit_shadow rows     │
                   │     whose source delta closed   │
                   │   • simulates Bybit close fill  │
                   │     at Bybit L2 top-of-book     │
                   │   • UPDATEs row                 │
                   │  TODAY'S DATA: avg +$1.57/trade │
                   │  (the dominant alpha source)    │
                   └─────────────────────────────────┘

                   ┌── Bybit Shadow Phase 2 Monitor ─┐
                   │  Runs in bybit-shadow-monitor   │
                   │  monitor_task():                │
                   │   • own L2 cache from bybit WS  │
                   │   • independent SL/TP/trail/    │
                   │     time_decay                  │
                   │   • only fires if daemon hasn't │
                   │     closed already              │
                   │  TODAY'S DATA: avg -$0.76/trade │
                   │  (eats some alpha on tail trades│
                   │   that would have closed better │
                   │   via daemon cascade)           │
                   └─────────────────────────────────┘

                   ┌── Bybit Demo (Mac dispatcher) ──┐
                   │  Runs as foreground process     │
                   │  poll_open_demos_to_close():    │
                   │   • finds bybit_demo rows w/    │
                   │     parent delta closed         │
                   │   • places reduce_only market   │
                   │     order on api-demo.bybit.com │
                   │   • UPDATEs row with real fill  │
                   │  Sample too small for verdict   │
                   └─────────────────────────────────┘
```

---

## 7. State Machine (per user_trades row)

```
                       ┌──────────────┐
                       │   created    │
                       │ status=open  │
                       │ closed_at=NULL│
                       └──────┬───────┘
                              │
                ┌─────────────┼──────────────┐
                ▼             ▼              ▼
       ┌──────────────┐  ┌──────────┐  ┌──────────────┐
       │  exit guard  │  │ external │  │ operator     │
       │ trigger      │  │  close   │  │ force_close  │
       │  (SL/TP/trail│  │ (orphan  │  │              │
       │  /max_age)   │  │  cleanup,│  │              │
       │              │  │ kill_swt)│  │              │
       └──────┬───────┘  └────┬─────┘  └──────┬───────┘
              │               │               │
              └───────────────┴───────────────┘
                              │
                              ▼
                       ┌──────────────┐
                       │   closed     │
                       │ status=closed│
                       │ closed_at=now│
                       │ pnl_usd      │
                       │ exit_price   │
                       │ metadata.    │
                       │  exit_reason │
                       └──────────────┘
```

---

## 8. Data Schema (key columns)

`user_trades` (one row per trade/mirror):
| col | type | note |
|---|---|---|
| id | uuid | PK |
| user_id | uuid | FK users |
| exchange | varchar | `delta_india` or `bybit` |
| trade_type | varchar | `paper`, `shadow`, `demo`, `real` |
| symbol | varchar | `BTC/USDT`, etc. |
| side | varchar | `long`/`short` |
| entry_price | float | actual fill price |
| quantity | float | in CONTRACTS (Delta), in BASE qty (Bybit) |
| opened_at, closed_at | timestamptz | tz-aware (`+00:00`) |
| pnl_usd | float | net of fees |
| fees_usd | float | open + close fees combined |
| status | varchar(20) | `open`, `closed`, `force_closed` |
| metadata | jsonb | scanner, regime, grade, ml_prob, sl, tp, lev, contract_size, exit_reason, mirror_of_delta_trade_id, fee_type, etc. |
| signal_data | jsonb | original signal payload |

`users` (key columns for sizing & mode):
| col | type | note |
|---|---|---|
| email | varchar | identity |
| bot_mode | varchar | `paper`, `shadow_live`, `live` |
| bybit_mode | varchar | `paper`, `live` |
| max_leverage | int | hard cap (typically 20) |
| preferred_leverage | int | default leverage if not signal-specified |
| max_position_notional | float | per-signal notional cap |
| max_daily_loss_pct | float | circuit-breaker trigger |
| **shadow_simulated_balance** | numeric | **the bankroll used for shadow sizing** (admin=1000, niranjan=443) |
| maker_patience_mode | varchar | `standard` / `patient` / `aggressive` |
| exit_policy | varchar | `current` (default) — placeholder for Wave 4 A/B |
| cohort_filter_enabled | bool | per-user cohort blacklist toggle |
| mark_alignment_enabled | bool | per-user paper-watermark exit alignment |

---

## 9. Verified Fixes (Today, 2026-04-26)

| # | Bug | File(s) | Fix | Verification |
|---|---|---|---|---|
| 1 | `bybit_shadow_monitor` crash loop on `IndeterminateDatatypeError: $4` | `scripts/bybit_shadow_monitor.py` | added `::text` cast on `jsonb_build_object` value param | service `is-active`; no more error in journald |
| 2 | shadow trades never reconciled on bot restart (orphan monitors) — root cause: `if not rows: return` exited before shadow code | `execution/user_real_manager.py` | removed early-return; shadow reconcile block now always runs | `USER X: shadow reconcile — found N open SHADOW trade(s)` log now appears between `RECON_START` and `RECON_DONE` for both users |
| 3a | `compute_size` used real wallet balance instead of `shadow_simulated_balance` for shadow_live users (lever3 rollback regression) | `execution/user_real_manager.py` | re-added init-time load + `compute_size` balance-resolution + ceiling resolution from same source | first capture: `using=1000.00 (shadow_sim)` for admin; subsequent restart broke it (Bug 3b) |
| 3b | `user_registry.user_config` dict was missing `shadow_simulated_balance` pass-through (also `exit_policy`, `cohort_filter_enabled`, `mark_alignment_enabled`) | `execution/user_registry.py` | added 4 missing keys to dict | **deployed; awaiting next SIZE_BAL trace to confirm sustained `using=shadow_sim`** |
| 4 | BOOK strip showed `+$2301.1k` because cap_deployed = `SUM(entry_price × quantity)` ignored Delta contract_size | `dashboard/server.py` | switched to `SUM(COALESCE(metadata.margin, entry × qty × COALESCE(metadata.contract_size, 1)))` | next dashboard refresh will show realistic margin total |
| 5 | Stuck-open trades never closed because `_monitor_trade` looped silently on `price=0` (REST cache broken, WS only feeds active subset of symbols) | `execution/user_real_manager.py` | added `MONITOR_NO_PRICE` warning at 60s no-price + `MONITOR_FORCE_CLOSE` failsafe at 2× max_age | failsafe wired; not yet triggered (no qualifying conditions in current window) |
| – | UI overlay (5 surfaces A/B/C/D/F + E lazy-loaded) | `dashboard/static/{js,css}/multi_exchange_overlay*.{js,css}` + `index.html` | shipped earlier; CSS=9.7kb JS=22.4kb | architect's earlier hard-refresh confirmed render |

---

## 10. OPEN BUGS / RISKS

### 🔴 CRITICAL

| # | Issue | Symptom | Root cause hypothesis | Action |
|---|---|---|---|---|
| O1 | **Delta India shadow PF=0.09** on identical signals where Bybit gets PF=3.44 | Delta loses $50/24h on the same 79 signals Bybit makes +$77 on | Either (a) Delta L2 fills modeled too generously (we credit fills tighter than reality), or (b) Delta India spreads are genuinely wider than Bybit and the live execution path inherits that | Audit `_close_shadow` fill simulation; compare to actual Delta demo fills from a paired test |
| O2 | **`_cached_balance` for both users is ~$0** (admin $0.68, niranjan $0.00) | Without Bug 3 fix, fallback default of $100 was used → wrong sizing entirely | Demo Delta wallets are essentially empty; the `fetch_balance` returns true balance | Ensure Bug 3b sustains across restarts; document that real-wallet balance is irrelevant for shadow sizing |
| O3 | **Phase 2 monitor's time_decay exits LOSE money** vs daemon cascade (avg -$0.76 vs +$1.57) | 10 trades in 24h closed via `time_decay_30-67m` netting -$7.62 | Monitor max_age (1800s SCALP) fires before daemon cascade has chance; monitor doesn't check "is delta source still open?" before time-deciding | Add a "wait-for-cascade" gate: if delta source still open AND age < 60min, defer monitor's own exit |

### 🟡 MEDIUM

| # | Issue | Symptom | Hypothesis | Action |
|---|---|---|---|---|
| O4 | **XRP/USDT is the only Bybit underperformer** (-$7.33 gap vs Delta) | Bybit -$12 vs Delta -$5 on same 18 XRP trades | XRP Bybit liquidity may be thinner than expected; spread model wrong | Pull paired XRP slippage; audit Bybit XRP L2 depth |
| O5 | **Niranjan's manager creates lazily on first signal** (admin's also; both lazy) | Reconcile only runs at first-signal time, so trades opened post-restart but pre-first-signal are orphaned briefly until reconcile fires | Lazy manager init in `get_or_create_manager` triggered on first `broadcast_signal` per user | At orchestrator startup, eagerly create managers for all `bot_mode!='paper'` users so reconcile fires immediately |
| O6 | **WS `_ws_prices` may be sparse for low-volume symbols** | Logger says "DeltaWebSocket up — symbols=['BTC/USDT','ETH/USDT','SOL/USDT']" but that's a `[:3]` log slice, real subscription is all 20 | Delta India may only push ticks on actively-traded books; remaining 17 fall back to broken REST | Bug 5 failsafe handles this; long-term: also pull bid/ask from `_delta_ws.l2_orderbook` (we have it) |
| O7 | **`_cached_balance` auth-error spam** in journald | `Balance fetch failed: Authentication failed: delta requires "apiKey" credential` on every cycle | Bot init refuses live connect without explicit keys (`owner=unspecified is not 'system'`) — correct behavior, but noisy | Either pass explicit demo keys OR catch+rate-limit the warning |

### 🟢 LOW (cosmetic / known-noisy)

| # | Issue |
|---|---|
| O8 | Multi-user multiplication: 1 signal → 7 rows. Expected behavior but confuses dashboard counts |
| O9 | `force_orphan_*` exit reasons polluting close stats from today's 4 cleanup rounds (42 trades) — will age out of 24h window naturally |
| O10 | Bybit demo dispatcher uses Mac residential IP — single point of failure if Mac sleeps |
| O11 | Cron `code_review_wrapper.sh` exits with `ERROR: usage:` on every invocation — broken job, needs disable |
| O12 | `maker_mode_verdict` cron runs every 6h — too frequent, demote to daily (same data freshness, less noise) |

### ❓ UNVERIFIED / NEEDS NEXT TRACE

| # | Question | Test |
|---|---|---|
| Q1 | Does Bug 3b fix sustain across restarts? | Wakeup capture in 4 min — expect `ssb=1000.0 → using=1000.00 (shadow_sim)` for admin and `ssb=443.0` for niranjan |
| Q2 | Does shadow reconcile actually rebuild monitors that close trades? | Need a signal to fire pre-restart, then restart, then watch reconciled trade close via SL/TP/max_age |
| Q3 | Does `MONITOR_FORCE_CLOSE` failsafe ever fire? | Only fires if a symbol's WS feed dies AND trade exceeds 2×max_age (60min SCALP) — won't see naturally unless feed breaks |

---

## 11. Decisions Needed from Architect

1. **Migrate live trading from Delta → Bybit?**
   Today's 24h data: Bybit shadow PF=3.44, Delta shadow PF=0.09 on identical signals. Bybit shadow advantage = +$127/day. Recommendation: **YES**, but validate with bybit_demo first (need 30+ clean trades).

2. **Phase 2 monitor: keep or kill?**
   Monitor's own exits net -$7.62/24h. Daemon cascade nets +$84.67/24h. Options:
   - (a) keep monitor as fail-safe only (fire only if daemon dead >5min)
   - (b) widen monitor max_age so daemon cascade always wins the race
   - (c) kill monitor entirely; rely on daemon + force-close failsafe (Bug 5)

3. **XRP cohort: continue or skip?**
   XRP is dragging Bybit (-$12 vs Delta -$5 on 18 trades). Either fix the model or block XRP from Bybit signals.

4. **Niranjan's `shadow_simulated_balance=$443`**
   Half of admin's $1000. Was that intentional (to simulate a smaller account) or stale config? If intentional, fine. If accidental, set both to same value so A/B compare is clean.

5. **Bot restart cadence + monitor durability**
   Today saw ~6 restarts; each pre-Bug 2 restart orphaned every shadow monitor. With Bug 2 fixed, this should be fine — but the eager-init recommendation (O5) closes the residual lazy-init window.

6. **Dashboard public URL strategy**
   Whitelist + iptables works for static IPs. Architect mentioned wanting public showcase via Cloudflare Tunnel for mobile-IP independence. Deferred — flag for next session.

---

## 12. Quick Reference: Key Logs to Watch

```bash
# Sizing trace (per signal)
grep "SIZE_BAL" /journalctl…
# Expected: user=admin ssb=1000.0 cached=$0.68 → using=1000.00 (shadow_sim)
#           user=niranjan ssb=443.0 cached=$0.00 → using=443.00 (shadow_sim)

# Reconcile trace (per restart)
grep "RECON_START\|shadow reconcile\|RECON_DONE" /journalctl…

# Exit-guard health
grep "_close_shadow\|MONITOR_NO_PRICE\|MONITOR_FORCE_CLOSE\|trail_stop\|sl_hit\|max_age" /journalctl…

# Failsafe activation (should be RARE)
grep "MONITOR_FORCE_CLOSE\|no_price_orphan_kill" /journalctl…
```

---

## 13. File-by-File Diff Index (today's session)

| file | lines touched | purpose |
|---|---|---|
| `scripts/bybit_shadow_monitor.py` | +1 | `$4::text` cast |
| `execution/user_real_manager.py` | ~+150 | shadow reconcile block, ssb init+ceiling, SIZE_BAL diag, MONITOR_NO_PRICE/FORCE_CLOSE failsafe |
| `execution/user_registry.py` | +12 | added 4 missing user_config keys; warning-level RECON_START/DONE |
| `dashboard/server.py` | ~+90 | `/api/multi-exchange/closed`, contract-aware BOOK cap, SL/TP/leverage/last_price in active |
| `dashboard/static/js/multi_exchange_overlay.js` | ~+200 | Surface E (Analytics 4-tab); SL/TP/PnL/lev in active rows; 4-cell hero; dropdown placement |
| `dashboard/static/css/multi_exchange_overlay.css` | ~+170 | Surface E styles; 4-cell hero; SL/TP coloring; dropdown |

---

**Spec status: living document. Will be updated as Bug 3b verifies and as Phase 2 monitor question is decided.**
