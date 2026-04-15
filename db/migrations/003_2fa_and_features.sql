-- VN Edge: 2FA + future feature support
CREATE TABLE IF NOT EXISTS user_2fa (
    user_id      UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    secret       VARCHAR(64) NOT NULL,
    enabled      BOOLEAN DEFAULT FALSE,
    backup_codes JSONB DEFAULT '[]'::jsonb,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    last_used    TIMESTAMPTZ
);

-- Email verification tokens
CREATE TABLE IF NOT EXISTS email_verification (
    user_id    UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    token      VARCHAR(64) UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    verified   BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Database backup metadata
CREATE TABLE IF NOT EXISTS backup_log (
    id         BIGSERIAL PRIMARY KEY,
    backup_at  TIMESTAMPTZ DEFAULT NOW(),
    file_size  BIGINT,
    duration_s FLOAT,
    success    BOOLEAN,
    note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_2fa_enabled ON user_2fa(enabled);
