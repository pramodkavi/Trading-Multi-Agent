import { useAgents } from "../../api/queries";
import { cardClass } from "../styles";
import { QueryState } from "./QueryState";

const usd = (n: number) => `$${n.toFixed(4)}`;

/** Per-role runs / failures per day and LLM spend per day (design §4.2 P7). */
export function AgentUsage() {
  const query = useAgents(7);
  return (
    <QueryState query={query} label="agent usage">
      {(data) => {
        const total = data.cost_per_day.reduce((sum, d) => sum + d.total_usd, 0);
        return (
          <div className="grid min-w-0 gap-4 md:grid-cols-2">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[22rem] text-left text-sm text-slate-900 dark:text-slate-100">
                <thead className="text-xs uppercase text-slate-700 dark:text-slate-300">
                  <tr>
                    <th className="py-1 pr-3">Day</th>
                    <th className="py-1 pr-3">Role</th>
                    <th className="py-1 pr-3">Runs</th>
                    <th className="py-1 pr-3">Errors</th>
                    <th className="py-1">Cost</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                  {data.usage.map((u) => (
                    <tr key={`${u.date}-${u.role}`}>
                      <td className="py-1.5 pr-3 font-mono text-xs">{u.date}</td>
                      <td className="py-1.5 pr-3">{u.role}</td>
                      <td className="py-1.5 pr-3">{u.runs}</td>
                      <td className="py-1.5 pr-3" data-testid="usage-errors">
                        {u.errors === null ? "—" : u.errors}
                      </td>
                      <td className="py-1.5">{usd(u.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className={`${cardClass} space-y-1 text-sm text-slate-900 dark:text-slate-100`}>
              <p className="text-xs font-semibold uppercase text-slate-700 dark:text-slate-300">LLM cost per day</p>
              {data.cost_per_day.map((d) => (
                <p key={d.date} className="flex justify-between gap-3">
                  <span className="font-mono text-xs">{d.date}</span>
                  <span>{usd(d.total_usd)}</span>
                </p>
              ))}
              <p className="flex justify-between gap-3 border-t border-slate-200 pt-1 font-semibold dark:border-slate-700">
                <span>7-day total</span>
                <span>{usd(total)}</span>
              </p>
            </div>
          </div>
        );
      }}
    </QueryState>
  );
}
