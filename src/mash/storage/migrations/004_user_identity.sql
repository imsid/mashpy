-- Request identity ---------------------------------------------------------
--
-- H2A 0.5.0 adds user_id to requests. The host's /h2a relay checks that a
-- subagent call's parent request belongs to the calling agent and copies the
-- parent's user_id, so runtime_request records both alongside the state.
-- Turns record user_id too, so conversation search and Cortex can scope to
-- one user.

ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS user_id TEXT;

-- Backfill from the accepted events, so the identity is rebuildable from the
-- log like the rest of the projection.
UPDATE runtime_request AS r
SET agent_id = e.agent_id,
    user_id = e.payload -> 'request_metadata' ->> 'user_id'
FROM runtime_event_log AS e
WHERE e.request_id = r.request_id
  AND e.event_type = 'runtime.request.accepted'
  AND r.agent_id IS NULL;

ALTER TABLE memory_turns ADD COLUMN IF NOT EXISTS user_id TEXT;

CREATE INDEX IF NOT EXISTS idx_memory_turns_user
    ON memory_turns(user_id, created_at)
    WHERE user_id IS NOT NULL AND workflow_id IS NULL;
