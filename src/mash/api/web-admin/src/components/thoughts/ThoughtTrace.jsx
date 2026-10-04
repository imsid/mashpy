import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Chip } from '../Chip.jsx';
import { CopyId } from '../CopyId.jsx';
import { api } from '../../lib/api.js';
import { formatTime } from '../../lib/format.js';
import { remarkThoughts, thoughtLogsPath } from '../../lib/thoughts.js';
import './thoughts.css';

function Summary({ summary, activeId, onSelect }) {
  const plugins = useMemo(() => [remarkGfm, [remarkThoughts, { prefix: summary.event_id }]], [summary.event_id]);
  return (
    <section className="thoughts-summary" aria-label={`Thought summary at ${formatTime(summary.created_at)}`}>
      <div className="thoughts-summary-time">{formatTime(summary.created_at)}</div>
      <ReactMarkdown remarkPlugins={plugins} components={{
        button: ({ node, children, ...props }) => (
          <button type="button" className="thoughts-phrase"
            data-phrase-id={props['data-phrase-id']}
            aria-pressed={activeId === props['data-phrase-id']}
            onClick={() => onSelect(props['data-phrase-id'], props['data-expression'])}>
            {children}
          </button>
        ),
        a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
        img: ({ alt, src }) => <span>{`![${alt || ''}](${src || ''})`}</span>,
      }}>{summary.thought_summary}</ReactMarkdown>
    </section>
  );
}

export function ThoughtTrace({ trace }) {
  const [active, setActive] = useState({ id: null, expression: 'focused' });
  const [top, setTop] = useState(0);
  const body = useRef(null);
  const companion = useRef(null);
  useEffect(() => {
    const measure = () => {
      if (!body.current || !companion.current) return;
      const selected = Array.from(body.current.querySelectorAll('[data-phrase-id]'))
        .find((element) => element.dataset.phraseId === active.id);
      if (!selected) return;
      const offset = selected.getBoundingClientRect().top - body.current.getBoundingClientRect().top - 25;
      setTop(Math.max(0, Math.min(offset, body.current.offsetHeight - companion.current.offsetHeight)));
    };
    const observer = new ResizeObserver(measure);
    observer.observe(body.current);
    measure();
    return () => observer.disconnect();
  }, [active.id, trace.summaries]);

  const select = (id, expression) => {
    setActive({ id, expression });
  };
  return (
    <article className="thoughts-trace" aria-label={`Thoughts for trace ${trace.trace_id}`}>
      <header className="thoughts-trace-header">
        <div className="thoughts-trace-meta">
          <Chip>{trace.agent_id}</Chip>
          <CopyId value={trace.trace_id} />
          <span>{formatTime(trace.latest_thought_at)}</span>
        </div>
        <Link to={thoughtLogsPath(trace)} className="text-xs font-medium text-indigo-600 hover:underline">View logs →</Link>
      </header>
      <div ref={body} className="thoughts-trace-body">
        <aside ref={companion} className="thoughts-companion" style={{ transform: `translateY(${top}px)` }} aria-label="Mushy">
          <picture>
            <source media="(prefers-reduced-motion: reduce)" srcSet={api.thoughtExpressionUrl(active.expression, 'png')} />
            <img src={api.thoughtExpressionUrl(active.expression)} alt={`Mushy: ${active.expression}`} loading="lazy" />
          </picture>
        </aside>
        <div className="thoughts-text">
          {trace.summaries.map((summary) => <Summary key={summary.event_id} summary={summary} activeId={active.id} onSelect={select} />)}
        </div>
      </div>
    </article>
  );
}
