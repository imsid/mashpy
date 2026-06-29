-- Add host_id to runtime_event_log and runtime_feedback.
-- Uses ADD COLUMN IF NOT EXISTS so it is safe to run against databases
-- created after 001_initial_schema.sql already included host_id.

ALTER TABLE runtime_event_log
    ADD COLUMN IF NOT EXISTS host_id TEXT;

ALTER TABLE runtime_feedback
    ADD COLUMN IF NOT EXISTS host_id TEXT;

CREATE INDEX IF NOT EXISTS idx_runtime_event_host_cursor
    ON runtime_event_log(app_id, host_id, event_id)
    WHERE host_id IS NOT NULL;
