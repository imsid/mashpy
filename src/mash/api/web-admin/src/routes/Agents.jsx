import { useEffect } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { agentAnchor, buildAgentUsage } from '../lib/agent.js';
import { useApi } from '../lib/useApi.js';

function AgentCard({ agent, usedIn, highlighted }) {
  const meta = agent.metadata;
  const name = meta?.display_name || agent.agent_id;
  return (
    <Card
      id={agentAnchor(agent.agent_id)}
      className={`flex h-full scroll-mt-6 flex-col gap-3 p-4 transition hover:border-slate-300 hover:shadow-sm ${highlighted ? 'border-indigo-300 ring-2 ring-indigo-100' : ''}`}
    >
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
            <Chip key={cap}>{cap}</Chip>
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
            {usedIn.map((usage) => usage.type === 'workflow' ? (
              <Link
                key={`${usage.type}-${usage.id}-${usage.role}`}
                to={`/workflows/${encodeURIComponent(usage.id)}`}
                className="hover:opacity-75"
              >
                <Chip>{usage.id} · {usage.role}</Chip>
              </Link>
            ) : (
              <Chip key={`${usage.type}-${usage.id}-${usage.role}`}>
                {usage.id} · {usage.role}
              </Chip>
            ))}
          </div>
        ) : (
          <span className="text-xs text-slate-400">No host or workflow references.</span>
        )}
      </div>
      <Link
        to={`/logs?agent=${encodeURIComponent(agent.agent_id)}&tab=sessions`}
        className="text-xs font-medium text-indigo-600 hover:underline"
      >
        View logs →
      </Link>
    </Card>
  );
}

export default function Agents() {
  const location = useLocation();
  const state = useApi(async () => {
    const [deployment, catalog] = await Promise.all([
      api.listAgents(),
      api.listWorkflows(),
    ]);
    const definitions = await Promise.all(
      (catalog.workflows || [])
        .filter((workflow) => workflow.mode === 'pipeline')
        .map((workflow) => api.getWorkflow(workflow.workflow_id)),
    );
    return { ...deployment, workflowDefinitions: definitions };
  }, []);

  useEffect(() => {
    if (state.loading || !location.hash) return;
    const id = location.hash.slice(1);
    document.getElementById(id)?.scrollIntoView({ block: 'center' });
  }, [location.hash, state.loading]);

  return (
    <div>
      <PageHeader
        title="Agents"
        description="The role-less agent pool — the building blocks for hosts."
      />
      <Async state={state} empty={(d) => !d.agents?.length}>
        {(data) => {
          const usage = buildAgentUsage(data.hosts, data.workflowDefinitions);
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
                  highlighted={location.hash === `#${agentAnchor(agent.agent_id)}`}
                />
              ))}
            </div>
          );
        }}
      </Async>
    </div>
  );
}
