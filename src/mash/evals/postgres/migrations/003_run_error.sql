-- Persist the failure reason for a row that could not be scored (e.g. the host
-- request errored out before producing output), so a null weighted_score in the
-- experiment carries its cause instead of showing a blank with no explanation.
ALTER TABLE eval_experiment_run
    ADD COLUMN IF NOT EXISTS error TEXT;
