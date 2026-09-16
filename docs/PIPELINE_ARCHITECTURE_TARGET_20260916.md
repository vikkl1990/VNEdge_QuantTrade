# Pipeline Architecture — Target Contract
**Authored:** 2026-09-16, following the scanner deep-dive series in `SCANNER_CLUSTER_ANALYSIS_TODO_20260915.md`.

The bot is not under-scanned. It is over-layered: many independent policies share one string (`regime`) and one float (`weighted_score` / `ml_probability`) that mean different things at each layer. The architectural fix is one ticket object and four explicit stages, instead of encoding product rules in comments, duplicate tuples, and hotfixes.

This doc is the target-state reference. Nothing in it has been implemented. Every finding below is corroborated by the specific commits/reads in `SCANNER_CLUSTER_ANALYSIS_TODO_20260915.md` — this doc doesn't re-derive them, it names the shape they point at.

---

## What is broken as architecture

1. **Score is not a unit.** Scanner checklist score, outer uncapped `weighted_score`, post-F VWAP penalty (−12), confidence alias, ML `p`, EV `p_win`, `classify_trade`'s 0.50/0.65 cuts — five different quantities. F races the wrong one; exits trust another.
2. **Regime is a god-string.** The detector, the routing roster, Veto 10, P3.11, `classify_trade`'s upgrades, and the EV table all switch on `trending_up` vs `sideways`. ADX 29 vs 30 changes the whole book. Dead `quiet`/`ranging` keys still exist in places.
3. **Thesis dies after F.** Sweep-fade vs bounce-hold vs bos-break are different bets. Mutex + winner-take-all + one SL clamp (0.55%-0.95%) + one trade-type ladder (or forced RUNNER) flattens them into the same position shape.
4. **Gates run at the wrong time.** P3.11 read a hardcoded `0.0` (confirmed and fixed 2026-09-16 — renamed, not stamped). The VWAP noise-zone penalty applies post-F. The session gate lives inside `structure_bounce` only, not in routing. The fee-viability pre-gate reads `_scanner_sl_tp`'s calibrated TP1; the tracker then rebuilds TP1/TP2/TP3 from `trade_type` regardless.
5. **The funnel isn't as visible as it looks.** Shadow diagnostics were a second, drift-prone reimplementation of scanner logic (several confirmed wrong before today's fixes). `blocked_htf`-style counters exist that may never increment. Print rate ≠ fill rate — no join exists yet. The joint-bar log is one day old.

## Target shape

```
BAR
  → FEATURES (regime context, map, ATR, session, HTF)   # no trade decision
  → CANDIDATES[]   each scanner may emit 0..1 Candidate
  → POLICY         hard filters on Candidate (session, side×trend, fee, liq)
  → RANK           one utility, or allow N if different cluster
  → TICKET         sl/tp/size/type computed once from TicketSpec
  → EXIT           only TicketSpec + HOLD profile
```

**`Candidate`** is the contract every scanner emits (0 or 1 per bar):
```
scanner, cluster, side, thesis          # fade | hold | break
score_internal                          # checklist, capped
entry, sl_struct, sl_vol_raw
session_ok, regime_label, htf, p_ml     # nullable
```

**`TicketSpec`** is computed once, in `_build_signal` (the codebase already has most of the machinery for this — the SL clamp already lives here) — clamp SL, fee_drag, trade_type. Scanners must not pretend their raw SL/TP is what actually executes.

## Concrete changes, in order (none implemented)

1. **Single policy module.** One `REVERSION` set, one `STRUCTURE` set, one `MOMENTUM` set. Veto 10, P0.8, the VWAP exemption, and P3.11 all import those sets instead of each declaring their own tuple. (`_REVERSION_SCANNERS` — unified 2026-09-15 — is a partial instance of this pattern already; the other two sets don't exist yet.)
2. **Rank on one number, freeze it.** F uses `score_at_F` only (already logged in the joint-bar row). Ban post-F mutation of that field. Soft penalties either apply before F to every candidate, or apply only to min-confidence after — never both in the same pipeline.
3. **Cluster mutex stays, but by thesis, not name.** The structure cluster (`structure_bounce`/`liquidity_sweep`/`bos_choch`/`order_block_entry`) grouping is correct for same-thesis collapse. An opposite-side bounce-vs-sweep on the same bar should be a logged fight, not a silent `max()`. Same-side: mutex (as today). Opposite-side: configurable, default block-both or take-higher-score — never take both.
4. **Regime → roster only.** Veto 10 should read `htf_bias` / 1h macro / EMA21 slope directly, not the ADX-derived regime label — otherwise the bot keeps fading higher-highs whenever the label happens to read "sideways." The detector stays a label for analytics/dashboards. Optional: hysteresis (ADX must leave the 28-32 band before the label flips) so the roster doesn't churn bar-to-bar.
5. **Exits follow thesis, not ML tiers.**
   - Fade (`liquidity_sweep`, `rsi_extreme`, `vwap_mean_revert`): SCALP/INTRADAY only, no forced RUNNER.
   - Hold/bounce: either `classify_trade()` or an explicit bounce spec — pick one, not the current `SCANNER_TRADE_TYPE` override sitting on top of `classify_trade()`.
   - Break (`bos_choch`): the 2-candle confirm stays until a holdout says otherwise (this session's open decision, still open).
   - Delete unused display metadata (already done, 2026-09-16); keep `_scanner_sl_tp` only for the fee estimate, and make that estimate use the post-clamp SL rather than the raw per-scanner value.
6. **ML is a gate or a ranker, never both, until calibrated.** Stamp `p` once, right after `score_candidate`. `ABSTAIN` must serialize as `null`, never `0.5`. `classify_trade`'s default-on-missing must not be `0.5` either — a missing probability is not a real mid-tier signal. HOLDS-level AUC does not license a RUNNER cut. A reliability table (Brier/ECE, per-slice, ABSTAIN dropped) comes before any threshold is trusted, per the discussion in the scanner TODO doc.
7. **Measurement is part of the runtime, not an afterthought.** Joint-bar log + post-clamp `sl_*` metadata + `veto10_fired` + bounce first-fail reasons (all shipped or in progress) stay and get finished. Funnel counters keyed by scanner. No second shadow-diagnostic block duplicating real scanner logic — that pattern is what caused the `trend_continuation`/`rsi_divergence` drift already found and fixed.

## What not to do while approaching this

- Add scanners or enforce-whitelist entries to "make it fire."
- Widen `liquidity_sweep`'s 0.25×ATR tolerance or drop its 55%-body gate, or drop `bos_choch`'s second confirmation candle, to chase print rate.
- Recalibrate the 07:00-15:00 UTC bounce session window from a 15-minute tail sample (2026-09-16's finding: the "1 print in 24h" was a boundary artifact, not a rate).
- Stamp ML into P3.11 "to restore the comment" — the comment describing an ML-bless escape was already found stale and the escape already found dead; restamping would be re-adding removed behavior without a holdout, not a bugfix.
- Ship a P3.12 as one more hotfix on the same chop-long cell P3.11 already covers.

## End state

Fourteen scanners can stay exactly as they are. Three clusters emit candidates; one policy decides legality; one ranker picks; one spec sizes and exits. Regime becomes a feature, not a router-plus-veto-plus-exit bus simultaneously. Until this contract exists, every accurate micro-fix from the last two days (diagnostics, P3.7 deletion, `sl_pct` logging) is real and correct in isolation, but is still patching a pipeline that disagrees with itself at the architecture level.

## Status

**Item 6 (ML is a gate or a ranker, never both until calibrated) — first concrete step shipped**, via an explicit plan-mode design + sign-off (commit `d431d62`, 2026-09-16): `ml_probability` is now stamped once, right after `score_candidate`, with abstain/stale/unreachable/API-error propagating as a real `null` instead of a fake `0.5` (this was the same failure class as P3.11's hardcoded-0.0, just for the real value). `classify_trade()`'s default-on-missing is no longer `0.5` — it now explicitly falls back to `SCALP`, the most conservative tier, on a null probability. `scripts/reliability_table.py` exists and runs clean, currently reporting every slice as `insufficient_sample` (18 real-scored closed fills total, spread across 6 tiny slices) — exactly the expected, honest state before any 0.50/0.65 cut can be trusted. See `SCANNER_CLUSTER_ANALYSIS_TODO_20260915.md` for the full writeup.

Items 1-5 and 7 remain unstarted. This doc is still the shared reference for when the rest is explicitly scoped. See `SCANNER_CLUSTER_ANALYSIS_TODO_20260915.md` for the still-open per-scanner decisions (`bos_choch` second gate, `ema_momentum` quarantine, `vwap_band` exclusion, forced-RUNNER override, reversion-tuple membership) that item 5 and item 1 above would resolve as a side effect of the broader contract, but that remain independently open regardless of when/whether the rest of this refactor happens.
