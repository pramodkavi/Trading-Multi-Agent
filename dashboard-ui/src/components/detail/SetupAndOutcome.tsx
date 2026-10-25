import { useState } from "react";
import type { LifecycleResponse } from "../../api/types";
import { formatAge, formatR, formatUtcDateTime } from "../../lib/format";
import { buttonClass } from "../ErrorPage";
import { cardClass, mutedClass } from "../styles";
import { JsonPanel } from "./JsonPanel";

const num = (v: number | null) => (v === null ? "—" : String(v));

/** The setup the Forecaster follows, the replay numbers, and the stored signal JSON. */
export function SetupAndOutcome({ lifecycle }: { lifecycle: LifecycleResponse }) {
  const { setup, resolution, signal } = lifecycle;
  const [showJson, setShowJson] = useState(false);
  return (
    <div className="grid min-w-0 gap-3 md:grid-cols-2">
      {setup ? (
        <div className={`${cardClass} text-sm text-slate-900 dark:text-slate-100`}>
          <p className="font-semibold">Tracked setup</p>
          <p className="mt-1 break-words font-mono text-xs">
            {`entry ${setup.entry_price} · SL ${setup.stop_loss} · TP1 ${setup.take_profit_1}`}
            {setup.take_profit_2 !== null ? ` · TP2 ${setup.take_profit_2}` : ""}
          </p>
          <p className={`${mutedClass} mt-1`}>
            {`${setup.latest_status ?? "not evaluated"} · evaluated ${formatAge(setup.evaluation_age_seconds)}`}
          </p>
        </div>
      ) : null}
      {resolution ? (
        <div className={`${cardClass} text-sm text-slate-900 dark:text-slate-100`}>
          <p className="font-semibold">Replay result</p>
          <p className="mt-1">{`${resolution.outcome} · ${formatR(resolution.realized_r)}`}</p>
          <p className={`${mutedClass} mt-1`}>
            {`max adverse ${num(resolution.max_adverse_excursion_r)} R · max favourable ${num(
              resolution.max_favorable_excursion_r,
            )} R · ${resolution.candles_used} candles · resolved ${formatUtcDateTime(resolution.resolved_at)}`}
          </p>
        </div>
      ) : null}
      <div className="md:col-span-2">
        <button type="button" className={buttonClass} aria-expanded={showJson} onClick={() => setShowJson((s) => !s)}>
          Signal JSON
        </button>
        {showJson ? (
          <JsonPanel
            value={{ payload: signal.payload, features: signal.features, outcome_metadata: signal.outcome_metadata }}
          />
        ) : null}
      </div>
    </div>
  );
}
