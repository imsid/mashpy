// Reconstruct a role-segmented conversation from a trace's runtime events.
//
// The literal prompt sent to the model is not persisted, so we rebuild the
// turn-by-turn conversation from what the runtime does record:
//   - invoke_agent.start  -> the user message
//   - chat.end            -> an assistant turn (text + tool calls, with that
//                            model call's token usage from its attributes)
//   - execute_tool.end    -> a tool result
//
// The system prompt is intentionally absent: no endpoint exposes it, and we
// don't want to fake it. Callers should surface that gap to the user.

const EV = {
  INVOKE_AGENT_START: 'invoke_agent.start',
  CHAT_END: 'chat.end',
  EXECUTE_TOOL_END: 'execute_tool.end',
};

// The model call's token usage, from its OpenTelemetry usage attributes.
function tokenUsage(attributes) {
  if (!attributes || attributes['gen_ai.usage.input_tokens'] == null) return null;
  return {
    input: attributes['gen_ai.usage.input_tokens'],
    output: attributes['gen_ai.usage.output_tokens'],
    cache_read: attributes['gen_ai.usage.cache_read.input_tokens'],
    cache_write: attributes['gen_ai.usage.cache_creation.input_tokens'],
  };
}

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
      case EV.INVOKE_AGENT_START: {
        if (typeof payload.message === 'string' && payload.message) {
          messages.push({ role: 'user', text: payload.message });
        }
        break;
      }
      case EV.CHAT_END: {
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
            tokenUsage: tokenUsage(payload.attributes),
          });
        }
        break;
      }
      case EV.EXECUTE_TOOL_END: {
        const result = payload.result || {};
        const attributes = payload.attributes || {};
        messages.push({
          role: 'tool',
          toolName: attributes['gen_ai.tool.name'],
          toolCallId: attributes['gen_ai.tool.call.id'],
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
