-- Migration 004: Admin audit log + broadcast message table (2026-04-19)
--
-- Captures every admin mutation action (POST/PUT/DELETE on /api/admin/*)
-- with before/after snapshots so we can trace who changed what, when.
--
-- Also adds a broadcast_messages table so admin can set a system-wide
-- banner viewed by all users without redeploying.

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    admin_user_id   UUID REFERENCES users(id) ON DELETE SET NULL,
    admin_email     VARCHAR(255),
    action          VARCHAR(80) NOT NULL,       -- e.g. 'user.update', 'api_key.add', 'user.delete'
    target_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
    target_email    VARCHAR(255),
    method          VARCHAR(10) NOT NULL,       -- POST / PUT / DELETE
    path            VARCHAR(255) NOT NULL,
    before_json     JSONB,                       -- snapshot of target object BEFORE change
    after_json      JSONB,                       -- snapshot of target object AFTER change
    ip_address      VARCHAR(45),
    user_agent      TEXT,
    status_code     INTEGER,                     -- HTTP response code from the handler
    result          VARCHAR(20) NOT NULL DEFAULT 'ok',  -- ok / error
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_admin ON admin_audit_log(admin_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_target ON admin_audit_log(target_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_action ON admin_audit_log(action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log(created_at DESC);


CREATE TABLE IF NOT EXISTS broadcast_messages (
    id              BIGSERIAL PRIMARY KEY,
    message         TEXT NOT NULL,
    severity        VARCHAR(20) NOT NULL DEFAULT 'info',  -- info / warning / critical
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    starts_at       TIMESTAMPTZ DEFAULT NOW(),
    ends_at         TIMESTAMPTZ,                  -- NULL = no auto-expiry
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_broadcast_active ON broadcast_messages(active, starts_at DESC);
