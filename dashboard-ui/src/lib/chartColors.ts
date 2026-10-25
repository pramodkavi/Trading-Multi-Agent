import { OTHER_RULES } from "./funnel";

/** Categorical slots 1-5 in fixed order; "Other" is the muted neutral, never a generated hue. */
export function ruleColor(rule: string, index: number): string {
  return rule === OTHER_RULES ? "var(--viz-other)" : `var(--viz-series-${Math.min(index, 4) + 1})`;
}
