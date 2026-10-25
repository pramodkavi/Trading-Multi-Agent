import { useAlarms } from "../../api/queries";
import { formatUtcDateTime } from "../../lib/format";
import type { Tone } from "../../lib/problems";
import { Badge } from "../Badge";
import { cardClass } from "../styles";
import { QueryState } from "./QueryState";

const STATE_TONE: Record<"OK" | "ALARM" | "INSUFFICIENT_DATA", Tone> = {
  OK: "ok",
  ALARM: "bad",
  INSUFFICIENT_DATA: "warn",
};

/** The CloudWatch Monitoring alarms; a CloudWatch failure is amber, never green. */
export function AlarmsPanel() {
  const query = useAlarms();
  return (
    <QueryState query={query} label="alarms">
      {(data) =>
        data.alarms === null ? (
          <Badge tone="warn">{data.note ?? "cloudwatch unavailable"}</Badge>
        ) : (
          <ul className={`${cardClass} divide-y divide-slate-200 dark:divide-slate-700`}>
            {data.alarms.map((a) => (
              <li key={a.name} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm first:pt-0 last:pb-0">
                <span className="font-medium text-slate-900 dark:text-slate-100">{a.name}</span>
                <Badge tone={STATE_TONE[a.state]}>{a.state}</Badge>
                <span className="font-mono text-xs text-slate-700 dark:text-slate-300">
                  {formatUtcDateTime(a.updated_at)}
                </span>
                {a.reason ? (
                  <span className="min-w-0 break-words text-xs text-slate-700 dark:text-slate-300">{a.reason}</span>
                ) : null}
              </li>
            ))}
          </ul>
        )
      }
    </QueryState>
  );
}
