-- Baseline schema for every Mash Postgres table: runtime event store, memory
-- store, and evals store. This file replaces the per-module migration
-- directories; the effects of their individual migrations are baked into the
-- definitions below. Every statement is idempotent, so applying this file
-- against a database created by the earlier per-module runners is safe.
-- (The file is named 001_baseline.sql rather than 001_initial_schema.sql
-- because upgraded databases already record the latter name in
-- _mash_migrations from the old runtime-only baseline.)

-- Runtime event store --------------------------------------------------------

CREATE TABLE IF NOT EXISTS runtime_event_log (
    event_id       BIGSERIAL,
    request_id     TEXT,
    trace_id       TEXT,
    app_id         TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    session_id     TEXT,
    host_id        TEXT,
    workflow_id    TEXT,
    workflow_run_id TEXT,
    seq            INTEGER,
    event_type     TEXT NOT NULL,
    loop_index     INTEGER,
    step_key       TEXT,
    dedupe_key     TEXT,
    payload        JSONB NOT NULL,
    created_at     DOUBLE PRECISION NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_event_event_id
    ON runtime_event_log(event_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_event_dedupe
    ON runtime_event_log(request_id, dedupe_key)
    WHERE request_id IS NOT NULL AND dedupe_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_event_request_seq
    ON runtime_event_log(request_id, seq)
    WHERE request_id IS NOT NULL AND seq IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_runtime_event_request
    ON runtime_event_log(request_id, seq)
    WHERE request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_runtime_event_app_cursor
    ON runtime_event_log(app_id, event_id);

CREATE INDEX IF NOT EXISTS idx_runtime_event_session_cursor
    ON runtime_event_log(app_id, session_id, event_id);

CREATE INDEX IF NOT EXISTS idx_runtime_event_trace_cursor
    ON runtime_event_log(app_id, trace_id, event_id);

CREATE INDEX IF NOT EXISTS idx_runtime_event_type
    ON runtime_event_log(event_type);

CREATE INDEX IF NOT EXISTS idx_runtime_event_host_cursor
    ON runtime_event_log(app_id, host_id, event_id)
    WHERE host_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS runtime_feedback (
    feedback_id   BIGSERIAL PRIMARY KEY,
    feedback_type TEXT NOT NULL,
    message       TEXT NOT NULL,
    app_id        TEXT NOT NULL,
    host_id       TEXT,
    session_id    TEXT,
    request_id    TEXT,
    trace_id      TEXT,
    context       JSONB NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_feedback_app_created
    ON runtime_feedback(app_id, created_at);

CREATE INDEX IF NOT EXISTS idx_runtime_feedback_message_fts
    ON runtime_feedback
    USING GIN (to_tsvector('simple', COALESCE(message, '')));

-- Memory store ----------------------------------------------------------------

-- Databases created before the trace_id rename (pre-0.10 _init_schema) still
-- carry the original turn_id column; the standalone rename migration is folded
-- in here.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'memory_turns' AND column_name = 'turn_id'
    ) THEN
        ALTER TABLE memory_turns RENAME COLUMN turn_id TO trace_id;
        ALTER TABLE memory_signals RENAME COLUMN turn_id TO trace_id;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS memory_turns (
    trace_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    agent_response TEXT NOT NULL,
    session_total_tokens BIGINT NOT NULL DEFAULT 0,
    workflow_id TEXT,
    workflow_run_id TEXT,
    task_id TEXT,
    replayable BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_signals (
    trace_id TEXT NOT NULL REFERENCES memory_turns(trace_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    signal_name TEXT NOT NULL,
    signal_value JSONB NOT NULL,
    PRIMARY KEY (trace_id, signal_name)
);

CREATE TABLE IF NOT EXISTS memory_logs (
    id BIGSERIAL PRIMARY KEY,
    app_id TEXT NOT NULL,
    session_id TEXT,
    trace_id TEXT,
    event_class TEXT NOT NULL,
    event_type TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_turns_session
    ON memory_turns(session_id);

CREATE INDEX IF NOT EXISTS idx_memory_turns_app
    ON memory_turns(app_id);

CREATE INDEX IF NOT EXISTS idx_memory_turns_workflow
    ON memory_turns(app_id, workflow_id)
    WHERE workflow_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_turns_workflow_run
    ON memory_turns(app_id, workflow_run_id)
    WHERE workflow_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_signals_app_session
    ON memory_signals(app_id, session_id);

CREATE INDEX IF NOT EXISTS idx_memory_signals_app_name
    ON memory_signals(app_id, signal_name);

CREATE INDEX IF NOT EXISTS idx_memory_logs_app_id
    ON memory_logs(app_id);

CREATE INDEX IF NOT EXISTS idx_memory_logs_session_id
    ON memory_logs(session_id);

CREATE INDEX IF NOT EXISTS idx_memory_logs_trace_id
    ON memory_logs(trace_id);

CREATE INDEX IF NOT EXISTS idx_memory_turns_user_message_tsv
    ON memory_turns
    USING GIN (to_tsvector('simple', COALESCE(user_message, '')));

CREATE INDEX IF NOT EXISTS idx_memory_turns_agent_response_tsv
    ON memory_turns
    USING GIN (to_tsvector('simple', COALESCE(agent_response, '')));

-- Evals store -----------------------------------------------------------------

-- An eval is a pure test definition (dataset + rubric). The host state
-- snapshot lives on the experiment, which records what was live when it ran;
-- deltas between experiments are computed at read time, never stored.

CREATE TABLE IF NOT EXISTS eval (
    eval_id       TEXT PRIMARY KEY,
    host_id       TEXT NOT NULL,
    user_guidance TEXT NOT NULL DEFAULT '',
    dataset_id    TEXT NOT NULL,
    rubric_id     TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS eval_host_id_idx ON eval (host_id);

CREATE TABLE IF NOT EXISTS eval_dataset (
    dataset_id TEXT PRIMARY KEY,
    eval_id    TEXT NOT NULL REFERENCES eval(eval_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS eval_dataset_row (
    row_id               TEXT PRIMARY KEY,
    dataset_id           TEXT NOT NULL REFERENCES eval_dataset(dataset_id) ON DELETE CASCADE,
    input                TEXT NOT NULL,
    scenario_description TEXT NOT NULL,
    sampling_category    TEXT NOT NULL,
    expected_behavior    TEXT NOT NULL,
    target_agents        JSONB NOT NULL DEFAULT '[]',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS eval_dataset_row_dataset_idx ON eval_dataset_row (dataset_id);

CREATE TABLE IF NOT EXISTS eval_rubric (
    rubric_id             TEXT PRIMARY KEY,
    eval_id               TEXT NOT NULL REFERENCES eval(eval_id) ON DELETE CASCADE,
    global_scoring_prompt TEXT NOT NULL,
    criteria              JSONB NOT NULL,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS eval_experiment (
    experiment_id       TEXT PRIMARY KEY,
    eval_id             TEXT NOT NULL REFERENCES eval(eval_id) ON DELETE CASCADE,
    workflow_run_id     TEXT,
    target_host_id      TEXT,
    agent_spec_snapshot JSONB NOT NULL,
    host_composition    JSONB NOT NULL DEFAULT '{}',
    rubric_snapshot     JSONB NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS eval_experiment_eval_id_idx ON eval_experiment (eval_id);
CREATE UNIQUE INDEX IF NOT EXISTS eval_experiment_workflow_run_idx
    ON eval_experiment(workflow_run_id)
    WHERE workflow_run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS eval_experiment_run (
    run_id         TEXT PRIMARY KEY,
    experiment_id  TEXT NOT NULL REFERENCES eval_experiment(experiment_id) ON DELETE CASCADE,
    row_id         TEXT NOT NULL,
    ordinal        INTEGER NOT NULL DEFAULT 0,
    input          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    actual_output  TEXT,
    weighted_score NUMERIC(5,4),
    scores         JSONB NOT NULL DEFAULT '{}',
    -- Host session the row executed under, so the admin UI can deep-link a
    -- run to its Logs session.
    session_id     TEXT,
    -- Failure reason for a row that could not be scored, so a null
    -- weighted_score carries its cause.
    error          TEXT,
    -- Operational metrics (tokens, steps, tool calls, latency, per-subagent
    -- breakdown) aggregated from the session's runtime events. JSONB because
    -- the metric set grows; promote hot keys to columns only if experiments
    -- later need to sort by them.
    metrics        JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (experiment_id, row_id)
);

CREATE INDEX IF NOT EXISTS eval_experiment_run_experiment_idx ON eval_experiment_run (experiment_id);
CREATE INDEX IF NOT EXISTS eval_experiment_run_status_idx
    ON eval_experiment_run(experiment_id, status, ordinal);

-- Workflows store ------------------------------------------------------------
-- Durable, observable forward-pipeline runs. Workflows own their run history and
-- step audit trail here, rather than reconstructing it from agent memory turns.
-- A code step produces no agent turns or runtime events, so workflow_step_events
-- is what makes it observable. Timestamps are epoch seconds (DOUBLE PRECISION),
-- matching the runtime event store.

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

-- The primary key is the deterministic identity of a lifecycle transition
-- (run, step, attempt, event_type), not seq. Step execution is at-least-once
-- under DBOS recovery, so a re-run re-appends the same transition; ON CONFLICT
-- DO NOTHING on this key makes those appends no-ops. seq orders events within a
-- step for display.
CREATE TABLE IF NOT EXISTS workflow_step_events (
    run_id      TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    step_id     TEXT NOT NULL,
    attempt     INTEGER NOT NULL DEFAULT 1,
    event_type  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    at          DOUBLE PRECISION NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, step_id, attempt, event_type)
);

CREATE INDEX IF NOT EXISTS idx_workflow_step_events_run
    ON workflow_step_events(run_id, at, seq);

CREATE INDEX IF NOT EXISTS idx_workflow_step_events_wf
    ON workflow_step_events(workflow_id, at);
