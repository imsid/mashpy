-- Per-request rollup -------------------------------------------------------------
--
-- Trace and session listings grouped the whole event log before applying their
-- LIMIT. runtime_request now carries what those listings show for each
-- request, written in the append transaction of the event that changes it:
--
-- trace_id, host_id, workflow_id, workflow_run_id: the first non-null value
--   any lifecycle or chat.end event of the request carried;
-- last_event_at: the latest lifecycle or chat.end event;
-- input_tokens, output_tokens, cache_read_tokens, cache_write_tokens: summed
--   over the request's chat.end events.
--
-- A request has one trace, so a listing picks its page of requests from these
-- columns and reads the event log only for the requests on that page.

ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS host_id TEXT;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS workflow_id TEXT;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS workflow_run_id TEXT;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS last_event_at DOUBLE PRECISION;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS input_tokens BIGINT NOT NULL DEFAULT 0;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS output_tokens BIGINT NOT NULL DEFAULT 0;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT NOT NULL DEFAULT 0;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT NOT NULL DEFAULT 0;

UPDATE runtime_request AS r
SET trace_id = rollup.trace_id,
    host_id = rollup.host_id,
    workflow_id = rollup.workflow_id,
    workflow_run_id = rollup.workflow_run_id,
    last_event_at = rollup.last_event_at,
    input_tokens = rollup.input_tokens,
    output_tokens = rollup.output_tokens,
    cache_read_tokens = rollup.cache_read_tokens,
    cache_write_tokens = rollup.cache_write_tokens
FROM (
    SELECT
        request_id,
        (ARRAY_AGG(trace_id ORDER BY seq) FILTER (WHERE trace_id IS NOT NULL))[1] AS trace_id,
        (ARRAY_AGG(host_id ORDER BY seq) FILTER (WHERE host_id IS NOT NULL))[1] AS host_id,
        (ARRAY_AGG(workflow_id ORDER BY seq) FILTER (WHERE workflow_id IS NOT NULL))[1]
            AS workflow_id,
        (ARRAY_AGG(workflow_run_id ORDER BY seq) FILTER (WHERE workflow_run_id IS NOT NULL))[1]
            AS workflow_run_id,
        MAX(created_at) AS last_event_at,
        COALESCE(SUM(NULLIF(payload -> 'attributes' ->> 'gen_ai.usage.input_tokens', '')::numeric)
            FILTER (WHERE event_type = 'chat.end'), 0) AS input_tokens,
        COALESCE(SUM(NULLIF(payload -> 'attributes' ->> 'gen_ai.usage.output_tokens', '')::numeric)
            FILTER (WHERE event_type = 'chat.end'), 0) AS output_tokens,
        COALESCE(SUM(NULLIF(payload -> 'attributes' ->> 'gen_ai.usage.cache_read.input_tokens', '')::numeric)
            FILTER (WHERE event_type = 'chat.end'), 0) AS cache_read_tokens,
        COALESCE(SUM(NULLIF(payload -> 'attributes' ->> 'gen_ai.usage.cache_creation.input_tokens', '')::numeric)
            FILTER (WHERE event_type = 'chat.end'), 0) AS cache_write_tokens
    FROM runtime_event_log
    WHERE request_id IS NOT NULL
    GROUP BY request_id
) AS rollup
WHERE rollup.request_id = r.request_id;

UPDATE runtime_request SET last_event_at = updated_at WHERE last_event_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_runtime_request_agent_activity
    ON runtime_request(agent_id, last_event_at DESC);

CREATE INDEX IF NOT EXISTS idx_runtime_request_session_activity
    ON runtime_request(session_id, last_event_at DESC)
    WHERE session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_runtime_request_host_activity
    ON runtime_request(host_id, last_event_at DESC)
    WHERE host_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_runtime_request_activity
    ON runtime_request(last_event_at DESC);

-- A session's events across every agent, for eval metrics.
CREATE INDEX IF NOT EXISTS idx_runtime_event_session
    ON runtime_event_log(session_id, event_id)
    WHERE session_id IS NOT NULL;
