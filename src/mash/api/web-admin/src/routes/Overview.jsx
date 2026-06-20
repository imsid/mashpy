import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { BarChart } from '../components/BarChart.jsx';
import { Button } from '../components/Form.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { compactNumber } from '../lib/format.js';

const DAY = 86400;
const WINDOW_DAYS = 30;

const USAGE_SERIES = [
  { key: 'traces', label: 'Traces', barClass: 'fill-blue-500', dotClass: 'bg-blue-500' },
  { key: 'tokens', label: 'Tokens', barClass: 'fill-emerald-500', dotClass: 'bg-emerald-500' },
];

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

// Fetch agents, then per-agent hourly usage + sessions, and aggregate into pool
// totals and a merged daily series. Hourly buckets (not daily) so re-bucketing
// to the viewer's local day is accurate: each bucket's instant maps to the
// right local day, and traces (request_count) and tokens come from the same
// source, keeping the two bars on the same day. Per-agent calls because the
// telemetry endpoints are agent-scoped by design.
async function loadOverview() {
  const { agents = [], hosts = [] } = await api.listAgents();
  const fromTs = Math.floor(Date.now() / 1000) - WINDOW_DAYS * DAY;

  const perAgent = await Promise.all(
    agents.map(async (a) => {
      const [usage, sessions] = await Promise.all([
        api.usage({ agent_id: a.agent_id, bucket: 'hour', from_ts: fromTs }).catch(() => ({ buckets: [] })),
        api.listSessions(a.agent_id).catch(() => ({ sessions: [] })),
      ]);
      return { usage: usage.buckets || [], sessions: sessions.sessions || [] };
    }),
  );

  const merged = new Map();
  let tokens = 0;
  let sessions = 0;
  let traceTotal = 0;
  for (const { usage, sessions: ss } of perAgent) {
    sessions += ss.length;
    for (const b of usage) {
      const bucketTokens = (b.input_tokens || 0) + (b.output_tokens || 0);
      const bucketTraces = b.request_count || 0;
      tokens += bucketTokens;
      traceTotal += bucketTraces;
      const key = localDayKey(b.bucket_start);
      const cur = merged.get(key) || { traces: 0, tokens: 0 };
      cur.traces += bucketTraces;
      cur.tokens += bucketTokens;
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
                <span className="text-xs text-slate-400">Last {WINDOW_DAYS} days</span>
              </div>
              <BarChart data={data.series} series={USAGE_SERIES} format={compactNumber} />
            </Card>
          </>
        )}
      </Async>
    </div>
  );
}
