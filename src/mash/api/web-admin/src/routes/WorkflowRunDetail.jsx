import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Button } from '../components/Form.jsx';
import { Chip } from '../components/Chip.jsx';
import { CopyId } from '../components/CopyId.jsx';
import { JsonBlock } from '../components/Json.jsx';
import { RunWorkflowDrawer } from '../components/workflows/RunWorkflowDrawer.jsx';
import { StepDetailDrawer } from '../components/workflows/StepDetailDrawer.jsx';
import {
  Breadcrumbs,
  Pipeline,
  StatusBadge,
} from '../components/workflows/WorkflowUI.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { formatTime } from '../lib/format.js';
import { durationSeconds, mergeWorkflowSteps, TERMINAL_RUN_STATUSES } from '../lib/workflow.js';

function Duration({ run }) {
  const seconds = durationSeconds(run.started_at, run.finished_at);
  if (seconds === null) return <span>—</span>;
  return <span>{seconds < 1 ? `${Math.round(seconds * 1000)}ms` : `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`}</span>;
}

export default function WorkflowRunDetail() {
  const { workflowId, runId } = useParams();
  const navigate = useNavigate();
  const definitionState = useApi(() => api.getWorkflow(workflowId), [workflowId]);
  const runState = useApi(() => api.getWorkflowRun(workflowId, runId), [workflowId, runId]);
  const eventsState = useApi(() => api.listWorkflowStepEvents(workflowId, runId), [workflowId, runId]);
  const [selectedStepId, setSelectedStepId] = useState(null);
  const [runAgainOpen, setRunAgainOpen] = useState(false);
  const [connection, setConnection] = useState('idle');
  const [actionError, setActionError] = useState(null);
  const [resuming, setResuming] = useState(false);
  const [resumePending, setResumePending] = useState(false);

  const terminal = TERMINAL_RUN_STATUSES.has(runState.data?.status);
  const shouldWatch = Boolean(runState.data) && (!terminal || resumePending);

  useEffect(() => {
    if (!shouldWatch) {
      setConnection(terminal ? 'complete' : 'idle');
      return undefined;
    }
    setConnection('connecting');
    return api.subscribeWorkflowRun(workflowId, runId, {
      onOpen: () => setConnection('live'),
      onError: () => setConnection('reconnecting'),
      onEvent: ({ event, data }) => {
        runState.reload();
        eventsState.reload();
        if (!resumePending && (event === 'workflow.completed' ||
          (event === 'workflow.error' && TERMINAL_RUN_STATUSES.has(data.status)))) {
          setConnection('complete');
        } else if (event === 'workflow.error') {
          setConnection('reconnecting');
        }
      },
    });
  }, [workflowId, runId, runState.data?.status, shouldWatch, terminal, resumePending]);

  useEffect(() => {
    if (connection !== 'reconnecting' || !shouldWatch) return undefined;
    const timer = window.setInterval(() => {
      runState.reload();
      eventsState.reload();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [connection, shouldWatch, runState.reload, eventsState.reload]);

  useEffect(() => {
    if (resumePending && runState.data?.status !== 'failed') setResumePending(false);
  }, [resumePending, runState.data?.status]);

  const mergedSteps = useMemo(
    () => mergeWorkflowSteps(definitionState.data?.steps, runState.data?.steps || []),
    [definitionState.data?.steps, runState.data?.steps],
  );
  const selectedStep = mergedSteps.find((step) => step.step_id === selectedStepId) || null;
  const selectedEvents = (eventsState.data?.events || []).filter(
    (event) => event.step_id === selectedStepId,
  );

  async function resumeRun() {
    if (!window.confirm('Resume this run under the same run ID from its failed step?')) return;
    setActionError(null);
    setResuming(true);
    try {
      await api.resumeWorkflowRun(workflowId, runId);
      setResumePending(true);
      setConnection('reconnecting');
      runState.reload();
      eventsState.reload();
    } catch (error) {
      setActionError(error.message || 'Failed to resume run.');
    } finally {
      setResuming(false);
    }
  }

  return (
    <div>
      <Breadcrumbs items={[
        { label: 'Workflows', to: '/workflows' },
        { label: workflowId, to: `/workflows/${encodeURIComponent(workflowId)}` },
        { label: 'Runs', to: `/workflows/${encodeURIComponent(workflowId)}/runs` },
        { label: runId },
      ]} />
      <Async state={runState}>
        {(run) => (
          <Async state={definitionState}>
            {(definition) => (
              <>
                <PageHeader
                  title="Workflow run"
                  description={definition.metadata?.display_name || workflowId}
                  actions={
                    <>
                      {run.status === 'failed' ? (
                        <Button variant="secondary" onClick={resumeRun} disabled={resuming}>
                          {resuming ? 'Resuming…' : 'Resume run'}
                        </Button>
                      ) : null}
                      {TERMINAL_RUN_STATUSES.has(run.status) ? (
                        <Button variant="primary" onClick={() => setRunAgainOpen(true)}>Run again</Button>
                      ) : null}
                    </>
                  }
                />

                {actionError ? (
                  <p className="mb-4 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{actionError}</p>
                ) : null}

                <Card className="mb-6 p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge status={run.status} />
                    <CopyId value={run.run_id} />
                    {!terminal ? (
                      <Chip tone={connection === 'live' ? 'emerald' : 'amber'}>{connection}</Chip>
                    ) : null}
                  </div>
                  <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                    <div><dt className="text-xs text-slate-400">Created</dt><dd>{formatTime(run.created_at)}</dd></div>
                    <div><dt className="text-xs text-slate-400">Started</dt><dd>{formatTime(run.started_at)}</dd></div>
                    <div><dt className="text-xs text-slate-400">Finished</dt><dd>{formatTime(run.finished_at)}</dd></div>
                    <div><dt className="text-xs text-slate-400">Duration</dt><dd><Duration run={run} /></dd></div>
                    {run.dedup_key ? <div><dt className="text-xs text-slate-400">Dedup key</dt><dd>{run.dedup_key}</dd></div> : null}
                    {run.session_id ? <div><dt className="text-xs text-slate-400">Session</dt><dd><CopyId value={run.session_id} /></dd></div> : null}
                  </dl>
                </Card>

                {run.error ? (
                  <section className="mb-6 rounded-md border border-rose-200 bg-rose-50 p-4">
                    <h2 className="text-xs font-medium uppercase tracking-wide text-rose-500">Workflow error</h2>
                    <p className="mt-1 whitespace-pre-wrap text-sm text-rose-700">{run.error}</p>
                  </section>
                ) : null}

                <div className="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <section>
                    <h2 className="mb-2 font-display text-sm font-semibold">Workflow input</h2>
                    {run.workflow_input !== null && run.workflow_input !== undefined ? (
                      <JsonBlock value={run.workflow_input} />
                    ) : <p className="rounded-md border border-dashed border-slate-200 p-4 text-sm text-slate-400">Waiting for the run store record.</p>}
                  </section>
                  <section>
                    <h2 className="mb-2 font-display text-sm font-semibold">Result</h2>
                    {run.result !== null && run.result !== undefined ? (
                      <JsonBlock value={run.result} />
                    ) : <p className="rounded-md border border-dashed border-slate-200 p-4 text-sm text-slate-400">No result recorded.</p>}
                  </section>
                </div>

                <section>
                  <h2 className="mb-3 font-display text-base font-semibold">Steps</h2>
                  <Pipeline definition={definition} steps={mergedSteps} onStepClick={(step) => setSelectedStepId(step.step_id)} />
                </section>

                <StepDetailDrawer
                  step={selectedStep}
                  events={selectedEvents}
                  workflowId={workflowId}
                  sessionId={run.session_id}
                  onClose={() => setSelectedStepId(null)}
                />
                <RunWorkflowDrawer
                  definition={definition}
                  open={runAgainOpen}
                  initialInput={run.workflow_input}
                  onClose={() => setRunAgainOpen(false)}
                  onStarted={(newRun) => {
                    navigate(
                      `/workflows/${encodeURIComponent(workflowId)}/runs/${encodeURIComponent(newRun.run_id)}`,
                    );
                  }}
                />
              </>
            )}
          </Async>
        )}
      </Async>
    </div>
  );
}
