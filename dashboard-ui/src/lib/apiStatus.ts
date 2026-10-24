import { ApiError, isDbResuming } from "../api/client";
import type { DbProbe, Health } from "../api/queries";

interface QueryState<T> {
  data: T | undefined;
  error: unknown;
  failureReason: unknown;
  isPending: boolean;
  isFetching: boolean;
}

export type HealthState = QueryState<Health>;
export type DbState = QueryState<DbProbe>;
export type ApiStatus = "ok" | "waking" | "error" | "checking";

/** Header chip state from `GET /api/health` + `GET /api/system/db`. */
export function deriveApiStatus(health: HealthState, db: DbState): ApiStatus {
  // Still retrying a 503 db_resuming (bounded backoff in queries.ts) → waking.
  if (db.data?.db === "resuming" || (db.isFetching && isDbResuming(db.failureReason))) {
    return "waking";
  }
  // Retries exhausted (error set, no fetch in flight) → error until the next poll.
  if (health.error || db.error || db.data?.db === "error") return "error";
  if (health.data && health.data.status !== "ok") return "error";
  if (health.isPending || db.isPending) return "checking";
  return "ok";
}

/** Human label for the chip; error variants say what went wrong. */
export function apiStatusLabel(status: ApiStatus, health: HealthState, db: DbState): string {
  switch (status) {
    case "ok":
      return "API ok";
    case "waking":
      return "Waking the database…";
    case "checking":
      return "Checking API…";
    case "error":
      if (isDbResuming(db.error)) return "Database did not wake";
      if (db.error instanceof ApiError && db.error.reason === "unauthorized_after_signin") {
        return "Signed in, but the API rejected the token";
      }
      if (health.error && !db.error) return "API unreachable";
      return "API error";
  }
}

export function formatAgo(updatedAt: number, now: number): string {
  if (updatedAt <= 0) return "not updated yet";
  const seconds = Math.max(0, Math.round((now - updatedAt) / 1000));
  if (seconds < 120) return `updated ${seconds} s ago`;
  return `updated ${Math.round(seconds / 60)} min ago`;
}
