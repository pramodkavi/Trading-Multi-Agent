import {
  apiStatusLabel,
  deriveApiStatus,
  type ApiStatus,
  type DbState,
  type HealthState,
} from "../lib/apiStatus";

// Text colours chosen for ≥ 4.5:1 on their backgrounds in both themes.
const STYLE: Record<ApiStatus, string> = {
  ok: "bg-emerald-50 text-emerald-800 ring-emerald-600/30 dark:bg-emerald-950 dark:text-emerald-200 dark:ring-emerald-400/30",
  waking:
    "bg-amber-50 text-amber-900 ring-amber-600/30 dark:bg-amber-950 dark:text-amber-200 dark:ring-amber-400/30",
  error:
    "bg-red-50 text-red-800 ring-red-600/30 dark:bg-red-950 dark:text-red-200 dark:ring-red-400/30",
  checking:
    "bg-slate-100 text-slate-800 ring-slate-500/30 dark:bg-slate-800 dark:text-slate-200 dark:ring-slate-400/30",
};

const DOT: Record<ApiStatus, string> = {
  ok: "bg-emerald-600 dark:bg-emerald-400",
  waking: "bg-amber-500 dark:bg-amber-400 motion-safe:animate-pulse",
  error: "bg-red-600 dark:bg-red-400",
  checking: "bg-slate-500 dark:bg-slate-400",
};

export function StatusChip({ health, db }: { health: HealthState; db: DbState }) {
  const status = deriveApiStatus(health, db);
  // Response bodies are untrusted JSON: guard the shape before using it.
  const rawVersion: unknown = health.data?.version;
  const version = typeof rawVersion === "string" ? rawVersion.slice(0, 7) : undefined;
  const rawLatency: unknown = db.data?.latency_ms;
  const latency = status === "ok" && typeof rawLatency === "number" ? rawLatency : undefined;
  return (
    <span
      role="status"
      aria-live="polite"
      data-status={status}
      title={latency !== undefined ? `Database probe ${latency} ms` : undefined}
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${STYLE[status]}`}
    >
      <span aria-hidden="true" className={`h-2 w-2 rounded-full ${DOT[status]}`} />
      <span>{apiStatusLabel(status, health, db)}</span>
      {version ? <span className="font-mono opacity-90">v{version}</span> : null}
    </span>
  );
}
