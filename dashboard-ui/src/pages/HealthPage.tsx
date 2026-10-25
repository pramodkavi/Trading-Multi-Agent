import { AgentUsage } from "../components/health/AgentUsage";
import { AlarmsPanel } from "../components/health/AlarmsPanel";
import { ConfigPanel } from "../components/health/ConfigPanel";
import { DbPanel } from "../components/health/DbPanel";
import { LambdaMetrics } from "../components/health/LambdaMetrics";
import { ScanHistory } from "../components/health/ScanHistory";
import { SweepsTable } from "../components/health/SweepsTable";
import { Section } from "../components/Section";

export default function HealthPage() {
  return (
    <section>
      <h1 className="mb-4 text-2xl font-semibold text-slate-900 dark:text-slate-100">Health</h1>
      <div className="grid min-w-0 gap-6">
        <Section title="Database">
          <DbPanel />
        </Section>
        <Section title="Scan history">
          <ScanHistory />
        </Section>
        <Section title="Alarms">
          <AlarmsPanel />
        </Section>
        <Section title="Agent usage & LLM cost">
          <AgentUsage />
        </Section>
        <Section title="Forecaster sweeps">
          <SweepsTable />
        </Section>
        <Section title="Lambda metrics (7 days)">
          <LambdaMetrics />
        </Section>
        <Section title="Configuration (inferred)">
          <ConfigPanel />
        </Section>
      </div>
    </section>
  );
}
