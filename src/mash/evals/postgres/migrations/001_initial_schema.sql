-- Initial schema for the Mash synthetic evals store.

CREATE TABLE IF NOT EXISTS eval (
    eval_id             TEXT PRIMARY KEY,
    host_id             TEXT NOT NULL,
    user_guidance       TEXT NOT NULL DEFAULT '',
    host_composition    JSONB NOT NULL,
    agent_spec_baseline JSONB NOT NULL,
    dataset_id          TEXT NOT NULL,
    rubric_id           TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    agent_spec_snapshot JSONB NOT NULL,
    agent_spec_delta    JSONB NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS eval_experiment_eval_id_idx ON eval_experiment (eval_id);

CREATE TABLE IF NOT EXISTS eval_experiment_run (
    run_id         TEXT PRIMARY KEY,
    experiment_id  TEXT NOT NULL REFERENCES eval_experiment(experiment_id) ON DELETE CASCADE,
    row_id         TEXT NOT NULL,
    input          TEXT NOT NULL,
    actual_output  TEXT,
    weighted_score NUMERIC(5,4),
    scores         JSONB NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS eval_experiment_run_experiment_idx ON eval_experiment_run (experiment_id);
