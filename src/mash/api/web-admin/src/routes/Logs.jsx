import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { PageHeader } from '../components/Page.jsx';
import { Async, Empty, Loading } from '../components/State.jsx';
import { Table } from '../components/Table.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Drawer } from '../components/Drawer.jsx';
import { Tabs } from '../components/Tabs.jsx';
import { FilterBar, FilterField, FilterActions } from '../components/Filters.jsx';
import { TextInput, Select, Button } from '../components/Form.jsx';
import { JsonBlock } from '../components/Json.jsx';
import { CopyId } from '../components/CopyId.jsx';
import { TraceDrawer } from '../components/TraceDrawer.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { useBreakpoint } from '../lib/useMediaQuery.js';
import { compactNumber, formatTime, formatDuration } from '../lib/format.js';
import { TRACE_STATUS } from '../lib/trace.js';

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

// TRACE_STATUS lives in lib/trace.js — the drawer renders the same chips.

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

// A workflow trace shows its workflow + run id; other traces show a dash.
function WorkflowCell({ trace }) {
  if (!trace.workflow_id) return <span className="text-slate-300">—</span>;
  return (
    <span className="flex flex-col gap-0.5">
      <Chip tone="indigo">{trace.workflow_id}</Chip>
      {trace.workflow_run_id ? (
        <CopyId value={trace.workflow_run_id} className="text-[11px]" />
      ) : null}
    </span>
  );
}

const TRACE_COLUMNS = [
  { key: 'started', header: 'Started', render: (r) => formatTime(r.started_at) },
  { key: 'trace_id', header: 'Trace ID', primary: true, render: (r) => <CopyId value={r.trace_id} /> },
  {
    key: 'status',
    header: 'Status',
    primary: true,
    render: (r) => {
      const status = TRACE_STATUS[r.status];
      if (!status) return <span className="text-slate-300">—</span>;
      return <Chip tone={status.tone}>{status.label}</Chip>;
    },
  },
  {
    key: 'agent',
    header: 'Ran on',
    render: (r) => (r.agent_id ? <Chip>{r.agent_id}</Chip> : <span className="text-slate-300">—</span>),
  },
  { key: 'workflow', header: 'Workflow', render: (r) => <WorkflowCell trace={r} /> },
  {
    key: 'duration',
    header: 'Duration',
    align: 'right',
    render: (r) => formatDuration((r.latest_event_at - r.started_at) * 1000),
  },
  {
    key: 'total_tokens',
    header: 'Tokens',
    align: 'right',
    render: (r) => compactNumber(r.total_tokens || 0),
  },
  {
    key: 'cache_read_tokens',
    header: 'Cache read',
    align: 'right',
    hideOnMobile: true,
    render: (r) =>
      r.cache_read_tokens ? compactNumber(r.cache_read_tokens) : <span className="text-slate-300">—</span>,
  },
  {
    key: 'cache_write_tokens',
    header: 'Cache write',
    align: 'right',
    hideOnMobile: true,
    render: (r) =>
      r.cache_write_tokens ? compactNumber(r.cache_write_tokens) : <span className="text-slate-300">—</span>,
  },
  { key: 'event_count', header: 'Events', align: 'right' },
];

// One session, in either of two shapes. Above `sm` it is a table row that
// toggles open into a nested trace table; below `sm` it is a card that does
// the same thing, since eight columns do not fit a phone. Both shapes come
// from this one component so the trace fetch happens once: only the shape the
// viewport calls for is ever mounted.
function SessionEntry({
  session,
  columnCount,
  expanded,
  onToggle,
  onSelectTrace,
  activeTraceId,
  statusFilter,
  refreshToken,
  wide,
}) {
  const tracesState = useApi(
    () =>
      expanded
        ? api.listTraces({
            session_id: session.session_id,
            status: statusFilter || undefined,
            limit: 100,
          })
        : Promise.resolve(null),
    [expanded, session.session_id, statusFilter, refreshToken],
  );

  const traces = useMemo(() => {
    const rows = tracesState.data?.traces || [];
    return [...rows].sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
  }, [tracesState.data]);

  const traceList =
    tracesState.loading && !tracesState.data ? (
      <Loading />
    ) : traces.length ? (
      <Table
        columns={TRACE_COLUMNS}
        rows={traces}
        getRowKey={(r) => r.trace_id}
        activeKey={activeTraceId}
        onRowClick={(t) => onSelectTrace({ ...t, __agentId: t.agent_id })}
      />
    ) : (
      <p className="py-3 text-center text-xs text-slate-400">
        {statusFilter
          ? `No ${TRACE_STATUS[statusFilter]?.label || statusFilter} traces in this session.`
          : 'No traces in this session.'}
      </p>
    );

  const cacheRead = session.cache_read_tokens;
  const cacheWrite = session.cache_write_tokens;

  if (!wide) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white">
        <button
          type="button"
          onClick={onToggle}
          className="flex w-full items-start gap-2 px-3 py-2.5 text-left"
        >
          <span className={`mt-0.5 shrink-0 text-slate-400 transition ${expanded ? 'rotate-90' : ''}`}>
            ›
          </span>
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-medium text-slate-900">
              <CopyId value={session.session_id} />
            </span>
            <span className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-400">
              {session.owner_agent_id ? <Chip>{session.owner_agent_id}</Chip> : null}
              <span>{formatTime(session.started_at)}</span>
              <span>{compactNumber(session.total_tokens)} tokens</span>
              <span>
                {session.trace_count} trace{session.trace_count === 1 ? '' : 's'}
              </span>
            </span>
          </span>
        </button>
        {expanded ? (
          <div className="border-t border-slate-100 bg-slate-50/60 px-2 py-2">{traceList}</div>
        ) : null}
      </div>
    );
  }

  return (
    <>
      <tr
        onClick={onToggle}
        className="cursor-pointer border-b border-slate-100 hover:bg-slate-50"
      >
        <td className="py-2.5 pl-4 pr-2 align-top text-slate-400">
          <span className={`inline-block transition ${expanded ? 'rotate-90' : ''}`}>›</span>
        </td>
        <td className="px-2 py-2.5 align-top">
          {session.owner_agent_id ? (
            <Chip>{session.owner_agent_id}</Chip>
          ) : (
            <span className="text-slate-300">—</span>
          )}
        </td>
        <td className="px-2 py-2.5 align-top">
          <CopyId value={session.session_id} />
        </td>
        <td className="px-2 py-2.5 align-top text-slate-500">{formatTime(session.started_at)}</td>
        <td className="px-2 py-2.5 text-right align-top tabular-nums text-slate-500">
          {compactNumber(session.total_tokens)}
        </td>
        <td
          className="px-2 py-2.5 text-right align-top tabular-nums text-slate-500"
          title={cacheRead ? `${cacheRead.toLocaleString()} tokens served from cache` : undefined}
        >
          {cacheRead ? compactNumber(cacheRead) : <span className="text-slate-300">—</span>}
        </td>
        <td
          className="px-2 py-2.5 text-right align-top tabular-nums text-slate-500"
          title={cacheWrite ? `${cacheWrite.toLocaleString()} tokens written to cache` : undefined}
        >
          {cacheWrite ? compactNumber(cacheWrite) : <span className="text-slate-300">—</span>}
        </td>
        <td className="px-2 py-2.5 pr-4 text-right align-top tabular-nums text-slate-500">
          {session.trace_count}
        </td>
      </tr>
      {expanded ? (
        <tr className="bg-slate-50/60">
          <td />
          <td colSpan={columnCount - 1} className="px-2 pb-3 pr-4 pt-1">
            {traceList}
          </td>
        </tr>
      ) : null}
    </>
  );
}

const SESSION_HEADERS = ['', 'Agent', 'Session ID', 'Started', 'Tokens', 'Cache read', 'Cache write', 'Traces'];

// Pool-wide session rollup, or scoped to sessions where the selected agent
// participated (as primary or subagent) and/or the selected workflow ran.
// Each session expands to its traces, which may span multiple agents.
function SessionsTab({ agentId, workflowId, initialSession, initialTrace }) {
  const state = useApi(
    () =>
      api.listSessionRollups({
        agent_id: agentId || undefined,
        workflow_id: workflowId || undefined,
        limit: 500,
      }),
    [agentId, workflowId],
  );

  const wide = useBreakpoint('sm');
  const [expanded, setExpanded] = useState(initialSession || null);
  const [selected, setSelected] = useState(null);
  const [sessionQuery, setSessionQuery] = useState(initialSession || '');
  const [traceQuery, setTraceQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [jumpError, setJumpError] = useState('');
  const [refreshToken, setRefreshToken] = useState(0);
  const openedTrace = useRef('');

  // Deep links (from Feedback / Overview) carry a session to focus on.
  useEffect(() => {
    if (initialSession) {
      setSessionQuery(initialSession);
      setExpanded(initialSession);
    }
  }, [initialSession]);

  const jumpToTrace = async (rawQuery = traceQuery) => {
    const q = rawQuery.trim();
    if (!q) return;
    setJumpError('');
    try {
      const { sessions = [] } = state.data || {};
      for (const session of sessions) {
        const { traces = [] } = await api.listTraces({ session_id: session.session_id, limit: 100 });
        const match = traces.find(
          (t) => String(t.trace_id) === q || String(t.trace_id).startsWith(q),
        );
        if (match) {
          setSelected({ ...match, __agentId: match.agent_id });
          setExpanded(session.session_id);
          setSessionQuery(session.session_id);
          return;
        }
      }
      setJumpError('No matching trace in recent sessions.');
    } catch {
      setJumpError('Trace lookup failed.');
    }
  };

  useEffect(() => {
    if (!initialTrace || !state.data || openedTrace.current === initialTrace) return;
    openedTrace.current = initialTrace;
    setTraceQuery(initialTrace);
    jumpToTrace(initialTrace);
    // The deep link is consumed once per trace id; subsequent reloads preserve the drawer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTrace, state.data]);

  return (
    <>
      <FilterBar>
        <FilterField label="Session" width="sm:w-72">
          <TextInput
            value={sessionQuery}
            placeholder="filter by session id"
            onChange={(e) => setSessionQuery(e.target.value)}
          />
        </FilterField>
        <FilterField label="Trace" width="sm:w-72">
          <div className="flex gap-2">
            <TextInput
              value={traceQuery}
              placeholder="open by trace id"
              onChange={(e) => setTraceQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') jumpToTrace();
              }}
            />
            <Button variant="secondary" onClick={() => jumpToTrace()}>
              Open
            </Button>
          </div>
        </FilterField>
        <FilterField label="Status" width="sm:w-40">
          <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">All statuses</option>
            {Object.entries(TRACE_STATUS).map(([value, { label }]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </FilterField>
        <FilterActions>
          <RefreshButton state={state} />
          {jumpError ? <span className="text-xs text-rose-600">{jumpError}</span> : null}
        </FilterActions>
      </FilterBar>

      <Async state={state} empty={(d) => !d.sessions?.length}>
        {(data) => {
          const q = sessionQuery.trim().toLowerCase();
          const sessions = q
            ? data.sessions.filter((s) => s.session_id.toLowerCase().includes(q))
            : data.sessions;
          if (!sessions.length) return <Empty>No sessions match that filter.</Empty>;
          const total = data.total ?? data.sessions.length;
          const truncated = total > data.sessions.length;
          return (
            <>
            <div className="mb-2 text-xs text-slate-400">
              {q
                ? `${sessions.length} of ${total} session${total === 1 ? '' : 's'}`
                : `${total} session${total === 1 ? '' : 's'}${truncated ? ` · showing latest ${data.sessions.length}` : ''}`}
            </div>
            {(() => {
              const entries = sessions.map((s) => (
                <SessionEntry
                  key={s.session_id}
                  session={s}
                  wide={wide}
                  columnCount={SESSION_HEADERS.length}
                  expanded={expanded === s.session_id}
                  onToggle={() =>
                    setExpanded((cur) => (cur === s.session_id ? null : s.session_id))
                  }
                  onSelectTrace={setSelected}
                  activeTraceId={selected?.trace_id}
                  statusFilter={statusFilter}
                  refreshToken={refreshToken}
                />
              ));
              if (!wide) return <div className="space-y-2">{entries}</div>;
              return (
                <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
                  <table className="min-w-full text-sm">
                    <thead>
                      <tr className="border-b border-slate-200 text-left text-xs font-medium uppercase tracking-wide text-slate-400">
                        {SESSION_HEADERS.map((h, i) => (
                          <th
                            key={h || 'chevron'}
                            className={`px-2 py-2.5 ${i === 0 ? 'pl-4' : ''} ${
                              i >= 4 ? 'text-right' : ''
                            } ${i === SESSION_HEADERS.length - 1 ? 'pr-4' : ''}`}
                          >
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>{entries}</tbody>
                  </table>
                </div>
              );
            })()}
            </>
          );
        }}
      </Async>

      <TraceDrawer
        open={selected !== null}
        trace={selected}
        agentId={selected?.__agentId || agentId}
        onClose={() => setSelected(null)}
        onChanged={() => {
          // A cancel/resume/rerun changes trace status, and a rerun adds a
          // whole new trace, so the expanded session's list is refetched.
          setRefreshToken((n) => n + 1);
          state.reload();
        }}
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
    {
      key: 'path',
      header: 'Path',
      primary: true,
      render: (r) => <span className="break-all font-mono text-xs">{r.path}</span>,
    },
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
      primary: true,
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
  const workflowsState = useApi(() => api.listWorkflows(), []);
  const workflows = workflowsState.data?.workflows || [];

  const tab = params.get('tab') || 'sessions';
  // Empty agent = pool-wide (all agents), matching Overview's aggregate counts.
  const agentId = params.get('agent') || '';
  const workflowId = params.get('workflow') || '';

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

      <FilterBar className="mb-4">
        <FilterField label="Agent">
          <Select value={agentId} onChange={(e) => update({ agent: e.target.value })}>
            <option value="">All agents</option>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.metadata?.display_name || a.agent_id}
              </option>
            ))}
          </Select>
        </FilterField>
        {tab === 'sessions' ? (
          <FilterField label="Workflow">
            <Select value={workflowId} onChange={(e) => update({ workflow: e.target.value })}>
              <option value="">All workflows</option>
              {workflows.map((w) => (
                <option key={w.workflow_id} value={w.workflow_id}>
                  {w.display_name || w.workflow_id}
                </option>
              ))}
            </Select>
          </FilterField>
        ) : null}
      </FilterBar>

      <Tabs tabs={TABS} value={tab} onChange={(id) => update({ tab: id })} />

      {tab === 'sessions' ? (
        <SessionsTab
          agentId={agentId}
          workflowId={workflowId}
          initialSession={params.get('session') || ''}
          initialTrace={params.get('trace') || ''}
        />
      ) : null}
      {tab === 'api' ? <ApiAccessTab /> : null}
      {tab === 'cli' ? <CliTab agentId={agentId} agents={agents} /> : null}
    </div>
  );
}
