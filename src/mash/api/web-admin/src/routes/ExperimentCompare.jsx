import { Link, useParams, useSearchParams } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Select } from '../components/Form.jsx';
import { Table } from '../components/Table.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { formatIso } from '../lib/format.js';

function DeltaValue({ delta, decimals = 2 }) {
  if (delta == null) return <span className="text-slate-300">—</span>;
  const tone = delta > 0 ? 'text-emerald-700' : delta < 0 ? 'text-rose-700' : 'text-slate-500';
  const sign = delta > 0 ? '+' : '';
  return (
    <span className={`tabular-nums text-sm font-medium ${tone}`}>
      {sign}
      {delta.toFixed(decimals)}
    </span>
  );
}

function ScorePair({ baseline, control, decimals = 2 }) {
  const fmt = (v) => (v == null ? '—' : v.toFixed(decimals));
  return (
    <span className="tabular-nums text-sm text-slate-600">
      {fmt(baseline)} <span className="text-slate-300">→</span> {fmt(control)}
    </span>
  );
}

function AgentChangesCard({ delta }) {
  return (
    <Card className="mb-5 p-4">
      <div className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
        Agent changes (baseline → control)
      </div>
      {delta?.length ? (
        <div className="space-y-2">
          {delta.map((d) => (
            <div key={d.agent_id} className="flex flex-wrap items-center gap-2 text-sm">
              <Chip tone="indigo">{d.agent_id}</Chip>
              {d.change === 'added' ? <Chip tone="emerald">added</Chip> : null}
              {d.change === 'removed' ? <Chip tone="rose">removed</Chip> : null}
              {d.change === 'modified'
                ? Object.keys(d.fields || {}).map((field) => (
                    <Chip key={field} tone="amber">
                      {field} changed
                    </Chip>
                  ))
                : null}
            </div>
          ))}
        </div>
      ) : (
        <p className="text-sm text-slate-400">
          No agent changes between these experiments.
        </p>
      )}
    </Card>
  );
}

function ScoresCard({ baseline, control }) {
  const b = baseline?.aggregate || {};
  const c = control?.aggregate || {};
  const criteria = [
    ...new Set([...Object.keys(b.by_criterion || {}), ...Object.keys(c.by_criterion || {})]),
  ];
  const meanDelta =
    b.mean_score != null && c.mean_score != null ? c.mean_score - b.mean_score : null;

  return (
    <Card className="mb-5 p-4">
      <div className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-400">
        Scores
      </div>
      <div className="space-y-2">
        <div className="flex items-center justify-between border-b border-slate-100 pb-2">
          <span className="text-sm font-medium text-slate-700">Aggregate score</span>
          <span className="flex items-center gap-3">
            <ScorePair baseline={b.mean_score} control={c.mean_score} />
            <DeltaValue delta={meanDelta} />
          </span>
        </div>
        {criteria.map((name) => {
          const bv = b.by_criterion?.[name];
          const cv = c.by_criterion?.[name];
          const delta = bv != null && cv != null ? cv - bv : null;
          return (
            <div key={name} className="flex items-center justify-between">
              <span className="text-sm text-slate-600">{name}</span>
              <span className="flex items-center gap-3">
                <ScorePair baseline={bv} control={cv} />
                <DeltaValue delta={delta} />
              </span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function OperationalCard({ baseline, control }) {
  const b = baseline?.aggregate?.operational || {};
  const c = control?.aggregate?.operational || {};
  if (!b.row_count && !c.row_count) return null;

  const metrics = [
    {
      label: 'Mean latency (ms)',
      baseline: b.mean_latency_ms,
      control: c.mean_latency_ms,
      decimals: 0,
    },
    {
      label: 'Total tokens (in)',
      baseline: b.total_tokens?.input,
      control: c.total_tokens?.input,
      decimals: 0,
    },
    {
      label: 'Total tokens (out)',
      baseline: b.total_tokens?.output,
      control: c.total_tokens?.output,
      decimals: 0,
    },
    { label: 'LLM calls', baseline: b.total_llm_calls, control: c.total_llm_calls, decimals: 0 },
    {
      label: 'Tool calls',
      baseline: b.total_tool_calls,
      control: c.total_tool_calls,
      decimals: 0,
    },
    { label: 'Mean steps / row', baseline: b.mean_steps, control: c.mean_steps, decimals: 2 },
  ];

  return (
    <Card className="mb-5 p-4">
      <div className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-400">
        Operational
      </div>
      <div className="space-y-2">
        {metrics.map((m) => {
          const delta = m.baseline != null && m.control != null ? m.control - m.baseline : null;
          return (
            <div key={m.label} className="flex items-center justify-between">
              <span className="text-sm text-slate-600">{m.label}</span>
              <span className="flex items-center gap-3">
                <ScorePair baseline={m.baseline} control={m.control} decimals={m.decimals} />
                <DeltaValue delta={delta} decimals={m.decimals} />
              </span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

const ROW_COLUMNS = [
  {
    key: 'input',
    header: 'Input',
    render: (r) => (
      <span className="block max-w-md truncate text-slate-700" title={r.input}>
        {r.input}
      </span>
    ),
  },
  {
    key: 'baseline_score',
    header: 'Baseline',
    align: 'right',
    render: (r) =>
      r.baseline_score != null ? (
        <span className="tabular-nums text-slate-600">{r.baseline_score.toFixed(2)}</span>
      ) : (
        <span className="text-slate-300">—</span>
      ),
  },
  {
    key: 'control_score',
    header: 'Control',
    align: 'right',
    render: (r) =>
      r.control_score != null ? (
        <span className="tabular-nums text-slate-600">{r.control_score.toFixed(2)}</span>
      ) : (
        <span className="text-slate-300">—</span>
      ),
  },
  {
    key: 'delta',
    header: 'Δ',
    align: 'right',
    render: (r) => <DeltaValue delta={r.delta} />,
  },
];

export default function ExperimentCompare() {
  const { evalId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const baselineId = searchParams.get('baseline');
  const controlId = searchParams.get('control');

  const state = useApi(
    () => api.compareExperiments(evalId, baselineId, controlId),
    [evalId, baselineId, controlId],
  );
  const experimentsState = useApi(() => api.listExperiments(evalId), [evalId]);
  const experiments = experimentsState.data?.experiments || [];

  const data = state.data;

  function setSide(side, experimentId) {
    setSearchParams(
      { baseline: baselineId, control: controlId, [side]: experimentId },
      { replace: true },
    );
  }

  function sidePicker(side, value) {
    return (
      <label className="block">
        <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-400">
          {side}
        </span>
        <div className="w-80">
          <Select value={value || ''} onChange={(e) => setSide(side, e.target.value)}>
            {experiments.map((e) => (
              <option key={e.experiment_id} value={e.experiment_id}>
                {e.experiment_id} · {formatIso(e.created_at)}
              </option>
            ))}
          </Select>
        </div>
      </label>
    );
  }

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
        <span>compare</span>
      </div>

      <PageHeader title="Compare Experiments" />

      <div className="mb-5 flex flex-wrap items-end gap-4">
        {sidePicker('baseline', baselineId)}
        <span className="pb-2 text-slate-300">vs</span>
        {sidePicker('control', controlId)}
      </div>

      <Async state={state}>
        {() => (
          <>
            <AgentChangesCard delta={data?.agent_spec_delta} />
            <ScoresCard baseline={data?.baseline} control={data?.control} />
            <OperationalCard baseline={data?.baseline} control={data?.control} />

            <div className="mb-3 text-xs font-medium uppercase tracking-wide text-slate-400">
              Rows by score movement
            </div>
            {data?.rows?.length ? (
              <Table columns={ROW_COLUMNS} rows={data.rows} getRowKey={(r) => r.row_id} />
            ) : (
              <Empty>No paired rows to compare.</Empty>
            )}
          </>
        )}
      </Async>
    </div>
  );
}
