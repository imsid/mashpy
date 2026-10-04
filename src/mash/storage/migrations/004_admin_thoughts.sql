-- Retire the prototype's artifact table without deleting any generated GIFs.
-- No application route reads or writes the archive. Keep 003 immutable for
-- databases which already applied it; new installs follow the same history.
ALTER TABLE mushy_artifact RENAME TO mushy_artifact_legacy;
COMMENT ON TABLE mushy_artifact_legacy IS
    'Archived Mushy GIF prototype data. Replaced by Admin Thoughts over runtime_event_log.';

-- Qualify thought-bearing traces before applying the feed's trace limit.
CREATE INDEX IF NOT EXISTS idx_runtime_thought_traces
    ON runtime_event_log(app_id, trace_id, created_at DESC, event_id DESC)
    WHERE trace_id IS NOT NULL
      AND event_type = 'runtime.llm.think.completed'
      AND jsonb_typeof(payload -> 'thought_summary') = 'string'
      AND payload ->> 'thought_summary' ~ '[^[:space:]]';
