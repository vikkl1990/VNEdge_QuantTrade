# Pre-registration: per-TF exit card replacing the 48-bar max hold — 2026-09-17

Follows the duration cuts in `storage/research/scanner_lab/duration/` (see
`docs/SCANNER_CLUSTER_ANALYSIS_TODO_20260915.md`). A fixed 48-bar cap is not
a hold policy: on 5m it is "give up after 4 hours", on 1h it is two days.
The clocks that matter — when 1R prints, when the stop prints, when MFE
stops rising — are already measured per TF from the live path.

No detector edits. Same entries as the scanner-lab dumps; only the exit changes.

## The card (v1)

| TF | Progress kill | Time cap if never 1R | After 1R |
|---|---|---|---|
| 5m | mfe < 0.3R by bar 6; mae >= 0.7R on bar 1–2 → flatten (late fill) | 12 bars | take 1R |
| 15m | mfe < 0.3R by bar 4 | 16 bars | CE trail (peak − 1.0R) + EMA21 invalidation |
| 1h | mfe < 0.3R by bar 4; mfe < 0.8R by bar 8 | 24 bars | CE trail (peak − 1.0R), no 1.5R default |

The −1R pattern stop outranks all of the above on every bar. Kills exit at
that bar's close. `mfe`/`mae` are in-life values only (bars up to and
including the current closed bar) — never the post-exit peak.

Fees: taker in / taker out, Scalper Offer credited per exit leg by hold time,
identical to the scanner-lab diagnosis.

## Pass rule

Fit 2026-03-01 → 06-30, judge 07-01 → 09-17, BTC and ETH separately, 21 live
scanners pooled per TF and per scanner. **Pass** if OOS avg R under the card is
higher than OOS avg R under the old exits (48-bar tp1; and the 48-bar trail)
on the same entries. Fire rate is unchanged by construction. If 5m is still
red under the card, that confirms hold time was never the 5m fault.

Not allowed: choosing caps by maximising in-sample hold on winners
(winners' T_MFE in the dumps is measured after exit and would say "hold 40").

## Results — 2026-09-17 (appended; card v1 unchanged)

Runner: `scripts/exit_card_lab.py`. Same entries as the scanner-lab dumps; OOS = Jul–Sep;
avg R after taker fees. Per-trade outputs under `storage/research/scanner_lab/duration/*_exit_card_*`.

| symbol | TF | old tp1 | old trail | CARD | Δ vs tp1 | Δ vs trail | verdict |
|---|---|---|---|---|---|---|---|
| BTC | 5m  | −0.177 | −0.141 | −0.125 | +0.053 | +0.016 | pass, still red |
| BTC | 15m | −0.142 | −0.074 | −0.119 | +0.024 | −0.045 | FAIL vs trail |
| BTC | 1h  | −0.119 | −0.036 | −0.007 | +0.112 | +0.029 | PASS |
| ETH | 5m  | −0.141 | −0.129 | −0.106 | +0.035 | +0.023 | pass, still red |
| ETH | 15m | −0.100 | −0.042 | −0.072 | +0.028 | −0.030 | FAIL vs trail |
| ETH | 1h  | −0.085 | +0.038 | −0.019 | +0.066 | −0.058 | FAIL vs trail |

- The card beats the fixed 1.5R target on every TF and symbol; it does **not** beat the plain
  CE trail on 15m (both) or ETH 1h. Mixed → v1 is not a pass as a whole.
- 5m stays red under the card (−0.11…−0.13). Confirmed: hold time was never the 5m fault.
- 15m failure mode: the bar-4 progress kill fires on ~half of all 15m trades (BTC 2,552/5,040;
  ETH 2,290/5,472) while the 15m clock in the duration cuts puts p50 T_1R at 14–16 bars —
  the kill runs ahead of the move's own clock. A 15m kill aligned to that clock is a v2
  pre-registration, not an edit here.
- 1h: BTC card ≈ breakeven after fees (−0.007) from −0.119; `structure_bounce` 1h +0.34R
  (BTC, n=71) / +0.25R (ETH, n=73), `rsi_extreme` BTC +0.14R, `volume_surge` ETH +0.31R.
  ETH 1h fails only because the plain trail was already +0.038 there and the kills shave it.
- `trend_pb` diagnostic entries under the card: no improvement (n ≤ 29; the family already
  carries its own progress/invalidation contract).

## Optional variant (not v1)

Fast/slow tape: last 3 bars directional with body sum >= 1.2 ATR → use a
short cap (5m 8, 15m 10, 1h 6) then trail; mixed closes with bodies
< 0.3 ATR → progress kill only, never extend the cap.
