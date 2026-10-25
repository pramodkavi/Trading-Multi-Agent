import type {
  OverviewResponse,
  SetupCardOut,
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
