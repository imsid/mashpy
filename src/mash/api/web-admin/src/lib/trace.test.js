import test from 'node:test';
import assert from 'node:assert/strict';

import { traceStatusFromRequest, traceStatusMeta } from './trace.js';

test('request status maps onto the trace chip vocabulary', () => {
  assert.equal(traceStatusFromRequest('failed'), 'error');
  assert.equal(traceStatusFromRequest('cancelled'), 'cancelled');
  assert.equal(traceStatusFromRequest('pending'), 'in_progress');
  assert.equal(traceStatusFromRequest('nonsense'), null);
});

test('cancelled renders as its own chip rather than falling through', () => {
  assert.equal(traceStatusMeta('cancelled').label, 'cancelled');
  assert.equal(traceStatusMeta('bogus'), null);
});
