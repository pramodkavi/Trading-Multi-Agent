import type { SignalFilters } from "../../api/types";
import { DECISION_KINDS, SIGNAL_OUTCOMES } from "../../lib/journalFilters";
import { buttonClass } from "../ErrorPage";

const SESSIONS = ["LONDON", "NY", "OVERLAP", "DAILY_WRAP", "ASIAN", "COOLDOWN", "AD_HOC"] as const;

const fieldClass =
  "mt-1 block w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100";
const labelClass = "block text-xs font-medium text-slate-700 dark:text-slate-300";

type TextKey = "symbol" | "tag" | "violated_rule";

/**
 * The journal's filters. Selects and dates apply immediately; text fields apply on
 * blur or Enter so each keystroke does not become a request.
 */
export function FilterBar({
  value,
  onChange,
}: {
  value: SignalFilters;
  onChange: (next: SignalFilters) => void;
}) {
  const set = <K extends keyof SignalFilters>(key: K, v: SignalFilters[K] | "") => {
    const kept = Object.entries(value).filter(([k]) => k !== key);
    const next = Object.fromEntries(v === "" || v === undefined ? kept : [...kept, [key, v]]);
    onChange(next as SignalFilters);
  };

  const text = (key: TextKey, label: string, upper = false) => (
    <div>
      <label className={labelClass}>
        {label}
        <input
          key={value[key] ?? ""}
          type="text"
          defaultValue={value[key] ?? ""}
          className={fieldClass}
          onBlur={(e) => {
            const v = e.currentTarget.value.trim();
            if (v !== (value[key] ?? "")) set(key, upper ? v.toUpperCase() : v);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
          }}
        />
      </label>
    </div>
  );

  return (
    <form
      aria-label="Filters"
      onSubmit={(e) => e.preventDefault()}
      className="grid grid-cols-2 gap-3 md:grid-cols-4"
    >
      <div>
        <label className={labelClass}>
          From
          <input
            type="date"
            value={value.since ?? ""}
            className={fieldClass}
            onChange={(e) => set("since", e.currentTarget.value)}
          />
        </label>
      </div>
      <div>
        <label className={labelClass}>
          To
          <input
            type="date"
            value={value.until ?? ""}
            className={fieldClass}
            onChange={(e) => set("until", e.currentTarget.value)}
          />
        </label>
      </div>
      {text("symbol", "Symbol", true)}
      <div>
        <label className={labelClass}>
          Decision
          <select
            value={value.kind ?? ""}
            className={fieldClass}
            onChange={(e) => set("kind", e.currentTarget.value as SignalFilters["kind"] | "")}
          >
            <option value="">All</option>
            {DECISION_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div>
        <label className={labelClass}>
          Outcome
          <select
            value={value.outcome ?? ""}
            className={fieldClass}
            onChange={(e) => set("outcome", e.currentTarget.value as SignalFilters["outcome"] | "")}
          >
            <option value="">All</option>
            {SIGNAL_OUTCOMES.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div>
        <label className={labelClass}>
          Session
          <select
            value={value.session ?? ""}
            className={fieldClass}
            onChange={(e) => set("session", e.currentTarget.value)}
          >
            <option value="">All</option>
            {SESSIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </div>
      {text("tag", "Tag")}
      {text("violated_rule", "Violated rule")}
      <div className="col-span-2 md:col-span-4">
        <button type="button" className={buttonClass} onClick={() => onChange({})}>
          Clear filters
        </button>
      </div>
    </form>
  );
}
