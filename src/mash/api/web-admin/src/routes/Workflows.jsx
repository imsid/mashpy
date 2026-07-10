import { useMemo, useState } from 'react';

import { PageHeader, Card } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Select, TextInput } from '../components/Form.jsx';
import { KindBadge, StatusBadge } from '../components/workflows/WorkflowUI.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { formatTime } from '../lib/format.js';
import { workflowCatalogEntries, workflowType } from '../lib/workflow.js';

const TYPE_LABELS = {
  all: 'All workflows',
  code: 'Code only',
  agent: 'Agent only',
  mixed: 'Mixed',
};

function WorkflowCard({ workflow }) {
  const type = workflowType(workflow);
  const preview = workflow.step_preview || [];
  return (
    <Card
      to={`/workflows/${encodeURIComponent(workflow.workflow_id)}`}
      className="flex h-full flex-col gap-4 p-4"
    >
      <div>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate font-display text-base font-semibold">{workflow.display_name}</h2>
            {workflow.display_name !== workflow.workflow_id ? <Mono>{workflow.workflow_id}</Mono> : null}
          </div>
          <Chip tone={type === 'mixed' ? 'indigo' : 'slate'}>
            {type} · {workflow.step_count}
          </Chip>
        </div>
        {workflow.description ? (
          <p className="mt-2 line-clamp-2 text-sm text-slate-600">{workflow.description}</p>
        ) : null}
      </div>

      <ol className="space-y-1.5 border-t border-slate-100 pt-3">
        {preview.map((step) => (
          <li key={step.step_id} className="flex items-center gap-2 text-sm">
            <span className="w-5 text-right text-xs tabular-nums text-slate-400">{step.ordinal + 1}</span>
            <KindBadge kind={step.kind} />
            <span className="truncate font-mono text-xs text-slate-700">{step.step_id}</span>
            {step.agent_id ? <span className="ml-auto truncate text-xs text-slate-400">{step.agent_id}</span> : null}
          </li>
        ))}
        {workflow.step_count > preview.length ? (
          <li className="pl-7 text-xs text-slate-400">+ {workflow.step_count - preview.length} more</li>
        ) : null}
      </ol>

      <div className="mt-auto flex items-center justify-between gap-3 border-t border-slate-100 pt-3 text-xs">
        {workflow.latest_run ? (
          <span className="flex min-w-0 items-center gap-2">
            <StatusBadge status={workflow.latest_run.status} />
            <span className="truncate text-slate-500">{formatTime(workflow.latest_run.started_at || workflow.latest_run.created_at)}</span>
          </span>
        ) : workflow.history_available ? (
          <span className="text-slate-400">Never run</span>
        ) : (
          <span className="text-slate-400">Run history unavailable</span>
        )}
        <span className="shrink-0 text-slate-400">Open workflow →</span>
      </div>
    </Card>
  );
}

export default function Workflows() {
  const state = useApi(() => api.listWorkflows(), []);
  const [query, setQuery] = useState('');
  const [type, setType] = useState('all');

  const catalog = useMemo(
    () => workflowCatalogEntries(state.data?.workflows),
    [state.data],
  );
  const workflows = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return catalog.filter((workflow) => {
      const matchesQuery = !normalized ||
        workflow.workflow_id.toLowerCase().includes(normalized) ||
        workflow.display_name.toLowerCase().includes(normalized);
      const matchesType = type === 'all' || workflowType(workflow) === type;
      return matchesQuery && matchesType;
    });
  }, [catalog, query, type]);

  return (
    <div>
      <PageHeader
        title="Workflows"
        description="Durable step pipelines registered in this deployment."
      />
      <div className="mb-4 flex flex-wrap gap-3">
        <div className="w-full max-w-sm">
          <TextInput
            aria-label="Search workflows"
            placeholder="Search by workflow id or name"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <div className="w-48">
          <Select aria-label="Workflow type" value={type} onChange={(event) => setType(event.target.value)}>
            {Object.entries(TYPE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </Select>
        </div>
      </div>
      <Async state={state}>
        {() => !catalog.length ? (
          <Empty>No step pipelines are registered.</Empty>
        ) : workflows.length ? (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {workflows.map((workflow) => <WorkflowCard key={workflow.workflow_id} workflow={workflow} />)}
          </div>
        ) : <Empty>No workflows match those filters.</Empty>}
      </Async>
    </div>
  );
}
