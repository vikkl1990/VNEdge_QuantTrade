# Pre-registration: multi-timeframe plan + 1m execution — 2026-09-20

Written before `scripts/mtf_lab.py` was run. The frame roles: 4h bias, 1h plan (stop, targets,
exit stack), 15m/5m arrival (today's scanner entries), 1m execution. The 1m frame never sets the
risk; every cell below keeps the plan's stop and R from the signal.

## Test A — does higher-frame agreement improve the 1h book?

Entries: the 500-day 1h scanner dumps (`variants/{SYM}_USDT_1h500d_trades.csv`, all live scanners,
live clamp). Structure: `data/market_structure.build_structure(k=2.0)` on 4h and on 1h candles; the
state used for a trade is that of the last 4h (1h) bar that CLOSED at or before the entry time.
Buckets: `agree` (long in `up` / short in `down`), `disagree` (long in `down` / short in `up`),
`range`/`none`; for 4h, for 1h, and `both_agree`. Metric: net trail R (`alt_trail − fee_r`).
Fit 120 d, 6 judge folds of 60 d. **Candidate** iff, on BOTH symbols: `agree` pooled judge net > 0,
positive in ≥ 4/6 folds, top-5 share < 0.6, and `agree − disagree` > 0 in the judge window.

## Test B — does 1m execution improve the same trades?

Entries: the 200-day dumps (5m, 15m, 1h scanner entries) restricted to entry times inside the 1m
coverage (Delta trade archives, 2026-01-01 → 09-01; overlap 2026-03-02 → 09-01). Plan per signal:
stop distance = `risk_pct` (the live clamp), R = entry × risk_pct / 100, notional $30,000. Exit is
simulated on 1m bars for every cell with the 1h card in minutes: kills at 4×tf and 8×tf minutes
(MFE < 0.3R / 0.8R), cap 24×tf min, window 48×tf min, trail 1R behind peak after 1R.
ATR5m = Wilder ATR14 on 5m bars at the signal time. W = 30 min execution window.

- **B0** baseline: market at the signal close (taker), stop on 1m touch.
- **B1** exit resolution: B0 entry, stop on 1m close-through (fill at that close).
- **E1** limit at entry − 0.25 × ATR5m (long; mirror short), maker; filled if a 1m low reaches it
  inside W, else the signal is skipped (R = 0).
- **E2** 1m pullback-then-resume: after a pullback ≥ 0.2 × ATR5m against the direction, enter at
  the first 1m close beyond the high (low) of the previous three 1m bars, taker; skipped after W.
Fees: yaml `fees:` block (Scalper Offer per hold time), maker entry only in E1.
Metrics per cell × tf × symbol: signals, fill %, net $ per signal (skips count 0), net $ per filled
trade, net R (plan units) per signal, stop-out %, entry improvement in ATR5m (filled only), hold.
Fit 2026-03-02 → 05-31, judge June / July / August (3 folds). **Improvement candidate** iff a cell
beats B0 on net $ per signal on BOTH symbols in the pooled judge and in ≥ 2/3 folds.
**Tradeable candidate** iff additionally net $ per signal > 0 on both symbols in the judge.
No cell may be re-parameterised (0.25, 0.2, three bars, W) after this is run. Nothing routes.

## Test C (added 2026-09-20 after A and B were read, before C was run)

A's `both_agree` bucket is the only cell that is positive on both symbols in the judge window, and B's
E1 is the only execution that beats the baseline on both symbols in every fold. C asks whether the
two together lift the book above zero. Entries: the Test-B signal set (5m/15m/1h, gap-free 1m
coverage), bucketed by 4h+1h structure agreement at entry (as in A), executed as B0 and as E1.
Same fit/judge folds as B. **Candidate** iff `agree × E1` net $ per signal > 0 on both symbols in the
pooled judge and in ≥ 2/3 folds. Same data as A and B, so this is a confirmation on overlapping
data, not a fresh out-of-sample; a pass here only licenses a live judge on new paper trades.

## Results (2026-09-20, appended; definitions unchanged)

`scripts/mtf_lab.py`; outputs in `storage/research/scanner_lab/mtf/`. Test B/C used only signals with
gap-free 1m coverage (ETH March archive is absent: BTC 15,890 signals Mar 2 → Aug 30, ETH 15,318
Apr 1 → Aug 30). Unit tests: tests/test_mtf_lab.py.

**Test A — higher-frame agreement on the 1h book (500 d, net trail R).** Direction is consistent:
`agree` beats `disagree` on both symbols for 4h, 1h and both (BTC −0.07 vs −0.26 on 4h; ETH +0.12 vs
−0.06). `both_agree`: BTC +0.16 judge (4/6 folds) but top-5 share 0.83 and fit −0.23; ETH +0.24
(5/6, top-5 0.39, fit +0.02). **Not a candidate** (BTC fails top-5 and fit sign). Occupancy ≈ 30 %
agree / 33 % disagree / 36 % range; both-agree ≈ 10 % of trades.

**Test B — 1m execution on the same plan (net $ per signal, $30k notional, judge Jun–Aug).**
- E1 limit 0.25 ATR5m inside the zone, maker: **beats B0 on both symbols at every tf in 3/3 folds**,
  +$9…+$20 per signal (0.03–0.07 % of notional), fill 86 %, stop-outs +1.5 pp. Improvement candidate:
  **yes**. Tradeable: **no** (still −$13…−$36 per signal).
- E2 pullback-then-resume: beats B0 by +$0.5…+$11, 2–3/3 folds; weaker than E1 and taker.
- B1 close-through stops: worse everywhere (−$1…−$9). Killed.

**Test C — both-agree × E1.** Pooled judge +$29.5/signal BTC (+0.13 R, 2/3 folds, top-5 0.35) and
+$30.5 ETH (+0.10 R, **1/3 folds**, top-5 0.37); fit −$19 / −$16. Fails the bar on ETH folds and on
fit sign. Month view: June −3/−23, July −40/−22, **August +147/+157** (BTC/ETH) — the whole judge
surplus is August's trend on both symbols; the structure-agreement filter only "worked" in the one
month where structure agreed with a persistent move.

**Verdict.** The multi-timeframe *execution* layer is real and cheap: a maker limit 0.25 ATR inside
the zone recovers ~$10–20 per $30k signal on both symbols in every fold without changing the plan.
The multi-timeframe *filter* is directionally right (never trade against 4h+1h structure) but does
not make the book positive: outside August, aligned trades lose like the rest. Nothing routes from
this note. What it licenses: (1) an execution-layer sign-off PR (1m limit-in-zone entry, maker,
30-min fill window, plan stop unchanged) judged live; (2) a "no counter-structure entries" veto as a
separate sign-off item, judged live on paper trades after the reset.
