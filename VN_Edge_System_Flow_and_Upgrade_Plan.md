# VN Edge — Full System Flow & Pro Quant Upgrade Plan
## April 4, 2026

---

## PART 1: CURRENT SYSTEM FLOW (End-to-End)

```
┌──────────────────────────────────────────────────────────────────────┐
│                        DATA PIPELINE                                 │
│                                                                      │
│  Delta Exchange API (REST + WebSocket)                               │
│       ↓                                                              │
│  DataFeed (data/feed.py)                                             │
│  - Polls 5 timeframes: 1m, 5m, 15m, 1h, 4h                        │
│  - 10 symbols: BTC, ETH, SOL, XRP, LTC, ADA, DOT, TAO, DOGE, LINK │
│  - 200 candles per timeframe per symbol                              │
│       ↓                                                              │
│  DataManager (data/manager.py)                                       │
│  - Stores OHLCV in LRU cache (max 5000 candles per key)            │
│  - Computes indicators: EMA 8/21/50/200, RSI 14, MACD, BB 20/2,   │
│    ATR 14, Supertrend, VWAP, Stochastic 14/3/3, OBV               │
│       ↓                                                              │
│  Orchestrator (bot/orchestrator.py)                                  │
│  - On each candle close → calls strategy.analyze(symbol, candles)   │
└──────────────────────────────────────────────────────────────────────┘
           ↓
┌──────────────────────────────────────────────────────────────────────┐
│                     SIGNAL GENERATION                                │
│                                                                      │
│  ScalpStrategy.analyze() (strategies/scalp_strategy.py)              │
│                                                                      │
│  Step 1: REGIME DETECTION                                            │
│    - Advanced detector (ADX, ATR pctile, BB squeeze, volume)        │
│    - 9 regimes: trending_up/down, breakout, sideways,               │
│      high_vol, mean_reversion, low_liquidity, quiet                 │
│    - Controls which scanners can run                                 │
│                                                                      │
│  Step 2: MTF CONTEXT                                                 │
│    - 4h session bias (+8/-12 confidence per scanner)                │
│    - 1h macro bias (veto counter-trend entries)                     │
│    - 15m confirmation (all scanners try 15m first, +10 bonus)       │
│                                                                      │
│  Step 3: SCANNER EXECUTION (12 active scanners)                     │
│    - structure_bounce (98% of trades, 72% WR)                       │
│    - liquidity_sweep, bos_choch, trend_continuation                 │
│    - ema_momentum, cvd_divergence, rsi_divergence                   │
│    - vwap_mean_revert, rsi_extreme, bb_squeeze                      │
│    - post_impulse, order_block_entry                                 │
│    - Each outputs: confidence score (0-100), side, entry, SL        │
│                                                                      │
│  Step 4: QUALITY GATES                                               │
│    - Score normalization + weight by scanner performance             │
│    - Fib 0.618 confluence bonus (+10)                               │
│    - Veto layer: 12 veto checks (regime, HTF, candle quality, etc.) │
│    - ML scoring (shadow mode): prob → verdict (SKIP/WEAK/TAKE)      │
│    - Grade assignment: A+ / A / B / C / REJECT                      │
│                                                                      │
│  Output: Signal with entry, SL, TP1/TP2/TP3, grade, ML verdict     │
└──────────────────────────────────────────────────────────────────────┘
           ↓
┌──────────────────────────────────────────────────────────────────────┐
│                     TRADE EXECUTION                                  │
│                                                                      │
│  Signal Tracker (bot/signal_tracker.py)                              │
│    - Dedup check (same symbol/price within cooldown)                │
│    - Trade type classification: SCALP / INTRADAY / RUNNER           │
│      (based on ML probability: <0.50=SCALP, 0.50-0.65=INTRA, >0.65=RUNNER)│
│    - Risk model: position sizing by grade + leverage (20-75x)       │
│       ↓                                                              │
│  Paper Engine (execution/paper_engine.py)                            │
│    - Simulated fill at signal price                                  │
│    - Tracks PnL, MFE, MAE per trade                                │
│       ↓                                                              │
│  Real Manager (execution/real_manager.py)                            │
│    - Smart qualify: grade, ML verdict (WEAK+), balance, daily limit │
│    - Bracket order on Delta: entry + SL + TP in one call            │
│    - Maker-first (3s) → taker fallback for guaranteed fill          │
│    - Max 15 trades/day, $15-50 margin per trade                     │
└──────────────────────────────────────────────────────────────────────┘
           ↓
┌──────────────────────────────────────────────────────────────────────┐
│                     EXIT MANAGEMENT                                  │
│                                                                      │
│  Single Trail System (lock_pct SL move):                             │
│    - Activates at peak MFE >= 0.3R (below = no trail)               │
│    - 0.3R → lock 70%, 0.5R → 80%, 0.75R → 85%, 1.0R → 88%        │
│    - SL moves up only (never down)                                   │
│    - Normal SL hit detection handles the exit                        │
│                                                                      │
│  Time-Based Exits (max_age — single system):                         │
│    - SCALP: early_kill 45s/0.08R → max_age 15min                   │
│    - INTRADAY: early_kill 60s/0.08R → max_age 20min                │
│    - RUNNER: no early_kill → max_age 8hr                            │
│                                                                      │
│  Partial Exit Ladder:                                                │
│    - 35% at 0.3R, remaining trails                                   │
│    - TP1 hit → move SL to breakeven                                 │
└──────────────────────────────────────────────────────────────────────┘
```

---

## PART 2: WHAT'S BROKEN AND WHY

### Current Performance
| Metric | Good Days (Apr 1-2) | Bad Days (Apr 3-4) | Gap |
|--------|--------------------:|-------------------:|----:|
| Win Rate | 79% | 43% | -36pp |
| Net/Trade | $2.07 | $0.30 | -$1.77 |
| Fee Drag | 39% | 85% | +46pp |
| Scanner diversity | 98% structure_bounce | 98% structure_bounce | Same |

### 5 Structural Problems

**Problem 1: ML Model is Useless**
- Trained on Mar 26 data (stale)
- In shadow mode (logs but doesn't filter)
- 79% of trades get WEAK verdict (0.40-0.49 prob) → all classified as SCALP
- No predictive edge: WEAK trades have 56% WR, TAKE trades have 67% WR — difference too small to be useful
- The ML is essentially a random number generator between 0.40-0.50

**Problem 2: Only 1 Scanner Works**
- 980 out of 1002 trades (98%) are from `structure_bounce`
- 12 scanners are routed but 11 never trigger because their conditions are too strict
- `liquidity_sweep`: 19 trades, 37% WR, -$0.33 — negative edge
- `bos_choch`: 1 trade, 0% WR — untested

**Problem 3: Fee Drag Destroys Edge**
- Round-trip taker fees: ~0.12% ($0.06 per $50 position)
- Average winning scalp: +0.15% gross → net +0.03% after fees
- Average losing scalp: -0.10% gross → net -0.22% after fees
- The fee asymmetry means you need >75% WR just to break even

**Problem 4: Entry Quality is Low**
- 46% of losses had MFE > 0.1R (price moved in our favor first)
- Entry timing is off — entering too late in the move
- No order flow confirmation (CVD, book imbalance)

**Problem 5: Exit Management Fights Itself**
- Was 6 conflicting systems (now reduced to 2)
- Trail locks profits too tight at low MFE levels
- max_age kills trades that just need more time

---

## PART 3: PRO QUANT UPGRADE PLAN

### Priority 1: Fix ML (Highest Impact — currently adding zero value)

**Current:** Stale XGBoost model, shadow mode, no edge
**Target:** LightGBM with daily walk-forward retraining, triple barrier labels

| Change | What | Impact | Difficulty |
|--------|------|--------|------------|
| **Switch to LightGBM** | All Kaggle crypto competition winners used LightGBM. Faster, better with categorical features, lower memory. | HIGH | Easy |
| **Triple barrier labeling** | Label trades by actual outcome (hit TP, hit SL, or timed out) instead of fixed horizon. Matches real trade mechanics. Use `mlfinpy` library. | HIGH | Medium |
| **Walk-forward retraining** | Retrain every 24h on last 2-4 weeks of data. Validate on 2-3 days. Current model is 10 days stale. | CRITICAL | Medium |
| **Feature engineering v2** | Add: cross-asset returns (BTC return as feature for alts), volatility ratios (5m/1h), consecutive candle count, VWAP deviation, funding rate delta | HIGH | Medium |
| **Regime as feature** | Feed regime state as categorical feature into ML. Different regimes have fundamentally different return distributions. | HIGH | Easy |

**Reference repos:**
- [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) — FreqAI pipeline for continuous retraining
- [asavinov/intelligent-trading-bot](https://github.com/asavinov/intelligent-trading-bot) — Clean ML feature engineering
- [stefan-jansen/machine-learning-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading) — De Prado methods

### Priority 2: Add Minimum Trade Viability Filter (Easy Win)

**Current:** No pre-trade fee check
**Target:** Reject trades where expected move < 3x round-trip fees

```python
# Add to scalp_strategy.py before signal emission
min_viable_move = entry_price * 0.0036  # 3x round-trip taker fee (0.12%)
expected_move = abs(tp1 - entry)
if expected_move < min_viable_move:
    reject("Fee-unviable: expected move $%.2f < min $%.2f" % (expected_move, min_viable_move))
```

This single filter would have prevented ~30% of the Apr 3-4 losses (tiny trades where fees ate the entire edge).

### Priority 3: Chandelier Exit (Replace Static Trail)

**Current:** Fixed lock_pct percentages per MFE level
**Target:** ATR-based Chandelier Exit that adapts to volatility

```python
# Chandelier Exit for scalping
chandelier_long  = highest_high(10) - ATR(10) * 2.0
chandelier_short = lowest_low(10) + ATR(10) * 2.0

# Regime-adaptive multiplier
if regime == "trending":
    mult = 2.5  # wider — let winners run
elif regime == "ranging":
    mult = 1.5  # tighter — take quick profits
elif regime == "volatile":
    mult = 1.8  # medium — protect capital
```

**Why better:** The current trail uses fixed percentages (70% at 0.3R, etc.) which don't adapt to actual market volatility. A 0.3R move in a low-vol market is significant; in high-vol it's noise. Chandelier automatically scales.

### Priority 4: HMM Regime Detection (Replace ADX-based)

**Current:** ADX + ATR percentile + BB squeeze
**Target:** Hidden Markov Model with 4 states

```python
from hmmlearn import hmm

# Features: returns, realized_vol, volume_ratio, atr_change
model = hmm.GaussianHMM(n_components=4, covariance_type="diag")
model.fit(feature_matrix)  # train on 2-4 weeks

# Live: predict current state
state = model.predict(current_features)[-1]
# Map states to regimes based on emission distributions
```

**Reference:** [CryptoMarket_Regime_Classifier](https://github.com/akash-kumar5/CryptoMarket_Regime_Classifier)

**Why better:** HMM captures state transitions probabilistically. The current detector makes hard threshold cuts (ADX>30 = trending, else not). HMM provides transition probabilities which can be used to detect regime CHANGES before they fully develop.

### Priority 5: CVD Divergence as Veto Signal

**Current:** CVD scanner exists but rarely triggers
**Target:** Use CVD as a VETO on all trades (not just its own scanner)

```python
# On every trade candidate, check CVD alignment
cvd_5m = compute_cvd(df_5m)
cvd_slope = (cvd_5m[-1] - cvd_5m[-5]) / 5

if side == "long" and cvd_slope < -threshold:
    veto("CVD divergence: price up but CVD down — don't buy")
if side == "short" and cvd_slope > threshold:
    veto("CVD divergence: price down but CVD up — don't sell")
```

**Why:** Order flow doesn't lie. If price rises but cumulative buying volume declines, smart money is distributing. This catches false breakouts and failed bounces.

### Priority 6: Maker-Only Entries (Fee Optimization)

**Current:** Maker-first with 3s taker fallback
**Target:** Pure maker with better pricing logic

```python
# Smart limit order placement
if side == "buy":
    limit_price = best_bid + tick_size  # join bid, don't cross spread
elif side == "sell":
    limit_price = best_ask - tick_size  # join ask

# Post-only with 5s timeout — if not filled, skip trade (no taker)
# Missing a trade is better than paying 3x the fees
```

**Impact on fee drag:**
- Current taker round-trip: 0.12% (0.059% * 2)
- Maker round-trip: 0.04% (0.02% * 2) on most exchanges
- **67% fee reduction** → fee drag drops from 39% to ~13%

### Priority 7: Scanner Diversification

**Why 11 scanners don't fire:**
- Their confidence thresholds are calibrated to the same scale as structure_bounce
- But structure_bounce has 10+ confirmation sources → scores 55-90
- Other scanners have 5-7 sources → max out at 50-70
- The quality gate at 40+ blocks many, and the veto layer deducts another 15-30

**Fix approach:**
- **Per-scanner minimum thresholds** (not a global 40)
- **Scanner-specific veto bypass** (structure_bounce has 12 veto checks; simpler scanners should have fewer)
- **Backtest each scanner in isolation** to find its true edge before combining

**Reference:** [NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity) uses completely separate entry logic per strategy, not a shared scoring system.

---

## PART 4: IMPLEMENTATION ROADMAP

### Week 1 (Immediate — No ML Required)
1. Add fee viability filter (reject trades < 3x fees) — **2 hours**
2. Switch to smart maker limit orders (join bid/ask, skip if not filled) — **4 hours**
3. Implement Chandelier Exit (replace static lock_pct) — **4 hours**
4. Per-scanner minimum thresholds — **2 hours**

### Week 2 (ML Foundation)
5. Switch labeling to triple barrier (TP, SL, max_time) — **6 hours**
6. Rebuild features: add cross-asset, volatility ratios, regime categorical — **8 hours**
7. Train LightGBM with walk-forward validation — **4 hours**
8. Set up daily retraining cron job — **4 hours**

### Week 3 (Advanced)
9. HMM regime detection (4-state) — **8 hours**
10. CVD divergence as universal veto — **4 hours**
11. Backtest each scanner in isolation — **8 hours**
12. Remove/fix scanners with no edge — **4 hours**

### Week 4 (Production Hardening)
13. Wire ML from shadow → live mode — **2 hours**
14. A/B test: ML-filtered vs unfiltered paper trades — **ongoing**
15. Optimize execution latency (CCXT Pro WebSocket) — **8 hours**
16. Build proper backtest framework for parameter validation — **16 hours**

---

## PART 5: KEY GITHUB REPOS TO STUDY

| Repo | Stars | What to Learn |
|------|-------|---------------|
| [freqtrade](https://github.com/freqtrade/freqtrade) | 39.9k | FreqAI ML pipeline, strategy architecture |
| [NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity) | High | Battle-tested multi-TF strategy |
| [intelligent-trading-bot](https://github.com/asavinov/intelligent-trading-bot) | 1.4k | Clean ML feature engineering |
| [smart-money-concepts](https://github.com/joshyattridge/smart-money-concepts) | Active | Python SMC/ICT library |
| [CryptoMarket_Regime_Classifier](https://github.com/akash-kumar5/CryptoMarket_Regime_Classifier) | New | HMM+LSTM regime detection |
| [market-regime-detection](https://github.com/taylorjmellon/market-regime-detection) | Active | K-Means+HMM regime detection |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 12k | RL trading reference |
| [machine-learning-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading) | High | De Prado methods |
| [delta-exchange/python-rest-client](https://github.com/delta-exchange/python-rest-client) | Official | Delta API reference |

---

## PART 6: SUCCESS METRICS

After implementing Weeks 1-2:
| Metric | Current | Target | How |
|--------|---------|--------|-----|
| Win Rate | 72% (declining) | **78-82%** (stable) | ML filtering + fee filter + better exits |
| Fee Drag | 39-85% | **15-25%** | Maker-only orders |
| Scanner Diversity | 98% structure_bounce | **<70%** structure_bounce | Per-scanner thresholds |
| Net/Trade | $0.30-2.07 | **$2.50+** (stable) | All above combined |
| ML Accuracy | Random (0.40-0.50) | **>0.55 AUC** | LightGBM + triple barrier + daily retrain |
| Max Drawdown | $100+ (Apr 3-4) | **<$30/day** | Better entry quality + fee filter |
