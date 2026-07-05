import { NavLink, Outlet } from 'react-router-dom';
import { api } from '../lib/api.js';
import { useApi } from '../lib/useApi.js';

const NAV = [
  { to: '/', label: 'Overview', end: true },
  { to: '/agents', label: 'Agents' },
  { to: '/tools', label: 'Tools' },
  { to: '/skills', label: 'Skills' },
  { to: '/workflows', label: 'Workflows' },
  { to: '/hosts', label: 'Hosts' },
  { to: '/logs', label: 'Logs' },
  { to: '/feedback', label: 'Feedback' },
  { to: '/evals', label: 'Evals' },
  { to: '/reference', label: 'Reference', divider: true },
];

function ObservabilityBadge() {
  const { data, error } = useApi(() => api.health(), []);
  const enabled = data?.observability?.enabled;
  const label = error
    ? 'API unreachable'
    : data
      ? enabled
        ? 'Observability on'
        : 'Observability off'
      : 'Checking…';
  const tone = error
    ? 'bg-rose-50 text-rose-700'
    : enabled
      ? 'bg-emerald-50 text-emerald-700'
      : 'bg-slate-100 text-slate-500';
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${tone}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
      {label}
    </span>
  );
}

export default function Shell() {
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-slate-200 bg-white px-5 py-3">
        <div className="flex items-center gap-2.5">
          <span className="font-display text-lg font-semibold tracking-tight">Mash</span>
          <span className="text-sm text-slate-400">Admin</span>
        </div>
        <ObservabilityBadge />
      </header>
      <div className="mx-auto flex max-w-7xl">
        <nav className="w-48 shrink-0 px-3 py-5">
          <ul className="space-y-0.5">
            {NAV.map((item) => (
              <li key={item.to} className={item.divider ? 'mt-2 border-t border-slate-100 pt-2' : undefined}>
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
        </nav>
        <main className="min-w-0 flex-1 px-6 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
