import { Link } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

function ToolCard({ agentId, tool }) {
  return (
    <Card
      to={`/tools/${encodeURIComponent(agentId)}/${encodeURIComponent(tool.name)}`}
      className="flex h-full flex-col gap-3 p-4"
    >
      <div>
        <div className="flex items-center justify-between gap-2">
          <h3 className="font-display text-base font-semibold">{tool.name}</h3>
          <div className="flex shrink-0 gap-1.5">
            {tool.requires_approval && (
              <Chip tone="amber">approval</Chip>
            )}
            {tool.parallel_safe === false && (
              <Chip tone="slate">sequential</Chip>
            )}
          </div>
        </div>
        {tool.description ? (
          <p className="mt-1 text-sm text-slate-600">{tool.description}</p>
        ) : (
          <p className="mt-1 text-sm italic text-slate-400">No description.</p>
        )}
      </div>

      <div className="mt-auto border-t border-slate-100 pt-3">
        <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">
          Agent
        </div>
        <Link
          to={`/logs?agent=${encodeURIComponent(agentId)}&tab=sessions`}
          onClick={(e) => e.stopPropagation()}
          className="inline-block"
        >
          <Mono>{agentId}</Mono>
        </Link>
      </div>
    </Card>
  );
}

export default function Tools() {
  const state = useApi(() => api.listTools(), []);

  return (
    <div>
      <PageHeader
        title="Tools"
        description="All tools registered across the agent pool."
      />
      <Async state={state} empty={(d) => !d.tools?.length}>
        {(data) => {
          const tools = [...data.tools].sort((a, b) =>
            a.tool.name.localeCompare(b.tool.name),
          );
          return (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {tools.map((entry) => (
                <ToolCard
                  key={`${entry.agent_id}:${entry.tool.name}`}
                  agentId={entry.agent_id}
                  tool={entry.tool}
                />
              ))}
            </div>
          );
        }}
      </Async>
    </div>
  );
}
