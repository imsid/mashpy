import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Table } from '../components/Table.jsx';
import { Drawer } from '../components/Drawer.jsx';
import { Markdown } from '../components/Markdown.jsx';
import { CopyId } from '../components/CopyId.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { formatIso } from '../lib/format.js';

function statusTone(status) {
  if (status === 'completed') return 'emerald';
  if (status === 'failed' || status === 'error' || status === 'cancelled') return 'rose';
  if (status === 'running') return 'indigo';
  return 'amber';
}

function scoreTone(score, min = 1, max = 5) {
  const mid = (min + max) / 2;
  if (score >= mid + (max - mid) / 2) return 'emerald';
  if (score < mid - (mid - min) / 2) return 'rose';
  return 'amber';
}

function ScoreBar({ score, min = 1, max = 5 }) {
  const pct = Math.max(0, Math.min(1, (score - min) / (max - min)));
  const tone = scoreTone(score, min, max);
  const barClass = {
    emerald: 'bg-emerald-400',
    amber: 'bg-amber-400',
    rose: 'bg-rose-400',
  }[tone];
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-slate-100">
        <div className={`h-full rounded-full ${barClass}`} style={{ width: `${pct * 100}%` }} />
      </div>
      <span className={`text-xs font-medium text-${tone}-700`}>{score.toFixed(2)}</span>
    </div>
  );
}

function AggregateCard({ aggregate }) {
  if (!aggregate) return null;
  const { mean_score, by_criterion, run_count, scored_count } = aggregate;

  return (
    <Card className="mb-5 p-4">
      <div className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-400">
        Aggregate scores
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        <div>
          <div className="text-xs text-slate-500">Mean score</div>
          {mean_score != null ? (
            <div className="mt-1">
              <ScoreBar score={mean_score} />
            </div>
          ) : (
            <span className="text-sm text-slate-400">—</span>
          )}
        </div>
        <div>
          <div className="text-xs text-slate-500">Runs</div>
          <div className="mt-0.5 text-sm font-medium text-slate-700">
            {scored_count ?? '—'} / {run_count ?? '—'} scored
          </div>
        </div>
        {by_criterion
          ? Object.entries(by_criterion).map(([name, score]) => (
              <div key={name}>
                <div className="truncate text-xs text-slate-500" title={name}>
                  {name}
                </div>
                <div className="mt-1">
                  <ScoreBar score={score} />
                </div>
              </div>
            ))
          : null}
      </div>
    </Card>
  );
}

function Stat({ label, value }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-0.5 tabular-nums text-sm font-medium text-slate-700">{value}</div>
    </div>
  );
}

function OperationalCard({ operational }) {
  const hasMetrics = operational?.row_count > 0;
  return (
    <Card className="mb-5 p-4">
      <div className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-400">
        Operational
      </div>
      {hasMetrics ? (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          <Stat
            label="Mean latency"
            value={
              operational.mean_latency_ms != null
                ? `${(operational.mean_latency_ms / 1000).toFixed(1)}s`
                : '—'
            }
          />
          <Stat label="Mean steps / row" value={operational.mean_steps ?? '—'} />
          <Stat
            label="Tokens (in / out)"
            value={`${(operational.total_tokens?.input ?? 0).toLocaleString()} / ${(operational.total_tokens?.output ?? 0).toLocaleString()}`}
          />
          <Stat
            label="Cached (read / write)"
            value={`${(operational.total_tokens?.cache_read ?? 0).toLocaleString()} / ${(operational.total_tokens?.cache_creation ?? 0).toLocaleString()}`}
          />
          <Stat label="LLM calls" value={operational.total_llm_calls ?? 0} />
          <Stat label="Tool calls" value={operational.total_tool_calls ?? 0} />
          <Stat label="Subagent steps" value={operational.total_subagent_steps ?? 0} />
        </div>
      ) : (
        <p className="text-sm text-slate-400">
          No operational metrics on this experiment's runs.
        </p>
      )}
    </Card>
  );
}

function SnapshotSection({ experiment }) {
  const composition = experiment?.host_composition || {};
  const snapshot = experiment?.agent_spec_snapshot || {};
  const agents = [composition.primary, ...(composition.subagents || [])].filter(Boolean);
  const agentIds = agents.length ? agents : Object.keys(snapshot);
  if (!agentIds.length) return null;
  return (
    <Card className="mb-5 p-4">
      <div className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
        Host snapshot at run start
      </div>
      <div className="space-y-2">
        {agentIds.map((agentId) => {
          const spec = snapshot[agentId] || {};
          return (
            <div key={agentId} className="flex flex-wrap items-center gap-2 text-sm">
              <Chip tone="indigo">{agentId}</Chip>
              {agentId === composition.primary ? <Chip>primary</Chip> : null}
              {spec.model ? <Chip tone="slate">{spec.model}</Chip> : null}
              {spec.tools?.length ? (
                <span className="text-xs text-slate-400">
                  {spec.tools.length} tool{spec.tools.length !== 1 ? 's' : ''}
                </span>
              ) : null}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function Section({ title, children }) {
  return (
    <div>
      <div className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
        {title}
      </div>
      {children}
    </div>
  );
}

function RunDrawer({ run, onClose }) {
  return (
    <Drawer open={!!run} onClose={onClose} title="Run result" subtitle={run?.row_id}>
      {run ? (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center gap-3">
            {run.weighted_score != null ? (
              <ScoreBar score={run.weighted_score} />
            ) : (
              <Chip tone="rose">not scored</Chip>
            )}
            {run.session_id ? (
              <Link
                to={`/logs?tab=sessions&session=${encodeURIComponent(run.session_id)}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-indigo-600 underline"
              >
                View session log ↗
              </Link>
            ) : null}
          </div>

          {run.scores && Object.keys(run.scores).length ? (
            <Section title="Criteria">
              <div className="space-y-2">
                {Object.entries(run.scores).map(([name, cs]) => (
                  <div key={name} className="rounded-md border border-slate-200 p-2.5">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium text-slate-700">{name}</span>
                      <span className="tabular-nums text-sm text-slate-600">{cs.score}</span>
                    </div>
                    {cs.rationale ? (
                      <p className="mt-1 text-xs leading-relaxed text-slate-500">{cs.rationale}</p>
                    ) : null}
                  </div>
                ))}
              </div>
            </Section>
          ) : null}

          {run.metrics ? (
            <Section title="Metrics">
              <div className="grid grid-cols-2 gap-3 rounded-md border border-slate-200 p-3 sm:grid-cols-3">
                <Stat
                  label="Latency"
                  value={
                    run.metrics.latency_ms != null
                      ? `${(run.metrics.latency_ms / 1000).toFixed(1)}s`
                      : '—'
                  }
                />
                <Stat label="Steps" value={run.metrics.steps ?? 0} />
                <Stat label="LLM calls" value={run.metrics.llm_calls ?? 0} />
                <Stat label="Tool calls" value={run.metrics.tool_calls ?? 0} />
                <Stat
                  label="Tokens (in / out)"
                  value={`${(run.metrics.tokens?.input ?? 0).toLocaleString()} / ${(run.metrics.tokens?.output ?? 0).toLocaleString()}`}
                />
                <Stat
                  label="Cached (read / write)"
                  value={`${(run.metrics.tokens?.cache_read ?? 0).toLocaleString()} / ${(run.metrics.tokens?.cache_creation ?? 0).toLocaleString()}`}
                />
                <Stat label="Subagent steps" value={run.metrics.num_subagent_steps ?? 0} />
              </div>
            </Section>
          ) : null}

          <Section title="Input">
            <div className="rounded-md border border-slate-200 bg-slate-50 p-3">
              <Markdown>{run.input}</Markdown>
            </div>
          </Section>

          <Section title="Output">
            {run.actual_output ? (
              <div className="rounded-md border border-slate-200 p-3">
                <Markdown>{run.actual_output}</Markdown>
              </div>
            ) : (
              <p className="text-sm text-slate-400">No output produced.</p>
            )}
          </Section>

          {run.session_id ? (
            <Section title="Session">
              <CopyId value={run.session_id} />
            </Section>
          ) : null}
        </div>
      ) : null}
    </Drawer>
  );
}

function RunsTable({ evalId, experimentId }) {
  const state = useApi(() => api.listRuns(evalId, experimentId), [evalId, experimentId]);
  const [selected, setSelected] = useState(null);
  const runs = state.data?.runs || [];

  if (state.loading && !state.data) return null;
  if (!runs.length) return <Empty>No run results yet.</Empty>;

  const criteriaNames = runs.length
    ? Object.keys(runs.find((r) => r.scores && Object.keys(r.scores).length)?.scores || {})
    : [];

  const columns = [
    {
      key: 'input',
      header: 'Input',
      render: (r) => (
        <span className="block max-w-xs truncate text-slate-700" title={r.input}>
          {r.input}
        </span>
      ),
    },
    {
      key: 'actual_output',
      header: 'Output',
      render: (r) =>
        r.actual_output ? (
          <span className="block max-w-xs truncate text-slate-500" title={r.actual_output}>
            {r.actual_output}
          </span>
        ) : (
          <span className="text-slate-300">—</span>
        ),
    },
    {
      key: 'weighted_score',
      header: 'Score',
      align: 'right',
      render: (r) =>
        r.weighted_score != null ? (
          <ScoreBar score={r.weighted_score} />
        ) : (
          <span className="text-slate-300">—</span>
        ),
    },
    ...criteriaNames.map((name) => ({
      key: `criterion_${name}`,
      header: name,
      align: 'right',
      render: (r) => {
        const cs = r.scores?.[name];
        if (!cs) return <span className="text-slate-300">—</span>;
        return (
          <span title={cs.rationale} className="cursor-help tabular-nums text-slate-600">
            {cs.score}
          </span>
        );
      },
    })),
  ];

  return (
    <>
      <Table
        columns={columns}
        rows={runs}
        getRowKey={(r) => r.run_id}
        onRowClick={setSelected}
        activeKey={selected?.run_id}
      />
      <RunDrawer run={selected} onClose={() => setSelected(null)} />
    </>
  );
}

export default function ExperimentDetail() {
  const { evalId, experimentId } = useParams();

  const state = useApi(
    () => api.getExperiment(evalId, experimentId),
    [evalId, experimentId],
  );

  const experiment = state.data?.experiment;
  const aggregate = state.data?.aggregate;

  return (
    <div>
      <div className="mb-5 flex items-center gap-2 text-sm text-slate-500">
        <Link to="/evals" className="hover:text-slate-700 hover:underline">
          Evals
        </Link>
        <span>›</span>
        <Link
          to={`/evals/${encodeURIComponent(evalId)}?tab=experiments`}
          className="hover:text-slate-700 hover:underline"
        >
          <Mono>{evalId}</Mono>
        </Link>
        <span>›</span>
        <Mono>{experimentId}</Mono>
      </div>

      <Async state={state}>
        {() => (
          <>
            <PageHeader
              title="Experiment"
              description={
                <span className="flex items-center gap-2">
                  <Chip tone={statusTone(experiment?.status)}>{experiment?.status}</Chip>
                  <span className="text-slate-400">{formatIso(experiment?.created_at)}</span>
                  {experiment?.completed_at ? (
                    <span className="text-slate-400">
                      → {formatIso(experiment.completed_at)}
                    </span>
                  ) : null}
                </span>
              }
            />

            <SnapshotSection experiment={experiment} />
            <AggregateCard aggregate={aggregate} />
            <OperationalCard operational={aggregate?.operational} />

            <div className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-400">
              Run results
            </div>
            <RunsTable evalId={evalId} experimentId={experimentId} />
          </>
        )}
      </Async>
    </div>
  );
}
