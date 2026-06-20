import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// Renders message text as GitHub-flavored markdown. Code uses a light
// background to match JsonBlock; no raw HTML is rendered (react-markdown
// escapes it by default), so untrusted message content is safe.
const COMPONENTS = {
  p: ({ children }) => <p className="mb-2 text-sm leading-relaxed text-slate-700 last:mb-0">{children}</p>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noreferrer" className="text-indigo-600 underline">
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="mb-2 list-disc pl-5 text-sm text-slate-700 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="mb-2 list-decimal pl-5 text-sm text-slate-700 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="mb-0.5">{children}</li>,
  h1: ({ children }) => <h1 className="mb-2 mt-1 text-base font-semibold text-slate-800">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-2 mt-1 text-sm font-semibold text-slate-800">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-1 mt-1 text-sm font-semibold text-slate-800">{children}</h3>,
  blockquote: ({ children }) => (
    <blockquote className="mb-2 border-l-2 border-slate-200 pl-3 text-sm italic text-slate-500">
      {children}
    </blockquote>
  ),
  code: ({ inline, children }) =>
    inline ? (
      <code className="rounded border border-slate-200 bg-slate-50 px-1 py-0.5 font-mono text-xs text-slate-800">
        {children}
      </code>
    ) : (
      <code className="font-mono text-xs text-slate-800">{children}</code>
    ),
  pre: ({ children }) => (
    <pre className="mb-2 max-h-96 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 text-xs leading-relaxed text-slate-800 last:mb-0">
      {children}
    </pre>
  ),
  table: ({ children }) => (
    <div className="mb-2 overflow-x-auto">
      <table className="text-sm text-slate-700">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border border-slate-200 px-2 py-1 text-left font-medium">{children}</th>,
  td: ({ children }) => <td className="border border-slate-200 px-2 py-1">{children}</td>,
};

export function Markdown({ children }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
      {children || ''}
    </ReactMarkdown>
  );
}
