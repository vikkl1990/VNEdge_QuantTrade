-- Migration 005: User self-service profile infrastructure (2026-04-19)
--
-- Adds support for user-initiated password + email changes, audit trail
-- of all profile mutations, and email-change pending-verification flow.

-- Audit log for user self-service changes (mirrors admin_audit_log but
-- written by users on their own profile).
CREATE TABLE IF NOT EXISTS user_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_email      VARCHAR(255),
    action          VARCHAR(80) NOT NULL,     -- e.g. 'password.change', 'email.change_request', 'api_key.add'
    method          VARCHAR(10) NOT NULL,
    path            VARCHAR(255) NOT NULL,
    before_json     JSONB,
    after_json      JSONB,
    ip_address      VARCHAR(45),
    user_agent      TEXT,
    status_code     INTEGER,
    result          VARCHAR(20) NOT NULL DEFAULT 'ok',   -- ok / error
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_audit_user ON user_audit_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_audit_action ON user_audit_log(action, created_at DESC);


-- Email-change requests: persists the new-email + verification token
-- while the user clicks the confirmation link sent to the NEW address.
CREATE TABLE IF NOT EXISTS email_change_requests (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    old_email       VARCHAR(255) NOT NULL,
    new_email       VARCHAR(255) NOT NULL,
    token           VARCHAR(64) UNIQUE NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    confirmed_at    TIMESTAMPTZ,
    ip_address      VARCHAR(45),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_email_change_user ON email_change_requests(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_change_token ON email_change_requests(token) WHERE confirmed_at IS NULL;
