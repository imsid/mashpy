import { Link, useParams, useNavigate } from 'react-router-dom';
import { PageHeader } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

export default function ToolDetail() {
  const { agentId, toolName } = useParams();
  const navigate = useNavigate();
  const state = useApi(() => api.listTools(), []);

  return (
    <div>
      <div className="mb-5 flex items-center gap-2 text-sm text-slate-500">
        <Link to="/tools" className="hover:text-slate-700">Tools</Link>
        <span>/</span>
        <span className="text-slate-700">{decodeURIComponent(toolName)}</span>
      </div>

      <Async state={state}>
        {(data) => {
          const entry = data.tools?.find(
            (t) =>
              t.agent_id === decodeURIComponent(agentId) &&
              t.tool.name === decodeURIComponent(toolName),
          );

          if (!entry) {
            return (
              <div className="rounded-lg border border-dashed border-slate-200 px-4 py-10 text-center text-sm text-slate-400">
                Tool not found.{' '}
                <button
                  onClick={() => navigate('/tools')}
                  className="underline hover:text-slate-600"
                >
                  Back to tools
                </button>
              </div>
            );
          }

          const { tool } = entry;
          const params = tool.parameters;
          const properties = params?.properties || {};
          const required = new Set(params?.required || []);

          return (
            <div className="max-w-2xl space-y-6">
              <div>
                <PageHeader title={tool.name} />
                {tool.description && (
                  <p className="text-sm text-slate-600">{tool.description}</p>
                )}
                <div className="mt-3 flex flex-wrap gap-2">
                  <div className="flex items-center gap-1.5">
                    <span className="text-xs text-slate-400">Agent</span>
                    <Link
                      to={`/logs?agent=${encodeURIComponent(entry.agent_id)}&tab=sessions`}
                    >
                      <Mono>{entry.agent_id}</Mono>
                    </Link>
                  </div>
                  {tool.requires_approval && (
                    <Chip tone="amber">requires approval</Chip>
                  )}
                  {tool.parallel_safe === false && (
                    <Chip tone="slate">sequential</Chip>
                  )}
                </div>
              </div>

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
                            {schema.type && (
                              <Chip tone="indigo">{schema.type}</Chip>
                            )}
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
