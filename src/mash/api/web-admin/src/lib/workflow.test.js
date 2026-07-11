import test from 'node:test';
import assert from 'node:assert/strict';

import { workflowRunEventsUrl } from './api.js';
import {
  initialWorkflowInput,
  mergeWorkflowSteps,
  parseJsonObject,
  schemaFields,
  validateWorkflowInput,
  workflowEventKey,
  workflowType,
} from './workflow.js';
import { agentAnchor, buildAgentUsage } from './agent.js';

const schema = {
  type: 'object',
  required: ['count'],
  properties: {
    count: { type: 'integer', minimum: 1, default: 3 },
    mode: { anyOf: [{ type: 'string' }, { type: 'null' }] },
  },
};

test('schema fields resolve required and nullable types', () => {
  assert.deepEqual(
    schemaFields(schema).map(({ name, required, type }) => ({ name, required, type })),
    [
      { name: 'count', required: true, type: 'integer' },
      { name: 'mode', required: false, type: 'string' },
    ],
  );
});

test('initial input applies defaults without replacing supplied values', () => {
  assert.deepEqual(initialWorkflowInput(schema), { count: 3 });
  assert.deepEqual(initialWorkflowInput(schema, { count: 8 }), { count: 8 });
});

test('input validation checks required fields and constraints', () => {
  assert.deepEqual(validateWorkflowInput(schema, {}), { count: 'Required.' });
  assert.deepEqual(validateWorkflowInput(schema, { count: 0 }), {
    count: 'Must be at least 1.',
  });
  assert.deepEqual(validateWorkflowInput(schema, { count: 2 }), {});
});

test('raw JSON input must be an object', () => {
  assert.deepEqual(parseJsonObject('{"ok":true}'), { value: { ok: true }, error: null });
  assert.equal(parseJsonObject('[]').error, 'Workflow input must be a JSON object.');
  assert.match(parseJsonObject('{bad').error, /valid JSON/);
});

test('definition steps remain visible before store rows arrive', () => {
  assert.deepEqual(
    mergeWorkflowSteps(
      [{ step_id: 'scan', kind: 'code', ordinal: 0 }],
      [{ step_id: 'scan', status: 'running', attempt: 2 }],
    ),
    [{ step_id: 'scan', kind: 'code', ordinal: 0, status: 'running', attempt: 2 }],
  );
});

test('stored historical steps are not hidden by the current definition', () => {
  assert.deepEqual(
    mergeWorkflowSteps([], [{ step_id: 'retired', status: 'completed', ordinal: 0 }]),
    [{ step_id: 'retired', status: 'completed', ordinal: 0 }],
  );
});

test('catalog types and event keys are deterministic', () => {
  assert.equal(workflowType({ step_kinds: { code: 1, agent: 1 } }), 'mixed');
  assert.equal(workflowType({ step_kinds: { agent: 2 } }), 'agent');
  assert.equal(
    workflowEventKey({ step_id: 'scan', attempt: 1, event_type: 'step.started', seq: 2 }),
    'scan:1:step.started:2',
  );
});

test('workflow event URLs encode definition and run ids', () => {
  assert.equal(
    workflowRunEventsUrl('wf/one', 'run/one'),
    '/api/v1/workflow/wf%2Fone/runs/run%2Fone/events',
  );
});

test('agent usage includes host roles and workflow step dependencies', () => {
  const usage = buildAgentUsage(
    [{ host_id: 'main', primary: 'primary', subagents: ['research'] }],
    [{
      workflow_id: 'gen-synthetic-evals',
      steps: [{ step_id: 'generate', kind: 'agent', agent_id: 'eval-agent' }],
    }, {
      workflow_id: 'run-experiment',
      steps: [{ step_id: 'judge-rows', kind: 'code', agent_ids: ['eval-judge-agent'] }],
    }],
  );
  assert.deepEqual(usage.get('primary'), [
    { type: 'host', id: 'main', role: 'primary' },
  ]);
  assert.deepEqual(usage.get('eval-agent'), [
    { type: 'workflow', id: 'gen-synthetic-evals', role: 'generate' },
  ]);
  assert.deepEqual(usage.get('eval-judge-agent'), [
    { type: 'workflow', id: 'run-experiment', role: 'judge-rows' },
  ]);
  assert.equal(agentAnchor('eval-agent/judge'), 'agent-eval-agent%2Fjudge');
});
