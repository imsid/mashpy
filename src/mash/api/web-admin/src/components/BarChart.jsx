import { useEffect, useRef, useState } from 'react';

import { compactNumber } from '../lib/format.js';

// A bar narrower than this reads as a sliver rather than a value, so the
// chart drops trailing buckets instead of drawing them. This is a floor, not
// a target: a caller that labels its own window (Overview says "Last N days")
// should narrow the series it passes, so this only catches containers too
// small for whatever it was handed.
const MIN_BAR_WIDTH = 6;
// Roughly the width of a "9/14" tick label plus breathing room.
const LABEL_WIDTH = 34;
const BAR_GAP = 1.5;
// Used only to size the window; the drawn gap depends on how many groups
// survive, which is what we are solving for here.
const NOMINAL_GROUP_GAP = 6;

// How many groups fit at a legible bar width.
function windowCapacity(width, seriesCount) {
  const perGroup =
    seriesCount * MIN_BAR_WIDTH + (seriesCount - 1) * BAR_GAP + NOMINAL_GROUP_GAP;
  return Math.max(1, Math.floor(width / perGroup));
}

// Track the rendered width of an element. The chart draws in real pixels, so
// it needs the number rather than a percentage.
function useElementWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect?.width;
      if (next) setWidth(next);
    });
    observer.observe(node);
    setWidth(node.getBoundingClientRect().width);
    return () => observer.disconnect();
  }, []);

  return [ref, width];
}

// Grouped, dependency-free bar chart. One group per `data` row, one bar per
// `series` entry. Each series is scaled to its OWN max so metrics with very
// different magnitudes (trace counts vs token totals) stay legible side by
// side. `series` is `[{ key, label, barClass, dotClass }]`; `data` rows are
// keyed by `series.key`. Built by hand since the data is just buckets.
//
// The svg is measured rather than stretched: a fixed viewBox scaled with
// `preserveAspectRatio="none"` distorted the bars and their corner radii on
// every width but one. Bar count and label density both come from the
// measured width, so a phone plots a shorter window instead of a smear.
export function BarChart({ data, series, height = 180, format = compactNumber }) {
  const [ref, width] = useElementWidth();

  if (!data?.length || !series?.length) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-slate-400">
        No data in this window.
      </div>
    );
  }

  // Hold the full series until the first measurement lands, so nothing
  // renders at a guessed width and then jumps.
  const plotted = width ? data.slice(-windowCapacity(width, series.length)) : data;

  const maxes = Object.fromEntries(
    series.map((s) => [s.key, Math.max(1, ...plotted.map((d) => d[s.key] || 0))]),
  );

  const viewW = width || 600;
  const plotH = height - 22; // leave room for x labels
  const groupGap = plotted.length > 20 ? 4 : 8;
  const groupW = (viewW - groupGap * (plotted.length - 1)) / plotted.length;
  const barW = Math.max(1, (groupW - BAR_GAP * (series.length - 1)) / series.length);
  const labelStep = Math.max(1, Math.ceil(plotted.length / Math.max(1, Math.floor(viewW / LABEL_WIDTH))));

  return (
    <div ref={ref}>
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
        className="w-full"
        style={{ height }}
        role="img"
      >
        <line x1="0" y1={plotH} x2={viewW} y2={plotH} stroke="#e2e8f0" strokeWidth="1" />
        {plotted.map((d, i) =>
          series.map((s, j) => {
            const max = maxes[s.key];
            const value = d[s.key] || 0;
            const h = max > 0 ? (value / max) * (plotH - 4) : 0;
            const x = i * (groupW + groupGap) + j * (barW + BAR_GAP);
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
        {plotted.map((d, i) => (
          <span key={i} className="flex-1 truncate text-center">
            {i % labelStep === 0 ? d.label : ''}
          </span>
        ))}
      </div>
    </div>
  );
}
