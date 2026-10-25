import { QueryClient, useQuery } from "@tanstack/react-query";
import { ApiError, apiFetch, isDbResuming } from "./client";
import type {
  AgentsResponse,
  AlarmsResponse,
  ConfigResponse,
  MetricsResponse,
  OverviewResponse,
  ScansResponse,
  SignalsPage,
  SweepsResponse,
} from "./types";

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

export const overviewKey = ["overview"] as const;
export const signalsKey = (limit: number) => ["signals", { limit }] as const;
export const scansKey = ["system", "scans"] as const;
export const agentsKey = (days: number) => ["system", "agents", { days }] as const;
export const sweepsKey = (days: number) => ["system", "sweeps", { days }] as const;
export const alarmsKey = ["system", "alarms"] as const;
export const metricsKey = (days: number) => ["system", "metrics", { days }] as const;
export const configKey = ["system", "config"] as const;

/** CloudWatch metrics bill per request: fetch once per page view, never on the 60 s poll. */
const METRICS_STALE_MS = 300_000;

/** `GET /api/overview` — the whole landing page in one round trip. */
export function useOverview() {
  return useQuery({
    queryKey: overviewKey,
    queryFn: ({ signal }) => apiFetch<OverviewResponse>("/overview", { signal }),
  });
}

/** `GET /api/signals?limit=n` — the newest journal rows. */
export function useRecentSignals(limit = 5) {
  return useQuery({
    queryKey: signalsKey(limit),
    queryFn: ({ signal }) => apiFetch<SignalsPage>(`/signals?limit=${limit}`, { signal }),
  });
}

/** `GET /api/system/scans` — the latest 50 scan runs. */
export function useScans() {
  return useQuery({
    queryKey: scansKey,
    queryFn: ({ signal }) => apiFetch<ScansResponse>("/system/scans", { signal }),
  });
}

/** `GET /api/system/agents?days=n` — per-role usage and LLM cost per day. */
export function useAgents(days = 7) {
  return useQuery({
    queryKey: agentsKey(days),
    queryFn: ({ signal }) => apiFetch<AgentsResponse>(`/system/agents?days=${days}`, { signal }),
  });
}

/** `GET /api/system/sweeps?days=n` — Forecaster evaluations per sweep. */
export function useSweeps(days = 7) {
  return useQuery({
    queryKey: sweepsKey(days),
    queryFn: ({ signal }) => apiFetch<SweepsResponse>(`/system/sweeps?days=${days}`, { signal }),
  });
}

/** `GET /api/system/alarms` — the Monitoring alarm states (null + note on a CloudWatch error). */
export function useAlarms() {
  return useQuery({
    queryKey: alarmsKey,
    queryFn: ({ signal }) => apiFetch<AlarmsResponse>("/system/alarms", { signal }),
  });
}

/** `GET /api/system/metrics?days=n` — the scan Lambda's daily metrics; no polling. */
export function useMetrics(days = 7) {
  return useQuery({
    queryKey: metricsKey(days),
    queryFn: ({ signal }) => apiFetch<MetricsResponse>(`/system/metrics?days=${days}`, { signal }),
    refetchInterval: false,
    staleTime: METRICS_STALE_MS,
  });
}

/** `GET /api/system/config` — the inferred config panel. */
export function useSystemConfig() {
  return useQuery({
    queryKey: configKey,
    queryFn: ({ signal }) => apiFetch<ConfigResponse>("/system/config", { signal }),
  });
}
