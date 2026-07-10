import { Link } from 'react-router-dom';

import { Drawer } from '../Drawer.jsx';
import { Chip, Mono } from '../Chip.jsx';
import { CopyId } from '../CopyId.jsx';
import { JsonBlock } from '../Json.jsx';
import { formatTime } from '../../lib/format.js';
import { KindBadge, StatusBadge } from './WorkflowUI.jsx';

function JsonSection({ title, value, empty }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">{title}</h3>
      {value !== null && value !== undefined ? <JsonBlock value={value} /> : (
        <p className="text-sm text-slate-400">{empty}</p>
      )}
    </section>
  );
}

export function StepDetailDrawer({ step, events, workflowId, sessionId, onClose }) {
  const traceUrl = step?.agent_request_id
    ? `/logs?tab=sessions&workflow=${encodeURIComponent(workflowId)}${
      sessionId ? `&session=${encodeURIComponent(sessionId)}` : ''
    }&trace=${encodeURIComponent(step.agent_request_id)}`
    : null;
  return (
    <Drawer
      open={Boolean(step)}
      onClose={onClose}
      title={step ? `Step ${Number(step.ordinal) + 1}` : 'Step'}
      subtitle={step?.step_id}
    >
      {step ? (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center gap-2">
            <Mono>{step.step_id}</Mono>
            <KindBadge kind={step.kind} />
            <StatusBadge status={step.status} />
            {step.attempt > 1 ? <Chip tone="amber">attempt {step.attempt}</Chip> : null}
          </div>

          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div><dt className="text-xs text-slate-400">Started</dt><dd>{formatTime(step.started_at)}</dd></div>
            <div><dt className="text-xs text-slate-400">Finished</dt><dd>{formatTime(step.finished_at)}</dd></div>
            {step.agent_id ? <div><dt className="text-xs text-slate-400">Agent</dt><dd>{step.agent_id}</dd></div> : null}
            {step.agent_request_id ? (
              <div>
                <dt className="text-xs text-slate-400">Agent request</dt>
                <dd><CopyId value={step.agent_request_id} /></dd>
              </div>
            ) : null}
          </dl>

          {traceUrl ? (
            <Link to={traceUrl} className="inline-flex text-sm font-medium text-indigo-600 hover:underline">
              Open agent trace →
            </Link>
          ) : null}

          {step.error ? (
            <section className="rounded-md border border-rose-200 bg-rose-50 p-3">
              <h3 className="text-xs font-medium uppercase tracking-wide text-rose-500">Error</h3>
              <p className="mt-1 whitespace-pre-wrap text-sm text-rose-700">{step.error}</p>
            </section>
          ) : null}

          <JsonSection title="Input snapshot" value={step.input_snapshot} empty="Not recorded yet." />
          <JsonSection title="Output snapshot" value={step.output_snapshot} empty="No output recorded." />

          <section>
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">Lifecycle events</h3>
            {events.length ? (
              <ol className="space-y-2 border-l border-slate-200 pl-4">
                {events.map((event) => (
                  <li key={`${event.attempt}-${event.event_type}-${event.seq}`} className="text-sm">
                    <div className="flex items-center justify-between gap-3">
                      <span className="font-medium text-slate-700">{event.event_type}</span>
                      <span className="text-xs text-slate-400">{formatTime(event.at)}</span>
                    </div>
                    {event.payload && Object.keys(event.payload).length ? (
                      <JsonBlock value={event.payload} className="mt-2" />
                    ) : null}
                  </li>
                ))}
              </ol>
            ) : <p className="text-sm text-slate-400">No lifecycle events recorded.</p>}
          </section>
        </div>
      ) : null}
    </Drawer>
  );
}
