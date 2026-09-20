# Pre-registration: `trend_pb` (trend-pullback family detector) — 2026-09-17

Written before any detector code. Changes to this definition after seeing
out-of-sample results are a new pre-registration, not an edit to this one.

## Why this family, why now

The 2026-09-17 scanner-lab diagnosis (see `docs/SCANNER_CLUSTER_ANALYSIS_TODO_20260915.md`,
"Diagnosis pass") showed no filter/exit/fee/stop combination rescues any of
the 21 scanners on BTC walk-forward. The one directionally useful split was
`post_impulse` routed in `trending_up` (+0.12R) vs unrouted (−0.14R). Trend
pullback is therefore the first family rebuilt; it **replaces** (does not sit
beside) `ema_momentum`, `trend_continuation`, `momentum_ride`,
`post_impulse`, `supertrend_flip`. `family_id = "trend_pb"`.

Sequence: lab on **15m and 1h first**. 5m only as a stress case (maker entry +
ATR-denominated R): on 5m the typical trade's MFE (0.57–0.81R) sits in the same
size as the fee (~0.18R); better geometry can raise MFE but not change that.

## Setup (long; short is the exact mirror)

1. **Impulse** — a closed bar with `body >= impulse_atr * ATR` (v1: 0.7), OR a
   close that breaks the prior 10-bar high by `>= 0.5 * ATR`. Freeze the
   impulse bar's high / low / time.
2. **Pullback** — 1–8 bars (15m/1h; cap 5 on 5m) after the impulse:
   no close below the impulse low; no wick below the impulse low (no new
   extreme against the impulse); depth `impulse_high − min(pullback low)
   <= 1.2 * ATR`; no pullback high above the impulse high (else it is
   continuation, not a pullback).
3. **Location** — every pullback bar's low holds above its EMA21, or (for a
   breakout-type impulse) above the broken 10-bar high.
4. **Trigger** — the current closed bar closes above the pullback swing high
   (max high of the pullback bars). The trigger bar is not part of the pullback.
5. **Score / veto, never AND gates** — Supertrend direction, EMA 8>21>50
   stack, RSI percentile, rel_vol magnitude are score only.
   Vetoes (v1): stretched — `close − EMA8 > 2 * ATR`; volume — trigger-bar
   volume `< 1.0 ×` the time-of-day median (same hour-of-day over the lookback).

## Risk (pattern-defined, part of the definition)

- Entry: next bar open, taker. In parallel, a maker limit at the trigger close
  (filled only if the next bar trades back to it). The lab reports both.
- Stop: pullback extreme (min pullback low) `− 0.1 * ATR`.
- Invalidation: a close back through EMA21 against the trade → exit at that
  close, stop not required.
- Expiry: 6 bars after entry with `MFE < 0.3R` → exit at that close.
- Targets, lab only: 1R, 1.5R, and the existing chandelier shape
  (`initial_risk × 1.0`, active after 0.3R MFE). No 3R claim.

## Policy (lives above the detector)

- Regime gate: longs only in `trending_up`, shorts only in `trending_down`.
  `sideways` → nothing.
- `allow_short` policy unchanged (ETH short off).
- UTC hour is recorded. No hour ban in v1; an "hours 00–12" filter is a
  pre-registered **variant**, not part of the v1 definition.

## Kill rules — v1 FAILS if, on Jul–Sep OOS, BTC and ETH, after taker fees:

- avg R `<= 0`, or
- median MFE `< 1.0R` on 15m/1h, or
- fires `> 3%` of bars (still a spray), or
- the edge exists only in Mar–Jun.

Maker-only green does not pass v1; that is a separate variant. If 15m/1h fail,
stop — do not loosen `impulse_atr` to get fills.

## Lab protocol

- Harness: `scripts/scanner_lab.py --family trend_pb` (same frames, indicators,
  regime detector, fee model, notional as the diagnosis).
- Fit: 2026-03-01 → 2026-06-30. Judge: 2026-07-01 → 2026-09-17.
- Symbols: BTC then ETH. Long and short reported separately.
- Variants, one degree of freedom per run:
  TF ∈ {15m, 1h, 5m}; `impulse_atr` ∈ {0.6, 0.7, 0.8}; entry ∈ {next-open taker,
  limit}; exit ∈ {1R, 1.5R, CE trail}. No stacking "best of six" after OOS.
- Comparison: the pooled old family on the same TF/period — the five replaced
  scanners' lab trades OR-ed (raw, and routed-only) — after costs. `trend_pb`
  must beat that bag, not zero.

## Results — 2026-09-17 (appended after the runs; the definition above is unchanged)

Runs: `scripts/family_lab.py --family trend_pb --symbol {BTC,ETH}/USDT --timeframes 15m,1h`
(v1 gated), plus `--no-regime-gate` diagnostics and the pre-registered
`impulse_atr` 0.6 / 0.8 variants on BTC. Outputs under
`storage/research/scanner_lab/variants/*_trend_pb_*`. Detector tests:
`tests/test_trend_pb.py` (7), contract simulator: `tests/test_family_lab.py` (6).

**Verdict: v1 FAILS. Stop. Routing untouched; the five scanners stay.**

- Fire rate is fine: 0.47–0.73% of bars on every TF/symbol (kill line 3%).
- The regime gate as pre-registered leaves single-digit samples: BTC 7 (15m) / 3 (1h),
  ETH 3 / 1. ~65% of fires land in bars the confirm-frame `MarketRegimeDetector`
  labels `sideways`. v1-as-written is unevaluable, not passable.
- Gate-off diagnostic (labelled, not a v1 result): BTC 15m n=68 IS / 29 OOS —
  IS avg −0.47R, OOS r1 +0.03R with top-5 share 4.75 (one trade), r15/trail
  negative; BTC 1h n=13/9, same shape. impulse 0.8: fails everywhere; 0.6: fails
  except 1h/r1 +0.016R on 10 trades (top-5 share 25.8). ETH 15m: median MFE 0.62R
  (< 1.0 rule), IS negative; ETH 1h n=5 — no evidence either way.
- Against the bag: BTC 1h old family OOS +0.05R (raw) / +0.17R trail (routed, n=20);
  **ETH 15m old family routed OOS +0.49R (n=51), ETH 1h raw +0.12 / trail +0.33R
  (n=164)** — the existing momentum scanners under the live routing beat
  `trend_pb` outright on ETH. Nothing here justifies a swap.

What the pass did establish: the geometry travels (median MFE 1.4R on BTC 15m/1h)
but the pullback-then-trigger entries do not carry an edge on these two pairs in
Mar–Sep 2026, in either window, at any allowed impulse. The regime labeller, not
the detector, is what would need a separate pre-registration before this family
is worth a second look.

## Explicitly out of scope for this pass

Routing rewrite (if the lab passes, the five scanners are swapped for
`trend_pb` behind the *existing* regime gate); stop-percent experiments;
`liquidity_sweep`; SHORT recipes for the replaced scanners (they die with the
family).
