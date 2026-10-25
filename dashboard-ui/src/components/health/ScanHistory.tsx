import { useScans } from "../../api/queries";
import type { ScanStatus } from "../../api/types";
import { formatDuration, formatUtcDateTime, truncate } from "../../lib/format";
import { isCreditError, type Tone } from "../../lib/problems";
import { Badge } from "../Badge";
import { QueryState } from "./QueryState";

const STATUS_TONE: Record<ScanStatus, Tone> = { SUCCESS: "ok", FAILED: "bad", RUNNING: "warn" };
const ERROR_MAX = 120;

/** The latest 50 scan runs (one row per symbol per invocation), newest first. */
export function ScanHistory() {
  const query = useScans();
  return (
    <QueryState query={query} label="scan history">
      {(data) =>
        data.items.length === 0 ? (
          <p className="text-sm text-slate-700 dark:text-slate-300">No scans recorded yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[40rem] text-left text-sm text-slate-900 dark:text-slate-100">
              <thead className="text-xs uppercase text-slate-700 dark:text-slate-300">
                <tr>
                  <th className="py-1 pr-3">Started (UTC)</th>
                  <th className="py-1 pr-3">Window</th>
                  <th className="py-1 pr-3">Status</th>
                  <th className="py-1 pr-3">Duration</th>
                  <th className="py-1 pr-3">Symbols</th>
                  <th className="py-1">Error</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                {data.items.map((row) => (
                  <tr key={row.id} data-testid="scan-row" className="align-top">
                    <td className="py-1.5 pr-3 font-mono text-xs">{formatUtcDateTime(row.started_at)}</td>
                    <td className="py-1.5 pr-3">{row.window}</td>
                    <td className="py-1.5 pr-3">
                      <Badge tone={STATUS_TONE[row.status]}>{row.status}</Badge>
                    </td>
                    <td className="py-1.5 pr-3">{formatDuration(row.duration_s)}</td>
                    <td className="py-1.5 pr-3">{row.symbols.join(", ")}</td>
                    <td className="py-1.5">
                      {row.error_message ? (
                        <span className="flex flex-wrap items-start gap-2">
                          {isCreditError(row.error_message) ? <Badge tone="bad">credits</Badge> : null}
                          <span
                            data-testid="scan-error"
                            title={row.error_message}
                            className="break-words font-mono text-xs text-red-800 dark:text-red-200"
                          >
                            {truncate(row.error_message, ERROR_MAX)}
                          </span>
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </QueryState>
  );
}
