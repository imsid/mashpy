import test from 'node:test';
import assert from 'node:assert/strict';

import { traceActions, traceStatusFromRequest, traceStatusMeta } from './trace.js';

test('cancel applies only while the trace is running', () => {
  assert.equal(traceActions('in_progress').cancel, true);
  assert.equal(traceActions('completed').cancel, false);
  assert.equal(traceActions('cancelled').cancel, false);
});

test('resume applies to cancelled traces but never to failed ones', () => {
  // DBOS refuses to resume a workflow that ended in ERROR, so offering resume
  // on a failed trace would promise something the runtime cannot do.
  assert.equal(traceActions('cancelled').resume, true);
  assert.equal(traceActions('error').resume, false);
  assert.equal(traceActions('in_progress').resume, false);
});

test('rerun applies to every finished trace', () => {
  for (const status of ['completed', 'error', 'cancelled']) {
    assert.equal(traceActions(status).rerun, true, status);
  }
  assert.equal(traceActions('in_progress').rerun, false);
});

test('unknown status offers no actions', () => {
  assert.deepEqual(traceActions(undefined), {
    cancel: false,
    resume: false,
    rerun: false,
  });
});

test('request status maps onto the trace chip vocabulary', () => {
  assert.equal(traceStatusFromRequest('failed'), 'error');
  assert.equal(traceStatusFromRequest('cancelled'), 'cancelled');
  assert.equal(traceStatusFromRequest('pending'), 'in_progress');
  assert.equal(traceStatusFromRequest('resumed'), 'in_progress');
  assert.equal(traceStatusFromRequest('nonsense'), null);
});

test('cancelled renders as its own chip rather than falling through', () => {
  assert.equal(traceStatusMeta('cancelled').label, 'cancelled');
  assert.equal(traceStatusMeta('bogus'), null);
});
