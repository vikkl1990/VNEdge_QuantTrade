# Scanner Cluster Analysis — TODO
**Authored:** 2026-09-15
**Data provenance:** last published all-time cut (through 2026-04-04, ~1,000 fills) read against the routing spec as of this date (2026-09-15). Live dashboards were unavailable when this analysis was done, so the performance numbers below are **not** re-verified against this repo's current trade history — this repo's local `storage/closed_signals.json` only holds 22 recent trades, far too few and far too recent to reproduce or contradict the 1,000-fill cut cited here. Treat everything dated after 2026-04 as unmeasured in public data until re-run against current data.

Companion doc: see the "Scanner Entry/Exit Logic" flow diagram (published artifact, this session) for the full entry/exit pipeline these clusters route through, and the routing/funnel-accounting fix in commit `f8ed935`.

---

## Why cluster, not scanner

Step F of the entry pipeline is winner-take-all by `weighted_score` — only one scanner's candidate can fill per symbol per bar. Two scanners in the same category firing on the same 5m close are one bet, not two, so measuring performance per-scanner instead of per-cluster overstates how many independent edges the book actually runs.

| Cluster | Live members | Dead / paper | Role in the funnel |
|---|---|---|---|
| Structure / ICT | `structure_bounce`, `liquidity_sweep`, `bos_choch`, `order_block_entry` | — | Level, sweep, BOS, unmitigated OB |
| Reversion | `rsi_divergence`, `cvd_divergence`, `vwap_mean_revert`, `rsi_extreme` | `vwap_bounce` | Extreme + fail-to-confirm |
| Momentum / trend | `trend_continuation`, `ema_momentum`, `bb_squeeze`, `post_impulse`, `volume_surge` | `supertrend_flip`, `momentum_ride`, `bb_band_walk`, `momentum_surge` | Impulse, pullback, expansion |
| Pattern | `candlestick_reversal` | — | Engulf / hammer at a 10-bar extreme |
| Unscored | — | `simple_bias` | Label factory only (learning mode) |

## What actually printed (last published cut, 1,000 fills)

| Cluster | Fills | Share | WR | Net | Avg / trade | Verdict on this sample |
|---|---:|---:|---:|---:|---:|---|
| Structure — `structure_bounce` | 980 | 98.0% | 73.5% | +$99.62 | +$0.10 | Only statistically live book |
| Structure — `liquidity_sweep` | 19 | 1.9% | 36.8% | −$0.33 | −$0.02 | Underwater, n too small to kill or promote |
| Structure — `bos_choch` | 1 | 0.1% | 0% | −$1.29 | −$1.29 | Anecdote |
| Reversion (all 4) | 0 | 0% | — | — | — | Routed, not filling |
| Momentum (all 5 live) | 0 | 0% | — | — | — | Routed, not filling |
| Pattern | 0 | 0% | — | — | — | Paper as of 2026-09-14 |
| Dead — `momentum_surge` (shadow) | shadow | — | ~38–39% | — | — | Correctly unwired |
| Dead — `supertrend_flip` (shadow) | shadow | — | 26% | — | — | Correctly unwired |

`order_block_entry`, `trend_continuation` (now the broadest-routed live scanner), `ema_momentum`, all four live reversion scanners, `bb_squeeze`, `post_impulse`, and `volume_surge` have **no published live fills**. A cluster with zero fills is not "flat" — it's unidentified: it could mean the trigger genuinely never occurs, or that it keeps losing step F to `structure_bounce` on the same bar.

## Cluster verdicts

**1 — Structure (the real book).** This cluster *is* the live system. Session-gated S/R rejection, wick close in the outer 55%, confirmation away from the level; allowed in every regime except low-liquidity; longs only (shorts measured −0.5 to −0.9 ATR historically, disabled). Economics: high WR (73.5%) with tiny dollars per ticket (+$0.10) — a fee-fragile scalp, not a runner. Fee drag ate 32–40% of gross on good days, 66%+ when WR fell under 60%, and 130% on 2026-04-04 (fees exceeded gross). A regime/exit tweak that day collapsed daily WR from high-70s to 50% then 21.4% — the edge is real in a stable regime and disappears when max-age/ADX gates thrash. `liquidity_sweep` (19 fills, 36.8% WR) and `bos_choch` (n=1) are the same level-reclaim idea with stricter triggers, not diversification — shadow them until each clears ≥200 post-funnel fills with a expectancy CI that excludes zero. `order_block_entry` is live-routed in breakout/ranging/volatile with zero published fills — plausibly losing step F to `structure_bounce` on the same bar every time.

**2 — Reversion.** Suppressed twice in the current pipeline: the VWAP noise-zone check (entry node B) — soft-penalty only today, see the corrected diagram — still penalizes the exact zone `vwap_mean_revert` and RSI/CVD extremes are built to fire in, and even when a reversion candidate clears that, it still competes with `structure_bounce` on raw `weighted_score` and loses. Not a failed edge — an edge that's never been measured in isolation. A prior related experiment (P2: 4h override of ranging→trending) was tested at 23% WR on 39 trades and disabled — the only clean reversion-adjacent result on record, and it lost.

**3 — Momentum / trend.** The most over-wired, least observed cluster. `trend_continuation` is the broadest-routed live scanner and still has zero published fills — either its trigger is rarer than its comments suggest, or it loses step F to a structure print on the same close every time. `ema_momentum` longs-only is already correct (shorts were 0% WR) but the long side itself is unmeasured. `post_impulse` is the most regime-restricted live scanner (`trending_up` only) — correct concentration, still zero fills. `bb_squeeze` has a real hard gate (`rel_vol > 1.0`) and is still idle. `volume_surge` is one day old in the spec (2026-09-14) and will collinear with `trend_continuation`/`bb_squeeze` on the same expansion bar — keep it in an isolated paper book. The three confirmed-dead scanners (`momentum_surge` ~38% WR, `supertrend_flip` 26% WR, `momentum_ride` shorts 33% WR) are cluster-level results, not one-off bad luck — do not rewire them back in without a fresh walk-forward cell.

**4 — Pattern.** `candlestick_reversal` fires on the same bar family as `structure_bounce` (rejection at a local extreme). Without a cluster mutex it will double-count the flagship the moment it goes live. Paper-only until n and non-overlap-vs-structure are both proven.

**5 — Unscored.** `simple_bias` is correctly locked to learning mode. If it ever leaks into step F, it contaminates every other cluster's measured WR and ML labels.

## Cross-cluster effects that dominate the table

- **Concentration.** 98% of fills from one scanner in one cluster. Adding routed scanners did not add independent bets — it added comments.
- **Fee wall.** High-WR structure scalps at $100 × 30x still surrender a third to two-thirds of gross to fees. Momentum and reversion need a larger R per trade or they'll look "fine" in win rate and still lose in dollars.
- **Routing vs. permission drift.** The April cut blocked several pairs the September spec now allows (e.g. `rsi_divergence` in volatile/high-vol, `trend_continuation` in ranging/volatile). Changing routing without a per-cluster holdout is how a 73% book quietly becomes a 50% day.
- **Side is a cluster, not a flag.** Every measured short in this family is bad: structure shorts −0.5 to −0.9 ATR, EMA shorts 0% WR, `momentum_ride` shorts 33% WR, `momentum_surge` ~38% WR. Analyze long-structure vs. everything-else; don't pool sides together.

## Operating verdict

| Cluster | Capital | Paper isolated | Research only | Why |
|---|---|---|---|---|
| Structure — `structure_bounce` long | Yes | — | — | Only cluster with both n and positive net PnL |
| Structure — sweep / BOS / OB | No | Yes | — | Same idea, no edge in this sample |
| Reversion | No | Yes, after VWAP rule split | — | Zero fills; current step B suppresses the setup |
| Momentum live set | No | One scanner, `trending_up` only | The other six | Zero fills + dead-file WRs |
| Pattern | No | Yes | — | Unvalidated, collinear with structure |
| Unscored / dead | No | No | Yes | Known WR holes or label-only |

## TODO — what to compute next (or this table stays fiction)

Per cluster, not per scanner, on **post-funnel fills only**:

- [ ] n, long vs. short, WR, mean R, p10 R, profit factor, fee drag as % of gross
- [ ] Overlap rate: fraction of winning bars where a second cluster also printed a candidate
- [ ] Steal rate: how often `structure_bounce` wins step F against a live momentum/reversion candidate on the same bar
- [ ] Counterfactual R: what the step-F loser would have scored if it had been taken instead
- [ ] Re-run all of the above against **current** trade history (not the 2026-04-04 cut) once enough post-fix volume accumulates — this repo's `storage/closed_signals.json` is the candidate source, but only has 22 trades as of this writing

**Promotion bar** (a cluster graduates from "paper isolated" to "capital" only when all of):
- n ≥ 200 isolated fills
- Expectancy confidence interval excludes 0
- Fee drag < 40% of gross
- Steal-rate against `structure_bounce` documented (not just assumed zero)

Anything below that bar is a name on the routing table, not a cluster with performance.

## Related, still-open from the same review pass

- Priority-1 routing/funnel-accounting fix: **shipped**, commit `f8ed935` (2026-09-15), verified live.
- VWAP noise-zone hard veto: confirmed **dormant** (`_VWAP_HARD_VETO_ENFORCE = False`), soft-penalty only — the reversion cluster's suppression is via the *soft* penalty stacking against `structure_bounce`'s weighted_score, not a hard block. Scanner-conditional VWAP rule (Priority 3) should be scoped against this fact, not against an assumed hard veto.
- Position sizing (`$100 × 30x` flat) overhaul: **not started**. Flagged to the user as needing explicit numeric risk-parameter sign-off (leverage caps by regime/style, EV-scaling formula) before touching live risk code, rather than inventing numbers unilaterally.
- Existing backtest/replay tooling found but not yet run from this checkout: `scripts/exit_variant_backtest.py` (exit-rule ablation — exactly the Priority-4 ask, though its own docstring says it can't model `no_momentum`/`exhaustion_*`/`dead_market`), `docs/validation/two_fold_results.json` (walk-forward, 2026-09-14), `storage/backtest_results/` (per-scanner-per-symbol backtests). The exit-variant script is hardcoded to a `/home/opc/crypto-trading-bot` root (looks built for the production VM) and its output dir (`storage/backtest_exit/`) is currently empty on this checkout.
