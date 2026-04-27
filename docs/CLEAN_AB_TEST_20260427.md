# Clean A/B Test — Paper vs Delta Shadow (zero noise)
**Started:** 2026-04-27 03:46 UTC
**Architect directive:** "lets have a clean test ... Paper as is | Delta admin shadow_live + niranjan shadow_live merge all | open all signals as paper to delta | bybit pause all operations"

---

## Hypothesis being tested

> **Does the bot's edge survive Delta India execution friction (entry slippage + fees + exit logic), with the noise of qualify-gates + Bybit + per-user A/B treatment removed?**

If paper shows PF ≈ 11 and delta_shadow shows PF ≥ 5 → real edge survives execution
If paper shows PF ≈ 11 and delta_shadow shows PF ~ 1 → execution friction eats most of the edge
If paper shows PF ≈ 11 and delta_shadow shows PF < 1 → execution layer fundamentally broken (need refactor)

---

## Setup (deployed in cryptobot at 03:46 UTC)

### What's RUNNING
| Component | Mode |
|---|---|
| Paper engine | UNCHANGED — fires all qualified scanner signals |
| Delta shadow (admin) | shadow_live, **STANDARD** exit guards (no relaxed mode) |
| Delta shadow (niranjan) | shadow_live, **STANDARD** exit guards (no relaxed mode) |
| `qualify_signal` | **BYPASSED** for shadow_live mode — every paper signal mirrors |

### What's PAUSED
| Component | Status |
|---|---|
| `bybit-shadow-daemon` | inactive (stopped + disabled) |
| `bybit-shadow-monitor` | inactive (stopped + disabled) |
| `incident_auto_responder` | edited — bybit removed from auto-restart whitelist |
| Mac demo dispatcher | architect must stop manually (foreground process) |

### What's DEFERRED for clean test
| Item | Why deferred |
|---|---|
| Niranjan A/B treatment (FIX 1+2 + Stage 1+2) | Both users on identical code path — merges samples |
| Bybit shadow + demo | Pure delta-vs-paper read; bybit re-enabled later |
| Bug A (symbol map gap) | bybit-side, irrelevant during pause |
| Bug B (Mac dispatcher metadata) | bybit-side, irrelevant during pause |

---

## Implementation details

### 1. Disabled niranjan A/B in `user_real_manager.py:__init__`
```python
self._relaxed_shadow_exits = False  # disabled for clean test
self._relaxed_shadow_simulation = False  # disabled for clean test
if self._is_shadow_live:
    logger.warning("CLEAN_AB_MODE: %s on STANDARD guards (relaxed disabled), "
                   "qualify_signal bypassed in shadow_live (every paper signal mirrors)",
                   self.user_email)
```
Code paths for relaxed mode REMAIN in the codebase (just gated False). Re-enable later by flipping the flags.

### 2. Bypassed qualify_signal in `user_real_manager.py:execute_signal`
```python
if getattr(self, "_is_shadow_live", False):
    qualified, reason = True, "shadow_clean_test_bypass"
else:
    qualified, reason = await self.qualify_signal(signal)
```
Live trades (when `bot_mode='live'`) still qualify normally.

### 3. Stopped Bybit services
```bash
sudo systemctl stop bybit-shadow-daemon bybit-shadow-monitor
sudo systemctl disable bybit-shadow-daemon bybit-shadow-monitor
```

### 4. Removed Bybit from Agent 9-A auto-restart whitelist
```python
WHITELIST_SERVICES = ["cryptobot"]  # bybit-* PAUSED
```

---

## Verification

```
Apr 27 03:47:54 cryptobot[3073880]:
  CLEAN_AB_MODE: niranjan_139@yahoo.co.in on STANDARD guards
  (relaxed disabled), qualify_signal bypassed in shadow_live
  (every paper signal mirrors)
Apr 27 03:48:37 cryptobot[3073880]:
  CLEAN_AB_MODE: admin@vnedge.com on STANDARD guards (...)

bybit-shadow-daemon = inactive
bybit-shadow-monitor = inactive
```

---

## Expected behavior

1. **Paper opens N signals per hour** (currently ~5-10/h depending on cohort_pause state)
2. **Each paper signal → 2 delta_shadow opens** (1 admin + 1 niranjan)
3. **NO bybit_shadow rows created** (daemon paused)
4. **NO bybit_demo rows created from VM-side** (Mac dispatcher = separate, architect to stop)
5. **No QUALIFY_REJECT logs** for shadow_live users (bypass)

If paper opens 10 → expect 20 delta_shadow opens (10 per user). Conversion ratio should hit 1.0× per user, vs the previous ~0.31 with qualify_signal active.

---

## What to measure (when sample reaches 30+ closed per user)

| Metric | Paper baseline | Delta admin target | Delta niranjan target |
|---|---|---|---|
| Conversion rate | 100% | ≥ 99% | ≥ 99% |
| WR | ~82% | ≥ 50% (real check) | ≥ 50% |
| Net PnL ($) | +$852/24h baseline | TBD | TBD |
| PF | 11.2 | ≥ 1.5 (acceptable), ≥ 5 (target) | ≥ 1.5 |
| Avg duration | 4.5 min | TBD (likely 10-30 min) | TBD |
| Top exit reason | trail_profit (74%) | depends on guards | depends on guards |

---

## Decision tree at verdict time

| Delta_shadow PF (per user) | Read | Action |
|---|---|---|
| ≥ 5.0 | execution layer is fine | re-enable bybit + niranjan A/B for venue compare |
| 1.5 — 5.0 | execution friction is real but manageable | re-enable bybit, deploy Stage 1+2 cautiously |
| 0.5 — 1.5 | execution borderline | deeper investigation: which exit reason dominates? |
| < 0.5 | execution layer fundamentally broken | escalate, architect-driven refactor |

---

## Restoration plan (when test concludes)

```bash
# 1. Re-enable bybit services
ssh -i ~/.ssh/cryptobot_oci opc@150.230.171.48 'sudo systemctl enable --now bybit-shadow-daemon bybit-shadow-monitor'

# 2. Re-add bybit to incident_auto_responder whitelist
# Edit scripts/incident_auto_responder.py line ~49:
WHITELIST_SERVICES = ["cryptobot", "bybit-shadow-daemon", "bybit-shadow-monitor"]

# 3. Restore niranjan A/B treatment
# Edit execution/user_real_manager.py:__init__ — restore the original flag logic
# (current commented-out form preserved for easy restore)

# 4. Restore qualify_signal
# Edit execution/user_real_manager.py:execute_signal — remove the shadow_live bypass

# Then deploy via Agent 3 Gatekeeper as usual.
```

---

## Backups (if rollback needed quickly)

```bash
# All deploys via Gatekeeper produce timestamped backups in .rollback/
# Most recent before this test:
.rollback/user_real_manager.py.bak_20260427_034620
```

To full-revert just this clean-A/B setup:
```bash
ssh -i ~/.ssh/cryptobot_oci opc@150.230.171.48 'sudo cp \
  /home/opc/crypto-trading-bot/.rollback/user_real_manager.py.bak_20260427_034620 \
  /home/opc/crypto-trading-bot/execution/user_real_manager.py && \
  sudo systemctl enable --now bybit-shadow-daemon bybit-shadow-monitor && \
  sudo systemctl restart cryptobot'
```
