import type { ReactNode } from "react";
import type { Tone } from "../lib/problems";

// Same palette as StatusChip: text ≥ 4.5:1 on its background in both themes.
const STYLE: Record<Tone, string> = {
  ok: "bg-emerald-50 text-emerald-800 ring-emerald-600/30 dark:bg-emerald-950 dark:text-emerald-200 dark:ring-emerald-400/30",
  bad: "bg-red-50 text-red-800 ring-red-600/30 dark:bg-red-950 dark:text-red-200 dark:ring-red-400/30",
  warn: "bg-amber-50 text-amber-900 ring-amber-600/30 dark:bg-amber-950 dark:text-amber-200 dark:ring-amber-400/30",
  neutral:
    "bg-slate-100 text-slate-800 ring-slate-500/30 dark:bg-slate-800 dark:text-slate-200 dark:ring-slate-400/30",
};

const DOT: Record<Tone, string> = {
  ok: "bg-emerald-600 dark:bg-emerald-400",
  bad: "bg-red-600 dark:bg-red-400",
  warn: "bg-amber-500 dark:bg-amber-400",
  neutral: "bg-slate-500 dark:bg-slate-400",
};

/** A pill with a coloured dot. The label carries the meaning; colour is never the only signal. */
export function Badge({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      data-tone={tone}
      title={title}
      className={`inline-flex max-w-full items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${STYLE[tone]}`}
    >
      <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${DOT[tone]}`} />
      <span className="min-w-0 truncate">{children}</span>
    </span>
  );
}
