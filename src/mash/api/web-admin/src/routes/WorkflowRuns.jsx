import { useNavigate, useParams, useSearchParams } from 'react-router-dom';

import { PageHeader } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { Button, Field, Select, TextInput } from '../components/Form.jsx';
import { Breadcrumbs, RunTable } from '../components/workflows/WorkflowUI.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

const PAGE_SIZE = 50;

export default function WorkflowRuns() {
  const { workflowId } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get('status') || '';
  const startTime = params.get('start') || '';
  const endTime = params.get('end') || '';
  const sortDesc = params.get('sort') !== 'oldest';
  const offset = Math.max(0, Number.parseInt(params.get('offset') || '0', 10) || 0);

  const state = useApi(
    () => api.listWorkflowRuns(workflowId, {
      status: status || undefined,
      start_time: startTime || undefined,
      end_time: endTime || undefined,
      limit: PAGE_SIZE,
      offset,
      sort_desc: sortDesc,
    }),
    [workflowId, status, startTime, endTime, offset, sortDesc],
  );

  function update(next) {
    const merged = new URLSearchParams(params);
    for (const [key, value] of Object.entries(next)) {
      if (value !== undefined && value !== null && value !== '') merged.set(key, String(value));
      else merged.delete(key);
    }
    if (!('offset' in next)) merged.delete('offset');
    setParams(merged, { replace: true });
  }

  return (
    <div>
      <Breadcrumbs items={[
        { label: 'Workflows', to: '/workflows' },
        { label: workflowId, to: `/workflows/${encodeURIComponent(workflowId)}` },
        { label: 'Runs' },
      ]} />
      <PageHeader title="Workflow runs" description={`Stored runs for ${workflowId}.`} />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <div className="w-44">
          <Field label="Status">
            <Select value={status} onChange={(event) => update({ status: event.target.value })}>
              <option value="">All</option>
              {['queued', 'running', 'completed', 'failed', 'cancelled'].map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </Select>
          </Field>
        </div>
        <div className="w-56">
          <Field label="Created after">
            <TextInput type="datetime-local" value={startTime} onChange={(event) => update({ start: event.target.value })} />
          </Field>
        </div>
        <div className="w-56">
          <Field label="Created before">
            <TextInput type="datetime-local" value={endTime} onChange={(event) => update({ end: event.target.value })} />
          </Field>
        </div>
        <div className="w-40">
          <Field label="Order">
            <Select value={sortDesc ? 'newest' : 'oldest'} onChange={(event) => update({ sort: event.target.value })}>
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
            </Select>
          </Field>
        </div>
        <Button variant="ghost" onClick={state.reload} disabled={state.loading}>↻ Refresh</Button>
      </div>

      <Async state={state}>
        {(data) => data.runs?.length ? (
          <>
            <RunTable
              runs={data.runs}
              onSelect={(run) => navigate(
                `/workflows/${encodeURIComponent(workflowId)}/runs/${encodeURIComponent(run.run_id)}`,
              )}
            />
            <div className="mt-4 flex items-center justify-between">
              <span className="text-xs text-slate-400">
                Showing {offset + 1}–{offset + data.runs.length}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  disabled={offset === 0}
                  onClick={() => update({ offset: Math.max(0, offset - PAGE_SIZE) })}
                >
                  Previous
                </Button>
                <Button
                  variant="secondary"
                  disabled={!data.has_more}
                  onClick={() => update({ offset: offset + PAGE_SIZE })}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        ) : <Empty>No runs match those filters.</Empty>}
      </Async>
    </div>
  );
}
