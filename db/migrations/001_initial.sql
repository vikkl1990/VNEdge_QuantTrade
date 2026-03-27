-- VN Edge Multi-User SaaS Schema
-- Migration 001: Initial tables

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Users
CREATE TABLE IF NOT EXISTS users (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email                   VARCHAR(255) UNIQUE NOT NULL,
    password_hash           VARCHAR(255) NOT NULL,
    role                    VARCHAR(20) NOT NULL DEFAULT 'trader',
    tier                    VARCHAR(20) NOT NULL DEFAULT 'free',
    full_name               VARCHAR(255),
    phone                   VARCHAR(50),
    address_line1           VARCHAR(255),
    address_line2           VARCHAR(255),
    city                    VARCHAR(100),
    state                   VARCHAR(100),
    country                 VARCHAR(100),
    postal_code             VARCHAR(20),
    id_verification_status  VARCHAR(20) DEFAULT 'unverified',
    timezone                VARCHAR(50) DEFAULT 'Asia/Kolkata',
    telegram_chat_id        VARCHAR(100),
    notify_on_signal        BOOLEAN DEFAULT TRUE,
    notify_on_trade         BOOLEAN DEFAULT TRUE,
    notify_on_tp_hit        BOOLEAN DEFAULT TRUE,
    notify_on_sl_hit        BOOLEAN DEFAULT TRUE,
    notify_on_system        BOOLEAN DEFAULT TRUE,
    trading_pairs           JSONB DEFAULT '["BTC/USDT","ETH/USDT","SOL/USDT"]'::jsonb,
    preferred_leverage      INTEGER DEFAULT 5,
    max_leverage            INTEGER DEFAULT 20,
    risk_per_trade_pct      FLOAT DEFAULT 1.0,
    max_daily_loss_pct      FLOAT DEFAULT 3.0,
    max_open_positions      INTEGER DEFAULT 3,
    bot_mode                VARCHAR(20) DEFAULT 'paper',
    is_active               BOOLEAN DEFAULT TRUE,
    email_verified          BOOLEAN DEFAULT FALSE,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    last_login              TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

-- API Keys (encrypted at rest)
CREATE TABLE IF NOT EXISTS user_api_keys (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    exchange        VARCHAR(50) NOT NULL DEFAULT 'delta',
    label           VARCHAR(50) NOT NULL,
    api_key_enc     BYTEA NOT NULL,
    api_secret_enc  BYTEA NOT NULL,
    base_url        VARCHAR(255),
    is_active       BOOLEAN DEFAULT TRUE,
    last_used       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, exchange, label)
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON user_api_keys(user_id);

-- Sessions (DB-backed, replaces in-memory dict)
CREATE TABLE IF NOT EXISTS sessions (
    token           VARCHAR(64) PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ip_address      VARCHAR(45),
    user_agent      TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    last_activity   TIMESTAMPTZ DEFAULT NOW(),
    request_count   INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- Login History (audit trail)
CREATE TABLE IF NOT EXISTS login_history (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    email           VARCHAR(255) NOT NULL,
    ip_address      VARCHAR(45),
    success         BOOLEAN NOT NULL,
    failure_reason  VARCHAR(100),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_login_history_user ON login_history(user_id);

-- User Trades (per-user paper + real)
CREATE TABLE IF NOT EXISTS user_trades (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trade_type      VARCHAR(10) NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    side            VARCHAR(10) NOT NULL,
    entry_price     FLOAT,
    exit_price      FLOAT,
    quantity        FLOAT,
    pnl_usd         FLOAT,
    fees_usd        FLOAT DEFAULT 0,
    status          VARCHAR(20) NOT NULL,
    signal_data     JSONB DEFAULT '{}'::jsonb,
    opened_at       TIMESTAMPTZ DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_user_trades_user ON user_trades(user_id);
CREATE INDEX IF NOT EXISTS idx_user_trades_status ON user_trades(status);
