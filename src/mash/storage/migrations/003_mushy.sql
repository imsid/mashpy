-- Completed artifacts only; source messages and summaries stay in runtime_event_log.
CREATE TABLE IF NOT EXISTS mushy_artifact (
    gif_id UUID PRIMARY KEY,
    agent_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    terminal_seq INTEGER NOT NULL,
    transitions JSONB NOT NULL CHECK (jsonb_typeof(transitions) = 'array'),
    gif_bytes BYTEA NOT NULL,
    UNIQUE (agent_id, request_id, terminal_seq)
);
