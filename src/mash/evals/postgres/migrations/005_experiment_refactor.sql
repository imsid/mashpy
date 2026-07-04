-- An eval is now a pure test definition (dataset + rubric); the host state
-- snapshot moves to the experiment, which records what was live when it ran.
-- Deltas are computed between experiments at read time, never stored.

ALTER TABLE eval DROP COLUMN host_composition;
ALTER TABLE eval DROP COLUMN agent_spec_baseline;

ALTER TABLE eval_experiment DROP COLUMN agent_spec_delta;
ALTER TABLE eval_experiment ADD COLUMN host_composition JSONB NOT NULL DEFAULT '{}';
