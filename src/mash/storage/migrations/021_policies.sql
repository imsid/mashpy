-- Policies -------------------------------------------------------------------
--
-- A policy is a named, durable resource that many sessions share. It owns the
-- sandbox and the browser profile, and sets the sandbox, browser and wait
-- rules. PUT /policies/{policy_id} replaces the whole document.

CREATE TABLE IF NOT EXISTS policies (
    policy_id   TEXT PRIMARY KEY,
    policy      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
