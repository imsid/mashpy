export function PageHeader({ title, description, actions }) {
  return (
    <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <div className="min-w-0">
        <h1 className="font-display text-xl font-semibold tracking-tight sm:text-2xl">{title}</h1>
        {description ? (
          <p className="mt-1 text-sm text-slate-500">{description}</p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex flex-wrap items-center gap-2 sm:shrink-0">{actions}</div>
      ) : null}
    </div>
  );
}

import { Link } from 'react-router-dom';

// A surface. Pass `to` (or `onClick`) to make it an interactive card: it lifts
// on hover, deepens its shadow, and exposes a focus ring for keyboard users.
export function Card({ children, className = '', to, onClick }) {
  const interactive = Boolean(to || onClick);
  const base = 'rounded-lg border border-slate-200 bg-white overflow-hidden';
  const motion = interactive
    ? 'group block text-left transition duration-200 ease-out hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-lg hover:shadow-slate-200/70 active:translate-y-0 active:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-900/10 focus-visible:ring-offset-2'
    : '';
  const cls = `${base} ${motion} ${className}`;

  if (to) {
    return (
      <Link to={to} className={cls}>
        {children}
      </Link>
    );
  }
  if (onClick) {
    return (
      <button type="button" onClick={onClick} className={`${cls} w-full`}>
        {children}
      </button>
    );
  }
  return <div className={cls}>{children}</div>;
}
