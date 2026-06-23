import { Link, useParams, useNavigate } from 'react-router-dom';
import { PageHeader } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

export default function ToolDetail() {
  const { toolName } = useParams();
  const navigate = useNavigate();
  const toolsState = useApi(() => api.listTools(), []);
  const countsState = useApi(() => api.listToolInvocations(), []);

  return (
    <div>
      <div className="mb-5 flex items-center gap-2 text-sm text-slate-500">
        <Link to="/tools" className="hover:text-slate-700">Tools</Link>
        <span>/</span>
        <span className="text-slate-700">{decodeURIComponent(toolName)}</span>
      </div>

      <Async state={toolsState}>
        {(data) => {
          const decoded = decodeURIComponent(toolName);
          const entry = data.tools?.find((t) => t.tool.name === decoded);

          if (!entry) {
            return (
              <div className="rounded-lg border border-dashed border-slate-200 px-4 py-10 text-center text-sm text-slate-400">
                Tool not found.{' '}
                <button onClick={() => navigate('/tools')} className="underline hover:text-slate-600">
                  Back to tools
                </button>
              </div>
            );
          }

          const { tool, agents } = entry;
          const params = tool.parameters;
          const properties = params?.properties || {};
          const required = new Set(params?.required || []);
          const counts = countsState.data?.invocations?.find((i) => i.tool_name === tool.name);

          return (
            <div className="max-w-2xl space-y-6">
              <div>
                <PageHeader title={tool.name} />
                {tool.description && (
                  <p className="text-sm text-slate-600">{tool.description}</p>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  {tool.requires_approval && <Chip tone="amber">requires approval</Chip>}
                  {tool.parallel_safe === false && <Chip tone="slate">sequential</Chip>}
                  {agents.map((agentId) => (
                    <div key={agentId} className="flex items-center gap-1.5">
                      <span className="text-xs text-slate-400">agent</span>
                      <Link to={`/logs?agent=${encodeURIComponent(agentId)}&tab=sessions`}>
                        <Mono>{agentId}</Mono>
                      </Link>
                    </div>
                  ))}
                </div>
              </div>

              {counts && (
                <div className="rounded-lg border border-slate-200 px-4 py-3">
                  <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Invocations</div>
                  <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
                    <span className="text-lg font-semibold text-slate-800">{counts.total.toLocaleString()} total</span>
                    {Object.entries(counts.by_agent).map(([agentId, count]) => (
                      <span key={agentId} className="text-sm text-slate-500">
                        {agentId}: {count.toLocaleString()}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {Object.keys(properties).length > 0 && (
                <div>
                  <h2 className="mb-3 text-sm font-semibold text-slate-700">Parameters</h2>
                  <div className="divide-y divide-slate-100 rounded-lg border border-slate-200">
                    {Object.entries(properties).map(([name, schema]) => (
                      <div key={name} className="flex gap-4 px-4 py-3">
                        <div className="w-40 shrink-0">
                          <Mono>{name}</Mono>
                          {required.has(name) && (
                            <span className="ml-1.5 text-xs text-rose-500">required</span>
                          )}
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            {schema.type && <Chip tone="indigo">{schema.type}</Chip>}
                          </div>
                          {schema.description && (
                            <p className="mt-1 text-sm text-slate-500">{schema.description}</p>
                          )}
                          {schema.enum && (
                            <div className="mt-1 flex flex-wrap gap-1">
                              {schema.enum.map((v) => (
                                <Chip key={String(v)} tone="slate">{String(v)}</Chip>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div>
                <h2 className="mb-3 text-sm font-semibold text-slate-700">Schema</h2>
                <pre className="overflow-auto rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-700">
                  {JSON.stringify(params, null, 2)}
                </pre>
              </div>
            </div>
          );
        }}
      </Async>
    </div>
  );
}
