import { useState } from "react";
import { useCriticReport, useCriticReports, useCriticReviews } from "../../api/queries";
import type { CriticReportSummary } from "../../api/types";
import { formatUtcDateTime } from "../../lib/format";
import { safeHttpsUrl } from "../../lib/links";
import { buttonClass } from "../ErrorPage";
import { QueryState } from "../health/QueryState";
import { mutedClass } from "../styles";

/** The report's markdown as plain text (no renderer), fetched only after the first open. */
function ReportText({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const [everOpened, setEverOpened] = useState(false);
  const report = useCriticReport(id, everOpened);
  return (
    <div className="mt-2">
      <button
        type="button"
        className={buttonClass}
        aria-expanded={open}
        onClick={() => {
          setOpen((o) => !o);
          setEverOpened(true);
        }}
      >
        Show report
      </button>
      {open ? (
        report.isPending ? (
          <p className={`${mutedClass} mt-2`}>Loading…</p>
        ) : report.isError ? (
          <p role="status" className="mt-2 text-sm text-red-800 dark:text-red-200">
            Could not load the report.
          </p>
        ) : (
          <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md border border-slate-200 bg-slate-50 p-3 text-xs text-slate-900 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100">
            {report.data.markdown}
          </pre>
        )
      ) : null}
    </div>
  );
}

function ReportItem({ report }: { report: CriticReportSummary }) {
  const pr = safeHttpsUrl(report.pr_url);
  return (
    <li className="min-w-0 py-3 first:pt-0 last:pb-0">
      <p className="font-mono text-xs text-slate-700 dark:text-slate-300">
        {`${formatUtcDateTime(report.period_start)} → ${formatUtcDateTime(report.period_end)} · written ${formatUtcDateTime(
          report.created_at,
        )}`}
      </p>
      <p className="mt-1 break-words text-sm text-slate-900 dark:text-slate-100">{report.summary ?? "No summary."}</p>
      {pr ? (
        <a
          href={pr}
          target="_blank"
          rel="noreferrer noopener"
          className="mt-1 inline-block text-sm underline decoration-slate-400 underline-offset-2"
        >
          Proposed-rules PR
        </a>
      ) : null}
      <ReportText id={report.id} />
    </li>
  );
}

const yesNo = (v: boolean | null) => (v === null ? "—" : v ? "Yes" : "No");

/** The latest Critic reports and decision-review grades. Both are empty until the first backfill. */
export function CriticPanel() {
  const reports = useCriticReports(5);
  const reviews = useCriticReviews(10);
  return (
    <div className="grid min-w-0 gap-6">
      <div>
        <h3 className="mb-2 text-sm font-semibold text-slate-900 dark:text-slate-100">Reports</h3>
        <QueryState query={reports} label="Critic reports">
          {(data) =>
            data.items.length === 0 ? (
              <p className={mutedClass}>No Critic reports yet — the first backfill is pending.</p>
            ) : (
              <ul className="divide-y divide-slate-200 dark:divide-slate-700">
                {data.items.map((r) => (
                  <ReportItem key={r.id} report={r} />
                ))}
              </ul>
            )
          }
        </QueryState>
      </div>
      <div>
        <h3 className="mb-2 text-sm font-semibold text-slate-900 dark:text-slate-100">Reviews</h3>
        <QueryState query={reviews} label="Critic reviews">
          {(data) =>
            data.items.length === 0 ? (
              <p className={mutedClass}>No reviews yet.</p>
            ) : (
              <div className="overflow-x-auto">
                <table
                  aria-label="Latest decision reviews"
                  className="w-full min-w-[40rem] text-left text-sm text-slate-900 dark:text-slate-100"
                >
                  <thead className="text-xs uppercase text-slate-700 dark:text-slate-300">
                    <tr>
                      <th scope="col" className="py-1 pr-3">
                        Decision
                      </th>
                      <th scope="col" className="py-1 pr-3">
                        Category
                      </th>
                      <th scope="col" className="py-1 pr-3">
                        Lesson
                      </th>
                      <th scope="col" className="py-1 pr-3">
                        Judge correct
                      </th>
                      <th scope="col" className="py-1">
                        Gate correct
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                    {data.items.map((r) => (
                      <tr key={`${r.signal_id}-${r.created_at}`} className="align-top">
                        <td className="py-1.5 pr-3">{r.decision_kind}</td>
                        <td className="break-all py-1.5 pr-3 font-mono text-xs">{r.failure_category ?? "—"}</td>
                        <td className="break-words py-1.5 pr-3">
                          {r.error ? `Review failed: ${r.error}` : (r.lesson ?? "—")}
                        </td>
                        <td className="py-1.5 pr-3">{yesNo(r.judge_correct)}</td>
                        <td className="py-1.5">{yesNo(r.gate_correct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }
        </QueryState>
      </div>
    </div>
  );
}
