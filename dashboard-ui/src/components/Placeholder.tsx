export function Placeholder({ title, step, summary }: { title: string; step: string; summary: string }) {
  return (
    <section>
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">{title}</h1>
      <div className="mt-4 rounded-lg border border-dashed border-slate-300 p-6 dark:border-slate-600">
        <p className="font-medium text-slate-800 dark:text-slate-200">Coming in Step {step}</p>
        <p className="mt-1 text-sm text-slate-700 dark:text-slate-300">{summary}</p>
      </div>
    </section>
  );
}
