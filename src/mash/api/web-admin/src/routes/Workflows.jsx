import { PageHeader, Card } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

// Metadata keys rendered structurally elsewhere — don't echo them as chips.
const META_CHIP_SKIP = new Set(['display_name', 'description']);

// Summarize a JSON-schema-ish structured-output spec into `field: type` pairs.
function schemaShape(schema) {
  const props = schema?.properties;
  if (!props || typeof props !== 'object') return [];
  return Object.entries(props).map(([name, def]) => {
    let type = def?.type || 'any';
    if (type === 'array' && def.items?.type) type = `${def.items.type}[]`;
    return { name, type };
  });
}

function WorkflowCard({ workflow }) {
  const meta = workflow.metadata || {};
  const name = meta.display_name || workflow.workflow_id;
  const tasks = workflow.tasks || [];
  const chips = Object.entries(meta).filter(
    ([k, v]) => !META_CHIP_SKIP.has(k) && v != null && v !== '',
  );
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div>
        <div className="flex items-center justify-between gap-2">
          <h3 className="font-display text-base font-semibold">{name}</h3>
          {name !== workflow.workflow_id ? <Mono>{workflow.workflow_id}</Mono> : null}
        </div>
        {meta.description ? (
          <p className="mt-1 text-sm text-slate-600">{meta.description}</p>
        ) : null}
        {workflow.skill_name || chips.length ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {workflow.skill_name ? (
              <Chip tone="indigo">skill: {workflow.skill_name}</Chip>
            ) : null}
            {chips.map(([k, v]) => (
              <Chip key={k}>
                {k}: {String(v)}
              </Chip>
            ))}
          </div>
        ) : null}
      </div>

      <div className="border-t border-slate-100 pt-3">
        <div className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
          Task chain ({tasks.length})
        </div>
        {tasks.length ? (
          <ol className="space-y-2.5">
            {tasks.map((task, idx) => {
              const shape = schemaShape(task.structured_output);
              return (
                <li key={task.task_id} className="text-sm">
                  <div className="flex items-center gap-2">
                    <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-slate-100 text-xs tabular-nums text-slate-500">
                      {idx + 1}
                    </span>
                    <Mono>{task.task_id}</Mono>
                    <span className="text-slate-300">→</span>
                    <span className="text-slate-600">{task.agent_id}</span>
                  </div>
                  {shape.length ? (
                    <div className="ml-7 mt-1.5">
                      <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-slate-400">
                        Output
                      </div>
                      <div className="flex flex-wrap gap-1">
                        {shape.map((f) => (
                          <span
                            key={f.name}
                            className="rounded bg-slate-50 px-1.5 py-0.5 font-mono text-xs text-slate-500"
                          >
                            {f.name}: <span className="text-slate-400">{f.type}</span>
                          </span>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ol>
        ) : (
          <span className="text-xs text-slate-400">No tasks defined.</span>
        )}
      </div>
    </Card>
  );
}

export default function Workflows() {
  const state = useApi(() => api.listWorkflows(), []);

  return (
    <div>
      <PageHeader
        title="Workflows"
        description="Ordered task chains registered in the pool."
      />
      <Async state={state} empty={(d) => !d.workflows?.length}>
        {(data) => {
          if (!data.workflows?.length) {
            return <Empty>No workflows registered in this pool.</Empty>;
          }
          return (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {data.workflows.map((workflow) => (
                <WorkflowCard key={workflow.workflow_id} workflow={workflow} />
              ))}
            </div>
          );
        }}
      </Async>
    </div>
  );
}
