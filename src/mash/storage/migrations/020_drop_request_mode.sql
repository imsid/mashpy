-- Requests have no fg/bg mode. A session runs one request at a time, and
-- parallel work goes to another session or to RunSubAgents children.

DROP INDEX IF EXISTS idx_runtime_request_session_mode;
ALTER TABLE runtime_request DROP COLUMN IF EXISTS mode;

CREATE INDEX IF NOT EXISTS idx_runtime_request_session_status
    ON runtime_request(session_id, status);
