# VN Edge — Regime & Scanner Architecture Review
## Peer Review Document | April 4, 2026

---

## 1. REGIME DETECTION SYSTEM

### 1.1 Architecture: Dual-Layer Detection

Two regime detectors exist. The **advanced detector** is the primary (called with 5m DataFrame), falling back to the **simple detector** when data is insufficient (<100 bars).

#### Advanced Detector (`strategies/regime.py` — `MarketRegimeDetector`)
- **Inputs:** 5m OHLCV DataFrame (100+ bars)
- **Indicators computed:** ADX(14), ATR percentile(14/100), EMA(50) slope(3-bar), BB bandwidth percentile(20/100), volume ratio vs 20-bar SMA
- **Outputs:** 9 regime types + confidence score (0-1)

#### Simple Detector (`strategies/regime_filter.py` — `RegimeFilter.detect_regime()`)
- **Inputs:** Pre-computed indicator dict (ema_8, ema_21, ema_50, close, atr, bb_bandwidth)
- **Outputs:** 5 regime types (trending_up/down, volatile, quiet, ranging)
- **Used as fallback** when DataFrame unavailable

### 1.2 Advanced Detector Classification Logic (Priority Order)

| Priority | Regime | Condition | Confidence | Rationale |
|----------|--------|-----------|------------|-----------|
| 1 | **low_liquidity** | `volume_ratio < 0.15` (15% of 20-bar SMA) | 0.85 | Thin volume = no reliable price discovery. **No trading.** |
| 2 | **trending_up/down** (strong) | `atr_pct >= 85 AND adx >= 40` | 0.80 | Extreme ATR + strong trend = high-conviction directional move |
| 3 | **high_volatility** | `atr_pct >= 85 AND adx < 40` | 0.75 | Extreme ATR without trend = whipsaw danger |
| 4 | **breakout** (squeeze) | `bb_squeeze AND adx > 30` | 0.70 | BB compression releasing with confirmed trend strength |
| 5 | **breakout** (expansion) | `bw_pctile >= 92 AND adx > 28` | 0.60 | Top 8% bandwidth expansion with moderate ADX |
| 6 | **trending_up/down** | `adx >= 30 AND abs(ema_slope) >= 0.15 AND DI aligned` | 0.55-0.95 | Confirmed trend: strong ADX + clear EMA slope + DI agreement |
| 7 | **mean_reversion** | `adx < 30 AND atr_pct < 20 AND bb_squeeze` | 0.65 | Dead market: no trend, low vol, compressed bands |
| 8 | **sideways** | Everything else | 0.50 | Default fallback — moderate activity, no clear direction |

**Key thresholds (hardened after Apr 3-4 loss analysis):**
- `ADX_TREND_THRESHOLD = 30` (raised from 25 — ADX 25-29 is NOT a confirmed trend)
- `BB_EXPANSION_PERCENTILE = 92` (raised from 80 — only top 8% bandwidth = genuine breakout)
- `VOLUME_LOW_LIQUIDITY = 0.15` (lowered for Delta India's thinner order books)
- Removed "ambiguous trend" fallback — ADX>=30 without clear EMA slope now falls to sideways

### 1.3 Simple Detector Fallback Logic

| Priority | Regime | Condition |
|----------|--------|-----------|
| 1 | **trending_up** | `ema8 > ema21 > ema50 AND close > ema8 AND bb_bw <= 0.04` |
| 2 | **trending_down** | `ema8 < ema21 < ema50 AND close < ema8 AND bb_bw <= 0.04` |
| 3 | **trending** (drift) | Partial EMA alignment + `ema50_displacement > 0.3%` |
| 4 | **volatile** | `bb_bandwidth > 0.04` (without EMA alignment) |
| 5 | **quiet** | `bb_bw < 0.008 AND no drift AND no EMA ordering` |
| 6 | **ranging** | Everything else |

### 1.4 Estimated Time Distribution

| Regime | Est. % of Time | Scanner Count | Risk Level |
|--------|---------------|---------------|------------|
| sideways | 35-45% | 9 scanners | Medium |
| trending_up/down | 20-30% | 11 scanners | Low |
| high_volatility | 5-10% | 6 scanners | High |
| low_liquidity | 5-15% | 0 (blocked) | N/A |
| breakout | 2-5% | 7 scanners | Medium |
| mean_reversion | 2-5% | 2 scanners | High |
| quiet (simple fallback) | 5-10% | 3 scanners | Low |

---

## 2. SCANNER SYSTEM

### 2.1 Active Scanners (12 routed, 2 disabled, 4 unrouted)

| Scanner | Category | Sides | Max Score | Regimes Active In | All-Time WR | Trades |
|---------|----------|-------|-----------|-------------------|-------------|--------|
| **structure_bounce** | Structure | L+S | ~100 | ALL (except low_liq) | 73.5% | 980 |
| **liquidity_sweep** | ICT | L+S | ~95 | ALL (except low_liq) | 36.8% | 19 |
| **bos_choch** | ICT | L+S | ~85 | trend/break/range/side/vol/hv | 0.0% | 1 |
| **trend_continuation** | Trend | L+S | ~95 | trend/breakout | N/A | 0 |
| **ema_momentum** | Trend | L only | ~90 | trend/break/vol/hv | N/A | 0 |
| **cvd_divergence** | Volume | L+S | ~80 | trend/range/side/mr | N/A | 0 |
| **rsi_divergence** | Reversal | L+S | ~85 | trend/range/side/vol/hv/mr | N/A | 0 |
| **vwap_mean_revert** | Reversal | L+S | ~100 | trend/range/side | N/A | 0 |
| **rsi_extreme** | Reversal | L+S | ~100 | ALL (except low_liq/mr) | N/A | 0 |
| **bb_squeeze** | Breakout | L+S | ~100 | trend/break/range/side | N/A | 0 |
| **post_impulse** | Trend | L only | ~100 | trending_up only | N/A | 0 |
| **order_block_entry** | ICT | L+S | ~95 | break/range/side/vol/hv | N/A | 0 |
| ~~supertrend_flip~~ | Disabled | — | — | — | 26% | shadow |
| ~~momentum_surge~~ | Disabled | — | — | — | 39% | shadow |
| *momentum_ride* | Unrouted | L only | ~100 | Not in routing table | — | 0 |
| *bb_band_walk* | Unrouted | L+S | ~100 | Not in routing table | — | 0 |
| *vwap_bounce* | Unrouted | L+S | ~75 | Not in routing table | — | 0 |
| *simple_bias* | ML-only | — | 0 | Learning mode only | — | 0 |

### 2.2 Scanner Confidence Score Breakdown

#### structure_bounce (Primary — 98% of all trades)
| Component | Points | Condition |
|-----------|--------|-----------|
| S/R rejection base | +30 | Price at support/resistance with rejection wick |
| Wick quality | +10-15 | Wick > 0.5x ATR |
| Inside structure zone | +5 | Price within S/R zone |
| Volume | +5-15 | Relative volume > 1.0x |
| Confirmation candle | +5 | Follow-through candle |
| Level strength | +5-15 | Untested/strong level |
| Touch count | +10 | Multiple touches at level |
| HTF alignment | +15 | 15m bias agrees |
| Multi-structure confluence | +10 | Multiple S/R levels cluster |
| **Typical achievable** | **55-90** | |

#### liquidity_sweep (ICT — Sweep+Reclaim)
| Component | Points | Condition |
|-----------|--------|-----------|
| Sweep+reclaim base | +38 | Price raids level, fails, snaps back |
| Sweep depth | +10-15 | Deep sweep > 1.0x ATR |
| MSS confirmation | +20 | Market Structure Shift after sweep |
| Displacement | +5-10 | Strong displacement candle |
| Volume | +5-10 | Volume > 1.0x on displacement |
| HTF alignment | +15/-5 | 15m agrees / conflicts |
| **Typical achievable** | **60-90** | |

#### vwap_mean_revert (P3 enhanced — was score-capped)
| Component | Points | Condition |
|-----------|--------|-----------|
| VWAP band touch | +35 | Price at 1st std dev band |
| Below 2nd std dev | +10 | Extreme deviation |
| Volume | +10 | Volume > 1.0x |
| RSI extreme | +10 | RSI < 40 (long) or > 60 (short) |
| HTF alignment | +15 | 15m agrees |
| MACD alignment (P3) | +10 | MACD histogram agrees |
| Strong reversal candle (P3) | +8 | Body > 55% of range |
| 5m confirmation (P3) | +5 | 5m bias agrees |
| Supertrend (P3) | +5 | Supertrend agrees |
| **Typical achievable** | **50-88** | (was capped at 65 pre-P3) |

#### rsi_extreme (P5 — newly activated)
| Component | Points | Condition |
|-----------|--------|-----------|
| RSI oversold/overbought | +30 | RSI < 35 turning up OR > 65 turning down |
| Extreme level | +10 | RSI < 25 or > 75 |
| Candle confirmation | +10 | Bullish/bearish candle |
| Rejection wick | +10 | Wick > 0.5x body |
| Volume | +5-15 | Volume > 0.8x |
| Supertrend | +10 | Supertrend agrees |
| BB %B extreme | +10 | %B < 0.1 or > 0.9 |
| HTF alignment | +10 | 15m agrees |
| 5m alignment | +5 | 5m agrees |
| **Typical achievable** | **50-95** | |

#### bb_squeeze (P5 — newly activated)
| Component | Points | Condition |
|-----------|--------|-----------|
| Squeeze breakout direction | +30 | %B > 0.75 (long) or < 0.25 (short) |
| Volume surge | +10-20 | Volume > 1.0x (required) |
| MACD confirmation | +10 | MACD histogram aligned |
| HTF alignment | +15 | 15m agrees |
| Strong body candle | +10 | Body > 60% of range |
| **Typical achievable** | **55-85** | |

---

## 3. REGIME → SCANNER ROUTING TABLE

```
┌─────────────────────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┐
│ Regime              │ SB  │ LS  │ BC  │ TC  │ EM  │ CVD │ RSI │ VMR │ RE  │ BS  │ PI  │ OBE │
│                     │     │     │     │     │     │ div │ div │     │ ext │ sqz │     │     │
├─────────────────────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┤
│ trending_up         │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │     │
│ trending_down       │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │     │     │
│ breakout            │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │     │     │     │     │  ✅ │     │  ✅ │
│ ranging/sideways    │  ✅ │  ✅ │  ✅ │     │     │  ✅ │  ✅ │  ✅ │  ✅ │  ✅ │     │  ✅ │
│ volatile/high_vol   │  ✅ │  ✅ │  ✅ │     │     │     │  ✅ │     │  ✅ │     │     │  ✅ │
│ mean_reversion      │  ✅ │  ✅ │     │     │     │     │     │     │     │     │     │     │
│ quiet               │  ✅ │  ✅ │     │     │     │     │     │     │  ✅ │     │     │     │
│ low_liquidity       │     │     │     │     │     │     │     │     │     │     │     │     │
└─────────────────────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┘

Legend: SB=structure_bounce  LS=liquidity_sweep  BC=bos_choch  TC=trend_continuation
        EM=ema_momentum  CVD=cvd_divergence  RSI=rsi_divergence  VMR=vwap_mean_revert
        RE=rsi_extreme  BS=bb_squeeze  PI=post_impulse  OBE=order_block_entry
```

---

## 4. POSITION SIZING BY REGIME

| Regime | Size Mult | SL Mult | EV Adj | Min Confidence | Counter-Trend |
|--------|-----------|---------|--------|----------------|---------------|
| trending_up/down | 1.0x (1.2x strong) | 1.0x | -0.05 | 60 | Hard block |
| breakout | 1.1x (1.32x strong) | 1.1x | -0.03 | 65 | Hard block |
| ranging/sideways | 0.7x | 0.85x | 0.00 | 70 | Allowed |
| volatile/high_vol | 0.5x | 1.3x | +0.05 | 75 | Hard block |
| mean_reversion | 0.8x | 0.9x | 0.00 | 68 | Allowed |
| quiet | 0.7x | 1.0x | 0.00 | 65 | Allowed |
| low_liquidity | 0.0x | — | +0.10 | — | Blocked |

---

## 5. MULTI-TIMEFRAME CHAIN

```
4h SESSION  →  1h MACRO  →  15m CONFIRMATION  →  5m ENTRY  →  1m TRIGGER
   │              │              │                   │             │
   │              │              │                   │             └─ Primary candle close
   │              │              │                   └─ Trend scanners use 5m
   │              │              └─ ALL scanners try 15m first (+10 quality bonus)
   │              └─ Macro bias: veto counter-trend entries (-15 penalty)
   └─ Session bias: combines with 1h (aligned=keep, conflict=neutral)
```

**Note:** P2 (4h override of ranging→trending) is **disabled** — proven destructive (23% WR over 39 trades).

---

## 6. PERFORMANCE DATA

### 6.1 Daily Performance
| Date | Trades | WR | Gross | Fees | Net | Fee Drag |
|------|-------:|---:|------:|-----:|----:|---------:|
| Mar 24 | 45 | 93.3% | $169 | $55 | $+114 | 32.4% |
| Mar 25 | 74 | 78.4% | $130 | $92 | $+38 | 70.5% |
| Mar 26 | 26 | 88.5% | $63 | $32 | $+31 | 51.4% |
| Mar 27 | 52 | 84.6% | $181 | $62 | $+119 | 34.1% |
| Mar 28 | 67 | 74.6% | $145 | $85 | $+60 | 58.6% |
| Mar 29 | 67 | 58.2% | $90 | $87 | $+4 | 96.2% |
| Mar 30 | 59 | 83.1% | $197 | $72 | $+125 | 36.7% |
| Mar 31 | 28 | 75.0% | $184 | $62 | $+123 | 33.4% |
| Apr 1 | 203 | 78.8% | $901 | $338 | $+563 | 37.5% |
| Apr 2 | 189 | 79.4% | $816 | $316 | $+501 | 38.7% |
| **Apr 3** | **176** | **50.0%** | **$395** | **$263** | **$+133** | **66.5%** |
| **Apr 4** | **14** | **21.4%** | **$19** | **$24** | **$-6** | **129.9%** |

### 6.2 Scanner Performance (All-Time)
| Scanner | Trades | WR | PnL | Avg PnL |
|---------|-------:|---:|----:|--------:|
| structure_bounce | 980 | 73.5% | $+99.62 | $+0.10 |
| liquidity_sweep | 19 | 36.8% | $-0.33 | $-0.02 |
| bos_choch | 1 | 0.0% | $-1.29 | $-1.29 |

### 6.3 Known Issues
- **Scanner concentration:** 98% of trades from structure_bounce. Other scanners are routed but rarely trigger due to strict conditions.
- **Apr 3 degradation:** max_age change (15m→3m) killed 18 trades. Reverted to 5m/8m.
- **Apr 3-4 regime changes:** Advanced detector was too permissive (ADX=25, bw_pctile=80). Hardened to ADX=30, bw_pctile=92.
- **Fee drag:** 38-40% on good days, spikes to 66%+ when WR drops below 60%.

---

## 7. CHANGES APPLIED (Apr 3-4)

| Change | Status | Impact |
|--------|--------|--------|
| P0: Advanced regime detector | Active (hardened) | Detects breakout, mean_reversion, high_vol, low_liquidity |
| P1: Ranging scanners 4→9 | Active | +cvd_divergence, bos_choch, order_block_entry, rsi_extreme, bb_squeeze |
| P2: 4h override ranging→trending | **DISABLED** | Proven destructive: 23% WR over 39 trades |
| P3: vwap_mean_revert score ceiling | Active | +MACD, candle quality, 5m, supertrend (+28 max) |
| P4: Consolidated routing tables | Active | Config alignment between two permission systems |
| P5: Activated rsi_extreme, bb_squeeze, post_impulse | Active | 3 scanners added to routing table |
| ADX trend threshold 25→30 | Active | Prevents false trend classification |
| BB expansion pctile 80→92 | Active | Prevents false breakout classification |
| Ambiguous trend fallback removed | Active | ADX+DI without slope → sideways, not trending |
| mean_reversion routing 6→2 scanners | Active | Only liquidity_sweep + structure_bounce |
| max_age SCALP 3m→5m, INTRADAY 5m→8m | Active | Prevents premature exit kills |
| MAX_DAILY_TRADES 5→15 | Active | More real trade capacity |
| ML filter accepts WEAK verdict | Active | WEAK+TAKE+STRONG_TAKE allowed (only SKIP blocked) |
| Order type auto→taker | Active | Market orders for guaranteed fills |
| TimeframesConfig +macro/session | Active | 1h and 4h candles now load |
| soft_vetos initialization bug | Fixed | Was UnboundLocalError on new regime paths |
