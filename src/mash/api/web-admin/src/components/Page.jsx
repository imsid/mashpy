export function PageHeader({ title, description, actions }) {
  return (
    <div className="mb-5 flex items-start justify-between gap-4">
      <div>
        <h1 className="font-display text-2xl font-semibold tracking-tight">{title}</h1>
        {description ? (
          <p className="mt-1 text-sm text-slate-500">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function Card({ children, className = '' }) {
  return (
    <div className={`rounded-lg border border-slate-200 bg-white ${className}`}>
      {children}
    </div>
  );
}
