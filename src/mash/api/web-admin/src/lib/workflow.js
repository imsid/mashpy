export const TERMINAL_RUN_STATUSES = new Set(['completed', 'failed', 'cancelled']);

export function statusTone(status) {
  if (status === 'completed') return 'emerald';
  if (status === 'failed') return 'rose';
  if (status === 'running') return 'indigo';
  if (status === 'queued' || status === 'pending' || status === 'cancelled') return 'slate';
  return 'amber';
}

export function workflowType(workflow) {
  if (workflow.mode === 'strategy') return 'strategy';
  const kinds = workflow.step_kinds || {};
  if (kinds.code && kinds.agent) return 'mixed';
  if (kinds.code) return 'code';
  if (kinds.agent) return 'agent';
  return 'pipeline';
}

export function workflowCatalogEntries(workflows = []) {
  return workflows.filter((workflow) => workflow.mode === 'pipeline');
}

export function resolveSchema(schema, rootSchema) {
  if (!schema || typeof schema !== 'object') return schema;
  const ref = schema.$ref;
  if (typeof ref !== 'string' || !ref.startsWith('#/')) return schema;
  const resolved = ref
    .slice(2)
    .split('/')
    .reduce((value, part) => value?.[part.replaceAll('~1', '/').replaceAll('~0', '~')], rootSchema);
  return resolved || schema;
}

export function schemaType(schema, rootSchema) {
  const resolved = resolveSchema(schema, rootSchema) || {};
  if (resolved.type) return Array.isArray(resolved.type)
    ? resolved.type.find((value) => value !== 'null')
    : resolved.type;
  const choices = resolved.anyOf || resolved.oneOf;
  if (Array.isArray(choices)) {
    const nonNull = choices.find((choice) => choice?.type !== 'null');
    return schemaType(nonNull, rootSchema);
  }
  return undefined;
}

export function schemaFields(schema) {
  if (!schema || typeof schema !== 'object') return [];
  const required = new Set(schema.required || []);
  return Object.entries(schema.properties || {}).map(([name, property]) => ({
    name,
    schema: resolveSchema(property, schema) || property,
    required: required.has(name),
    type: schemaType(property, schema),
  }));
}

export function initialWorkflowInput(schema, supplied = {}) {
  const input = supplied && typeof supplied === 'object' && !Array.isArray(supplied)
    ? { ...supplied }
    : {};
  for (const field of schemaFields(schema)) {
    if (!(field.name in input) && field.schema?.default !== undefined) {
      input[field.name] = field.schema.default;
    }
  }
  return input;
}

export function parseJsonObject(text) {
  let value;
  try {
    value = JSON.parse(text || '{}');
  } catch (error) {
    return { value: null, error: `Input must be valid JSON: ${error.message}` };
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return { value: null, error: 'Workflow input must be a JSON object.' };
  }
  return { value, error: null };
}

export function validateWorkflowInput(schema, input) {
  if (!schema) return {};
  const errors = {};
  for (const field of schemaFields(schema)) {
    const value = input[field.name];
    if (field.required && (value === undefined || value === null || value === '')) {
      errors[field.name] = 'Required.';
      continue;
    }
    if (value === undefined || value === null || value === '') continue;
    if (field.type === 'string' && typeof value !== 'string') {
      errors[field.name] = 'Must be text.';
    } else if (field.type === 'integer' && !Number.isInteger(value)) {
      errors[field.name] = 'Must be a whole number.';
    } else if (field.type === 'number' && typeof value !== 'number') {
      errors[field.name] = 'Must be a number.';
    } else if (field.type === 'boolean' && typeof value !== 'boolean') {
      errors[field.name] = 'Must be true or false.';
    } else if (field.type === 'array' && !Array.isArray(value)) {
      errors[field.name] = 'Must be an array.';
    } else if (field.type === 'object' && (typeof value !== 'object' || Array.isArray(value))) {
      errors[field.name] = 'Must be an object.';
    }
    if (typeof value === 'number') {
      if (field.schema.minimum !== undefined && value < field.schema.minimum) {
        errors[field.name] = `Must be at least ${field.schema.minimum}.`;
      }
      if (field.schema.maximum !== undefined && value > field.schema.maximum) {
        errors[field.name] = `Must be at most ${field.schema.maximum}.`;
      }
    }
    if (typeof value === 'string') {
      if (field.schema.minLength !== undefined && value.length < field.schema.minLength) {
        errors[field.name] = `Must contain at least ${field.schema.minLength} characters.`;
      }
      if (field.schema.maxLength !== undefined && value.length > field.schema.maxLength) {
        errors[field.name] = `Must contain at most ${field.schema.maxLength} characters.`;
      }
    }
  }
  return errors;
}

export function mergeWorkflowSteps(definitionSteps = [], runSteps = []) {
  const stored = new Map(runSteps.map((step) => [step.step_id, step]));
  const known = new Set(definitionSteps.map((step) => step.step_id));
  const merged = definitionSteps.map((definition) => ({
    ...definition,
    status: 'pending',
    attempt: 1,
    ...stored.get(definition.step_id),
  }));
  const historical = runSteps
    .filter((step) => !known.has(step.step_id))
    .sort((left, right) => Number(left.ordinal) - Number(right.ordinal));
  return [...merged, ...historical];
}

export function workflowEventKey(event) {
  return [event.step_id || '', event.attempt || 0, event.event_type || '', event.seq || 0].join(':');
}

export function durationSeconds(startedAt, finishedAt, now = Date.now() / 1000) {
  if (!startedAt) return null;
  return Math.max(0, Number(finishedAt || now) - Number(startedAt));
}
