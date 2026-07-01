import { Link } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

function InvocationCount({ toolName, invocations }) {
  if (!invocations) return null;
  const entry = invocations.find((i) => i.tool_name === toolName);
  if (!entry) return null;
  const byAgent = Object.entries(entry.by_agent || {});
  return (
    <div className="mt-auto border-t border-slate-100 pt-3">
      <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">
        Invocations
      </div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-sm font-semibold text-slate-800">{entry.total.toLocaleString()}</span>
        {byAgent.length > 1 &&
          byAgent.map(([agentId, count]) => (
            <span key={agentId} className="text-xs text-slate-500">
              {agentId}: {count.toLocaleString()}
            </span>
          ))}
      </div>
    </div>
  );
}

function ToolCard({ entry, invocations }) {
  const { tool, agents } = entry;
  return (
    <Card
      to={`/tools/${encodeURIComponent(tool.name)}`}
      className="flex h-full flex-col gap-3 p-4"
    >
      <div>
        <div className="flex items-start justify-between gap-2">
          <h3 className="min-w-0 truncate font-display text-base font-semibold">{tool.name}</h3>
          <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
            {tool.requires_approval && <Chip tone="amber">approval</Chip>}
            {tool.parallel_safe === false && <Chip tone="slate">sequential</Chip>}
          </div>
        </div>
        {tool.description ? (
          <p className="mt-1 line-clamp-4 text-sm text-slate-600">{tool.description}</p>
        ) : (
          <p className="mt-1 text-sm italic text-slate-400">No description.</p>
        )}
      </div>

      <div className="flex flex-wrap gap-1.5">
        {agents.map((agentId) => (
          <Link
            key={agentId}
            to={`/logs?agent=${encodeURIComponent(agentId)}&tab=sessions`}
            onClick={(e) => e.stopPropagation()}
            className="inline-block"
          >
            <Mono>{agentId}</Mono>
          </Link>
        ))}
      </div>

      <InvocationCount toolName={tool.name} invocations={invocations} />
    </Card>
  );
}

export default function Tools() {
  const toolsState = useApi(() => api.listTools(), []);
  const countsState = useApi(() => api.listToolInvocations(), []);

  return (
    <div>
      <PageHeader
        title="Tools"
        description="All tools registered across the agent pool."
      />
      <Async state={toolsState} empty={(d) => !d.tools?.length}>
        {(data) => {
          const tools = [...data.tools].sort((a, b) =>
            a.tool.name.localeCompare(b.tool.name),
          );
          const invocations = countsState.data?.invocations ?? null;
          return (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {tools.map((entry) => (
                <ToolCard
                  key={entry.tool.name}
                  entry={entry}
                  invocations={invocations}
                />
              ))}
            </div>
          );
        }}
      </Async>
    </div>
  );
}
