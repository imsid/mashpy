import { Link } from 'react-router-dom';
import { PageHeader, Card } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

function SkillCard({ agentId, skill }) {
  return (
    <Card
      to={`/skills/${encodeURIComponent(agentId)}/${encodeURIComponent(skill.name)}`}
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

      <div className="mt-auto border-t border-slate-100 pt-3">
        <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">
          Agent
        </div>
        <Link
          to={`/logs?agent=${encodeURIComponent(agentId)}&tab=sessions`}
          onClick={(e) => e.stopPropagation()}
          className="inline-block"
        >
          <Mono>{agentId}</Mono>
        </Link>
      </div>
    </Card>
  );
}

export default function Skills() {
  const state = useApi(() => api.listSkills(), []);

  return (
    <div>
      <PageHeader
        title="Skills"
        description="All skills registered across the agent pool."
      />
      <Async state={state} empty={(d) => !d.skills?.length}>
        {(data) => {
          const skills = [...data.skills].sort((a, b) =>
            a.skill.name.localeCompare(b.skill.name),
          );
          return (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {skills.map((entry) => (
                <SkillCard
                  key={`${entry.agent_id}:${entry.skill.name}`}
                  agentId={entry.agent_id}
                  skill={entry.skill}
                />
              ))}
            </div>
          );
        }}
      </Async>
    </div>
  );
}
