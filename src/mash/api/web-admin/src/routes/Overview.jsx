import { useState } from 'react';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { BarChart } from '../components/BarChart.jsx';
import { Button } from '../components/Form.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { compactNumber } from '../lib/format.js';

const DAY = 86400;
const WINDOW_DAYS = 7;

function Stat({ label, value, hint, to }) {
  return (
    <Card to={to} className="px-4 py-3">
      <div className="flex items-start justify-between gap-2">
        <div className="text-2xl font-semibold tabular-nums">{value}</div>
        {to ? (
          <span className="text-slate-300 transition group-hover:translate-x-0.5 group-hover:text-slate-500">
            →
          </span>
        ) : null}
      </div>
      <div className="mt-0.5 text-xs font-medium uppercase tracking-wide text-slate-400">
        {label}
      </div>
      {hint ? <div className="mt-0.5 text-xs text-slate-400">{hint}</div> : null}
    </Card>
  );
}

// Key a unix timestamp to the start of its day in the viewer's local timezone.
// The backend buckets on UTC midnight, so without this the bars land under the
// wrong day for anyone west of UTC and disagree with the local times in Logs.
function localDayKey(unixSeconds) {
  const d = new Date(unixSeconds * 1000);
  d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}

// Fetch agents, then per-agent usage + sessions + recent traces, and aggregate
// into pool totals and a merged daily series. Per-agent calls because the
// telemetry endpoints are agent-scoped by design.
async function loadOverview() {
  const { agents = [], hosts = [] } = await api.listAgents();
  const fromTs = Math.floor(Date.now() / 1000) - WINDOW_DAYS * DAY;

  const perAgent = await Promise.all(
    agents.map(async (a) => {
      const [usage, sessions, traces] = await Promise.all([
        api.usage({ agent_id: a.agent_id, bucket: 'day', from_ts: fromTs }).catch(() => ({ buckets: [] })),
        api.listSessions(a.agent_id).catch(() => ({ sessions: [] })),
        api.listTraces({ agent_id: a.agent_id, limit: 100 }).catch(() => ({ traces: [] })),
      ]);
      return {
        usage: usage.buckets || [],
        sessions: sessions.sessions || [],
        traces: traces.traces || [],
      };
    }),
  );

  // Aggregate tokens (from usage) and traces (from recent traces) by local day.
  const merged = new Map();
  let tokens = 0;
  let sessions = 0;
  let traceTotal = 0;
  for (const { usage, sessions: ss, traces } of perAgent) {
    sessions += ss.length;
    for (const b of usage) {
      tokens += b.input_tokens + b.output_tokens;
      const key = localDayKey(b.bucket_start);
      const cur = merged.get(key) || { traces: 0, tokens: 0 };
      cur.tokens += b.input_tokens + b.output_tokens;
      merged.set(key, cur);
    }
    for (const t of traces) {
      if (!t.started_at || t.started_at < fromTs) continue;
      traceTotal += 1;
      const key = localDayKey(t.started_at);
      const cur = merged.get(key) || { traces: 0, tokens: 0 };
      cur.traces += 1;
      merged.set(key, cur);
    }
  }

  // Build a contiguous local-day series so empty days still show.
  const series = [];
  for (let i = WINDOW_DAYS - 1; i >= 0; i -= 1) {
    const day = new Date();
    day.setHours(0, 0, 0, 0);
    day.setDate(day.getDate() - i);
    const key = Math.floor(day.getTime() / 1000);
    const entry = merged.get(key) || { traces: 0, tokens: 0 };
    const label = day.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' });
    series.push({ label, traces: entry.traces, tokens: entry.tokens });
  }

  return {
    counts: { agents: agents.length, hosts: hosts.length, sessions, tokens, traces: traceTotal },
    series,
  };
}

export default function Overview() {
  const state = useApi(loadOverview, []);
  const [metric, setMetric] = useState('traces');

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
            <div className="grid grid-cols-3 gap-3">
              <Stat label="Agents" value={data.counts.agents} to="/agents" />
              <Stat label="Hosts" value={data.counts.hosts} to="/hosts" />
              <Stat
                label="Sessions"
                value={data.counts.sessions}
                hint="across all agents"
                to="/logs?tab=sessions"
              />
            </div>

            <Card className="p-4">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-sm font-semibold">Usage</h2>
                <div className="flex gap-1 rounded-md border border-slate-200 p-0.5 text-xs">
                  {['traces', 'tokens'].map((m) => (
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
    </div>
  );
}
