import type { RiskBudget } from "../../api/types";
import { formatCountdown, formatUtcTime } from "../../lib/format";
import { useNow } from "../../lib/useNow";
import { Badge } from "../Badge";
import { cardClass } from "../styles";

function NextScan({ at }: { at: string }) {
  const now = useNow(1_000);
  const seconds = (new Date(at).getTime() - now) / 1_000;
  return <span>{`Next scan in ${formatCountdown(seconds)}`}</span>;
}

/** The SPEC §1.6 stateful caps as the gate will see them at the next scan. */
export function RiskBudgetPanel({ risk }: { risk: RiskBudget }) {
  return (
    <div className={`${cardClass} space-y-1.5 text-sm text-slate-900 dark:text-slate-100`}>
      <p>{`Open setups ${risk.open_setups} / ${risk.open_cap}`}</p>
      <p>{`Published 24 h ${risk.published_24h} / ${risk.daily_cap}`}</p>
      <p className="flex flex-wrap items-center gap-2">
        <span>{`Loss streak ${risk.loss_streak}`}</span>
        {risk.pause_active ? (
          <Badge tone="bad">
            {risk.pause_until ? `Paused until ${formatUtcTime(risk.pause_until)} UTC` : "Paused"}
          </Badge>
        ) : null}
      </p>
      <p className="flex flex-wrap items-center gap-2">
        <span>{`Session ${risk.session}`}</span>
        {risk.session_blocked ? <Badge tone="warn">no new signals</Badge> : null}
      </p>
      <p>
        <NextScan at={risk.next_scan_at} />
      </p>
    </div>
  );
}
