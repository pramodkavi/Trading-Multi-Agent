import { isDbResuming } from "../api/client";
import { useOverview } from "../api/queries";
import { AlertBanner } from "../components/AlertBanner";
import { buttonClass } from "../components/ErrorPage";
import { Section } from "../components/Section";
import { HealthStrip } from "../components/overview/HealthStrip";
import { LatestSignals } from "../components/overview/LatestSignals";
import { OpenSetups } from "../components/overview/OpenSetups";
import { RiskBudgetPanel } from "../components/overview/RiskBudgetPanel";
import { WindowsToday } from "../components/overview/WindowsToday";
import { mutedClass } from "../components/styles";
import { collectProblems } from "../lib/problems";
import { useNow } from "../lib/useNow";

export default function OverviewPage() {
  const query = useOverview();
  const now = useNow(15_000);

  let body;
  if (query.data !== undefined) {
    const o = query.data;
    body = (
      <div className="grid min-w-0 gap-6">
        {/* A failed background refresh keeps the last good data (TanStack v5): show it, flag it stale. */}
        {query.isError ? (
          <p role="status" className="text-sm text-amber-900 dark:text-amber-200">
            Could not refresh the overview — showing the last data received.
          </p>
        ) : null}
        <AlertBanner problems={collectProblems(o)} />
        <div className="grid min-w-0 gap-6 md:grid-cols-2">
          <Section title="Health">
            <HealthStrip health={o.health} />
          </Section>
          <Section title="Risk budget">
            <RiskBudgetPanel risk={o.risk} />
          </Section>
        </div>
        <Section title="Open setups">
          <OpenSetups setups={o.open_setups} />
        </Section>
        <Section title="Today's scans">
          <WindowsToday windows={o.windows_today} now={new Date(now)} />
        </Section>
        <Section title="Latest signals">
          <LatestSignals />
        </Section>
      </div>
    );
  } else if (query.isPending) {
    body = (
      <p className={mutedClass}>
        {isDbResuming(query.failureReason) ? "Waking the database…" : "Loading overview…"}
      </p>
    );
  } else {
    body = (
      <div
        role="alert"
        className="rounded-lg border-2 border-red-600/50 bg-red-50 p-4 text-red-900 dark:border-red-400/50 dark:bg-red-950 dark:text-red-100"
      >
        <p className="font-semibold">Could not load the overview</p>
        <p className="break-words text-sm">{query.error?.message}</p>
        <button type="button" className={`${buttonClass} mt-3`} onClick={() => void query.refetch()}>
          Retry
        </button>
      </div>
    );
  }
  return (
    <section>
      <h1 className="mb-4 text-2xl font-semibold text-slate-900 dark:text-slate-100">Overview</h1>
      {body}
    </section>
  );
}
