import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { FunnelGroupBy } from "../../api/types";
import { ruleColor } from "../../lib/chartColors";
import { groupLabel, type RejectionSeries } from "../../lib/funnel";
import { mutedClass } from "../styles";
import { DataTable } from "./DataTable";

/** Risk-gate rejections per group, stacked by rule, with a legend and a data table. */
export function RejectionChart({ series, groupBy }: { series: RejectionSeries; groupBy: FunnelGroupBy }) {
  if (series.rules.length === 0) {
    return <p className={mutedClass}>No rejections in this period.</p>;
  }
  const rows: Record<string, string | number>[] = series.rows.map((r) => ({
    ...r,
    label: groupLabel(String(r.key), groupBy),
  }));
  return (
    <div>
      <ul aria-label="Legend" className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-slate-800 dark:text-slate-200">
        {series.rules.map((rule, i) => (
          <li key={rule} className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="h-2.5 w-2.5 rounded-sm" style={{ background: ruleColor(rule, i) }} />
            <span className="break-all">{rule}</span>
          </li>
        ))}
      </ul>
      <div role="img" aria-label="Stacked bar chart of risk-gate rejections by rule for each group. A data table follows.">
        <ResponsiveContainer width="100%" height={260} initialDimension={{ width: 640, height: 260 }}>
          <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--viz-grid)" vertical={false} />
            <XAxis dataKey="label" stroke="var(--viz-axis)" tick={{ fill: "var(--viz-text-muted)", fontSize: 12 }} />
            <YAxis
              allowDecimals={false}
              stroke="var(--viz-axis)"
              tick={{ fill: "var(--viz-text-muted)", fontSize: 12 }}
              width={32}
            />
            <Tooltip
              cursor={{ fill: "var(--viz-grid)", opacity: 0.4 }}
              contentStyle={{
                background: "var(--viz-surface)",
                border: "1px solid var(--viz-axis)",
                color: "var(--viz-text)",
                fontSize: 12,
              }}
            />
            {series.rules.map((rule, i) => (
              <Bar
                key={rule}
                dataKey={rule}
                stackId="rejections"
                fill={ruleColor(rule, i)}
                stroke="var(--viz-surface)"
                strokeWidth={2}
                maxBarSize={24}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <DataTable
        caption="Risk-gate rejections by rule for each group"
        columns={["Group", ...series.rules]}
        rows={rows.map((r) => [String(r.label), ...series.rules.map((rule) => Number(r[rule] ?? 0))])}
      />
    </div>
  );
}
