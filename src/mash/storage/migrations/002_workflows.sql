-- Workflows v2 store: durable, observable forward-pipeline runs.
--
-- Workflows own their run history and step audit trail here, rather than
-- reconstructing it from agent memory turns. A code step produces no agent
-- turns or runtime events, so workflow_step_events is what makes it observable.
-- Timestamps are epoch seconds (DOUBLE PRECISION), matching the rest of the
-- Mash schema.

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id         TEXT PRIMARY KEY,
    workflow_id    TEXT NOT NULL,
    status         TEXT NOT NULL,
    workflow_input JSONB NOT NULL DEFAULT '{}'::jsonb,
    result         JSONB,
    error          TEXT,
    dedup_key      TEXT,
    session_id     TEXT,
    created_at     DOUBLE PRECISION NOT NULL,
    started_at     DOUBLE PRECISION,
    finished_at    DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_wf_created
    ON workflow_runs(workflow_id, created_at);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_status
    ON workflow_runs(status);

CREATE TABLE IF NOT EXISTS workflow_steps (
    run_id           TEXT NOT NULL
        REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    workflow_id      TEXT NOT NULL,
    step_id          TEXT NOT NULL,
    ordinal          INTEGER NOT NULL,
    kind             TEXT NOT NULL,
    status           TEXT NOT NULL,
    input_snapshot   JSONB,
    output_snapshot  JSONB,
    error            TEXT,
    attempt          INTEGER NOT NULL DEFAULT 1,
    agent_request_id TEXT,
    started_at       DOUBLE PRECISION,
    finished_at      DOUBLE PRECISION,
    PRIMARY KEY (run_id, step_id)
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_run_ordinal
    ON workflow_steps(run_id, ordinal);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_wf
    ON workflow_steps(workflow_id);

CREATE TABLE IF NOT EXISTS workflow_step_events (
    run_id      TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    step_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    at          DOUBLE PRECISION NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, step_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_workflow_step_events_run
    ON workflow_step_events(run_id, at);

CREATE INDEX IF NOT EXISTS idx_workflow_step_events_wf
    ON workflow_step_events(workflow_id, at);
