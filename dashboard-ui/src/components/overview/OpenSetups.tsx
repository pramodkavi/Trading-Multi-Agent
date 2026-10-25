import type { SetupCardOut } from "../../api/types";
import { formatAge, formatR } from "../../lib/format";
import type { Tone } from "../../lib/problems";
import { Badge } from "../Badge";
import { cardClass, mutedClass } from "../styles";

const VERDICT_TONE: Record<string, Tone> = { STILL_VALID: "ok", AT_RISK: "warn", INVALIDATED: "bad" };

function rClass(r: number | null): string {
  if (r === null) return "";
  if (r > 0) return "text-emerald-700 dark:text-emerald-300";
  if (r < 0) return "text-red-700 dark:text-red-300";
  return "";
}

function SetupCard({ setup }: { setup: SetupCardOut }) {
  const verdict = setup.latest_status;
  return (
    <li className={cardClass}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-semibold text-slate-900 dark:text-slate-100">
          <span>{setup.symbol}</span> <span className="font-normal">{setup.direction}</span>
        </p>
        {setup.unrealised_r !== null ? (
          <span className={`font-mono text-sm font-semibold ${rClass(setup.unrealised_r)}`}>
            {formatR(setup.unrealised_r)}
          </span>
        ) : (
          <span className={mutedClass}>{setup.unrealised_r_note ?? "—"}</span>
        )}
      </div>
      <p className="mt-1 break-words font-mono text-xs text-slate-800 dark:text-slate-200">
        {`entry ${setup.entry_price} · SL ${setup.stop_loss} · TP1 ${setup.take_profit_1}`}
        {setup.take_profit_2 !== null ? ` · TP2 ${setup.take_profit_2}` : ""}
      </p>
      <p className="mt-2 flex flex-wrap items-center gap-2">
        <Badge tone={verdict ? (VERDICT_TONE[verdict] ?? "neutral") : "neutral"}>
          {verdict ?? "not evaluated"}
        </Badge>
        <span className={mutedClass}>{formatAge(setup.evaluation_age_seconds)}</span>
      </p>
    </li>
  );
}

/** One card per OPEN setup, with live R and the Forecaster's latest verdict. */
export function OpenSetups({ setups }: { setups: SetupCardOut[] }) {
  if (setups.length === 0) return <p className={mutedClass}>No open setups.</p>;
  return (
    <ul className="grid gap-3 sm:grid-cols-2">
      {setups.map((s) => (
        <SetupCard key={s.setup_id} setup={s} />
      ))}
    </ul>
  );
}
