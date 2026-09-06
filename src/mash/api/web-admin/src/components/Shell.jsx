import { useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';

import { useBreakpoint, useScrollLock } from '../lib/useMediaQuery.js';

// Grouped by the domain model: the deployed pool and its compositions,
// then runtime activity, then docs. Agents and Workflows precede Hosts
// because a host composes them.
const NAV = [
  {
    items: [{ to: '/', label: 'Overview', end: true }],
  },
  {
    label: 'Deployment',
    items: [
      { to: '/agents', label: 'Agents' },
      { to: '/workflows', label: 'Workflows' },
      { to: '/hosts', label: 'Hosts' },
      { to: '/tools', label: 'Tools' },
      { to: '/skills', label: 'Skills' },
    ],
  },
  {
    label: 'Activity',
    items: [
      { to: '/logs', label: 'Logs' },
      { to: '/feedback', label: 'Feedback' },
      { to: '/evals', label: 'Evals' },
    ],
  },
  {
    divider: true,
    items: [{ to: '/reference', label: 'Reference' }],
  },
];

// The nav list itself, rendered twice: pinned in the sidebar at `lg`, and
// inside the overlay drawer below it. Same markup both times so the two can
// never drift.
function SidebarNav({ onNavigate }) {
  return (
    <>
      {NAV.map((group, i) => (
        <div
          key={group.label || i}
          className={group.divider ? 'mt-2 border-t border-slate-100 pt-2' : i > 0 ? 'mt-4' : undefined}
        >
          {group.label ? (
            <div className="px-3 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              {group.label}
            </div>
          ) : null}
          <ul className="space-y-0.5">
            {group.items.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    `flex min-h-11 items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition lg:min-h-0 ${
                      isActive
                        ? 'bg-slate-100 text-slate-900'
                        : 'text-slate-600 hover:bg-slate-50'
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </>
  );
}

function MenuIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.75" aria-hidden>
      <path d="M3 5.5h14M3 10h14M3 14.5h14" strokeLinecap="round" />
    </svg>
  );
}

export default function Shell() {
  const isDesktop = useBreakpoint('lg');
  const [navOpen, setNavOpen] = useState(false);
  const { pathname } = useLocation();

  // The drawer only exists below `lg`; widening the window past the sidebar
  // breakpoint dismisses it rather than leaving an orphaned overlay.
  useEffect(() => {
    if (isDesktop) setNavOpen(false);
  }, [isDesktop]);

  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!navOpen) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') setNavOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navOpen]);

  useScrollLock(navOpen);

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 flex items-center gap-2 border-b border-slate-200 bg-white px-3 py-2.5 pt-[max(0.625rem,env(safe-area-inset-top))] sm:px-5 sm:py-3">
        <button
          type="button"
          onClick={() => setNavOpen(true)}
          className="-ml-1 flex h-11 w-11 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 hover:text-slate-900 lg:hidden"
          aria-label="Open navigation"
          aria-expanded={navOpen}
        >
          <MenuIcon />
        </button>
        <div className="flex items-center gap-2.5">
          <span className="font-display text-lg font-semibold tracking-tight">Mash</span>
          <span className="text-sm text-slate-400">Admin</span>
        </div>
      </header>

      {navOpen ? (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-slate-900/30"
            onClick={() => setNavOpen(false)}
            aria-hidden
          />
          <nav className="absolute inset-y-0 left-0 flex w-64 max-w-[80%] flex-col overflow-y-auto bg-white px-3 pb-[max(1rem,env(safe-area-inset-bottom))] pt-[max(0.75rem,env(safe-area-inset-top))] shadow-xl">
            <div className="mb-2 flex items-center justify-between px-3">
              <span className="font-display text-base font-semibold tracking-tight">Mash</span>
              <button
                type="button"
                onClick={() => setNavOpen(false)}
                className="-mr-2 flex h-11 w-11 items-center justify-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                aria-label="Close navigation"
              >
                ✕
              </button>
            </div>
            <SidebarNav onNavigate={() => setNavOpen(false)} />
          </nav>
        </div>
      ) : null}

      <div className="mx-auto flex max-w-7xl">
        <nav className="hidden w-48 shrink-0 px-3 py-5 lg:block">
          <SidebarNav />
        </nav>
        <main className="min-w-0 flex-1 px-4 py-5 pb-[max(1.25rem,env(safe-area-inset-bottom))] sm:px-6 sm:py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
