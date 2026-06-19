// Shared async-state rendering helpers, so every section handles loading,
// errors, and emptiness the same way.

export function Loading({ label = 'Loading…' }) {
  return <div className="py-10 text-center text-sm text-slate-400">{label}</div>;
}

export function ErrorState({ error, onRetry }) {
  const message = error?.message || 'Something went wrong.';
  const disabled = error?.code === 'OBSERVABILITY_DISABLED';
  return (
    <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-6 text-center">
      <p className="text-sm font-medium text-rose-700">{message}</p>
      {disabled ? (
        <p className="mt-1 text-xs text-rose-500">
          Telemetry endpoints are disabled for this deployment.
        </p>
      ) : null}
      {onRetry && !disabled ? (
        <button
          onClick={onRetry}
          className="mt-3 rounded-md border border-rose-300 bg-white px-3 py-1.5 text-xs font-medium text-rose-700 hover:bg-rose-100"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function Empty({ children = 'Nothing here yet.' }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-200 px-4 py-10 text-center text-sm text-slate-400">
      {children}
    </div>
  );
}

// Render children once `state` has resolved, otherwise the right placeholder.
export function Async({ state, children, empty }) {
  if (state.loading && !state.data) return <Loading />;
  if (state.error) return <ErrorState error={state.error} onRetry={state.reload} />;
  if (empty && empty(state.data)) return <Empty />;
  return children(state.data);
}
