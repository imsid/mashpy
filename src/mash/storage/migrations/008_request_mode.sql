-- Request mode ---------------------------------------------------------------
--
-- A request runs in the foreground (fg), where the conversation waits on it,
-- or in the background (bg), where the user keeps talking. The accepted event
-- writes the session, the mode, who started the request and when, and the
-- request whose RunSubAgents call started it. runtime.request.mode_changed
-- updates the mode while the request runs.
--
-- started_by is 'user', or the agent_id of the agent that started the request.
-- parent_request_id is NULL when the user started it.

ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'fg';
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS started_by TEXT NOT NULL DEFAULT 'user';
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS started_at DOUBLE PRECISION;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS parent_request_id TEXT;

CREATE INDEX IF NOT EXISTS idx_runtime_request_session_mode
    ON runtime_request(session_id, mode, status);

CREATE INDEX IF NOT EXISTS idx_runtime_request_parent
    ON runtime_request(parent_request_id)
    WHERE parent_request_id IS NOT NULL;
