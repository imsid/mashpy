-- Sessions and events ----------------------------------------------------------
--
-- Every input to a host is an event (session_id, event_id, body). A session is
-- created by its first event and binds to that event's host and policy. Events
-- wait in the session's queue in seq order and run one at a time: at most one
-- is started per session. The table is also the ledger: when an event's
-- request ends, its outcome is copied onto the row, which outlives the request.

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    host_id     TEXT NOT NULL,
    -- No foreign key: sessions and their ledgers outlive a deleted policy.
    policy_id   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    event_id     TEXT NOT NULL,
    seq          BIGSERIAL NOT NULL,
    status       TEXT NOT NULL CHECK (status IN
                   ('queued', 'started', 'completed', 'failed', 'cancelled')),
    -- Set at dequeue.
    request_id   TEXT,
    -- app_id, message, context, metadata, structured_output
    body         JSONB NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    -- Copied from the request when it ends.
    response     JSONB,
    error        JSONB,
    closed_at    TIMESTAMPTZ,
    PRIMARY KEY (session_id, event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS events_one_active
    ON events (session_id) WHERE status = 'started';
CREATE INDEX IF NOT EXISTS events_queue
    ON events (session_id, seq) WHERE status = 'queued';
CREATE UNIQUE INDEX IF NOT EXISTS events_request
    ON events (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_seq ON events (seq);
CREATE INDEX IF NOT EXISTS sessions_host ON sessions (host_id);
