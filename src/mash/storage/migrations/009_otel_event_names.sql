-- OpenTelemetry event names --------------------------------------------------
--
-- Each runtime event is now named after the span it bounds (invoke_agent,
-- chat, execute_tool, mash.*) and keeps its OpenTelemetry attributes under
-- payload.attributes with their GenAI convention names. A span ends once: the
-- three terminal request events fold into invoke_agent.end, and the tool and
-- subagent completions into execute_tool.end, each with a status of ok, error
-- or cancelled. This rewrites rows written under the old names and deletes the
-- events that no longer exist. Model and provider were never stored on the old
-- runtime rows, so rewritten chat spans carry neither.

-- The trace id now starts on the request's first event.
UPDATE runtime_event_log AS e
SET trace_id = (
    SELECT t.trace_id
    FROM runtime_event_log AS t
    WHERE t.request_id = e.request_id AND t.trace_id IS NOT NULL
    ORDER BY t.event_id
    LIMIT 1
)
WHERE e.event_type = 'runtime.request.accepted' AND e.trace_id IS NULL;

UPDATE runtime_event_log
SET event_type = 'invoke_agent.start',
    payload = payload || jsonb_build_object(
        'attributes', jsonb_strip_nulls(jsonb_build_object(
            'gen_ai.operation.name', 'invoke_agent',
            'gen_ai.agent.id', agent_id,
            'gen_ai.agent.name', agent_id,
            'gen_ai.conversation.id', session_id,
            'mash.request_id', request_id,
            'mash.host_id', host_id,
            'mash.request.mode', COALESCE(payload ->> 'mode', 'fg')
        ))
    )
WHERE event_type = 'runtime.request.accepted';

UPDATE runtime_event_log
SET event_type = 'mash.context_load.start', payload = '{}'::jsonb
WHERE event_type = 'runtime.trace.started';

UPDATE runtime_event_log
SET event_type = 'mash.context_load.end', payload = '{"status": "ok"}'::jsonb
WHERE event_type = 'runtime.context.loaded';

UPDATE runtime_event_log
SET event_type = 'chat.start',
    payload = '{"attributes": {"gen_ai.operation.name": "chat"}}'::jsonb
WHERE event_type = 'runtime.llm.think.started';

UPDATE runtime_event_log
SET event_type = 'chat.end',
    payload = (payload - 'token_usage' - 'trace_id') || jsonb_build_object(
        'status', 'ok',
        'attributes', jsonb_strip_nulls(jsonb_build_object(
            'gen_ai.operation.name', 'chat',
            'gen_ai.usage.input_tokens', (payload -> 'token_usage' ->> 'input')::numeric,
            'gen_ai.usage.output_tokens', (payload -> 'token_usage' ->> 'output')::numeric,
            'gen_ai.usage.cache_read.input_tokens',
                (payload -> 'token_usage' ->> 'cache_read')::numeric,
            'gen_ai.usage.cache_creation.input_tokens',
                (payload -> 'token_usage' ->> 'cache_write')::numeric,
            'mash.usage.reasoning_tokens', (payload -> 'token_usage' ->> 'reasoning')::numeric,
            'gen_ai.response.finish_reasons',
                CASE WHEN payload ->> 'stop_reason' IS NOT NULL
                     THEN jsonb_build_array(payload ->> 'stop_reason') END
        ))
    )
WHERE event_type = 'runtime.llm.think.completed';

UPDATE runtime_event_log
SET event_type = 'execute_tool.start',
    payload = jsonb_build_object(
        'arguments', '{}'::jsonb,
        'attributes', jsonb_strip_nulls(jsonb_build_object(
            'gen_ai.operation.name', 'execute_tool',
            'gen_ai.tool.name', payload ->> 'tool_name',
            'gen_ai.tool.call.id', payload ->> 'tool_call_id',
            'gen_ai.tool.type', 'function'
        ))
    )
WHERE event_type = 'runtime.tool.call.started';

UPDATE runtime_event_log
SET event_type = 'execute_tool.end',
    payload = jsonb_strip_nulls(jsonb_build_object(
        'status', CASE WHEN (payload -> 'result' ->> 'is_error') = 'true'
                       THEN 'error' ELSE 'ok' END,
        'duration_ms', payload -> 'duration_ms',
        'result', payload -> 'result',
        'structured_output', payload -> 'structured_output',
        'attributes', jsonb_strip_nulls(jsonb_build_object(
            'gen_ai.operation.name', 'execute_tool',
            'gen_ai.tool.name', payload ->> 'tool_name',
            'gen_ai.tool.call.id', payload ->> 'tool_call_id',
            'gen_ai.tool.type', 'function',
            'error.type', CASE WHEN (payload -> 'result' ->> 'is_error') = 'true'
                               THEN 'tool_error' END
        ))
    ))
WHERE event_type IN ('runtime.tool.call.completed', 'runtime.subagent.call.completed');

UPDATE runtime_event_log
SET event_type = 'mash.step.end',
    payload = payload || jsonb_build_object(
        'status', 'ok',
        'attributes', jsonb_build_object('mash.loop_index', loop_index)
    )
WHERE event_type = 'runtime.step.completed';

UPDATE runtime_event_log
SET event_type = 'mash.interaction.start',
    payload = payload || jsonb_build_object(
        'attributes', jsonb_build_object('mash.interaction.type', payload ->> 'type')
    )
WHERE event_type = 'runtime.interaction.create';

UPDATE runtime_event_log
SET event_type = 'mash.interaction.end',
    payload = payload || jsonb_build_object(
        'status', CASE WHEN (payload ->> 'cancelled') = 'true' THEN 'cancelled' ELSE 'ok' END
    )
WHERE event_type = 'runtime.interaction.ack';

UPDATE runtime_event_log
SET event_type = 'invoke_agent.end', payload = payload || '{"status": "ok"}'::jsonb
WHERE event_type = 'runtime.request.completed';

UPDATE runtime_event_log
SET event_type = 'invoke_agent.end',
    payload = payload || jsonb_build_object(
        'status', 'error',
        'attributes', jsonb_build_object(
            'error.type', COALESCE(payload ->> 'error_type', 'error')
        )
    )
WHERE event_type = 'runtime.request.failed';

UPDATE runtime_event_log
SET event_type = 'invoke_agent.end',
    payload = payload || '{"status": "cancelled", "attributes": {"mash.request.status": "cancelled"}}'::jsonb
WHERE event_type = 'runtime.request.cancelled';

UPDATE runtime_event_log
SET event_type = 'mash.request.resumed',
    payload = payload || jsonb_build_object(
        'attributes', jsonb_build_object('mash.request_id', request_id)
    )
WHERE event_type = 'runtime.request.resumed';

UPDATE runtime_event_log
SET event_type = 'mash.request.mode_changed',
    payload = payload || jsonb_build_object(
        'attributes', jsonb_build_object('mash.request.mode', payload ->> 'mode')
    )
WHERE event_type = 'runtime.request.mode_changed';

UPDATE runtime_event_log
SET event_type = 'chat.delta'
WHERE event_type = 'llm.response.delta';

DELETE FROM runtime_event_log
WHERE event_type IN ('runtime.turn.persisted', 'runtime.step.failed')
   OR event_type LIKE 'agent.%'
   OR event_type LIKE 'llm.request.%'
   OR event_type LIKE 'mcp.tool.%'
   OR event_type LIKE 'memory.search.%'
   OR event_type LIKE 'subagent.%';

-- The thoughts feed reads chat.end now.
DROP INDEX IF EXISTS idx_runtime_thought_traces;
CREATE INDEX IF NOT EXISTS idx_runtime_thought_traces
    ON runtime_event_log(app_id, trace_id, created_at DESC, event_id DESC)
    WHERE trace_id IS NOT NULL
      AND event_type = 'chat.end'
      AND jsonb_typeof(payload -> 'thought_summary') = 'string'
      AND payload ->> 'thought_summary' ~ '[^[:space:]]';
