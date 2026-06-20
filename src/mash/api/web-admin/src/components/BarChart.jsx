import { compactNumber } from '../lib/format.js';

// Minimal dependency-free bar chart. `data` is an array of
// `{ label, value }`; renders responsive SVG bars with a baseline and a
// peak gridline. Built by hand since the data is just buckets.
export function BarChart({ data, height = 160, format = compactNumber }) {
  if (!data?.length) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-slate-400">
        No data in this window.
      </div>
    );
  }

  const max = Math.max(1, ...data.map((d) => d.value));
  const barGap = 6;
  const viewW = 600;
  const plotH = height - 24; // leave room for x labels
  const barW = (viewW - barGap * (data.length - 1)) / data.length;

  return (
    <div className="flex w-full gap-2">
      <div
        className="flex flex-col justify-between py-0.5 text-right text-[10px] text-slate-400"
        style={{ height }}
      >
        <span>{format(max)}</span>
        <span className="pb-5">0</span>
      </div>
      <div className="min-w-0 flex-1">
        <svg
          viewBox={`0 0 ${viewW} ${height}`}
          preserveAspectRatio="none"
          className="h-40 w-full"
          role="img"
        >
          <line x1="0" y1={plotH} x2={viewW} y2={plotH} stroke="#e2e8f0" strokeWidth="1" />
          {data.map((d, i) => {
            const h = max > 0 ? (d.value / max) * (plotH - 4) : 0;
            const x = i * (barW + barGap);
            return (
              <rect
                key={i}
                x={x}
                y={plotH - h}
                width={barW}
                height={h}
                rx="2"
                className="fill-blue-500"
              >
                <title>{`${d.label}: ${format(d.value)}`}</title>
              </rect>
            );
          })}
        </svg>
        <div className="mt-1 flex justify-between text-[10px] text-slate-400">
          {data.map((d, i) => (
            <span key={i} className="flex-1 truncate text-center">
              {d.label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
