import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';

import { PageHeader, Card } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { Button } from '../components/Form.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Disclosure, JsonBlock } from '../components/Json.jsx';
import { RunWorkflowDrawer } from '../components/workflows/RunWorkflowDrawer.jsx';
import {
  Breadcrumbs,
  Pipeline,
  RunTable,
  SchemaSummary,
} from '../components/workflows/WorkflowUI.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

export default function WorkflowDetail() {
  const { workflowId } = useParams();
  const navigate = useNavigate();
  const [runOpen, setRunOpen] = useState(false);
  const definitionState = useApi(() => api.getWorkflow(workflowId), [workflowId]);
  const runsState = useApi(
    () => api.listWorkflowRuns(workflowId, { limit: 5, sort_desc: true }),
    [workflowId],
  );

  return (
    <div>
      <Breadcrumbs items={[{ label: 'Workflows', to: '/workflows' }, { label: workflowId }]} />
      <Async state={definitionState}>
        {(definition) => {
          const metadata = definition.metadata || {};
          const displayName = metadata.display_name || definition.workflow_id;
          return (
            <>
              <PageHeader
                title={displayName}
                description={metadata.description}
                actions={<Button variant="primary" onClick={() => setRunOpen(true)}>Run workflow</Button>}
              />
              <div className="space-y-6">
                <Card className="p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <Chip tone="indigo">pipeline</Chip>
                    <Mono>{definition.workflow_id}</Mono>
                    <span className="text-sm text-slate-500">
                      {definition.steps.length} step{definition.steps.length === 1 ? '' : 's'} ·{' '}
                      {definition.steps.filter((step) => step.kind === 'code').length} code ·{' '}
                      {definition.steps.filter((step) => step.kind === 'agent').length} agent
                    </span>
                  </div>
                  {Object.keys(metadata).length ? (
                    <div className="mt-3">
                      <Disclosure label="Raw metadata">
                        <JsonBlock value={metadata} />
                      </Disclosure>
                    </div>
                  ) : null}
                </Card>

                <SchemaSummary schema={definition.input_schema} title="Workflow input" />

                <section>
                  <h2 className="mb-3 font-display text-base font-semibold">Pipeline</h2>
                  <Pipeline definition={definition} />
                </section>

                <section>
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <h2 className="font-display text-base font-semibold">Recent runs</h2>
                    <Link
                      to={`/workflows/${encodeURIComponent(definition.workflow_id)}/runs`}
                      className="text-sm font-medium text-indigo-600 hover:underline"
                    >
                      View all runs →
                    </Link>
                  </div>
                  <Async state={runsState}>
                    {(data) => data.runs?.length ? (
                      <RunTable
                        runs={data.runs}
                        onSelect={(run) => navigate(
                          `/workflows/${encodeURIComponent(definition.workflow_id)}/runs/${encodeURIComponent(run.run_id)}`,
                        )}
                      />
                    ) : <Empty>This workflow has not run yet.</Empty>}
                  </Async>
                </section>
              </div>

              <RunWorkflowDrawer
                definition={definition}
                open={runOpen}
                onClose={() => setRunOpen(false)}
                onStarted={(run) => navigate(
                  `/workflows/${encodeURIComponent(definition.workflow_id)}/runs/${encodeURIComponent(run.run_id)}`,
                )}
              />
            </>
          );
        }}
      </Async>
    </div>
  );
}
