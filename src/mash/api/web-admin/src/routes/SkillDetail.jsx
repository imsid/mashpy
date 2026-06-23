import { Link, useParams, useNavigate } from 'react-router-dom';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { PageHeader } from '../components/Page.jsx';
import { Async } from '../components/State.jsx';
import { Chip, Mono } from '../components/Chip.jsx';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

export default function SkillDetail() {
  const { agentId, skillName } = useParams();
  const navigate = useNavigate();
  const state = useApi(() => api.listSkills(), []);

  return (
    <div>
      <div className="mb-5 flex items-center gap-2 text-sm text-slate-500">
        <Link to="/skills" className="hover:text-slate-700">Skills</Link>
        <span>/</span>
        <span className="text-slate-700">{decodeURIComponent(skillName)}</span>
      </div>

      <Async state={state}>
        {(data) => {
          const entry = data.skills?.find(
            (s) =>
              s.agent_id === decodeURIComponent(agentId) &&
              s.skill.name === decodeURIComponent(skillName),
          );

          if (!entry) {
            return (
              <div className="rounded-lg border border-dashed border-slate-200 px-4 py-10 text-center text-sm text-slate-400">
                Skill not found.{' '}
                <button
                  onClick={() => navigate('/skills')}
                  className="underline hover:text-slate-600"
                >
                  Back to skills
                </button>
              </div>
            );
          }

          const { skill } = entry;

          return (
            <div className="max-w-2xl space-y-6">
              <div>
                <PageHeader title={skill.name} />
                {skill.description && (
                  <p className="text-sm text-slate-600">{skill.description}</p>
                )}
                <div className="mt-3 flex flex-wrap gap-2">
                  <div className="flex items-center gap-1.5">
                    <span className="text-xs text-slate-400">Agent</span>
                    <Link
                      to={`/logs?agent=${encodeURIComponent(entry.agent_id)}&tab=sessions`}
                    >
                      <Mono>{entry.agent_id}</Mono>
                    </Link>
                  </div>
                  <Chip tone="indigo">{skill.type}</Chip>
                </div>
              </div>

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
