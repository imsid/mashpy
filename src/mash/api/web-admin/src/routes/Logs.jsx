import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { PageHeader } from '../components/Page.jsx';
import { Async, Empty, Loading } from '../components/State.jsx';
import { Table } from '../components/Table.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Drawer } from '../components/Drawer.jsx';
import { TextInput, Select, Button } from '../components/Form.jsx';
import { JsonBlock } from '../components/Json.jsx';
import { TraceDrawer } from '../components/TraceDrawer.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { compactNumber, formatTime, formatDuration } from '../lib/format.js';

const TABS = [
  { id: 'sessions', label: 'Sessions' },
  { id: 'api', label: 'API' },
  { id: 'cli', label: 'CLI' },
];

const COMMAND_EVENT = {
  'command.start': { label: 'start', tone: 'slate' },
  'command.complete': { label: 'complete', tone: 'emerald' },
  'command.error': { label: 'error', tone: 'rose' },
};

// Refresh control that spins its glyph while the request is in flight.
function RefreshButton({ state }) {
  return (
    <Button variant="ghost" onClick={state.reload} disabled={state.loading}>
      <span className={state.loading ? 'inline-block animate-spin' : 'inline-block'}>↻</span>
      Refresh
    </Button>
  );
}

function statusTone(code) {
  if (code >= 500) return 'rose';
  if (code >= 400) return 'amber';
  return 'emerald';
}

const TRACE_COLUMNS = [
  { key: 'time', header: 'Time', render: (r) => formatTime(r.latest_event_at) },
  {
    key: 'host_id',
    header: 'Host',
    render: (r) => (r.host_id ? <Mono>{r.host_id}</Mono> : <span className="text-slate-300">—</span>),
  },
  {
    key: 'duration',
    header: 'Duration',
    align: 'right',
    render: (r) => formatDuration((r.latest_event_at - r.started_at) * 1000),
  },
  { key: 'event_count', header: 'Events', align: 'right' },
  {
    key: 'trace_id',
    header: 'Trace',
    render: (r) => <Mono>{String(r.trace_id).slice(0, 12)}…</Mono>,
  },
];

// One session row: a header that toggles open, revealing its traces lazily.
function SessionRow({ agentId, session, expanded, onToggle, onSelectTrace, activeTraceId }) {
  const tracesState = useApi(
    () =>
      expanded
        ? api.listTraces({ agent_id: agentId, session_id: session.session_id, limit: 100 })
        : Promise.resolve(null),
    [expanded, agentId, session.session_id],
  );

  const traces = useMemo(() => {
    const rows = tracesState.data?.traces || [];
    return [...rows].sort((a, b) => (b.latest_event_at || 0) - (a.latest_event_at || 0));
  }, [tracesState.data]);

  return (
    <div className="border-b border-slate-100 last:border-0">
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm hover:bg-slate-50"
      >
        <span className={`text-slate-400 transition ${expanded ? 'rotate-90' : ''}`}>›</span>
        <Mono>{session.session_id}</Mono>
        <span className="ml-auto flex items-center gap-4 text-xs text-slate-400">
          <span>{session.turn_count} turns</span>
          <span className="tabular-nums">{compactNumber(session.session_total_tokens)} tok</span>
          <span>{formatTime(session.last_activity_at)}</span>
        </span>
      </button>
      {expanded ? (
        <div className="bg-slate-50/60 px-4 pb-3 pt-1">
          {tracesState.loading && !tracesState.data ? (
            <Loading />
          ) : traces.length ? (
            <Table
              columns={TRACE_COLUMNS}
              rows={traces}
              getRowKey={(r) => r.trace_id}
              activeKey={activeTraceId}
              onRowClick={onSelectTrace}
            />
          ) : (
            <p className="py-3 text-center text-xs text-slate-400">No traces in this session.</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

function SessionsTab({ agentId, initialSession }) {
  const state = useApi(
    () => (agentId ? api.listSessions(agentId) : Promise.resolve({ sessions: [] })),
    [agentId],
  );
  const [expanded, setExpanded] = useState(initialSession || null);
  const [selected, setSelected] = useState(null);
  const [sessionQuery, setSessionQuery] = useState(initialSession || '');
  const [traceQuery, setTraceQuery] = useState('');
  const [jumpError, setJumpError] = useState('');

  // Deep links (from Feedback / Overview) carry a session to focus on.
  useEffect(() => {
    if (initialSession) {
      setSessionQuery(initialSession);
      setExpanded(initialSession);
    }
  }, [initialSession]);

  if (!agentId) return <Empty>Pick an agent to view its sessions.</Empty>;

  const jumpToTrace = async () => {
    const q = traceQuery.trim();
    if (!q) return;
    setJumpError('');
    try {
      const { traces = [] } = await api.listTraces({ agent_id: agentId, limit: 100 });
      const match = traces.find(
        (t) => String(t.trace_id) === q || String(t.trace_id).startsWith(q),
      );
      if (match) {
        setSelected(match);
        setExpanded(match.session_id);
      } else {
        setJumpError('No matching trace in the recent 100.');
      }
    } catch {
      setJumpError('Trace lookup failed.');
    }
  };

  return (
    <>
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-slate-600">Session</span>
          <div className="w-56">
            <TextInput
              value={sessionQuery}
              placeholder="filter by session id"
              onChange={(e) => setSessionQuery(e.target.value)}
            />
          </div>
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-slate-600">Trace</span>
          <div className="flex w-72 gap-2">
            <TextInput
              value={traceQuery}
              placeholder="open by trace id"
              onChange={(e) => setTraceQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') jumpToTrace();
              }}
            />
            <Button variant="secondary" onClick={jumpToTrace}>
              Open
            </Button>
          </div>
        </label>
        <RefreshButton state={state} />
        {jumpError ? <span className="pb-1.5 text-xs text-rose-600">{jumpError}</span> : null}
      </div>

      <Async state={state} empty={(d) => !d.sessions?.length}>
        {(data) => {
          const q = sessionQuery.trim().toLowerCase();
          const sessions = q
            ? data.sessions.filter((s) => s.session_id.toLowerCase().includes(q))
            : data.sessions;
          if (!sessions.length) return <Empty>No sessions match that filter.</Empty>;
          return (
            <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
              {sessions.map((s) => (
                <SessionRow
                  key={s.session_id}
                  agentId={agentId}
                  session={s}
                  expanded={expanded === s.session_id}
                  onToggle={() =>
                    setExpanded((cur) => (cur === s.session_id ? null : s.session_id))
                  }
                  onSelectTrace={setSelected}
                  activeTraceId={selected?.trace_id}
                />
              ))}
            </div>
          );
        }}
      </Async>

      <TraceDrawer
        open={selected !== null}
        trace={selected}
        agentId={agentId}
        onClose={() => setSelected(null)}
      />
    </>
  );
}

function ApiAccessTab() {
  const state = useApi(() => api.listApiEvents({ limit: 200 }), []);
  const [selected, setSelected] = useState(null);

  const columns = [
    { key: 'time', header: 'Time', render: (r) => formatTime(r.created_at) },
    { key: 'method', header: 'Method', render: (r) => <Mono>{r.method}</Mono> },
    { key: 'path', header: 'Path', render: (r) => <span className="font-mono text-xs">{r.path}</span> },
    {
      key: 'status_code',
      header: 'Status',
      render: (r) => <Chip tone={statusTone(r.status_code)}>{r.status_code}</Chip>,
    },
    {
      key: 'duration_ms',
      header: 'Latency',
      align: 'right',
      render: (r) => formatDuration(r.duration_ms),
    },
  ];

  return (
    <>
      <div className="mb-3 flex justify-end">
        <RefreshButton state={state} />
      </div>
      <Async state={state} empty={(d) => !d.events?.length}>
        {(data) => (
          <Table
            columns={columns}
            rows={data.events}
            getRowKey={(r) => r.api_event_id}
            activeKey={selected?.api_event_id}
            onRowClick={setSelected}
          />
        )}
      </Async>
      <Drawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected ? `${selected.method} ${selected.path}` : ''}
        subtitle={selected ? `status ${selected.status_code} · ${formatDuration(selected.duration_ms)}` : ''}
      >
        {selected ? (
          <div className="space-y-4">
            <Section title="Query params" value={selected.query_params} />
            <Section title="Request headers" value={selected.request_headers} />
            <Section title="Request body" value={selected.request_body} />
            <Section title="Response headers" value={selected.response_headers} />
            <Section title="Response body" value={selected.response_body} />
          </div>
        ) : null}
      </Drawer>
    </>
  );
}

function CliTab({ agentId }) {
  const state = useApi(
    () => (agentId ? api.listCommandEvents({ agent_id: agentId, limit: 500 }) : Promise.resolve({ events: [] })),
    [agentId],
  );

  if (!agentId) return <Empty>Pick an agent to view its CLI activity.</Empty>;

  const columns = [
    { key: 'time', header: 'Time', render: (r) => formatTime(r.created_at) },
    {
      key: 'event',
      header: 'Event',
      render: (r) => {
        const kind = COMMAND_EVENT[r.event_type] || { label: r.event_type, tone: 'slate' };
        return <Chip tone={kind.tone}>{kind.label}</Chip>;
      },
    },
    {
      key: 'command',
      header: 'Command',
      render: (r) =>
        r.payload?.command_name ? (
          <Mono>{r.payload.command_name}</Mono>
        ) : (
          <span className="text-slate-300">—</span>
        ),
    },
    {
      key: 'detail',
      header: 'Detail',
      render: (r) => {
        const p = r.payload || {};
        if (p.error) return <span className="text-rose-600">{p.error}</span>;
        if (p.args) return <span className="font-mono text-xs text-slate-500">{p.args}</span>;
        return <span className="text-slate-300">—</span>;
      },
    },
    {
      key: 'duration',
      header: 'Duration',
      align: 'right',
      render: (r) =>
        r.payload?.duration_ms != null ? (
          formatDuration(r.payload.duration_ms)
        ) : (
          <span className="text-slate-300">—</span>
        ),
    },
  ];

  return (
    <>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs text-slate-400">
          Lifecycle events for <Mono>/commands</Mono> run in the REPL.
        </p>
        <RefreshButton state={state} />
      </div>
      <Async state={state} empty={(d) => !d.events?.length}>
        {(data) => {
          const rows = [...data.events].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
          return <Table columns={columns} rows={rows} getRowKey={(r) => r.event_id} />;
        }}
      </Async>
    </>
  );
}

function Section({ title, value }) {
  return (
    <div>
      <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">
        {title}
      </div>
      <JsonBlock value={value} />
    </div>
  );
}

export default function Logs() {
  const [params, setParams] = useSearchParams();
  const agentsState = useApi(() => api.listAgents(), []);
  const agents = agentsState.data?.agents || [];

  const tab = params.get('tab') || 'sessions';
  const agentId = params.get('agent') || agents[0]?.agent_id || '';

  const update = (next) => {
    const merged = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v) merged.set(k, v);
      else merged.delete(k);
    }
    setParams(merged, { replace: true });
  };

  return (
    <div>
      <PageHeader
        title="Logs"
        description="Sessions, traces, API access, and CLI activity for one agent."
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-slate-600">Agent</span>
          <div className="w-56">
            <Select value={agentId} onChange={(e) => update({ agent: e.target.value })}>
              {agents.map((a) => (
                <option key={a.agent_id} value={a.agent_id}>
                  {a.metadata?.display_name || a.agent_id}
                </option>
              ))}
            </Select>
          </div>
        </label>
      </div>

      <div className="mb-4 flex gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => update({ tab: t.id })}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition ${
              tab === t.id
                ? 'border-slate-900 text-slate-900'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'sessions' ? (
        <SessionsTab agentId={agentId} initialSession={params.get('session') || ''} />
      ) : null}
      {tab === 'api' ? <ApiAccessTab /> : null}
      {tab === 'cli' ? <CliTab agentId={agentId} /> : null}
    </div>
  );
}
