// The tab strip above a sectioned page. `tabs` is `[{ id, label, count? }]`.
// Four places carried an identical `flex gap-1 border-b` strip; this is that
// strip, made scrollable so a set of tabs wider than the viewport can be
// reached instead of being clipped. It bleeds to the screen edges below `sm`
// so the scroll affordance reads as part of the page.
export function Tabs({ tabs, value, onChange, className = '' }) {
  return (
    <div
      className={`no-scrollbar -mx-4 mb-4 flex gap-1 overflow-x-auto border-b border-slate-200 px-4 sm:mx-0 sm:px-0 ${className}`}
      role="tablist"
    >
      {tabs.map((tab) => {
        const active = tab.id === value;
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(tab.id)}
            className={`-mb-px shrink-0 whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-medium capitalize transition ${
              active
                ? 'border-slate-900 text-slate-900'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {tab.label}
            {tab.count ? <span className="ml-1.5 text-xs text-slate-400">({tab.count})</span> : null}
          </button>
        );
      })}
    </div>
  );
}
