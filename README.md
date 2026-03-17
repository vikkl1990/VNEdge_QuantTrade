# Crypto Trading Bot

Automated crypto derivatives trading bot built for **Delta Exchange India**. Uses multi-timeframe technical analysis with 8 scalp scanners and an investment-grade trend follower to generate high-probability trade signals.

## Features

- **8 Scalp Scanners** — EMA Momentum, Trend Continuation, RSI Divergence, RSI Extreme, Momentum Ride, BB Band Walk, Post-Impulse, BB Squeeze
- **Investment Strategy** — Momentum trend follower with regime-aware filtering
- **Market Regime Detection** — Adapts thresholds based on volatility, liquidity, and trend state
- **Real-time Dashboard** — Live signal status, per-scanner diagnostics, trade history, and VM health monitoring
- **Risk Management** — ATR-based stop losses, position sizing, max drawdown limits, and correlation guards
- **Telegram Alerts** — Instant notifications for signals, trades, and system events
- **Signal Learning** — Tracks signal outcomes to refine scanner confidence over time

## Architecture

```
├── strategies/          # Signal generation
│   ├── scalp_strategy.py    # 8 scalp scanners (1m/5m timeframes)
│   ├── momentum_trend.py    # Investment trend follower (15m/1h)
│   ├── regime.py            # Market regime detection
│   ├── scoring.py           # Signal confidence scoring
│   └── multi_strategy.py    # Strategy coordinator
├── bot/                 # Core orchestration
│   ├── orchestrator.py      # Main bot loop
│   ├── trade_monitor.py     # Open position management
│   ├── signal_tracker.py    # Signal outcome tracking
│   └── heartbeat.py         # Health monitoring
├── exchange/            # Exchange connectivity
├── execution/           # Order execution engine
├── risk/                # Risk management & position sizing
├── dashboard/           # Web UI (Flask)
│   ├── server.py            # API endpoints
│   ├── templates/           # HTML templates
│   └── static/              # JS, CSS assets
├── alerts/              # Telegram notification system
├── journal/             # Trade journaling
├── config/              # YAML configuration
├── deploy/              # OCI deployment scripts
├── backtest/            # Backtesting framework
└── main.py              # Entry point
```

## Scanners

| Scanner | What It Catches | Key Conditions |
|---------|----------------|----------------|
| EMA Momentum | Fresh EMA crossovers | EMA8 crosses EMA21, RSI 40-70, volume > 1.2x |
| Trend Continuation | Pullbacks in trends | EMA stack aligned, RSI 40-58, bullish candle at support |
| RSI Divergence | Momentum divergences | Price new high + RSI lower high, or inverse |
| RSI Extreme | Oversold/overbought reversals | RSI < 25 or > 75 with reversal candle |
| Momentum Ride | Running trends | EMA8 > EMA21 > EMA50, MACD accelerating, volume > 1.5x |
| BB Band Walk | Bollinger Band breakouts | Price above BB upper 2+ candles, volume > 1.3x |
| Post-Impulse | Re-entries after impulse moves | Large impulse candle + shallow pullback + small current candle |
| BB Squeeze | Volatility expansion | BB bandwidth expanding after squeeze, directional breakout |

## Operating Modes

```bash
# Signal-only (default) — generates alerts, no trades
python main.py --mode signal_only

# Paper trading — simulated trades on live data
python main.py --mode paper

# Live trading
python main.py --mode live

# Backtesting
python main.py --mode backtest --symbols BTCUSDT

# Forward test — paper trades with full logging
python main.py --mode forward_test
```

## Setup

### Prerequisites
- Python 3.10+
- Delta Exchange India account (API key & secret)

### Installation
```bash
git clone https://github.com/vikkl1990/crypto-trading-bot.git
cd crypto-trading-bot
pip install -r requirements.txt
cp .env.example .env  # Add your API keys
```

### Configuration
Edit `.env` with your credentials:
```
DELTA_API_KEY=your_key
DELTA_API_SECRET=your_secret
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### Run
```bash
python main.py --mode signal_only
```

Dashboard available at `http://localhost:8080`

## Deployment

Deployed on **Oracle Cloud (OCI)** free-tier ARM instance. See `deploy/` for:
- `oracle_vm_create.sh` — Provision new VM
- `oracle_vm_manage.sh` — Start/stop/status management
- `upgrade_vm.sh` — Upgrade to A1.Flex (2 OCPU / 4GB RAM)

## Performance

- **100 trades** tracked
- **57% win rate** overall
- Best scanner: EMA Momentum (66% WR, +$15.25)
- Best scanner: Trend Continuation (83% WR, +$2.18)

## Dashboard

The web dashboard provides:
- Live signal scanner status with per-scanner diagnostic reasons
- Signal drought indicator (why no signals are firing)
- Market regime badges (trending, sideways, high-volatility, low-liquidity)
- Trade history and P&L tracking
- VM infrastructure health monitoring

---

Built with Claude Code
