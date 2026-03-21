# VN Edge — System Design Document

## 1. System Overview

```
                          VN EDGE TRADING BOT
                     ============================

  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
  │   VM1 (Bot)  │    │  VM2 (ML)    │    │   Delta      │
  │  150.230.    │◄──►│  158.101.    │    │   Exchange   │
  │  171.48      │    │  112.94      │    │   (India)    │
  │              │    │              │    │              │
  │  Bot Engine  │    │  ML Trainer  │    │  Futures     │
  │  Dashboard   │    │  ML Dashboard│    │  WebSocket   │
  │  :8080       │    │  :8081       │    │  REST API    │
  └──────┬───────┘    └──────────────┘    └──────┬───────┘
         │                                       │
         └───────────────────────────────────────┘
                    WebSocket + REST
```

**Tech Stack:** Python 3.13, asyncio, aiohttp, ccxt, LightGBM, pandas
**Exchange:** Delta India (futures, USDT-margined)
**Pairs:** BTC/USDT, ETH/USDT, AVAX/USDT, SOL/USDT, DOGE/USDT
**Account:** $1,000 paper, max $100 margin/trade, 0-75x leverage

---

## 2. Bot Architecture — Signal Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│                    SIGNAL PIPELINE (every candle)                │
│                                                                 │
│  CANDLES ──► REGIME ──► SCANNER ──► VETO ──► ML ──► EXECUTE    │
│   (1m/5m)   (15m)     ROUTING     LAYER    GATE    (paper)     │
│                                                                 │
│  Step 1:    Step 2:    Step 3:    Step 4:   Step 5:  Step 6:   │
│  Fetch      Detect     Only run   Hard      ML prob  Size,     │
│  OHLCV      market     scanners   block on  < 0.50   lever,    │
│  from       regime     allowed    ANY veto  = BLOCK  execute   │
│  exchange              in this              ≥ 0.50             │
│                        regime               = TRADE            │
└─────────────────────────────────────────────────────────────────┘
```

### 2.1 Regime Detection (15m candles)

The regime engine classifies current market state on the 15m timeframe:

| Regime | Condition | Allowed Scanners |
|--------|-----------|-----------------|
| `trending_up` | EMA8 > EMA21 > EMA50, slope positive | trend_continuation, ema_momentum, structure_bounce |
| `trending_down` | EMA8 < EMA21 < EMA50, slope negative | trend_continuation, ema_momentum, structure_bounce |
| `breakout` | Price breaks key level with volume | structure_bounce, order_block_entry, ema_momentum |
| `ranging/sideways` | EMAs flat, price oscillating | vwap_mean_revert, rsi_divergence, structure_bounce |
| `volatile` | ATR > 1.5x average | structure_bounce, order_block_entry |
| `quiet` | ATR < 0.7x average | **NO TRADING** |
| `low_liquidity` | Volume < threshold | **NO TRADING** |

**Key principle:** Regime is the PRIMARY engine, not a filter. It determines which scanners are even allowed to run.

### 2.2 Scanners (Signal Generators)

Each scanner checks a specific technical pattern over the last 3-8 candles (sequence-based, not snapshot):

| Scanner | Logic | Regime Fit |
|---------|-------|-----------|
| `structure_bounce` | Price at S/R level + rejection wick + volume spike + confirmation candle | Trending, Breakout, Ranging (edges), Volatile |
| `trend_continuation` | Impulse candle + 2-4 bar pullback + holds EMA21 + volume trigger | Trending |
| `ema_momentum` | EMA8 crosses EMA21 + pullback to cross area + RSI turns + volume | Trending, Breakout |
| `vwap_mean_revert` | Price deviates from VWAP > 1.5 ATR + reversion signal | Ranging |
| `rsi_divergence` | Price makes new extreme but RSI diverges + confirmation | Ranging |
| `order_block_entry` | Institutional order block level + price reaction | Breakout, Volatile |
| `liquidity_sweep` | Liquidity grab below/above key level + reversal | Learning mode only |
| `bb_squeeze` | **DISABLED** — 36% ML accuracy, no edge proven | None |

### 2.3 Veto Layer (Hard Blocks)

After a scanner produces a candidate signal, ALL veto conditions are checked. **Any single veto = NO TRADE:**

```python
VETO_CONDITIONS = [
    "REGIME_CONFLICT"    # Scanner not allowed in current regime
    "HTF_MISMATCH"       # Higher-timeframe bias opposes signal direction
    "NEGATIVE_EV"        # Expected value calculation says REJECT
    "LOW_VOLATILITY"     # ATR ratio < 0.7 (dead market)
    "NO_VOLUME"          # Volume ratio < 1.0 (no participation)
    "RECENT_DUPLICATE"   # Same symbol+side+price within 30 min
    "CONFLICT"           # Opposite direction already active on same symbol
]
```

### 2.4 ML Gate (Hard Veto at 50%)

```
ML probability < 0.50  →  HARD BLOCK (no trade)
ML probability 0.50-0.55  →  SCALP trade type
ML probability 0.55-0.65  →  INTRADAY trade type
ML probability ≥ 0.65  →  RUNNER trade type + confidence bonus
```

### 2.5 Trade Type Classification

After ML passes, the trade is classified into one of 3 types based on ML probability + context boosters:

| | SCALP | INTRADAY | RUNNER |
|---|---|---|---|
| **ML Prob** | < 0.50 (with boost) or 0.50-0.55 | 0.55-0.65 | ≥ 0.65 |
| **SL (ATR mult)** | 0.9x | 1.15x | 1.5x |
| **TP1 R:R** | 0.8:1 | 1.2:1 | 1.5:1 |
| **TP2 R:R** | 1.2:1 | 2.0:1 | 3.0:1 |
| **TP3** | None | 3.0:1 | 5.0:1 |
| **Time Stop** | 5 bars (hard kill) | 15 bars (soft) | None |
| **Max Age** | 30 min | 2 hours | 8 hours |
| **Trail ATR** | 0.6x | 1.0x | 1.5x |

**Context Boosters** (can upgrade/downgrade type):
- Trending regime + HTF aligned → upgrade one tier
- Ranging regime + VWAP noise zone → downgrade one tier
- Confidence ≥ 95 → minimum INTRADAY

---

## 3. Position Sizing — Fixed Fractional Risk Model

```
ACCOUNT_SIZE = $1,000
RISK_PER_TRADE = 0.75% = $7.50
MAX_MARGIN = $100
MAX_LEVERAGE = 75x

Position Size = Risk Amount / SL Distance %
Leverage = Position Size / Margin (derived, not input)
Margin = min(Position Size / Leverage, $100)  ← hard cap
```

**Confidence-based leverage tiers:**
| Confidence | Max Leverage | Max Margin |
|-----------|-------------|-----------|
| 90+ (A+) | 75x | $100 |
| 80+ (A) | 50x | $100 |
| 70+ (B) | 30x | $80 |
| 60+ (C) | 20x | $60 |
| < 60 | 10x | $50 |

**Contract Sizes (Delta Exchange):**
- BTC: 0.001 BTC per contract
- ETH: 0.01 ETH per contract
- SOL: 0.1 SOL per contract
- AVAX: 0.1 AVAX per contract
- DOGE: 1.0 DOGE per contract

---

## 4. Exit Management

### 4.1 Take Profit Scaling
- TP1 hit → close 35% of position, move SL to breakeven
- TP2 hit → close 35% of position, activate ATR trailing stop
- TP3 hit → close remaining 30%

### 4.2 Trailing Stop (ATR-based)
After breakeven is set, trail stop using ATR multiplier based on trade type and regime:
```
Trail Price = High/Low - (ATR × trail_mult × regime_mult)
```
Regime multipliers: trending=1.3x, ranging=0.7x, volatile=0.9x

### 4.3 Time Stops
| Trade Type | Bars | Behavior |
|-----------|------|----------|
| SCALP | 5 bars | Hard kill — close immediately |
| INTRADAY | 15 bars | Soft — close only if < 0.1R profit |
| RUNNER | None | No time stop |

### 4.4 Early Kill (Scalper Window)
- SCALP only: if MFE < 0.10R after 3 minutes → kill immediately
- Saves fee damage on trades that never moved

### 4.5 Fee Structure (Delta Exchange)
| Fee Type | Rate | When |
|----------|------|------|
| Taker (standard) | 0.06% per side | Normal trades |
| Scalper entry | 0.05% | Opening within Scalper window |
| Scalper exit | 0.00% | Closing within Scalper window |
| Settlement | 0.02% | Applied on all trades |

Scalper windows: BTC=27min, ETH/AVAX=12min, SOL/DOGE=12min

---

## 5. ML System Design

### 5.1 Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    ML PIPELINE (VM2)                     │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐ │
│  │ Candle   │  │ Feature  │  │ Candidate│  │ Model  │ │
│  │ Collector│─►│ Builder  │─►│ Trainer  │─►│ Server │ │
│  │          │  │          │  │(LightGBM)│  │ (API)  │ │
│  └──────────┘  └──────────┘  └──────────┘  └────────┘ │
│                                                   │     │
│                                    ┌──────────────┘     │
│                                    ▼                    │
│                              ┌──────────┐               │
│                              │ ML       │               │
│                              │ Dashboard│               │
│                              │ :8081    │               │
│                              └──────────┘               │
└─────────────────────────────────────────────────────────┘
```

### 5.2 Feature Engineering Principles

**Golden rules:**
1. Pure candle math only — no raw RSI, MACD, pattern names
2. Relationships, not values — distance ratios, normalized deviations
3. ATR-normalized everything — makes features comparable across pairs
4. Delta features — state change is more predictive than state

**Feature Categories:**

| Category | Features | Why |
|----------|----------|-----|
| **VWAP** | dist_from_vwap (ATR-normalized), vwap_slope, vwap_reversion_speed | #1 most important feature confirmed by ML |
| **Trend** | ema8_21_dist, ema21_50_dist, trend_strength, trend_alignment | Regime context for scanner validation |
| **Volatility** | atr_ratio (current/avg), vol_compression, squeeze_width, range_pct | Opportunity sizing |
| **Volume** | volume_z_score, cvd_slope, relative_volume, buy_sell_ratio | Confirmation of moves |
| **Structure** | dist_to_support, dist_to_resistance, swing_position | S/R context |
| **Candle** | body_ratio, wick_ratio, close_position, displacement | Entry quality |
| **Time** | hour_sin/cos, day_of_week, session (asia/europe/us) | Session patterns |
| **Momentum** | return_5bar, return_15bar, momentum_acceleration | Directional strength |
| **Transition** | delta_vwap_dist, delta_trend_strength, delta_atr_ratio, impulse_decay | State change signals |

### 5.3 Training Approach

```
Label: "Will MFE reach 0.2R+ within X bars?" (MFE-based, not full outcome)
Dataset: Scanner-triggered candidates only + small control sample
Split: Walk-forward validation (train early, test later)
Model: LightGBM classifier (gradient boosted trees)
Eval: Walk-forward AUC must be > 0.55 (if ~0.50, no edge)
```

**Training pipeline:**
1. Collect candle data for all pairs (1m, 5m, 15m)
2. Run scanners to identify candidate entry points
3. Build features at each candidate point
4. Label using MFE: did price reach TP (0.2R+) before SL?
5. Train LightGBM with walk-forward cross-validation
6. Evaluate: AUC, calibration curves, bucket spread
7. Deploy model to scoring API on VM2

### 5.4 Model Strategy (Phased)

| Phase | Approach | When |
|-------|----------|------|
| **Phase 1** (current) | Shared model across all pairs, symbol as feature | Now |
| **Phase 2** | Same model, pair-specific probability thresholds | When Phase 1 proves OOS edge |
| **Phase 3** | Pair-specific models | Only for pairs with 500+ OOS candidates |

### 5.5 ML Scoring Flow (Live)

```
Bot (VM1) generates candidate signal
  → Builds feature vector (same features as training)
  → HTTP POST to VM2 ML API: /api/score
  → VM2 returns: {probability: 0.58, verdict: "TAKE"}
  → Bot checks: prob ≥ 0.50 → execute, prob < 0.50 → BLOCK
```

### 5.6 ML Feedback Loop (Live → Training)

```
Trade closes on VM1
  → signal_tracker._send_ml_feedback()
  → Writes to ml_live_feedback.jsonl:
      {symbol, scanner, trade_type, ml_prob, ml_verdict,
       pnl_usd, pnl_pct, exit_r, mfe_r, mae_r, fees,
       duration_sec, exit_reason, regime, session}
  → Also calls training_dataset.update_outcome()
  → File synced to VM2 periodically
  → ML Dashboard shows: by-pair, by-scanner, by-type,
    pair×scanner matrix, ML accuracy vs actual
```

### 5.7 Calibration Requirements

Before ML is used for hard decisions:
- **Bucket spread > 15%**: Top bucket WR must beat bottom by 15%+
- **Rank correlation > 0.7**: Higher ML prob = higher actual WR
- **Walk-forward AUC > 0.55**: Must hold out-of-sample
- **Monotonic buckets**: WR should increase with each probability bucket

---

## 6. Operating Modes

| Mode | ML Veto | Regime Gate | Scanners | Purpose |
|------|---------|------------|----------|---------|
| `paper_learning` | OFF | OFF | ALL run | Collect ML training data |
| `paper_enforced` | ON (< 0.50 blocked) | ON | Regime-routed | Test with all safety gates |
| `live` | ON (strictest) | ON | Regime-routed | Real money execution |

**Current mode: `paper_enforced`**

---

## 7. Dashboard & Monitoring

### VM1 Dashboard (:8080) — Live Trading
- **Live tab:** Active trades (with confidence, setup reason, R:R, ML prob, regime, trade type badges), signal feed, opportunity funnel, veto stats
- **Analytics tab:** Equity curve, closed trades table, R-metrics, exit quality, scanner health, session analysis, paper balance

### VM2 ML Dashboard (:8081) — ML Training Lab
- **Overview:** Pipeline status, training config, best/worst setups
- **Candidates:** Per-pair ML training stats (raw WR, ML top quartile WR, AUC, calibration)
- **Scanner Results:** Backtest performance per scanner×symbol×timeframe
- **Calibration:** Probability bucket visualization (predicted vs actual)
- **Features:** Feature importance bars, category breakdown
- **TF Comparison:** Timeframe performance comparison
- **Walk-Forward:** Out-of-sample validation windows
- **Live Feedback:** Real-time trade outcomes by pair, scanner, trade type, ML accuracy tracking

---

## 8. Risk Management

| Control | Value | Enforcement |
|---------|-------|-------------|
| Max margin per trade | $100 | Hard cap in signal_tracker |
| Max leverage | 75x | Confidence-gated |
| Risk per trade | 0.75% ($7.50) | Fixed fractional |
| Max 1 position per symbol+side | — | Duplicate prevention |
| No opposite positions | — | Conflict blocker |
| 30-min cooldown same price | — | Re-entry prevention |
| Quiet regime = no trading | — | Regime router |
| ML < 50% = no trading | — | Hard ML veto |

---

## 9. File Structure

```
crypto-trading-bot/
├── main.py                          # Entry point
├── bot/
│   ├── orchestrator.py              # Main loop, wires everything
│   ├── signal_tracker.py            # Trade tracking, exits, ML feedback
│   ├── ev_engine.py                 # Expected value calculator
│   ├── ml_scorer.py                 # ML scoring client (calls VM2)
│   ├── mode_manager.py              # Operating mode control
│   └── signal_learner.py            # Learns from trade outcomes
├── strategies/
│   ├── scalp_strategy.py            # Main strategy (scanners, regime, veto)
│   ├── multi_strategy.py            # Wrapper (scalp + investment)
│   ├── regime_filter.py             # Regime detection + scanner routing
│   └── base.py                      # Base strategy class
├── exchange/
│   ├── delta_ws.py                  # Delta Exchange WebSocket
│   └── exchange_manager.py          # REST API wrapper
├── execution/
│   └── paper_engine.py              # Paper trading execution
├── dashboard/
│   ├── server.py                    # Dashboard API server
│   └── templates/index.html         # Dashboard UI
├── ml_training/
│   ├── run_trainer.py               # ML training entry point
│   ├── dashboard.py                 # ML dashboard server
│   ├── candidate_trainer.py         # LightGBM training pipeline
│   ├── feature_builder.py           # Feature engineering
│   └── templates/ml_dashboard.html  # ML dashboard UI
├── config/
│   └── settings.yaml                # All configuration
├── storage/
│   ├── active_signals.json          # Currently open trades
│   ├── closed_signals.json          # Trade history
│   └── ml_live_feedback.jsonl       # ML feedback (trade outcomes)
└── scripts/
    └── sync_feedback.sh             # Sync feedback VM1→VM2
```

---

## 10. Known Issues & Next Steps

### Implemented
- [x] Regime-first scanner routing
- [x] ML hard veto gate (< 0.50 blocked)
- [x] 3-tier trade types (SCALP/INTRADAY/RUNNER)
- [x] Fixed fractional risk model
- [x] ML feedback loop (trade outcomes → training data)
- [x] Dashboard: trade type badges, ML prob display
- [x] bb_squeeze disabled (proven no edge)
- [x] Switched to paper_enforced mode

### Pending (from Plan)
- [ ] Sequence-based scanner rewrites (currently still snapshot logic)
- [ ] Setup lifecycle (CANDIDATE → CONFIRMED → EXECUTABLE)
- [ ] Calibrated EV (scanner + side + regime + session key)
- [ ] Candle quality filters (body_ratio, close_in_direction, displacement)
- [ ] Dead session blocking (hours 2-5, 10-11 UTC)
- [ ] VWAP hard veto (distance < 0.3 ATR = noise zone)
- [ ] Full backtest comparison (before vs after)
- [ ] Walk-forward AUC validation on current model
