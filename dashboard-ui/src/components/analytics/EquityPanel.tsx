import { useState } from "react";
import { useEquityCurve } from "../../api/queries";
import { equityLines } from "../../lib/analytics";
import type { PeriodDays } from "../../lib/period";
import { EquityChart } from "../charts/EquityChart";
import { QueryState } from "../health/QueryState";
import { mutedClass } from "../styles";

const labelClass = "inline-flex items-center gap-2 text-sm text-slate-800 dark:text-slate-200";

/** The hypothetical R-multiple curve; the counterfactual curves are opt-in and dashed. */
export function EquityPanel({ days }: { days: PeriodDays }) {
  const query = useEquityCurve(days);
  const [showVetoed, setShowVetoed] = useState(false);
  const [showGate, setShowGate] = useState(false);
  return (
    <QueryState query={query} label="equity curve">
      {(data) => (
        <div className="grid min-w-0 gap-3">
          <p className={mutedClass}>
            Hypothetical: 1 R risk per published signal. Counterfactual curves replay decisions that were never taken.
          </p>
          <div className="flex flex-wrap gap-x-6 gap-y-2">
            <label className={labelClass}>
              <input type="checkbox" checked={showVetoed} onChange={(e) => setShowVetoed(e.currentTarget.checked)} />
              Show vetoed (counterfactual)
            </label>
            <label className={labelClass}>
              <input type="checkbox" checked={showGate} onChange={(e) => setShowGate(e.currentTarget.checked)} />
              Show gate-rejected (counterfactual)
            </label>
          </div>
          <EquityChart
            lines={equityLines(data)}
            visible={{ published: true, vetoed: showVetoed, gate_rejected: showGate }}
          />
        </div>
      )}
    </QueryState>
  );
}
