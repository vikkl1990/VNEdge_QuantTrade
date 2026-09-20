# Pre-registration: order-flow features from Delta trade archives — 2026-09-18

Written before the screen runs. Input: Delta monthly futures trade archives
(ETHUSD Jan–Sep 2026 minus March; BTCUSD Jan–Sep 2026), ~40M trades with
price, size, timestamp and `buyer_role` (taker = buy-initiated). Aggregated to
1-minute bars by `scripts/orderflow_aggregate.py`; features and screen in
`scripts/orderflow_lab.py`.

## Why this is different from everything screened so far

Every prior pass (21 scanners, two rebuilt families, the structural edge map,
cross-venue lag, funding) used OHLCV only and found nothing stable and
fee-clearing. Trade-level data adds the one axis candles cannot: who was the
aggressor, how large, how fast. If there is an exploitable signal on this
venue at this size, it is most likely here.

## Features (all on closed bars, data <= t), on 15m and 1h

aggressor imbalance `delta_ratio`; `delta_z` (rolling 96 / 48 bars); real CVD
divergence over 4 and 12 bars (price return sign vs summed delta sign);
large-trade imbalance `big_imb` and share `big_share` (size >= month's 95th
percentile); aggressor count share `n_buy_share`; `intensity` (trades vs
same-hour median); `avg_size_rel`; `absorption` (delta_z < −1 with close >= open,
or > +1 with close <= open); `vwap_dev` ((close − bar VWAP)/ATR).

Targets: forward close-to-close return — 15m: 1h, 4h; 1h: 4h, 12h, 24h.

## Results — 2026-09-18 (appended; screen unchanged)

Aggregated: ETHUSD 44,991,243 trades / 295,930 minutes; BTCUSD 64,586,185 trades / 323,309
minutes; 2026-01-01 → 09-01 (ETH March absent). Screen: 15m and 1h, fit 90d, 5 judge folds of 30d.

| symbol | tf | cells | stable | random expectation | fee-clearing candidates |
|---|---|---|---|---|---|
| ETH | 15m | 94 | 4 | ≈12 | 0 |
| ETH | 1h | 141 | 1 | ≈18 | 0 |
| BTC | 15m | 94 | 6 | ≈12 | 0 |
| BTC | 1h | 141 | 4 | ≈18 | 2 (one condition, two horizons) |

Stable counts are at or below chance on both symbols at both timeframes. **No candidate under
the pre-registered both-symbols rule.**

The one lead: BTC 1h `cvd_div_12 = −1` — price up over the last 12 bars while summed aggressor
delta was net selling — followed by a **positive** 12h/24h return (+0.30% / +0.44% in judge,
4/5 and 5/5 folds, fit +0.32% / +0.41%, n_fit 216, n_judge 540 overlapping bars ≈ 45 / 22
independent events). That is the opposite of the textbook "bearish divergence → short": a
rise against net selling reading as absorption/continuation. It does not replicate on ETH
(see the side-by-side printed in the session log). Given 141 cells, ~18 stable by chance and
two correlated horizons of one condition, this is a lead for a trade-level pre-registration on
**additional** BTC trade history (2025 archives), not a result. Nothing routed.

Baselines: judge-period forward returns are mildly positive on both symbols (ETH 24h +0.14%,
BTC +0.11%) — drift, not signal.

## Screen

Quantile bins (5) from the FIT period only; categorical for the divergence /
absorption flags. Fit = first 90 days; judge folds = 30 days each. Stable =
same sign in >= 4/5 judge folds (or folds − 1 when fewer) AND in fit. Candidate
= stable AND |judge mean| > 2 × 0.118% (taker round trip), on BOTH symbols.
Random expectation for the stable count is printed next to the result so the
screen is judged against chance, not zero. No cell is re-parameterised after
its judge result is seen. A candidate gets its own trade-level pre-registration
(entry, stop, exit, cost) before anything is routed.
