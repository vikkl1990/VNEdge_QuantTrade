# Pre-registration: the 1h/15m brew — 2026-09-17

Written before the runs. Grid is fixed here; every cell gets reported.

## What the clocks license (from `duration_lab` / `exit_card_lab`)

- 5m: live R ≈ 5× a 5m ATR; two-thirds of trades touch neither 1R nor the stop
  in 4h; fees 0.18R/trade. **No 5m cells.**
- 15m: T_1R ≈ T_stop ≈ 15 bars; a bar-4 progress kill runs ahead of the move.
- 1h: p50 MFE 1.6–2.2R, 68–75% reach 1R, the stop clock is one bar ahead of the
  1R clock, the peak forms after the fixed 1.5R exit. **Stop placement and a
  trailing exit are the levers; hold time is not.**
- `trend_pb` geometry travels (81–83% reach 1R, p50 T_1R 5–6 bars on 15m) but
  the pullback-extreme stop is hit at bar ~4 on 45% of trades while MAE p25
  before the peak is 1.2R — the stop is inside the path's own noise.

## Grid (one degree of freedom per cell; BTC and ETH; fit Mar–Jun, judge Jul–Sep)

**G1 — existing scanners, pattern stop instead of the %-clamp** (`scanner_lab --sl-mode pattern`):
stop = the scanner's own `stop_loss` (structure zone / sweep wick / BB mid / …),
TP by the scanner's `tp_rr`, no 0.55–0.95% clamp, no liquidation check. TFs 15m, 1h.
Then the exit card (`exit_card_lab --dump …`) on those entries.
Read per scanner: OOS avg R after taker fees vs the same scanner under the live clamp.

**G2 — `trend_pb` v2 stop** (`family_lab --stop-mode impulse`): stop = impulse bar low
− 0.1 ATR (long; mirror short) instead of pullback extreme. Everything else v1.

**G3 — `trend_pb` regime labeller** (`family_lab --regime-source ema_slope`):
`slope = (EMA21[t] − EMA21[t−20]) / ATR` on the traded TF; `trending_up` if slope > +1.0,
`trending_down` if < −1.0, else `sideways` — the ±1.0 ATR/20-bar convention
`structure_bounce` already uses for with/counter-trend scoring. Replaces the
confirm-frame `MarketRegimeDetector` only for the family gate. Run with v1 stop
and with G2 stop (4 cells per symbol).

## Results — G1/G2/G3 (2026-09-17, appended; grid unchanged)

Runs: `scanner_lab --sl-mode pattern` (BTC/ETH 15m,1h) → `exit_card_lab --dump`;
`family_lab` × {stop impulse, regime ema_slope, both} × {BTC, ETH}. Candidate bar
applied to every scanner × tf × exit × stop cell from the exit-card outputs (clamp and
pattern), both symbols.

- **No cell meets the candidate bar on either symbol.** The nearest miss, and the only
  cross-symbol, cross-exit, cross-stop positive, is `structure_bounce` 1h: OOS +0.34R
  (BTC, n=71, card) / +0.27R (trail); ETH +0.25R / +0.31R (n=73); still positive under
  pattern stops (+0.17/+0.21 BTC, +0.14/+0.16 ETH). It fails on **IS avg −0.25…−0.39R**
  (OOS-only) and **top-5 share 1.4–2.9** (five trades carry the OOS total).
- G1 pattern stops are strictly worse than the live clamp on 15m (BTC OOS −0.44 vs −0.14;
  ETH −0.29 vs −0.10 under tp1): the scanners' own invalidation levels sit inside the
  path's noise (same failure as `trend_pb`'s pullback stop). `vwap_mean_revert` 1h at
  −4.2R/trade is pathological (2σ-band stop → near-zero risk). **The 0.55–0.95% clamp is
  protective on 15m/1h, not the fault.**
- G2/G3 `trend_pb`: the EMA-slope labeller lets ~6× more fires through the gate (BTC 15m
  45 vs 7) and they lose (OOS −0.18…−0.46R); every 1h "pass" is n=5 with top-5 share 1.0.
  Family closed.
- 5m: untouched by design; the earlier card result stands (still red).

## G4 — sample extension (pre-registered before running)

Only one cell survived on direction: `structure_bounce` 1h with a trailing exit. Its
failure is statistical (OOS-only, outlier-carried), not directional. Extend the sample
rather than the grid: 500 days of 1h (Delta serves it), BTC and ETH, live clamp,
`scanner_lab --timeframes 1h --days 500 --tag _1h500d`, then `exit_card_lab --dump`, then
**rolling folds** (fit 120d / judge 60d, stepped) on trail and card exits. Candidate iff
avg R > 0 after taker fees in ≥ 4 of the judge folds on both symbols, with top-5 share
< 0.6 pooled. Everything else in the extended sample is reported but was not
pre-registered as a candidate.

### G4 results — 500 days of 1h (2025-05-05 → 2026-09-17), 6 judge folds of 60d

`scripts/fold_lab.py` on `duration/{BTC,ETH}_USDT_exit_card_1h500d_trades.csv`.
**No candidate on either symbol.**

- `structure_bounce` 1h: BTC positive in 2 of 6 folds (pooled −0.10R trail / −0.10 card,
  n=367); ETH 2 of 6 (−0.04 / −0.08, n=408). The two positive folds are the last two —
  the 200-day "+0.34R OOS" was those four months, not the scanner.
- Nearest misses, ETH only: `ema_momentum` 1h trail 5/6 folds, pooled +0.21R, n=182,
  top-5 0.77 (bar is < 0.6); `volume_surge` 1h card 4/6, +0.15R, top-5 0.72. Both are
  2/6 and negative on BTC. Symbol-bound and outlier-heavy → reported, not candidates.
- Pooled 1h, all live scanners: BTC tp1 −0.15 IS / −0.12 OOS, trail −0.13 / −0.04,
  card −0.14 / −0.01; ETH tp1 −0.12 / −0.08, trail −0.04 / +0.04, card −0.06 / −0.02.
  The trailing/card exits are a consistent ~0.1R improvement over the fixed target on
  1h across 500 days — a real, exit-side result — but from a negative base.

**Verdict of the brew:** across 21 scanner definitions, one new family with three
pre-registered variants, three stop definitions, seven exit models, two fee assumptions
and 500 days × 2 symbols × 6 folds, no entry definition in this codebase carries a
walk-forward edge on BTC/ETH after Delta taker fees at $30k notional. What is robust:
(1) 1h ≫ 15m ≫ 5m for travel-vs-cost; (2) the 0.55–0.95% clamp is the right stop scale
on 1h (pattern and 1.5×ATR stops are both worse); (3) trail + progress kills beat fixed
targets on 1h; (4) fees are 0.11–0.18R/trade and maker entry is worth ~+0.05R.

### G5 — beyond entries: the forward-return surface, cross-venue lag, funding (2026-09-17)

`scripts/edge_map_lab.py` (500 days of 1h, both symbols, fit 120d, 6 judge folds of 60d):
forward 4h/12h/24h return conditioned on UTC hour, weekday, funding-window proximity, position
in the 24h range, past 1h/4h/24h returns, ATR percentile, volume vs same-hour median, RSI14, and
(ETH) BTC's past 1h/4h return. Stable cells (same sign in ≥5/6 judge folds and in fit):
**ETH 7 of 267, BTC 12 of 237 — fewer than the ~11% a random surface would produce.** None clears
2× the 0.118% round trip on both symbols; the one BTC cell (Thursday 24h short, +0.19% net,
5/6) is a ~54-Thursday weekday effect absent on ETH. Asia-range (00–08 UTC) breakout at London on
15m: 38–39% wins, negative in fit and judge on both symbols.

Cross-venue lead-lag, Binance ETHUSDT perp vs Delta ETHUSD, 10,080 aligned 1m bars: corr 0.996 at
lag 0, |basis| σ 0.005%, no minute with a gap > 0.08%; basis mean-reversion worth 0.01–0.03% per
5m against 0.118% fees. Delta's price is Binance's price.

Funding (Delta `FUNDING:` candles, 500 days, same folds): ETH 0 stable cells of 36, BTC 2 of 48,
none fee-clearing. Delta funding is capped at 0.01%/8h and sits at the cap most of the time
(ETH median = p90 = 0.0100), so "extreme funding" is degenerate here.

### G6 — scalper and swing angles on the existing scanners (2026-09-18)

**Scalper** (`exit_card_lab --mode scalper`): every scanner's 5m entries, take +0.35% / stop −0.60%
in price, forced flat at 6 bars so the exit is always inside the Scalper Offer window (exit fee 0);
entry taker ($17.7) and maker ($7.1). BTC/ETH 200 d, fit Mar–Jun / judge Jul–Sep. **All red with a
taker entry** (−$10…−$30 per trade, both symbols): 72–78% of entries touch neither +0.35% nor −0.6%
inside 30 minutes (takes 11–36%). Maker entry adds ≈ $10.6 and lifts only the best rows to ≈ $0
(`fvg_fill` BTC +$0.2 on 48 trades). No scalp edge in these entries.

**Swing** (`scanner_lab --timeframes 4h --days 500 --max-hold-bars 30`, 1d confirm; then
`exit_card_lab --mode swing` and pooled 4 × 60-day folds): BTC 4h pooled trail −0.04R, positive in
1/4 folds; ETH 4h pooled trail +0.07R, 2/4 folds, top-5 share 0.61 — the +0.30R OOS headline was the
last fold plus ETH's Jul–Sep drift. 1h entries under a two-day swing card: no candidate (ETH
`ema_momentum` 5/6 folds +0.19R but top-5 0.84; BTC nothing). Per-regime splits are inconsistent
across symbols (ETH `mean_reversion` +0.30R, BTC `mean_reversion` −0.21R).

Verdict: with the scanners we have there is neither a scalper edge (entries don't move inside the
free-exit window) nor a swing edge (entries have no multi-day direction). Nothing routed.

### G7 — fee model removed: the gross view (2026-09-19)

Same entries and exits, fees set to zero (the dumps carry `fee_r` per trade; `alt_*` are gross R).
Pooled gross tp1 avg R: BTC 5m −0.01 / 15m −0.03 / 1h −0.06; ETH +0.00 / +0.00 / +0.02; gross trail
BTC ≈ 0.00, ETH +0.01 / +0.05 / +0.10. Fees are 0.11–0.16 R per trade. **Without fees the book is
breakeven; fees are the whole net loss** — but breakeven gross means the entries carry ≈ no
information, not that a cost fix unlocks an edge.

Gross cells positive in both windows with top-5 share < 0.6 (per symbol): ETH 15m
`trend_continuation` (gross +0.12–0.17 R, net −0.00…+0.05), ETH 1h `volume_surge` trail (gross +0.27,
**net +0.16 R**, n=260, 4/6 folds, top-5 0.44), ETH 1h `bb_band_walk` trail (gross +0.20, net +0.09,
4/5), BTC 1h `momentum_surge` (gross +0.12–0.14, net +0.00…+0.03, 5/6; routed to no regime live),
BTC 5m `post_impulse` / ETH 5m `liquidity_sweep` (gross +0.02–0.03, fee-eaten). **No gross cell is
positive on both symbols.** Full table: `storage/research/scanner_lab/duration/gross_view.csv`.

## Pass / kill (per cell, OOS, taker fees)

Candidate only if, on **both** symbols: avg R > 0, n_oos ≥ 30, top-5 share < 0.6,
IS avg R not < −0.1 (no OOS-only edges), fires ≤ 3% of bars. Otherwise: reported, not shipped.
No cell may be re-parameterised after its OOS is seen. No routing change from this note;
a passing cell gets its own sign-off PR.

#### G7b — every scanner, fees removed (2026-09-19)

Full per-scanner table (gross tp1 / trail / ladder, fee R, net, win%, IS/OOS gross, positive folds,
top-5 share): `storage/research/scanner_lab/duration/gross_view_all_scanners.csv`
(5m/15m from the 200d dumps; 1h/4h from the 500d dumps). Gross trail R by cell, BTC | ETH:

| scanner | 5m | 15m | 1h | 4h |
|---|---|---|---|---|
| bb_band_walk | +0.01 / +0.04 | −0.05 / +0.00 | +0.03 / **+0.20** | **+0.23 / +0.54** |
| bb_squeeze | −0.05 / −0.03 | −0.05 / −0.02 | +0.03 / +0.06 | **+0.21 / +0.36** |
| candlestick_reversal | +0.01 / +0.01 | +0.00 / +0.05 | −0.00 / +0.08 | +0.03 / +0.04 |
| ema_momentum | −0.01 / −0.03 | −0.08 / +0.01 | −0.02 / **+0.32** | +0.10 / **+0.40** |
| liquidity_sweep | −0.01 / +0.03 | +0.02 / +0.12 | −0.05 / +0.09 | **+0.29** / +0.10 |
| momentum_ride | −0.01 / +0.02 | **+0.18** / +0.09 | — | — |
| momentum_surge | −0.01 / +0.03 | +0.08 / **+0.18** | **+0.13** / +0.01 | −0.19 / +0.07 |
| post_impulse | +0.05 / +0.06 | −0.06 / +0.10 | +0.07 / **+0.15** | **+0.13 / +0.20** |
| rsi_divergence | +0.05 / +0.03 | +0.01 / −0.04 | −0.03 / +0.01 | −0.32 / — |
| rsi_extreme | +0.04 / +0.01 | +0.03 / −0.04 | −0.05 / −0.06 | −0.00 / +0.15 |
| structure_bounce | +0.06 / +0.05 | +0.03 / +0.11 | +0.01 / +0.07 | **+0.15** / +0.10 |
| supertrend_flip | −0.04 / −0.01 | −0.05 / +0.00 | +0.02 / **+0.17** | +0.11 / **+0.21** |
| trend_continuation | −0.02 / −0.02 | +0.06 / **+0.17** | −0.05 / **+0.18** | +0.06 / **+0.24** |
| volume_surge | +0.02 / +0.02 | −0.04 / +0.04 | +0.01 / **+0.27** | — |
| vwap_mean_revert | −0.06 / −0.01 | −0.05 / −0.04 | −0.03 / +0.00 | +0.00 / +0.10 |
| bos_choch / fvg_fill | +0.15 / −0.05 · +0.00 / +0.04 | — · +0.00 / +0.12 | — | — |

Bold = gross clears the 0.11–0.16R fee. Reading:

- **5m: every scanner is within ±0.06R of zero gross on both symbols** (fees 0.14–0.17R). No 5m
  scanner is worth its fee even with the fee removed; the entries are noise at this scale.
- **15m: gross clears the fee on one symbol only** (`momentum_ride` BTC, `momentum_surge` /
  `trend_continuation` / `liquidity_sweep` ETH), and only under the trail exit; tp1 gross ≤ +0.12R.
- **1h: the ETH book is gross-positive under trail for 12 of 14 scanners** (+0.32 `ema_momentum`,
  +0.27 `volume_surge`, +0.20 `bb_band_walk`, +0.18 `trend_continuation`, +0.17 `supertrend_flip`,
  +0.15 `post_impulse`); the BTC 1h book is ≈ 0 for every scanner except `momentum_surge` (+0.13,
  6/6 folds, top-5 0.89). Same scanners, same code, opposite symbol → the ETH surplus is ETH's
  2025-26 trend, not the scanner.
- **4h: three cells are gross-positive on both symbols under trail** — `bb_band_walk` (+0.23/+0.54),
  `bb_squeeze` (+0.21/+0.36), `post_impulse` (+0.13/+0.20). None is a candidate: n = 57–83 per
  symbol, 2/4–3/4 folds, and **top-5 share 1.0–28.7** (five trades exceed the whole OOS sum;
  gross tp1 is −0.12…+0.09 on the same entries). The 4h trail R is the live 0.55–0.95% clamp
  applied to a 4h bar, so one held trend pays 5–10R and the mean is that trend.
- Net of the actual fee, no scanner × TF is positive on both symbols at any exit (G7 stands).
