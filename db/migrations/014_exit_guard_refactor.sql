-- Migration 014 — Phase 5.8: Exit Guard Refactor
-- Date: 2026-04-25
--
-- Registers the two new exit_reason values introduced by the unified
-- dead-signal guard in execution/exit_guards.py:
--
--   'dead_signal_unified'   — primary unified kill (replaces quick_kill,
--                             no_proof_of_life, early_kill, zombie_kill).
--                             Fires only after fee floor + grace + patience
--                             window has been satisfied; never decapitates a
--                             trade before round-trip fees could plausibly
--                             have been recovered.
--
--   'stalled_after_15min'   — secondary backstop for the middle-zone trades
--                             that survive the unified kill but never
--                             develop. Replaces the prior zombie_kill at
--                             a 15-minute (900s) threshold.
--
-- See docs/EXIT_GUARD_REFACTOR_5_8.md for the full design + rollout plan.
--
-- Schema impact: NONE — exit_reason is stored as a string inside the
-- user_trades.metadata JSONB blob and the storage/closed_signals.json
-- archive. There is no enum or check constraint to alter. This migration
-- exists as the canonical record that these reason codes are now valid,
-- and creates a JSONB index for analytics queries that group by exit_reason.
--
-- Rollback: this migration is idempotent and additive — no rollback needed.
-- If 5.8 is reverted, simply stop emitting the new reason strings; existing
-- rows remain queryable.

-- Optional analytics index: speeds up "group by exit_reason" rollups that
-- pick the value out of metadata->>'exit_reason' on the per-user trades
-- table. Idempotent.
CREATE INDEX IF NOT EXISTS idx_user_trades_exit_reason
    ON user_trades ((metadata->>'exit_reason'));

-- Document the registered exit_reason values inline. Stored in a
-- dedicated reference table so cohort/audit queries can JOIN against
-- it instead of hardcoding the string set in Python.
CREATE TABLE IF NOT EXISTS exit_reason_registry (
    reason          VARCHAR(64) PRIMARY KEY,
    introduced_in   VARCHAR(16) NOT NULL,
    deprecated_in   VARCHAR(16),
    category        VARCHAR(32) NOT NULL,   -- 'kill' | 'profit' | 'backstop' | 'sl'
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Pre-5.8 reasons (documented for completeness; deprecated_in='5.8' for the
-- four guards collapsed into dead_signal_unified).
INSERT INTO exit_reason_registry (reason, introduced_in, deprecated_in, category, description)
VALUES
    ('quick_kill',           '5.4',  '5.8', 'kill',     'DOA cut at 30s if peak<=0.02R AND current<-0.05R. Replaced by dead_signal_unified.'),
    ('no_proof_of_life',     '5.1',  '5.8', 'kill',     'A+/A-only proof-of-life cut at 90s/180s. Replaced by dead_signal_unified.'),
    ('early_kill',           '4.0',  '5.8', 'kill',     'B/C grade early kill at 60-90s. Replaced by dead_signal_unified.'),
    ('zombie_kill',          '5.6',  '5.8', 'kill',     '600s middle-zone cut. Replaced by stalled_after_15min.')
ON CONFLICT (reason) DO UPDATE SET
    deprecated_in = EXCLUDED.deprecated_in,
    description   = EXCLUDED.description;

-- Post-5.8 reasons (this is the new state).
INSERT INTO exit_reason_registry (reason, introduced_in, deprecated_in, category, description)
VALUES
    ('dead_signal_unified',  '5.8', NULL, 'kill',     'Unified fee-floor-aware dead-signal guard. Fires past patience window when peak<fee_floor AND current<-0.10R.'),
    ('stalled_after_15min',  '5.8', NULL, 'kill',     'Middle-zone backstop. Fires at 900s when peak<0.20R AND current<-0.05R.')
ON CONFLICT (reason) DO NOTHING;

-- Untouched (kept) reasons — registered for the JOIN to be lossless.
INSERT INTO exit_reason_registry (reason, introduced_in, deprecated_in, category, description)
VALUES
    ('sl_hit',               '1.0', NULL, 'sl',       '1R initial stop hit.'),
    ('trail_profit',         '1.0', NULL, 'profit',   'SL trailed past entry into profit before being hit.'),
    ('dead_market',          '4.1', NULL, 'kill',     '180s in quiet/MR regime, peak<0.08R, current<-0.10R.'),
    ('mfe_pullback',         '4.0', NULL, 'profit',   'After peak>=0.30R, current dropped below 40% of peak.'),
    ('exhaustion_wick',      '5.2', NULL, 'profit',   'Peak>=0.30R AND opposite-side wick>60% of body.'),
    ('exhaustion_shrink',    '5.2', NULL, 'profit',   'Peak>=0.30R AND 3 consecutive shrinking bodies.'),
    ('no_momentum',          '4.1', NULL, 'kill',     'RUNNER 600-900s with peak<0.15-0.20R.'),
    ('time_decay_30m',       '1.0', NULL, 'backstop', '30-minute SCALP backstop.'),
    ('time_decay_60m',       '1.0', NULL, 'backstop', '60-minute non-SCALP backstop.')
ON CONFLICT (reason) DO NOTHING;
