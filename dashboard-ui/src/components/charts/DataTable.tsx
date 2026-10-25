import type { ReactNode } from "react";

/** A table view of a chart's numbers, collapsed by default (the accessible fallback). */
export function DataTable({
  caption,
  columns,
  rows,
}: {
  caption: string;
  columns: string[];
  rows: (string | number)[][];
}): ReactNode {
  return (
    <details className="mt-3 text-sm text-slate-900 dark:text-slate-100">
      <summary className="cursor-pointer text-slate-700 underline decoration-slate-400 underline-offset-2 dark:text-slate-300">
        Show data table
      </summary>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[16rem] text-left">
          <caption className="sr-only">{caption}</caption>
          <thead className="text-xs uppercase text-slate-700 dark:text-slate-300">
            <tr>
              {columns.map((c) => (
                <th key={c} scope="col" className="py-1 pr-3">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
            {rows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j} className="break-words py-1.5 pr-3 font-mono text-xs">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
