import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Drawer } from '../components/Drawer.jsx';
import { Button, Field, TextArea } from '../components/Form.jsx';
import { Table } from '../components/Table.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { formatIso } from '../lib/format.js';

const DONE_STATUSES = new Set(['completed', 'failed', 'cancelled', 'error']);
const TABS = ['dataset', 'rubric', 'experiments'];

function statusTone(status) {
  if (status === 'completed') return 'emerald';
  if (status === 'failed' || status === 'error' || status === 'cancelled') return 'rose';
  if (status === 'running') return 'indigo';
  return 'amber';
}

// ---- Dataset Tab ----

const CATEGORY_LABELS = {
  random: 'random',
  multi_tool: 'multi-tool',
  multi_agent: 'multi-agent',
  high_tokens: 'high-tokens',
  long_running: 'long-running',
  short_running: 'short-running',
};

const ROW_COLUMNS = [
  {
    key: 'sampling_category',
    header: 'Category',
    render: (r) => <Chip>{CATEGORY_LABELS[r.sampling_category] || r.sampling_category}</Chip>,
  },
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
    key: 'scenario_description',
    header: 'Scenario',
    render: (r) => (
      <span className="block max-w-xs truncate text-slate-500" title={r.scenario_description}>
        {r.scenario_description}
      </span>
    ),
  },
  {
    key: 'target_agents',
    header: 'Agents',
    render: (r) => (
      <div className="flex flex-wrap gap-1">
        {(r.target_agents || []).map((a) => (
          <Chip key={a} tone="indigo">
            {a}
          </Chip>
        ))}
      </div>
    ),
  },
];

function DatasetTab({ rows }) {
  if (!rows?.length) return <Empty>No dataset rows.</Empty>;
  return <Table columns={ROW_COLUMNS} rows={rows} getRowKey={(r) => r.row_id} />;
}

// ---- Rubric Tab ----

function RubricTab({ evalId, rubric, onUpdated }) {
  const [editing, setEditing] = useState(false);
  const [criteria, setCriteria] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  function startEdit() {
    setCriteria(rubric.criteria.map((c) => ({ ...c, weight: String(c.weight) })));
    setEditing(true);
  }

  function cancel() {
    setEditing(false);
    setSaveError(null);
  }

  async function handleSave(e) {
    e.preventDefault();
    setSaveError(null);
    setSaving(true);
    try {
      const parsed = criteria.map((c) => ({ ...c, weight: parseFloat(c.weight) }));
      await api.updateRubric(evalId, { criteria: parsed });
      setEditing(false);
      onUpdated();
    } catch (err) {
      setSaveError(err.message || 'Save failed.');
    } finally {
      setSaving(false);
    }
  }

  if (!rubric) return <Empty>No rubric.</Empty>;

  return (
    <div className="space-y-5">
      {rubric.global_scoring_prompt ? (
        <Card className="p-4">
          <div className="mb-1.5 text-xs font-medium uppercase tracking-wide text-slate-400">
            Global scoring prompt
          </div>
          <p className="whitespace-pre-wrap text-sm text-slate-700">{rubric.global_scoring_prompt}</p>
        </Card>
      ) : null}

      <div>
        <div className="mb-3 flex items-center justify-between">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-400">
            Criteria ({rubric.criteria?.length || 0})
          </span>
          {!editing ? (
            <Button variant="secondary" onClick={startEdit}>
              Edit weights
            </Button>
          ) : null}
        </div>

        {editing ? (
          <form onSubmit={handleSave} className="space-y-3">
            {criteria.map((c, idx) => (
              <Card key={c.name} className="p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1">
                    <div className="font-medium text-slate-800">{c.name}</div>
                    <p className="mt-0.5 text-sm text-slate-500">{c.description}</p>
                    <p className="mt-1 text-xs text-slate-400 italic">{c.scoring_prompt}</p>
                    <div className="mt-1 text-xs text-slate-400">
                      Scale: {c.scale_min}–{c.scale_max}
                    </div>
                  </div>
                  <label className="block shrink-0">
                    <span className="mb-1 block text-xs font-medium text-slate-600">Weight</span>
                    <input
                      type="number"
                      step="0.01"
                      min="0"
                      max="1"
                      value={c.weight}
                      onChange={(e) => {
                        const next = [...criteria];
                        next[idx] = { ...next[idx], weight: e.target.value };
                        setCriteria(next);
                      }}
                      className="w-24 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm shadow-sm focus:border-slate-400 focus:outline-none focus:ring-1 focus:ring-slate-300"
                    />
                  </label>
                </div>
              </Card>
            ))}
            {saveError ? <p className="text-sm text-rose-600">{saveError}</p> : null}
            <div className="flex gap-2">
              <Button type="submit" variant="primary" disabled={saving}>
                {saving ? 'Saving…' : 'Save'}
              </Button>
              <Button type="button" variant="secondary" onClick={cancel}>
                Cancel
              </Button>
            </div>
          </form>
        ) : (
          <div className="space-y-2">
            {rubric.criteria?.map((c) => (
              <Card key={c.name} className="flex items-start gap-4 p-4">
                <div className="flex-1">
                  <div className="font-medium text-slate-800">{c.name}</div>
                  <p className="mt-0.5 text-sm text-slate-500">{c.description}</p>
                  <p className="mt-1 text-xs text-slate-400 italic">{c.scoring_prompt}</p>
                </div>
                <div className="shrink-0 text-right">
                  <div className="text-sm font-semibold text-slate-700">
                    {(c.weight * 100).toFixed(0)}%
                  </div>
                  <div className="text-xs text-slate-400">
                    {c.scale_min}–{c.scale_max}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ---- Experiments Tab ----

function RunExperimentDrawer({ open, onClose, evalId, onDone }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!job || DONE_STATUSES.has(job.status)) return;
    const timer = setTimeout(async () => {
      try {
        const run = await api.getWorkflowRun('score-evals', job.runId);
        setJob((j) => ({ ...j, status: run.status, jobError: run.error }));
        if (run.status === 'completed') onDone();
      } catch (err) {
        setJob((j) => ({ ...j, status: 'error', jobError: err.message }));
      }
    }, 2000);
    return () => clearTimeout(timer);
  }, [job, onDone]);

  async function handleRun() {
    setError(null);
    setJob(null);
    try {
      const run = await api.runWorkflow('score-evals', { input: { eval_id: evalId } });
      setJob({ runId: run.run_id, status: run.status });
    } catch (err) {
      setError(err.message || 'Failed to start workflow.');
    }
  }

  function handleClose() {
    setJob(null);
    setError(null);
    onClose();
  }

  const running = job && !DONE_STATUSES.has(job.status);
  const done = job?.status === 'completed';
  const failed = job && !running && !done;

  return (
    <Drawer
      open={open}
      onClose={handleClose}
      title="Run Experiment"
      subtitle={`Scores eval ${evalId} with the current agent configuration.`}
      footer={
        !job ? (
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={handleClose}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleRun}>
              Run
            </Button>
          </div>
        ) : done ? (
          <div className="flex justify-end">
            <Button variant="primary" onClick={handleClose}>
              Done
            </Button>
          </div>
        ) : null
      }
    >
      {!job ? (
        <div className="space-y-3">
          <p className="text-sm text-slate-600">
            This will score all dataset rows using the current rubric and agent spec, creating a new
            experiment with run results.
          </p>
          {error ? <p className="text-sm text-rose-600">{error}</p> : null}
        </div>
      ) : (
        <div className="space-y-3 py-6 text-center">
          <Chip tone={statusTone(job.status)} className="text-sm">
            {job.status}
          </Chip>
          {running ? (
            <p className="text-sm text-slate-500">Experiment running — checking status…</p>
          ) : done ? (
            <p className="text-sm text-emerald-700">Experiment completed.</p>
          ) : failed ? (
            <div>
              <p className="text-sm text-rose-600">Workflow {job.status}.</p>
              {job.jobError ? (
                <p className="mt-1 text-xs text-slate-400">{job.jobError}</p>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
    </Drawer>
  );
}

const EXP_COLUMNS = [
  {
    key: 'experiment_id',
    header: 'Experiment',
    render: (r) => <Mono>{r.experiment_id}</Mono>,
  },
  {
    key: 'status',
    header: 'Status',
    render: (r) => <Chip tone={statusTone(r.status)}>{r.status}</Chip>,
  },
  {
    key: 'created_at',
    header: 'Created',
    render: (r) => <span className="text-slate-500">{formatIso(r.created_at)}</span>,
  },
  {
    key: 'completed_at',
    header: 'Completed',
    render: (r) => (
      <span className="text-slate-500">
        {r.completed_at ? formatIso(r.completed_at) : '—'}
      </span>
    ),
  },
  {
    key: 'delta',
    header: 'Changes',
    render: (r) => {
      const delta = r.agent_spec_delta || [];
      if (!delta.length) return <span className="text-slate-400 text-xs">baseline</span>;
      return (
        <div className="flex flex-wrap gap-1">
          {delta.map((d) => (
            <Chip key={d.agent_id} tone="indigo">
              {d.agent_id}
            </Chip>
          ))}
        </div>
      );
    },
  },
];

function ExperimentsTab({ evalId, experiments, onRunDone }) {
  const [runOpen, setRunOpen] = useState(false);
  const navigate = useNavigate();

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-400">
          {experiments?.length || 0} experiment{experiments?.length !== 1 ? 's' : ''}
        </span>
        <Button variant="primary" onClick={() => setRunOpen(true)}>
          Run experiment
        </Button>
      </div>

      {experiments?.length ? (
        <Table
          columns={EXP_COLUMNS}
          rows={experiments}
          getRowKey={(r) => r.experiment_id}
          onRowClick={(r) =>
            navigate(`/evals/${encodeURIComponent(evalId)}/experiments/${encodeURIComponent(r.experiment_id)}`)
          }
        />
      ) : (
        <Empty>No experiments yet. Click Run experiment to score this eval.</Empty>
      )}

      <RunExperimentDrawer
        open={runOpen}
        onClose={() => setRunOpen(false)}
        evalId={evalId}
        onDone={() => {
          setRunOpen(false);
          onRunDone();
        }}
      />
    </div>
  );
}

// ---- Main EvalDetail ----

export default function EvalDetail() {
  const { evalId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = TABS.includes(searchParams.get('tab')) ? searchParams.get('tab') : 'dataset';

  const state = useApi(() => api.getEval(evalId), [evalId]);
  const experimentsState = useApi(
    () => api.listExperiments(evalId),
    [evalId],
  );

  const data = state.data;
  const eval_ = data?.eval;
  const rows = data?.rows || [];
  const rubric = data?.rubric;
  const experiments = experimentsState.data?.experiments || [];

  function setTab(tab) {
    setSearchParams({ tab }, { replace: true });
  }

  return (
    <div>
      <div className="mb-5 flex items-center gap-2 text-sm text-slate-500">
        <Link to="/evals" className="hover:text-slate-700 hover:underline">
          Evals
        </Link>
        <span>›</span>
        <Mono>{evalId}</Mono>
      </div>

      <Async state={state}>
        {() => (
          <>
            <PageHeader
              title="Eval Detail"
              description={
                <span className="flex items-center gap-2">
                  <Chip>{eval_?.host_id}</Chip>
                  <span className="text-slate-400">{formatIso(eval_?.created_at)}</span>
                </span>
              }
            />

            {eval_?.user_guidance ? (
              <Card className="mb-5 p-4">
                <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">
                  Guidance
                </div>
                <p className="text-sm text-slate-700">{eval_.user_guidance}</p>
              </Card>
            ) : null}

            <div className="mb-5 flex gap-1 border-b border-slate-200">
              {TABS.map((tab) => (
                <button
                  key={tab}
                  onClick={() => setTab(tab)}
                  className={`px-4 pb-2 text-sm font-medium capitalize transition ${
                    activeTab === tab
                      ? 'border-b-2 border-slate-900 text-slate-900'
                      : 'text-slate-500 hover:text-slate-700'
                  }`}
                >
                  {tab}
                  {tab === 'dataset' && rows.length ? (
                    <span className="ml-1.5 text-xs text-slate-400">({rows.length})</span>
                  ) : null}
                  {tab === 'experiments' && experiments.length ? (
                    <span className="ml-1.5 text-xs text-slate-400">({experiments.length})</span>
                  ) : null}
                </button>
              ))}
            </div>

            {activeTab === 'dataset' && <DatasetTab rows={rows} />}
            {activeTab === 'rubric' && (
              <RubricTab evalId={evalId} rubric={rubric} onUpdated={state.reload} />
            )}
            {activeTab === 'experiments' && (
              <ExperimentsTab
                evalId={evalId}
                experiments={experiments}
                onRunDone={experimentsState.reload}
              />
            )}
          </>
        )}
      </Async>
    </div>
  );
}
