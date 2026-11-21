// Trace status vocabulary.
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

// Map the request-status endpoint's statuses onto the trace chips so a drawer
// and its list row never disagree.
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

