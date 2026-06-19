const TONES = {
  slate: 'bg-slate-100 text-slate-600',
  indigo: 'bg-indigo-50 text-indigo-700',
  emerald: 'bg-emerald-50 text-emerald-700',
  amber: 'bg-amber-50 text-amber-700',
  rose: 'bg-rose-50 text-rose-700',
};

export function Chip({ children, tone = 'slate', className = '' }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${TONES[tone] || TONES.slate} ${className}`}
    >
      {children}
    </span>
  );
}

export function Mono({ children, className = '' }) {
  return (
    <code className={`rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-600 ${className}`}>
      {children}
    </code>
  );
}
