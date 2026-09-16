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
- Existing backtest/replay tooling found but not yet run from this checkout: `scripts/exit_variant_backtest.py` (exit-rule ablation — exactly the Priority-4 ask, though its own docstring says it can't model `no_momentum`/`exhaustion_*`/`dead_market`), `docs/validation/two_fold_results.json` (walk-forward, 2026-09-14), `storage/backtest_results/` (per-scanner-per-symbol backtests). **Confirmed broken on this checkout**: `scripts/exit_variant_backtest.py` imports `backtest.execution_replay.engine`/`fill_model`/`metrics`, none of which exist under `backtest/execution_replay/` here (only `candle_cache.py` does) — `ModuleNotFoundError` on import. The repo's own capability registry (`scripts/backtest_engineer.py`) independently lists `exit_logic_replay` as `🔴 GAP — not implemented`, which turns out to be the accurate status; the exit-variant script was likely written against a different branch/environment (it's hardcoded to a `/home/opc/crypto-trading-bot` root, matching the production VM shape seen in `/api/infra`) and never finished landing here. The registry's `counterfactual_exit` entry (`scripts/counterfactual_exit_analyzer.py`, marked ✅ available) is the more promising lead for the "counterfactual R if the loser of step F had been taken instead" ask — not yet checked whether it actually runs.

## Cluster mutex — shipped

Commit `2c55c19` (2026-09-15) implements the intra-cluster + inter-cluster mutex at step F exactly as specified: `SCANNER_CLUSTER` (structure / reversion / momentum / pattern-aliased-to-structure / unscored) plus `_apply_cluster_mutex()`, inserted before the confluence bonus so confluence can no longer reward same-cluster density. Verified against 7 scenarios (two same-cluster prints, cross-cluster pick both directions, paper-vs-live within and across clusters, single-member clusters passing through untouched, `simple_bias` handling in/out of learning mode) — all correct. New funnel tags: `blocked_cluster_sibling`, `blocked_cluster_paper`, `blocked_cluster_research`. Cross-bar cluster cooldown (proposed alongside the mutex) deliberately **not** included in this pass — deferred to avoid compounding two behavior changes in one sitting; worth a follow-up once the intra-bar lock's effect on fill mix is visible in the funnel.

Also shipped in the same commit: `blocked_vwap_noise` was wired only to the dead hard-veto branch (would have sat at 0 forever, same shape as the `blocked_htf` bug); added `soft_penalty_vwap_noise` on the actual live soft-penalty path. Extended the existing VWAP-proximity exemption (already live for `vwap_mean_revert`/`rsi_divergence`/`cvd_divergence`) to `rsi_extreme` — same reversion category, no reason found for its exclusion. `structure_bounce` deliberately left out of that exemption per the same holdout-before-plausibility-argument discipline.

## Score calibration (rank by EV, not weighted_score) — blocked, not started

Investigated before attempting. `bot/ev_engine.py`'s `EVEngine.compute_ev()` requires `MIN_SAMPLES = 20` historical trades per (scanner, regime) before it computes a real EV — below that it returns `verdict="INSUFFICIENT_DATA"` with **`ev=0.0` as a placeholder**, not a real estimate. Per the cluster performance data above, every live scanner except `structure_bounce` has near-zero published fills, so naively swapping the step-F ranking key from `weighted_score` to `ev` today would make nearly every candidate tie at a meaningless 0.0 and rank arbitrarily — actively worse for exactly the under-observed scanners the cluster mutex was just built to give a fair shot at being measured.

This needs a cold-start fix before it's safe to wire in — most directly, the shrinkage-prior idea already proposed alongside the calibration critique itself (blend a scanner's raw EV toward its cluster's mean EV, weighted by sample count, so a low-n scanner isn't stuck at a flat placeholder but also isn't trusted on 3 trades). That's a real modeling task, not a same-session patch — logged here rather than attempted blind.

## Regime-string vocabulary mismatch — partially fixed, one piece deliberately left open

Verified an external claim about the live regime detector against the actual code (`strategies/regime.py`, `config/constants.py`). Confirmed: this codebase has **two** regime classifiers with different vocabularies.

- **Primary** — `MarketRegimeDetector._classify()` — a 7-branch priority ladder, always returns one of exactly 7 `MarketRegime` enum values: `trending_up`, `trending_down`, `sideways`, `breakout`, `mean_reversion`, `high_volatility`, `low_liquidity`. HTF EMA(50) slope is computed and stored on `RegimeContext` but never passed into `_classify` — confirmed genuinely unused in the label, not just under-weighted.
- **Fallback** — `RegimeFilter.detect_regime()`'s own simple EMA+BB heuristic, used only when the primary detector throws (missing data, exceptions). Its own vocabulary had `volatile`/`quiet`/`ranging` — 3 of 5 possible outputs that don't exist in the primary detector's enum at all.

**Fixed** (commit `8e36ff2`): remapped the fallback to the real 7-value vocabulary (`volatile`→`high_volatility`, `quiet`/`ranging`→`sideways`), confirmed non-regressive since the routing table's dead/duplicate keys had identical-or-superset scanner lists to their real counterparts. Also fixed `_REGIME_ADJ`'s `liquidity_sweep`/`rsi_divergence`/`cvd_divergence`/`vwap_mean_revert` discount, which was keyed on the same dead `ranging`/`quiet` strings and had therefore never actually applied — rekeyed to `sideways`.

**Resolved as (c)** (commit pending in this session): three more "quiet"-only checks were dead for the identical reason —
- `_structural_prefilter`'s hard block: `if regime == "quiet" and atr_ratio < 0.4: return pass=False` (was scalp_strategy.py:919)
- The Indian-market-hours "quiet" scanner override (only fired `if regime in ("quiet",)`, was scalp_strategy.py:~2068)
- Veto 9's "REGIME MISMATCH... blocked in quiet market" — commented as an "absolute no-trade rule" for every non-`structure_bounce` scanner (was scalp_strategy.py:~3011)

All three assumed "quiet" is a *rare, more extreme* condition than ordinary chop. `sideways` is the detector's majority-of-bars default, so remapping these to `sideways` the way the routing tables were would have turned a rule meant to fire occasionally into one firing on most bars — effectively locking the live book down to `structure_bounce` during ordinary sideways markets, not just genuinely dead ones. Options were (a) give the primary detector back a real, distinct `QUIET` classification with a holdout-validated threshold, (b) accept the lockdown and remap anyway, or (c) delete the dead branches, since that's the only option that doesn't alter live occupancy. **Deleted all three** — each replaced with a comment pointing here, `regime_scanner_ok` (a variable only ever written, never read) removed along with its block, `low_liquidity`'s real hard-veto in `_structural_prefilter` left untouched and verified still present. Option (a) — a real QUIET classification — stays open for whoever wants to propose a holdout-validated threshold for it; this only closed off the dead, silently-never-firing version.

Related, lower-priority cleanup noticed during the same string audit: most *other* regime-keyed dicts in the codebase (`bot/ev_engine.py`'s `REGIME_EV_ADJUSTMENTS`, several checks in `bot/signal_tracker.py` and `dashboard/server.py`) already OR "sideways"/"high_volatility" together with the dead "ranging"/"volatile"/"quiet" strings, so they were already safe by accident — no behavior fix needed there, just harmless dead dict keys. Not touched.

## ADX thresholds inside `_classify` — the `28` literal is now named (see "Sign-off batch" below)

An external review of the same `MarketRegimeDetector._classify` ladder (2026-09-15), checked line-by-line against the code and confirmed accurate in every claim. Captured here as reference for whoever next touches regime thresholds, since none of this is visible from the code's own comments in one place.

The same 14-period Wilder ADX is tested against **three different, independently-hardcoded cutoffs**, not one canonical "trend threshold":

| Constant | Value | Where it fires |
|---|---:|---|
| `ADX_STRONG_TREND` | 40 | Branch 1 (high-vol override): `atr_percentile >= 85` and `ADX >= 40` still gets labeled `TRENDING_UP/DOWN` instead of `HIGH_VOLATILITY` |
| `ADX_TREND_THRESHOLD` | 30 | Branch 2 (squeeze-breakout): `bb_squeeze and ADX > 30` → `BREAKOUT` (0.70 conf). Also branch 4 (named trend): `ADX >= 30` AND `|EMA50 slope| >= 0.15%/3bars` AND DI agrees → `TRENDING_UP/DOWN` |
| *(unnamed, hardcoded)* | **28** | Branch 3 (expansion-breakout): `bw_percentile >= 92 and ADX > 28` → `BREAKOUT` (0.60 conf) |

The `28` is a bare literal in the code (`strategies/regime.py`, the `bw_percentile >= self.BB_EXPANSION_PERCENTILE and adx_val > 28` line) — not `self.ADX_TREND_THRESHOLD`, not its own named constant. Effect: a bar with ADX 29 and bandwidth at the 92nd percentile is called `BREAKOUT`; the same ADX 29 with ordinary bandwidth falls through to `SIDEWAYS`. If the intent is "one definition of directional," `28` and `30` should be the same constant — **not fixed here**: unifying them is a threshold change (it would reclassify some ADX 28-29 + wide-band bars from `BREAKOUT` to `SIDEWAYS`), and this session's whole pattern has been "no threshold change without a holdout" — matches the quiet-regime decision above, logged rather than guessed at.

Other verified, code-confirmed properties of the ADX ladder worth knowing before touching it:
- `_classify` never sees HTF slope — it's computed and stored on `RegimeContext.htf_trend_direction` but not passed into `_classify` at all (same finding as the earlier regime-vocabulary audit).
- No hysteresis: one 5m close crossing 30 can flip the whole regime label (and therefore the entire scanner roster) bar-to-bar. Only the latest ADX value is compared to a constant — Wilder's own "rising vs falling ADX" distinction isn't used.
- `+DI`/`−DI` set `trend_direction` for every regime, but only the high-vol-override branch and the named-trend branch actually consume it — `BREAKOUT` labels (branches 2 and 3) carry no side at all.
- A confirmed, narrow quirk: branch 1's `trend_dir >= 0 ? TRENDING_UP : TRENDING_DOWN` maps an exact `+DI == -DI` tie (`trend_dir == 0`) to `TRENDING_UP`, not "neutral" — harmless in practice (exact DI ties are rare) but worth knowing if this branch is ever revisited.

## Structure map prior — verified, not touched

Traced one level below the scanners themselves: `_scan_structure_bounce` and `_scan_liquidity_sweep` don't detect S/R independently — `structure_bounce` reads `self._structure_map.nearest_support`/`nearest_resistance` (built once per symbol per 5m close by `build_structure_map()`, `data/structure.py`), while `_scan_liquidity_sweep` builds its own, completely separate equal-high/low detection inline. Both get called "cluster" logic; they aren't the same algorithm, and this is a large part of why `structure_bounce` and `liquidity_sweep` can disagree about where "the" level is on the same bar. Verified line-by-line against the live file, including one empirical test. All of the below is read-only findings — nothing here has been changed.

**Three incompatible price-clusterers in the live path** (a `SCANNER_CLUSTER`/`_apply_cluster_mutex` grouping from commit `2c55c19` is a *different* thing — it groups scanners by thesis, never looks at prices, not relevant to this section):

| | Horizontal S/R (`find_horizontal_sr`) | Map liquidity (`find_liquidity_zones`) | Sweep trigger (`_scan_liquidity_sweep` step 1) |
|---|---|---|---|
| Input | 3-bar pivots only | same pivots | every raw high/low, no pivot filter |
| Window | last 100 bars | last 100 bars | last 50 bars excl. current, searches back 30 |
| Tolerance | `0.30×ATR` | `0.15%` of price | `0.25×ATR` |
| Grouping | sort by price, greedy merge, running-mean center | all pairs within tolerance | count of other bars within tolerance of *this* bar |
| Winner | every zone with ≥2 touches kept | every qualifying pair kept (duplicates) | newest (most recent) bar with a qualifying twin, then `break` |

Three unrelated units (`0.30×ATR` vs `0.15%` vs `0.25×ATR`) means no shared scale — as ATR% moves, which tolerance is "wider" changes. `find_horizontal_sr`/`find_liquidity_zones` only ever see 3-bar-pivot highs/lows (via `find_swings()`); the sweep scanner reads every raw bar in its window. A continuation high in a live grind isn't a pivot yet (needs a lower high after it to confirm) — so the sweep's raw-bar detector fires on it immediately while the S/R/liquidity generators haven't accepted it as a level yet, i.e. only *after* the bar that would've already stopped out a bounce fade of it.

**Proven dead or wrong objects** (confirmed via direct code read, one via a synthetic test):
- `StructureMap` (the dataclass, `data/structure.py:42-50`) has no `demand_zones`/`supply_zones` fields — only `levels`, `nearest_support`, `nearest_resistance`, and the vwap fields. `_scan_liquidity_sweep`'s "sweep into OB" bonus (`getattr(sm, 'demand_zones', [])` / `getattr(sm, 'supply_zones', [])`) can only ever see the empty-list default. Dead code — confirmed by grep, nothing else in the codebase sets those attributes.
- `find_liquidity_zones` enumerates **all pairs** of swing highs/lows within its 0.15% tolerance, not clusters — confirmed empirically: a synthetic test with 4 near-equal swing lows produced exactly 6 duplicate `StructureLevel` entries (C(4,2)=6) at the identical price/zone. `_scan_structure_bounce`'s multi-structure-confluence check (`nearby = [l for l in sm.levels if abs(l.price - target_level.price)/close < 0.003]`) then counts these near-duplicates as independent confirming levels — inflating the "Multi-structure confluence (N levels)" tag with what's actually one real cluster counted N times.
- Sweep's own equal-low search has a masked bug: `eq_low_level` starts at `0.0`, so `if touches >= 1 and (eq_low_level == 0 or l_val < eq_low_level): ...; break` breaks on the very first qualifying candidate (scanning newest-to-oldest) — the `l_val < eq_low_level` half of that condition can never actually matter, since the loop always exits before a second candidate could be compared. It's "newest low with a twin," not "the lowest/most significant equal-low," despite the code's shape suggesting otherwise.

**Proven mix that `structure_bounce` actually trades against:**
- The map is built from `confirm_df` (the 15m confirmation frame, `self.confirm_tf` default `"15m"`) whenever ≥50 bars of it exist, falling back to the 5m frame only when it doesn't (`strategies/scalp_strategy.py:1577`) — while the bounce scanner's own wick/body/close-position math is scored entirely on 5m bars. A 15m-built zone is wide in 5m-ATR terms, which makes the scanner's `in_zone` check easy to satisfy.
- `nearest_support`/`nearest_resistance` are chosen by **raw distance to current price only** — `build_structure_map` sorts all levels by `|price − close|` and takes the first support/resistance in that sorted list. Strength, touch count, and level type are never part of the selection; they only show up afterward as a capped `+15` scoring bonus and a label in the confirmation string.
- `_scan_structure_bounce` explicitly excludes `order_block`-type levels from being its S/R (`level_type != "order_block"`, present on this checkout — confirmed real, though noted as absent from an earlier reference snapshot) but has **no equivalent exclusion for `vwap_band`**. Only ±1σ VWAP bands are ever inserted as levels (2σ is computed and discarded); a bounce can therefore fade VWAP±1σ as its "support"/"resistance" while still eating the separate VWAP noise-zone −20/−25 soft penalty on the same signal — `vwap_mean_revert`/`rsi_divergence`/`cvd_divergence`/`rsi_extreme` are all exempted from that penalty (see the regime-vocabulary section above), `structure_bounce` is not.

**Explicitly not touched this session, with reasons** (same discipline as the quiet-regime and EV-ranking items above — each of these is a real behavior change, not a mechanical fix):
- Deleting sweep's dead `demand_zones`/`supply_zones` getattr bonus would be occupancy-neutral (it never fired) — safe whenever someone wants to do routine cleanup, just not bundled into this pass.
- ~~Deduplicating `find_liquidity_zones`'s pairwise output~~ — **shipped, with sign-off** (see "Sign-off batch" below). A before/after comparison of confluence-bonus effect on live fills is still outstanding — this fixed the generator, it didn't measure the downstream scoring shift.
- Excluding `vwap_band` from bounce the way `order_block` already is: a real product decision, not a bug fix — either VWAP is a valid S/R type for this scanner (in which case it shouldn't also eat the noise-zone penalty) or it isn't (in which case exclude it like OB). The current state — exemption for four scanners but not this one, while the map still offers VWAP as a nearest-level candidate to the one scanner not exempted — is the actual contradiction, and either resolution changes what the flagship scanner does.
- Unifying the 15m-map-vs-5m-wick mismatch is a holdout-sized question (does forcing both onto one timeframe change fill rate or quality?), not a same-sitting change.

**What to measure on bounce's own fills before touching any generator** (mirrors the cluster-mutex "let it run and watch the counters" discipline above): `level_type` of `nearest_support`/`nearest_resistance` at signal time (`sr` / `order_block` / `liquidity` / `vwap_band`), `touch_count`, `last_touch_bars_ago`, whether `struct_source` was actually the 15m frame or the 5m fallback, and distance to the strongest level within 1 ATR (to see what swapping nearest-pick for max-strength-pick would have changed). Until that table exists, the honest description of the live edge is "fade whatever `build_structure_map` calls nearest on the confirmation frame" — the scanner's own checklist (wick %, body %, volume) decorates that prior; it doesn't independently establish the level.

## Joint bar log — shipped, running (`b48d9d9`, `87fe22a`, `fbafbcd`)

The instrument for steal rate, print rate, level-type select rate, and whether `blocked_cluster_sibling` actually fires: one JSONL row per symbol-bar to `storage/joint_bar_log.jsonl` (gitignored) whenever `structure_bounce`/`liquidity_sweep` were eligible to run, written from `scan_results` at the exact point step F resolves — before the confluence bonus can still mutate whichever candidate survived the cluster mutex. Read-only: no scoring, routing, or threshold changed to add it.

**Score-at-F is provably clean**, not just empirically clean so far: bounce and sweep share the `"structure"` cluster, and `_apply_cluster_mutex` (which runs *before* confluence) collapses same-cluster candidates to one survivor via `max(pool, key=weighted_score)` — so the two can never both still be in `tradeable` when confluence's asymmetric boost (bounce capped at `min(8, bonus)`, sweep uncapped) would apply. The mutex has already decided between them using the same unmutated scores the log reads. Separately, the post-F soft vetoes (VWAP penalty, the 15m EMA21 −12) mutate `best`/`best.confidence` — a different variable, built only for whichever scanner wins the *whole* bar, populated well after the log call — with no path back to the `ScanResult` objects the logger reads. Both fields are structurally immune to post-F contamination.

`weighted_score` can and does exceed 100 (seen live: 84 and 101) — that's expected. The scanner's own `confidence = max(min(score,100),0)` cap only applies to the scanner's internal 0–100 checklist output; `weighted_score` is a separate, uncapped value built by the outer scanner-normalization/confluence layer, and F ranks on `weighted_score`, not the capped `confidence`. Log both values as-is; the >100 reading is not a bug to fix.

**The corrected EMA21-slope formula for bounce** (replaces any earlier "sign(slope)×10" shorthand — verified against `strategies/scalp_strategy.py:5481`):

```
s_ATR = (EMA21[t] - EMA21[t-20]) / ATR      # same frame throughout — 15m if that
                                              # attempt triggered, 5m fallback if not
Δ = +10   if s_ATR >  1.0  and slope direction agrees with the trade side
    -10   if s_ATR < -1.0  and slope direction opposes the trade side
     0    if |s_ATR| <= 1.0   (dead zone)
```

20 bars is a slow filter: ≈5h of drift if the 15m confirm attempt is what triggered, ≈100min if the 5m fallback did. Most sideways-regime bars should sit in the dead zone, so bounce's F score usually carries no slope term at all — sweep, by contrast, has no equivalent gate on its own HTF ±15/−5.

**Production readout so far** (small n, reported honestly, not as a result): as of the last check, 120+ bars scanned, `structure_bounce` silent every single time so far, several `liquidity_sweep` prints across `eqh`/`eql`/`roll_high` sources, zero bars with both bounce and sweep printed. Consistent with "980:19 is occupancy, not F-theft," but nowhere near enough bars to call it. **Not touching** `0.25×ATR` (sweep tolerance), the `55%` body gate, or the `1.0 ATR` slope threshold on this sample — the log is doing its job; watching for: the first `bounce_printed` row (any source), then the first bar with both printed, the distribution of `ema21_slope_adj` on bounce-printed rows (expecting mostly `0`s), and the `sweep_source` mix over a larger sample.

**Known imprecision, found live**: `blocked_cluster_sibling` on the joint-bar row is a *global* funnel-counter delta ("did the mutex suppress a same-cluster sibling anywhere this bar"), not specifically "did bounce and sweep collide." The `"structure"` cluster has four members (`structure_bounce`, `liquidity_sweep`, `bos_choch`, `order_block_entry`) — a `bos_choch`-vs-`order_block_entry` (or either against sweep) collision would also set this field to `true` on a row where bounce/sweep themselves never printed. Observed exactly this in production: `blocked_cluster_sibling=true` fired 3 times with 0 bars showing both bounce and sweep printed, so those 3 events are almost certainly other structure-cluster pairs, not the bounce-vs-sweep contest this instrument exists to measure. A precise version would need the mutex itself to tag *which* scanner names collided, not just increment a count — not built yet. Until then, a non-zero `blocked_cluster_sibling` on a row where `bounce_printed` and `sweep_printed` aren't both `true` should be read as "some structure-cluster collision happened, not necessarily this one."

## Reversion scanner membership — unified (see "Sign-off batch" below)

A real product inconsistency, not a style nit — found while verifying Veto 10 (`strategies/scalp_strategy.py:3164`, the post-F "block counter-trend for momentum scanners" rule) against the code. There are three independently-declared "reversion scanners" tuples, two identical and one different:

| Gate | Members | Effect on `rsi_extreme` |
|---|---|---|
| Veto 10 (`_reversion_scanners`, line 3167) | `rsi_divergence`, `cvd_divergence`, `vwap_mean_revert` | treated as momentum → hard-blocked counter-trend in `trending_up`/`trending_down` like any other non-reversion scanner |
| P0.8 HTF-hard-veto exemption (`_reversion_scanners_p08`, line 3303) | same 3, declared separately | same — momentum |
| VWAP noise-zone penalty exemption (`_reversion_names`/`_reversion_conf_names`, this session's fix, lines 2506/2619) | those 3 **+ `rsi_extreme`** | exempt — skips the −20/−25 |

So `rsi_extreme` is reversion for one mechanism and momentum for two others, depending on which of three near-duplicate tuples happens to gate that particular check. **Not merged in this pass** — unifying them changes how often Veto 10 and the P0.8 HTF-hard-veto actually fire for `rsi_extreme` (more or fewer hard blocks), a real behavior change that doesn't belong in the same patch as documenting the inconsistency or as the joint-bar logging work. Whoever picks this up next should decide the correct membership once (probably: all four, or introduce one shared constant), then thread it through all three gates in one deliberate change — not fix it as a side effect of something else.

## `veto10_fired` on the joint-bar row — shipped (`981e815`)

Shipped via the build/write split, exactly as scoped below (kept for the design record):

- The log write is **not** after the veto-classification loop — it's a build-then-write split: the row dict is built at the existing hook (right after cluster mutex) with `veto10_fired: null` and held as `self._pending_joint_bar_row`, then flushed (written exactly once) at whichever comes first — one of the early-return sites before the classification loop (stays `null` — genuinely unknown, never `false`) or the classification loop itself (set `true` if `"REGIME SIDE:"` was appended to `vetos` for this bar's winner, `false` if the loop ran and it wasn't).
- Implemented via the surgical option: 9 individually-instrumented early-return sites (cluster-mutex-empty, zero-confidence, investment-block, empty-regime, weak-setup-veto, shadow-mode, ema_momentum-short, scanner-cooldown, side-flip-cooldown) each call `self._flush_pending_joint_bar_row()`, plus the classification-loop flush itself — not the `try/finally`-wrap alternative, which was rejected as too risky given the nested try/excepts already inside that ~950-line stretch.
- Running in production now. Target cell once enough rows accumulate: `sideways + eqh short + veto10_fired=false` vs. `trending_up + eqh short + veto10_fired=true` — the split that actually isolates what Veto 10 does and doesn't own.

## Dead sweep OB-zone bonus removed (`890cbb9`)

`_scan_liquidity_sweep`'s step 6 read `self._structure_map.demand_zones`/`supply_zones` via `getattr(sm, ..., [])` for a scoring bonus — `StructureMap` has no such fields (confirmed in `data/structure.py`), so `getattr` always returned `[]` and the bonus never fired. Deleted the dead branch and the now-unused `sm = self._structure_map` line (confirmed no other use in the function). Occupancy-neutral — this code never executed.

## Stale comments and docstring corrected (`890cbb9`)

`bot/signal_tracker.py` had comments/messages describing a partial-profit ladder that no longer matches the code: `# Book partial profit: 60% of position at TP1` → actual is 35% (fixed comment + the `"60% booked"` message string); `# Book 25% partial at TP2` → actual is 35% (fixed comment + message string, comment now also notes the 65%-35%=30% runner remainder). `TrackedSignal.from_signal`'s docstring described the old "Fixed Fractional Risk Model (Phase 2)"; rewritten to describe the current $100-margin/30x-leverage model actually in use. Documentation-only, no behavior change.

## `fill_price_captured` bug fix + 5 stale test rewrites (`2fbb685`)

Found while chasing what looked like test drift in `TestTP1TrailExit`, turned out to be a real bug: the one-time estimated-slippage capture in `_update_prices_inner()` (`bot/signal_tracker.py`) was gated on `ts.fill_price == ts.signal_price`, which is true both "capture hasn't run yet" and "capture ran and slippage was exactly zero" — a trade in the latter state could have the block re-fire on a later, unrelated price tick and misattribute that move as entry slippage, shifting `entry_price`/TP levels mid-trade. Fixed with an explicit `fill_price_captured: bool` field (defaults `False`, set `True` right after either capture branch), with a `from_dict()` backfill (`True` if absent) so trades persisted before this field existed don't spuriously re-trigger the capture on a warm restart. This is a real behavior fix, not mechanical — flagging it here even though it was implemented, since it changes a live code path's semantics rather than just tests/docs.

Also rewrote 5 tests that asserted pre-2026-09-12 (pre-HOLD-profile) behavior:
- `test_long_tp1_trail_exit` / `test_short_tp1_trail_exit`: hardcoded stale TP1 price targets and used the minimal `_make_tracker()` helper (lacks attributes needed on the full close path). Now read `ts.tp1` dynamically (with a sanity assertion `ts.tp1 > ts.entry_price`) and use `_make_full_tracker()`, so they stay correct across future R-multiple-ladder changes instead of going stale again.
- `test_early_kill_scalp` / `test_momentum_kill_intraday`: asserted an early/momentum kill fires; `early_kill_sec`/`no_momentum_sec` are 0 (disabled) for every trade type under the current HOLD profile, so neither ever fires. Rewritten to assert the position stays active — this is intentional current behavior per the 2026-09-12 HOLD-profile decision, not something to silently re-enable.
- `test_classify_intraday_mid_ml` — see the new finding below; assertion updated to match current `classify_trade()` output, not silently left broken or silently "fixed" by changing the threshold.

Suite: 320 passed/16 failed → 325 passed/11 failed. Remaining 11 are pre-existing, unrelated to this thread (API auth-mode assertions in `test_api.py`, one RSI-all-up NaN edge case in `test_indicators.py`).

## New finding: RUNNER upgrade gate may be swallowing the INTRADAY tier — needs a decision

`classify_trade()` (`bot/signal_tracker.py`) buckets by `ml_prob` first (`<0.50`→SCALP, `0.50–0.649`→INTRADAY, `≥0.65`→RUNNER), then has an upgrade rule: if the base bucket is INTRADAY and `regime` is trending and HTF is aligned and `vwap_zone == "clear"`, it upgrades to RUNNER **as long as `ml_prob >= 0.50`** — i.e. the upgrade's own ml-prob gate is the same threshold that put it in INTRADAY to begin with. The practical effect: under trending + HTF-aligned + clear-VWAP, there is no `ml_prob` value that lands as INTRADAY — the entire 0.50–0.649 band gets swept into RUNNER too, and only `ml_prob < 0.50` (SCALP) or `is_ranging`/HTF-not-aligned/VWAP-not-clear keeps a signal out of RUNNER in that regime.

Found because `tests/test_integration.py::TestTradeClassification::test_classify_intraday_mid_ml` (`ml_probability=0.55, regime="trending_up", htf_bias=1`, default `vwap_zone="clear"`) has been asserting `TRADE_TYPE_INTRADAY` and has actually been returning `TRADE_TYPE_RUNNER` since at least the 2026-09-12 HOLD-profile change — the test was silently wrong, not the code. I updated the assertion to match current behavior (RUNNER) rather than change the threshold, per the standing rule not to touch scoring/routing/thresholds without sign-off.

**This needs a decision, not a silent fix either way:**
- If the RUNNER-upgrade's intent was "mid-tier ML gets held to RUNNER's longer horizon too, when context is this strongly trend-aligned" — current behavior is correct, and `test_classify_intraday_mid_ml`'s original intent (a distinct "stays INTRADAY" case) should probably be re-pointed at different inputs (e.g. `htf_bias=0` or `vwap_zone="noise"`) to keep exercising that path, or retired.
- If the intent was "only ML ≥0.65 gets pulled into RUNNER by trend context, INTRADAY should still exist as a real 0.50–0.649 outcome in trending markets" — the upgrade gate's `ml_prob >= 0.50` should be `>= 0.65` (or some other value above the INTRADAY floor), which is a real threshold change requiring the usual sign-off/holdout.

Not touched pending that call.

## Sign-off batch (`39916b1`) — resolved the RUNNER-gate question + 3 more items

User reviewed all four open items above and confirmed a decision on each before any code changed:

- **RUNNER upgrade gate — resolved as intentional, not a bug.** The inner `ml_prob >= 0.50` check was dead code (unreachable — `trade_type == TRADE_TYPE_INTRADAY` already guarantees it), but the upgrade rule itself mirrors the SCALP→INTRADAY upgrade a few lines below (which also fires below its target tier's own floor, at `ml_prob >= 0.40` vs. INTRADAY's 0.50 floor) — a deliberate "trend context compensates for a lower ML score" pattern applied consistently at both tiers, not an accidental threshold collision. Deleted the dead branch (cosmetic only, no behavior change). `test_classify_intraday_mid_ml` re-pointed at `htf_bias=0` (not aligned), which genuinely stays INTRADAY, instead of the `htf_bias=1` combo that the upgrade rule correctly claims.
- **Reversion-scanner unification — shipped.** All three tuples (Veto 10's `_reversion_scanners`, P0.8's `_reversion_scanners_p08`, the VWAP-exemption's `_reversion_names`/`_reversion_conf_names`) replaced with one module-level `_REVERSION_SCANNERS = ("rsi_divergence", "cvd_divergence", "vwap_mean_revert", "rsi_extreme")`. Real behavior change: `rsi_extreme` counter-trend trades now get Veto 10's soft penalty instead of the hard block, and the P0.8 HTF-hard-veto exemption — bringing it in line with the VWAP noise-zone exemption it already had.
- **ADX `28` literal — named only, value untouched.** `strategies/regime.py`'s bare `28` at the expansion-breakout branch is now `ADX_EXPANSION_BREAKOUT_THRESHOLD`. Deliberately not unified with `ADX_TREND_THRESHOLD` (30) — user confirmed cosmetic-only for this pass; the value question still needs a holdout.
- **Liquidity-zone dedup — shipped.** `find_liquidity_zones` replaced its all-pairs double loop with `_cluster_equal_pivots`, a greedy percentage-tolerance merge (same style as `find_horizontal_sr`'s ATR-based merge). Verified with a synthetic 4-near-equal-low test: 1 `StructureLevel` with `touch_count=4`, where the old code produced 6 (`C(4,2)`) duplicates at the same price/zone. User confirmed this despite it being a real scoring change (removes confluence-bonus inflation in `_scan_structure_bounce`).
- **vwap_band exclusion — deliberately deferred, not part of this batch.** Checked `storage/joint_bar_log.jsonl` for enough data to decide empirically (the way `order_block`'s exclusion was data-backed, 41% WR / -1.05 ATR over 335 setups) — only 54 rows exist, 0 with `bounce_printed=true`. Revisit once the log has real `vwap_band` samples.

Suite after this batch: 325 passed / 11 failed, unchanged from before (no regressions).

## Scanner print-rate deep dive + routing-table audit (2026-09-15/16, `9ea0c8d`)

**Scope note:** everything below is a *print-rate* map — bars a scanner was on the roster (`allowed_scanners`) vs. bars its function returned a setup (`ScanResult.setup_result is not None`). It says nothing about F wins, cluster-mutex steals, Veto 10, or fills — those are separate, downstream layers not covered by `storage/research/scanner_funnel.jsonl`. Do not reconcile these print counts against `storage/joint_bar_log.jsonl` fill counts without the pass-F/veto columns in between.

**Per-scanner print rate, 14 routed scanners, up to 7782 sampled bars** (`triggered`/`total`): liquidity_sweep 1075/7782 (13.8%), structure_bounce 955/7782 (12.3%), cvd_divergence 873/5231 (16.7%), rsi_divergence 777/4714 (16.5%), vwap_mean_revert 374/5231 (7.1%), rsi_extreme 265/4714 (5.6%), bb_squeeze 221/3919 (5.6%), candlestick_reversal 67/788 (8.5%), volume_surge 31/424 (7.3%), trend_continuation 17/751 (2.3%), bos_choch 15/7004 (0.2%), post_impulse 13/104 (12.5%), order_block_entry 3/6473 (0.05%), ema_momentum 0/751 (0%).

**Roster (`REGIME_SCANNER_ROUTING`, lines 2095-2198) audited against the live dict, not docs — zero undocumented drift found.** `structure_bounce`/`liquidity_sweep` are on all 9 non-empty regimes (explains their 7782 ceiling); `ranging`/`sideways` are literal copies of each other, as are `volatile`/`high_volatility`; `mean_reversion` carries only sweep+bounce; `quiet` carries sweep+bounce+rsi_extreme+candlestick_reversal; `low_liquidity` is `[]` (no trading). `post_impulse` is `trending_up`-only (1 of 9 regimes) — its small n=104 is a roster fact, not a gate finding. The only names not on this table at all (`vwap_bounce`, `supertrend_flip`, `momentum_ride`, `bb_band_walk`, `momentum_surge`, `simple_bias` except as a learning-mode extra) are reachable only via an enforced `regime_whitelist` variant, never by default.

**Overlay chain confirmed exactly**: `REGIME_SCANNER_ROUTING.get(regime, [])` → `_apply_regime_whitelist_variants` (lines 1297-1396, reads `storage/research/scanner_variants.json`, additive-only, `enforce` mode appends + logs `variant_enforced`, `shadow` mode logs `variant_would_fire` only, never mutates the list) → learning-mode extras (`liquidity_sweep`+`simple_bias` appended only if the list is already non-empty) → if still empty, `REGIME VETO` and `return []`. **`storage/research/scanner_variants.json` does not currently exist on disk** — the overlay is a no-op right now; the raw dict above *is* the live roster.

**The old "Indian Market Regime Override" is not merely dead — it no longer exists in the file.** Already deleted in `e4c41fd` (this session, same day): the guard was `if indian_ctx and indian_ctx.regime_override == "ranging_limited" and not allowed_scanners: if regime in ("quiet",):` — unreachable since `quiet`'s routing list is never empty. Fully removed, not just inert.

**Per-scanner findings, ranked by what's actionable:**

- **`bos_choch` (15/7004, 0.2%) — genuine two-candle confirmation chain, confirmed via exact line read, not a duplicate check.** After the break candle (`bar[-3]`) clears displacement `> 0.6×ATR` (the scanner's own log text said `> 0.4×ATR` — wrong, now fixed to `0.6×` in `9ea0c8d`), TWO separate consecutive candles must each independently clear a `body_ratio >= 0.55` in-direction test: `confirm_bar` at `df.iloc[-2]` (lines ~5980-5998) and, separately, `entry_bar` at `df.iloc[-1]` (the "MSS Confirmation Gate," lines 6062-6079). A code comment at 6064-6066 confirms this second gate used to `NameError` on every call (100% silent failure) before being patched — the patch fixed the crash but left the double-gate. **Open decision, not resolved**: keep the two-candle requirement (deliberate ICT-style confirmation) or drop the second gate as leftover over-caution from the NameError-era patch. Either way needs a holdout, not a guess.
- **`order_block_entry` (3/6473, 0.05%) — earlier TF-mismatch theory retracted.** The structure map (containing order-block zones) is built from `confirm_df` (`self.confirm_tf`, default `"15m"`), and the scan loop tries `confirm_df` FIRST for every scanner (line ~2318, "Try 15m FIRST for ALL scanners") before falling back to the primary/5m frame — so the primary attempt runs on the *same* 15m frame the zones were built from, not a mismatched one. The near-zero rate is fully explained by `detect_order_blocks()` (`data/structure.py`): a 1.5×ATR impulse gate to form a zone, plus an **unbounded, permanent** mitigation check (any wick, at any later bar, invalidates the zone forever) — a zone-lifetime policy question, not a missing-print bug. Do not shrink the 1.5×ATR gate to "make it fire" without first measuring mitigation-kill rate vs. proximity-miss rate vs. any real TF issue.
- **`ema_momentum` (0/751, 0%) — compounding AND-chain + a deliberate hard SHORT block.** 5 largely-independent conditions (rare 15-bar EMA cross, tight 0.6×ATR pullback proximity, an exact single-bar RSI turning-point, directional trigger candle, `rel_vol>=0.7`) stacked on top of a hard-coded line ("DATA: ema_momentum SHORT = 0% WR — block bearish entirely") that removes roughly half of all eligible crosses outright, routed to only 3 of 9 regimes. No unreachable/always-false bug found — every branch is individually satisfiable, comments show devs already progressively relaxed thresholds and still hit zero. **Open decision**: quarantine at import like the six unrouted research scanners, or accept a structurally-expected zero. Turning shorts back on is a new strategy, not a fix.
- **`structure_bounce`'s 07:00-15:00 UTC session window** (lines 5412-5419) — confirmed inside the scanner function itself, before any trigger logic, so it correctly belongs in this print-rate table (not a post-F veto living elsewhere).
- **Fixed, occupancy-neutral (shipped in `9ea0c8d`)**: the `scanner_funnel.jsonl` "reason" text comes from a separate, single-bar shadow-diagnostic block (~lines 1940-2075) that had drifted from the real per-scanner code in two confirmed cases — `trend_continuation`'s diagnostic asserted an RSI 40-58/42-60 gate that **does not exist anywhere in the real scanner** (verified full read: the real gate is EMA8/21 direction + 0.01% gap, then an 8-bar impulse→pullback→trigger sequence with no RSI check at all), and `rsi_divergence`'s diagnostic quoted `<40`/`>60` cutoffs against a real gate of `rsi_now<52`/`>48` with the swing's own RSI past `42`/`55` and a 4+pt gap. Both corrected to match the real code. `vwap_mean_revert` had no diagnostic entry at all (logged `""` for every non-trigger, including its silent stochastic/OBV veto) — added one. Verified this whole diagnostic dict only feeds near-miss dashboard tiles and funnel bookkeeping, never `scan_results.setup_result` — none of this touched step F, cluster mutex, or trade selection.

**Two decisions still open, need explicit sign-off before either is touched**: (1) `bos_choch`'s second 55%-body confirmation gate — keep or drop; (2) `ema_momentum`'s zero-print status — quarantine or accept. `vwap_band` exclusion from `structure_bounce` (carried over from the prior section) remains blocked on sample size — still only 54 joint-bar-log rows as of last check.

## Full forming→entry→exit walkthrough, all 14 scanners (2026-09-16)

Companion to the print-rate table above: for each routed scanner, what has to build up before it prints ("forming"), the exact trigger/entry-price/stop-loss formula ("entry"), and how it exits. The exit mechanism turned out to be **identical for 13 of 14 scanners** and is documented once rather than per-scanner:

- `entry_price`/`stop_loss` in the *raw* `_SetupResult` a scanner returns are **not** what ends up on the trade — see the `_scanner_sl_tp` correction below. The scanner's own SL is only the "structure" candidate fed into `_build_signal`.
- `_build_signal` (lines ~7470-7620) computes `sl_dist = max(structure_component, ATR × per-scanner sl_atr × self._sl_adjust)`, then **clamps to [`self.min_sl_pct`, `self.max_sl_pct`] = [0.55%, 0.95%] of entry price**, plus a 0.1% slippage buffer. Every scanner's real stop distance lands in that band regardless of how tight or wide its own raw formula looked.
- `trade_type = classify_trade(sig)` (ML-probability tiers + context upgrades) decides TP1/TP2/TP3 as `entry ± risk_dist × TRADE_TYPE_CONFIG[trade_type]["tpN_rr"]` — **unless** `SCANNER_TRADE_TYPE` overrides it. Today that override list has exactly one entry: `structure_bounce → RUNNER`, unconditional.
- Partial exits (35%/35%/30% at TP1/TP2/trail), the chandelier trail (active once MFE≥0.3R), and the 8h max-age ladder are shared infrastructure, not per-scanner.

**Correction — `_scanner_sl_tp` is not unused, contrary to the premise raised for it.** It has two live, real consumers: (1) the fee-viability pre-gate (~line 3823) that hard-blocks trade *creation* if the scanner's calibrated `tp1_rr` implies too small an expected move relative to fees, and (2) the ATR-volatility-floor term inside `_build_signal`'s SL clamp described above. Deleting it, as originally proposed, would have silently changed every scanner's real stop-loss distance and removed a live risk gate — not done. What *was* confirmed dead and removed: 4 `signal.metadata` fields (`scanner_sl_atr`/`tp1_rr`/`tp2_rr`/`tp3_rr`) that displayed these calibrated values as if they were the trade's real exit levels — repo-wide grep confirmed nothing ever read them back, `TrackedSignal.from_signal()` always recomputes TP1/TP2/TP3 from `TRADE_TYPE_CONFIG` instead.

**Per-scanner forming→entry, condensed** (full prose version was posted to chat 2026-09-16, not duplicated here in full):

- **structure_bounce** (forced RUNNER): 07:00-15:00 UTC hard window → rejection wick at nearest S/R (excludes `order_block`, still allows `vwap_band` — unresolved) → confirmation candle → silent stoch/OBV veto (now has a diagnostic string, see below). SHORT disabled by default. `entry=close`.
- **liquidity_sweep**: equal-high/low or rolling-fallback sweep+reclaim → hard gate `body_ratio≥0.55`. `entry=close`, `SL=wick∓0.15×ATR` (tightest raw SL of any scanner, though still clamped to 0.55%-0.95% downstream).
- **bos_choch**: 18-bar range break, displacement>0.6×ATR, **two separate consecutive candles** each needing body≥55% in-direction (confirm bar at `[-2]`, a second "MSS" check at `[-1]` — confirmed genuinely two different bars, not a duplicate).
- **order_block_entry**: unbounded-lifetime OB zone (1.5×ATR impulse to form, any later wick mitigates forever) → **`entry_price = zone midpoint`, the only scanner that isn't a market/close entry.**
- **rsi_divergence** / **cvd_divergence**: swing-extreme + RSI or CVD-proxy divergence, standard routing.
- **vwap_mean_revert** / **rsi_extreme**: band/RSI extreme + reversal candle; `rsi_extreme`'s docstring said `<30`/`>70`, code gates at `<35`/`>65` — fixed.
- **ema_momentum** (0% print): EMA cross → pullback → RSI turn → volume, **but bearish crosses are hard-blocked outright**, halving eligible events before the rest of the 5-condition chain even runs.
- **trend_continuation** / **bb_squeeze** / **volume_surge** / **post_impulse**: impulse/squeeze/breakout + volume gates, standard routing; `post_impulse` LONG-only, `trending_up`-only.
- **candlestick_reversal**: 4 exclusive patterns at a 10-bar swing extreme, standard routing.

## Mechanical fixes shipped (`b9a6847`, 2026-09-16)

- `rsi_extreme` docstring corrected (`<30`/`>70` → `<35`/`>65`, matching code).
- `_build_signal`'s docstring corrected (stale "0.4-1.2%" SL clamp comment → real `0.55%/0.95%`).
- `structure_bounce` given a real shadow-diagnostic entry (previously logged `""` for every non-trigger) — reports outside-session-window status and the stoch/OBV veto, same pattern as `vwap_mean_revert`'s fix in `9ea0c8d`.
- `liquidity_sweep`: deleted a confirmed-unreachable dead branch (`sweep_depth<0.35 and body_ratio<0.55` — the hard gate above it already returns `None` whenever `body_ratio<0.55`, so this could never be true).
- `liquidity_sweep`: removed **double-counted** `rel_vol` scoring — the eq-high/eq-low branch scored it once, an unconditional Step 5 scored the same value again for every path (including the rolling-fallback branch, which never got the first score at all). Left Step 5 as the single, uniform scoring point. **Flagged explicitly**: this is a real, if modest, confidence reduction for eq-based sweep signals specifically — not dead code, a scoring change, done on explicit instruction.
- Removed the 4 write-only `signal.metadata` fields described in the correction above.

Suite: 325 passed / 11 failed, unchanged. Bot restarted clean.

## `classify_trade()` exit tiers, the 4-layer fee gate, and the P3.11/P3.7 correction (2026-09-16)

**Correction to a prior turn's claim: `_scanner_sl_tp` is confirmed correctly held, not deleted** — reaffirmed by a second, independent pass. The 0.55%-0.95% clamp in `_build_signal` is the real risk band; raw scanner SL formulas (sweep's 0.15×ATR, bounce's `min(2×ATR, 0.5%)`) are only inputs to `max(structure, volatility)` before that clamp applies. "Tiny SL" concerns need post-clamp `sl_pct` logged per ticket before they're a real finding, not the raw formula read in isolation.

**The fee gate is 4 layers deep, not 2** (all inside `TrackedSignal.from_signal`, `bot/signal_tracker.py`) — the earlier writeup only had the first two:
1. `fee_drag_r > 0.8` → hard block, `confidence=0` (line ~602).
2. `fee_drag_r >= 0.6` (`not fee_check["viable"]`) → soft block, `confidence=0` (line ~613).
3. **P4 hotfix** (line ~626): `fee_drag_r > 0.30` AND `trade_type in (SCALP, INTRADAY)` AND `regime in (high_volatility, sideways, ranging, quiet)` → block. This is the one firing live in the current bot log ("FEE BLOCK P4... fee_drag=0.49R (>0.30)").
4. **FIX B universal cap** (line ~661): `fee_drag_r > 0.50`, any trade_type, any regime → block. Added after RUNNER trades were found bypassing layer 3 entirely (P4 only covers SCALP/INTRADAY).

Given `liquidity_sweep`'s tight raw SL (0.15×ATR, though clamped same as everyone else), layers 3/4 are a real candidate for why its 13.8% print rate doesn't translate to a proportional fill rate — flagged as a slice to check in the print→F→fee_drag→fill join, not yet measured.

**`classify_trade()` base tiers** (`bot/signal_tracker.py:127-132`, confirmed): `ml_prob≥0.65`→RUNNER (1.5/3.0/5.0R), `≥0.50`→INTRADAY (1.2/2.0/3.0R), else SCALP (0.8/1.2R, no TP3). Default on a missing `ml_probability` key is `0.5` → lands in INTRADAY, indistinguishable from a real mid-confidence model output. `SCANNER_TRADE_TYPE["structure_bounce"]=RUNNER` overrides this unconditionally for bounce only, bypassing both the 0.65 cut and the ranging-regime RUNNER→INTRADAY chop downgrade that applies to every other scanner.

**P3.11 / P3.7 — confirmed not ML gates, matching a self-documented in-code audit** (`strategies/scalp_strategy.py:3396-3521`), same treatment as the `_scanner_sl_tp` correction:
- `_ml_prob_p311 = 0.0` (line 3419) is a bare literal — never read from `best`, `indicators`, or anywhere. The code's own 2026-09-11 comment already says why: *"_SetupResult has neither ml_probability nor grade, and ML is scored further down the pipeline... both escapes were inert."* Confirmed, not newly discovered.
- **P3.11** therefore always evaluates its `ml_prob<0.55` term as true — it's really `chop-regime(high_vol/mean_rev/sideways) + LONG + htf_bias≤0 + not structure_bounce`, no ML content despite the name. Its own header comment claims an "A+ grade escape" that a later audit note *in the same comment block* says was tested and removed for being the worst-performing subgroup — the header is stale, the code (no grade term in the boolean) matches the removal.
- **P3.7** is confirmed permanently dead by a self-contradiction, not an accident: `_p37_sideways_trap` requires `is_sb` (line 3475), but the firing condition at line 3509 is `if _p37_sideways_trap and not _is_sb_setup:` — the same fact checked twice with opposite polarity, so it can never fire. Matches its own comment: "Disabled."
- **Open decision, not implemented**: stamp `ml_probability` before this point in the pipeline so the term means something, or delete the dead clause and rename P3.11 for what it actually gates. Not a threshold question — a stamp-or-don't-read question. Holding per explicit instruction.

**Next real deliverable, not computed yet**: a reliability table (Brier score, ECE via equal-mass bins, mean-`p`-vs-WR-vs-mean-R per bin) on closed bounce fills, using the `p` actually stamped at signal time, with `ABSTAIN`/missing/injected-0.5 rows dropped. `y` defined as net-R-after-fees, not TP1-hit. Needs ~200 scored fills per slice (scanner×regime×side) to be meaningful — bounce is the only scanner with a plausible shot at that count soon; sweep's ~19 fills is not a curve. Until this exists, 0.50/0.65 are uncalibrated policy, not probabilities, and are not being retuned.

## P3.11/P3.7 resolved (`f77c6b6`, 2026-09-16)

Explicit decision on the stamp-vs-rename question above: **P3.7 deleted entirely** (permanently-dead self-contradiction, landmine risk if someone "fixed" one of the two `is_sb` checks independently). **P3.11 renamed, not stamped** — `_ml_prob_p311` (the hardcoded `0.0` literal) and its dead `<0.55` compare are gone; the veto string/hotfix metric renamed `P3.11 CHOP LONG HTF`/`p3_11_chop_long_htf` (was `CHOP LONG TRAP`/`p3_11_chop_long_block`). The remaining boolean is unchanged: chop regime + LONG + `htf_bias≤0` + not `structure_bounce` — a regime-side gate, same family as Veto 10, no ML content. Explicitly not wired to a real `ml_probability` — that would tighten/loosen this gate against the live model's distribution with no reliability table to justify a cutoff, a holdout decision deliberately deferred pending the Brier/ECE work described above. Also confirmed `_build_signal`'s post-clamp `sl_pct` metadata already exists (pre-existing `metadata["sl_pct"]`) — no duplicate field added, two more stale "0.4-1.2%" comments fixed to match the real 0.55%/0.95% clamp. Suite unchanged (325/11), bot restarted clean.

## ML stamp-once + reliability table shipped (`d431d62`, 2026-09-16)

Full architectural design in `PIPELINE_ARCHITECTURE_TARGET_20260916.md` (item 6). Same failure class as P3.11's hardcoded-0.0 bug, but for the real `ml_probability`: it was silently coerced from a genuine abstain/stale-model `None` to a fake `0.5` at two independent points in `strategies/scalp_strategy.py` (scoring call site and, separately, the metadata write after `_build_signal`), and `bot/signal_tracker.py` had three different fallback defaults (0.5/0.0/0) across its three readers. Fixed: one canonical `stamped_ml_probability` computed once right after the ML scorer call, `None` propagated as a real `null` into `signal.metadata["ml_probability"]` on `ABSTAIN_*`/`STALE_MODEL`/`UNREACHABLE`/`API_ERROR`. **`classify_trade()` now falls back to `TRADE_TYPE_SCALP` on a null probability** — explicitly signed off as a real occupancy change (9 of 27 closed `structure_bounce` trades are `STALE_MODEL`, common not rare), tagged `metadata["ml_tier_reason"]="ml_abstain"` for visibility. New test added (no prior test exercised a missing/null `ml_probability`). New `scripts/reliability_table.py`: equal-mass-binned Brier/ECE/mean-R reliability table on `storage/closed_signals_archive.jsonl`, dropping the same no-real-score verdicts. Smoke-run against the current 31-row archive (18 with a real score) — every one of 6 tiny slices correctly flagged `insufficient_sample`, no fabricated curve; two of the larger (still-tiny) slices already show negative Brier skill vs. base rate, consistent with "uncalibrated policy, not a probability," but n is nowhere near enough to conclude anything yet. Suite: 326/11 (one new test), bot restarted clean.

## Still open (unchanged, no new decisions made)

`bos_choch`'s second confirmation gate (keep/drop), `ema_momentum`'s zero-print status (quarantine/accept), `vwap_band` exclusion from `structure_bounce` (blocked on sample size), `SCANNER_TRADE_TYPE["structure_bounce"]=RUNNER` (keep forced vs. let `classify_trade()` decide — needs its own holdout on bounce's mean R and `fee_drag_r` specifically), the three-tuple reversion-scanner membership question for `rsi_extreme` in Veto 10/P0.8, and a possible future `p<X` gate for P3.11 once a reliability table exists (separate flag, default off, per explicit instruction). All on hold. Re-run `scripts/reliability_table.py` periodically as the archive grows — it's the gate on ever trusting a 0.50/0.65 cut.
