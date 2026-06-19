import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { PageHeader } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
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
  { id: 'requests', label: 'Requests' },
  { id: 'sessions', label: 'Sessions' },
  { id: 'api', label: 'API access' },
];

function statusTone(code) {
  if (code >= 500) return 'rose';
  if (code >= 400) return 'amber';
  return 'emerald';
}

function RequestsTab({ agentId, hostId, sessionId }) {
  const state = useApi(
    () =>
      agentId
        ? api.listTraces({
            agent_id: agentId,
            host_id: hostId || undefined,
            session_id: sessionId || undefined,
            limit: 100,
          })
        : Promise.resolve({ traces: [] }),
    [agentId, hostId, sessionId],
  );
  const [selected, setSelected] = useState(null);

  if (!agentId) return <Empty>Pick an agent to view its request traces.</Empty>;

  const columns = [
    { key: 'time', header: 'Time', render: (r) => formatTime(r.latest_event_at) },
    {
      key: 'host_id',
      header: 'Host',
      render: (r) => (r.host_id ? <Mono>{r.host_id}</Mono> : <span className="text-slate-300">—</span>),
    },
    {
      key: 'session_id',
      header: 'Session',
      render: (r) => <Mono>{r.session_id || '—'}</Mono>,
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

  return (
    <>
      <Async state={state} empty={(d) => !d.traces?.length}>
        {(data) => (
          <Table
            columns={columns}
            rows={data.traces}
            getRowKey={(r) => r.trace_id}
            activeKey={selected?.trace_id}
            onRowClick={setSelected}
          />
        )}
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

function SessionsTab({ agentId, onOpenSession }) {
  const state = useApi(
    () => (agentId ? api.listSessions(agentId) : Promise.resolve({ sessions: [] })),
    [agentId],
  );

  if (!agentId) return <Empty>Pick an agent to view its sessions.</Empty>;

  const columns = [
    { key: 'session_id', header: 'Session', render: (r) => <Mono>{r.session_id}</Mono> },
    { key: 'turn_count', header: 'Turns', align: 'right' },
    {
      key: 'session_total_tokens',
      header: 'Tokens',
      align: 'right',
      render: (r) => compactNumber(r.session_total_tokens),
    },
    {
      key: 'last_activity_at',
      header: 'Last activity',
      render: (r) => formatTime(r.last_activity_at),
    },
  ];

  return (
    <Async state={state} empty={(d) => !d.sessions?.length}>
      {(data) => (
        <Table
          columns={columns}
          rows={data.sessions}
          getRowKey={(r) => r.session_id}
          onRowClick={(r) => onOpenSession(r.session_id)}
        />
      )}
    </Async>
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

  const tab = params.get('tab') || 'requests';
  const agentId = params.get('agent') || agents[0]?.agent_id || '';
  const hostId = params.get('host') || '';
  const sessionId = params.get('session') || '';

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
        description="Request traces, sessions, and API access for one agent."
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
        {tab === 'requests' ? (
          <>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-slate-600">Host</span>
              <div className="w-40">
                <TextInput
                  value={hostId}
                  placeholder="any"
                  onChange={(e) => update({ host: e.target.value })}
                />
              </div>
            </label>
            {sessionId ? (
              <Button variant="ghost" onClick={() => update({ session: '' })}>
                session: {sessionId} ✕
              </Button>
            ) : null}
          </>
        ) : null}
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

      {tab === 'requests' ? (
        <RequestsTab agentId={agentId} hostId={hostId} sessionId={sessionId} />
      ) : null}
      {tab === 'sessions' ? (
        <SessionsTab
          agentId={agentId}
          onOpenSession={(s) => update({ session: s, tab: 'requests' })}
        />
      ) : null}
      {tab === 'api' ? <ApiAccessTab /> : null}
    </div>
  );
}
