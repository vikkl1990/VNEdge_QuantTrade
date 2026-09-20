# Pre-registration: naked market structure — 2026-09-19

Written before the module or the lab was run. Definitions, hypotheses and kill rules are fixed here.
Price only: no indicator other than ATR14 (Wilder) as the unit of scale.

## Definitions (`data/market_structure.py`)

- **Swing**: ATR ZigZag. A candidate high is the running max while the leg is rising; it becomes a
  *confirmed swing high* on the first bar whose low ≤ candidate − k·ATR14 (that bar's ATR). Mirror for
  lows. **k = 2.0 on both 1h and 4h**, fixed. Each swing carries `idx` (where it printed) and
  `confirmed_idx` (when it became knowable). Every feature and event below uses only swings with
  `confirmed_idx ≤ current bar`.
- **Labels**: each confirmed swing is HH/LH (vs previous confirmed high) or HL/LL (vs previous
  confirmed low). First of each kind is unlabelled.
- **State** (updates only on a confirmation bar): `up` if last high is HH and last low is HL;
  `down` if LH and LL; else `range`.
- **Events** (bar close vs the last confirmed, still-unbroken swing; a swing can be broken once):
  `BOS_up` = close > last swing high while state `up`; `CHoCH_up` = same break while state `down`;
  `RB_up` = same break while state `range` (range break). Mirror `_down`. Recorded: break distance
  in ATR, bars since the swing printed.
- **Naked level**: a confirmed swing extreme not yet traded to since its confirmation. **Touch** =
  first bar whose high ≥ naked high (low ≤ naked low); `touch_reject` if the close is back on the
  original side, `touch_through` otherwise. A level is consumed by its first touch.
- **Range** (state `range` only): last confirmed swing high/low; `pos_in_range = (close − lo)/(hi − lo)`;
  `bars_in_range` since the state flipped to range.

## Hypotheses (directional forward close-to-close return; 1h horizons 4/12/24 bars, 4h 1/3/6 bars)

- H0 reference: state `up` → long, `down` → short, every bar.
- H1 CHoCH: `CHoCH_up` → long, `CHoCH_down` → short (reversal follows the first opposing break).
- H2 BOS: `BOS_up` → long, `BOS_down` → short (continuation).
- H3 Naked touch-reject: touch of naked high → short, naked low → long. Reported also split by level
  age above/below the fit-period median (one sub-bin, pre-declared).
- H3b Touch-through: same levels, close through → direction of the break.
- H4 BOS distance: H2 events split into fit-period terciles of break distance in ATR.
- H5 Range edges: state `range`, `pos_in_range` < 0.2 → long, > 0.8 → short.
- H6 Range break: `RB_up` → long, `RB_down` → short.

## Judge

500 days of 1h and 4h (Delta, cached), BTC and ETH. Fit = first 120 days (bin edges and medians
from fit only); 6 judge folds of 60 days. **Stable** = judge-fold means share the fit sign in ≥ 5/6
folds. **Candidate** = stable on BOTH symbols, same hypothesis and horizon, with |pooled judge mean|
> 2 × 0.118 % (taker round trip). Null: 300 draws of the same number of bars with the same direction
labels placed at random; the printed `p_null` is the share of draws that match or beat the observed
judge mean with the observed fold agreement. Independent-event count (events ≥ horizon apart) is
printed beside every n. No cell may be re-parameterised (k, thresholds, bins) after this is run.
Nothing routes from this note; a passing cell gets its own sign-off PR.

## Results (2026-09-19, appended; definitions unchanged)

`scripts/structure_lab.py` (k = 2.0), 500 d of 1h and 4h, BTC and ETH. Per-bar frames and the
hypothesis table are in `storage/research/scanner_lab/structure/`.

**Structure sanity.** 1h: ~1,320 confirmed swings per symbol, median spacing 7 bars, confirm lag
3 bars, median leg 3.5 ATR; state occupancy ≈ 35 % range / 33 % down / 31 % up; 50–100 events of
each type; ~220–240 naked touches of each kind. 4h: ~318 swings, 9–31 events per type (too few for
a 120-day fit; H1/H2/H4/H6 on 4h are descriptive only).

**Judge.** Stable cells 10 of 78 (13 %) against ~22 % expected by chance. **Candidates on both
symbols: 0.** No hypothesis holds its fit sign in ≥ 5/6 folds on both BTC and ETH at any horizon.

- H0 state: ETH 1h 5/6 at every horizon (+0.13 %/12h) but BTC 2–3/6 and ≈ 0. ETH's 2025-26 drift.
- H1 CHoCH (1h): negative in judge on both symbols at 12h/24h (BTC −0.30 %/24h, ETH −0.24 %),
  i.e. the first opposing break is *faded*, but only 1–3/6 folds; n_fit 23–31.
- H2 BOS (1h): BTC 4h 5/6 but +0.064 % < fee; ETH 4/6; 12h/24h negative on both.
- H3 naked touch-reject (1h): ≈ 0 on both symbols (|judge| ≤ 0.07 %), 2–4/6.
- H3 naked touch-reject (4h): **the rejection loses on both symbols** — fit −0.27…−0.32 % BTC /
  −0.36…−0.93 % ETH, judge −0.10/−0.24 % BTC (4/6, 3/6) and −0.48/−0.95 % ETH (5/6, 4/6) at
  12h/24h. Same sign in fit and judge on both symbols; fails the fold bar on BTC. n_fit 21–26,
  n_judge 84–97, ~70–90 independent. The consistent reading is "a naked 4h level is run, not
  respected", the opposite of the hypothesis. Reported as a lead, not re-parameterised.
- H3b touch-through: ETH 1h 4h-horizon 5/6, +0.145 % (net +0.03); BTC ≈ 0.
- H5 range edge (1h): fading the edge loses — ETH −0.40 %/24h, 5/6, p_null 0.000, net of the
  inverse +0.28 %; BTC judge also −0.24 %/24h but fit +0.21 (1/6). Judge-period agreement on the
  *inverse* (breakout at the range edge) on both symbols, fit disagreement on BTC → not a candidate.
- H6 range break (1h): ≈ 0, 1–3/6.
- 4h events, descriptive (whole 500 d, halves): CHoCH 12h/24h negative on both symbols in both
  halves (BTC −0.37/−0.65 %, ETH −0.23/−0.65 %, n 21–25); BOS 24h positive on both (+0.42/+0.61 %,
  mostly second half, n 48–49); range break opposite signs across symbols.

**Verdict.** Naked structure as defined here carries no two-symbol walk-forward edge at 1h. The one
direction that agrees across symbols, frames and windows is *against* the textbook: 4h naked levels
get run through and 4h CHoCH gets faded, i.e. structure levels attract price rather than hold it.
That is the only lead worth a follow-up (a second, non-overlapping sample or a lower k for more 4h
events, pre-registered separately). Nothing routes.
