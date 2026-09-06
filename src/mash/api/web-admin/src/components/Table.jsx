// Generic data table. `columns` is an array of
// `{ key, header, render?, className?, align? }`; `render(row)` overrides the
// default `row[key]`. `onRowClick` makes rows interactive.
//
// Below `sm` the same `columns` config renders each row as a stacked card
// instead, since the wide tables here (10 trace columns, 9 eval columns) are
// not readable through a sideways scroll on a phone. Two optional column
// fields steer that rendering:
//   `primary`     this column is a card title line rather than a labelled row
//   `hideOnMobile` drop this column from the card entirely
// Neither affects the table above `sm`, and a config that sets neither still
// gets a sensible card with its first column as the title.

function cardSections(columns) {
  const marked = columns.filter((col) => col.primary);
  const headers = marked.length ? marked : columns.slice(0, 1);
  const body = columns.filter((col) => !headers.includes(col) && !col.hideOnMobile);
  return { headers, body };
}

function cellValue(col, row) {
  return col.render ? col.render(row) : row[col.key];
}

function RowCard({ columns, row, onClick, active }) {
  const { headers, body } = cardSections(columns);
  const cls = `block w-full rounded-lg border bg-white text-left ${
    active ? 'border-slate-300 ring-1 ring-slate-200' : 'border-slate-200'
  }`;

  const content = (
    <>
      <div className="space-y-1 px-3 py-2.5">
        {headers.map((col) => (
          <div key={col.key} className="min-w-0 text-sm font-medium text-slate-900">
            {cellValue(col, row)}
          </div>
        ))}
      </div>
      {body.length ? (
        <dl className="border-t border-slate-100 px-3 py-1.5">
          {body.map((col) => (
            <div key={col.key} className="flex items-start justify-between gap-3 py-1 text-sm">
              <dt className="shrink-0 text-xs leading-5 text-slate-400">{col.header}</dt>
              <dd className="min-w-0 text-right tabular-nums">{cellValue(col, row)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </>
  );

  if (onClick) {
    return (
      <button type="button" onClick={onClick} className={cls}>
        {content}
      </button>
    );
  }
  return <div className={cls}>{content}</div>;
}

export function Table({ columns, rows, getRowKey, onRowClick, activeKey }) {
  const keyFor = (row, idx) => (getRowKey ? getRowKey(row, idx) : idx);
  const isActive = (key) => activeKey !== undefined && key === activeKey;

  return (
    <>
      <div className="space-y-2 sm:hidden">
        {rows.map((row, idx) => {
          const key = keyFor(row, idx);
          return (
            <RowCard
              key={key}
              columns={columns}
              row={row}
              active={isActive(key)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            />
          );
        })}
      </div>

      <div className="hidden overflow-x-auto rounded-lg border border-slate-200 bg-white sm:block">
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
              const key = keyFor(row, idx);
              return (
                <tr
                  key={key}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={`border-b border-slate-100 last:border-0 ${
                    onRowClick ? 'cursor-pointer hover:bg-slate-50' : ''
                  } ${isActive(key) ? 'bg-slate-50' : ''}`}
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={`px-4 py-2.5 align-top ${col.align === 'right' ? 'text-right tabular-nums' : ''} ${col.cellClassName || ''}`}
                    >
                      {cellValue(col, row)}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
