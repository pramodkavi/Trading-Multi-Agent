import type {
  AgentsResponse,
  AlarmsResponse,
  ConfigResponse,
  CriticReportsPage,
  EquityCurveResponse,
  EquityPoint,
  FunnelGroup,
  FunnelResponse,
  EvaluationOut,
  LifecycleResponse,
  LifecycleStep,
  MetricsResponse,
  OverviewResponse,
  ScanRowOut,
  ReviewsPage,
  ScansResponse,
  SetupCardOut,
  SignalRowOut,
  SkepticValueResponse,
  SweepsResponse,
  WinRateResponse,
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

export const SIGNAL_ID = "11111111-1111-4111-8111-111111111111";
export const PAST_SIGNAL_ID = "22222222-2222-4222-8222-222222222222";

export function makeStep(role: string, summary: Record<string, unknown>, n: number): LifecycleStep {
  return {
    role,
    run_id: `run-${role}-${n}`,
    created_at: `2026-10-25T08:03:0${n}Z`,
    latency_ms: 1200,
    cost_usd: role === "skeptic" || role === "judge" ? 0.0123 : null,
    summary,
  };
}

export function makeEvaluation(overrides: Partial<EvaluationOut> = {}): EvaluationOut {
  return {
    id: "eval-1",
    setup_id: "setup-1",
    signal_id: SIGNAL_ID,
    evaluated_at: "2026-10-25T09:05:00Z",
    status: "STILL_VALID",
    outcome: null,
    reasoning: "Structure intact.",
    error: null,
    latency_ms: 900,
    token_usage: {},
    cost_usd: 0.004,
    ...overrides,
  };
}

/** A full PUBLISHED chain: analyzer → historian → skeptic → judge → 2 evaluations → replay → review. */
export function makeLifecycle(overrides: Partial<LifecycleResponse> = {}): LifecycleResponse {
  return {
    signal: {
      ...makeRow({ id: SIGNAL_ID, outcome: "WIN", realized_r: 3 }),
      payload: { symbol: "BTCUSDT", entry_price: 100 },
      features: { ob_zone_low: 98 },
      outcome_metadata: null,
    },
    steps: [
      makeStep(
        "analyzer",
        {
          direction: "LONG",
          entry_price: 100,
          stop_loss: 95,
          take_profit_1: 115,
          take_profit_2: null,
          risk_reward_ratio: 3,
          tags: ["htf-bias"],
        },
        1,
      ),
      makeStep(
        "historian",
        {
          win_rate: 0.6,
          sample_size: 5,
          wins: 3,
          losses: 2,
          matches: [{ signal_id: PAST_SIGNAL_ID, symbol: "ETHUSDT", outcome: "WIN" }],
        },
        2,
      ),
      makeStep("skeptic", { severity: "MEDIUM", recommends_against: false, headline: "DXY rising" }, 3),
      makeStep("judge", { ruling: "PUBLISH", confidence: 0.8, caveat: null }, 4),
    ],
    evaluations: [
      makeEvaluation({ id: "e1" }),
      makeEvaluation({
        id: "e2",
        evaluated_at: "2026-10-25T13:05:00Z",
        status: "INVALIDATED",
        outcome: "WIN",
        reasoning: "TP1 reached.",
      }),
    ],
    resolution: {
      signal_id: SIGNAL_ID,
      decision_kind: "PUBLISHED",
      outcome: "WIN",
      decided_at: "2026-10-25T08:03:00Z",
      entered_at: "2026-10-25T08:30:00Z",
      resolved_at: "2026-10-25T13:00:00Z",
      realized_r: 3,
      max_adverse_excursion_r: 0.4,
      max_favorable_excursion_r: 3.2,
      tp2_reached: false,
      candles_used: 120,
      engine_version: "v1",
    },
    review: {
      signal_id: SIGNAL_ID,
      decision_kind: "PUBLISHED",
      review: { lesson: "Good entry at the OB." },
      model: "claude-sonnet-4-5",
      token_usage: {},
      cost_usd: 0.02,
      error: null,
      attempts: 1,
      created_at: "2026-10-26T00:00:00Z",
    },
    setup: makeSetup({ signal_id: SIGNAL_ID }),
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

// -- Funnel (P4) and analytics (P6) fixtures -------------------------------------

export function makeFunnelGroup(key = "total", overrides: Partial<FunnelGroup> = {}): FunnelGroup {
  return {
    key,
    scans: 40,
    symbols_scanned: 160,
    proposals: 30,
    gate_rejected: 18,
    gate_passed: 12,
    judge_publish: 2,
    judge_caveat: 1,
    judge_skip: 9,
    judge_none: 0,
    published: 3,
    rejections_by_rule: { RULE_2_MIN_RR: 10, RULE_3_PREMIUM_DISCOUNT: 8 },
    ...overrides,
  };
}

export function makeFunnel(overrides: Partial<FunnelResponse> = {}): FunnelResponse {
  return {
    since: "2026-09-25T00:00:00Z",
    until: "2026-10-25T10:00:00Z",
    group_by: "day",
    groups: [
      makeFunnelGroup("2026-10-24", {
        scans: 20,
        symbols_scanned: 80,
        proposals: 14,
        gate_rejected: 9,
        gate_passed: 5,
        published: 1,
        rejections_by_rule: { RULE_2_MIN_RR: 6, RULE_3_PREMIUM_DISCOUNT: 3 },
      }),
      makeFunnelGroup("2026-10-25", {
        scans: 20,
        symbols_scanned: 80,
        proposals: 16,
        gate_rejected: 9,
        gate_passed: 7,
        published: 2,
        rejections_by_rule: { RULE_2_MIN_RR: 4, RULE_3_PREMIUM_DISCOUNT: 5 },
      }),
    ],
    totals: makeFunnelGroup("total"),
    top_rejections_7d: [
      { rule: "RULE_2_MIN_RR", count: 6 },
      { rule: "RULE_3_PREMIUM_DISCOUNT", count: 4 },
    ],
    top_rejections_30d: [
      { rule: "RULE_2_MIN_RR", count: 10 },
      { rule: "RULE_3_PREMIUM_DISCOUNT", count: 8 },
    ],
    ...overrides,
  };
}

export function makeWinRate(overrides: Partial<WinRateResponse> = {}): WinRateResponse {
  return {
    since: "2026-07-27T00:00:00Z",
    until: "2026-10-25T10:00:00Z",
    by: "tag",
    buckets: [
      { key: "htf-bias", wins: 5, losses: 3, decisive: 8, win_rate: 0.625, n: 10 },
      { key: "liquidity-sweep", wins: 1, losses: 2, decisive: 3, win_rate: 1 / 3, n: 4 },
      { key: "unresolved-tag", wins: 0, losses: 0, decisive: 0, win_rate: null, n: 2 },
    ],
    source_counts: { replayed: 9, forecaster: 3 },
    ...overrides,
  };
}

export function makeSkeptic(overrides: Partial<SkepticValueResponse> = {}): SkepticValueResponse {
  return {
    since: "2026-07-27T00:00:00Z",
    until: "2026-10-25T10:00:00Z",
    respected: { n: 6, wins: 3, losses: 2, win_rate: 0.6 },
    overruled_with_caveat: { n: 4, wins: 2, losses: 1, win_rate: 2 / 3 },
    overruled: { n: 3, wins: 0, losses: 0, win_rate: null },
    no_macro_data_excluded: 2,
    unruled: 1,
    source_counts: { replayed: 9, forecaster: 3 },
    ...overrides,
  };
}

export function makePoint(at: string, r: number, cumulative: number): EquityPoint {
  return { signal_id: `sig-${at}`, at, r, cumulative_r: cumulative, source: "replayed" };
}

export function makeEquity(overrides: Partial<EquityCurveResponse> = {}): EquityCurveResponse {
  return {
    since: "2026-07-27T00:00:00Z",
    until: "2026-10-25T10:00:00Z",
    published: [
      makePoint("2026-10-01T08:03:00Z", 3, 3),
      makePoint("2026-10-05T13:03:00Z", -1, 2),
      makePoint("2026-10-09T08:03:00Z", 3, 5),
    ],
    vetoed: [makePoint("2026-10-02T08:03:00Z", 3, 3), makePoint("2026-10-06T08:03:00Z", -1, 2)],
    gate_rejected: [makePoint("2026-10-03T08:03:00Z", -1, -1)],
    source_counts: { replayed: 6, forecaster: 0 },
    ...overrides,
  };
}

export function makeCriticReports(overrides: Partial<CriticReportsPage> = {}): CriticReportsPage {
  return {
    items: [
      {
        id: "99999999-9999-4999-8999-999999999999",
        period_start: "2026-10-01T00:00:00Z",
        period_end: "2026-10-15T00:00:00Z",
        created_at: "2026-10-16T22:30:00Z",
        pr_url: "https://github.com/pramodkavi/Trading-Multi-Agent/pull/30",
        summary: "Most gate rejections were RULE_2_MIN_RR; the Judge vetoed 9 of 12.",
      },
    ],
    ...overrides,
  };
}

export function makeReviews(overrides: Partial<ReviewsPage> = {}): ReviewsPage {
  return {
    items: [
      {
        signal_id: SIGNAL_ID,
        decision_kind: "VETOED",
        failure_category: "over_cautious_veto",
        lesson: "The veto ignored a clean OB retest.",
        judge_correct: false,
        gate_correct: true,
        skeptic_vindicated: null,
        confidence: "high",
        error: null,
        created_at: "2026-10-16T22:31:00Z",
      },
    ],
    next_cursor: null,
    ...overrides,
  };
}
