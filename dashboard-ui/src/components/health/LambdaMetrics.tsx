import { useMetrics } from "../../api/queries";
import { formatDuration } from "../../lib/format";
import { Badge } from "../Badge";
import { QueryState } from "./QueryState";

/** The scan Lambda's daily metrics; fetched once per page view (CloudWatch bills per request). */
export function LambdaMetrics() {
  const query = useMetrics(7);
  return (
    <QueryState query={query} label="Lambda metrics">
      {(data) =>
        data.days === null ? (
          <Badge tone="warn">{data.note ?? "cloudwatch unavailable"}</Badge>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[24rem] text-left text-sm text-slate-900 dark:text-slate-100">
              <thead className="text-xs uppercase text-slate-700 dark:text-slate-300">
                <tr>
                  <th className="py-1 pr-3">Day</th>
                  <th className="py-1 pr-3">Invocations</th>
                  <th className="py-1 pr-3">Errors</th>
                  <th className="py-1">p95 duration</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                {data.days.map((d) => (
                  <tr key={d.date}>
                    <td className="py-1.5 pr-3 font-mono text-xs">{d.date}</td>
                    <td className="py-1.5 pr-3">{d.invocations}</td>
                    <td className="py-1.5 pr-3">{d.errors}</td>
                    <td className="py-1.5">
                      {d.duration_p95_ms === null ? "—" : formatDuration(d.duration_p95_ms / 1000)}
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
