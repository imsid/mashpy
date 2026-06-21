-- Baseline memory store schema.
-- All one-off ALTER TABLE effects from the original _init_schema() are baked in.
-- Every statement is idempotent so re-running against an existing database is safe.

CREATE TABLE IF NOT EXISTS memory_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    agent_response TEXT NOT NULL,
    session_total_tokens BIGINT NOT NULL DEFAULT 0,
    workflow_id TEXT,
    workflow_run_id TEXT,
    task_id TEXT,
    replayable BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_signals (
    turn_id TEXT NOT NULL REFERENCES memory_turns(turn_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    signal_name TEXT NOT NULL,
    signal_value JSONB NOT NULL,
    PRIMARY KEY (turn_id, signal_name)
);

CREATE TABLE IF NOT EXISTS memory_logs (
    id BIGSERIAL PRIMARY KEY,
    app_id TEXT NOT NULL,
    session_id TEXT,
    trace_id TEXT,
    event_class TEXT NOT NULL,
    event_type TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_turns_session
    ON memory_turns(session_id);

CREATE INDEX IF NOT EXISTS idx_memory_turns_app
    ON memory_turns(app_id);

CREATE INDEX IF NOT EXISTS idx_memory_turns_workflow
    ON memory_turns(app_id, workflow_id)
    WHERE workflow_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_turns_workflow_run
    ON memory_turns(app_id, workflow_run_id)
    WHERE workflow_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_signals_app_session
    ON memory_signals(app_id, session_id);

CREATE INDEX IF NOT EXISTS idx_memory_signals_app_name
    ON memory_signals(app_id, signal_name);

CREATE INDEX IF NOT EXISTS idx_memory_logs_app_id
    ON memory_logs(app_id);

CREATE INDEX IF NOT EXISTS idx_memory_logs_session_id
    ON memory_logs(session_id);

CREATE INDEX IF NOT EXISTS idx_memory_logs_trace_id
    ON memory_logs(trace_id);

CREATE INDEX IF NOT EXISTS idx_memory_turns_user_message_tsv
    ON memory_turns
    USING GIN (to_tsvector('simple', COALESCE(user_message, '')));

CREATE INDEX IF NOT EXISTS idx_memory_turns_agent_response_tsv
    ON memory_turns
    USING GIN (to_tsvector('simple', COALESCE(agent_response, '')));
