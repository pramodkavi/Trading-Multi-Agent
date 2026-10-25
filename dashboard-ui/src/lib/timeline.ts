import type { LifecycleResponse } from "../api/types";
import { formatR } from "./format";
import type { Tone } from "./problems";

export type TimelineKind =
  | "analyzer"
  | "gate"
  | "historian"
  | "skeptic"
  | "judge"
  | "forecast"
  | "outcome"
  | "review";

export interface TimelineStep {
  id: string;
  kind: TimelineKind;
  title: string;
  tone: Tone;
  at: string | null;
  summary: string;
  /** Set for steps backed by an `agent_runs` row: the full output is fetched by this id. */
  runId: string | null;
  details: Record<string, unknown> | null;
}

const OUTCOME_TONE: Record<string, Tone> = { WIN: "ok", LOSS: "bad" };

function text(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function percent(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "n/a";
}

/** The decision's story in order; only the steps whose data exists are emitted. */
export function buildTimeline(l: LifecycleResponse): TimelineStep[] {
  const out: TimelineStep[] = [];
  const byRole = (role: string) => l.steps.find((s) => s.role === role);

  const analyzer = byRole("analyzer");
  if (analyzer) {
    const s = analyzer.summary;
    const proposal = "direction" in s && s.direction !== null && s.direction !== undefined;
    out.push({
      id: `analyzer-${analyzer.run_id}`,
      kind: "analyzer",
      title: proposal ? "Analyzer proposal" : "Analyzer skip",
      tone: "neutral",
      at: analyzer.created_at,
      summary: proposal
        ? `${text(s.direction)} · entry ${text(s.entry_price)} · SL ${text(s.stop_loss)} · TP1 ${text(
            s.take_profit_1,
          )} · R:R ${text(s.risk_reward_ratio)}`
        : `${text(s.reason)}${s.details ? ` — ${String(s.details)}` : ""}`,
      runId: analyzer.run_id,
      details: s,
    });
  }

  const sig = l.signal;
  if (sig.violated_rule || sig.decision_kind === "GATE_REJECTED") {
    out.push({
      id: "gate",
      kind: "gate",
      title: "Risk gate rejected",
      tone: "bad",
      at: null,
      summary: [sig.violated_rule, sig.skip_details].filter(Boolean).join(": ") || "Rejected by a hard rule",
      runId: null,
      details: null,
    });
  }

  const historian = byRole("historian");
  if (historian) {
    const s = historian.summary;
    out.push({
      id: `historian-${historian.run_id}`,
      kind: "historian",
      title: "Historian",
      tone: "neutral",
      at: historian.created_at,
      summary: `${text(s.sample_size, "0")} similar setups · win rate ${percent(s.win_rate)} (${text(
        s.wins,
        "0",
      )} W / ${text(s.losses, "0")} L)`,
      runId: historian.run_id,
      details: s,
    });
  }

  const skeptic = byRole("skeptic");
  if (skeptic) {
    const s = skeptic.summary;
    const noMacro = s.no_macro_data === true;
    const severity = typeof s.severity === "string" ? s.severity.toUpperCase() : "";
    let tone: Tone = "neutral";
    if (noMacro) tone = "warn";
    else if (s.recommends_against === true) tone = "bad";
    else if (severity === "HIGH" || severity === "MEDIUM") tone = "warn";
    out.push({
      id: `skeptic-${skeptic.run_id}`,
      kind: "skeptic",
      title: "Skeptic",
      tone,
      at: skeptic.created_at,
      summary: noMacro ? "Macro data unavailable" : `${text(s.severity)} — ${text(s.headline)}`,
      runId: skeptic.run_id,
      details: s,
    });
  }

  const judge = byRole("judge");
  if (judge) {
    const s = judge.summary;
    out.push({
      id: `judge-${judge.run_id}`,
      kind: "judge",
      title: "Judge",
      tone: s.ruling === "PUBLISH" ? "ok" : "warn",
      at: judge.created_at,
      summary: `${text(s.ruling)} (confidence ${text(s.confidence)})${s.caveat ? ` — ${String(s.caveat)}` : ""}`,
      runId: judge.run_id,
      details: s,
    });
  }

  for (const e of l.evaluations) {
    let tone: Tone = "ok";
    if (e.status === "ERROR") tone = "bad";
    else if (e.status === "AT_RISK") tone = "warn";
    else if (e.status === "INVALIDATED") tone = e.outcome ? (OUTCOME_TONE[e.outcome] ?? "neutral") : "neutral";
    out.push({
      id: `forecast-${e.id}`,
      kind: "forecast",
      title: `Forecaster: ${e.status}`,
      tone,
      at: e.evaluated_at,
      summary: e.status === "ERROR" ? text(e.error) : text(e.reasoning),
      runId: null,
      details: { ...e },
    });
  }

  if (l.resolution) {
    out.push({
      id: "outcome",
      kind: "outcome",
      title: "Outcome (replay)",
      tone: OUTCOME_TONE[l.resolution.outcome] ?? "neutral",
      at: l.resolution.resolved_at,
      summary: `${l.resolution.outcome} · ${formatR(l.resolution.realized_r)}`,
      runId: null,
      details: { ...l.resolution },
    });
  } else if (sig.outcome) {
    out.push({
      id: "outcome",
      kind: "outcome",
      title: "Outcome",
      tone: OUTCOME_TONE[sig.outcome] ?? "neutral",
      at: null,
      summary: sig.outcome,
      runId: null,
      details: sig.outcome_metadata,
    });
  }

  if (l.review) {
    const failed = l.review.error !== null;
    out.push({
      id: "review",
      kind: "review",
      title: "Critic review",
      tone: failed ? "warn" : "neutral",
      at: l.review.created_at,
      summary: failed
        ? `Review failed: ${text(l.review.error)}`
        : text(l.review.review.lesson, "Reviewed"),
      runId: null,
      details: l.review.review,
    });
  }

  return out;
}
