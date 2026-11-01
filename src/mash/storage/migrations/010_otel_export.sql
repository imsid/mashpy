-- OpenTelemetry export ---------------------------------------------------------
--
-- The host's OTLP exporter tails runtime_event_log by event_id and exports a
-- request when its invoke_agent.end is stored. It keeps the last position it
-- handed off here, one row per exporter ("requests", "workflows"), so a
-- restart picks up where it stopped.

CREATE TABLE IF NOT EXISTS otel_export_cursor (
    name       TEXT PRIMARY KEY,
    event_id   BIGINT NOT NULL DEFAULT 0,
    updated_at DOUBLE PRECISION NOT NULL
);

-- Workflow runs are exported when they finish, in the order they finished.
-- finished_seq records that order; a run resumed and finished again takes a
-- new one.
CREATE SEQUENCE IF NOT EXISTS workflow_run_finished_seq;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS finished_seq BIGINT;
CREATE INDEX IF NOT EXISTS idx_workflow_runs_finished_seq
    ON workflow_runs(finished_seq)
    WHERE finished_seq IS NOT NULL;
