import type { DecisionKind, WindowSummary } from "../../api/types";
import { formatUtcTime, truncate } from "../../lib/format";
import { windowState, type Tone, type WindowState } from "../../lib/problems";
import { Badge } from "../Badge";
import { cardClass } from "../styles";

const STATE_TONE: Record<WindowState, Tone> = {
  ok: "ok",
  failed: "bad",
  running: "warn",
  pending: "neutral",
  missed: "warn",
};

const STATE_LABEL: Record<WindowState, string> = {
  ok: "ran ok",
  failed: "failed",
  running: "running",
  pending: "not run yet",
  missed: "missed",
};

const KIND_TONE: Record<DecisionKind, Tone> = {
  PUBLISHED: "ok",
  VETOED: "warn",
  GATE_REJECTED: "neutral",
  NO_SETUP: "neutral",
};

const ERROR_MAX = 140;

function WindowCard({ window: w, now }: { window: WindowSummary; now: Date }) {
  const state = windowState(w, now);
  const error = w.scan?.error ?? null;
  return (
    <li data-testid="window-card" className={`${cardClass} min-w-0`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-semibold text-slate-900 dark:text-slate-100">
          {w.label} <span className="font-normal text-slate-700 dark:text-slate-300">{formatUtcTime(w.fire_time)} UTC</span>
        </p>
        <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>
      </div>
      {error ? (
        <p
          data-testid="window-error"
          title={error}
          className="mt-2 break-words font-mono text-xs text-red-800 dark:text-red-200"
        >
          {truncate(error, ERROR_MAX)}
        </p>
      ) : null}
      {w.symbols.length > 0 ? (
        <ul className="mt-2 flex flex-wrap gap-1.5">
          {w.symbols.map((chip) => (
            <li key={`${chip.symbol}-${chip.signal_id}`}>
              <Badge tone={KIND_TONE[chip.kind]}>
                {chip.violated_rule
                  ? `${chip.symbol} ${chip.kind} · ${chip.violated_rule}`
                  : `${chip.symbol} ${chip.kind}`}
              </Badge>
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

/** Today's scheduled windows with per-symbol decision chips. */
export function WindowsToday({ windows, now }: { windows: WindowSummary[]; now: Date }) {
  return (
    <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {windows.map((w) => (
        <WindowCard key={w.label} window={w} now={now} />
      ))}
    </ul>
  );
}
