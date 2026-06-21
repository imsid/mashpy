-- Initial schema for the Mash runtime event store.
-- This is the authoritative baseline; all prior one-off ALTER TABLE
-- statements are baked into the column definitions below.

CREATE TABLE IF NOT EXISTS runtime_event_log (
    event_id       BIGSERIAL,
    request_id     TEXT,
    trace_id       TEXT,
    app_id         TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    session_id     TEXT,
    host_id        TEXT,
    workflow_id    TEXT,
    workflow_run_id TEXT,
    seq            INTEGER,
    event_type     TEXT NOT NULL,
    loop_index     INTEGER,
    step_key       TEXT,
    dedupe_key     TEXT,
    payload        JSONB NOT NULL,
    created_at     DOUBLE PRECISION NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_event_event_id
    ON runtime_event_log(event_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_event_dedupe
    ON runtime_event_log(request_id, dedupe_key)
    WHERE request_id IS NOT NULL AND dedupe_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_event_request_seq
    ON runtime_event_log(request_id, seq)
    WHERE request_id IS NOT NULL AND seq IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_runtime_event_request
    ON runtime_event_log(request_id, seq)
    WHERE request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_runtime_event_app_cursor
    ON runtime_event_log(app_id, event_id);

CREATE INDEX IF NOT EXISTS idx_runtime_event_session_cursor
    ON runtime_event_log(app_id, session_id, event_id);

CREATE INDEX IF NOT EXISTS idx_runtime_event_trace_cursor
    ON runtime_event_log(app_id, trace_id, event_id);

CREATE INDEX IF NOT EXISTS idx_runtime_event_type
    ON runtime_event_log(event_type);

CREATE INDEX IF NOT EXISTS idx_runtime_event_host_cursor
    ON runtime_event_log(app_id, host_id, event_id)
    WHERE host_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS runtime_feedback (
    feedback_id   BIGSERIAL PRIMARY KEY,
    feedback_type TEXT NOT NULL,
    message       TEXT NOT NULL,
    app_id        TEXT NOT NULL,
    host_id       TEXT,
    session_id    TEXT,
    request_id    TEXT,
    trace_id      TEXT,
    context       JSONB NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_feedback_app_created
    ON runtime_feedback(app_id, created_at);

CREATE INDEX IF NOT EXISTS idx_runtime_feedback_message_fts
    ON runtime_feedback
    USING GIN (to_tsvector('simple', COALESCE(message, '')));
