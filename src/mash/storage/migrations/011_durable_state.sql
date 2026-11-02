-- Durable request and workflow-run state ---------------------------------------
--
-- Requests and workflow runs used to run as DBOS workflows. They now run on
-- Mash's own engines, which keep their durable state here (see
-- docs/design/drop-dbos.md):
--
-- runtime_request.checkpoint is a request's loop state, overwritten after each
-- step. recoveries counts how many times a process continued the request after
-- a crash; past the limit the request fails.
--
-- workflow_runs.recoveries does the same for a run. A run resumes from its
-- completed workflow_steps rows, so it needs no checkpoint of its own.

ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS checkpoint JSONB;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS recoveries INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_runtime_request_running
    ON runtime_request(agent_id)
    WHERE status = 'running';

ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS recoveries INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_workflow_runs_unfinished
    ON workflow_runs(run_id)
    WHERE status IN ('queued', 'running');

-- Deduplication: one queued or running run per workflow and dedup key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_active_dedup
    ON workflow_runs(workflow_id, dedup_key)
    WHERE dedup_key IS NOT NULL AND status IN ('queued', 'running');

-- Requests and runs DBOS left unfinished have no checkpoint to continue from.
-- Putting them at the recovery limit makes the first start after the upgrade
-- fail them through the normal path, which records why.
UPDATE runtime_request SET recoveries = 10 WHERE status = 'running';
UPDATE workflow_runs SET recoveries = 10 WHERE status IN ('queued', 'running');

DROP SCHEMA IF EXISTS dbos CASCADE;
