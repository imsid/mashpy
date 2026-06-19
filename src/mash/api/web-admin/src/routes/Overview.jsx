import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { BarChart } from '../components/BarChart.jsx';
import { Button } from '../components/Form.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { compactNumber, formatTime } from '../lib/format.js';

const DAY = 86400;
const WINDOW_DAYS = 7;

function Stat({ label, value, to }) {
  const body = (
    <Card className="px-4 py-3 transition hover:border-slate-300">
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
      <div className="mt-0.5 text-xs font-medium uppercase tracking-wide text-slate-400">
        {label}
      </div>
    </Card>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

// Fetch agents, then per-agent usage + sessions, and aggregate into pool totals
// and a merged daily series. Per-agent calls because the telemetry endpoints
// are agent-scoped by design.
async function loadOverview() {
  const { agents = [], hosts = [] } = await api.listAgents();
  const fromTs = Math.floor(Date.now() / 1000) - WINDOW_DAYS * DAY;

  const perAgent = await Promise.all(
    agents.map(async (a) => {
      const [usage, sessions] = await Promise.all([
        api.usage({ agent_id: a.agent_id, bucket: 'day', from_ts: fromTs }).catch(() => ({ buckets: [] })),
        api.listSessions(a.agent_id).catch(() => ({ sessions: [] })),
      ]);
      return { usage: usage.buckets || [], sessions: sessions.sessions || [] };
    }),
  );

  const merged = new Map();
  let requests = 0;
  let tokens = 0;
  let sessions = 0;
  for (const { usage, sessions: ss } of perAgent) {
    sessions += ss.length;
    for (const b of usage) {
      requests += b.request_count;
      tokens += b.input_tokens + b.output_tokens;
      const cur = merged.get(b.bucket_start) || { requests: 0, tokens: 0 };
      cur.requests += b.request_count;
      cur.tokens += b.input_tokens + b.output_tokens;
      merged.set(b.bucket_start, cur);
    }
  }

  // Build a contiguous day series so empty days still show.
  const todayBucket = Math.floor(Date.now() / 1000 / DAY) * DAY;
  const series = [];
  for (let i = WINDOW_DAYS - 1; i >= 0; i -= 1) {
    const bucketStart = todayBucket - i * DAY;
    const entry = merged.get(bucketStart) || { requests: 0, tokens: 0 };
    const label = new Date(bucketStart * 1000).toLocaleDateString(undefined, {
      month: 'numeric',
      day: 'numeric',
    });
    series.push({ label, requests: entry.requests, tokens: entry.tokens });
  }

  return {
    counts: { agents: agents.length, hosts: hosts.length, sessions, requests, tokens },
    series,
  };
}

// Merge SSE event streams across every agent into one rolling feed.
function useLiveFeed(agentIds) {
  const [events, setEvents] = useState([]);
  const sourcesRef = useRef([]);

  useEffect(() => {
    if (!agentIds.length) return undefined;
    const sources = agentIds.map((agentId) =>
      api.streamEvents({ agent_id: agentId }, (event) => {
        setEvents((cur) => [{ ...event, _key: `${event.event_id}-${agentId}` }, ...cur].slice(0, 30));
      }),
    );
    sourcesRef.current = sources;
    return () => sources.forEach((s) => s.close());
  }, [agentIds.join(',')]);

  return events;
}

function LiveFeed({ agentIds }) {
  const events = useLiveFeed(agentIds);
  return (
    <Card className="divide-y divide-slate-100">
      {events.length === 0 ? (
        <div className="px-4 py-8 text-center text-sm text-slate-400">
          Waiting for activity…
        </div>
      ) : (
        events.map((e) => (
          <div key={e._key} className="flex items-center gap-3 px-4 py-2 text-sm">
            <span className="w-20 shrink-0 text-xs tabular-nums text-slate-400">
              {formatTime(e.created_at).split(', ')[1] || ''}
            </span>
            <Mono>{e.agent_id || e.app_id}</Mono>
            {e.host_id ? <Chip>{e.host_id}</Chip> : null}
            <span className="truncate text-slate-600">
              {String(e.event_type || '').replace('runtime.', '')}
            </span>
          </div>
        ))
      )}
    </Card>
  );
}

export default function Overview() {
  const state = useApi(loadOverview, []);
  const agentsState = useApi(() => api.listAgents(), []);
  const [metric, setMetric] = useState('requests');

  const agentIds = (agentsState.data?.agents || []).map((a) => a.agent_id);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description="Deployment health and recent activity across the pool."
        actions={
          <Button variant="ghost" onClick={state.reload}>
            Refresh
          </Button>
        }
      />

      <Async state={state}>
        {(data) => (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Stat label="Agents" value={data.counts.agents} to="/agents" />
              <Stat label="Hosts" value={data.counts.hosts} to="/hosts" />
              <Stat label="Sessions" value={data.counts.sessions} to="/logs?tab=sessions" />
              <Stat label={`Requests / ${WINDOW_DAYS}d`} value={data.counts.requests} to="/logs" />
            </div>

            <Card className="p-4">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-sm font-semibold">Usage</h2>
                <div className="flex gap-1 rounded-md border border-slate-200 p-0.5 text-xs">
                  {['requests', 'tokens'].map((m) => (
                    <button
                      key={m}
                      onClick={() => setMetric(m)}
                      className={`rounded px-2 py-1 font-medium capitalize ${
                        metric === m ? 'bg-slate-900 text-white' : 'text-slate-500'
                      }`}
                    >
                      {m}
                    </button>
                  ))}
                </div>
              </div>
              <BarChart
                data={data.series.map((d) => ({ label: d.label, value: d[metric] }))}
                format={compactNumber}
              />
            </Card>
          </>
        )}
      </Async>

      <div>
        <div className="mb-2 flex items-center gap-2">
          <h2 className="text-sm font-semibold">Live activity</h2>
          <span className="inline-flex items-center gap-1 text-xs text-emerald-600">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
            streaming
          </span>
        </div>
        <LiveFeed agentIds={agentIds} />
      </div>
    </div>
  );
}
