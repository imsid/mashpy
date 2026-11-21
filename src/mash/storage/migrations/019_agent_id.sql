-- app_id meant the agent id. It is renamed agent_id everywhere, so app_id can
-- name the calling application.
--
-- runtime_event_log carried both columns with the same value, so app_id is
-- dropped there; dropping it drops the indexes that led with it, which are
-- recreated on agent_id. The memory and feedback tables rename the column and
-- the indexes named after it.

ALTER TABLE runtime_event_log DROP COLUMN IF EXISTS app_id;

CREATE INDEX IF NOT EXISTS idx_runtime_event_agent_cursor
    ON runtime_event_log(agent_id, event_id);

CREATE INDEX IF NOT EXISTS idx_runtime_event_session_cursor
    ON runtime_event_log(agent_id, session_id, event_id);

CREATE INDEX IF NOT EXISTS idx_runtime_event_trace_cursor
    ON runtime_event_log(agent_id, trace_id, event_id);

CREATE INDEX IF NOT EXISTS idx_runtime_event_host_cursor
    ON runtime_event_log(agent_id, host_id, event_id)
    WHERE host_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_runtime_thought_traces
    ON runtime_event_log(agent_id, trace_id, created_at DESC, event_id DESC)
    WHERE trace_id IS NOT NULL
      AND event_type = 'chat.end'
      AND jsonb_typeof(payload -> 'thought_summary') = 'string'
      AND payload ->> 'thought_summary' ~ '[^[:space:]]';

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'runtime_feedback', 'memory_turns', 'memory_signals', 'memory_logs',
        'memory_session'
    ] LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = t AND column_name = 'app_id'
        ) THEN
            EXECUTE format('ALTER TABLE %I RENAME COLUMN app_id TO agent_id', t);
        END IF;
    END LOOP;
END $$;

ALTER INDEX IF EXISTS idx_runtime_feedback_app_created
    RENAME TO idx_runtime_feedback_agent_created;
ALTER INDEX IF EXISTS idx_memory_turns_app
    RENAME TO idx_memory_turns_agent;
ALTER INDEX IF EXISTS idx_memory_signals_app_session
    RENAME TO idx_memory_signals_agent_session;
ALTER INDEX IF EXISTS idx_memory_signals_app_name
    RENAME TO idx_memory_signals_agent_name;
ALTER INDEX IF EXISTS idx_memory_logs_app_id
    RENAME TO idx_memory_logs_agent_id;
