import type {
  AgentsResponse,
  AlarmsResponse,
  ConfigResponse,
  MetricsResponse,
  OverviewResponse,
  ScanRowOut,
  ScansResponse,
  SetupCardOut,
  SignalRowOut,
  SweepsResponse,
  WindowOutcome,
  WindowSummary,
} from "../api/types";

export const CREDIT_ERROR =
  "BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', " +
  "'message': 'Your credit balance is too low to access the Anthropic API.'}}";

export function makeOutcome(overrides: Partial<WindowOutcome> = {}): WindowOutcome {
  return {
    label: "LONDON",
    started_at: "2026-10-25T08:03:00Z",
    status: "SUCCESS",
    symbols: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
    error: null,
    ...overrides,
  };
}

export function makeWindow(
  label: string,
  fireTime: string,
  scan: WindowOutcome | null,
): WindowSummary {
  return {
    label,
    fire_time: fireTime,
    scan,
    symbols: scan
      ? [{ symbol: "BTCUSDT", kind: "NO_SETUP", violated_rule: null, signal_id: `sig-${label}` }]
      : [],
  };
}

export function makeSetup(overrides: Partial<SetupCardOut> = {}): SetupCardOut {
  return {
    setup_id: "setup-1",
    signal_id: "sig-1",
    symbol: "BTCUSDT",
    direction: "LONG",
    entry_price: 100,
    stop_loss: 95,
    take_profit_1: 115,
    take_profit_2: null,
    opened_at: "2026-10-25T08:10:00Z",
    age_seconds: 3600,
    last_evaluated_at: "2026-10-25T09:05:00Z",
    evaluation_age_seconds: 300,
    latest_status: "STILL_VALID",
    latest_reasoning: "Structure intact.",
    unrealised_r: 0.5,
    unrealised_r_note: null,
    ...overrides,
  };
}

/** A healthy overview at 2026-10-25 10:00 UTC; override pieces per test. */
export function makeOverview(overrides: Partial<OverviewResponse> = {}): OverviewResponse {
  return {
    generated_at: "2026-10-25T10:00:00Z",
    health: {
      last_scan: makeOutcome(),
      forecaster: {
        open_setups: 1,
        stale: 0,
        evaluations_last_window: 1,
        errors_last_window: 0,
      },
      alarms: [
        {
          name: "ProviderErrorAlarm",
          state: "OK",
          updated_at: "2026-10-24T00:00:00Z",
          reason: null,
        },
      ],
      alarms_note: null,
      db: "ok",
    },
    risk: {
      open_setups: 1,
      open_cap: 3,
      published_24h: 1,
      daily_cap: 5,
      loss_streak: 0,
      pause_active: false,
      pause_until: null,
      session: "LONDON",
      session_blocked: false,
      next_scan_at: "2026-10-25T13:03:00Z",
      seconds_to_next_scan: 10_980,
    },
    open_setups: [makeSetup()],
    windows_today: [
      makeWindow("LONDON", "2026-10-25T08:03:00Z", makeOutcome()),
      makeWindow("NY", "2026-10-25T13:03:00Z", null),
      makeWindow("OVERLAP", "2026-10-25T15:03:00Z", null),
      makeWindow("DAILY_WRAP", "2026-10-25T22:03:00Z", null),
    ],
    ...overrides,
  };
}

/** A journal row; defaults to a published BTCUSDT long, override per test. */
export function makeRow(overrides: Partial<SignalRowOut> = {}): SignalRowOut {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    scan_id: "scan-1",
    symbol: "BTCUSDT",
    strategy: "smc",
    direction: "LONG",
    status: "PUBLISHED",
    created_at: "2026-10-25T08:03:00Z",
    tags: ["htf-bias"],
    outcome: null,
    entry_price: 100,
    stop_loss: 95,
    take_profit_1: 115,
    take_profit_2: null,
    risk_reward_ratio: 3,
    skip_reason: null,
    skip_details: null,
    decision_kind: "PUBLISHED",
    judge_ruling: "PUBLISH",
    session: "LONDON",
    has_setup: true,
    violated_rule: null,
    resolution_outcome: null,
    realized_r: null,
    ...overrides,
  };
}

// -- System health (P7) fixtures ------------------------------------------------

export function makeScanRow(overrides: Partial<ScanRowOut> = {}): ScanRowOut {
  return {
    id: "scan-1",
    window: "LONDON",
    started_at: "2026-10-25T08:03:00Z",
    completed_at: "2026-10-25T08:04:10Z",
    duration_s: 70,
    status: "SUCCESS",
    symbols: ["BTCUSDT"],
    error_message: null,
    ...overrides,
  };
}

export function makeScans(items: ScanRowOut[] = [makeScanRow()]): ScansResponse {
  return { items };
}

export function makeAlarms(overrides: Partial<AlarmsResponse> = {}): AlarmsResponse {
  return {
    alarms: [
      { name: "ProviderErrorAlarm", state: "OK", updated_at: "2026-10-24T00:00:00Z", reason: "ok" },
      { name: "ForecasterErrorAlarm", state: "OK", updated_at: "2026-10-24T00:00:00Z", reason: null },
    ],
    note: null,
    ...overrides,
  };
}

export function makeAgents(overrides: Partial<AgentsResponse> = {}): AgentsResponse {
  return {
    since: "2026-10-19T00:00:00Z",
    until: "2026-10-25T10:00:00Z",
    usage: [
      { date: "2026-10-25", role: "skeptic", runs: 4, errors: null, cost_usd: 0.1234 },
      { date: "2026-10-25", role: "scan", runs: 4, errors: 3, cost_usd: 0 },
    ],
    cost_per_day: [
      { date: "2026-10-24", total_usd: 0.5, by_role: { skeptic: 0.5 } },
      { date: "2026-10-25", total_usd: 0.25, by_role: { skeptic: 0.25 } },
    ],
    ...overrides,
  };
}

export function makeSweeps(overrides: Partial<SweepsResponse> = {}): SweepsResponse {
  return {
    since: "2026-10-19T00:00:00Z",
    until: "2026-10-25T10:00:00Z",
    sweeps: [
      { sweep_start: "2026-10-25T08:08:00Z", evaluations: 1, errors: 0 },
      { sweep_start: "2026-10-25T13:08:00Z", evaluations: 1, errors: 1 },
    ],
    ...overrides,
  };
}

export function makeMetrics(overrides: Partial<MetricsResponse> = {}): MetricsResponse {
  return {
    function_name: "CryptoSignals-Compute-ScanLambda-abc",
    days: [
      { date: "2026-10-25", invocations: 5, errors: 1, duration_p95_ms: 61234.5 },
      { date: "2026-10-24", invocations: 5, errors: 0, duration_p95_ms: null },
    ],
    note: null,
    ...overrides,
  };
}

export function makeConfig(): ConfigResponse {
  return {
    watchlist: { value: ["BTCUSDT", "ETHUSDT"], inferred: true, source: "scan_runs, last 7 days" },
    model: { value: "claude-sonnet-4-5", inferred: true, source: "latest agent run" },
    macro_providers_configured: { value: true, inferred: true, source: "last 10 Skeptic outputs" },
    langfuse_configured: { value: false, inferred: true, source: "dashboard settings" },
    schedule: {
      windows: [
        { label: "LONDON", time_utc: "08:03" },
        { label: "NY", time_utc: "13:03" },
      ],
      inferred: false,
      source: "SPEC schedule",
    },
  };
}

/** Routes for every `/system/*` call the Health page makes, healthy by default. */
export function healthRoutes(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    "/system/db": { db: "ok", latency_ms: 9 },
    "/system/scans": makeScans(),
    "/system/alarms": makeAlarms(),
    "/system/agents": makeAgents(),
    "/system/sweeps": makeSweeps(),
    "/system/metrics": makeMetrics(),
    "/system/config": makeConfig(),
    ...overrides,
  };
}
