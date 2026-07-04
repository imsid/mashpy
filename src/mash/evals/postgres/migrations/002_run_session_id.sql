-- Link each experiment run to the host session it executed under, so the admin
-- UI can deep-link a run to its Logs session.
ALTER TABLE eval_experiment_run
    ADD COLUMN IF NOT EXISTS session_id TEXT;
