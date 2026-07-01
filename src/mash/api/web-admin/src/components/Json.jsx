import { useState } from 'react';
import { CopyButton } from './CopyId.jsx';

function stringify(value) {
  if (typeof value === 'string') {
    // Render JSON strings as parsed objects when possible.
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function JsonBlock({ value, className = '' }) {
  const text = stringify(value);
  return (
    <div className={`group relative ${className}`}>
      <pre className="max-h-96 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 font-mono text-xs leading-relaxed text-slate-800">
        {text}
      </pre>
      <div className="absolute right-1.5 top-1.5 opacity-0 transition-opacity group-hover:opacity-100">
        <CopyButton getValue={() => text} className="hover:bg-slate-200" />
      </div>
    </div>
  );
}

// Collapsible labelled section, used for raw payloads in the trace drawer.
export function Disclosure({ label, hint, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-md border border-slate-200">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm font-medium text-slate-700"
      >
        <span className={`text-slate-400 transition ${open ? 'rotate-90' : ''}`}>›</span>
        {label}
        {hint ? <span className="ml-1 text-xs font-normal text-slate-400">{hint}</span> : null}
      </button>
      {open ? <div className="border-t border-slate-100 p-3">{children}</div> : null}
    </div>
  );
}
