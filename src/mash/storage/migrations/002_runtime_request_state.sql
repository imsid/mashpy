-- Request lifecycle state --------------------------------------------------
--
-- Terminality used to be re-derived on every read by scanning the event log
-- for a hand-maintained list of event type names and taking the last one by
-- seq. That coupled a request's state to both the names and the row order,
-- and the same scan was reimplemented, with a different name list, in
-- resume_request.
--
-- This table records the state instead. It is a projection of the log, not a
-- second source of truth: rows are written in the same transaction as the
-- lifecycle event that causes them, and the table is rebuildable from the log
-- by replaying the backfill below.

CREATE TABLE IF NOT EXISTS runtime_request (
    request_id   TEXT PRIMARY KEY,
    -- running | completed | failed | cancelled
    status       TEXT NOT NULL,
    -- seq of the event that made the request terminal; NULL while running.
    -- Readers bound terminality by the prefix they have delivered, so they
    -- need the position, not just the fact.
    terminal_seq INTEGER,
    updated_at   DOUBLE PRECISION NOT NULL
);

-- Backfill from the existing log, replaying the name-and-order rule one last
-- time so it can be deleted from the read path.
INSERT INTO runtime_request (request_id, status, terminal_seq, updated_at)
SELECT
    last_lifecycle.request_id,
    CASE last_lifecycle.event_type
        WHEN 'runtime.request.completed' THEN 'completed'
        WHEN 'runtime.request.failed'    THEN 'failed'
        WHEN 'runtime.request.cancelled' THEN 'cancelled'
        ELSE 'running'
    END,
    CASE
        WHEN last_lifecycle.event_type = 'runtime.request.resumed'
        THEN NULL
        ELSE last_lifecycle.seq
    END,
    last_lifecycle.created_at
FROM (
    SELECT DISTINCT ON (request_id)
        request_id, event_type, seq, created_at
    FROM runtime_event_log
    WHERE request_id IS NOT NULL
      AND event_type IN (
          'runtime.request.completed',
          'runtime.request.failed',
          'runtime.request.cancelled',
          'runtime.request.resumed',
          'runtime.request.accepted'
      )
    ORDER BY request_id, seq DESC
) AS last_lifecycle
ON CONFLICT (request_id) DO NOTHING;
