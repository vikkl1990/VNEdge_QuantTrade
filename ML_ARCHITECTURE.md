# VN Edge — ML System Architecture
# Complete Technical Specification
# Generated: 2026-03-20

Perfect! Now I have comprehensive coverage of the codebase. Let me compile all this into a comprehensive architecture document.

## COMPREHENSIVE ARCHITECTURE DOCUMENT
### VN Edge Crypto Trading Bot - Complete System Overview

---

## 1. SYSTEM OVERVIEW

The VN Edge bot is a **three-VM distributed system** designed for high-frequency crypto scalping on Delta Exchange (India).

### VM Distribution:
- **VM1 (Trading Node)**: Live signal generation, execution, dashboard (port 8080)
- **VM2 (ML/Backtest Node)**: Historical data collection, model training, backtesting (port 8081)
- **Network**: Components communicate via REST APIs and shared storage (cloud-synced)

### Core Philosophy:
- **Empirical edge-based trading**: Uses Expected Value computed from actual R-metrics, not theoretical scores
- **Pure candle math for ML**: Features extracted directly from price/volume, not indicators
- **Regime-aware execution**: Different scanners allowed in different market conditions
- **Graduated signal output**: Signals emitted at tiers (strong/valid/weak/near_miss/rejected), not binary
- **Adaptive learning**: Continuous improvement from closed trades via SignalLearner and TradeMonitor

---

## 2. BOT CORE (VM1)

**File**: `/bot/orchestrator.py`

### Entry Point & Main Loop

The `BotOrchestrator` is the central nervous system that coordinates all subsystems in a single async event loop.

**Key Components:**
- Manages event loop: candle data → analysis → execution → alerting → persistence
- Maintains all subsystem instances: exchange, data_manager, strategy, risk_manager, execution_engine
- Implements heartbeat monitoring and graceful shutdown
- Runs state persistence every 60 seconds and position checks every 15 seconds

**Main Orchestration Flow:**
```
1. Await data feed events (new candle on 1m, 5m, 15m timeframes)
2. Run strategy.analyze() on all symbols
3. Filter signals through decision_engine (synthesizes scores, regimes, EV)
4. Apply risk checks (daily loss limit, position count, leverage limits)
5. Execute via execution_engine (paper or live)
6. Track signal via signal_tracker (TP/SL monitoring)
7. Send alerts via alert_manager (Telegram, console)
8. Journal trade via trade_journal (CSV/JSON export)
9. Update dashboard via dashboard_server
10. Persist state to disk
```

**State Management:**
- Open signals persisted to `storage/active_signals.json`
- Closed signals persisted to `storage/closed_signals.json`
- Stats (per-scanner R-metrics) persisted to `storage/signal_stats.json`
- Trade journal exported to `storage/trades.csv` and `storage/trades.json`

**Operating Modes:**
- `signal_only`: Generate alerts, no trades
- `paper`: Simulated execution, learning mode
- `live`: Real exchange orders (requires API keys)
- `backtest`: Historical replay
- `forward_test`: Paper trades on live data with full logging

---

## 3. STRATEGY ENGINE

**File**: `/strategies/scalp_strategy.py` (15 scanners total)

### The `ScalpStrategy.analyze()` Method

This is the heart of signal generation. It runs every time a new candle arrives.

**Method Signature:**
```python
analyze(symbol: str, candles_dict: Dict[str, pd.DataFrame]) -> List[Signal]
```

**Input Data:**
- Primary TF: 1m candles (minimum 50 bars)
- Confirm TF: 5m candles (optional, for ATR calculation)
- HTF (Higher TF): 15m candles (optional, for bias)

**Processing Pipeline:**

1. **Session-Aware Gating** (lines 359-385)
   - Asia Late (2:30-9:00 IST): 37% WR → -15 confidence penalty
   - Asia Early (9:00-13:30): 50% WR → -5 penalty
   - Europe (13:30-20:30): 63% WR → +5 boost
   - US (20:30-2:30): 57% WR → no penalty
   
   These are soft adjustments (penalties), not hard blocks. Learned from 112 actual trades.

2. **Indicator Computation** (line 391)
   - EMAs: 8, 21, 50, 200
   - ATR (14), RSI (14), MACD (12/26/9), Bollinger Bands (20, 2σ)
   - VWAP, relative volume, Supertrend, Fibonacci retracement, CHOCH detection

3. **Regime Detection** (line 474)
   - Calls `_regime_filter.detect_regime(indicators)`
   - Outputs: trending_up/down, sideways, ranging, volatile, quiet, breakout, mean_reversion
   - Used for:
     - Scanner filtering (some scanners blocked in certain regimes)
     - Confidence adjustments (regime-specific boosts)
     - EV threshold adjustments (higher bar in choppy markets)

4. **Structure Map Building** (line 444)
   - Calls `build_structure_map(df, price, atr)`
   - Computes: support/resistance zones, order blocks, swing highs/lows, VWAP bands
   - Used by structure_bounce and vwap_mean_revert scanners

5. **Run All Scanners** (lines 486-504)
   - 15 scanner functions executed (see section 4 below)
   - Each returns `ScanResult` with:
     - `raw_score`: 0-100 before weighting
     - `weighted_score`: after scanner weight applied
     - `tier`: strong/valid/weak/near_miss/rejected
     - Confirmations list and penalties list
     - Hard block reasons

6. **Score Weighting & Filtering** (multiple points)
   - Apply scanner weights (from `ScannerWeightManager`)
   - Apply regime filter: block certain scanners, apply confidence boosts
   - Apply veto system:
     - ATR ratio < 0.7 = dead market, most signals rejected
     - Regime-incompatible scanner = hard block
     - Cost/spread too high = rejection
     - HTF trend disagreement = penalty (learning mode) or block (enforced mode)

7. **EV Gating** (line 620+)
   - Calls `EV engine` with best scanner result
   - If verdict is "REJECT" = don't trade (negative expectancy)
   - If verdict is "REDUCED" = reduce position size to 50-80%
   - If verdict is "TRADE" = full size (or boosted to 130% if EV very high)
   - If "INSUFFICIENT_DATA" = trade at 80% size (learning phase)

8. **Signal Emission**
   - Only signals scoring >= 65 (min_confidence) are emitted as `Signal` objects
   - Each signal includes:
     - Symbol, side (long/short), entry_price
     - Stop loss (ATR-based), take profits (TP1/TP2/TP3 as R-multiples)
     - Confidence (0-100), grade (A/B/C based on confidence)
     - Setup type, scanner name, regime context
     - Session, time of signal

**Key Design Decision: Graduated Tiers (not Binary)**

Instead of `if score >= 65: emit; else: skip`, the strategy produces:
- **TIER_STRONG** (score >= 80): A-grade, full size
- **TIER_VALID** (score >= 65): B-grade, normal size
- **TIER_WEAK** (score >= 50): C-grade, reduced size (logged as opportunity, may not trade)
- **TIER_NEAR_MISS** (score >= 35): Setup forming (dashboard only)
- **TIER_REJECTED** (score < 35): Ignore

This allows dashboard to show "near setups" for transparency without trading low-confidence signals.

---

## 4. SCANNER SUBSYSTEM (15 Scanners)

**File**: `/strategies/scalp_strategy.py` lines 1469-3416

Each scanner is a `_scan_*()` method that detects a specific market pattern. Returns `Optional[_SetupResult]` (None if not triggered, else details).

### Core 6 Scanners (Production)

#### 1. **EMA Momentum** (`_scan_ema_momentum`)
- **Pattern**: EMA8/21 cross → pullback → RSI turn → volume confirm
- **Sequence** (4-step):
  1. EMA8 crosses above/below EMA21 in last 6 candles (not current bar)
  2. Price pulled back to test EMA midpoint (within 0.6× ATR)
  3. RSI turned in signal direction (rising for LONG, falling for SHORT)
  4. Current candle is directional + volume > 0.9× avg
- **Data Point**: Bearish crosses are 0% WR → blocked entirely
- **Confidence Range**: 50-75 before bonuses
- **Regime**: Preferred in trending_up/down (gets +5 boost)

#### 2. **Trend Continuation** (`_scan_trend_continuation`)
- **Pattern**: Impulse → pullback → hold → trigger
- **Sequence** (4-step):
  1. Impulse candle (body > 0.8× ATR in trend direction) within last 8 bars
  2. 1-5 bar pullback (lower highs for LONG, higher lows for SHORT)
  3. Price holds above EMA21 (LONG) or below EMA21 (SHORT) during pullback
  4. Current candle is directional with volume > 0.9× avg
- **Why**: Replaces old single-candle check that fired 148 times in 7 days at 12% WR
- **EMA Gap Check**: Gap must be > 0.05% to confirm real trend
- **Confidence Range**: 50-75
- **Regime**: Preferred in trending (gets +5 boost)

#### 3. **RSI Divergence** (`_scan_rsi_divergence`)
- **Pattern**: Price makes new low while RSI makes higher low (bullish), or vice versa (bearish)
- **Bullish Criteria**:
  - Price near recent low (within 0.1%)
  - RSI 8+ points higher than at that low (strong divergence requirement)
  - RSI currently < 40 (was in oversold territory)
  - Original RSI < 30 (was truly oversold)
- **Bearish Criteria** (mirror):
  - Price near recent high, RSI 8+ points lower, RSI > 60, original RSI > 70
- **Base Confidence**: 35 + bonuses for deep oversold/overbought
- **Volume & MACD Confirmation**: +10 if agree with signal direction
- **Regime**: Works in sideways/ranging (preferred)

#### 4. **Bollinger Band Squeeze** (`_scan_bb_squeeze`)
- **Pattern**: BB bandwidth compressed (squeeze) + expansion with directional breakout
- **Squeeze Detection**:
  - Recent bandwidth in bottom 25% of last 100 bars
  - Current bandwidth > previous × 1.05 (5% expansion)
- **Directional Confirmation**:
  - If Pct B > 0.75 + bullish candle → LONG
  - If Pct B < 0.25 + bearish candle → SHORT
- **Volume Must Confirm**: rel_vol > 1.0× avg (required, not optional)
- **MACD Confirmation**: +10 if histogram agrees
- **HTF Alignment**: +15 if higher timeframe is aligned
- **Confidence Range**: 30-70
- **Regime**: Works in all (preferred in breakout)

#### 5. **Structure Bounce** (`_scan_structure_bounce`)
- **Pattern**: Price approaches S/R → rejection wick → volume spike → confirmation
- **Sequence** (4-step):
  1. **APPROACH**: Price within zone of support or resistance (last 3-5 candles, within 0.3%)
  2. **REJECTION**: Candle at level with:
     - Lower wick > 30% of range (for LONG support bounce)
     - Close in upper half (for LONG, showing rejection of lower prices)
     - Upper wick > 30% of range (for SHORT resistance rejection)
     - Close in lower half (for SHORT)
  3. **VOLUME**: Volume spike on rejection candle (> 1.2× avg)
  4. **CONFIRMATION**: Current candle closes in signal direction away from level
- **Structure Map Sources**:
  - Swing highs/lows (last 50 bars)
  - Order blocks (imbalances)
  - Liquidity voids
  - VWAP bands
- **Confidence Range**: 30-75 based on wick size and zone type
- **Regime**: Works in all (preferred in trending)

#### 6. **VWAP Mean Revert** (`_scan_vwap_mean_revert`)
- **Pattern**: Price touches VWAP band extreme + reversal candle
- **LONG Condition**:
  - Close <= VWAP lower band (±1σ or ±2σ)
  - Lower wick > 50% of candle body (rejection of low prices)
  - Close > open (bullish candle)
- **SHORT Condition** (mirror):
  - Close >= VWAP upper band
  - Upper wick > 50% of body
  - Close < open (bearish candle)
- **Bonus Confirmations**:
  - If at 2σ band (extreme) → +10
  - If volume spike → +10
  - If RSI oversold (< 35 for LONG) or overbought (> 65 for SHORT) → +10
  - If HTF aligned → +15
- **Confidence Range**: 30-70
- **Regime**: Preferred in ranging/sideways

### Additional 9 Scanners (Extended Arsenal)

#### 7. **Supertrend Flip** (`_scan_supertrend_flip`)
#### 8. **RSI Extreme** (`_scan_rsi_extreme`)
#### 9. **Momentum Ride** (`_scan_momentum_ride`)
#### 10. **BB Band Walk** (`_scan_bb_band_walk`)
#### 11. **Post-Impulse** (`_scan_post_impulse`)
#### 12. **Momentum Surge** (`_scan_momentum_surge`)
#### 13. **Liquidity Sweep** (`_scan_liquidity_sweep`)
#### 14. **Order Block Entry** (`_scan_order_block_entry`)
#### 15. **Simple Bias (ML)** (`_scan_simple_bias`)

**Note**: These 9 are auxiliary. The core 6 above are the production setups.

---

## 5. REGIME FILTER & DYNAMIC POSITION SIZING

**File**: `/strategies/regime_filter.py`

### Regime Detection

Method: `detect_regime(indicators: Dict) -> str`

Uses EMA alignment, Bollinger Bandwidth, ATR volatility ratio:

```
trending_up:     EMA8 > EMA21 > EMA50 > EMA200, close above all
trending_down:   EMA8 < EMA21 < EMA50 < EMA200, close below all
quiet:           ATR / ATR_SMA < 0.7 (< 70% of normal volatility)
volatile:        BB_width / avg_BB_width > 1.5 (150% of normal)
ranging:         BB_width / avg_BB_width < 0.5 (50% of normal)
breakout:        Price near BB extremes with expanding volume
sideways:        Default (EMA not aligned, no trend)
```

### Scanner Permissions Per Regime

**Example: Trending_up**
- Allowed: All scanners (no blocking)
- Preferred: EMA Momentum, Momentum Ride, Post-Impulse (+5 confidence boost)
- Size Mult: 1.0× (normal)
- SL Mult: 1.0× (normal SL distance)
- EV Threshold Adj: -0.05 (lower EV bar, trend adds edge)

**Example: Ranging**
- Allowed: Only mean-reversion (VWAP Reclaim, RSI Divergence, BB Squeeze)
- Blocked: Momentum scanners (EMA Momentum, Momentum Ride, Post-Impulse, Momentum Surge)
- Preferred: VWAP Reclaim, RSI Divergence (+3 boost)
- Size Mult: 0.7× (reduced size in choppy markets)
- SL Mult: 0.85× (tighter stops)
- EV Threshold Adj: 0.0 (normal EV bar)

**Example: Low Liquidity**
- No trades allowed (hard block)
- EV Threshold Adj: +0.10 (much higher bar if somehow allowed)

### Confidence Adjustments

Regime + signal properties → multiplier:

```
EV > threshold & in preferred regime      → 1.1× multiplier
EV > threshold & in neutral regime        → 1.0× multiplier
EV > threshold & in hostile regime        → blocked
Regime stability < 2 minutes              → 0.8× (unstable regime)
HTF disagreement (learning mode)          → 0.7× (still allowed, just penalized)
Session penalty (asia_late)               → -15 confidence points
```

---

## 6. EXECUTION ENGINES (VM1)

### Paper Engine (`execution/paper_engine.py`)

Simulates order execution with fees and slippage.

**Features:**
- Initial balance: 10,000 USD (configurable)
- Scalper-tier fees:
  - Taker: 0.05% per side
  - Maker: 0.02% per side (but scalper doesn't use maker fills)
  - Settlement: 0.06% (round-trip fee, Delta India specific)
  - Total round-trip: ~0.11% (entry 0.05% + exit 0.00% + settlement 0.06%)
- Slippage: 0.05% (simulates market impact)
- Leverage: 5× default, 20× max (per risk config)

**Trade Lifecycle:**
1. **Entry**: Place order at entry_price + slippage, deduct taker fee
2. **Position Management**:
   - Track highest/lowest price since entry (for MFE/MAE)
   - Monitor TP1/TP2/TP3 levels
   - Apply trailing stops if enabled
3. **TP Management** (from config):
   - TP1 at 1.5× R: close 60% of position
   - TP2 at 2.5× R: close 25% of position
   - TP3 at 4.0× R: trail remaining 15%
4. **Exit**:
   - Record P&L, fees, R-multiple
   - Update balance
   - Close trade in database

**Take Profit Levels** (configurable in `risk.take_profit` section):
```yaml
tp1_rr: 1.5          # 1.5× risk
tp1_close_pct: 60    # Close 60% of position
tp2_rr: 2.5          # 2.5× risk
tp2_close_pct: 25    # Close 25%
tp3_rr: 4.0          # 4.0× risk (runner)
tp3_close_pct: 15    # Trail 15%
```

**Trailing Stop Settings**:
```yaml
trailing.enabled: true
trailing.activation_rr: 0.8     # Start trailing after 0.8R gain
trailing.trail_pct: 0.4%        # Tighten stop by 0.4% of current price
trailing.break_even_after_tp1: true  # Move SL to break-even after TP1 hit
```

### Live Engine (`execution/engine.py`)

Translates signals to real exchange orders via CCXT.

**Order Types:**
- Limit orders (primary)
- Market orders (fallback if limit takes > 5 seconds)
- Stop-loss orders (via exchange if supported)
- Take-profit orders (partial closes via reduce_only)

**Error Handling:**
- Retry logic: up to 3 retries, exponential backoff
- Partial fill handling: if entry partially fills, adjust SL/TP proportionally
- Order cancellation: if TP/SL hit before entry fills, cancel entry
- Connection recovery: automatic reconnect with state reconstruction

---

## 7. EXCHANGE INTEGRATION (VM1)

**File**: `/exchange/ccxt_client.py`

### CCXT Pro (WebSocket-based)

Uses `ccxt.pro` for WebSocket streaming, falls back to REST polling.

**Supported Exchanges:**
- Binance
- Bybit
- OKX
- Delta Exchange (India-specific)

**Delta India Specifics:**
```python
API_Base: "https://api.india.delta.exchange"
WebSocket: wss://stream.india.delta.exchange/ws
Market Type: Futures (perpetual swaps)
Symbol Format: "BTC/USDT" (no suffix, Delta handles conversion internally)
Fee: Scalper tier = 0.02% maker, 0.05% taker, 0.06% settlement
```

**Data Subscriptions:**
- OHLCV (1m, 5m, 15m, 1h, 4h, 1d) via WebSocket + REST fallback
- Ticker (last price, bid/ask, 24h change)
- Order book (L2 snapshots)
- Positions (for live mode)
- Balance (for position sizing)
- Funding rates (for carry trades, if enabled)

**Rate Limiting:**
- Rest polling interval: 1s for ticker, 5s for OHLCV
- WebSocket priority (lower latency)
- Automatic backoff on rate limit response

---

## 8. SIGNAL TRACKER (VM1)

**File**: `/bot/signal_tracker.py`

### Lifecycle Management

Each generated signal becomes a "TrackedSignal" that persists from entry until exit.

**Signal Lifecycle:**
```
ENTRY (signal generated)
  ↓
ACTIVE (monitoring TP/SL)
  ├─ TP1 hit (60%) → CLOSED (partial exit)
  ├─ TP2 hit (25%) → CLOSED (partial exit)
  ├─ TP3 hit (15%) → CLOSED (full exit)
  ├─ SL hit → CLOSED (loss)
  ├─ Timeout (4 hours) → CLOSED (at market price)
  └─ Scalper Window Reached (27 min BTC, 12 min others) → CLOSED (free exit, Delta offer)
```

### P&L Tracking & R-Metrics

For each closed signal, compute:
- **R-multiple**: (Exit Price - Entry Price) / Risk (SL distance)
  - If TP1 hit: positive R
  - If TP2 hit: 2.5× R
  - If SL hit: -1.0R
- **MFE** (Max Favorable Excursion): highest price favorable direction / risk
- **MAE** (Max Adverse Excursion): lowest unfavorable price / risk

### Per-Scanner Statistics

Aggregate closed signals by scanner name into `by_setup` dict:

```python
by_setup = {
    "ema_momentum": {
        "total": 25,
        "win_rate": 64.0,  # percentage
        "avg_win_r": 2.1,
        "avg_loss_r": -0.9,
        "max_r": 5.2,
        "min_r": -1.0,
        "total_r": 15.2,
        "mfe_avg": 2.8,
        "mae_avg": 0.7,
        ...
    },
    "structure_bounce": { ... },
    ...
}
```

This data powers the **EV Engine** (see section 9).

### Persistence

- Active signals: `storage/active_signals.json` (updated in real-time)
- Closed signals: `storage/closed_signals.json` (append-only)
- Stats: `storage/signal_stats.json` (aggregate metrics)
- All files saved every 5 seconds or on signal closure

---

## 9. EXPECTED VALUE (EV) ENGINE (VM1)

**File**: `/bot/ev_engine.py`

### Purpose

Replace rule-based "score >= 65 → trade" with empirical edge gating based on actual R-metrics.

### EV Formula

```
EV = (P_win × avg_win_R) - ((1 - P_win) × |avg_loss_R|)
```

Where:
- `P_win`: Historical win rate (decimal, e.g., 0.64 for 64%)
- `avg_win_R`: Average R-multiple of winning trades (e.g., 2.1)
- `avg_loss_R`: Average R-multiple of losing trades (negative, e.g., -0.9)

### Decision Thresholds (Base)

```
EV > 0.10R (TRADE_EV_THRESHOLD)
  → VERDICT: "TRADE"
  → Size multiplier: 1.0 to 1.3 (scales with EV strength)
  
0.0R < EV < 0.10R (REDUCED_EV_THRESHOLD)
  → VERDICT: "REDUCED"
  → Size multiplier: 0.5 to 0.8 (marginal edge)
  
EV < 0.0R
  → VERDICT: "REJECT"
  → Size multiplier: 0.0 (no trade)
  
< 8 samples
  → VERDICT: "INSUFFICIENT_DATA"
  → Size multiplier: 0.8 (learning phase, reduced size)
```

### Regime-Specific Adjustments

Thresholds adjusted per market regime:

```python
REGIME_EV_ADJUSTMENTS = {
    "trending_up": -0.05,          # Lower bar (trend adds edge)
    "trending_down": -0.05,
    "breakout": -0.03,
    "sideways": 0.0,               # Normal bar
    "ranging": 0.0,
    "mean_reversion": 0.0,
    "volatile": +0.05,             # Higher bar (more noise)
    "high_volatility": +0.05,
    "quiet": 0.0,
    "low_liquidity": +0.10,        # Much higher bar (risky)
}
```

### Calibrated Lookup

When computing EV, tries most-specific key first, falls back to coarser:

```
1. "{scanner}_{side}_{regime}_{session}"  (most specific)
   e.g., "ema_momentum_long_trending_up_europe"
2. "{scanner}_{side}_{regime}"
   e.g., "ema_momentum_long_trending_up"
3. "{scanner}_{side}"
   e.g., "ema_momentum_long"
4. "{scanner}"  (coarsest, original)
   e.g., "ema_momentum"
```

This allows fine-tuned edge measurement by direction, regime, and session while gracefully falling back to aggregate data.

### Output: EVResult

```python
@dataclass
class EVResult:
    scanner: str                # "ema_momentum"
    ev: float                   # e.g., 0.15 (in R-multiples)
    p_win: float                # e.g., 0.64 (win probability)
    avg_win_r: float            # e.g., 2.1
    avg_loss_r: float           # e.g., -0.9 (negative)
    sample_count: int           # 25 trades
    verdict: str                # "TRADE", "REDUCED", "REJECT", "INSUFFICIENT_DATA"
    size_multiplier: float      # 0.0-1.3 (position size adjustment)
    reason: str                 # Human-readable explanation
```

---

## 10. DECISION ENGINE (VM1)

**File**: `/bot/decision_engine.py`

### High-Level Market Assessment

Synthesizes all available data into a single actionable directive:

```python
@dataclass
class Decision:
    # Market State
    market_state: str           # TREND_STRONG, TREND_WEAK, SIDEWAYS, CHOP, LOW_LIQ
    edge_status: str            # STRONG, MEDIUM, WEAK, OFF
    action: str                 # LONG, SHORT, WAIT
    
    # Best Opportunity
    best_symbol: str            # "BTC/USDT"
    best_scanner: str           # "ema_momentum"
    best_score: float           # weighted score
    best_grade: str             # A/B/C
    best_tier: str              # strong/valid/weak
    
    # Risk State
    risk_state: str             # NORMAL, REDUCED, BLOCKED
    risk_reason: str            # explanation
    
    # Context
    regime: str                 # current market regime
    session: str                # europe, asia_early, etc.
    rolling_expectancy: float   # last 20 trades' EV
    drawdown_pct: float         # current drawdown
    signals_this_hour: int      # rate limiting
    
    # Trade Plan (when action is LONG/SHORT)
    best_entry: float           # entry price
    best_stop: float            # stop loss
    best_target: float          # TP1 (1.5× R)
    best_atr: float             # ATR for context
    
    # EV Data
    best_ev: float              # EV of best scanner in R
    best_p_win: float           # win probability
    ev_verdict: str             # TRADE / REDUCED / REJECT / INSUFFICIENT_DATA
    
    reason: str                 # "Strong edge in trending market"
    updated_at: str             # ISO timestamp
```

### Decision Logic

**Edge Status** (based on rolling_expectancy):
- STRONG: rolling_exp > 0.3R (proven positive edge)
- MEDIUM: rolling_exp > 0.1R (decent edge)
- WEAK: rolling_exp > 0.0R (marginal edge)
- OFF: rolling_exp < 0.0R (no edge or in drawdown)

**Action Selection**:
- If no valid signals → action = "WAIT"
- If best_score >= 65 && ev_verdict != "REJECT" → action = "LONG" or "SHORT"
- If risk_state = "BLOCKED" → force action = "WAIT" (override signal)

**Risk State**:
- NORMAL: daily loss < limit, open positions < max, equity above peak - max_dd
- REDUCED: daily loss > 50% of limit (reduce size by 50%)
- BLOCKED: daily loss > limit OR max_dd exceeded (no new trades)

---

## 11. ML TRAINING PIPELINE (VM2)

**Files**: `/ml_training/trainer.py`, `candidate_trainer.py`, `feature_builder.py`, `ml_model.py`, `scanner_backtester.py`

### Workflow Phases

**Phase 1: Candle Collection**

File: `/ml_training/candle_collector.py`

Fetches historical OHLCV data from exchange, stores as parquet for fast access.

**Symbols**: BTC/USDT, ETH/USDT, SOL/USDT, AVAX/USDT, + alts (11 total)

**Timeframes & Lookback**:
```python
TF_DAYS = {
    "1m": 90 days,
    "3m": 120 days,
    "5m": 150 days,
    "15m": 180 days,
    "1h": 180 days,
    "4h": 180 days,
    "1d": 365 days,
}
```

**Storage**: `storage/candle_cache/{SYMBOL}_{TIMEFRAME}.parquet`

---

**Phase 2: Scanner Backtesting**

File: `/ml_training/scanner_backtester.py`

Runs each scanner on historical data with **identical live logic**, measures actual edge.

**Simulated Trade Lifecycle** (for each scanner signal):
1. Find entry bar and entry price
2. Calculate SL and TP levels
3. Simulate trade forward:
   - Track highest/lowest price
   - Check for TP1/TP2/TP3 hits
   - Check for SL hit
   - Apply 4-hour timeout
   - Apply Scalper window (27 min BTC, 12 min others) for free exit
4. Record exit: price, R-multiple, MFE, MAE, exit reason
5. Apply fees: entry_fee + settlement_fee + exit_fee

**Output**: Per-scanner backtest results saved to `storage/backtest_results/{SCANNER}_{SYMBOL}_{TIMEFRAME}.json`

**Metrics Computed**:
```json
{
    "scanner": "ema_momentum",
    "symbol": "BTC/USDT",
    "timeframe": "5m",
    "total_trades": 145,
    "win_rate": 64.1,           // percentage
    "avg_r": 0.42,              // average R-multiple
    "total_r": 60.9,            // sum of all R-multiples
    "mfe_avg": 1.8,             // max favorable
    "mae_avg": 0.5,             // max adverse
    "sharpe": 1.23,             // Sharpe ratio
    "max_dd": 2.1,              // max drawdown in R
    "expectancy": 0.42,         // same as avg_r
    "regime_breakdown": {       // per-regime stats
        "trending_up": { "wr": 72, "avg_r": 0.52, "total": 45 },
        "sideways": { "wr": 55, "avg_r": 0.28, "total": 32 },
        ...
    },
    "session_breakdown": {      // per-session stats
        "europe": { "wr": 68, "avg_r": 0.58, ... },
        "us": { "wr": 61, "avg_r": 0.38, ... },
        ...
    }
}
```

---

**Phase 3: Feature Building**

File: `/ml_training/feature_builder.py`

Extracts ~35 features from pure candle math (NOT indicators).

**Feature Categories**:

1. **Momentum Features** (price change, NOT raw price):
   - Return (last close - previous close) / previous close
   - Return over 2, 3, 5, 10 candles

2. **Volatility Features**:
   - ATR (14), ATR ratio (current / 20-bar SMA)
   - Candle range as % of ATR
   - BB bandwidth, BB width ratio

3. **Candle Structure** (pure OHLC geometry):
   - Body ratio (body / range)
   - Upper wick ratio
   - Lower wick ratio
   - Is bullish (1 if close > open, 0 otherwise)

4. **Trend Strength**:
   - EMA gap: (ema8 - ema21) / ema21 (normalized)
   - EMA slope: pct_change over 3 bars

5. **Volume Intelligence**:
   - Relative volume (current / 20-bar SMA)
   - Volume z-score (deviation from mean in std devs)

6. **Market Context**:
   - Distance from VWAP
   - VWAP band level (how close to ±1σ or ±2σ)
   - RSI (kept for compatibility, NOT used as raw feature—only in gate decisions)

7. **Compression/Expansion**:
   - BB squeeze indicator (1 if in bottom 25% bandwidth, 0 otherwise)
   - Bandwidth trend (expanding vs contracting)

8. **Time Features**:
   - Session encoding (one-hot: asia_late, asia_early, europe, us)
   - Hour of day (0-23)

9. **Recent Memory** (last N candles aggregates):
   - 3-bar return, 5-bar return (trend persistence)
   - 5-bar high/low (swing extremes)

10. **Trade-Specific** (added at scoring time, not in training):
    - Distance from entry to TP (in %)
    - Distance from entry to SL (in %)
    - Risk/reward ratio

**Key Philosophy**: No indicators as features. All features are either:
- Price movement (returns, not levels)
- Volatility (ATR ratios, ranges)
- Participation (volume z-scores, not raw volume)
- Geometry (wick ratios, body ratios)

---

**Phase 4: ML Model Training**

File: `/ml_training/ml_model.py`

Trains classifier on historical candles to predict trade success.

**Architecture**:
- **Classifier**: Random Forest (works on small datasets, interpretable)
- **Input**: ~35 features from feature_builder
- **Output**: Probability (0-1) that price reaches TP before SL within X minutes
- **Validation**: Walk-forward validation (train months 1-2, test month 3)
- **Calibration**: CalibratedClassifierCV (convert raw RF probabilities to true probabilities)

**Training Modes**:
```python
label_mode = "directional"     # Will price move +0.2 ATR in my direction in next 12 bars?
# OR
label_mode = "binary"          # Will trade reach TP before SL?
# OR
label_mode = "regression"      # What will be the R-multiple?
```

**Output: Feature Importances**

```python
feature_importances = {
    "return_3bar": 0.18,              # 3-bar return is most important
    "atr_ratio": 0.14,                # ATR volatility regime
    "candle_body_ratio": 0.10,        # Candle structure (rejection wicks matter)
    "volume_zscore": 0.09,
    "ema_gap": 0.08,
    ...
}
```

This reveals what actually matters for trade success (not what the rule-coded veto system uses).

---

**Phase 5: Candidate Trainer**

File: `/ml_training/candidate_trainer.py`

Focuses on **scanner candidates** — not predicting the market, but ranking scanner outputs.

**Pipeline per Scanner**:
1. Scan all bars with the scanner (e.g., ema_momentum)
2. For each signal found, compute:
   - **Market State Features** (from feature_builder): momentum, volatility, trend strength, etc.
   - **Gate/Veto Features**: regime type (one-hot encoded), regime stability, HTF alignment, EMA slope, VWAP distance, volume z-score, session, volatility regime, candle body_ratio, range_vs_atr
3. Simulate trade outcome: TP before SL? What was the R-multiple?
4. Train classifier:
   - Input: market features + gate features
   - Output: 1 if trade won, 0 if lost
5. Walk-forward validate
6. Report:
   - **Raw WR**: all candidates
   - **Rule-filtered WR**: after hand-coded veto system applied
   - **ML Top-Quartile WR**: if model ranks candidates, top 25% by score

**Key Insight**: The gate/veto features ARE inputs to the model. The model learns which combinations matter most and can potentially improve beyond hand-coded rules.

**Example Output**:
```
Scanner: ema_momentum (BTC/USDT, 5m)
Raw candidates: 145
Raw WR: 62%
After veto system: 92 accepted (64% WR)
ML top-quartile: 23 trades (72% WR)
→ ML improves WR from 64% to 72% by reranking within accepted candidates
```

---

**Phase 6: ML Dashboard (Port 8081)**

File: `/ml_training/dashboard.py`

Serves training progress, results, feature importances, walk-forward metrics.

**API Endpoints**:
- `/api/status`: Current training phase, progress %
- `/api/results`: All backtest results (scanner × symbol × TF)
- `/api/scanner/{name}`: Detailed results for one scanner
- `/api/models`: Trained model metadata (feature count, accuracy, walk-forward scores)
- `/api/features`: Feature importance breakdown
- `/api/comparison`: Timeframe comparison (1m vs 5m vs 15m)
- `/api/history`: Training run history (dates, metrics, improvements)

---

## 12. SIGNAL LEARNER (VM1)

**File**: `/bot/signal_learner.py`

### Adaptive Learning from Trade Outcomes

Continuously learns from closed trades to improve future signal quality.

**Tracked Dimensions**:

1. **Per-Setup Performance**:
   ```python
   {
       "ema_momentum": {
           "total": 25,
           "wins": 16,
           "losses": 9,
           "win_rate": 0.64,
           "avg_pnl": 0.42,
           "confidence_mult": 1.1,    # Adjust confidence for future signals
           ...
       }
   }
   ```

2. **Per-Condition Performance** (binned features):
   ```python
   {
       "rsi_zone:oversold": {"wins": 8, "losses": 2, "avg_pnl": 0.65},
       "rsi_zone:overbought": {"wins": 3, "losses": 5, "avg_pnl": -0.18},
       "volume:high": {"wins": 12, "losses": 3, "avg_pnl": 0.58},
       "hour:09": {"wins": 4, "losses": 6, "avg_pnl": -0.12},
       "regime:trending_up": {"wins": 18, "losses": 4, "avg_pnl": 0.52},
       ...
   }
   ```

3. **Setup + Condition Combinations**:
   ```python
   {
       "ema_momentum:rsi_zone:oversold": {"wins": 6, "losses": 1, "score": 0.85},
       "ema_momentum:volume:high": {"wins": 12, "losses": 2, "score": 0.86},
       ...
   }
   ```

### Confidence Adjustment Algorithm

When a new signal is generated:
1. Determine setup type (e.g., "ema_momentum")
2. Extract feature conditions (RSI zone, volume level, regime, session, hour)
3. Look up historical performance:
   - Setup-level adjustment (e.g., ema_momentum has 64% WR → 1.1× multiplier)
   - Condition-level adjustments (e.g., volume high → +0.9× bonus)
   - Combo adjustments (setup + condition → specific score)
4. Apply hard floors:
   - If setup has < 5 samples, don't adjust (insufficient data)
   - If combo has very negative history, potentially block (add to _blocked_combos)
5. Multiply original confidence by adjustment factor

**Example**:
```
Signal: ema_momentum, confidence 70, BTC long, high volume, oversold RSI, europe session
Setup adjustment: 1.1× (ema_momentum has 64% WR, 25 trades)
Volume bonus: +5 (high volume historically good, +5% extra)
Session bonus: +5 (europe session best, +5%)
Adjusted confidence: 70 × 1.1 + 5 + 5 = 86
```

### Loss Streak Protection

Tracks current win/loss streak (updated by orchestrator):
- On loss streak: reduce all confidence by (streak × 2)%
- On win streak: boost all confidence by (streak × 1)%

Example: 3-loss streak → all new signals reduced by 6 confidence points.

---

## 13. TRADE MONITOR AGENT (VM1)

**File**: `/bot/trade_monitor.py`

### Real-Time Loss Analysis & Recommendations

Every closed trade is analyzed for root cause:

**Loss Categories** (for losses only):
- `wrong_direction`: Shorting uptrend, longing downtrend
- `noise_stopout`: SL too tight, stopped by noise
- `weak_setup`: Low confidence / Grade C (should filter out)
- `post_tp1_reversal`: Hit TP1 then reversed to SL
- `wide_sl_loss`: SL reasonable, just wrong trade
- `expired`: 4-hour timeout
- `fast_stop`: Stopped within 2 minutes (whipsaw)
- `high_conf_failure`: High confidence (75+) but lost

### Metrics Tracked

**Running Metrics**:
- Total analyzed, wins, losses
- Win streak / loss streak (current)
- Max win streak, max loss streak
- Rolling 20-trade win rate & PnL
- Peak balance & current drawdown
- Max drawdown ever

**Per-Setup Loss Tracking**:
```python
{
    "ema_momentum": {
        "total_losses": 5,
        "loss_categories": {
            "wrong_direction": 2,
            "post_tp1_reversal": 1,
            "wide_sl_loss": 2,
        },
        "avg_loss_r": -0.85,
        "recommendation": "Increase min confidence from 65 to 70"
    }
}
```

**Per-Side Tracking**:
```python
{
    "long": {"wins": 8, "losses": 3, "pnl": 0.65},
    "short": {"wins": 4, "losses": 6, "pnl": -0.42},
}
```

**Hourly Breakdown**:
```python
hourly_stats = {
    0: {"wins": 1, "losses": 2, "pnl": -0.18},
    9: {"wins": 3, "losses": 0, "pnl": 0.85},  # 9am IST is strong
    ...
}
```

### Actionable Recommendations

Generated when patterns emerge:

```python
{
    "type": "scanner_underperformance",
    "severity": "high",
    "message": "ema_momentum shorts have -0.2R avg — suppress SHORT side",
    "action": "Suppress short signals for ema_momentum until 5 more wins"
},
{
    "type": "time_pattern",
    "severity": "medium",
    "message": "2:30-9:00 IST has 37% WR — reduce size 50% or block entirely",
    "action": "Implement asia_late session penalty (done in strategy)"
},
{
    "type": "drawdown_recovery",
    "severity": "critical",
    "message": "Currently in 2.1% drawdown from peak — reduce position size to 0.5×",
    "action": "Wait for next session or reduce EV threshold +0.10R"
}
```

---

## 14. SCANNER WEIGHTS MANAGER

**File**: `/strategies/scanner_weights.py`

### Dynamic Weight Adjustment

Each scanner gets a weight multiplier (0.0-1.4) based on R-performance data.

**Thresholds**:
```
Expectancy > +0.5R  → STRONG_BOOST (1.4× weight)
Expectancy > +0.3R  → BOOSTED (1.2× weight)
Expectancy > 0.0R   → ACTIVE (1.0× weight, normal)
Expectancy < -0.1R  → REDUCED (0.6× weight)
Expectancy < -0.3R  → SUPPRESSED (0.0× weight, disabled)
```

**Example**:
- ema_momentum: 25 trades, 64% WR, +0.42R avg → expectancy = +0.42R → BOOSTED (1.2×)
- structure_bounce: 18 trades, 72% WR, +0.58R avg → expectancy = +0.58R → STRONG_BOOST (1.4×)
- supertrend_flip: 23 trades, 26% WR, -0.30R avg → expectancy = -0.30R → SUPPRESSED (0.0×, runs in shadow only)

**Shadow Mode**:
Suppressed scanners still run in the background, collecting data (for recovery).

**Recovery Mechanism**:
If a suppressed scanner gets 10+ trades with +0.1R or better expectancy, enter "probation":
- Run at 0.6× weight (REDUCED) instead of 0.0
- If next 20 trades show +0.3R or better, fully restore (ACTIVE)

---

## 15. RISK MANAGEMENT (VM1)

**File**: `/risk/manager.py`

### Risk Constraints

**Daily Loss Limit**:
```yaml
max_daily_loss_pct: 3.0    # Stop trading if lost 3% of account in a day
```

**Position Limits**:
```yaml
max_open_positions: 3       # Max 3 concurrent trades
max_position_size_usd: 10000  # Max $10k per trade
max_exposure_per_symbol_pct: 50.0  # Max 50% of account in one symbol
max_correlated_exposure_pct: 100.0  # Max 100% across correlated pairs (BTC=anchor)
```

**Leverage Limits**:
```yaml
default_leverage: 5         # Use 5× by default
max_leverage: 20            # Hard cap at 20×
```

**Stop Loss Sizing**:
```yaml
stop_loss:
  type: "atr"
  atr_multiplier: 1.8       # SL = close ± 1.8 × ATR (5m)
```

### Circuit Breaker

After 3 losses in a row:
- Cool-off for 60 minutes (no new trades)
- Don't enforce — just warn user

---

## 16. DASHBOARDS (VM1 & VM2)

### Main Dashboard (VM1, Port 8080)

**File**: `/dashboard/server.py`

Serves a single-page web UI with real-time updates.

**API Endpoints**:
- `/api/status`: Bot status (running/paused/stopped), mode, symbols
- `/api/positions`: Open positions with P&L
- `/api/signals`: Latest signals (active + recent closed)
- `/api/trades`: Trade history (last 50)
- `/api/performance`: PnL curve, win rate, Sharpe ratio
- `/api/scanner_stats`: Per-scanner R-metrics
- `/api/decision`: Current decision (LONG/SHORT/WAIT + reason)
- `/api/alerts`: Alert history (Telegram, console, email)

**UI Features**:
- Real-time candle display (last price, 1m/5m/15m)
- Signal board: incoming signals with confidence/grade
- Position dashboard: open trades, TP/SL levels, P&L
- Performance chart: equity curve, drawdown, rolling win rate
- Scanner health: weights, expectancies, status
- Risk metrics: daily loss %, exposure, leverage
- Journal export: CSV/JSON download

**Refresh Interval**: 5 seconds (configurable)

---

### ML Dashboard (VM2, Port 8081)

**File**: `/ml_training/dashboard.py`

Shows training progress and backtest results.

**Sections**:
- Training Status: current phase, progress %, ETA
- Backtest Results: table of all scanners with metrics
- Walk-Forward Metrics: accuracy, precision, recall per fold
- Feature Importance: bar chart of top-20 features
- Timeframe Comparison: 1m vs 5m vs 15m edge
- Candidate Trainer Results: raw WR vs ML-filtered WR improvement
- Model Cards: individual model performance (symbol, side, timeframe)

---

## 17. CONFIGURATION (settings.yaml)

**File**: `/config/settings.yaml`

### Structure

```yaml
bot:
  name: "VN Edge"
  version: "2.0.0"
  mode: "paper"                    # signal_only | paper | live | backtest | forward_test
  operating_mode: "paper_learning"

symbols:
  - "BTC/USDT"
  - "ETH/USDT"
  - "AVAX/USDT"

exchange:
  name: "delta"                    # Connects to Delta India
  region: "india"
  market_type: "futures"

timeframes:
  primary: "1m"
  higher: "15m"
  trigger: "1m"

strategy:
  active: "multi_strategy"         # Scalp + investment strategies

indicators:
  ema: { fast: 9, medium: 21, slow: 50, trend: 200 }
  rsi: { period: 14, overbought: 70, oversold: 30 }
  macd: { fast: 12, slow: 26, signal: 9 }
  atr: { period: 14, multiplier: 1.5 }
  bollinger: { period: 20, std_dev: 2.0 }

filters:
  min_confidence: 70               # Raised from 55
  min_grade: "B"                   # Raised from "C"
  chop_filter: false
  spread_max_pct: 0.15
  cooldown_seconds: 180            # 3 min between signals (same symbol)
  max_signals_per_hour: 12         # Quality > quantity

risk:
  risk_per_trade_pct: 1.0
  max_position_size_usd: 10000
  max_daily_loss_pct: 3.0
  max_open_positions: 3
  default_leverage: 5
  max_leverage: 20
  
  stop_loss:
    type: "atr"
    atr_multiplier: 1.8            # Wider SL for noise survival
  
  take_profit:
    tp1_rr: 1.5
    tp1_close_pct: 60              # Close 60% at TP1
    tp2_rr: 2.5
    tp2_close_pct: 25              # Close 25% at TP2
    tp3_rr: 4.0
    tp3_close_pct: 15              # Trail 15%
  
  trailing:
    enabled: true
    activation_rr: 0.8
    trail_pct: 0.4
    break_even_after_tp1: true

paper_trading:
  initial_balance: 10000
  maker_fee_rate: 0.0002           # Scalper: 0.02%
  taker_fee_rate: 0.0005           # Scalper: 0.05%
  settlement_fee_rate: 0.0006      # Delta: 0.06%
  slippage_pct: 0.05

grid:
  enabled: true
  grid_pct: 0.003                  # 0.3% spacing
  num_levels: 15
  position_usd: 50
  leverage: 10
  smart_buys: true

alerts:
  telegram:
    enabled: true
    alert_on_confirmed: true
    alert_on_tp_hit: true
    alert_on_sl_hit: true

logging:
  level: "INFO"
  file_enabled: true
  console_enabled: true

dashboard:
  enabled: true
  refresh_interval: 5
```

---

## 18. DATA FLOW DIAGRAM

```
┌─────────────────────────────────────────────────────────────────┐
│  VM1 (TRADING NODE)                                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌─ Exchange (Delta India) ─┐                                   │
│  │ WebSocket: prices        │                                   │
│  │ REST: positions, balance │                                   │
│  └───────────────┬──────────┘                                   │
│                  │                                              │
│                  ▼                                              │
│  ┌──────────────────────────────────┐                           │
│  │ DataFeed (feed.py)               │                           │
│  │ Subscribe to OHLCV (1m,5m,15m)   │                           │
│  │ Buffer candles, emit events      │                           │
│  └────────────────┬─────────────────┘                           │
│                   │                                              │
│    ┌──────────────┴──────────────┐                              │
│    │                             │                              │
│    ▼                             ▼                              │
│ ┌─────────────────────┐    ┌──────────────────────┐            │
│ │ ScalpStrategy       │    │ DecisionEngine       │            │
│ │ analyze()           │    │ synthesize()         │            │
│ │ - 15 scanners       │    │ - best signal        │            │
│ │ - regime filter     │    │ - risk state         │            │
│ │ - EV gating         │    │ - action: LONG/SHORT │            │
│ └────────┬────────────┘    └────────┬─────────────┘            │
│          │                          │                           │
│          └──────────────┬───────────┘                           │
│                         │                                       │
│                         ▼                                       │
│        ┌────────────────────────────┐                          │
│        │ RiskManager                │                          │
│        │ - daily loss check         │                          │
│        │ - position sizing          │                          │
│        │ - leverage validation      │                          │
│        └─────────────┬──────────────┘                          │
│                      │                                         │
│                      ▼                                         │
│        ┌─────────────────────────────────┐                    │
│        │ ExecutionEngine (paper or live)  │                    │
│        │ - place orders                  │                    │
│        │ - manage TP/SL                  │                    │
│        │ - track P&L                     │                    │
│        └─────────────┬────────────────────┘                   │
│                      │                                         │
│       ┌──────────────┼──────────────┐                          │
│       │              │              │                          │
│       ▼              ▼              ▼                          │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐             │
│  │SignalTrkr│  │TradeMonitor  │  │AlertManager  │             │
│  │TP/SL    │  │ loss analysis │  │ Telegram     │             │
│  │monitoring│  │recommendations│  │ console      │             │
│  └────┬─────┘  └──────┬────────┘  └──────┬───────┘            │
│       │               │                   │                    │
│       │               │                   ▼                    │
│       │               │           (notifications)             │
│       │               │                                        │
│       └───────┬───────┴────────┐                              │
│               │                │                              │
│               ▼                ▼                              │
│         ┌──────────────┐  ┌──────────────┐                   │
│         │TradeJournal  │  │Dashboard     │                   │
│         │ CSV/JSON     │  │ port 8080    │                   │
│         │ export       │  │ REST APIs    │                   │
│         └──────────────┘  └──────────────┘                   │
│                                                                │
│  ┌──────────────────────────────────────┐                    │
│  │ SignalLearner (continuous learning)  │                    │
│  │ - per-setup confidence mult          │                    │
│  │ - per-condition adjustments          │                    │
│  │ - blocked combos                     │                    │
│  └──────────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────┐
│  VM2 (ML/BACKTEST NODE)                                       │
├──────────────────────────────────────────────────────────────┤
│                                                                │
│  ┌────────────────────────────────┐                          │
│  │ CandleCollector                │                          │
│  │ - fetch 90-180 days of OHLCV   │                          │
│  │ - cache as parquet             │                          │
│  └────────────┬───────────────────┘                          │
│               │                                               │
│               ▼                                               │
│  ┌────────────────────────────────┐                          │
│  │ ScannerBacktester              │                          │
│  │ - run each scanner on hist data│                          │
│  │ - measure actual edge per scan │                          │
│  │ - walk-forward validation      │                          │
│  └────────────┬───────────────────┘                          │
│               │                                               │
│       ┌───────┴────────────────┐                             │
│       │                        │                             │
│       ▼                        ▼                             │
│  ┌─────────────────┐   ┌──────────────────┐                │
│  │FeatureBuilder   │   │MLProbabilityModel│                │
│  │ - pure candle   │   │ - classify win/loss               │
│  │   math features │   │ - walk-forward validate           │
│  │ - 35 features   │   │ - feature importance              │
│  └────────┬────────┘   └──────┬───────────┘                │
│           │                   │                             │
│           └───────┬───────────┘                             │
│                   │                                         │
│                   ▼                                         │
│          ┌──────────────────┐                              │
│          │CandidateTrainer  │                              │
│          │ - rank scanner   │                              │
│          │   candidates     │                              │
│          │ - compare raw vs │                              │
│          │   ML-filtered WR │                              │
│          └────────┬─────────┘                              │
│                   │                                         │
│                   ▼                                         │
│          ┌──────────────────┐                              │
│          │ML Dashboard      │                              │
│          │ port 8081        │                              │
│          │ training status  │                              │
│          │ results & charts │                              │
│          └──────────────────┘                              │
│                                                             │
└─────────────────────────────────────────────────────────────┘


┌──────────────────────────────────────┐
│ SHARED STORAGE (Cloud-synced)        │
├──────────────────────────────────────┤
│ storage/                             │
│  ├── active_signals.json             │
│  ├── closed_signals.json             │
│  ├── signal_stats.json (EV data)     │
│  ├── signal_learner.json             │
│  ├── scanner_weights.json            │
│  ├── trade_monitor.json              │
│  ├── trades.csv / trades.json        │
│  ├── candle_cache/                   │
│  │   ├── BTC_USDT_1m.parquet         │
│  │   ├── BTC_USDT_5m.parquet         │
│  │   └── ...                         │
│  ├── backtest_results/               │
│  │   ├── ema_momentum_BTC_5m.json    │
│  │   └── ...                         │
│  └── ml_models/                      │
│      ├── btc_long_5m.pkl             │
│      └── ...                         │
└──────────────────────────────────────┘
```

---

## 19. KEY DESIGN PRINCIPLES

1. **Empirical Over Theoretical**: Every decision (trade, size, threshold) backed by actual R-metrics from closed trades, not wishful thinking.

2. **Graduated Signals Not Binary**: Signals emitted at tiers (strong/valid/weak/near_miss), allowing the user to decide risk tolerance.

3. **Pure Candle Math for ML**: Features extracted from price, volume, and volatility only—no indicator soup that obscures real relationships.

4. **Regime-Aware Execution**: Market regime detected automatically; different scanners allowed in different conditions (trend followers in trends, mean-reversion in ranges).

5. **Walk-Forward Always**: All ML models validated on out-of-sample data (months 1-2 training, month 3 testing), preventing curve-fitting illusions.

6. **Continuous Learning**: Every closed trade improves future signals:
   - SignalLearner adjusts confidence per setup + condition
   - ScannerWeightManager suppresses/boosts scanners based on edge
   - TradeMonitorAgent provides actionable loss categorization

7. **Transparency**: Every decision logged and accessible via dashboard:
   - Why signal was generated (confirmations)
   - Why signal was rejected (penalties, hard blocks)
   - Why trade was closed (TP/SL/timeout/scalper window)
   - Why loss occurred (root cause category)

8. **Risk-First Design**: Position sizing, leverage, daily loss limits all enforced before execution engine is called.

9. **Session & Regime Awareness**: Data shows different sessions have different win rates; strategy adapts expectations (session penalty/boost) and applies regime-specific scanner filtering.

10. **Recovery Mechanism**: Suppressed scanners can recover (shadow mode → probation → full restoration) based on fresh edge evidence.

---

## 20. SUMMARY TABLE

| Component | File | Purpose | VM |
|-----------|------|---------|-----|
| BotOrchestrator | bot/orchestrator.py | Central event loop coordinator | 1 |
| ScalpStrategy | strategies/scalp_strategy.py | 15-scanner signal generator | 1 |
| RegimeFilter | strategies/regime_filter.py | Market regime detection + scanner filtering | 1 |
| EVEngine | bot/ev_engine.py | Empirical edge gating (R-metrics based) | 1 |
| DecisionEngine | bot/decision_engine.py | Synthesize all signals → LONG/SHORT/WAIT | 1 |
| ExecutionEngine (Paper/Live) | execution/engine.py, paper_engine.py | Order placement + TP/SL management | 1 |
| SignalTracker | bot/signal_tracker.py | TP/SL monitoring, P&L recording, R-metrics | 1 |
| SignalLearner | bot/signal_learner.py | Adaptive confidence adjustment per setup+condition | 1 |
| TradeMonitorAgent | bot/trade_monitor.py | Loss analysis, root cause categorization | 1 |
| ScannerWeightManager | strategies/scanner_weights.py | Dynamic scanner weight adjustment | 1 |
| DashboardServer | dashboard/server.py | Main dashboard (port 8080) | 1 |
| CCXTClient | exchange/ccxt_client.py | Exchange integration (WebSocket + REST) | 1 |
| DataFeed | data/feed.py | Candle streaming & buffering | 1 |
| RiskManager | risk/manager.py | Daily loss, position count, leverage limits | 1 |
| TradeJournal | journal/trade_journal.py | Trade export (CSV/JSON) | 1 |
| AlertManager | alerts/manager.py | Telegram, console, email notifications | 1 |
| CandleCollector | ml_training/candle_collector.py | Fetch + cache 90-180 day OHLCV | 2 |
| ScannerBacktester | ml_training/scanner_backtester.py | Backtest each scanner, measure edge | 2 |
| FeatureBuilder | ml_training/feature_builder.py | Extract 35 features from pure candle math | 2 |
| MLProbabilityModel | ml_training/ml_model.py | Train RF classifier for trade success | 2 |
| CandidateTrainer | ml_training/candidate_trainer.py | Rank scanner candidates via ML | 2 |
| MLDashboard | ml_training/dashboard.py | Training progress + results (port 8081) | 2 |

---

This comprehensive architecture document covers all major systems, data flows, design decisions, and implementations. Each component's role in the larger ecosystem is clear, and the empirical, adaptive nature of the bot is emphasized throughout.