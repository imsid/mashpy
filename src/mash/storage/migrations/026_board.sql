-- The board ---------------------------------------------------------------------
--
-- A session's turns are its board: entries numbered 1, 2, 3 in the session.
-- seq is taken on insert as the session's next number; one request runs at a
-- time per session, and the unique index enforces it and orders the board.
--
-- tokens is the entry's size. board_tokens is the tokens on the board since
-- its last summary, this entry included, so the compaction check reads the
-- last entry instead of adding up the board. Existing entries start at 0.
--
-- The companion reads the boards of a user's other conversations on a host:
-- sessions with the same host_id and policy_id, newest first. The new index
-- leads with host_id, so it replaces sessions_host.

ALTER TABLE memory_turns ADD COLUMN IF NOT EXISTS seq BIGINT;
ALTER TABLE memory_turns ADD COLUMN IF NOT EXISTS tokens BIGINT NOT NULL DEFAULT 0;
ALTER TABLE memory_turns ADD COLUMN IF NOT EXISTS board_tokens BIGINT NOT NULL DEFAULT 0;

UPDATE memory_turns AS t
SET seq = numbered.seq
FROM (
    SELECT trace_id,
           ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY created_at, trace_id) AS seq
    FROM memory_turns
) AS numbered
WHERE t.trace_id = numbered.trace_id;

ALTER TABLE memory_turns ALTER COLUMN seq SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_turns_session_seq
    ON memory_turns(session_id, seq);

CREATE INDEX IF NOT EXISTS sessions_host_policy
    ON sessions (host_id, policy_id, created_at);

DROP INDEX IF EXISTS sessions_host;
