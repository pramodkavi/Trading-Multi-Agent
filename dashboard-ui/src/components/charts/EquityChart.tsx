import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatR } from "../../lib/format";
import { type EquityLine } from "../../lib/analytics";
import { mutedClass } from "../styles";
import { DataTable } from "./DataTable";

const STYLE: Record<EquityLine["id"], { color: string; dash: string | undefined }> = {
  published: { color: "var(--viz-series-1)", dash: undefined },
  vetoed: { color: "var(--viz-series-2)", dash: "6 4" },
  gate_rejected: { color: "var(--viz-series-3)", dash: "2 3" },
};

const day = (t: number) => new Date(t).toISOString().slice(5, 10);

/** One row per timestamp, one column per line, so a single LineChart can draw them all. */
function mergeLines(lines: EquityLine[]): Record<string, number>[] {
  const byTime = new Map<number, Record<string, number>>();
  for (const line of lines) {
    for (const p of line.points) {
      const row = byTime.get(p.t) ?? { t: p.t };
      row[line.id] = p.cumulativeR;
      byTime.set(p.t, row);
    }
  }
  return [...byTime.values()].sort((a, b) => (a.t ?? 0) - (b.t ?? 0));
}

/**
 * The hypothetical cumulative-R curves. Counterfactual lines are dashed (not just another
 * colour); every visible line is named in the legend with its final R, and a table repeats it.
 */
export function EquityChart({ lines, visible }: { lines: EquityLine[]; visible: Record<string, boolean> }) {
  const shown = lines.filter((l) => visible[l.id] !== false);
  if (lines.every((l) => l.points.length === 0)) {
    return <p className={mutedClass}>No resolved outcomes in this period.</p>;
  }
  const finalText = (l: EquityLine) => (l.finalR === null ? "—" : formatR(l.finalR));
  return (
    <div>
      <ul aria-label="Legend" className="mb-2 flex flex-wrap gap-x-5 gap-y-1 text-sm text-slate-800 dark:text-slate-200">
        {shown.map((l) => (
          <li key={l.id} className="inline-flex items-center gap-2">
            <svg aria-hidden="true" width="22" height="8" viewBox="0 0 22 8">
              <line
                x1="0"
                y1="4"
                x2="22"
                y2="4"
                stroke={STYLE[l.id].color}
                strokeWidth="2"
                strokeLinecap="round"
                strokeDasharray={STYLE[l.id].dash}
              />
            </svg>
            <span>{`${l.label} · final ${finalText(l)}`}</span>
          </li>
        ))}
      </ul>
      <div role="img" aria-label="Equity curve: cumulative R of each published decision over time. A data table follows.">
        <ResponsiveContainer width="100%" height={280} initialDimension={{ width: 640, height: 280 }}>
          <LineChart data={mergeLines(shown)} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--viz-grid)" vertical={false} />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={["dataMin", "dataMax"]}
              tickFormatter={day}
              stroke="var(--viz-axis)"
              tick={{ fill: "var(--viz-text-muted)", fontSize: 12 }}
            />
            <YAxis
              stroke="var(--viz-axis)"
              tick={{ fill: "var(--viz-text-muted)", fontSize: 12 }}
              width={36}
              tickFormatter={(v: number) => `${v} R`}
            />
            <ReferenceLine y={0} stroke="var(--viz-axis)" />
            <Tooltip
              labelFormatter={(t) => day(Number(t))}
              contentStyle={{
                background: "var(--viz-surface)",
                border: "1px solid var(--viz-axis)",
                color: "var(--viz-text)",
                fontSize: 12,
              }}
            />
            {shown.map((l) => (
              <Line
                key={l.id}
                type="stepAfter"
                dataKey={l.id}
                name={l.label}
                stroke={STYLE[l.id].color}
                strokeWidth={2}
                strokeDasharray={STYLE[l.id].dash}
                strokeLinecap="round"
                dot={false}
                activeDot={{ r: 4, stroke: "var(--viz-surface)", strokeWidth: 2 }}
                connectNulls
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <DataTable
        caption="Equity curve: points and final R per line"
        columns={["Series", "Points", "Final R"]}
        rows={shown.map((l) => [l.label, l.points.length, finalText(l)])}
      />
    </div>
  );
}
