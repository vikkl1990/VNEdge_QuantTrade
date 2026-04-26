# A/B Test — Relaxed Shadow Exits (FIX 1+2)
**Started:** 2026-04-26 15:48 UTC
**Hypothesis:** Delta shadow PF=0.09 is caused by defensive exit guards firing on shadow execution slippage, not real adverse movement. Loosening the guards in a slippage-aware way recovers the paper edge.

---

## What changed

`execution/exit_guards.py:should_kill_dead_signal()` now accepts `relaxed_shadow=False` parameter. When True:

| Constant | Standard | RELAXED | Why |
|---|---|---|---|
| `UNIFIED_KILL_CURRENT_R` | -0.10R | **-0.16R** | absorbs the ~0.06R slippage hole shadow starts in |
| `FEE_FLOOR multiplier` | 1.0× | **1.5×** | trade has more room before deemed "never made fees" |
| `PATIENCE multiplier` | 4× / 2× / 2.5× / 1.5× | × **1.5** | late developers get 50% more time |
| `STALL_CURRENT_R` | -0.05R | **-0.075R** | 15-min stall kill less aggressive |
| `GRACE_SEC`, `STALL_AGE_SEC` | unchanged | unchanged | structural windows preserved |

`execution/user_real_manager.py:__init__` sets `self._relaxed_shadow_exits = (is_shadow_live AND user_email == 'niranjan_139@yahoo.co.in')`.

`_monitor_trade()` passes the flag to `should_kill_dead_signal()`.

## A/B groups

| Group | User | Mode | Treatment |
|---|---|---|---|
| **CONTROL** | admin@vnedge.com | shadow_live | standard guards (current) |
| **TREATMENT** | niranjan_139@yahoo.co.in | shadow_live | RELAXED guards |

Both users receive identical signals from the same paper engine via `UserRealRegistry.broadcast_signal()`. The ONLY difference is exit guard behavior.

## Hypothesis

Today's 24h data:
- Delta shadow PF = **0.09**, net **-$50.27** across 50 trades
- 8 of 9 exit-reason categories net negative
- $47/$50 of daily loss came from defensive exits

If hypothesis correct:
- Niranjan (relaxed): PF should rise from 0.09 → **>1.0** (likely 1.5-2.5)
- Admin (control): PF stays at ~0.09
- Niranjan's `dead_signal_unified` exits: drop from 8 → ~2-3
- Niranjan's `stalled_after_15min` exits: drop modestly
- Niranjan's `trail_profit` / `tp_hit` exits: rise (more trades reach winning territory)

If hypothesis WRONG:
- Niranjan trades just hold longer to bigger losses (max_age firing on losers that the guards correctly killed earlier)
- Niranjan's `time_decay_30m` (max_age) exits dominate, all losers
- Net PnL gap unchanged or worse

## Verification log

```
Apr 26 15:48:45 cryptobot-vm-a1 cryptobot[2957113]:
  RELAXED_SHADOW_EXITS: ENABLED for niranjan_139@yahoo.co.in
  (A/B treatment: kill_R=-0.16, patience×1.5, fee_floor×1.5, stall_R=-0.075)
```

Admin shows NO `RELAXED_SHADOW_EXITS` log → control mode confirmed.

## Verdict timeline

The pre-existing scheduled task `wave6c-paper-shadow-gap-verdict-48h` fires
**2026-04-27 14:30 UTC** — measures admin (filter+sizing ON) vs niranjan
(originally control). That verdict will pick up THIS A/B too because niranjan
is now also "relaxed exits" treated. So tomorrow's verdict tells us:

**Niranjan (cohort filter + relaxed exits) vs Admin (cohort filter + standard exits)**

Min sample needed: **20 closed trades per user** for credible verdict.
Expected by 14:30 UTC tomorrow: ~30-40 closed per user (current rate ~1.5/h).

## Monitoring queries

```sql
-- Per-user closed-trade aggregate (post-deploy only)
SELECT u.email, COUNT(*) as n,
       SUM(CASE WHEN ut.pnl_usd>0 THEN 1 ELSE 0 END) as wins,
       ROUND(100.0 * SUM(CASE WHEN ut.pnl_usd>0 THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*),0)::numeric, 1) as wr_pct,
       ROUND(SUM(ut.pnl_usd)::numeric, 2) as net,
       ROUND((SUM(CASE WHEN ut.pnl_usd>0 THEN ut.pnl_usd ELSE 0 END) /
              NULLIF(SUM(CASE WHEN ut.pnl_usd<0 THEN -ut.pnl_usd ELSE 0 END), 0))::numeric, 2) as pf
FROM user_trades ut JOIN users u ON u.id=ut.user_id
WHERE ut.exchange='delta_india' AND ut.trade_type='shadow'
  AND ut.closed_at >= '2026-04-26 15:48:00+00'
GROUP BY u.email;

-- Per-user exit-reason breakdown
SELECT u.email,
       COALESCE(metadata::jsonb->>'exit_reason',status) as reason,
       COUNT(*) as n, ROUND(SUM(pnl_usd)::numeric,2) as net
FROM user_trades ut JOIN users u ON u.id=ut.user_id
WHERE ut.exchange='delta_india' AND ut.trade_type='shadow'
  AND ut.closed_at >= '2026-04-26 15:48:00+00'
GROUP BY u.email, reason ORDER BY u.email, n DESC;
```

## Rollback procedure

If niranjan's PnL drops materially worse than admin's within 4 hours, rollback:

```bash
ssh -i ~/.ssh/cryptobot_oci opc@150.230.171.48 'sudo cp \
  /home/opc/crypto-trading-bot/.rollback/exit_guards.py.bak_20260426_154503 \
  /home/opc/crypto-trading-bot/execution/exit_guards.py && \
  sudo cp /home/opc/crypto-trading-bot/.rollback/user_real_manager.py.bak_20260426_154638 \
  /home/opc/crypto-trading-bot/execution/user_real_manager.py && \
  sudo systemctl restart cryptobot'
```

## Files modified

| File | Lines | Change |
|---|---|---|
| `execution/exit_guards.py` | +37 | Added `relaxed_shadow` param + 4 multipliers |
| `execution/user_real_manager.py` | +18 init, +2 call site | Per-user gate + flag-passing |

Tests: `pytest tests/test_exit_guards.py` — 47 passed (no regression on standard mode).

Deploy log: see `storage/deploy_log.txt` entries `20260426_154503` (exit_guards) and `20260426_154638` (user_real_manager).

Both deploys went through Agent 3 (Deploy Gatekeeper) — its first production use. Backups stored at `.rollback/{exit_guards,user_real_manager}.py.bak_20260426_*`.
