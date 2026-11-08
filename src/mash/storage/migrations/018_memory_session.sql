-- Per-agent session list -----------------------------------------------------------
--
-- GET /agent/{id}/sessions grouped every turn the agent ever saved before it
-- could sort. memory_session keeps one row per (app_id, session_id) with what
-- that listing shows, written in the transaction that saves a turn. Turns are
-- never updated or deleted, so the row stays exact:
--
-- turn_count: the session's turns for the agent;
-- last_activity_at: the latest turn's created_at;
-- session_total_tokens: the largest running total any turn recorded.

CREATE TABLE IF NOT EXISTS memory_session (
    app_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_count BIGINT NOT NULL,
    last_activity_at DOUBLE PRECISION NOT NULL,
    session_total_tokens BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, session_id)
);

INSERT INTO memory_session (
    app_id, session_id, turn_count, last_activity_at, session_total_tokens
)
SELECT app_id, session_id, COUNT(*), MAX(created_at), MAX(session_total_tokens)
FROM memory_turns
GROUP BY app_id, session_id
ON CONFLICT (app_id, session_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_memory_session_activity
    ON memory_session(app_id, last_activity_at DESC, session_id);
