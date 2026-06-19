// Reconstruct a role-segmented conversation from a trace's runtime events.
//
// The literal prompt sent to the model is not persisted, so we rebuild the
// turn-by-turn conversation from what the runtime does record:
//   - runtime.trace.started        -> the user message
//   - runtime.llm.think.completed  -> an assistant turn (text + tool calls,
//                                     with that LLM call's token usage)
//   - runtime.(subagent.)tool.call.completed -> a tool result
//
// The system prompt is intentionally absent: no endpoint exposes it, and we
// don't want to fake it. Callers should surface that gap to the user.

const EV = {
  TRACE_STARTED: 'runtime.trace.started',
  THINK_COMPLETED: 'runtime.llm.think.completed',
  TOOL_COMPLETED: 'runtime.tool.call.completed',
  SUBAGENT_COMPLETED: 'runtime.subagent.call.completed',
};

function assistantText(payload) {
  if (typeof payload.assistant_text === 'string' && payload.assistant_text) {
    return payload.assistant_text;
  }
  // Fall back to concatenating text blocks if no flat text was recorded.
  const blocks = Array.isArray(payload.assistant_blocks) ? payload.assistant_blocks : [];
  return blocks
    .map((b) => (typeof b === 'string' ? b : b?.text || ''))
    .filter(Boolean)
    .join('\n');
}

export function reconstructMessages(events) {
  const sorted = [...(events || [])].sort(
    (a, b) => Number(a.event_id) - Number(b.event_id),
  );
  const messages = [];

  for (const event of sorted) {
    const payload = event.payload || {};
    switch (event.event_type) {
      case EV.TRACE_STARTED: {
        if (typeof payload.message === 'string' && payload.message) {
          messages.push({ role: 'user', text: payload.message });
        }
        break;
      }
      case EV.THINK_COMPLETED: {
        const text = assistantText(payload);
        const toolCalls = (payload.tool_calls || [])
          .filter((tc) => tc && typeof tc === 'object')
          .map((tc) => ({
            id: tc.id,
            name: tc.name,
            arguments: tc.arguments ?? {},
          }));
        if (text || toolCalls.length) {
          messages.push({
            role: 'assistant',
            text,
            toolCalls,
            tokenUsage: payload.token_usage || null,
          });
        }
        break;
      }
      case EV.TOOL_COMPLETED:
      case EV.SUBAGENT_COMPLETED: {
        const result = payload.result || {};
        messages.push({
          role: 'tool',
          toolName: payload.tool_name,
          toolCallId: payload.tool_call_id,
          content: result.content,
          isError: Boolean(result.is_error),
        });
        break;
      }
      default:
        break;
    }
  }

  return messages.map((m, idx) => ({ ...m, index: idx }));
}

export function previewText(message) {
  if (message.role === 'tool') {
    const flag = message.isError ? '⚠ ' : '';
    return flag + asText(message.content);
  }
  if (message.role === 'assistant' && !message.text && message.toolCalls?.length) {
    const names = message.toolCalls.map((t) => t.name).join(', ');
    return `${message.toolCalls.length} tool call${message.toolCalls.length > 1 ? 's' : ''}: ${names}`;
  }
  return message.text || '';
}

// Best-effort stringification of arbitrary tool content for previews.
export function asText(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
