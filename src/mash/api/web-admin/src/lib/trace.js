// Trace status vocabulary and the actions available on a trace.
//
// A trace is one request, so the request lifecycle is the trace's status and
// the server addresses the request by trace id. `cancelled` mirrors the
// runtime's own terminal state; a resumed request reports `in_progress` again.

export const TRACE_STATUS = {
  completed: { label: 'completed', tone: 'emerald' },
  error: { label: 'error', tone: 'rose' },
  cancelled: { label: 'cancelled', tone: 'slate' },
  in_progress: { label: 'running', tone: 'indigo' },
};

export const TERMINAL_TRACE_STATUSES = new Set(['completed', 'error', 'cancelled']);

// The request-status endpoint speaks DBOS's vocabulary; map it onto the trace
// chips so a drawer and its list row never disagree.
const REQUEST_STATUS_TO_TRACE = {
  completed: 'completed',
  failed: 'error',
  error: 'error',
  cancelled: 'cancelled',
  pending: 'in_progress',
  running: 'in_progress',
  resumed: 'in_progress',
};

export function traceStatusFromRequest(status) {
  return REQUEST_STATUS_TO_TRACE[String(status || '')] || null;
}

export function traceStatusMeta(status) {
  return TRACE_STATUS[String(status || '')] || null;
}

/** Which actions apply to a trace in this state.
 *
 * Cancel needs something running. Resume covers cancelled only — DBOS refuses
 * to resume a workflow that ended in error, so rerun is a failed request's
 * only restart. Rerun applies to anything already finished.
 */
export function traceActions(status) {
  const value = String(status || '');
  return {
    cancel: value === 'in_progress',
    resume: value === 'cancelled',
    rerun: TERMINAL_TRACE_STATUSES.has(value),
  };
}
