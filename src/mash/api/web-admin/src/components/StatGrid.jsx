import { Card } from './Page.jsx';

// A row of headline numbers. Four places rendered their own version of this
// with an unconditional grid (grid-cols-3 on Overview, grid-cols-4 on the
// trace drawer), none of which survive a phone. The column rule is decided
// here once: two across on a phone, then up to the caller's count.
//
// Tailwind scans for literal class strings, so the variants are spelled out
// rather than built from `columns`.
const COLUMNS = {
  2: 'grid-cols-2',
  3: 'grid-cols-2 sm:grid-cols-3',
  4: 'grid-cols-2 sm:grid-cols-3 lg:grid-cols-4',
};

export function StatGrid({ children, columns = 4, className = '' }) {
  return (
    <div className={`grid gap-3 ${COLUMNS[columns] || COLUMNS[4]} ${className}`}>{children}</div>
  );
}

// The headline variant: a large number in its own card, optionally a link.
export function Stat({ label, value, hint, to }) {
  return (
    <Card to={to} className="px-4 py-3">
      <div className="flex items-start justify-between gap-2">
        <div className="truncate text-xl font-semibold tabular-nums sm:text-2xl">{value}</div>
        {to ? (
          <span className="text-slate-300 transition group-hover:translate-x-0.5 group-hover:text-slate-500">
            →
          </span>
        ) : null}
      </div>
      <div className="mt-0.5 text-xs font-medium uppercase tracking-wide text-slate-400">
        {label}
      </div>
      {hint ? <div className="mt-0.5 text-xs text-slate-400">{hint}</div> : null}
    </Card>
  );
}

// The compact variant, for dense contexts like a drawer: label above a small
// value, hint on the title attribute.
export function StatTile({ label, value, hint }) {
  return (
    <div className="rounded-md border border-slate-200 px-3 py-2" title={hint}>
      <div className="text-xs text-slate-400">{label}</div>
      <div className="mt-0.5 text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}
