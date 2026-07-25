import { NavLink, Outlet } from 'react-router-dom';

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

export default function Shell() {
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-slate-200 bg-white px-5 py-3">
        <div className="flex items-center gap-2.5">
          <span className="font-display text-lg font-semibold tracking-tight">Mash</span>
          <span className="text-sm text-slate-400">Admin</span>
        </div>
      </header>
      <div className="mx-auto flex max-w-7xl">
        <nav className="w-48 shrink-0 px-3 py-5">
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
                      className={({ isActive }) =>
                        `flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition ${
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
        </nav>
        <main className="min-w-0 flex-1 px-6 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
