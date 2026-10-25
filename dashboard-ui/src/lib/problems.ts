import type { OverviewResponse, WindowOutcome, WindowSummary } from "../api/types";

export type Tone = "ok" | "bad" | "warn" | "neutral";

export interface Problem {
  id: "credits" | "scans_failed" | "alarm" | "forecaster" | "alarms_unavailable";
  tone: "bad" | "warn";
  title: string;
  detail: string;
}

/** A window with no scan is "missed" once this long has passed since its fire time. */
export const MISSED_GRACE_MS = 15 * 60_000;

/** True when a stored scan error is Anthropic's "credit balance is too low" rejection. */
export function isCreditError(text: string | null | undefined): boolean {
  return typeof text === "string" && /credit balance/i.test(text);
}

function failedOutcomes(o: OverviewResponse): WindowOutcome[] {
  const fromWindows = o.windows_today
    .map((w) => w.scan)
    .filter((s): s is WindowOutcome => s !== null && s.status === "FAILED");
  // Overnight `windows_today` is empty of scans; the strip's last scan (yesterday's wrap) still counts.
  const last = o.health.last_scan;
  if (fromWindows.length === 0 && last?.status === "FAILED") return [last];
  return fromWindows;
}

const plural = (n: number, one: string, many: string) => (n === 1 ? one : many);

/** Everything wrong right now, most severe first; `[]` when healthy. Drives the red banner. */
export function collectProblems(o: OverviewResponse): Problem[] {
  const problems: Problem[] = [];

  const failed = failedOutcomes(o);
  const creditHits = failed.filter((s) => isCreditError(s.error));
  if (creditHits.length > 0) {
    problems.push({
      id: "credits",
      tone: "bad",
      title: "Anthropic credit balance too low",
      detail: `Scans are failing at the LLM call; top up Anthropic credits. ${creditHits.length} ${plural(
        creditHits.length,
        "window",
        "windows",
      )} affected.`,
    });
  }
  const otherFailures = failed.length - creditHits.length;
  if (otherFailures > 0) {
    problems.push({
      id: "scans_failed",
      tone: "bad",
      title: `${otherFailures} scan ${plural(otherFailures, "window", "windows")} failed today`,
      detail: "See the scan history on the Health page for the error.",
    });
  }

  const alarming = (o.health.alarms ?? []).filter((a) => a.state === "ALARM");
  if (alarming.length > 0) {
    problems.push({
      id: "alarm",
      tone: "bad",
      title: `${alarming.length} CloudWatch ${plural(alarming.length, "alarm", "alarms")} in ALARM`,
      detail: alarming.map((a) => a.name).join(", "),
    });
  }

  const f = o.health.forecaster;
  if (f.stale > 0 || f.errors_last_window > 0) {
    problems.push({
      id: "forecaster",
      tone: "warn",
      title: "Forecaster needs attention",
      detail: `${f.stale} stale open ${plural(f.stale, "setup", "setups")}, ${f.errors_last_window} ${plural(
        f.errors_last_window,
        "error",
        "errors",
      )} in the last window.`,
    });
  }

  if (o.health.alarms === null) {
    problems.push({
      id: "alarms_unavailable",
      tone: "warn",
      title: "Alarm status unavailable",
      detail: o.health.alarms_note ?? "cloudwatch unavailable",
    });
  }

  return problems;
}

export type WindowState = "ok" | "failed" | "running" | "pending" | "missed";

/** Where a scheduled window stands: never green/red until it actually ran. */
export function windowState(w: WindowSummary, now: Date): WindowState {
  if (w.scan === null) {
    return now.getTime() >= new Date(w.fire_time).getTime() + MISSED_GRACE_MS ? "missed" : "pending";
  }
  if (w.scan.status === "FAILED") return "failed";
  if (w.scan.status === "RUNNING") return "running";
  return "ok";
}
