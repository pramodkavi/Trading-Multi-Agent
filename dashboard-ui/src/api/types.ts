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

// -- Journal (P3), only the slice the Overview lists ----------------------------

export interface SignalRowOut {
  id: string;
  scan_id: string;
  symbol: string;
  strategy: string;
  direction: SignalDirection | null;
  status: string;
  created_at: string;
  decision_kind: DecisionKind;
  judge_ruling: string | null;
  session: string | null;
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
