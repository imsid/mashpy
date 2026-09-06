// The filter row above a list. Every list route had hand-rolled this as
// `flex flex-wrap items-end gap-3` with fixed-width children (w-72, w-80,
// w-56, w-44, w-40), which overflow a phone viewport. Here the fields are a
// full-width column below `sm` and the wrapping row above it, so the desktop
// layout is unchanged and no route restates the rule.

export function FilterBar({ children, className = '' }) {
  return (
    <div className={`mb-3 flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end ${className}`}>
      {children}
    </div>
  );
}

// `width` is the desktop width only; the field is always full width below
// `sm`. Pass a `sm:w-*` class to widen a field that needs the room.
export function FilterField({ label, width = 'sm:w-56', children, className = '' }) {
  return (
    <label className={`block w-full ${width} ${className}`}>
      {label ? (
        <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      ) : null}
      {children}
    </label>
  );
}

// Trailing controls (refresh, run, an inline error). They sit on the filter
// baseline on desktop and wrap into their own row on a phone.
export function FilterActions({ children, className = '' }) {
  return (
    <div className={`flex flex-wrap items-center gap-2 sm:pb-0.5 ${className}`}>{children}</div>
  );
}
