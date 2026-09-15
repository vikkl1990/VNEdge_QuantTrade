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

## ADX thresholds inside `_classify` — verified reference, not touched

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
- Deduplicating `find_liquidity_zones`'s pairwise output changes scoring, not just occupancy — if the confluence bonus has been quietly inflated by counting duplicate levels as independent confirmation, removing that inflation could shift which candidates clear `MIN_SETUP_STRENGTH` or beat a cluster-mate at step F. Needs a before/after comparison, not a silent fix.
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

**Production readout so far** (small n, reported honestly, not as a result): as of the last check, 13 bars logged, 6 printed — 3 `eqh` shorts, 2 `eql` longs, 1 `roll_high` short (all `liquidity_sweep`), `structure_bounce` silent every single time so far, `blocked_cluster_sibling` still `false` throughout (mutex idle — nothing to arbitrate). Consistent with "980:19 is occupancy, not F-theft," but nowhere near enough bars to call it. **Not touching** `0.25×ATR` (sweep tolerance), the `55%` body gate, or the `1.0 ATR` slope threshold on this sample — the log is doing its job; watching for: the first `bounce_printed` row (any source), then the first bar with both printed, the distribution of `ema21_slope_adj` on bounce-printed rows (expecting mostly `0`s), and the `sweep_source` mix over a larger sample.

## Reversion scanner membership is not one set

A real product inconsistency, not a style nit — found while verifying Veto 10 (`strategies/scalp_strategy.py:3164`, the post-F "block counter-trend for momentum scanners" rule) against the code. There are three independently-declared "reversion scanners" tuples, two identical and one different:

| Gate | Members | Effect on `rsi_extreme` |
|---|---|---|
| Veto 10 (`_reversion_scanners`, line 3167) | `rsi_divergence`, `cvd_divergence`, `vwap_mean_revert` | treated as momentum → hard-blocked counter-trend in `trending_up`/`trending_down` like any other non-reversion scanner |
| P0.8 HTF-hard-veto exemption (`_reversion_scanners_p08`, line 3303) | same 3, declared separately | same — momentum |
| VWAP noise-zone penalty exemption (`_reversion_names`/`_reversion_conf_names`, this session's fix, lines 2506/2619) | those 3 **+ `rsi_extreme`** | exempt — skips the −20/−25 |

So `rsi_extreme` is reversion for one mechanism and momentum for two others, depending on which of three near-duplicate tuples happens to gate that particular check. **Not merged in this pass** — unifying them changes how often Veto 10 and the P0.8 HTF-hard-veto actually fire for `rsi_extreme` (more or fewer hard blocks), a real behavior change that doesn't belong in the same patch as documenting the inconsistency or as the joint-bar logging work. Whoever picks this up next should decide the correct membership once (probably: all four, or introduce one shared constant), then thread it through all three gates in one deliberate change — not fix it as a side effect of something else.

## `veto10_fired` on the joint-bar row — scoped, not yet shipped

Right instinct, more than a bolt-on. Design, to avoid the two ways to get it wrong:

- **Do not** move the log write to after the veto-classification loop — several early-return points sit between the current hook (right after cluster mutex) and that loop (zero-confidence check, the momentum_trend/simple_bias investment block, empty-regime check, `MIN_SETUP_STRENGTH` weak-setup veto, the scanner-cooldown hard block, the side-flip-cooldown hard block), and moving the only write past them would silently drop rows for exactly those bars — breaking the print-rate denominator the whole instrument exists for.
- **Do** keep exactly one write per eligible bar, but make it a build-then-write split: build the row dict at the existing hook with `veto10_fired: null`, hold it pending, and flush it (write exactly once) at whichever comes first — an early return before the classification loop (stays `null` — genuinely unknown, never `false`) or the classification loop itself (set `true` if `"REGIME SIDE:"` was appended to `vetos` for this bar's winner, `false` if the loop ran and it wasn't — `false` positively means "checked, didn't fire," not "don't know").
- Implementing this safely means either instrumenting every one of those ~6-7 early-return sites individually (surgical, but easy to miss one as the code evolves) or wrapping the whole ~950-line stretch from mutex through the classification loop in a `try/finally` (guarantees the flush, but is a much bigger, riskier structural change than anything else in this thread — that stretch has its own nested try/excepts already). Neither is a same-turn bolt-on the way the other joint-bar fields were. Logged here as the next concrete piece of this instrument, not implemented yet — once shipped, the target cell is `sideways + eqh short + veto10_fired=false` vs. `trending_up + eqh short + veto10_fired=true`, which is the split that actually isolates what Veto 10 does and doesn't own.
