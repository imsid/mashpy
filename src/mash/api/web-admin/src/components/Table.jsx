// Generic data table. `columns` is an array of
// `{ key, header, render?, className?, align? }`; `render(row)` overrides the
// default `row[key]`. `onRowClick` makes rows interactive.
export function Table({ columns, rows, getRowKey, onRowClick, activeKey }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-left text-xs font-medium uppercase tracking-wide text-slate-400">
            {columns.map((col) => (
              <th
                key={col.key}
                className={`px-4 py-2.5 ${col.align === 'right' ? 'text-right' : ''} ${col.className || ''}`}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, idx) => {
            const key = getRowKey ? getRowKey(row, idx) : idx;
            const active = activeKey !== undefined && key === activeKey;
            return (
              <tr
                key={key}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={`border-b border-slate-100 last:border-0 ${
                  onRowClick ? 'cursor-pointer hover:bg-slate-50' : ''
                } ${active ? 'bg-slate-50' : ''}`}
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={`px-4 py-2.5 align-top ${col.align === 'right' ? 'text-right tabular-nums' : ''} ${col.cellClassName || ''}`}
                  >
                    {col.render ? col.render(row) : row[col.key]}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
