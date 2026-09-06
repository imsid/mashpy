import { useEffect } from 'react';

import { useScrollLock } from '../lib/useMediaQuery.js';

// Slide-over panel. Below `sm` it is a full-screen sheet with a sticky header
// and footer, since a phone has no room for a panel beside the page. From
// `sm` it is the right-side panel it has always been. The footer respects the
// bottom safe area so its actions clear the home indicator.
export function Drawer({ open, onClose, title, subtitle, children, footer }) {
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') onClose?.();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  useScrollLock(open);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-40">
      <div
        className="absolute inset-0 bg-slate-900/20"
        onClick={onClose}
        aria-hidden
      />
      <aside className="absolute inset-0 flex flex-col bg-white shadow-xl sm:left-auto sm:right-0 sm:top-0 sm:h-full sm:w-full sm:max-w-2xl">
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-slate-200 px-4 py-3 pt-[max(0.75rem,env(safe-area-inset-top))] sm:px-5 sm:py-4 sm:pt-4">
          <div className="min-w-0">
            <h2 className="truncate font-display text-base font-semibold">{title}</h2>
            {subtitle ? (
              <p className="mt-0.5 truncate text-xs text-slate-500">{subtitle}</p>
            ) : null}
          </div>
          <button
            onClick={onClose}
            className="-mr-2 -mt-1 flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-700 sm:-mt-0.5 sm:h-9 sm:w-9"
            aria-label="Close"
          >
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">{children}</div>
        {footer ? (
          <div className="shrink-0 border-t border-slate-200 px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-5 sm:pb-3">
            {footer}
          </div>
        ) : null}
      </aside>
    </div>
  );
}
