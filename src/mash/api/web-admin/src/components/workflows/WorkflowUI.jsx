import { Link } from 'react-router-dom';

import { Card } from '../Page.jsx';
import { Chip, Mono } from '../Chip.jsx';
import { CopyId } from '../CopyId.jsx';
import { Table } from '../Table.jsx';
import { formatTime } from '../../lib/format.js';
import { durationSeconds, schemaFields, statusTone } from '../../lib/workflow.js';

export function StatusBadge({ status }) {
  const value = String(status || 'unknown');
  return <Chip tone={statusTone(value)}>{value}</Chip>;
}

export function KindBadge({ kind }) {
  return <Chip tone={kind === 'agent' ? 'indigo' : 'slate'}>{kind || 'unknown'}</Chip>;
}

export function Breadcrumbs({ items }) {
  return (
    <div className="mb-5 flex flex-wrap items-center gap-2 text-sm text-slate-500">
      {items.map((item, index) => (
        <span key={`${item.label}-${index}`} className="flex items-center gap-2">
          {index ? <span className="text-slate-300">›</span> : null}
          {item.to ? (
            <Link to={item.to} className="hover:text-slate-700 hover:underline">
              {item.label}
            </Link>
          ) : (
            <span className="text-slate-700">{item.label}</span>
          )}
        </span>
      ))}
    </div>
  );
}

function modelTitle(schema, fallback) {
  return schema?.title || fallback;
}

export function SchemaSummary({ schema, title = 'Input contract' }) {
  if (!schema) {
    return (
      <Card className="p-4">
        <h2 className="font-display text-sm font-semibold">{title}</h2>
        <p className="mt-1 text-sm text-slate-500">
          Untyped JSON object. Use JSON mode when starting a run.
        </p>
      </Card>
    );
  }
  const fields = schemaFields(schema);
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="font-display text-sm font-semibold">{title}</h2>
          {schema.title ? <p className="mt-0.5 text-xs text-slate-400">{schema.title}</p> : null}
        </div>
        <Chip>{fields.length} field{fields.length === 1 ? '' : 's'}</Chip>
      </div>
      <SchemaFieldList schema={schema} />
    </Card>
  );
}

function SchemaFieldList({ schema, emptyLabel = 'No declared fields.' }) {
  const fields = schemaFields(schema);
  if (!fields.length) return <p className="mt-3 text-sm text-slate-400">{emptyLabel}</p>;
  return (
    <dl className="mt-3 divide-y divide-slate-100 rounded-md border border-slate-200">
      {fields.map((field) => (
        <div key={field.name} className="grid grid-cols-[minmax(0,1fr)_minmax(5rem,0.7fr)_minmax(0,1.4fr)] gap-3 px-3 py-2.5 text-sm">
          <dt className="flex min-w-0 items-center gap-1.5 font-mono text-xs text-slate-700">
            <span className="truncate" title={field.name}>{field.name}</span>
            {field.required ? <span className="text-rose-500" title="Required">*</span> : null}
          </dt>
          <dd className="text-xs text-slate-500">{field.type || 'schema'}</dd>
          <dd className="text-xs text-slate-500">
            {field.schema.description ||
              (field.schema.default !== undefined ? `Default: ${String(field.schema.default)}` : '—')}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function StepContract({ label, schema, fallback }) {
  return (
    <div className="min-w-0 rounded-md border border-slate-200 bg-slate-50/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{label}</p>
        <span className="truncate text-xs text-slate-500" title={modelTitle(schema, fallback)}>
          {modelTitle(schema, fallback)}
        </span>
      </div>
      {schema ? (
        <SchemaFieldList schema={schema} />
      ) : (
        <p className="mt-3 text-xs text-slate-400">Receives the current pipeline payload unchanged.</p>
      )}
    </div>
  );
}

export function Pipeline({ definition, steps, onStepClick }) {
  if (definition.mode === 'strategy') {
    return (
      <Card className="p-4">
        <div className="flex items-center gap-2">
          <Chip tone="amber">custom strategy</Chip>
          <Mono>{definition.strategy}</Mono>
        </div>
        <p className="mt-3 text-sm leading-relaxed text-slate-600">
          This workflow owns a non-linear execution shape and its own result surface.
          Generic step inspection and run history are not available here.
        </p>
      </Card>
    );
  }

  const rows = steps || definition.steps || [];
  return (
    <div className="space-y-2">
      <p className="text-xs text-slate-400">
        Before each step, the previous output overlays the immutable workflow input.
      </p>
      {rows.map((step, index) => {
        const content = (
          <Card className={`p-4 ${onStepClick ? 'transition hover:border-slate-300 hover:shadow-sm' : ''}`}>
            <div className="flex items-start justify-between gap-4">
              <div className="flex min-w-0 items-start gap-3">
                <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-100 text-xs font-semibold tabular-nums text-slate-500">
                  {index + 1}
                </span>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <Mono>{step.step_id}</Mono>
                    <KindBadge kind={step.kind} />
                    {step.status ? <StatusBadge status={step.status} /> : null}
                    {step.attempt > 1 ? <Chip tone="amber">attempt {step.attempt}</Chip> : null}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                    <span>
                      {step.input_schema ? modelTitle(step.input_schema, 'Typed input') : 'Pass-through input'}
                      {' → '}
                      {modelTitle(step.output_schema, 'Typed output')}
                    </span>
                    {step.agent_id ? onStepClick ? (
                      <span>agent {step.agent_id}</span>
                    ) : (
                      <Link
                        to={`/agents#agent-${encodeURIComponent(step.agent_id)}`}
                        className="text-indigo-600 hover:underline"
                      >
                        agent {step.agent_id}
                      </Link>
                    ) : null}
                    {step.skill_name ? onStepClick ? (
                      <span>skill {step.skill_name}</span>
                    ) : (
                      <Link
                        to={`/skills/${encodeURIComponent(step.skill_name)}`}
                        className="text-indigo-600 hover:underline"
                      >
                        skill {step.skill_name}
                      </Link>
                    ) : null}
                    {step.timeout_s ? <span>timeout {step.timeout_s}s</span> : null}
                    {step.started_at ? (
                      <span>
                        {formatTime(step.started_at)} · {formatStepDuration(step)}
                      </span>
                    ) : null}
                  </div>
                  {step.error ? <p className="mt-2 line-clamp-2 text-xs text-rose-600">{step.error}</p> : null}
                  {!onStepClick ? (
                    <div className="mt-3 grid gap-3 xl:grid-cols-2">
                      <StepContract label="Input" schema={step.input_schema} fallback="Pass-through" />
                      <StepContract label="Output" schema={step.output_schema} fallback="Typed output" />
                    </div>
                  ) : null}
                </div>
              </div>
              {onStepClick ? <span className="text-xs text-slate-400">Inspect →</span> : null}
            </div>
          </Card>
        );
        return (
          <div key={step.step_id}>
            {onStepClick ? (
              <button type="button" onClick={() => onStepClick(step)} className="w-full text-left">
                {content}
              </button>
            ) : content}
            {index < rows.length - 1 ? (
              <div className="ml-[1.65rem] h-4 border-l border-slate-300" aria-hidden />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function formatStepDuration(step) {
  const seconds = durationSeconds(step.started_at, step.finished_at);
  if (seconds === null) return '—';
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
}

export function RunTable({ runs, onSelect }) {
  return (
    <Table
      columns={[
        { key: 'status', header: 'Status', render: (run) => <StatusBadge status={run.status} /> },
        { key: 'started', header: 'Started', render: (run) => formatTime(run.started_at || run.created_at) },
        { key: 'duration', header: 'Duration', render: (run) => formatStepDuration(run) },
        { key: 'run_id', header: 'Run ID', render: (run) => <CopyId value={run.run_id} /> },
        { key: 'dedup_key', header: 'Dedup key', render: (run) => run.dedup_key || <span className="text-slate-300">—</span> },
        {
          key: 'error',
          header: 'Error',
          render: (run) => run.error ? (
            <span className="block max-w-xs truncate text-rose-600" title={run.error}>{run.error}</span>
          ) : <span className="text-slate-300">—</span>,
        },
      ]}
      rows={runs}
      getRowKey={(run) => run.run_id}
      onRowClick={onSelect}
    />
  );
}
