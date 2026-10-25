import { Link, useParams } from "react-router-dom";
import { ApiError, isDbResuming } from "../api/client";
import { useSignal } from "../api/queries";
import { Badge } from "../components/Badge";
import { SetupAndOutcome } from "../components/detail/SetupAndOutcome";
import { StepCard } from "../components/detail/StepCard";
import { buttonClass } from "../components/ErrorPage";
import { Section } from "../components/Section";
import { mutedClass } from "../components/styles";
import { formatUtcDateTime } from "../lib/format";
import { buildTimeline } from "../lib/timeline";
import { KIND_TONE, outcomeTone } from "../lib/tones";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const backLink = (
  <Link to="/signals" className="text-sm underline decoration-slate-400 underline-offset-2">
    ← Back to signals
  </Link>
);

function NotFound() {
  return (
    <section>
      <h1 className="mb-2 text-2xl font-semibold text-slate-900 dark:text-slate-100">Signal not found</h1>
      <p className={`${mutedClass} mb-3`}>No decision with this id exists in the journal.</p>
      {backLink}
    </section>
  );
}

function Loaded({ id }: { id: string }) {
  const query = useSignal(id);

  if (query.data === undefined) {
    if (query.isPending) {
      return (
        <p className={mutedClass}>{isDbResuming(query.failureReason) ? "Waking the database…" : "Loading signal…"}</p>
      );
    }
    if (query.error instanceof ApiError && query.error.status === 404) return <NotFound />;
    return (
      <div
        role="alert"
        className="rounded-lg border-2 border-red-600/50 bg-red-50 p-4 text-red-900 dark:border-red-400/50 dark:bg-red-950 dark:text-red-100"
      >
        <p className="font-semibold">Could not load the signal</p>
        <p className="break-words text-sm">{query.error?.message}</p>
        <button type="button" className={`${buttonClass} mt-3`} onClick={() => void query.refetch()}>
          Retry
        </button>
      </div>
    );
  }

  const l = query.data;
  const s = l.signal;
  const steps = buildTimeline(l);
  return (
    <section>
      <div className="mb-3">{backLink}</div>
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">
        {[s.symbol, s.direction].filter(Boolean).join(" ")}
      </h1>
      {query.isError ? (
        <p role="status" className="mt-2 text-sm text-amber-900 dark:text-amber-200">
          Could not refresh the signal — showing the last data received.
        </p>
      ) : null}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Badge tone={KIND_TONE[s.decision_kind]}>{s.decision_kind}</Badge>
        {s.outcome ? <Badge tone={outcomeTone(s.outcome)}>{s.outcome}</Badge> : null}
        {s.judge_ruling ? <span className={mutedClass}>{`Judge: ${s.judge_ruling}`}</span> : null}
        {s.session ? <span className={mutedClass}>{`Session: ${s.session}`}</span> : null}
        <span className={`${mutedClass} font-mono text-xs`}>{formatUtcDateTime(s.created_at)} UTC</span>
      </div>
      {s.tags.length > 0 ? (
        <p className="mt-2 flex flex-wrap gap-1.5">
          {s.tags.map((t) => (
            <Badge key={t} tone="neutral">
              {t}
            </Badge>
          ))}
        </p>
      ) : null}
      <p className={`${mutedClass} mt-2 break-all font-mono text-xs`}>{s.id}</p>

      <div className="mt-6 grid min-w-0 gap-6">
        <Section title="Decision chain">
          <ol className="min-w-0">
            {steps.map((step) => (
              <StepCard key={step.id} step={step} signalId={s.id} />
            ))}
          </ol>
        </Section>
        <SetupAndOutcome lifecycle={l} />
      </div>
    </section>
  );
}

export default function SignalDetailPage() {
  const { id = "" } = useParams();
  return UUID.test(id) ? <Loaded id={id} /> : <NotFound />;
}
