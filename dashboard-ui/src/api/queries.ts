import { QueryClient, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ApiError, apiFetch, isDbResuming } from "./client";
import { toApiQuery } from "../lib/journalFilters";
import { periodSince, type PeriodDays } from "../lib/period";
import type {
  AgentRunOut,
  AgentsResponse,
  AlarmsResponse,
  ConfigResponse,
  CriticReportOut,
  CriticReportsPage,
  EquityCurveResponse,
  FunnelGroupBy,
  FunnelResponse,
  LifecycleResponse,
  MetricsResponse,
  OverviewResponse,
  ReviewsPage,
  ScansResponse,
  SignalFilters,
  SignalsPage,
  SkepticValueResponse,
  SweepsResponse,
  WinRateBy,
  WinRateResponse,
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

export const JOURNAL_PAGE_SIZE = 50;
export const signalsJournalKey = (filters: SignalFilters) => ["signals", "journal", filters] as const;
export const signalKey = (id: string) => ["signal", id] as const;
export const agentRunKey = (signalId: string, runId: string) => ["signal", signalId, "run", runId] as const;

/** `GET /api/signals` — the journal, paged by the server's opaque keyset cursor. */
export function useSignalsJournal(filters: SignalFilters) {
  return useInfiniteQuery({
    queryKey: signalsJournalKey(filters),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) =>
      apiFetch<SignalsPage>(`/signals?${toApiQuery(filters, JOURNAL_PAGE_SIZE, pageParam)}`, { signal }),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
}

/** `GET /api/signals/{id}` — one decision's whole lifecycle. */
export function useSignal(id: string) {
  return useQuery({
    queryKey: signalKey(id),
    queryFn: ({ signal }) => apiFetch<LifecycleResponse>(`/signals/${id}`, { signal }),
  });
}

/** `GET /api/signals/{id}/runs/{run_id}` — the full output of one agent step; only when `enabled`. */
export function useAgentRun(signalId: string, runId: string, enabled: boolean) {
  return useQuery({
    queryKey: agentRunKey(signalId, runId),
    queryFn: ({ signal }) =>
      apiFetch<AgentRunOut>(`/signals/${signalId}/runs/${runId}`, { signal }),
    enabled,
  });
}

// -- P4 funnel, P6 analytics, Critic ---------------------------------------------------

/** `?since=<utc-midnight>&k=v…` — only a midnight `since`, so keys are stable for a UTC day. */
function periodQuery(days: PeriodDays, extra: Record<string, string> = {}): string {
  return new URLSearchParams({ since: periodSince(days), ...extra }).toString();
}

export const funnelKey = (since: string, groupBy: FunnelGroupBy) => ["funnel", since, groupBy] as const;
export const winRateKey = (since: string, by: WinRateBy) => ["analytics", "win-rate", since, by] as const;
export const skepticKey = (since: string) => ["analytics", "skeptic-value", since] as const;
export const equityKey = (since: string) => ["analytics", "equity-curve", since] as const;
export const criticReportsKey = (limit: number) => ["critic", "reports", limit] as const;
export const criticReportKey = (id: string) => ["critic", "report", id] as const;
export const criticReviewsKey = (limit: number) => ["critic", "reviews", limit] as const;

/** `GET /api/funnel` — stage counts and rejections per day / window / symbol. */
export function useFunnel(days: PeriodDays, groupBy: FunnelGroupBy) {
  return useQuery({
    queryKey: funnelKey(periodSince(days), groupBy),
    queryFn: ({ signal }) =>
      apiFetch<FunnelResponse>(`/funnel?${periodQuery(days, { group_by: groupBy })}`, { signal }),
  });
}

/** `GET /api/analytics/win-rate` — published win rate per tag / session / strategy / week. */
export function useWinRate(days: PeriodDays, by: WinRateBy) {
  return useQuery({
    queryKey: winRateKey(periodSince(days), by),
    queryFn: ({ signal }) =>
      apiFetch<WinRateResponse>(`/analytics/win-rate?${periodQuery(days, { by })}`, { signal }),
  });
}

/** `GET /api/analytics/skeptic-value` — outcomes by how the Judge treated the objection. */
export function useSkepticValue(days: PeriodDays) {
  return useQuery({
    queryKey: skepticKey(periodSince(days)),
    queryFn: ({ signal }) =>
      apiFetch<SkepticValueResponse>(`/analytics/skeptic-value?${periodQuery(days)}`, { signal }),
  });
}

/** `GET /api/analytics/equity-curve` — the hypothetical R curves. */
export function useEquityCurve(days: PeriodDays) {
  return useQuery({
    queryKey: equityKey(periodSince(days)),
    queryFn: ({ signal }) =>
      apiFetch<EquityCurveResponse>(`/analytics/equity-curve?${periodQuery(days)}`, { signal }),
  });
}

/** `GET /api/critic/reports?limit=n` — the latest Critic reports (no markdown). */
export function useCriticReports(limit = 5) {
  return useQuery({
    queryKey: criticReportsKey(limit),
    queryFn: ({ signal }) => apiFetch<CriticReportsPage>(`/critic/reports?limit=${limit}`, { signal }),
  });
}

/** `GET /api/critic/reports/{id}` — one report with its markdown; only when `enabled`. */
export function useCriticReport(id: string, enabled: boolean) {
  return useQuery({
    queryKey: criticReportKey(id),
    queryFn: ({ signal }) => apiFetch<CriticReportOut>(`/critic/reports/${id}`, { signal }),
    enabled,
  });
}

/** `GET /api/critic/reviews?limit=n` — the latest decision-review grades. */
export function useCriticReviews(limit = 10) {
  return useQuery({
    queryKey: criticReviewsKey(limit),
    queryFn: ({ signal }) => apiFetch<ReviewsPage>(`/critic/reviews?limit=${limit}`, { signal }),
  });
}
