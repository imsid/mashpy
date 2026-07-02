import { Link, useParams } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Table } from '../components/Table.jsx';
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

function DeltaSection({ delta }) {
  if (!delta?.length) return null;
  return (
    <Card className="mb-5 p-4">
      <div className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
        Agent changes vs baseline
      </div>
      <div className="space-y-2">
        {delta.map((d) => (
          <div key={d.agent_id} className="flex flex-wrap items-start gap-2 text-sm">
            <Chip tone="indigo">{d.agent_id}</Chip>
            {d.system_prompt_changed ? <Chip tone="amber">prompt changed</Chip> : null}
            {d.llm_model_changed ? <Chip tone="amber">model changed</Chip> : null}
            {d.tools_added?.map((t) => (
              <Chip key={`+${t}`} tone="emerald">+{t}</Chip>
            ))}
            {d.tools_removed?.map((t) => (
              <Chip key={`-${t}`} tone="rose">-{t}</Chip>
            ))}
            {d.mcp_servers_added?.map((s) => (
              <Chip key={`mcp+${s}`} tone="emerald">+mcp:{s}</Chip>
            ))}
            {d.mcp_servers_removed?.map((s) => (
              <Chip key={`mcp-${s}`} tone="rose">-mcp:{s}</Chip>
            ))}
          </div>
        ))}
      </div>
    </Card>
  );
}

function RunsTable({ evalId, experimentId }) {
  const state = useApi(() => api.listRuns(evalId, experimentId), [evalId, experimentId]);
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

  return <Table columns={columns} rows={runs} getRowKey={(r) => r.run_id} />;
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

            <DeltaSection delta={experiment?.agent_spec_delta} />
            <AggregateCard aggregate={aggregate} />

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
