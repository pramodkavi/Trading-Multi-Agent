import { Link } from "react-router-dom";
import { formatUtcDateTime } from "../../lib/format";
import type { TimelineStep } from "../../lib/timeline";
import { outcomeTone } from "../../lib/tones";
import { Badge } from "../Badge";
import { mutedClass } from "../styles";
import { RawOutput } from "./RawOutput";

interface Match {
  signal_id: string;
  symbol: string;
  outcome: string | null;
}

function matchesOf(step: TimelineStep): Match[] {
  const raw = step.details?.matches;
  return Array.isArray(raw) ? (raw as Match[]) : [];
}

/** One timeline entry: title, summary, accounting, Historian neighbour links, lazy raw output. */
export function StepCard({ step, signalId }: { step: TimelineStep; signalId: string }) {
  const matches = step.kind === "historian" ? matchesOf(step) : [];
  const meta = [
    step.at ? formatUtcDateTime(step.at) : null,
    step.latencyMs !== null ? `${step.latencyMs} ms` : null,
    step.costUsd !== null ? `$${step.costUsd.toFixed(4)}` : null,
  ].filter((m): m is string => m !== null);

  return (
    <li
      data-testid="timeline-step"
      data-tone={step.tone}
      className="relative min-w-0 border-l-2 border-slate-300 pb-5 pl-4 last:pb-0 dark:border-slate-600"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-semibold text-slate-900 dark:text-slate-100">{step.title}</h3>
        <Badge tone={step.tone}>{step.kind}</Badge>
        {meta.length > 0 ? <span className={`${mutedClass} font-mono text-xs`}>{meta.join(" · ")}</span> : null}
      </div>
      <p className="mt-1 break-words text-sm text-slate-900 dark:text-slate-100">{step.summary}</p>
      {matches.length > 0 ? (
        <ul className="mt-2 flex flex-wrap gap-2">
          {matches.map((m) => (
            <li key={m.signal_id}>
              <Link
                to={`/signals/${m.signal_id}`}
                className="inline-flex items-center gap-1.5 text-sm underline decoration-slate-400 underline-offset-2"
              >
                <span>{m.symbol}</span>
                {m.outcome ? <Badge tone={outcomeTone(m.outcome)}>{m.outcome}</Badge> : null}
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
      {step.runId ? <RawOutput signalId={signalId} runId={step.runId} /> : null}
    </li>
  );
}
