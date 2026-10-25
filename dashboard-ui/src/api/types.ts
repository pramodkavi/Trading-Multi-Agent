/**
 * TypeScript mirrors of the dashboard API response models the Overview and
 * Health pages read. Names and fields match `src/dashboard/api/schemas.py` and
 * `cloudwatch.py`; `datetime` / `date` / `UUID` are ISO strings on the wire.
 */

export type ScanStatus = "RUNNING" | "SUCCESS" | "FAILED";
export type DecisionKind = "PUBLISHED" | "VETOED" | "GATE_REJECTED" | "NO_SETUP";
export type SignalDirection = "LONG" | "SHORT";

export interface AlarmState {
  name: string;
  state: "OK" | "ALARM" | "INSUFFICIENT_DATA";
  updated_at: string;
  reason: string | null;
}

// -- Overview (P1) -------------------------------------------------------------

export interface WindowOutcome {
  label: string;
  started_at: string;
  status: ScanStatus;
  symbols: string[];
  error: string | null;
}

export interface ForecasterCoverage {
  open_setups: number;
  stale: number;
  evaluations_last_window: number;
  errors_last_window: number;
}

export interface HealthStrip {
  last_scan: WindowOutcome | null;
  forecaster: ForecasterCoverage;
  alarms: AlarmState[] | null;
  alarms_note: string | null;
  db: "ok";
}

export interface RiskBudget {
  open_setups: number;
  open_cap: number;
  published_24h: number;
  daily_cap: number;
  loss_streak: number;
  pause_active: boolean;
  pause_until: string | null;
  session: string;
  session_blocked: boolean;
  next_scan_at: string;
  seconds_to_next_scan: number;
}

export interface SetupCardOut {
  setup_id: string;
  signal_id: string;
  symbol: string;
  direction: SignalDirection;
  entry_price: number;
  stop_loss: number;
  take_profit_1: number;
  take_profit_2: number | null;
  opened_at: string;
  age_seconds: number;
  last_evaluated_at: string | null;
  evaluation_age_seconds: number | null;
  latest_status: string | null;
  latest_reasoning: string | null;
  unrealised_r: number | null;
  unrealised_r_note: string | null;
}

export interface SymbolChip {
  symbol: string;
  kind: DecisionKind;
  violated_rule: string | null;
  signal_id: string;
}

export interface WindowSummary {
  label: string;
  fire_time: string;
  scan: WindowOutcome | null;
  symbols: SymbolChip[];
}

export interface OverviewResponse {
  generated_at: string;
  health: HealthStrip;
  risk: RiskBudget;
  open_setups: SetupCardOut[];
  windows_today: WindowSummary[];
}

// -- Journal (P3) and signal lifecycle (P2) -------------------------------------

export type SignalOutcome = "WIN" | "LOSS" | "BREAKEVEN" | "INVALIDATED" | "EXPIRED";

export interface SignalRowOut {
  id: string;
  scan_id: string;
  symbol: string;
  strategy: string;
  direction: SignalDirection | null;
  status: string;
  created_at: string;
  tags: string[];
  outcome: SignalOutcome | null;
  entry_price: number | null;
  stop_loss: number | null;
  take_profit_1: number | null;
  take_profit_2: number | null;
  risk_reward_ratio: number | null;
  skip_reason: string | null;
  skip_details: string | null;
  decision_kind: DecisionKind;
  judge_ruling: string | null;
  session: string | null;
  has_setup: boolean;
  violated_rule: string | null;
  resolution_outcome: string | null;
  realized_r: number | null;
}

export interface SignalFilters {
  since?: string;
  until?: string;
  symbol?: string;
  kind?: DecisionKind;
  outcome?: SignalOutcome;
  tag?: string;
  session?: string;
  violated_rule?: string;
}

export interface SignalDetailOut extends SignalRowOut {
  payload: Record<string, unknown>;
  features: Record<string, unknown>;
  outcome_metadata: Record<string, unknown> | null;
}

export interface LifecycleStep {
  role: string;
  run_id: string;
  created_at: string;
  latency_ms: number;
  cost_usd: number | null;
  summary: Record<string, unknown>;
}

export interface EvaluationOut {
  id: string;
  setup_id: string;
  signal_id: string;
  evaluated_at: string;
  status: "STILL_VALID" | "AT_RISK" | "INVALIDATED" | "ERROR";
  outcome: SignalOutcome | null;
  reasoning: string | null;
  error: string | null;
  latency_ms: number;
  token_usage: Record<string, unknown>;
  cost_usd: number | null;
}

export interface ResolutionOut {
  signal_id: string;
  decision_kind: DecisionKind;
  outcome: "WIN" | "LOSS" | "EXPIRED" | "UNRESOLVABLE";
  decided_at: string;
  entered_at: string | null;
  resolved_at: string;
  realized_r: number;
  max_adverse_excursion_r: number | null;
  max_favorable_excursion_r: number | null;
  tp2_reached: boolean;
  candles_used: number;
  engine_version: string;
}

export interface ReviewOut {
  signal_id: string;
  decision_kind: string;
  review: Record<string, unknown>;
  model: string;
  token_usage: Record<string, unknown>;
  cost_usd: number | null;
  error: string | null;
  attempts: number;
  created_at: string;
}

export interface AgentRunOut {
  id: string;
  scan_id: string;
  agent_role: string;
  strategy: string | null;
  input_hash: string;
  output: Record<string, unknown>;
  latency_ms: number;
  token_usage: Record<string, unknown>;
  cost_usd: number | null;
  created_at: string;
}

export interface LifecycleResponse {
  signal: SignalDetailOut;
  steps: LifecycleStep[];
  evaluations: EvaluationOut[];
  resolution: ResolutionOut | null;
  review: ReviewOut | null;
  setup: SetupCardOut | null;
}
export interface SignalsPage {
  items: SignalRowOut[];
  next_cursor: string | null;
}

// -- System health (P7) --------------------------------------------------------

export interface ScanRowOut {
  id: string;
  window: string;
  started_at: string;
  completed_at: string | null;
  duration_s: number | null;
  status: ScanStatus;
  symbols: string[];
  error_message: string | null;
}

export interface ScansResponse {
  items: ScanRowOut[];
}

export interface UsageDayOut {
  date: string;
  role: string;
  runs: number;
  errors: number | null;
  cost_usd: number;
}

export interface CostDay {
  date: string;
  total_usd: number;
  by_role: Record<string, number>;
}

export interface AgentsResponse {
  since: string;
  until: string;
  usage: UsageDayOut[];
  cost_per_day: CostDay[];
}

export interface SweepRow {
  sweep_start: string;
  evaluations: number;
  errors: number;
}

export interface SweepsResponse {
  since: string;
  until: string;
  sweeps: SweepRow[];
}

export interface AlarmsResponse {
  alarms: AlarmState[] | null;
  note: string | null;
}

export interface MetricDay {
  date: string;
  invocations: number;
  errors: number;
  duration_p95_ms: number | null;
}

export interface MetricsResponse {
  function_name: string | null;
  days: MetricDay[] | null;
  note: string | null;
}

export interface Inferred<T> {
  value: T;
  inferred: true;
  source: string;
}

export interface ScheduleOut {
  windows: { label: string; time_utc: string }[];
  inferred: false;
  source: string;
}

export interface ConfigResponse {
  watchlist: Inferred<string[]>;
  model: Inferred<string | null>;
  macro_providers_configured: Inferred<boolean | null>;
  langfuse_configured: Inferred<boolean>;
  schedule: ScheduleOut;
}

// -- Decision funnel (P4) --------------------------------------------------------

export type FunnelGroupBy = "day" | "window" | "symbol";

export interface FunnelGroup {
  key: string;
  scans: number;
  symbols_scanned: number;
  proposals: number;
  gate_rejected: number;
  gate_passed: number;
  judge_publish: number;
  judge_caveat: number;
  judge_skip: number;
  judge_none: number;
  published: number;
  rejections_by_rule: Record<string, number>;
}

export interface RejectionCount {
  rule: string;
  count: number;
}

export interface FunnelResponse {
  since: string;
  until: string;
  group_by: FunnelGroupBy;
  groups: FunnelGroup[];
  totals: FunnelGroup;
  top_rejections_7d: RejectionCount[];
  top_rejections_30d: RejectionCount[];
}

// -- Performance analytics (P6) and the Critic -------------------------------------

export type WinRateBy = "tag" | "session" | "strategy" | "time";

export interface SourceCounts {
  replayed: number;
  forecaster: number;
}

export interface WinRateBucket {
  key: string;
  wins: number;
  losses: number;
  decisive: number;
  win_rate: number | null;
  n: number;
}

export interface WinRateResponse {
  since: string;
  until: string;
  by: WinRateBy;
  buckets: WinRateBucket[];
  source_counts: SourceCounts;
}

export interface SkepticBucket {
  n: number;
  wins: number;
  losses: number;
  win_rate: number | null;
}

export interface SkepticValueResponse {
  since: string;
  until: string;
  respected: SkepticBucket;
  overruled_with_caveat: SkepticBucket;
  overruled: SkepticBucket;
  no_macro_data_excluded: number;
  unruled: number;
  source_counts: SourceCounts;
}

export interface EquityPoint {
  signal_id: string;
  at: string;
  r: number;
  cumulative_r: number;
  source: "replayed" | "forecaster";
}

export interface EquityCurveResponse {
  since: string;
  until: string;
  published: EquityPoint[];
  vetoed: EquityPoint[];
  gate_rejected: EquityPoint[];
  source_counts: SourceCounts;
}

export interface CriticReportSummary {
  id: string;
  period_start: string;
  period_end: string;
  created_at: string;
  pr_url: string | null;
  summary: string | null;
}

export interface CriticReportsPage {
  items: CriticReportSummary[];
}

export interface CriticReportOut {
  id: string;
  period_start: string;
  period_end: string;
  report: Record<string, unknown>;
  markdown: string;
  pr_url: string | null;
  created_at: string;
}

export interface ReviewSummary {
  signal_id: string;
  decision_kind: string;
  failure_category: string | null;
  lesson: string | null;
  judge_correct: boolean | null;
  gate_correct: boolean | null;
  skeptic_vindicated: string | null;
  confidence: string | null;
  error: string | null;
  created_at: string;
}

export interface ReviewsPage {
  items: ReviewSummary[];
  next_cursor: string | null;
}
