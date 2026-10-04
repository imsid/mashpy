-- Thoughts reuses runtime_event_log; no additional tables are needed.
-- Qualify thought-bearing traces before applying the feed's trace limit.
CREATE INDEX IF NOT EXISTS idx_runtime_thought_traces
    ON runtime_event_log(app_id, trace_id, created_at DESC, event_id DESC)
    WHERE trace_id IS NOT NULL
      AND event_type = 'runtime.llm.think.completed'
      AND jsonb_typeof(payload -> 'thought_summary') = 'string'
      AND payload ->> 'thought_summary' ~ '[^[:space:]]';
