import { useSearchParams } from "react-router-dom";
import { ApiError, isDbResuming } from "../api/client";
import { useFunnel } from "../api/queries";
import type { FunnelGroupBy, RejectionCount } from "../api/types";
import { PeriodSelect } from "../components/charts/PeriodSelect";
import { RejectionChart } from "../components/charts/RejectionChart";
import { SegmentedControl } from "../components/charts/SegmentedControl";
import { StageBars } from "../components/charts/StageBars";
import { buttonClass } from "../components/ErrorPage";
import { Section } from "../components/Section";
import { mutedClass } from "../components/styles";
import { funnelStages, judgeBreakdown, rejectionSeries } from "../lib/funnel";
import { parsePeriod, type PeriodDays } from "../lib/period";

const DEFAULT_DAYS: PeriodDays = 30;
const DEFAULT_GROUP: FunnelGroupBy = "day";
const GROUPS = [
  { value: "day", label: "Day" },
  { value: "window", label: "Window" },
  { value: "symbol", label: "Symbol" },
] as const;

function parseGroup(raw: string | null): FunnelGroupBy {
  return GROUPS.find((g) => g.value === raw)?.value ?? DEFAULT_GROUP;
}

function RejectionTable({ title, rows, windowLabel }: { title: string; rows: RejectionCount[]; windowLabel: string }) {
  if (rows.length === 0) return <p className={mutedClass}>{`No rejections in the last ${windowLabel}.`}</p>;
  return (
    <div className="overflow-x-auto">
      <table aria-label={title} className="w-full min-w-[14rem] text-left text-sm text-slate-900 dark:text-slate-100">
        <thead className="text-xs uppercase text-slate-700 dark:text-slate-300">
          <tr>
            <th scope="col" className="py-1 pr-3">
              Rule
            </th>
            <th scope="col" className="py-1">
              Count
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
          {rows.map((r) => (
            <tr key={r.rule}>
              <td className="break-all py-1.5 pr-3 font-mono text-xs">{r.rule}</td>
              <td className="py-1.5 font-mono text-xs">{r.count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function FunnelPage() {
  const [params, setParams] = useSearchParams();
  const days = parsePeriod(params.get("days"), DEFAULT_DAYS);
  const group = parseGroup(params.get("group"));
  const query = useFunnel(days, group);

  // Defaults stay out of the URL, so `/funnel` and `/funnel?days=30&group=day` are the same view.
  const update = (nextDays: PeriodDays, nextGroup: FunnelGroupBy) => {
    const next = new URLSearchParams();
    if (nextDays !== DEFAULT_DAYS) next.set("days", String(nextDays));
    if (nextGroup !== DEFAULT_GROUP) next.set("group", nextGroup);
    setParams(next, { replace: true });
  };

  let body;
  if (query.data !== undefined) {
    const f = query.data;
    const judge = judgeBreakdown(f.totals);
    body = (
      <div className="grid min-w-0 gap-6">
        {query.isError ? (
          <p role="status" className="text-sm text-amber-900 dark:text-amber-200">
            Could not refresh the funnel — showing the last data received.
          </p>
        ) : null}
        <Section title="Decision funnel">
          {f.totals.scans === 0 ? (
            <p className={mutedClass}>No scans in this period.</p>
          ) : (
            <>
              <StageBars stages={funnelStages(f.totals)} />
              <p className={`${mutedClass} mt-3`}>
                {`Judge: ${judge.publish} publish · ${judge.caveat} with caveat · ${judge.skip} skip${
                  judge.none > 0 ? ` · ${judge.none} unruled` : ""
                }`}
              </p>
            </>
          )}
        </Section>
        {f.totals.scans === 0 ? null : (
          <Section title="Rejections by rule">
            <RejectionChart series={rejectionSeries(f.groups)} groupBy={group} />
          </Section>
        )}
        <Section title="Top rejection rules">
          <p className={`${mutedClass} mb-3`}>Always the last 7 and 30 days, whatever period is selected.</p>
          <div className="grid min-w-0 gap-6 md:grid-cols-2">
            <div>
              <h3 className="mb-1 text-sm font-semibold text-slate-900 dark:text-slate-100">Last 7 days</h3>
              <RejectionTable
                title="Top rejection rules, last 7 days"
                rows={f.top_rejections_7d}
                windowLabel="7 days"
              />
            </div>
            <div>
              <h3 className="mb-1 text-sm font-semibold text-slate-900 dark:text-slate-100">Last 30 days</h3>
              <RejectionTable
                title="Top rejection rules, last 30 days"
                rows={f.top_rejections_30d}
                windowLabel="30 days"
              />
            </div>
          </div>
        </Section>
      </div>
    );
  } else if (query.isPending) {
    body = (
      <p className={mutedClass}>
        {isDbResuming(query.failureReason) ? "Waking the database…" : "Loading funnel…"}
      </p>
    );
  } else if (query.error instanceof ApiError && query.error.status === 422) {
    body = <p className={mutedClass}>That date range is not valid.</p>;
  } else {
    body = (
      <div
        role="alert"
        className="rounded-lg border-2 border-red-600/50 bg-red-50 p-4 text-red-900 dark:border-red-400/50 dark:bg-red-950 dark:text-red-100"
      >
        <p className="font-semibold">Could not load the funnel</p>
        <p className="break-words text-sm">{query.error?.message}</p>
        <button type="button" className={`${buttonClass} mt-3`} onClick={() => void query.refetch()}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <section>
      <h1 className="mb-4 text-2xl font-semibold text-slate-900 dark:text-slate-100">Funnel</h1>
      <div className="mb-6 flex flex-wrap items-end gap-x-6 gap-y-3">
        <PeriodSelect value={days} onChange={(d) => update(d, group)} />
        <SegmentedControl
          label="Group by"
          options={GROUPS}
          value={group}
          onChange={(g) => update(days, g)}
        />
      </div>
      {body}
    </section>
  );
}
