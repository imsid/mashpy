import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

// Map each agent_id to the hosts that reference it, with the role it plays.
function buildHostUsage(hosts) {
  const usage = new Map();
  for (const host of hosts || []) {
    const add = (agentId, role) => {
      if (!agentId) return;
      const list = usage.get(agentId) || [];
      list.push({ hostId: host.host_id, role });
      usage.set(agentId, list);
    };
    add(host.primary, 'primary');
    for (const sub of host.subagents || []) add(sub, 'subagent');
  }
  return usage;
}

function AgentCard({ agent, usedIn }) {
  const meta = agent.metadata;
  const name = meta?.display_name || agent.agent_id;
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div>
        <div className="flex items-center justify-between gap-2">
          <h3 className="font-display text-base font-semibold">{name}</h3>
          <Mono>{agent.agent_id}</Mono>
        </div>
        {meta?.description ? (
          <p className="mt-1 text-sm text-slate-600">{meta.description}</p>
        ) : (
          <p className="mt-1 text-sm italic text-slate-400">No metadata registered.</p>
        )}
      </div>

      {meta?.capabilities?.length ? (
        <div className="flex flex-wrap gap-1.5">
          {meta.capabilities.map((cap) => (
            <Chip key={cap} tone="indigo">
              {cap}
            </Chip>
          ))}
        </div>
      ) : null}

      {meta?.usage_guidance ? (
        <p className="text-xs leading-relaxed text-slate-500">
          <span className="font-medium text-slate-600">When to use: </span>
          {meta.usage_guidance}
        </p>
      ) : null}

      <div className="mt-auto border-t border-slate-100 pt-3">
        <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">
          Used in
        </div>
        {usedIn?.length ? (
          <div className="flex flex-wrap gap-1.5">
            {usedIn.map(({ hostId, role }) => (
              <Chip key={`${hostId}-${role}`} tone={role === 'primary' ? 'emerald' : 'slate'}>
                {hostId} · {role}
              </Chip>
            ))}
          </div>
        ) : (
          <span className="text-xs text-slate-400">No host composition.</span>
        )}
      </div>
    </Card>
  );
}

export default function Agents() {
  const state = useApi(() => api.listAgents(), []);

  return (
    <div>
      <PageHeader
        title="Agents"
        description="The role-less agent pool — the building blocks for hosts."
      />
      <Async state={state} empty={(d) => !d.agents?.length}>
        {(data) => {
          const usage = buildHostUsage(data.hosts);
          const agents = [...data.agents].sort((a, b) =>
            a.agent_id.localeCompare(b.agent_id),
          );
          return (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {agents.map((agent) => (
                <AgentCard
                  key={agent.agent_id}
                  agent={agent}
                  usedIn={usage.get(agent.agent_id)}
                />
              ))}
            </div>
          );
        }}
      </Async>
    </div>
  );
}
