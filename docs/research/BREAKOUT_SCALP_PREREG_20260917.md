# Pre-registration: `breakout_scalp` — the real-account pattern — 2026-09-17

Reverse-engineered from a real Delta ETHUSD order history (13 trades, 12 wins,
2026-09-10 → 09-17; see the session notes): longs taken as price makes a fresh
4-hour high with EMA8 > EMA21, a small trailing take (+0.3–0.4%) tagged by a
stop order above entry, a wide protective stop (~−1.1%), exits inside the
30-minute Scalper window whenever possible (exit fee 0), ~$37k notional.
Written before any lab run; nothing below is tuned on lab data.

## Definition (long; short is the mirror at the 4h low)

- Frame: 5m closed bars (primary, matches the account's timing). 15m as the
  long-sample variant (500 days; Delta serves 5m only ~200 days).
- Signal: the closed bar's close is above the prior 48-bar (4h) high — first
  bar of a new 4h high only (cross event, not every bar above), and EMA8 > EMA21
  on the same frame. RSI(14) recorded, not gated.
- Entry: next bar open, taker. One open trade at a time.
- Take: first touch of entry × (1 + 0.35%) — modelled as a resting stop-market
  above entry, exit taker (free if hold ≤ 1800 s per the FeeModel).
- Stop: entry × (1 − 1.1%), first touch, exit taker.
- Max hold: 12 h (the account's longest hold was 11.3 h) → close at that bar.
- Sizing/fees: $1000 × 30x = $30k notional; `FeeModel` from settings.yaml
  (taker 0.059%/side incl. GST, Scalper Offer per exit leg). Also reported with
  the offer OFF, to show how much of the edge is the free exit.

## Results — 2026-09-17 (appended; definition unchanged)

Runner: `scripts/breakout_scalp_lab.py`; outputs under `storage/research/scanner_lab/variants/*_breakout_scalp_*`.

**Not a candidate in any cell.** 5m/200d (4 folds) and 15m/500d (6 folds), BTC and ETH, long and
short, offer ON and OFF: avg $ per trade −$27 … −$55, positive folds 0 of 4 / 0 of 6 everywhere
(one +fold for ETH persist=2). The rule reproduces the account's *shape* — 69–78% hit rate,
20–80-minute holds, most exits by take — but at +0.35% vs −1.1% the geometry needs ≈ 76–78% wins
after fees; the mechanical breakout delivers 65–78% and fires ≈ 5×/day (1.6–2.3% of bars) against
the account's ≈ 1.5/day. Variants: take 0.25/0.50, stop 0.8/1.5, persist 2, no EMA gate — all red.
15m/500d also fails the fire-rate rule (3.6–4.4% of bars).

v2 (post-hoc, from the account's fills, not lab output): the account's only loss was cut at
−0.43% after 2.6 min, not at the −1.1% stop → stop 0.45%. Hit rate collapses to 53–55%
(−$27 … −$35/trade). The account's 92% on 13 trades is discretion (entry selection and manual
loss-cutting), not a geometry a rule reproduces.

Closed. What it adds to the record: the small-take / wide-stop / free-exit shape is the right
*cost* answer for 5m (fees $22 not $44, takes inside the tape's typical excursion), but it needs
a hit rate no mechanical 4h-breakout entry produced on these pairs in 200–500 days.

## Kill rules

Rolling folds (5m/200d: 30-day judge windows after a 60-day fit; 15m/500d:
60-day windows after 120 days). Candidate iff, on **both** BTC and ETH:
avg $ after fees > 0 in ≥ 4 of the judge folds, pooled avg > 0, pooled top-5
share < 0.6, fires ≤ 3% of bars. Long and short reported separately; the
`allow_short` policy is applied afterwards, not inside the test.
Variants (one degree of freedom each, all reported): take ∈ {0.25%, 0.35%,
0.50%}; stop ∈ {0.8%, 1.1%, 1.5%}; persistence ∈ {1, 2 bars}.
No re-parameterisation after OOS. No routing change from this note.
