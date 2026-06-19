import { useEffect } from 'react';

// Right-side slide-over panel. Renders nothing when `open` is false.
export function Drawer({ open, onClose, title, subtitle, children, footer }) {
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') onClose?.();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-20">
      <div
        className="absolute inset-0 bg-slate-900/20"
        onClick={onClose}
        aria-hidden
      />
      <aside className="absolute right-0 top-0 flex h-full w-full max-w-2xl flex-col bg-white shadow-xl">
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div className="min-w-0">
            <h2 className="font-display text-base font-semibold">{title}</h2>
            {subtitle ? (
              <p className="mt-0.5 truncate text-xs text-slate-500">{subtitle}</p>
            ) : null}
          </div>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
            aria-label="Close"
          >
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer ? (
          <div className="border-t border-slate-200 px-5 py-3">{footer}</div>
        ) : null}
      </aside>
    </div>
  );
}
