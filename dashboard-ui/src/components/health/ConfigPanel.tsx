import type { ReactNode } from "react";
import { useSystemConfig } from "../../api/queries";
import type { Inferred } from "../../api/types";
import { cardClass } from "../styles";
import { QueryState } from "./QueryState";

function yesNo(v: boolean | null): string {
  if (v === null) return "unknown";
  return v ? "yes" : "no";
}

function Row({ label, value, inferred }: { label: string; value: ReactNode; inferred?: Inferred<unknown> }) {
  return (
    <div className="flex flex-wrap justify-between gap-x-4 gap-y-0.5 py-1.5 text-sm">
      <dt className="text-slate-700 dark:text-slate-300">{label}</dt>
      <dd className="min-w-0 break-words text-right text-slate-900 dark:text-slate-100">
        {value}
        {inferred ? (
          <span className="block text-xs text-slate-700 dark:text-slate-300">{`inferred from ${inferred.source}`}</span>
        ) : null}
      </dd>
    </div>
  );
}

/** What the dashboard can infer about the deployed configuration (it cannot read the scan config). */
export function ConfigPanel() {
  const query = useSystemConfig();
  return (
    <QueryState query={query} label="configuration">
      {(c) => (
        <dl className={`${cardClass} divide-y divide-slate-200 dark:divide-slate-700`}>
          <Row label="Watchlist" value={c.watchlist.value.join(", ") || "—"} inferred={c.watchlist} />
          <Row label="Model" value={c.model.value ?? "unknown"} inferred={c.model} />
          <Row
            label="Macro providers configured"
            value={yesNo(c.macro_providers_configured.value)}
            inferred={c.macro_providers_configured}
          />
          <Row label="Langfuse configured" value={yesNo(c.langfuse_configured.value)} inferred={c.langfuse_configured} />
          <Row
            label="Schedule (UTC)"
            value={c.schedule.windows.map((w) => `${w.label} ${w.time_utc}`).join(" · ")}
          />
        </dl>
      )}
    </QueryState>
  );
}
