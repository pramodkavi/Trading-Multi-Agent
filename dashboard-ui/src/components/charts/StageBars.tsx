import type { Stage } from "../../lib/funnel";
import { mutedClass } from "../styles";

/** Percent width of the bar: `count / max`, 0 when the whole funnel is empty. */
function barWidth(count: number, max: number): string {
  if (max <= 0) return "0%";
  return `${Number(((count * 100) / max).toFixed(3))}%`;
}

/** The funnel as horizontal bars, one per stage; thin, one hue, the value at the end of the bar. */
export function StageBars({ stages }: { stages: Stage[] }) {
  const max = Math.max(0, ...stages.map((s) => s.count));
  return (
    <ul aria-label="Funnel stages" className="grid gap-3">
      {stages.map((s) => (
        <li key={s.id} data-stage={s.id} className="min-w-0">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 text-sm text-slate-900 dark:text-slate-100">
            <span className="font-medium">{s.label}</span>
            <span className="flex items-baseline gap-3">
              <span className="font-mono">{s.count}</span>
              <span className={`${mutedClass} min-w-[9rem] text-right text-xs`}>
                {s.ofPrevious === null ? "—" : `${(s.ofPrevious * 100).toFixed(1)}% of previous`}
              </span>
            </span>
          </div>
          <div className="mt-1 h-3 rounded-sm bg-slate-100 dark:bg-slate-800">
            <div
              data-bar={s.id}
              className="h-3 rounded-r bg-[var(--viz-series-1)]"
              style={{ width: barWidth(s.count, max) }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}
