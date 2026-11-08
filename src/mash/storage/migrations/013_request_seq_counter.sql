-- Per-request event sequence counter ----------------------------------------
--
-- A request's events are numbered 1, 2, 3, ... in runtime_event_log.seq. The
-- next number used to come from an advisory lock plus MAX(seq) over the
-- request's rows, in a transaction of four statements per event. A streamed
-- answer writes dozens of events, so that was the hottest write path.
--
-- The counter row is bumped in the same statement that inserts the event. Its
-- row lock serializes a request's appends until they commit, so seq order is
-- commit order and a reader that resumes after seq n misses nothing. An
-- append that turns out to be a duplicate (same dedupe key) still uses a
-- number, so seq can have gaps; readers only compare seq values.

CREATE TABLE IF NOT EXISTS runtime_request_seq (
    request_id TEXT PRIMARY KEY,
    last_seq   INTEGER NOT NULL
);

INSERT INTO runtime_request_seq (request_id, last_seq)
SELECT request_id, MAX(seq)
FROM runtime_event_log
WHERE request_id IS NOT NULL AND seq IS NOT NULL
GROUP BY request_id
ON CONFLICT (request_id) DO NOTHING;
