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

// Workflow task sessions are keyed `workflow:{workflow_id}:task:{task_id}:run:{run_id}`.
// Parse that so workflow runs read as a workflow + run, not a raw run-scoped blob.
function parseWorkflowSession(sessionId) {
  const m = /^workflow:(.+?):task:(.+?):run:(.+)$/.exec(sessionId || '');
  if (!m) return null;
  return { workflowId: m[1], taskId: m[2], runId: m[3] };
}

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

// Identify a session row: workflow runs get a workflow chip + short run id;
// everything else shows its raw session id. `agentLabel` is shown in pool-wide
// (all-agents) mode so each row carries its owning agent.
function SessionLabel({ sessionId, agentLabel }) {
  const wf = parseWorkflowSession(sessionId);
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {agentLabel ? <Chip>{agentLabel}</Chip> : null}
      {wf ? (
        <>
          <Chip tone="indigo">{wf.workflowId}</Chip>
          <span className="text-xs text-slate-400">task {wf.taskId}</span>
          <Mono>run {wf.runId.length > 10 ? `…${wf.runId.slice(-8)}` : wf.runId}</Mono>
        </>
      ) : (
        <Mono>{sessionId}</Mono>
      )}
    </span>
  );
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
function SessionRow({ session, expanded, onToggle, onSelectTrace, activeTraceId, showAgent }) {
  const agentId = session.__agentId;
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
        <SessionLabel sessionId={session.session_id} agentLabel={showAgent ? agentId : null} />
        <span className="ml-auto flex shrink-0 items-center gap-4 text-xs text-slate-400">
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
              onRowClick={(t) => onSelectTrace({ ...t, __agentId: agentId })}
            />
          ) : (
            <p className="py-3 text-center text-xs text-slate-400">No traces in this session.</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

// Sessions for one agent, or merged across the whole pool when `agentId` is
// empty. Each session is tagged with its owning agent so rows, traces, and the
// trace drawer all resolve correctly in pool-wide mode.
function SessionsTab({ agentId, agents, initialSession }) {
  const agentKey = agents.map((a) => a.agent_id).join(',');
  const allAgents = !agentId;

  const state = useApi(() => {
    const owners = agentId ? [agentId] : agents.map((a) => a.agent_id);
    if (!owners.length) return Promise.resolve({ sessions: [] });
    return Promise.all(
      owners.map((id) =>
        api
          .listSessions(id)
          .then((d) => (d.sessions || []).map((s) => ({ ...s, __agentId: id })))
          .catch(() => []),
      ),
    ).then((lists) => ({
      sessions: lists.flat().sort((a, b) => (b.last_activity_at || 0) - (a.last_activity_at || 0)),
    }));
  }, [agentId, agentKey]);

  const workflowsState = useApi(() => api.listWorkflows(), []);
  const workflows = workflowsState.data?.workflows || [];

  const [expanded, setExpanded] = useState(initialSession || null);
  const [selected, setSelected] = useState(null);
  const [sessionQuery, setSessionQuery] = useState(initialSession || '');
  const [workflowFilter, setWorkflowFilter] = useState('');
  const [traceQuery, setTraceQuery] = useState('');
  const [jumpError, setJumpError] = useState('');

  // Deep links (from Feedback / Overview) carry a session to focus on.
  useEffect(() => {
    if (initialSession) {
      setSessionQuery(initialSession);
      setExpanded(initialSession);
    }
  }, [initialSession]);

  const jumpToTrace = async () => {
    const q = traceQuery.trim();
    if (!q) return;
    setJumpError('');
    const owners = agentId ? [agentId] : agents.map((a) => a.agent_id);
    try {
      const results = await Promise.all(
        owners.map((id) =>
          api
            .listTraces({ agent_id: id, limit: 100 })
            .then((d) => (d.traces || []).map((t) => ({ ...t, __agentId: id })))
            .catch(() => []),
        ),
      );
      const match = results
        .flat()
        .find((t) => String(t.trace_id) === q || String(t.trace_id).startsWith(q));
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
        {workflows.length ? (
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">Workflow</span>
            <div className="w-52">
              <Select value={workflowFilter} onChange={(e) => setWorkflowFilter(e.target.value)}>
                <option value="">All sessions</option>
                {workflows.map((w) => (
                  <option key={w.workflow_id} value={w.workflow_id}>
                    {w.metadata?.display_name || w.workflow_id}
                  </option>
                ))}
              </Select>
            </div>
          </label>
        ) : null}
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
          let sessions = data.sessions;
          if (workflowFilter) {
            sessions = sessions.filter(
              (s) => parseWorkflowSession(s.session_id)?.workflowId === workflowFilter,
            );
          }
          if (q) {
            sessions = sessions.filter((s) => s.session_id.toLowerCase().includes(q));
          }
          if (!sessions.length) return <Empty>No sessions match that filter.</Empty>;
          return (
            <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
              {sessions.map((s) => (
                <SessionRow
                  key={`${s.__agentId}:${s.session_id}`}
                  session={s}
                  showAgent={allAgents}
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
        agentId={selected?.__agentId || agentId}
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

// CLI command events for one agent, or merged across the pool when `agentId`
// is empty (matching the Sessions tab's all-agents default).
function CliTab({ agentId, agents }) {
  const agentKey = agents.map((a) => a.agent_id).join(',');
  const allAgents = !agentId;

  const state = useApi(() => {
    const owners = agentId ? [agentId] : agents.map((a) => a.agent_id);
    if (!owners.length) return Promise.resolve({ events: [] });
    return Promise.all(
      owners.map((id) =>
        api
          .listCommandEvents({ agent_id: id, limit: 500 })
          .then((d) => (d.events || []).map((e) => ({ ...e, __agentId: id })))
          .catch(() => []),
      ),
    ).then((lists) => ({ events: lists.flat() }));
  }, [agentId, agentKey]);

  const columns = [
    { key: 'time', header: 'Time', render: (r) => formatTime(r.created_at) },
    ...(allAgents
      ? [{ key: 'agent', header: 'Agent', render: (r) => <Chip>{r.__agentId || r.app_id}</Chip> }]
      : []),
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
          return (
            <Table columns={columns} rows={rows} getRowKey={(r) => `${r.__agentId}:${r.event_id}`} />
          );
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
  // Empty agent = pool-wide (all agents), matching Overview's aggregate counts.
  const agentId = params.get('agent') || '';

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
        description="Sessions, traces, API access, and CLI activity across the pool."
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-slate-600">Agent</span>
          <div className="w-56">
            <Select value={agentId} onChange={(e) => update({ agent: e.target.value })}>
              <option value="">All agents</option>
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
        <SessionsTab agentId={agentId} agents={agents} initialSession={params.get('session') || ''} />
      ) : null}
      {tab === 'api' ? <ApiAccessTab /> : null}
      {tab === 'cli' ? <CliTab agentId={agentId} agents={agents} /> : null}
    </div>
  );
}
