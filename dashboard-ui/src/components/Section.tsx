import type { ReactNode } from "react";

/** A titled page section; the heading gives screen readers a landmark to jump to. */
export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="min-w-0">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-700 dark:text-slate-300">
        {title}
      </h2>
      {children}
    </section>
  );
}
