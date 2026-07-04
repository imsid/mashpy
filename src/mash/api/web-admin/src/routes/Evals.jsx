import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Drawer } from '../components/Drawer.jsx';
import { Button, Field, Select, TextArea, TextInput } from '../components/Form.jsx';
import { Table } from '../components/Table.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { formatIso } from '../lib/format.js';

const DONE_STATUSES = new Set(['completed', 'failed', 'cancelled', 'error']);

function statusTone(status) {
  if (status === 'completed') return 'emerald';
  if (status === 'failed' || status === 'error' || status === 'cancelled') return 'rose';
  return 'amber';
}

function GenerateDrawer({ open, onClose, hosts, onDone }) {
  const [hostId, setHostId] = useState('');
  const [guidance, setGuidance] = useState('');
  const [rowCount, setRowCount] = useState('20');
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);

  const resolvedHost = hostId || hosts[0]?.host_id || '';

  useEffect(() => {
    if (!job || DONE_STATUSES.has(job.status)) return;
    const timer = setTimeout(async () => {
      try {
        const run = await api.getWorkflowRun('gen-synthetic-evals', job.runId);
        setJob((j) => ({ ...j, status: run.status, jobError: run.error }));
        if (run.status === 'completed') onDone();
      } catch (err) {
        setJob((j) => ({ ...j, status: 'error', jobError: err.message }));
      }
    }, 2000);
    return () => clearTimeout(timer);
  }, [job, onDone]);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    const parsedRowCount = parseInt(rowCount, 10);
    if (!Number.isInteger(parsedRowCount) || parsedRowCount < 1 || parsedRowCount > 100) {
      setError('Rows must be a whole number between 1 and 100.');
      return;
    }
    setJob(null);
    try {
      const run = await api.runWorkflow('gen-synthetic-evals', {
        input: {
          host_id: resolvedHost,
          user_guidance: guidance.trim(),
          row_count: parsedRowCount,
        },
      });
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
  const failed = job && (job.status === 'failed' || job.status === 'error' || job.status === 'cancelled');

  return (
    <Drawer
      open={open}
      onClose={handleClose}
      title="Generate Evals"
      subtitle="Runs the gen-synthetic-evals workflow to build a dataset and rubric."
      footer={
        !job ? (
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={handleClose}>
              Cancel
            </Button>
            <Button variant="primary" form="gen-form" type="submit">
              Generate
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
        <form id="gen-form" onSubmit={handleSubmit} className="space-y-4">
          <Field label="Host">
            <Select value={resolvedHost} onChange={(e) => setHostId(e.target.value)}>
              {hosts.map((h) => (
                <option key={h.host_id} value={h.host_id}>
                  {h.host_id}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Rows" hint="Number of dataset rows to generate (1–100).">
            <TextInput
              type="number"
              min={1}
              max={100}
              value={rowCount}
              onChange={(e) => setRowCount(e.target.value)}
            />
          </Field>
          <Field label="Guidance" hint="Describe what kinds of scenarios to generate.">
            <TextArea
              rows={5}
              value={guidance}
              onChange={(e) => setGuidance(e.target.value)}
              placeholder="e.g. Focus on multi-tool scenarios where the agent must chain several calls."
            />
          </Field>
          {error ? <p className="text-sm text-rose-600">{error}</p> : null}
        </form>
      ) : (
        <div className="space-y-3 py-6 text-center">
          <Chip tone={statusTone(job.status)} className="text-sm">
            {job.status}
          </Chip>
          {running ? (
            <p className="text-sm text-slate-500">Workflow running — checking status…</p>
          ) : done ? (
            <p className="text-sm text-emerald-700">Eval generated successfully.</p>
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

const COLUMNS = [
  {
    key: 'eval_id',
    header: 'Eval',
    render: (row) => (
      <Link
        to={`/evals/${encodeURIComponent(row.eval_id)}`}
        className="font-mono text-xs text-slate-700 hover:text-slate-900 hover:underline"
        onClick={(e) => e.stopPropagation()}
      >
        {row.eval_id}
      </Link>
    ),
  },
  {
    key: 'host_id',
    header: 'Host',
    render: (row) => <Chip>{row.host_id}</Chip>,
  },
  {
    key: 'created_at',
    header: 'Created',
    render: (row) => (
      <span className="text-slate-500">{formatIso(row.created_at)}</span>
    ),
  },
  {
    key: 'actions',
    header: '',
    align: 'right',
    render: () => null,
  },
];

export default function Evals() {
  const hostsState = useApi(() => api.listHosts(), []);
  const hosts = hostsState.data?.hosts || [];

  const [hostFilter, setHostFilter] = useState('');
  const [genOpen, setGenOpen] = useState(false);
  const [deleting, setDeleting] = useState(null);

  const state = useApi(
    () => api.listEvals(hostFilter ? { host_id: hostFilter } : undefined),
    [hostFilter],
  );
  const evals = state.data?.evals || [];

  async function handleDelete(e, evalId) {
    e.stopPropagation();
    if (!window.confirm(`Delete eval ${evalId} and all its experiments?`)) return;
    setDeleting(evalId);
    try {
      await api.deleteEval(evalId);
      state.reload();
    } finally {
      setDeleting(null);
    }
  }

  const columns = [
    ...COLUMNS.slice(0, 3),
    {
      key: 'actions',
      header: '',
      align: 'right',
      render: (row) => (
        <Button
          variant="danger"
          className="text-xs"
          disabled={deleting === row.eval_id}
          onClick={(e) => handleDelete(e, row.eval_id)}
        >
          Delete
        </Button>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="Evals"
        description="Synthetic evaluation datasets, rubrics, and experiment results."
        actions={
          <Button variant="primary" onClick={() => setGenOpen(true)}>
            Generate
          </Button>
        }
      />

      <div className="mb-4 flex items-end gap-3">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-slate-600">Host</span>
          <div className="w-48">
            <Select value={hostFilter} onChange={(e) => setHostFilter(e.target.value)}>
              <option value="">All hosts</option>
              {hosts.map((h) => (
                <option key={h.host_id} value={h.host_id}>
                  {h.host_id}
                </option>
              ))}
            </Select>
          </div>
        </label>
      </div>

      <Async state={state} empty={(d) => !d?.evals?.length}>
        {() =>
          evals.length ? (
            <Table
              columns={columns}
              rows={evals}
              getRowKey={(row) => row.eval_id}
            />
          ) : (
            <Empty>No evals yet. Click Generate to create one.</Empty>
          )
        }
      </Async>

      <GenerateDrawer
        open={genOpen}
        onClose={() => setGenOpen(false)}
        hosts={hosts}
        onDone={() => {
          state.reload();
        }}
      />
    </div>
  );
}
