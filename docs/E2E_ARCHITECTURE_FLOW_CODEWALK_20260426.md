# VN Edge — End-to-End Architecture, Flow, and Code Walk
**Audience:** Architect, future maintainers, on-call engineers
**Status:** Authoritative reference · 2026-04-26 (post bug-bash session)
**Companion doc:** `docs/FULL_FLOW_SPEC_20260426.md` (executive summary version)

---

## Document Map

| § | Section |
|---|---|
| 0 | Glossary |
| 1 | What VN Edge is and why it exists |
| 2 | Physical architecture (hosts, exchanges, network) |
| 3 | Logical architecture (subsystems & responsibilities) |
| 4 | Service inventory (every running process) |
| 5 | End-to-end signal lifecycle |
| 6 | Code walk per lifecycle phase (file:line) |
| 7 | Data schema (`user_trades`, `users`, indexes) |
| 8 | Configuration reference (env vars, user_config, constants) |
| 9 | Dashboard API surface |
| 10 | State machines (trade, signal, manager) |
| 11 | Failure modes & recovery patterns |
| 12 | Bugs verified-fixed today (with file:line) |
| 13 | Open bugs / risks (prioritized) |
| 14 | Operational runbook |
| 15 | Decision log (architect calls required) |

---

## §0. Glossary

| Term | Meaning |
|---|---|
| **paper** | Oracle execution path; no fees, no slippage; baseline for measuring edge |
| **shadow** | Simulated execution at a real exchange's L2 (Delta India OR Bybit), using realistic fees but no actual orders. Measures "edge after this venue's friction." |
| **demo** | Real orders on the exchange's sandbox endpoint (`api-demo.bybit.com`) with sandbox $1M USDT. Real exchange clock + fills. |
| **real** | Live orders with real money. Currently NO real trading is active (`bot_mode='shadow_live'`). |
| **manager** | A `UserRealManager` instance — one per active user, encapsulates that user's execution state |
| **broadcast** | Fan-out of one paper signal to all active users' managers via `UserRealRegistry.broadcast_signal()` |
| **mirror** | A `bybit_shadow` or `bybit_demo` row whose `metadata.mirror_of_delta_trade_id` points at a `delta_shadow` parent |
| **cascade** | When a Delta close triggers Bybit shadow/demo closes via daemon/dispatcher polling |
| **monitor** | Per-trade async task spawned at trade-open that polls price every 500ms and fires SL/TP/trail/max_age |
| **reconcile** | At restart/manager-init, scan DB for open trades that lost their monitor task; rebuild record + respawn monitor |
| **cohort** | A `(symbol, side)` tuple. Used for blacklist/pause logic when a cohort produces consecutive losses |
| **smart_max_age** | SCALP=1800s (30min), INTRADAY/RUNNER=3600s (60min) — hard exit if open longer |
| **PPP** | Proof-of-Profitability — a regressor that predicts whether a signal can clear round-trip fees |

---

## §1. What VN Edge Is and Why It Exists

VN Edge is a **multi-user, multi-venue crypto perpetuals trading bot** with three core principles:

1. **Edge is measured in PAPER, not in execution.** The paper engine is the ground-truth oracle. Every venue is judged by how much paper alpha survives its friction.
2. **Multiple venues run in parallel** for the same signal so we can A/B execution quality without re-running history.
3. **Per-user isolation.** Each active user gets their own `UserRealManager` with own circuit breaker, sizing, cohort blacklist, and trade history.

A single qualified paper signal produces **7 rows in `user_trades`** per signal:
- `1 paper` (oracle)
- `2 delta_shadow` (admin + niranjan)
- `2 bybit_shadow` (mirrored from each delta_shadow by daemon)
- `2 bybit_demo` (Mac dispatch from each delta_shadow)

Today's data after fixes: **Bybit shadow PF=3.44, Delta shadow PF=0.09 on identical 79 signals over 24h.** Bybit captures +$77 on signals where Delta loses -$50.

---

## §2. Physical Architecture

```
                            ┌──── EXCHANGES ────┐
┌─────────────────┐         │                   │
│ ARCHITECT'S MAC │ HTTPS   │ Delta India       │
│ (residential IP)│ ──────► │   api.india.delta │
│                 │         │   socket.india.   │
│ bybit_demo_     │         │     delta         │
│  dispatcher.py  │         │                   │
│  (foreground)   │ HTTPS   │ Bybit Demo        │
│  - polls VM via │ ──────► │   api-demo.bybit  │
│    SSH+psql     │         │                   │
│  - places real  │ WSS     │ Bybit Public WS   │
│    Bybit demo   │ ──────► │   stream.bybit    │
│    orders       │         │                   │
└────────┬────────┘         │ Bybit Live        │
         │ ssh              │   api.bybit       │
         │ ssh+psql         │   (geo-blocked    │
         ▼                  │    from VM REST,  │
┌────────────────────────┐  │    Trade-WS workaround
│  ORACLE CLOUD VM       │  │    available)     │
│  150.230.171.48        │  └───────────────────┘
│  (Oracle India region) │            ▲
│                        │            │
│  ┌──────────────────┐  │  HTTPS/WSS │
│  │ cryptobot.       │  │ ───────────┘
│  │  service         │  │
│  │  - main loop     │  │
│  │  - per-user      │  │
│  │    shadow exec   │  │
│  │  - dashboard     │  │
│  │    :8080         │  │
│  └──────────────────┘  │
│                        │
│  ┌──────────────────┐  │
│  │ bybit-shadow-    │  │
│  │  daemon.service  │  │  WSS  ┌──────────┐
│  │  - mirrors delta │──┼──────►│ Bybit WS │
│  │    opens         │  │       └──────────┘
│  │  - cascades      │  │
│  │    closes        │  │
│  └──────────────────┘  │
│                        │
│  ┌──────────────────┐  │
│  │ bybit-shadow-    │  │
│  │  monitor.service │  │  WSS  ┌──────────┐
│  │  (Phase 2)       │──┼──────►│ Bybit WS │
│  │  - own L2 cache  │  │       └──────────┘
│  │  - own SL/TP/    │  │
│  │    trail         │  │
│  └──────────────────┘  │
│                        │
│  ┌──────────────────┐  │
│  │ PostgreSQL 14    │  │
│  │  (vnedge db)     │  │
│  │  - user_trades   │  │
│  │  - users         │  │
│  │  - user_api_keys │  │
│  │  - signal_data   │  │
│  │  - audit_logs    │  │
│  └──────────────────┘  │
│                        │
│  iptables INPUT:       │
│   - whitelist          │
│   - dashpf 60conn/60s  │
└────────────────────────┘
```

**Why Mac for Bybit demo:** Oracle VM resolves to a CloudFront-fronted IP that Bybit India geo-blocks at the REST layer. Mac residential IP isn't blocked. Public WS works from VM (used by daemon + monitor for L2 + price feed).

---

## §3. Logical Architecture

```
┌────────────────────── cryptobot.service (main process) ──────────────────────┐
│                                                                              │
│   ┌─────────┐   ┌──────────┐   ┌──────────────┐   ┌──────────────────┐       │
│   │ data/   │   │ strategy │   │ decision     │   │ signal_tracker   │       │
│   │ feed    │──►│ multi_   │──►│ engine.py    │──►│ .py              │       │
│   │ (WS+REST│   │ strategy │   │ (LONG/SHORT/ │   │ (track_signal,   │       │
│   │  hybrid)│   │ + ml/ppp │   │  WAIT)       │   │  grade/conf gate)│       │
│   └─────────┘   └──────────┘   └──────────────┘   └────────┬─────────┘       │
│                                                            │                 │
│                                                            ▼                 │
│   ┌────────────────────────────────────────────────────────────────────┐     │
│   │  bot/orchestrator.py:_process_signal()                             │     │
│   │  Branches:                                                         │     │
│   │    A. paper → execution/paper_engine.py                            │     │
│   │    B. real_manager (legacy global, mostly disabled)                │     │
│   │    C. user_registry.broadcast_signal()  ← MULTI-USER FAN-OUT       │     │
│   │    D. journal record                                               │     │
│   │    E. websocket push to dashboard                                  │     │
│   └────────────────────────────────────────────────────────────────────┘     │
│                                                            │                 │
│                                                            ▼                 │
│   ┌────────────────────────────────────────────────────────────────────┐     │
│   │  execution/user_registry.py UserRealRegistry                       │     │
│   │   - _refresh_active_users() every 60s                              │     │
│   │   - get_or_create_manager(user_info) (LAZY on first signal)        │     │
│   │   - broadcast_signal(): asyncio.gather of mgr.execute_signal()     │     │
│   └────────────────────────────────────────────────────────────────────┘     │
│                                                            │                 │
│                              ┌─────────────────────────────┤                 │
│                              ▼                             ▼                 │
│   ┌─────────────────────────────────┐   ┌─────────────────────────────────┐  │
│   │ UserRealManager (admin)         │   │ UserRealManager (niranjan)      │  │
│   │  - qualify_signal(): 22 gates   │   │  - qualify_signal(): 22 gates   │  │
│   │  - compute_size():              │   │  - compute_size():              │  │
│   │    margin/leverage/lots         │   │    margin/leverage/lots         │  │
│   │  - record_shadow_trade()        │   │  - record_shadow_trade()        │  │
│   │  - _monitor_trade(): 500ms loop │   │  - _monitor_trade(): 500ms loop │  │
│   │    SL/TP/trail/max_age          │   │    SL/TP/trail/max_age          │  │
│   │  - _close_shadow(): sim L2 fill │   │  - _close_shadow(): sim L2 fill │  │
│   └─────────────────────────────────┘   └─────────────────────────────────┘  │
│                                                                              │
│   ┌────────────────────────────────────────────────────────────────────┐     │
│   │  dashboard/server.py — aiohttp on :8080                            │     │
│   │   serves SPA + ~80 REST endpoints + WebSocket /ws                  │     │
│   └────────────────────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────────────┘

┌─── bybit-shadow-daemon (separate systemd service) ───┐
│  scripts/bybit_shadow_simulator.py:main()            │
│  Loop every 5s:                                      │
│   1. fetch new delta_shadow opens not yet mirrored   │
│      → simulate Bybit fill at WS L2 top-of-book      │
│      → INSERT bybit_shadow row w/ mirror_of_delta…   │
│   2. fetch_orphaned_open_mirrors():                  │
│      bybit_shadow rows whose source delta closed     │
│      → simulate Bybit close at L2                    │
│      → UPDATE bybit_shadow row                       │
│  TODAY'S DATA: cascade exits = +$1.57/trade ✅       │
└──────────────────────────────────────────────────────┘

┌─── bybit-shadow-monitor (Phase 2 independent guard) ─┐
│  scripts/bybit_shadow_monitor.py:main()              │
│   - WS subscribe wss://stream.bybit.com/v5/public/   │
│     linear orderbook.1                               │
│   - Maintain BybitL2Cache: {sym: (bid, ask)}         │
│   - Loop every 2s over open bybit_shadow trades      │
│     → evaluate_exit(): SL/TP/trail/time_decay        │
│     → close_bybit_shadow() if hit                    │
│  TODAY'S DATA: monitor's own exits = -$0.76/trade ⚠️ │
└──────────────────────────────────────────────────────┘

┌─── Mac dispatcher (foreground process on architect's Mac) ───┐
│  mac_dispatcher/bybit_demo_dispatcher.py:main_loop()         │
│   Every 10s (or POLL_SEC env):                               │
│    1. _ssh_psql_via_stdin(): poll VM for unmirrored          │
│       delta_shadow opens                                     │
│    2. place_demo_open(): POST /v5/order/create on            │
│       api-demo.bybit.com (Market IOC)                        │
│    3. SSH back: INSERT bybit_demo row                        │
│    4. fetch_open_demos_to_close(): demo rows whose parent    │
│       delta closed                                           │
│    5. place_demo_close(): reduce_only Market IOC             │
│   STATUS: not currently running (architect to verify)        │
└──────────────────────────────────────────────────────────────┘
```

---

## §4. Service Inventory

| Service | Host | Restart | Port | Role |
|---|---|---|---|---|
| `cryptobot.service` | VM | systemd `Restart=always` `RestartSec=30` | 8080 (dashboard) | main trading engine + per-user shadow exec + dashboard |
| `bybit-shadow-daemon.service` | VM | systemd | — | mirrors delta_shadow opens AND cascades closes |
| `bybit-shadow-monitor.service` | VM | systemd | — | Phase 2 independent exit guard for bybit_shadow |
| `dashboard-whitelist-sync.service` | VM | systemd-timer hourly | — | syncs `dashboard_whitelist` table → iptables |
| `crypto-learning.service` | VM | systemd | — | continuous ML retrainer (background) |
| `bybit_demo_dispatcher.py` | Mac | foreground (manual) | — | mirrors delta_shadow → real Bybit demo orders |

**Cron jobs (24 active):** auto_revert_detector, cohort_drift, lever_verdict, eod_reconcile, maker_mode_verdict, paper_vs_shadow_gap, strategy_decay_monitor, etc.

---

## §5. End-to-End Signal Lifecycle

A single qualified signal traverses **12 logical phases** in 4-12 seconds end-to-end:

```
Phase 1.   ◯ Candle close (5m bar finalized on Delta India WS)
              │
Phase 2.   ◯ Strategy scan (each scanner inspects new candle)
              │
Phase 3.   ◯ ML scoring (xgboost prob attached to candidates)
              │
Phase 4.   ◯ Decision engine (action LONG/SHORT/WAIT chosen)
              │
Phase 5.   ◯ Signal tracker (paper "open" recorded; grade/conf gate)
              │
Phase 6.   ◯ Paper execution (paper engine virtual fill at L2 mid)
              │
Phase 7.   ◯ Multi-user broadcast (gather across active users)
              │
Phase 8.   ◯ Per-user qualify (22 gates: balance/grade/conf/cohort/...)
              │
Phase 9.   ◯ Per-user sizing (compute_size: balance→margin→lots)
              │
Phase 10.  ◯ Per-user record (delta_shadow row INSERT in user_trades)
              │
Phase 11.  ◯ _monitor_trade() task spawned (500ms SL/TP/trail loop)
              │
              ├── Phase 11a. bybit-shadow-daemon polls (5s) → INSERT bybit_shadow row
              │
              ├── Phase 11b. Mac dispatcher polls (10s) → POST /v5/order/create →
              │              INSERT bybit_demo row
              │
              ├── Phase 11c. bybit-shadow-monitor task picks up new bybit_shadow
              │              into its in-memory states dict
              │
Phase 12.  ◯ Exit (SL/TP/trail/max_age fires in monitor) → _close_shadow → DB UPDATE
              │
              ├── Phase 12a. Daemon detects parent delta closed → close cascade for
              │              bybit_shadow (UPDATE row at Bybit L2)
              │
              ├── Phase 12b. Mac dispatcher detects parent closed → place_demo_close
              │              (reduce_only Market IOC) → UPDATE bybit_demo row
              │
              └── Phase 12c. Phase 2 monitor's own time_decay fires if daemon
                             cascade slow (TODAY: this is net-negative — see Bug O3)
```

---

## §6. Code Walk Per Phase

All paths relative to `/Users/scorpion/Desktop/Claude AI Crypto Bot/crypto-trading-bot/`.

### §6.0 Boot

**Entry point:** `main.py`

Boot sequence (paraphrased):
```
main.py
  ↓ argparse: --mode paper|signal_only|live|backtest
  ↓ config.loader.load_config()
  ↓ logger.setup_logging() (LOG_LEVEL env, default WARNING)
  ↓ exchange.factory.create_exchange()
  ↓ BotOrchestrator(config, exchange) [bot/orchestrator.py:__init__]
  ↓ orchestrator.start()
  ↓ orchestrator.run() — main async loop
```

### §6.1 — Phase 1: Candle close

**File:** `bot/orchestrator.py:1485`
```python
async def _on_candle_close(self, *, symbol: str, timeframe: str, candle: dict) -> None:
    # called by data feed when a new bar finalizes
    # dispatches to scanners
```

### §6.2 — Phase 2-4: Scan → ML → Decision

**File:** `strategies/multi_strategy.py` (composite scanner runner)
**File:** `bot/decision_engine.py` (LONG/SHORT/WAIT decision)
**File:** `bot/ml_scorer.py` (calls VM2 ML service for prob)

Output: `signal_dict` with keys:
```python
{
  "symbol": "BTC/USDT",
  "side": "long" | "short",
  "entry_price": float,
  "stop_loss": float,
  "take_profit": float,
  "confidence": float (0-100),
  "metadata": {
    "scanner": str, "regime": str, "grade": "A+|A|B|C",
    "ml_probability": float, "trade_type": "SCALP|INTRADAY|RUNNER",
    "initial_risk": float (in USD)
  }
}
```

### §6.3 — Phase 5: Signal tracker (paper open + gate)

**File:** `bot/signal_tracker.py:813`
```python
def track_signal(self, signal_dict: Dict[str, Any]) -> None:
    # Min gates: grade != REJECT, confidence >= 45
    # Fee viability gate (execution/exit_guards.py:fee_floor_r)
    # Duplicate prevention: max 1 active per (symbol, side)
    # Inserts TrackedSignal into self._active
```

### §6.4 — Phase 6: Paper execution

**File:** `execution/paper_engine.py`
- Simulates fill at L2 mid (no fees in paper — pure oracle)
- Inserts `user_trades` row with `trade_type='paper'`

### §6.5 — Phase 7: Multi-user broadcast

**File:** `bot/orchestrator.py:2034`
```python
asyncio.create_task(self._user_registry.broadcast_signal(sig_dict))
```

**File:** `execution/user_registry.py:67`
```python
async def broadcast_signal(self, signal):
    # Refresh active users every 60s
    await self._refresh_active_users()
    # Fan out — gather all user manager executions
    for user_info in self._active_users_cache:
        mgr = await self.get_or_create_manager(user_info)
        await mgr.execute_signal(signal)
```

`get_or_create_manager` (line 110+):
- Returns existing manager if cached
- Else builds a new `UserRealManager(user_id, user_email, user_config, delta_client)`
- Spawns `_recon_both()` task — calls `mgr.reconcile_open_trades()` then `_reconcile_from_exchange()`
- **NEW today (warning-level logs):** `RECON_START`, `RECON_DONE`, `Registry: created manager`

### §6.6 — Phase 8: Per-user qualify

**File:** `execution/user_real_manager.py:728`
```python
async def qualify_signal(self, signal: dict) -> Tuple[bool, str]:
    # 22 sequential gates, return False at first failure:
    # 1. user disabled? → 'user_disabled'
    # 2. global kill switch? → 'kill_switch_engaged'
    # 3. live balance floor? → 'user_live_balance_floor:$X<$Y'
    # 4. circuit breaker? → 'user_cb_tripped:Nlosses/$Mdaily'
    # 5. daily trade cap? → 'user_daily_limit:N/M'
    # 6. preferred symbols filter? → 'user_symbol_filter:SYM'
    # 7. venue supports symbol in mode?
    # 8. confidence floor (45)? → 'user_conf_floor:X<45'
    # 9. grade in (A+,A,B)? → 'user_grade_filter:X'
    # 10. ml prob >= ml_threshold? → 'user_ml_floor:X<Y'
    # 11. fee_wall reject (A+ only at low ML)?
    # 12. SL distance > 4%? → 'user_margin_guard:sl=X%>4%'
    # 13. live emergency halt?
    # 14. multi_scanner_dedup (60s bucket)?
    # 15. max open positions (3)?
    # 16. duplicate hedge (opposite side same symbol)?
    # 17. duplicate same-side (>=3)?
    # 18. mean_reversion regime hard-reject?
    # 19. cohort blacklist last-5-all-lost?
    # ALL PASS → return True, "qualified"
```

### §6.7 — Phase 9: Per-user sizing

**File:** `execution/user_real_manager.py:952` (`compute_size`)
```python
def compute_size(self, signal: dict) -> Tuple[float, int, int]:
    # === BALANCE RESOLUTION (today's Bug 3 fix) ===
    if _is_shadow_live AND shadow_simulated_balance is not None:
        balance = float(shadow_simulated_balance)  # admin=$1000, niranjan=$443
    else:
        balance = self._cached_balance or 100.0    # fallback default $100

    usable = balance * 0.85                        # 15% reserve

    # === GRADE-BASED BASE MARGIN ===
    if grade == "A+": base = min(75, usable * 0.20)
    elif grade == "A": base = min(65, usable * 0.15)
    elif grade == "B": base = min(55, usable * 0.12)
    else:              base = min(45, usable * 0.10)

    # === MULTIPLIERS (compounding) ===
    conviction = clamp(0.80, 0.80 + (ml-0.50)*1.25, 1.30)
    grade_mult = 1.0 if grade != 'C' else 0.70
    regime_mult = {trending:0.90, breakout:0.70, high_vol:0.70, sideways:0.50, ...}
    ladder_mult = 0.50 if signal['_is_ladder_entry'] else 1.0

    margin = base * size_mult * conviction * grade_mult * regime_mult * ladder_mult

    # === FLOOR + CEILING (today's Bug 3 fix #2) ===
    ceiling = max(balance * 0.22, 10.0)            # uses SAME balance source as base
    margin  = max(10.0, min(margin, ceiling))

    # === LEVERAGE + LOTS ===
    leverage = min(self.max_leverage, 50)          # typically 20x
    contract_size = PRODUCT_MAP[symbol]['contract_size_demo' or 'contract_size']
    notional = margin * leverage
    lots = max(1, int(notional / (entry_price * contract_size)))

    return margin, leverage, lots
```

**Diagnostic log (warning level since today):**
```python
logger.warning("SIZE_BAL: user=%s bot_mode=%s _is_sl=%s ssb=%s cached=%s → using=%.2f (%s)",
               user_email, _bot_mode, _is_sl, _ssb, _cached_balance, balance, _bal_src)
```

### §6.8 — Phase 10: Record delta_shadow

**File:** `execution/user_real_manager.py:1101`
```python
async def execute_signal(self, signal: dict) -> Optional[Dict]:
    # 1. qualify
    qualified, reason = await self.qualify_signal(signal)
    if not qualified: return None

    # 2. compute size
    margin, leverage, lots = self.compute_size(signal)

    # 3. fetch L2 from orchestrator's _ws_prices / _delta_ws.l2_orderbook
    # 4. simulate taker fill at top-of-book worst-case
    # 5. _record_shadow_trade(symbol, side, fill, sl, tp, margin, lots, leverage, ...)
```

### §6.9 — Phase 11: Spawn monitor

**File:** `execution/user_real_manager.py:1746` (inside record_shadow_trade)
```python
trade._is_shadow = True   # critical flag → routes _monitor_trade → _close_shadow
self.open_trades[trade_id] = trade
asyncio.create_task(self._monitor_trade(trade_id))
```

**File:** `execution/user_real_manager.py:1885` (`_monitor_trade`)
```python
async def _monitor_trade(self, trade_id: str):
    # Bug 5 failsafe state
    _no_price_streak = 0
    _no_price_warned = False

    while True:
        trade = self.open_trades.get(trade_id)
        if not trade: break

        # Read price from orchestrator._ws_prices
        prices = getattr(self._price_feed, '_ws_prices', {}) or {}
        price = prices.get(trade.symbol, 0)

        if price <= 0:
            _no_price_streak += 1
            # Bug 5 (today): 60s no-price warning + force-close at 2× max_age
            if _no_price_streak == 60: logger.warning("MONITOR_NO_PRICE: ...")
            if (_no_price_streak > 60 and age > _max_age_for_type * 2):
                logger.warning("MONITOR_FORCE_CLOSE: ...")
                await self._close_shadow(trade, trade.entry_price, "no_price_orphan_kill")
                break
            await asyncio.sleep(1); continue

        _no_price_streak = 0
        # Update MFE from price tick (also from candle high if available)
        # ... [chandelier trail + breakeven + lock tiers] ...
        # Check exits in priority order:
        #   1. SL hit
        #   2. TP hit
        #   3. trail_stop
        #   4. exhaustion_shrink
        #   5. early_kill (DOA)
        #   6. time_decay (max_age 1800s SCALP / 3600s INTRADAY)  [line 2222]
        #   → await self._close_trade(trade, price, reason)
        await asyncio.sleep(0.5)
```

**File:** `execution/user_real_manager.py:2818` (`_close_trade`)
```python
async def _close_trade(self, trade, exit_price, reason):
    # SHADOW BRANCH (line 2825)
    if getattr(trade, "_is_shadow", False):
        return await self._close_shadow(trade, exit_price, reason)
    # else: real-order close path (cancel server stop, market close, settle)
```

**File:** `execution/user_real_manager.py:2467` (`_close_shadow`)
```python
async def _close_shadow(self, trade, exit_price, reason):
    # Derive shadow exit from L2 top-of-book (taker worst-case)
    if book and book['bids'] and book['asks']:
        actual_exit = book['bids'][0][0] if trade.side == 'long' else book['asks'][0][0]
    else:
        actual_exit = float(exit_price)
    # Compute gross/fees/funding/NET
    # UPDATE user_trades SET status='closed', closed_at=NOW(), exit_price, pnl_usd, ...
```

### §6.10 — Phase 11a + 12a: Bybit shadow daemon (open mirror + close cascade)

**File:** `scripts/bybit_shadow_simulator.py:398` (`main()`)

Loop body (paraphrased):
```
while True:
    # ── (1) Mirror new delta_shadow opens ──
    new_opens = fetch new delta_shadow rows since last poll
    for d in new_opens:
        if no bybit_shadow with mirror_of_delta_trade_id == d.id:
            sim_fill = bybit_l2_top_of_book(d.symbol, d.side)  # taker side
            INSERT user_trades(
                exchange='bybit', trade_type='shadow',
                entry_price=sim_fill, fees_usd=sim_fill*qty*0.0708%,
                metadata={..., 'mirror_of_delta_trade_id': str(d.id)}
            )

    # ── (2) Cascade closes from delta → bybit ── [TODAY: avg +$1.57/trade]
    orphans = fetch_orphaned_open_mirrors(con, lookback_sec=120)  [line 182]
    for o in orphans:
        if update_mirror_with_close(con, o, snapshots):  [line 217]
            # UPDATE bybit_shadow row with simulated close fill
            pass

    sleep(WS_SAMPLE_SEC = 3)
```

### §6.11 — Phase 12c: Bybit Phase 2 monitor (independent exit guard)

**File:** `scripts/bybit_shadow_monitor.py:316` (`main()`)

```python
async def main():
    cache = BybitL2Cache()           # {sym: (bid, ask, ts)}
    pool = await asyncpg.create_pool(DSN)
    stop = asyncio.Event()

    # Spawn WS task — subscribes wss://stream.bybit.com/v5/public/linear
    # orderbook.1.{SYMBOL} for all tracked symbols
    asyncio.create_task(ws_task(cache, stop))

    # Spawn monitor task — line 266
    await monitor_task(cache, pool, stop)
```

**File:** `scripts/bybit_shadow_monitor.py:266` (`monitor_task`)
```python
async def monitor_task(cache, pool, stop):
    states = {}    # tid -> TradeState (peak_mfe_r etc.)
    while not stop.is_set():
        opens = await fetch_open_bybit_shadows(pool)  # JOIN delta source for SL/TP
        for r in opens:
            tid = r['bid']
            if tid not in states: states[tid] = TradeState(...)

            bid, ask = cache.get(r['symbol'])
            decision = evaluate_exit(states[tid], bid, ask, now)  [line 154]
            if decision.should_exit:
                await close_bybit_shadow(pool, tid, decision.exit_price,
                                         decision.reason, ...)
                states.pop(tid, None)

        await asyncio.sleep(2)
```

**File:** `scripts/bybit_shadow_monitor.py:154` (`evaluate_exit`)
```python
def evaluate_exit(s, bid, ask, now_epoch):
    # Compute current_r based on SL distance
    # Update peak_mfe_r
    # 1. SL hit → ExitDecision(True, "sl_hit")
    # 2. TP hit → ExitDecision(True, "tp_hit")
    # 3. trail: if peak_mfe > 0.4R, ratchet trail to peak - 0.2R; if hit, exit
    # 4. time decay: if age > MAX_HOLD_SEC (1800s), exit
    # else: hold
```

**File:** `scripts/bybit_shadow_monitor.py:236` (`close_bybit_shadow`) — TODAY: `$4::text` cast (Bug 1 fix)
```python
async def close_bybit_shadow(pool, trade_id, exit_price, reason, ...):
    # compute close fee at Bybit India taker = 0.0708%
    # UPDATE user_trades SET status='closed', closed_at=NOW(),
    #   exit_price=$1, pnl_usd=$2, fees_usd=$3,
    #   metadata = metadata || jsonb_build_object(
    #       'close_via', 'bybit_shadow_monitor_v2',
    #       'close_exit_reason', $4::text)   ← FIX HERE
    #   WHERE id = $5::uuid
```

### §6.12 — Phase 11b + 12b: Mac demo dispatcher

**File:** `mac_dispatcher/bybit_demo_dispatcher.py:378` (`main_loop`)
```python
def main_loop():
    while True:
        try:
            # Fetch unmirrored delta_shadow opens
            new = fetch_open_deltas_to_mirror(lookback_sec=300)
            for d in new:
                fill = place_demo_open(d)  # POST /v5/order/create Market IOC
                if fill: insert_demo_row(d, fill)

            # Cascade closes
            to_close = fetch_open_demos_to_close(lookback_sec=120)  [line 168]
            for trade in to_close:
                close_fill = place_demo_close(trade)  [line 296]
                if close_fill: update_demo_close(trade, close_fill)
        except Exception as e:
            log.warning(...)
        time.sleep(POLL_SEC)
```

**SSH+psql helper at line 134** (`_ssh_psql_via_stdin`):
```python
def _ssh_psql_via_stdin(sql, extra_args):
    # Avoids shell-quoting hell by feeding SQL via stdin
    cmd = ['ssh', '-i', SSH_KEY, SSH_TARGET, 'PGPASSWORD=... psql ...'] + extra_args
    return subprocess.run(cmd, input=sql, capture_output=True, text=True)
```

---

## §7. Data Schema

### `user_trades`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid FK | |
| `exchange` | varchar | `delta_india` \| `bybit` \| NULL (paper) |
| `trade_type` | varchar | `paper` \| `shadow` \| `demo` \| `real` |
| `symbol` | varchar | `BTC/USDT` etc. |
| `side` | varchar | `long` \| `short` |
| `entry_price` | float | actual fill price |
| `exit_price` | float | NULL if open |
| `quantity` | float | **CONTRACTS** for Delta (e.g. BTC contract = 0.001 BTC), **BASE QTY** for Bybit |
| `opened_at`, `closed_at` | timestamptz | tz-aware (`+00:00`) |
| `pnl_usd` | float | net of fees (gross − fees) |
| `fees_usd` | float | open + close fees combined |
| `status` | varchar(20) | `open` \| `closed` \| `force_closed` |
| `metadata` | jsonb | scanner, regime, grade, ml_prob, sl, tp, lev, contract_size, fee_type, exit_reason, mirror_of_delta_trade_id, close_via |
| `signal_data` | jsonb | original signal payload (immutable) |

**Indexes (recommended; verify):**
- `(closed_at DESC) WHERE closed_at IS NOT NULL`
- `(opened_at DESC) WHERE closed_at IS NULL`
- `(exchange, trade_type, closed_at DESC)`
- `((metadata->>'mirror_of_delta_trade_id'))`

### `users` (sizing & mode-relevant columns)

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `email` | varchar UNIQUE | |
| `bot_mode` | varchar | `paper` \| `shadow_live` \| `live` |
| `bybit_mode` | varchar | `paper` \| `live` |
| `max_leverage` | int | hard cap (typically 20) |
| `preferred_leverage` | int | default leverage if not signal-specified |
| `max_position_notional` | float | per-signal notional cap |
| `max_daily_loss_pct` | float | circuit-breaker trigger |
| **`shadow_simulated_balance`** | numeric | **the bankroll for shadow sizing**. admin=1000, niranjan=443 |
| `maker_patience_mode` | varchar | `standard` \| `patient` \| `aggressive` |
| `exit_policy` | varchar | `current` (placeholder) |
| `cohort_filter_enabled` | bool | per-user cohort blacklist toggle |
| `cohort_blacklist_paused_until` | timestamptz | if set in future, qualify_signal blocks |
| `mark_alignment_enabled` | bool | per-user paper-watermark exit alignment |
| `is_active` | bool | manager creation depends on this AND `bot_mode!='paper'` |

### `user_api_keys`

Per-user per-exchange keys. Encrypted with Fernet. Schema: `id, user_id, exchange, label, api_key_enc, api_secret_enc, created_at`.

### Other tables (briefly)

- `signal_data` — append-only signal journal
- `audit_logs` — admin actions, key rotations, kill-switch events
- `dashboard_whitelist` — IP allowlist synced to iptables hourly
- `auto_revert_events` — code-deploy rollback record

---

## §8. Configuration Reference

### Environment variables (`.env` on VM)

```bash
DATABASE_URL=postgresql://vnedge:VnEdge2026db@localhost:5432/vnedge
DELTA_API_KEY=...        # admin's Delta India key (rarely used - shadow mode)
DELTA_API_SECRET=...
DELTA_DEMO_API_KEY=...   # Delta India DEMO key (used by orchestrator)
DELTA_DEMO_API_SECRET=...
BYBIT_API_KEY=...        # admin's Bybit live key
BYBIT_API_SECRET=...
BYBIT_DEMO_API_KEY=...   # Bybit demo key (used by Mac dispatcher)
BYBIT_DEMO_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
LOG_LEVEL=WARNING        # default; INFO/DEBUG suppressed
PYTHONUNBUFFERED=1       # required for journald to see daemon output
```

### `user_config` keys (built from `users` row in `user_registry.py:244-275`)

```python
user_config = {
  "max_leverage": int,
  "max_daily_loss_usd": float,             # = max_daily_loss_pct × 100
  "max_position_notional": 500,
  "trading_pairs": list,
  "min_confidence": 45,
  "ml_threshold": 0.55,
  "size_multiplier": 1.0,
  "max_daily_trades": 15,
  "bot_mode": str,
  "maker_patience_mode": str,              # standard/patient/aggressive
  "exit_policy": str,                      # current (placeholder)
  "cohort_filter_enabled": bool,
  "shadow_simulated_balance": float|None,  # ← TODAY'S BUG 3b FIX
  "mark_alignment_enabled": bool,
}
```

### Hardcoded constants (sample)

| Constant | Value | Where |
|---|---|---|
| `MIN_CONFIDENCE` | 45 | qualify_signal |
| `MAX_LEVERAGE_HARDCAP` | 50x | compute_size |
| `MAX_OPEN_POSITIONS_PER_USER` | 3 | qualify_signal |
| `MAX_SAME_SIDE_SAME_SYMBOL` | 3 | qualify_signal |
| `SCALP_MAX_AGE_SEC` | 1800 (30 min) | _monitor_trade:2222 |
| `INTRADAY_MAX_AGE_SEC` | 3600 (60 min) | _monitor_trade:2222 |
| `SHADOW_FORCE_CLOSE_NO_PRICE_AT` | 2× max_age + 60s | _monitor_trade:1929 |
| `BYBIT_TAKER_FEE_PCT` | 0.000708 | bybit_shadow_monitor.py |
| `BYBIT_MAKER_FEE_PCT` | 0.000118 | bybit_shadow_simulator.py |
| `DAEMON_INTERVAL_SEC` | 5 | bybit_shadow_simulator main loop |
| `WS_SAMPLE_SEC` | 3 | bybit_shadow_simulator |
| `LOOKBACK_SEC` | 120 | fetch_orphaned_open_mirrors |
| `MONITOR_POLL_SEC` | 2 | bybit_shadow_monitor:monitor_task |

---

## §9. Dashboard API Surface

Registered in `dashboard/server.py` around line 700-840. ~80 endpoints. Summary by family:

```
/api/overview                       — header strip data
/api/quant-metrics                  — Sharpe/Sortino/MaxDD/PF
/api/exchange-comparison            — Delta vs Bybit aggregate
/api/multi-exchange/overview        — per-bucket active+last+today
/api/multi-exchange/closed          — per-bucket trade list (Surface E)

/api/paper/{active,closed,stats}    — paper-only views
/api/shadow/{exchanges,active,closed,stats}  — shadow per-exchange

/api/tracker/{stats,active,closed}  — signal_tracker introspection
/api/scanner-stats                  — per-scanner WR breakdown
/api/r-metrics                      — R-multiple distribution
/api/monitor/report                 — exit-guard health
/api/signal-status                  — funnel metrics
/api/infra                          — host/service health

/api/positions, /api/signals, /api/trades, /api/performance, /api/alerts
                                    — legacy aggregate endpoints

/api/ai/insights                    — LLM-summarized recent activity
/api/admin/*                        — kill-switch, user mgmt, audit
/api/replay/*                       — re-qualify a signal
/api/backtest/*                     — backtest upload + result
/api/profile/*, /api/twofa/*, /api/email/*  — user account
/ws                                  — WebSocket for live updates
```

Static UI:
- `dashboard/templates/index.html` — single SPA shell
- `dashboard/static/js/app.js` (legacy main bundle)
- `dashboard/static/js/{overview_strip, multi_exchange_overlay, trade_stream, quant_heroes, ...}.js`
- `dashboard/static/css/{theme_dark, multi_exchange_overlay, multi_exchange_badges, quant_heroes, ...}.css`

---

## §10. State Machines

### Trade lifecycle

```
            ┌──────────┐
            │ created  │  status='open', closed_at=NULL
            └────┬─────┘
                 │
   ┌─────────────┼──────────────┐
   ▼             ▼              ▼
exit_guard   parent_close    operator_force
(SL/TP/      cascade         (cleanup,
 trail/      (daemon or      kill_switch)
 max_age)    Mac dispatcher)
   │             │              │
   └─────────────┴──────────────┘
                 │
                 ▼
            ┌──────────┐
            │  closed  │  status='closed', closed_at=NOW()
            │          │  exit_price set, pnl_usd computed
            │          │  metadata.exit_reason set
            └──────────┘
```

### Signal lifecycle (paper, in `signal_tracker`)

```
scanner emits → grade/conf gate → tracked (active) → monitored
                                       │
                              SL/TP/trail/max_age
                                       │
                                  closed (oracle)
                                       │
                                feeds learning loop
```

### Manager lifecycle (`UserRealManager`)

```
LAZY: created on first broadcast_signal for that user
   │
   ├── reconcile_open_trades()  ← TODAY'S BUG 2 FIX in this path
   │      ├── reconcile real trades (orig)
   │      └── reconcile shadow trades (NEW)  → respawns _monitor_trade
   │
   ├── execute_signal()  on each broadcast
   │      └── qualify → compute_size → record → spawn monitor
   │
   └── (per open trade) _monitor_trade tasks
                          ├── reads orchestrator._ws_prices
                          ├── fires SL/TP/trail/max_age
                          └── _close_shadow / _close_trade
```

---

## §11. Failure Modes & Recovery Patterns

| Failure | Detection | Recovery |
|---|---|---|
| WS price feed stale | `Stale data for SYM` log every 60s | Bug 5 failsafe force-closes at 2×max_age |
| REST orderbook auth fail | `Balance fetch failed` log | Falls back to WS price; sizing uses default $100 (mitigated by Bug 3 ssb path) |
| `bybit-shadow-monitor` SQL crash | systemd `Restart=always` | TODAY: `$4::text` Bug 1 fix prevents crash loop |
| Bot restart orphans monitors | NEW: silent until trades stuck open | Bug 2 reconcile rebuilds monitor on next manager-init |
| User cohort losing 5 in a row | `cohort_blacklist_paused_until` set in DB | Auto-pause 14h, qualify_signal blocks |
| Daily loss > $25 | `cb.is_tripped()` | Manager halts trading until next UTC day |
| 3 consecutive losses | `cb.consecutive_losses>=3` | Manager halts trading, auto-resume on next UTC day |
| Mac dispatcher dies | NO health check | TODAY: 30 min without bybit_demo opens despite delta opens. Need watchdog. |
| Delta WS disconnect | logged + auto-reconnect in `delta_ws.py` | Reconnects; trades-in-flight may briefly miss prices (Bug 5 covers) |
| iptables rule expired | dashboard 403s | `dashboard-whitelist-sync.service` re-syncs hourly |

---

## §12. Bugs Verified-Fixed Today

| # | Bug | File:line | Fix | Evidence |
|---|---|---|---|---|
| 1 | `bybit_shadow_monitor` `IndeterminateDatatypeError: $4` crash loop | `scripts/bybit_shadow_monitor.py:258` | added `$4::text` cast in `jsonb_build_object` value param | service `is-active`, no error in journald |
| 2 | shadow trades orphaned on every restart (early `return` blocked shadow code path) | `execution/user_real_manager.py:387` (removed early return) + `:480-575` (added shadow reconcile block) | `if not rows: return` removed; new shadow-reconcile branch always runs | live trace `13:49:38`: `RECONCILED: SOL/USDT short ... resuming monitor` |
| 3a | `compute_size` ignored `shadow_simulated_balance` (lever3 rollback regression) | `execution/user_real_manager.py:194-203` (init), `:956-973` (compute_size base), `:1051-1057` (ceiling) | re-added init + balance source resolve + ceiling resolve from same source | UNVERIFIED in live trace yet (cohort pause blocks new signals till tomorrow) |
| 3b | `user_registry.user_config` missing 4 pass-through keys (`shadow_simulated_balance` chief among them) | `execution/user_registry.py:261-273` | added `exit_policy, cohort_filter_enabled, shadow_simulated_balance, mark_alignment_enabled` | UNVERIFIED in live trace yet |
| 4 | BOOK strip showed `+$2,301.1k` phantom (raw qty×entry, contract-naive) | `dashboard/server.py:1090-1110` | switched cap_deployed to `SUM(COALESCE(metadata.margin, entry × qty × COALESCE(metadata.contract_size, 1)))` | next dashboard refresh shows realistic margin |
| 5 | `_monitor_trade` deadlocked on `price=0` when feed stale (no failsafe) | `execution/user_real_manager.py:1885-1945` | `MONITOR_NO_PRICE` warning at 60s + `MONITOR_FORCE_CLOSE` at 2×max_age | wired; not yet triggered (no qualifying conditions in current window) |
| – | Multi-exchange UI overlay (5 surfaces A/B/C/D/F + Surface E lazy 4-tab) | `dashboard/static/{js,css}/multi_exchange_overlay*.{js,css}` + `dashboard/templates/index.html` | hides legacy DOM; injects `mex-*` containers; new endpoint `/api/multi-exchange/closed` | architect hard-refresh confirmed render |

---

## §13. Open Bugs / Risks (Prioritized)

### 🔴 CRITICAL

| # | Issue | Symptom | Likely cause | Action |
|---|---|---|---|---|
| **O1** | Delta India shadow PF=**0.09** vs Bybit shadow PF=**3.44** on identical 79 signals | -$50 vs +$77 over 24h | Either Delta L2 fill simulation too generous (we credit fills tighter than reality), OR Delta India spreads are genuinely worse | Audit `_close_shadow` Delta-side fill simulation; cross-check vs actual Delta DEMO fills from a paired test |
| **O2** | `_cached_balance` is ≈$0 for both users (admin $0.68, niranjan $0.00) | Without Bug 3 fix, sizing fell back to $100 default and produced absurd results | Demo Delta wallets are essentially empty; `fetch_balance` reports truth | Confirm Bug 3b sustains across restarts; long-term replace `_cached_balance` defaults with explicit per-user config |
| **O3** | Phase 2 monitor's `time_decay` exits net **-$7.62/24h** (vs daemon cascade +$84.67) | 10 trades closed via monitor's own time_decay all losers | Monitor max_age fires before daemon cascade has chance — and at that point the trade is already in time-decay loss territory | Add "wait-for-cascade" gate: if delta source still open AND age < 60min, defer monitor's own exit. OR raise monitor's max_age above daemon's typical detection window |
| **O4** | Mac demo dispatcher silently DEAD for 30+ min (no `bybit_demo` rows despite 2 new `delta_shadow` opens) | bybit_demo trade rate = 0 in last 30 min | Process probably exited / Mac slept / poll loop crashed | Architect: check `ps aux \| grep bybit_demo_dispatcher` on Mac; add launchd plist for auto-restart |
| **O5** | Reconciled monitors NOT actually closing trades (despite reconcile firing successfully) | SOL/USDT shadow trades still open at 36 min (past max_age=30min) after `RECONCILED ... resuming monitor` log | Resumed monitor task either (a) silently exits, or (b) loops without reaching max_age check, or (c) `_close_trade`/`_close_shadow` raises silently | Add post-reconcile `MONITOR_HEARTBEAT` log every 60s; verify `self.open_trades.get(trade_id)` returns the rebuilt record |

### 🟡 MEDIUM

| # | Issue | Action |
|---|---|---|
| O6 | XRP/USDT only Bybit underperformer (-$7 gap vs Delta) | Audit Bybit XRP L2 depth; consider blocking XRP signals to Bybit |
| O7 | Manager creation is lazy on first signal per user | Eager-init at orchestrator startup so reconcile fires immediately |
| O8 | WS price `_ws_prices` may be sparse for low-volume symbols | Long-term: also pull bid/ask from `_delta_ws.l2_orderbook` (we have it cached) as backup |
| O9 | Delta auth-error spam every 60s in journald | Pass `DELTA_DEMO_API_KEY/SECRET` env vars OR rate-limit warning |
| O10 | Niranjan `shadow_simulated_balance=$443` half of admin's $1000 | Confirm intentional A/B vs accidental |
| O11 | Bot in `cohort_blacklist_paused_until` until tomorrow morning | Either wait till expiry to verify Bug 3b live, or manually clear pause (re-exposes cohort risk) |

### 🟢 LOW

| # | Issue |
|---|---|
| O12 | 7 rows/signal multi-user noise (expected; documented) |
| O13 | Cron `code_review_wrapper.sh` exits with `ERROR: usage:` every run — disable |
| O14 | `maker_mode_verdict` cron 6h → recommend daily (same data freshness, less noise) |
| O15 | Stale-data warnings for non-WS-active symbols cycle once/min — could rate-limit |
| O16 | Bybit demo `stalled_after_15min` matches Phase 2 monitor failure mode — same root cause class as O3 |

---

## §14. Operational Runbook

### Restart cryptobot
```bash
ssh -i ~/.ssh/cryptobot_oci opc@150.230.171.48 'sudo systemctl restart cryptobot'
# wait ~90s for boot + first SHADOW ENTRY log
```

### Restart auxiliary services
```bash
ssh ... 'sudo systemctl restart bybit-shadow-daemon'
ssh ... 'sudo systemctl restart bybit-shadow-monitor'
```

### Deploy a Python file change
```bash
cd ~/Desktop/Claude\ AI\ Crypto\ Bot/crypto-trading-bot
python3 -c "import ast; ast.parse(open('PATH').read()); print('OK')"
scp -i ~/.ssh/cryptobot_oci PATH opc@150.230.171.48:/tmp/
ssh -i ~/.ssh/cryptobot_oci opc@150.230.171.48 'sudo cp /tmp/FILE /home/opc/crypto-trading-bot/PATH && sudo chown opc:opc /home/opc/crypto-trading-bot/PATH && sudo systemctl restart cryptobot'
```

### Force-close stuck trades (emergency)
```sql
UPDATE user_trades
   SET status='closed', closed_at=NOW(), exit_price=entry_price,
       pnl_usd=0.0, fees_usd=COALESCE(fees_usd,0),
       metadata = COALESCE(metadata,'{}'::jsonb) ||
                  jsonb_build_object('exit_reason','force_orphan_cleanup','close_via','operator_force')
 WHERE closed_at IS NULL
   AND opened_at < NOW() - INTERVAL '15 minutes';
```

### Key journald greps
```bash
# sizing trace per signal
sudo journalctl -u cryptobot --since '5 minutes ago' | grep SIZE_BAL

# reconcile path health
sudo journalctl -u cryptobot --since '5 minutes ago' | grep -E 'RECON_START|shadow reconcile|RECONCILED|RECON_DONE'

# exit guard health (today's failsafe)
sudo journalctl -u cryptobot --since '5 minutes ago' | grep -E 'MONITOR_NO_PRICE|MONITOR_FORCE_CLOSE'

# bybit shadow daemon health
sudo journalctl -u bybit-shadow-daemon --since '5 minutes ago' | tail -30

# bybit shadow monitor health
sudo journalctl -u bybit-shadow-monitor --since '5 minutes ago' | tail -30
```

### Per-bucket open count
```sql
SELECT exchange||'|'||trade_type as bucket,
       COUNT(*) as n,
       MAX(EXTRACT(EPOCH FROM (NOW()-opened_at))/60)::int as oldest_min
  FROM user_trades
 WHERE closed_at IS NULL
 GROUP BY bucket
 ORDER BY bucket;
```

### Per-bucket 24h aggregate
```sql
SELECT exchange||'|'||trade_type as bucket,
       COUNT(*) as n,
       SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) as wins,
       ROUND(SUM(pnl_usd)::numeric, 2) as net_pnl,
       ROUND(SUM(fees_usd)::numeric, 2) as fees,
       ROUND((SUM(CASE WHEN pnl_usd>0 THEN pnl_usd ELSE 0 END) /
              NULLIF(SUM(CASE WHEN pnl_usd<0 THEN -pnl_usd ELSE 0 END), 0))::numeric, 2) as pf
  FROM user_trades
 WHERE closed_at >= NOW() - INTERVAL '24 hours'
 GROUP BY bucket
 ORDER BY bucket;
```

---

## §15. Decision Log (Architect Calls Required)

| # | Question | Today's Data Says | Default if no decision |
|---|---|---|---|
| D1 | Migrate live trading Delta India → Bybit? | Yes — Bybit shadow PF 3.44 vs Delta 0.09; +$127/day edge | Stay on Delta (no real trading active right now anyway) |
| D2 | Phase 2 bybit-shadow-monitor: keep / fail-safe-only / kill? | Currently nets -$7.62/24h on its own exits while daemon cascade nets +$84.67 | Keep running but recommend "fail-safe only" mode (fire only if daemon dead >5min) |
| D3 | XRP/USDT cohort: continue or skip? | Only Bybit underperformer (-$7 gap vs Delta -$5) | Continue but flag for cohort-blacklist if 5/5 last lose |
| D4 | Niranjan `shadow_simulated_balance=$443` — intentional A/B or stale config? | Resulted in 2.3× admin sizing post-fix (correctly proportional to sim bankroll) | Leave as-is unless architect says "set both equal" |
| D5 | Eager manager init at orchestrator startup? | Closes the lazy-init orphan window seen today | Worth shipping; low risk, high systemic improvement |
| D6 | Cloudflare Tunnel for dashboard public access? | Solves architect's mobile-IP problem from earlier session | Defer — separate session |
| D7 | Mac demo dispatcher watchdog (launchd plist)? | Currently silently dead → no demo data accumulating | Recommend ship — one-time setup, eliminates future "no demo trades" surprises |
| D8 | Resolve Bug O5 (reconciled monitors don't close)? | SOL trades 36min open after RECONCILED log, no close | Requires diagnosis — add MONITOR_HEARTBEAT log + verify open_trades dict ownership |

---

## Appendix A: file index touched today (2026-04-26)

| File | Lines added/changed | Purpose |
|---|---|---|
| `scripts/bybit_shadow_monitor.py` | +1 | Bug 1: `$4::text` cast |
| `execution/user_real_manager.py` | ~+150 | Bug 2 (shadow reconcile branch), Bug 3a (ssb init + base + ceiling), Bug 5 (no-price failsafe), SIZE_BAL diagnostic |
| `execution/user_registry.py` | +12 | Bug 3b (4 missing user_config keys), warning-level RECON_START/DONE/created-manager |
| `dashboard/server.py` | ~+90 | `/api/multi-exchange/closed`, contract-aware BOOK cap, SL/TP/leverage/last_price in active rows |
| `dashboard/static/js/multi_exchange_overlay.js` | ~+200 | Surface E (Analytics 4-tab); SL/TP/PnL/lev in active rows; 4-cell hero; dropdown placement |
| `dashboard/static/css/multi_exchange_overlay.css` | ~+170 | Surface E styles; 4-cell hero; SL/TP coloring; dropdown |

## Appendix B: Verified-vs-Unverified status

| Bug | Code | Live evidence |
|---|---|---|
| 1 | ✅ deployed | ✅ daemon running, no $4 errors |
| 2 | ✅ deployed | ✅ `RECONCILED: SOL/USDT short ... resuming monitor` |
| 3a | ✅ deployed | ❌ no SIZE_BAL trace yet (cohort pause blocks signals) |
| 3b | ✅ deployed | ❌ no SIZE_BAL trace yet (cohort pause blocks signals) |
| 4 | ✅ deployed | ✅ no more $2.3M phantom in BOOK strip |
| 5 | ✅ deployed | ⚠️ failsafe wired; not yet triggered |
| O5 | ❌ NEW BUG | reconciled monitor not firing exits; SOL trades 36min stuck |

---

**END OF DOCUMENT.**

Spec status: living. Update on every bug closed or new architectural change. Companion exec-summary at `docs/FULL_FLOW_SPEC_20260426.md`.
