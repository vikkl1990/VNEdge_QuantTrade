# VN Edge — Production Crypto Trading System

Multi-tenant, ML-powered crypto trading bot for Delta India Exchange.

## Architecture

```
Signal Engine (shared)
    ↓ signal fires
    ├─ Paper Trade (shared, all users see same data)
    ├─ Global Real Manager (admin's API keys)
    └─ UserRealRegistry (broadcasts to each user)
         ├─ User A → User A's API keys → User A's exchange
         ├─ User B → User B's API keys → User B's exchange
         └─ User C → paper-only (no real exec)
```

## Key Features

### Trading
- 9 parallel scanners (structure_bounce, ema_momentum, vwap_bounce, rsi_divergence, liquidity_sweep, bos_choch, cvd_divergence, trend_continuation, order_block_entry)
- LightGBM ML scoring per scanner × pair family (liquid_majors / secondary / high_beta)
- Per-user risk limits (leverage, daily loss, max positions)
- Independent per-user trade monitoring (500ms async loops)
- Chandelier trail + MFE lock tiers
- IOC limit orders with 15bp slippage tolerance
- Circuit breaker (3 consecutive losses → auto-trip)

### BotBrain (Unified Memory)
- 4D performance matrix: `setup × regime × symbol × hour`
- Regime transition predictions (Markov chain)
- Hourly performance heatmap
- Daily session summaries
- Bayesian parameter optimizer
- **Phase 3 ACTIVE**: scanner gating (blocks <30% WR combos)
- **Phase 4 ACTIVE**: dynamic ML thresholds (lower for proven setups)
- Phase 5/7 flag-gated

### Multi-Tenant Auth
- Role-based access (admin / trader / viewer)
- bcrypt passwords (salt=12), Fernet-encrypted API keys
- TOTP 2FA (Google Authenticator)
- Email verification (24h token)
- DB-backed sessions (PostgreSQL)
- Login audit trail with IP tracking

### Production Ops
- Nightly PostgreSQL backups (14-day retention)
- Health monitoring → Telegram alerts
- ML model sync VM4→VM1 every 15 min
- Weekly ML retrain (Monday 4am UTC)
- Supervisor auto-restart on stale feeds
- Warm restart (candle cache persistence)

## Tech Stack

- **Python 3.13** + aiohttp
- **PostgreSQL 13** for multi-user data
- **Delta India Exchange** (testnet + live)
- **LightGBM** for ML models
- **Chart.js** for visualizations

## Quick Start

### Environment
```bash
cp .env.example .env
# Required:
# DATABASE_URL=postgresql://vnedge:pwd@localhost:5432/vnedge
# FERNET_KEY=<run auth.crypto.generate_fernet_key()>
# DASHBOARD_PASSWORD=<your admin password>
# DELTA_API_KEY=<from Delta>
# DELTA_API_SECRET=<from Delta>
```

### Migrations
```bash
psql -U vnedge -d vnedge -f db/migrations/001_initial.sql
psql -U vnedge -d vnedge -f db/migrations/002_user_strategies.sql
psql -U vnedge -d vnedge -f db/migrations/003_2fa_and_features.sql
```

### Start
```bash
python3 main.py
# Dashboard: http://localhost:8080
# Default admin: admin@vnedge.com / <DASHBOARD_PASSWORD>
```

## User Workflow

1. **Admin creates user** (Admin tab → "+ Add User")
2. **User logs in** → Profile tab
3. **User adds Delta API keys** (Admin can also add on user's behalf)
4. **User selects mode**: Paper / Demo (testnet) / Live (real money)
5. **Signal fires** → paper trade (shared) + user's real trade executes on their account

## See Also

- [API.md](API.md) — Full API reference (90+ endpoints)
- [PRODUCTION_OPS.md](PRODUCTION_OPS.md) — Deployment + monitoring runbook
