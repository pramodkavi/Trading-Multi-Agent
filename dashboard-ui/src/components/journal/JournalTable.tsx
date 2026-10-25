import { Link } from "react-router-dom";
import type { SignalRowOut } from "../../api/types";
import { formatR, formatUtcDateTime } from "../../lib/format";
import { KIND_TONE, outcomeTone } from "../../lib/tones";
import { Badge } from "../Badge";

const dash = (v: string | null) => v ?? "—";

/** The journal as a table; the symbol links to the decision's detail page. */
export function JournalTable({ rows }: { rows: SignalRowOut[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[48rem] text-left text-sm text-slate-900 dark:text-slate-100">
        <thead className="text-xs uppercase text-slate-700 dark:text-slate-300">
          <tr>
            <th className="py-1 pr-3">Time (UTC)</th>
            <th className="py-1 pr-3">Symbol</th>
            <th className="py-1 pr-3">Dir</th>
            <th className="py-1 pr-3">Decision</th>
            <th className="py-1 pr-3">Judge</th>
            <th className="py-1 pr-3">Outcome</th>
            <th className="py-1 pr-3">Session</th>
            <th className="py-1 pr-3">R</th>
            <th className="py-1">Rule</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
          {rows.map((r) => (
            <tr key={r.id} className="align-top">
              <td className="py-1.5 pr-3 font-mono text-xs">{formatUtcDateTime(r.created_at)}</td>
              <td className="py-1.5 pr-3 font-semibold">
                <Link to={`/signals/${r.id}`} className="underline decoration-slate-400 underline-offset-2">
                  {r.symbol}
                </Link>
              </td>
              <td className="py-1.5 pr-3">{dash(r.direction)}</td>
              <td className="py-1.5 pr-3">
                <Badge tone={KIND_TONE[r.decision_kind]}>{r.decision_kind}</Badge>
              </td>
              <td className="py-1.5 pr-3">{dash(r.judge_ruling)}</td>
              <td className="py-1.5 pr-3">
                {r.outcome ? <Badge tone={outcomeTone(r.outcome)}>{r.outcome}</Badge> : "—"}
              </td>
              <td className="py-1.5 pr-3">{dash(r.session)}</td>
              <td className="py-1.5 pr-3 font-mono text-xs">{formatR(r.realized_r)}</td>
              <td className="max-w-[14rem] break-words py-1.5 text-xs">{dash(r.violated_rule)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
