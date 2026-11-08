-- History replay ------------------------------------------------------------------
--
-- A request replays its session's latest summary checkpoint and the last few
-- turns after it. It used to read every turn of the session to keep three.
-- The query now reads only those turns, newest first, through this index; it
-- leads with session_id, so it also replaces the session-only index.

CREATE INDEX IF NOT EXISTS idx_memory_turns_session_order
    ON memory_turns(session_id, app_id, created_at, trace_id);

DROP INDEX IF EXISTS idx_memory_turns_session;
