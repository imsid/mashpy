import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

function Stat({ label, value }) {
  return (
    <Card className="px-4 py-3">
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
      <div className="mt-0.5 text-xs font-medium uppercase tracking-wide text-slate-400">
        {label}
      </div>
    </Card>
  );
}

export default function Overview() {
  const state = useApi(() => api.listAgents(), []);

  return (
    <div>
      <PageHeader
        title="Overview"
        description="Deployment health and recent activity across the pool."
      />
      <Async state={state}>
        {(data) => (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Agents" value={data.agents?.length ?? 0} />
            <Stat label="Hosts" value={data.hosts?.length ?? 0} />
            <Stat label="Sessions" value="—" />
            <Stat label="Requests / 24h" value="—" />
          </div>
        )}
      </Async>
      <p className="mt-6 text-sm text-slate-400">
        Usage chart and live activity feed land in Phase 2.
      </p>
    </div>
  );
}
