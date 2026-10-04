import { useSearchParams } from 'react-router-dom';
import { PageHeader } from '../components/Page.jsx';
import { Async, Empty } from '../components/State.jsx';
import { FilterBar, FilterField, FilterActions } from '../components/Filters.jsx';
import { Select, Button } from '../components/Form.jsx';
import { ThoughtTrace } from '../components/thoughts/ThoughtTrace.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

function ThoughtFeed({ agentId, traceId }) {
  const state = useApi(() => api.listThoughts({ agent_id: agentId, trace_id: traceId }), [agentId, traceId]);
  return (
    <>
      <FilterBar>
        <FilterActions>
          <Button variant="ghost" onClick={state.reload} disabled={state.loading}>↻ Refresh</Button>
          <span className="text-xs text-slate-500">{traceId ? 'Selected trace' : 'Latest 50 traces with thoughts'} · original wording in full</span>
        </FilterActions>
      </FilterBar>
      <Async state={state}>
        {(data) => data.traces.length ? (
          <div className="thoughts-feed">
            {data.traces.map((trace) => <ThoughtTrace key={`${trace.agent_id}:${trace.trace_id}`} trace={trace} />)}
          </div>
        ) : <Empty>No thought summaries recorded{traceId ? ' for this trace' : agentId ? ' for this agent' : ''} yet.</Empty>}
      </Async>
    </>
  );
}

export default function Thoughts() {
  const [params, setParams] = useSearchParams();
  const agentsState = useApi(() => api.listAgents(), []);
  const agentId = params.get('agent') || '';
  const traceId = params.get('trace') || '';
  const selectAgent = (value) => {
    const next = new URLSearchParams(params);
    if (value) next.set('agent', value); else next.delete('agent');
    next.delete('trace');
    setParams(next, { replace: true });
  };
  return (
    <div>
      <PageHeader title="Thoughts" description="The agent's own words, with Mushy alongside. Select an underlined phrase to see its expression." />
      <Async state={agentsState}>
        {(data) => <FilterBar><FilterField label="Agent">
          <Select value={agentId} onChange={(event) => selectAgent(event.target.value)}>
            <option value="">All agents</option>
            {agentId && !data.agents.some((a) => a.agent_id === agentId) ? <option value={agentId}>{agentId}</option> : null}
            {data.agents.map((agent) => <option key={agent.agent_id} value={agent.agent_id}>{agent.metadata?.display_name || agent.agent_id}</option>)}
          </Select>
        </FilterField></FilterBar>}
      </Async>
      {traceId ? (
        <FilterBar>
          <FilterActions>
            <span className="break-all text-xs text-slate-500">Trace: {traceId}</span>
            <Button variant="ghost" onClick={() => selectAgent(agentId)}>Show latest thoughts</Button>
          </FilterActions>
        </FilterBar>
      ) : null}
      <ThoughtFeed key={`${agentId}:${traceId}`} agentId={agentId} traceId={traceId} />
    </div>
  );
}
