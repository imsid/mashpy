import { Link, useParams, useNavigate } from 'react-router-dom';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { PageHeader } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

export default function SkillDetail() {
  const { skillName } = useParams();
  const navigate = useNavigate();
  const skillsState = useApi(() => api.listSkills(), []);
  const countsState = useApi(() => api.listSkillInvocations(), []);

  return (
    <div>
      <div className="mb-5 flex items-center gap-2 text-sm text-slate-500">
        <Link to="/skills" className="hover:text-slate-700">Skills</Link>
        <span>/</span>
        <span className="text-slate-700">{decodeURIComponent(skillName)}</span>
      </div>

      <Async state={skillsState}>
        {(data) => {
          const decoded = decodeURIComponent(skillName);
          const entry = data.skills?.find((s) => s.skill.name === decoded);

          if (!entry) {
            return (
              <div className="rounded-lg border border-dashed border-slate-200 px-4 py-10 text-center text-sm text-slate-400">
                Skill not found.{' '}
                <button onClick={() => navigate('/skills')} className="underline hover:text-slate-600">
                  Back to skills
                </button>
              </div>
            );
          }

          const { skill, agents } = entry;
          const counts = countsState.data?.invocations?.find((i) => i.skill_name === skill.name);

          return (
            <div className="max-w-2xl space-y-6">
              <div>
                <PageHeader title={skill.name} />
                {skill.description && (
                  <p className="text-sm text-slate-600">{skill.description}</p>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <Chip tone="indigo">{skill.type}</Chip>
                  {agents.map((agentId) => (
                    <div key={agentId} className="flex items-center gap-1.5">
                      <span className="text-xs text-slate-400">agent</span>
                      <Link to={`/logs?agent=${encodeURIComponent(agentId)}&tab=sessions`}>
                        <Mono>{agentId}</Mono>
                      </Link>
                    </div>
                  ))}
                </div>
              </div>

              {counts && (
                <div className="rounded-lg border border-slate-200 px-4 py-3">
                  <div className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Invocations</div>
                  <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
                    <span className="text-lg font-semibold text-slate-800">{counts.total.toLocaleString()} total</span>
                    {Object.entries(counts.by_agent).map(([agentId, count]) => (
                      <span key={agentId} className="text-sm text-slate-500">
                        {agentId}: {count.toLocaleString()}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {skill.content ? (
                <div>
                  <h2 className="mb-3 text-sm font-semibold text-slate-700">Content</h2>
                  <div className="prose prose-sm prose-slate max-w-none rounded-lg border border-slate-200 bg-white px-5 py-4">
                    <Markdown remarkPlugins={[remarkGfm]}>{skill.content}</Markdown>
                  </div>
                </div>
              ) : (
                <div className="rounded-lg border border-dashed border-slate-200 px-4 py-6 text-center text-sm text-slate-400">
                  Skill content is loaded from the filesystem and not available here.
                </div>
              )}
            </div>
          );
        }}
      </Async>
    </div>
  );
}
