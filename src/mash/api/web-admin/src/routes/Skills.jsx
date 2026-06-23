import { Link } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

function InvocationCount({ skillName, invocations }) {
  if (!invocations) return null;
  const entry = invocations.find((i) => i.skill_name === skillName);
  if (!entry) return null;
  const byAgent = Object.entries(entry.by_agent || {});
  return (
    <div className="mt-auto border-t border-slate-100 pt-3">
      <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">
        Invocations
      </div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-sm font-semibold text-slate-800">{entry.total.toLocaleString()}</span>
        {byAgent.length > 1 &&
          byAgent.map(([agentId, count]) => (
            <span key={agentId} className="text-xs text-slate-500">
              {agentId}: {count.toLocaleString()}
            </span>
          ))}
      </div>
    </div>
  );
}

function SkillCard({ entry, invocations }) {
  const { skill, agents } = entry;
  return (
    <Card
      to={`/skills/${encodeURIComponent(skill.name)}`}
      className="flex h-full flex-col gap-3 p-4"
    >
      <div>
        <div className="flex items-center justify-between gap-2">
          <h3 className="font-display text-base font-semibold">{skill.name}</h3>
          <Chip tone="indigo">{skill.type}</Chip>
        </div>
        {skill.description ? (
          <p className="mt-1 text-sm text-slate-600">{skill.description}</p>
        ) : (
          <p className="mt-1 text-sm italic text-slate-400">No description.</p>
        )}
      </div>

      <div className="flex flex-wrap gap-1.5">
        {agents.map((agentId) => (
          <Link
            key={agentId}
            to={`/logs?agent=${encodeURIComponent(agentId)}&tab=sessions`}
            onClick={(e) => e.stopPropagation()}
            className="inline-block"
          >
            <Mono>{agentId}</Mono>
          </Link>
        ))}
      </div>

      <InvocationCount skillName={skill.name} invocations={invocations} />
    </Card>
  );
}

export default function Skills() {
  const skillsState = useApi(() => api.listSkills(), []);
  const countsState = useApi(() => api.listSkillInvocations(), []);

  return (
    <div>
      <PageHeader
        title="Skills"
        description="All skills registered across the agent pool."
      />
      <Async state={skillsState} empty={(d) => !d.skills?.length}>
        {(data) => {
          const skills = [...data.skills].sort((a, b) =>
            a.skill.name.localeCompare(b.skill.name),
          );
          const invocations = countsState.data?.invocations ?? null;
          return (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {skills.map((entry) => (
                <SkillCard
                  key={entry.skill.name}
                  entry={entry}
                  invocations={invocations}
                />
              ))}
            </div>
          );
        }}
      </Async>
    </div>
  );
}
