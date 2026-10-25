import { useState } from "react";
import { useWinRate } from "../../api/queries";
import type { WinRateBy } from "../../api/types";
import { formatWinRate, sortByKey, winRateRows } from "../../lib/analytics";
import type { PeriodDays } from "../../lib/period";
import { Badge } from "../Badge";
import { SegmentedControl } from "../charts/SegmentedControl";
import { QueryState } from "../health/QueryState";
import { mutedClass } from "../styles";

const BUCKETS = [
  { value: "tag", label: "Tag" },
  { value: "session", label: "Session" },
  { value: "strategy", label: "Strategy" },
  { value: "time", label: "Week" },
] as const;

const BUCKET_NAME: Record<WinRateBy, string> = { tag: "tag", session: "session", strategy: "strategy", time: "week" };

/** Published win rate per bucket; always with its sample size, never a bare percentage. */
export function WinRatePanel({ days }: { days: PeriodDays }) {
  const [by, setBy] = useState<WinRateBy>("tag");
  const query = useWinRate(days, by);
  return (
    <div className="grid min-w-0 gap-3">
      <SegmentedControl label="Bucket" options={BUCKETS} value={by} onChange={setBy} />
      <QueryState query={query} label="win rate">
        {(data) => {
          const rows = by === "time" ? sortByKey(winRateRows(data.buckets)) : winRateRows(data.buckets);
          if (rows.length === 0) return <p className={mutedClass}>No published decisions in this period.</p>;
          return (
            <>
              <div className="overflow-x-auto">
                <table
                  aria-label={`Win rate by ${BUCKET_NAME[by]}`}
                  className="w-full min-w-[30rem] text-left text-sm text-slate-900 dark:text-slate-100"
                >
                  <thead className="text-xs uppercase text-slate-700 dark:text-slate-300">
                    <tr>
                      <th scope="col" className="py-1 pr-3">
                        {BUCKETS.find((b) => b.value === by)?.label}
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
                      <tr key={r.key} className="align-top">
                        <td className="max-w-[16rem] break-all py-1.5 pr-3">{r.label}</td>
                        <td className="py-1.5 pr-3 font-mono text-xs">{r.n}</td>
                        <td className="py-1.5 pr-3 font-mono text-xs">{`${r.wins} / ${r.losses}`}</td>
                        <td className="py-1.5">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-mono text-xs">{formatWinRate(r.pct)}</span>
                            {r.pct !== null ? (
                              <span aria-hidden="true" className="h-2 w-24 rounded-sm bg-slate-100 dark:bg-slate-800">
                                <span
                                  className="block h-2 rounded-r bg-[var(--viz-series-1)]"
                                  style={{ width: `${Math.round(r.pct * 100)}%` }}
                                />
                              </span>
                            ) : null}
                            {r.lowSample ? <Badge tone="warn">low sample</Badge> : null}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className={mutedClass}>
                {`Outcomes: ${data.source_counts.replayed} replayed, ${data.source_counts.forecaster} forecaster-judged.`}
              </p>
            </>
          );
        }}
      </QueryState>
    </div>
  );
}
