import { isDbResuming } from "../api/client";
import type { DbProbe, Health } from "../api/queries";

interface QueryState<T> {
  data: T | undefined;
  error: unknown;
  failureReason: unknown;
  isPending: boolean;
}

export type HealthState = QueryState<Health>;
export type DbState = QueryState<DbProbe>;
export type ApiStatus = "ok" | "waking" | "error" | "checking";

/** Header chip state from `GET /api/health` + `GET /api/system/db`. */
export function deriveApiStatus(health: HealthState, db: DbState): ApiStatus {
  if (db.data?.db === "resuming" || isDbResuming(db.error) || isDbResuming(db.failureReason)) {
    return "waking";
  }
  if (health.error || db.error || db.data?.db === "error") return "error";
  if (health.data && health.data.status !== "ok") return "error";
  if (health.isPending || db.isPending) return "checking";
  return "ok";
}

export function formatAgo(updatedAt: number, now: number): string {
  if (updatedAt <= 0) return "not updated yet";
  const seconds = Math.max(0, Math.round((now - updatedAt) / 1000));
  if (seconds < 120) return `updated ${seconds} s ago`;
  return `updated ${Math.round(seconds / 60)} min ago`;
}
