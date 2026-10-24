import { QueryClient, useQuery } from "@tanstack/react-query";
import { ApiError, apiFetch, isDbResuming } from "./client";

export interface Health {
  status: string;
  version: string;
}

export interface DbProbe {
  db: "ok" | "resuming" | "error";
  latency_ms: number;
}

/** 503 `db_resuming` backoff: 2+4+8+15+15+15 s ≈ 60 s total (design §3.4). */
const DB_RESUMING_DELAYS_MS = [2_000, 4_000, 8_000, 15_000, 15_000, 15_000] as const;

export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isDbResuming(error)) return failureCount < DB_RESUMING_DELAYS_MS.length;
  // Client errors (incl. 401 — apiFetch already renewed/redirected) won't fix themselves.
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < 2;
}

export function retryDelay(failureIndex: number, error: unknown): number {
  if (isDbResuming(error)) {
    return DB_RESUMING_DELAYS_MS[Math.min(failureIndex, DB_RESUMING_DELAYS_MS.length - 1)] ?? 15_000;
  }
  return Math.min(1_000 * 2 ** failureIndex, 8_000);
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        refetchInterval: 60_000,
        refetchIntervalInBackground: false,
        staleTime: 30_000,
        retry: shouldRetry,
        retryDelay,
      },
    },
  });
}

export const healthKey = ["health"] as const;
export const dbProbeKey = ["system", "db"] as const;

/** `GET /api/health` — open, DB-free; carries the deployed version. */
export function useHealth() {
  return useQuery({
    queryKey: healthKey,
    queryFn: ({ signal }) => apiFetch<Health>("/health", { auth: false, signal }),
  });
}

/** `GET /api/system/db` — authenticated; wakes Aurora if it is paused. */
export function useDbProbe() {
  return useQuery({
    queryKey: dbProbeKey,
    queryFn: ({ signal }) => apiFetch<DbProbe>("/system/db", { signal }),
  });
}
