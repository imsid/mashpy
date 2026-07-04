-- Operational metrics for a scored row (tokens, steps, tool calls, latency,
-- per-subagent breakdown), aggregated from the host session's runtime events.
-- JSONB because the metric set grows; promote hot keys to columns only if we
-- later need to sort experiments by them.
ALTER TABLE eval_experiment_run
    ADD COLUMN IF NOT EXISTS metrics JSONB;
