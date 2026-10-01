-- setter-memory schema
-- =====================
-- The audit trail for the AI Setter rebuild. Every inbound message and every
-- agent decision gets logged here so accuracy is computed, not asserted.
-- This server fronts exactly these two tables; nothing else is created.
--
-- Apply once per database:
--   psql "$SETTER_MEMORY_DATABASE_URL" -f schema.sql
--
-- Trust domain: database only. This server makes no external network calls.

-- One row per inbound message seen by the intake layer, keyed by the
-- webhook-supplied message_id (not our surrogate id) so re-deliveries
-- and duplicate webhooks collapse to a single canonical row.
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,              -- our surrogate id (uuid hex)
    message_id  TEXT UNIQUE NOT NULL,          -- canonical id from the webhook (Close/GHL)
    channel     TEXT NOT NULL,                 -- e.g. 'sms', 'email' (Phase 2)
    source      TEXT NOT NULL,                 -- e.g. 'close', 'ghl'
    sender      TEXT NOT NULL,                 -- phone number or email address
    body        TEXT NOT NULL,                 -- full message text
    received_at TIMESTAMPTZ,                   -- when the provider says it arrived
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()  -- when we logged it
);

-- One row per agent decision about a message. Multiple decisions per
-- message are allowed (re-classification, shadow runs); readers take the
-- latest row by created_at.
CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,              -- our surrogate id (uuid hex)
    message_id  TEXT NOT NULL
                REFERENCES messages(message_id)
                ON DELETE CASCADE,             -- a decision never outlives its message
    intent      TEXT NOT NULL,                 -- e.g. 'booking_request', 'reschedule_request'
    confidence  DOUBLE PRECISION NOT NULL,     -- classifier confidence in [0, 1]
    reasoning   TEXT NOT NULL,                 -- free-text: why this decision
    skill       TEXT,                          -- skill that fired, NULL if none
    mode        TEXT,                          -- active, shadow, or none
    would_send  BOOLEAN,                       -- false in forced shadow
    send_tool   TEXT,                          -- populated only for executable plans
    escalation_note TEXT,                      -- safety downgrade or reviewer note
    action_taken TEXT NOT NULL,                -- e.g. 'sent_sms', 'shadow_logged',
                                               --      'dropped_opt_out', 'none'
    action_plan JSONB,                         -- planned steps shown to the user/reviewer
    execution   JSONB,                         -- execution/safety outcome
    send_preview JSONB,                        -- rendered body + recipient in forced shadow
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE decisions ADD COLUMN IF NOT EXISTS mode TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS would_send BOOLEAN;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS send_tool TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS escalation_note TEXT;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS action_plan JSONB;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS execution JSONB;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS send_preview JSONB;

-- Latest-decision lookup per message (what get_state reads).
CREATE INDEX IF NOT EXISTS decisions_message_created_idx
    ON decisions (message_id, created_at DESC);
