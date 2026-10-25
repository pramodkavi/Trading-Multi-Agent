import { useRecentSignals } from "../../api/queries";
import type { DecisionKind } from "../../api/types";
import { formatUtcDateTime } from "../../lib/format";
import type { Tone } from "../../lib/problems";
import { Badge } from "../Badge";
import { cardClass, mutedClass } from "../styles";

const KIND_TONE: Record<DecisionKind, Tone> = {
  PUBLISHED: "ok",
  VETOED: "warn",
  GATE_REJECTED: "neutral",
  NO_SETUP: "neutral",
};

/** The five newest decisions. Plain text until the signal detail route lands (Step 4.7). */
export function LatestSignals() {
  const query = useRecentSignals(5);
  if (query.isPending) return <p className={mutedClass}>Loading signals…</p>;
  if (query.isError) return <p className={mutedClass}>Could not load signals.</p>;
  if (query.data.items.length === 0) return <p className={mutedClass}>No signals yet.</p>;
  return (
    <ul data-testid="latest-signals" className={`${cardClass} divide-y divide-slate-200 dark:divide-slate-700`}>
      {query.data.items.map((s) => (
        <li key={s.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm first:pt-0 last:pb-0">
          <span className="font-mono text-xs text-slate-700 dark:text-slate-300">
            {formatUtcDateTime(s.created_at)}
          </span>
          <span className="font-semibold text-slate-900 dark:text-slate-100">{s.symbol}</span>
          <span className={mutedClass}>{s.direction ?? "—"}</span>
          <Badge tone={KIND_TONE[s.decision_kind]}>{s.decision_kind}</Badge>
          {s.judge_ruling ? <span className={mutedClass}>{s.judge_ruling}</span> : null}
        </li>
      ))}
    </ul>
  );
}
