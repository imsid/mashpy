-- Retention ------------------------------------------------------------------
--
-- With MASH_RETENTION_DAYS set, the host deletes what finished before the
-- window, in batches (mash.storage.retention). These indexes find each batch
-- without scanning the tables.

-- Finished requests, oldest first; their events go with them.
CREATE INDEX IF NOT EXISTS idx_runtime_request_finished
    ON runtime_request(updated_at)
    WHERE status <> 'running';

-- Events that belong to no request are deleted by age.
CREATE INDEX IF NOT EXISTS idx_runtime_event_unowned_created
    ON runtime_event_log(created_at)
    WHERE request_id IS NULL;

-- Finished workflow runs, oldest first.
CREATE INDEX IF NOT EXISTS idx_workflow_runs_finished_at
    ON workflow_runs(finished_at)
    WHERE status IN ('completed', 'failed', 'cancelled');
