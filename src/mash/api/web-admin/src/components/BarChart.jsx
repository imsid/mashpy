import { compactNumber } from '../lib/format.js';

// Grouped, dependency-free bar chart. One group per `data` row, one bar per
// `series` entry. Each series is scaled to its OWN max so metrics with very
// different magnitudes (trace counts vs token totals) stay legible side by side.
// `series` is `[{ key, label, barClass, dotClass }]`; `data` rows are keyed by
// `series.key`. Built by hand since the data is just buckets.
export function BarChart({ data, series, height = 180, format = compactNumber }) {
  if (!data?.length || !series?.length) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-slate-400">
        No data in this window.
      </div>
    );
  }

  const maxes = Object.fromEntries(
    series.map((s) => [s.key, Math.max(1, ...data.map((d) => d[s.key] || 0))]),
  );

  const viewW = 600;
  const plotH = height - 22; // leave room for x labels
  const groupGap = data.length > 20 ? 4 : 8;
  const barGap = 1.5;
  const groupW = (viewW - groupGap * (data.length - 1)) / data.length;
  const barW = (groupW - barGap * (series.length - 1)) / series.length;
  const labelStep = Math.ceil(data.length / 10);

  return (
    <div>
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {series.map((s) => (
          <span key={s.key} className="flex items-center gap-1.5 text-slate-500">
            <span className={`h-2.5 w-2.5 rounded-sm ${s.dotClass}`} />
            {s.label}
            <span className="text-slate-400">· peak {format(maxes[s.key])}</span>
          </span>
        ))}
      </div>
      <svg
        viewBox={`0 0 ${viewW} ${height}`}
        preserveAspectRatio="none"
        className="w-full"
        style={{ height }}
        role="img"
      >
        <line x1="0" y1={plotH} x2={viewW} y2={plotH} stroke="#e2e8f0" strokeWidth="1" />
        {data.map((d, i) =>
          series.map((s, j) => {
            const max = maxes[s.key];
            const value = d[s.key] || 0;
            const h = max > 0 ? (value / max) * (plotH - 4) : 0;
            const x = i * (groupW + groupGap) + j * (barW + barGap);
            return (
              <rect
                key={`${i}-${s.key}`}
                x={x}
                y={plotH - h}
                width={barW}
                height={h}
                rx="1.5"
                className={s.barClass}
              >
                <title>{`${d.label} · ${s.label}: ${format(value)}`}</title>
              </rect>
            );
          }),
        )}
      </svg>
      <div className="mt-1 flex text-[10px] text-slate-400">
        {data.map((d, i) => (
          <span key={i} className="flex-1 truncate text-center">
            {i % labelStep === 0 ? d.label : ''}
          </span>
        ))}
      </div>
    </div>
  );
}
