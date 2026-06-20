import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { Drawer } from '../components/Drawer.jsx';
import { Button, Field, TextInput, TextArea, Select } from '../components/Form.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

function randomSession() {
  return `admin-${Math.random().toString(36).slice(2, 8)}`;
}

function HostCard({ host, onEdit, onTest }) {
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-display text-base font-semibold">{host.host_id}</h3>
        <div className="flex gap-1.5">
          <Button variant="ghost" onClick={() => onTest(host)}>
            Test
          </Button>
          <Button variant="ghost" onClick={() => onEdit(host)}>
            Edit
          </Button>
        </div>
      </div>
      <div className="space-y-2 text-sm">
        <div className="flex items-center gap-2">
          <span className="w-20 shrink-0 text-xs font-medium uppercase tracking-wide text-slate-400">
            Primary
          </span>
          <Chip>{host.primary}</Chip>
        </div>
        <div className="flex items-start gap-2">
          <span className="w-20 shrink-0 pt-0.5 text-xs font-medium uppercase tracking-wide text-slate-400">
            Subagents
          </span>
          <div className="flex flex-wrap gap-1.5">
            {host.subagents?.length ? (
              host.subagents.map((s) => <Chip key={s}>{s}</Chip>)
            ) : (
              <span className="text-xs text-slate-400">none</span>
            )}
          </div>
        </div>
        {host.workflows?.length ? (
          <div className="flex items-start gap-2">
            <span className="w-20 shrink-0 pt-0.5 text-xs font-medium uppercase tracking-wide text-slate-400">
              Workflows
            </span>
            <div className="flex flex-wrap gap-1.5">
              {host.workflows.map((w) => (
                <Chip key={w}>{w}</Chip>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </Card>
  );
}

function HostFormDrawer({ open, host, agents, onClose, onSaved }) {
  const isNew = !host?.host_id;
  const [hostId, setHostId] = useState('');
  const [primary, setPrimary] = useState('');
  const [subagents, setSubagents] = useState([]);
  const [workflows, setWorkflows] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [initialized, setInitialized] = useState(false);

  // Seed form state once per open.
  if (open && !initialized) {
    setHostId(host?.host_id || '');
    setPrimary(host?.primary || agents[0]?.agent_id || '');
    setSubagents(host?.subagents || []);
    setWorkflows((host?.workflows || []).join(', '));
    setInitialized(true);
  }
  if (!open && initialized) setInitialized(false);

  const toggleSub = (id) =>
    setSubagents((cur) =>
      cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id],
    );

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.defineHost(hostId.trim(), {
        primary: primary.trim(),
        subagents: subagents.filter((s) => s !== primary),
        workflows: workflows
          .split(',')
          .map((w) => w.trim())
          .filter(Boolean),
      });
      onSaved();
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={isNew ? 'New host' : `Edit ${host.host_id}`}
      subtitle="Compositions are in-memory and reset on restart unless defined in code."
      footer={
        <div className="flex items-center justify-between">
          <span className="text-xs text-rose-600">{error?.message || ''}</span>
          <div className="flex gap-2">
            <Button variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={save}
              disabled={saving || !hostId.trim() || !primary}
            >
              {saving ? 'Saving…' : 'Save host'}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-4">
        <Field label="Host ID" hint={isNew ? 'Unique id for this composition.' : undefined}>
          <TextInput
            value={hostId}
            disabled={!isNew}
            onChange={(e) => setHostId(e.target.value)}
            placeholder="assistant"
          />
        </Field>
        <Field label="Primary agent">
          <Select value={primary} onChange={(e) => setPrimary(e.target.value)}>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.metadata?.display_name || a.agent_id} ({a.agent_id})
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Subagents">
          <div className="max-h-48 space-y-1 overflow-y-auto rounded-md border border-slate-200 p-2">
            {agents
              .filter((a) => a.agent_id !== primary)
              .map((a) => (
                <label key={a.agent_id} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={subagents.includes(a.agent_id)}
                    onChange={() => toggleSub(a.agent_id)}
                  />
                  <span>{a.metadata?.display_name || a.agent_id}</span>
                  <Mono>{a.agent_id}</Mono>
                </label>
              ))}
          </div>
        </Field>
        <Field label="Workflows" hint="Comma-separated workflow ids (optional).">
          <TextInput
            value={workflows}
            onChange={(e) => setWorkflows(e.target.value)}
            placeholder="onboarding, digest"
          />
        </Field>
      </div>
    </Drawer>
  );
}

function TestRequestDrawer({ open, host, onClose }) {
  const navigate = useNavigate();
  const [message, setMessage] = useState('');
  const [sessionId, setSessionId] = useState(randomSession());
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const send = async () => {
    setSending(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.submitHostRequest(host.host_id, {
        message: message.trim(),
        session_id: sessionId.trim(),
      });
      setResult(res);
    } catch (err) {
      setError(err);
    } finally {
      setSending(false);
    }
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={host ? `Test ${host.host_id}` : 'Test host'}
      subtitle="Submit a request to this host's primary agent."
      footer={
        <div className="flex items-center justify-between">
          <span className="text-xs text-rose-600">{error?.message || ''}</span>
          <Button
            variant="primary"
            onClick={send}
            disabled={sending || !message.trim() || !sessionId.trim()}
          >
            {sending ? 'Sending…' : 'Send request'}
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        <Field label="Session ID">
          <TextInput value={sessionId} onChange={(e) => setSessionId(e.target.value)} />
        </Field>
        <Field label="Message">
          <TextArea
            rows={4}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Ask the host something…"
          />
        </Field>
        {result ? (
          <Card className="space-y-2 bg-slate-50 p-3 text-sm">
            <div className="font-medium text-emerald-700">Request accepted</div>
            <div className="flex items-center gap-2">
              <span className="text-slate-500">Agent</span>
              <Mono>{result.agent_id}</Mono>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-slate-500">Request</span>
              <Mono>{result.request_id}</Mono>
            </div>
            <Button
              variant="secondary"
              onClick={() =>
                navigate(
                  `/logs?agent=${encodeURIComponent(result.agent_id)}&session=${encodeURIComponent(result.session_id || sessionId)}`,
                )
              }
            >
              View in Logs →
            </Button>
          </Card>
        ) : null}
      </div>
    </Drawer>
  );
}

export default function Hosts() {
  const state = useApi(() => api.listHosts(), []);
  const agentsState = useApi(() => api.listAgents(), []);
  const [editing, setEditing] = useState(null);
  const [testing, setTesting] = useState(null);

  const agents = agentsState.data?.agents || [];

  return (
    <div>
      <PageHeader
        title="Hosts"
        description="Active compositions: a primary agent, its subagents, and workflows."
        actions={
          <Button
            variant="primary"
            onClick={() => setEditing({})}
            disabled={!agents.length}
          >
            New host
          </Button>
        }
      />
      <Async state={state} empty={(d) => !d.hosts?.length}>
        {(data) => (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {[...data.hosts]
              .sort((a, b) => a.host_id.localeCompare(b.host_id))
              .map((host) => (
                <HostCard
                  key={host.host_id}
                  host={host}
                  onEdit={setEditing}
                  onTest={setTesting}
                />
              ))}
          </div>
        )}
      </Async>

      <HostFormDrawer
        open={editing !== null}
        host={editing}
        agents={agents}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          state.reload();
        }}
      />
      <TestRequestDrawer
        open={testing !== null}
        host={testing}
        onClose={() => setTesting(null)}
      />
    </div>
  );
}
