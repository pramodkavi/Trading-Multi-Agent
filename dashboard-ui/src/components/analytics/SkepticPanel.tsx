import { useSkepticValue } from "../../api/queries";
import { formatWinRate, skepticRows } from "../../lib/analytics";
import type { PeriodDays } from "../../lib/period";
import { QueryState } from "../health/QueryState";
import { mutedClass } from "../styles";

/** How decisions ended, by whether the Judge respected or overruled the Skeptic's objection. */
export function SkepticPanel({ days }: { days: PeriodDays }) {
  const query = useSkepticValue(days);
  return (
    <QueryState query={query} label="Skeptic value">
      {(data) => {
        const rows = skepticRows(data);
        const judged = rows.reduce((sum, r) => sum + r.n, 0);
        const excluded = data.no_macro_data_excluded;
        const footer = `${excluded} ${excluded === 1 ? "run" : "runs"} excluded (no macro data) · ${data.unruled} with no ruling`;
        if (judged === 0) {
          return (
            <>
              <p className={mutedClass}>No objections were judged in this period.</p>
              {excluded > 0 || data.unruled > 0 ? <p className={`${mutedClass} mt-1`}>{footer}</p> : null}
            </>
          );
        }
        return (
          <>
            <div className="overflow-x-auto">
              <table
                aria-label="Skeptic value"
                className="w-full min-w-[30rem] text-left text-sm text-slate-900 dark:text-slate-100"
              >
                <thead className="text-xs uppercase text-slate-700 dark:text-slate-300">
                  <tr>
                    <th scope="col" className="py-1 pr-3">
                      Judge response
                    </th>
                    <th scope="col" className="py-1 pr-3">
                      Decisions
                    </th>
                    <th scope="col" className="py-1 pr-3">
                      Win / loss
                    </th>
                    <th scope="col" className="py-1">
                      Win rate
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                  {rows.map((r) => (
                    <tr key={r.id} className="align-top">
                      <td className="py-1.5 pr-3">
                        <span className="block font-medium">{r.label}</span>
                        <span className={`${mutedClass} block text-xs`}>{r.note}</span>
                      </td>
                      <td className="py-1.5 pr-3 font-mono text-xs">{r.n}</td>
                      <td className="py-1.5 pr-3 font-mono text-xs">{`${r.wins} / ${r.losses}`}</td>
                      <td className="py-1.5 font-mono text-xs">{formatWinRate(r.pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className={`${mutedClass} mt-2`}>{footer}</p>
          </>
        );
      }}
    </QueryState>
  );
}
