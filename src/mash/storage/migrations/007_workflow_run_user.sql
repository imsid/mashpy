-- The user a workflow run acts for. Its agent steps submit their requests
-- for this user, so their bash runs in this user's sandbox.
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS user_id TEXT;
