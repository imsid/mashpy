import { useState } from 'react';

const CopyIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="9" y="9" width="13" height="13" rx="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </svg>
);

// Standalone copy button. Pass `getValue` (called on click) or `value` (string).
export function CopyButton({ getValue, value, className = '' }) {
  const [copied, setCopied] = useState(false);
  const copy = async (e) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(getValue ? getValue() : String(value));
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard unavailable */
    }
  };
  return (
    <button
      type="button"
      onClick={copy}
      title="Copy to clipboard"
      className={`shrink-0 rounded p-0.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600 ${className}`}
    >
      {copied ? <span className="text-xs text-emerald-600">✓</span> : <CopyIcon />}
    </button>
  );
}

// Full, untruncated identifier with a copy-to-clipboard affordance. Ids
// (session_id, trace_id) are never truncated so they can be copied verbatim.
export function CopyId({ value, className = '' }) {
  if (!value) return <span className="text-slate-300">—</span>;
  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`}>
      <code className="break-all font-mono text-xs text-slate-600">{value}</code>
      <CopyButton value={value} />
    </span>
  );
}
