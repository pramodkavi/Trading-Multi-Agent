import { PERIODS, type PeriodDays } from "../../lib/period";
import { SegmentedControl } from "./SegmentedControl";

const OPTIONS = PERIODS.map((d) => ({ value: String(d), label: `${d} days` }));

/** Last 7 / 30 / 90 days, as a labelled radio group. */
export function PeriodSelect({ value, onChange }: { value: PeriodDays; onChange: (d: PeriodDays) => void }) {
  return (
    <SegmentedControl
      label="Period"
      options={OPTIONS}
      value={String(value)}
      onChange={(v) => onChange(Number(v) as PeriodDays)}
    />
  );
}
