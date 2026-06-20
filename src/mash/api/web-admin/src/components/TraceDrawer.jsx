import { useMemo, useState } from 'react';
import { Drawer } from './Drawer.jsx';
import { Chip, Mono } from './Chip.jsx';
import { Card } from './Page.jsx';
import { JsonBlock, Disclosure } from './Json.jsx';
import { TextInput, Select } from './Form.jsx';
import { Loading, ErrorState } from './State.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { reconstructMessages, previewText } from '../lib/conversation.js';
import { compactNumber, formatDuration, tokensInOut } from '../lib/format.js';

const ROLE_TONE = { user: 'emerald', assistant: 'indigo', tool: 'amber', system: 'slate' };

function StatTile({ label, value }) {
  return (
    <div className="rounded-md border border-slate-200 px-3 py-2">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="mt-0.5 text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function SpanNode({ node, depth = 0 }) {
  const duration = node.duration_ms ?? node.duration ?? 0;
  const children = node.children || [];
  return (
    <div>
      <div
        className="flex items-center justify-between gap-2 py-0.5 text-xs"
        style={{ paddingLeft: `${depth * 14}px` }}
      >
        <span className="truncate">
          <span className="text-slate-400">{node.kind ? `${node.kind} ` : ''}</span>
          <span className="text-slate-700">{node.name}</span>
        </span>
        <span className="shrink-0 tabular-nums text-slate-400">
          {formatDuration(duration)}
        </span>
      </div>
      {children.map((child, idx) => (
        <SpanNode key={idx} node={child} depth={depth + 1} />
      ))}
    </div>
  );
}

function MessageDetail({ message }) {
  if (!message) {
    return <div className="text-sm text-slate-400">Select a message.</div>;
  }
  if (message.role === 'tool') {
    return (
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Chip tone={message.isError ? 'rose' : 'amber'}>
            {message.isError ? 'tool error' : 'tool result'}
          </Chip>
          <Mono>{message.toolName}</Mono>
        </div>
        <JsonBlock value={message.content} />
      </div>
    );
  }
  if (message.role === 'assistant') {
    return (
      <div className="space-y-3">
        {message.text ? (
          <p className="whitespace-pre-wrap text-sm text-slate-700">{message.text}</p>
        ) : null}
        {message.toolCalls?.length ? (
          <div className="space-y-2">
            <div className="text-xs font-medium uppercase tracking-wide text-slate-400">
              Tool calls ({message.toolCalls.length})
            </div>
            {message.toolCalls.map((tc, idx) => (
              <Card key={tc.id || idx} className="p-2">
                <div className="mb-1.5 flex items-center gap-2">
                  <Mono>{tc.name}</Mono>
                </div>
                <JsonBlock value={tc.arguments} />
              </Card>
            ))}
          </div>
        ) : null}
        {!message.text && !message.toolCalls?.length ? (
          <p className="text-sm italic text-slate-400">Empty turn.</p>
        ) : null}
      </div>
    );
  }
  // user / system
  return <p className="whitespace-pre-wrap text-sm text-slate-700">{message.text}</p>;
}

function MessagesInspector({ messages }) {
  const [selected, setSelected] = useState(0);
  const [role, setRole] = useState('all');
  const [query, setQuery] = useState('');

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return messages.filter((m) => {
      if (role !== 'all' && m.role !== role) return false;
      if (!q) return true;
      return previewText(m).toLowerCase().includes(q);
    });
  }, [messages, role, query]);

  const active = messages.find((m) => m.index === selected) || filtered[0] || null;

  if (!messages.length) {
    return (
      <p className="text-sm text-slate-400">
        No conversation could be reconstructed for this trace.
      </p>
    );
  }

  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-xs font-medium text-slate-500">
          {messages.length} message{messages.length > 1 ? 's' : ''}
        </span>
        <span className="text-xs text-slate-300">·</span>
        <span className="text-xs text-slate-400">
          system prompt not captured per request
        </span>
      </div>
      <div className="mb-3 flex gap-2">
        <TextInput
          placeholder="Search messages…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="w-32 shrink-0">
          <Select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="all">All roles</option>
            <option value="user">User</option>
            <option value="assistant">Assistant</option>
            <option value="tool">Tool</option>
          </Select>
        </div>
      </div>
      <div className="grid grid-cols-5 gap-3">
        <ul className="col-span-2 max-h-80 space-y-1 overflow-y-auto">
          {filtered.map((m) => (
            <li key={m.index}>
              <button
                onClick={() => setSelected(m.index)}
                className={`w-full rounded-md border px-2 py-1.5 text-left ${
                  active?.index === m.index
                    ? 'border-slate-300 bg-slate-50'
                    : 'border-transparent hover:bg-slate-50'
                }`}
              >
                <div className="flex items-center gap-1.5">
                  <Chip tone={ROLE_TONE[m.role]}>{m.role}</Chip>
                  {m.tokenUsage ? (
                    <span className="text-[10px] tabular-nums text-slate-400">
                      {tokensInOut(
                        m.tokenUsage.input ?? m.tokenUsage.input_tokens,
                        m.tokenUsage.output ?? m.tokenUsage.output_tokens,
                      )}
                    </span>
                  ) : null}
                </div>
                <div className="mt-1 line-clamp-2 text-xs text-slate-500">
                  {previewText(m) || <span className="italic">empty</span>}
                </div>
              </button>
            </li>
          ))}
        </ul>
        <div className="col-span-3 max-h-80 overflow-y-auto rounded-md border border-slate-200 p-3">
          <MessageDetail message={active} />
        </div>
      </div>
    </div>
  );
}

export function TraceDrawer({ open, trace, agentId, onClose }) {
  const sessionId = trace?.session_id;
  const traceId = trace?.trace_id;

  const analysisState = useApi(
    () =>
      traceId
        ? api.traceAnalysis({ agent_id: agentId, session_id: sessionId, trace_id: traceId })
        : Promise.resolve(null),
    [agentId, sessionId, traceId],
  );
  const eventsState = useApi(
    () =>
      traceId
        ? api.listEvents({ agent_id: agentId, session_id: sessionId, trace_id: traceId })
        : Promise.resolve(null),
    [agentId, sessionId, traceId],
  );

  const messages = useMemo(
    () => reconstructMessages(eventsState.data?.events),
    [eventsState.data],
  );

  const analysis = analysisState.data;
  const tokens = analysis?.tokens || {};
  const counts = analysis?.counts || {};
  const durationMs = analysis?.total_duration_ms || 0;
  const throughput =
    durationMs > 0 ? (Number(tokens.output_tokens) || 0) / (durationMs / 1000) : 0;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={traceId ? `Trace ${traceId}` : 'Trace'}
      subtitle={`${agentId}${sessionId ? ` · session ${sessionId}` : ''}`}
    >
      {analysisState.loading && !analysis ? <Loading /> : null}
      {analysisState.error ? (
        <ErrorState error={analysisState.error} onRetry={analysisState.reload} />
      ) : null}

      {analysis ? (
        <div className="space-y-5">
          <div className="grid grid-cols-4 gap-2">
            <StatTile label="Duration" value={formatDuration(durationMs)} />
            <StatTile
              label="Tokens"
              value={tokensInOut(tokens.input_tokens, tokens.output_tokens)}
            />
            <StatTile label="Throughput" value={`${throughput.toFixed(1)}/s`} />
            <StatTile
              label="Tools"
              value={`${counts.tool_call_count || 0} (${counts.tool_error_count || 0}✗)`}
            />
          </div>

          <section>
            <h3 className="mb-2 text-sm font-semibold">Messages</h3>
            {eventsState.loading && !eventsState.data ? (
              <Loading />
            ) : (
              <MessagesInspector messages={messages} />
            )}
          </section>

          {analysis.span_tree ? (
            <Disclosure label="Span tree">
              <div className="space-y-0.5">
                <SpanNode node={analysis.span_tree} />
              </div>
            </Disclosure>
          ) : null}

          <Disclosure label="Raw events" hint={`${eventsState.data?.events?.length || 0}`}>
            <JsonBlock value={eventsState.data?.events || []} />
          </Disclosure>
        </div>
      ) : null}
    </Drawer>
  );
}
