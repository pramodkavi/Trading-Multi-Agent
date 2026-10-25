import { useSweeps } from "../../api/queries";
import { formatUtcDateTime } from "../../lib/format";
import { Badge } from "../Badge";
import { QueryState } from "./QueryState";

/** Forecaster evaluations per sweep, newest first; sweeps with errors are highlighted. */
export function SweepsTable() {
  const query = useSweeps(7);
  return (
    <QueryState query={query} label="Forecaster sweeps">
      {(data) =>
        data.sweeps.length === 0 ? (
          <p className="text-sm text-slate-700 dark:text-slate-300">No sweeps in this window.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[20rem] text-left text-sm text-slate-900 dark:text-slate-100">
              <thead className="text-xs uppercase text-slate-700 dark:text-slate-300">
                <tr>
                  <th className="py-1 pr-3">Sweep (UTC)</th>
                  <th className="py-1 pr-3">Evaluations</th>
                  <th className="py-1">Errors</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                {[...data.sweeps].reverse().map((s) => (
                  <tr key={s.sweep_start}>
                    <td className="py-1.5 pr-3 font-mono text-xs">{formatUtcDateTime(s.sweep_start)}</td>
                    <td className="py-1.5 pr-3">{s.evaluations}</td>
                    <td className="py-1.5">
                      {s.errors > 0 ? <Badge tone="warn">{`${s.errors} errors`}</Badge> : 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </QueryState>
  );
}
