# Phase 2 — Shadow-of-Shadow Forward Test
**Authored:** 2026-04-27
**Goal:** Forward-test N exit configurations in parallel against real-time market data.

---

## Why this exists

Phase 1 (`counterfactual_exit_sweep.py`) is APPROXIMATE — uses recorded `peak_mfe_r` as proxy and assumes linear PnL interpolation for max_age. Findings are directional but not precise.

Phase 2 generates LIVE data for each exit config under real market conditions. No approximation.

---

## Architecture

### Per paper signal — fan out to N+1 trades per user

```
Paper signal fires
  └─► UserRealManager.execute_signal()
        ├─► trade row #1: exit_config_id="primary"  (current production logic)
        ├─► trade row #2: exit_config_id="v1_5min_tight"
        ├─► trade row #3: exit_config_id="v2_10min_no_kill"
        ├─► trade row #4: exit_config_id="v3_30min_paper_aligned"
        └─► trade row #5: exit_config_id="v4_60min_unrestricted"
```

Each row gets its own `_monitor_trade` task with config-specific exit logic.

### user_trades schema additions

```sql
-- Already supported via metadata jsonb (no schema change needed)
metadata.exit_config_id        — string identifier of which config
metadata.exit_config_summary   — human-readable summary for analysis
metadata.is_phase2_virtual     — true to distinguish from production rows
```

### Exit configs to test

| ID | max_age | trail_trigger | trail_lock | dead_signal | stall | tp_R | Notes |
|---|---|---|---|---|---|---|---|
| `primary` | 600s | 0.5R | 80% | -0.10R | -0.05R | none | Current production after Action 1 |
| `v1_5min_tight` | 300s | 0.5R | 80% | -0.10R | OFF | none | Most aggressive timeout |
| `v2_10min_no_kill` | 600s | 0.5R | 80% | OFF | OFF | none | Trail+timeout only |
| `v3_30min_paper` | 1800s | 0.3R | 50% | OFF | OFF | none | Paper-aligned (let winners run) |
| `v4_60min_unrestricted` | 3600s | 0.7R | 80% | OFF | OFF | 2.0R | TP added, no defensive kills |

5 configs × 2 users × ~10 paper signals/h = **100 virtual trades/h** — way more sample, fast verdict.

### Logic in user_real_manager

```python
EXIT_CONFIGS = [
    {"id": "primary",          "max_age": 600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill": -0.10, "stall_kill": -0.05, "tp_R": None},
    {"id": "v1_5min_tight",    "max_age": 300, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill": -0.10, "stall_kill": None,  "tp_R": None},
    {"id": "v2_10min_no_kill", "max_age": 600, "trail_trigger": 0.5, "trail_lock": 0.80,
     "dead_kill": None,  "stall_kill": None,  "tp_R": None},
    {"id": "v3_30min_paper",   "max_age": 1800,"trail_trigger": 0.3, "trail_lock": 0.50,
     "dead_kill": None,  "stall_kill": None,  "tp_R": None},
    {"id": "v4_60min_unrest",  "max_age": 3600,"trail_trigger": 0.7, "trail_lock": 0.80,
     "dead_kill": None,  "stall_kill": None,  "tp_R": 2.0},
]

# In execute_signal, after qualify+sizing pass:
if getattr(self, "_phase2_shadow_of_shadow", False):
    for cfg in EXIT_CONFIGS:
        await self._record_shadow_trade_with_config(signal, sizing_result, cfg)
else:
    await self._record_shadow_trade(signal, sizing_result)  # current single-trade path

# In _monitor_trade, exit logic reads trade._exit_config:
cfg = getattr(trade, "_exit_config", None) or DEFAULT_CFG
max_age = cfg["max_age"]
trail_trigger = cfg["trail_trigger"]
# ... etc
```

---

## Storage / analysis

Each virtual trade row carries its config_id in metadata. Analysis script:

```sql
-- After 24h of virtual trades, compute per-config aggregate
SELECT
    metadata::jsonb->>'exit_config_id' as config,
    COUNT(*) as n,
    SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) as wins,
    ROUND(SUM(pnl_usd)::numeric, 2) as net,
    ROUND(AVG(pnl_usd)::numeric, 3) as avg,
    ROUND((SUM(CASE WHEN pnl_usd>0 THEN pnl_usd ELSE 0 END) /
           NULLIF(SUM(CASE WHEN pnl_usd<0 THEN -pnl_usd ELSE 0 END), 0))::numeric, 2) as pf
FROM user_trades
WHERE metadata::jsonb->>'is_phase2_virtual' = 'true'
  AND closed_at >= NOW() - INTERVAL '24 hours'
GROUP BY config
ORDER BY net DESC;
```

This gives the **leaderboard** the architect asked for, with REAL PnL on REAL price evolution, no approximation.

---

## Risks / mitigations

| Risk | Mitigation |
|---|---|
| 5× DB write load | each trade is ~1KB; 100 trades/h = 100KB/h — negligible |
| Per-trade monitor task explosion (5×) | tasks are async + lightweight (just polling _ws_prices); bounded by max_open=5 per cfg |
| confusing existing dashboards | filter `is_phase2_virtual='true'` to keep dashboards on production data |
| Sizing discrepancy | use IDENTICAL margin/lots across all 5 configs (same fill simulation) |
| Disk exhaustion | configs only run during the experiment window (e.g. 48h) then auto-disable |

---

## Implementation effort

- ~250 LOC in user_real_manager.py
- ~50 LOC analysis script (`scripts/phase2_leaderboard.py`)
- ~30 LOC env-flag toggle (`PHASE2_SOS=true` to enable)
- 1 doc + 1 commit + 1 push

Expected dev time: **2-4 hours** to ship + 24h forward test for verdict.

---

## Phase 2 launch criteria (don't ship until):

1. Phase 1 max_age tightening has run for at least 1h to confirm it doesn't break anything
2. Architect explicit greenlight (this doc reviewed)
3. Bug 3c sizing fix verified working (already confirmed at 02:21 UTC)

---

## Phase 3 (post-Phase 2)

Build proper backtest replay over historical L2 + signals. Agent 13's currently-flagged capability gap. Multi-day project.

The three-phase ladder gives us:
- **Phase 1 (DONE)**: directional approximation in 1 hour
- **Phase 2 (NEXT)**: precise forward test in 24-48h
- **Phase 3 (LATER)**: precise historical backtest in 3-5 days

Each phase informs the next without blocking it.
