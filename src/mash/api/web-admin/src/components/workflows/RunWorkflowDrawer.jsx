import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { Drawer } from '../Drawer.jsx';
import { Button, Field, Select, TextArea, TextInput } from '../Form.jsx';
import { api } from '../../lib/api.js';
import {
  initialWorkflowInput,
  parseJsonObject,
  schemaFields,
  validateWorkflowInput,
} from '../../lib/workflow.js';

function ErrorText({ children }) {
  return children ? <p className="mt-1 text-xs text-rose-600">{children}</p> : null;
}

function complexDrafts(fields, input) {
  return Object.fromEntries(
    fields
      .filter((field) => field.type === 'array' || field.type === 'object' || !field.type)
      .map((field) => [
        field.name,
        JSON.stringify(input[field.name] ?? (field.type === 'array' ? [] : {}), null, 2),
      ]),
  );
}

export function RunWorkflowDrawer({ definition, open, initialInput, onClose, onStarted }) {
  const fields = useMemo(() => schemaFields(definition?.input_schema), [definition]);
  const [mode, setMode] = useState('form');
  const [input, setInput] = useState({});
  const [raw, setRaw] = useState('{}');
  const [drafts, setDrafts] = useState({});
  const [dedupKey, setDedupKey] = useState('');
  const [errors, setErrors] = useState({});
  const [submitError, setSubmitError] = useState(null);
  const [duplicateRunId, setDuplicateRunId] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open || !definition) return;
    const seeded = initialWorkflowInput(definition.input_schema, initialInput || {});
    setInput(seeded);
    setRaw(JSON.stringify(seeded, null, 2));
    setDrafts(complexDrafts(schemaFields(definition.input_schema), seeded));
    setMode(definition.input_schema ? 'form' : 'json');
    setDedupKey('');
    setErrors({});
    setSubmitError(null);
    setDuplicateRunId(null);
  }, [open, definition, initialInput]);

  if (!definition) return null;

  function updateField(name, value) {
    setInput((current) => {
      const next = { ...current };
      if (value === undefined || value === '') delete next[name];
      else next[name] = value;
      return next;
    });
    setErrors((current) => ({ ...current, [name]: undefined }));
  }

  function switchMode(nextMode) {
    if (nextMode === 'json') {
      setRaw(JSON.stringify(input, null, 2));
      setMode('json');
      return;
    }
    const parsed = parseJsonObject(raw);
    if (parsed.error) {
      setSubmitError(parsed.error);
      return;
    }
    setInput(parsed.value);
    setDrafts(complexDrafts(fields, parsed.value));
    setSubmitError(null);
    setMode('form');
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setSubmitError(null);
    setDuplicateRunId(null);
    let workflowInput;
    if (mode === 'json') {
      const parsed = parseJsonObject(raw);
      if (parsed.error) {
        setSubmitError(parsed.error);
        return;
      }
      workflowInput = parsed.value;
    } else {
      workflowInput = { ...input };
      const nextErrors = {};
      for (const field of fields) {
        if (!(field.name in drafts)) continue;
        const parsed = parseJsonValue(drafts[field.name], field.type);
        if (parsed.error) nextErrors[field.name] = parsed.error;
        else workflowInput[field.name] = parsed.value;
      }
      Object.assign(nextErrors, validateWorkflowInput(definition.input_schema, workflowInput));
      if (Object.keys(nextErrors).length) {
        setErrors(nextErrors);
        return;
      }
    }

    setSubmitting(true);
    try {
      const run = await api.runWorkflow(definition.workflow_id, {
        input: workflowInput,
        ...(dedupKey.trim() ? { dedup_key: dedupKey.trim() } : {}),
      });
      onStarted(run);
    } catch (error) {
      if (error.code === 'WORKFLOW_DUPLICATE_RUN' && error.details?.run_id) {
        setDuplicateRunId(error.details.run_id);
        setSubmitError('A run with this dedup key is already active.');
      } else if (error.code === 'WORKFLOW_INPUT_INVALID' && error.details?.errors) {
        const fieldErrors = {};
        for (const item of error.details.errors) {
          const field = item.loc?.[0];
          if (field) fieldErrors[field] = item.msg;
        }
        setErrors(fieldErrors);
        setSubmitError(error.message);
      } else {
        setSubmitError(error.message || 'Failed to start workflow.');
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title="Run workflow"
      subtitle={definition.workflow_id}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button variant="primary" form="run-workflow-form" type="submit" disabled={submitting}>
            {submitting ? 'Starting…' : 'Run workflow'}
          </Button>
        </div>
      }
    >
      <form id="run-workflow-form" onSubmit={handleSubmit} className="space-y-5">
        {definition.input_schema ? (
          <div className="flex gap-1 border-b border-slate-200">
            {['form', 'json'].map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => switchMode(item)}
                className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium capitalize ${
                  mode === item
                    ? 'border-slate-900 text-slate-900'
                    : 'border-transparent text-slate-500'
                }`}
              >
                {item}
              </button>
            ))}
          </div>
        ) : (
          <p className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-700">
            This workflow does not publish a typed input contract. Provide a JSON object.
          </p>
        )}

        {mode === 'json' ? (
          <Field label="Workflow input" hint="A JSON object passed unchanged to the run.">
            <TextArea
              rows={14}
              className="font-mono"
              value={raw}
              onChange={(event) => setRaw(event.target.value)}
            />
          </Field>
        ) : fields.length ? (
          <div className="space-y-4">
            {fields.map((field) => (
              <SchemaField
                key={field.name}
                field={field}
                value={input[field.name]}
                draft={drafts[field.name]}
                error={errors[field.name]}
                onChange={(value) => updateField(field.name, value)}
                onDraftChange={(value) => {
                  setDrafts((current) => ({ ...current, [field.name]: value }));
                  setErrors((current) => ({ ...current, [field.name]: undefined }));
                }}
              />
            ))}
          </div>
        ) : (
          <p className="text-sm text-slate-500">This workflow takes no declared input fields.</p>
        )}

        <details className="rounded-md border border-slate-200 p-3">
          <summary className="cursor-pointer text-sm font-medium text-slate-700">Advanced</summary>
          <div className="mt-3">
            <Field
              label="Dedup key"
              hint="While a run with this key is active, another run with the same key is rejected."
            >
              <TextInput value={dedupKey} onChange={(event) => setDedupKey(event.target.value)} />
            </Field>
          </div>
        </details>

        {submitError ? (
          <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
            {submitError}
            {duplicateRunId ? (
              <Link
                to={`/workflows/${encodeURIComponent(definition.workflow_id)}/runs/${encodeURIComponent(duplicateRunId)}`}
                className="ml-1 font-medium underline"
              >
                Open active run
              </Link>
            ) : null}
          </div>
        ) : null}
      </form>
    </Drawer>
  );
}

function SchemaField({ field, value, draft, error, onChange, onDraftChange }) {
  const label = `${field.name}${field.required ? ' *' : ''}`;
  const hint = field.schema.description || typeHint(field);
  const choices = field.schema.enum;
  if (Array.isArray(choices)) {
    return (
      <Field label={label} hint={hint}>
        <Select
          value={value ?? ''}
          onChange={(event) => onChange(
            choices.find((choice) => String(choice) === event.target.value) ?? event.target.value,
          )}
        >
          {!field.required ? <option value="">—</option> : null}
          {choices.map((choice) => <option key={String(choice)} value={choice}>{String(choice)}</option>)}
        </Select>
        <ErrorText>{error}</ErrorText>
      </Field>
    );
  }
  if (field.type === 'boolean') {
    return (
      <Field label={label} hint={hint}>
        <span className="flex items-center gap-2 text-sm text-slate-700">
          <input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} />
          Enabled
        </span>
        <ErrorText>{error}</ErrorText>
      </Field>
    );
  }
  if (field.type === 'array' || field.type === 'object' || !field.type) {
    return (
      <Field label={label} hint={hint}>
        <TextArea rows={5} className="font-mono" value={draft ?? ''} onChange={(event) => onDraftChange(event.target.value)} />
        <ErrorText>{error}</ErrorText>
      </Field>
    );
  }
  const numeric = field.type === 'integer' || field.type === 'number';
  return (
    <Field label={label} hint={hint}>
      <TextInput
        type={numeric ? 'number' : 'text'}
        step={field.type === 'integer' ? '1' : numeric ? 'any' : undefined}
        min={field.schema.minimum}
        max={field.schema.maximum}
        value={value ?? ''}
        onChange={(event) => {
          if (!numeric) onChange(event.target.value);
          else if (event.target.value === '') onChange(undefined);
          else onChange(field.type === 'integer' ? parseInt(event.target.value, 10) : parseFloat(event.target.value));
        }}
      />
      <ErrorText>{error}</ErrorText>
    </Field>
  );
}

function parseJsonValue(text, type) {
  try {
    const value = JSON.parse(text || (type === 'array' ? '[]' : '{}'));
    if (type === 'array' && !Array.isArray(value)) return { error: 'Must be a JSON array.' };
    if (type === 'object' && (!value || typeof value !== 'object' || Array.isArray(value))) {
      return { error: 'Must be a JSON object.' };
    }
    return { value };
  } catch (error) {
    return { error: `Invalid JSON: ${error.message}` };
  }
}

function typeHint(field) {
  const constraints = [];
  if (field.type) constraints.push(field.type);
  if (field.schema.minimum !== undefined) constraints.push(`min ${field.schema.minimum}`);
  if (field.schema.maximum !== undefined) constraints.push(`max ${field.schema.maximum}`);
  if (field.schema.default !== undefined) constraints.push(`default ${String(field.schema.default)}`);
  return constraints.join(' · ');
}
