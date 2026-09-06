import { useState } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { TextInput, Select } from '../components/Form.jsx';
import { FilterBar, FilterField } from '../components/Filters.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';
import { formatTime } from '../lib/format.js';

const RANGES = [
  { id: '7', label: 'Last 7 days', days: 7 },
  { id: '30', label: 'Last 30 days', days: 30 },
  { id: '0', label: 'All time', days: null },
];

export default function Feedback() {
  const agentsState = useApi(() => api.listAgents(), []);
  const agents = agentsState.data?.agents || [];
  const [agentId, setAgentId] = useState('');
  const [rangeId, setRangeId] = useState('7');
  const [query, setQuery] = useState('');

  const resolvedAgent = agentId || agents[0]?.agent_id || '';
  const range = RANGES.find((r) => r.id === rangeId);
  const after = range.days ? Math.floor(Date.now() / 1000) - range.days * 86400 : 0;

  const state = useApi(
    () =>
      resolvedAgent
        ? api.listFeedback({ agent_id: resolvedAgent, after, q: query.trim() || undefined })
        : Promise.resolve({ feedback: [] }),
    [resolvedAgent, after, query],
  );

  return (
    <div>
      <PageHeader
        title="Feedback"
        description="Notes captured via the REPL /feedback command."
      />

      <FilterBar className="mb-4">
        <FilterField label="Agent">
          <Select value={resolvedAgent} onChange={(e) => setAgentId(e.target.value)}>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.metadata?.display_name || a.agent_id}
              </option>
            ))}
          </Select>
        </FilterField>
        <FilterField label="Range" width="sm:w-40">
          <Select value={rangeId} onChange={(e) => setRangeId(e.target.value)}>
            {RANGES.map((r) => (
              <option key={r.id} value={r.id}>
                {r.label}
              </option>
            ))}
          </Select>
        </FilterField>
        <FilterField label="Search" width="sm:flex-1">
          <TextInput
            value={query}
            placeholder="Filter messages…"
            onChange={(e) => setQuery(e.target.value)}
          />
        </FilterField>
      </FilterBar>

      <Async state={state} empty={(d) => !d.feedback?.length}>
        {(data) => (
          <div className="space-y-2">
            {data.feedback.map((f) => (
              <Card key={f.feedback_id} className="p-3">
                <p className="text-sm text-slate-700">{f.message}</p>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                  <span>{formatTime(f.created_at)}</span>
                  {f.host_id ? <Chip>{f.host_id}</Chip> : null}
                  {f.session_id ? (
                    <Link
                      to={`/logs?tab=sessions&agent=${encodeURIComponent(resolvedAgent)}&session=${encodeURIComponent(f.session_id)}`}
                      className="inline-flex items-center gap-1 hover:text-slate-600"
                    >
                      session <Mono>{f.session_id}</Mono>
                    </Link>
                  ) : null}
                </div>
              </Card>
            ))}
          </div>
        )}
      </Async>
    </div>
  );
}
