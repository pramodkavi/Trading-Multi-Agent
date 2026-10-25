import { PERIODS, type PeriodDays } from "../../lib/period";

const base =
  "rounded-md border px-3 py-1.5 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 dark:focus-visible:outline-sky-400";
const on = "border-slate-900 bg-slate-900 text-white dark:border-slate-100 dark:bg-slate-100 dark:text-slate-900";
const off =
  "border-slate-300 bg-white text-slate-800 hover:bg-slate-100 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800";

/** Last 7 / 30 / 90 days, as a labelled radio group. */
export function PeriodSelect({ value, onChange }: { value: PeriodDays; onChange: (d: PeriodDays) => void }) {
  return (
    <div role="radiogroup" aria-label="Period" className="flex flex-wrap gap-2">
      {PERIODS.map((d) => (
        <button
          key={d}
          type="button"
          role="radio"
          aria-checked={d === value}
          className={`${base} ${d === value ? on : off}`}
          onClick={() => onChange(d)}
        >
          {`${d} days`}
        </button>
      ))}
    </div>
  );
}
