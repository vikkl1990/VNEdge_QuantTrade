-- VN Edge Multi-User Schema
-- Migration 002: User strategies + trading config

CREATE TABLE IF NOT EXISTS user_strategies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                VARCHAR(100) NOT NULL,
    is_active           BOOLEAN DEFAULT TRUE,
    scanners_enabled    JSONB DEFAULT '["structure_bounce","ema_momentum","vwap_bounce","rsi_divergence","liquidity_sweep","bos_choch","cvd_divergence","trend_continuation","order_block_entry"]'::jsonb,
    min_confidence      FLOAT DEFAULT 55,
    ml_threshold        FLOAT DEFAULT 0.60,
    size_multiplier     FLOAT DEFAULT 1.0 CHECK (size_multiplier BETWEEN 0.1 AND 3.0),
    regime_overrides    JSONB DEFAULT '{}'::jsonb,
    max_daily_trades    INTEGER DEFAULT 15,
    allowed_regimes     JSONB DEFAULT '["trending_up","trending_down","breakout","sideways"]'::jsonb,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_user_strategies_user ON user_strategies(user_id);

-- Add max_daily_loss_usd to users if not exists (supplement pct-based field)
DO $$ BEGIN
    ALTER TABLE users ADD COLUMN IF NOT EXISTS max_daily_loss_usd FLOAT DEFAULT 25.0;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS max_position_notional FLOAT DEFAULT 500.0;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;
