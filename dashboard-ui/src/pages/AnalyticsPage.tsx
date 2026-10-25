import { useSearchParams } from "react-router-dom";
import { CriticPanel } from "../components/analytics/CriticPanel";
import { EquityPanel } from "../components/analytics/EquityPanel";
import { SkepticPanel } from "../components/analytics/SkepticPanel";
import { WinRatePanel } from "../components/analytics/WinRatePanel";
import { PeriodSelect } from "../components/charts/PeriodSelect";
import { Section } from "../components/Section";
import { parsePeriod, type PeriodDays } from "../lib/period";

const DEFAULT_DAYS: PeriodDays = 90;

export default function AnalyticsPage() {
  const [params, setParams] = useSearchParams();
  const days = parsePeriod(params.get("days"), DEFAULT_DAYS);

  // The default stays out of the URL, so `/analytics` and `/analytics?days=90` are the same view.
  const setDays = (next: PeriodDays) =>
    setParams(next === DEFAULT_DAYS ? new URLSearchParams() : new URLSearchParams({ days: String(next) }), {
      replace: true,
    });

  return (
    <section>
      <h1 className="mb-4 text-2xl font-semibold text-slate-900 dark:text-slate-100">Analytics</h1>
      <div className="mb-6">
        <PeriodSelect value={days} onChange={setDays} />
      </div>
      <div className="grid min-w-0 gap-8">
        <Section title="Win rate">
          <WinRatePanel days={days} />
        </Section>
        <Section title="Skeptic value">
          <SkepticPanel days={days} />
        </Section>
        <Section title="Equity curve">
          <EquityPanel days={days} />
        </Section>
        <Section title="Critic">
          <CriticPanel />
        </Section>
      </div>
    </section>
  );
}
