import { PageHeader, Card } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

function WorkflowCard({ workflow }) {
  const meta = workflow.metadata || {};
  const name = meta.display_name || workflow.workflow_id;
  const tasks = workflow.tasks || [];
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div>
        <div className="flex items-center justify-between gap-2">
          <h3 className="font-display text-base font-semibold">{name}</h3>
          <Mono>{workflow.workflow_id}</Mono>
        </div>
        {meta.description ? (
          <p className="mt-1 text-sm text-slate-600">{meta.description}</p>
        ) : null}
      </div>

      <div className="border-t border-slate-100 pt-3">
        <div className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
          Task chain ({tasks.length})
        </div>
        {tasks.length ? (
          <ol className="space-y-1.5">
            {tasks.map((task, idx) => (
              <li key={task.task_id} className="flex items-center gap-2 text-sm">
                <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-slate-100 text-xs tabular-nums text-slate-500">
                  {idx + 1}
                </span>
                <Mono>{task.task_id}</Mono>
                <span className="text-slate-300">→</span>
                <span className="text-slate-600">{task.agent_id}</span>
              </li>
            ))}
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
