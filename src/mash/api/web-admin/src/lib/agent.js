export function buildAgentUsage(hosts = [], workflows = []) {
  const usage = new Map();
  const add = (agentId, entry) => {
    if (!agentId) return;
    const entries = usage.get(agentId) || [];
    entries.push(entry);
    usage.set(agentId, entries);
  };

  for (const host of hosts) {
    add(host.primary, { type: 'host', id: host.host_id, role: 'primary' });
    for (const agentId of host.subagents || []) {
      add(agentId, { type: 'host', id: host.host_id, role: 'subagent' });
    }
  }

  for (const workflow of workflows) {
    for (const step of workflow.steps || []) {
      if (step.kind === 'agent' && step.agent_id) {
        add(step.agent_id, {
          type: 'workflow',
          id: workflow.workflow_id,
          role: step.step_id,
        });
      }
      for (const agentId of step.agent_ids || []) {
        add(agentId, {
          type: 'workflow',
          id: workflow.workflow_id,
          role: step.step_id,
        });
      }
    }
  }
  return usage;
}

export function agentAnchor(agentId) {
  return `agent-${encodeURIComponent(agentId)}`;
}
