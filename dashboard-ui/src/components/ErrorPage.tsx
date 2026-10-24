import type { ReactNode } from "react";

/** Full-page message used before the shell exists (config / sign-in failures). */
export function ErrorPage({
  title,
  message,
  children,
}: {
  title: string;
  message: string;
  children?: ReactNode;
}) {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <section
        role="alert"
        className="w-full max-w-lg rounded-lg border border-red-300 bg-white p-6 shadow-sm dark:border-red-800 dark:bg-slate-900"
      >
        <h1 className="text-lg font-semibold text-red-800 dark:text-red-300">{title}</h1>
        <p className="mt-2 break-words text-sm text-slate-800 dark:text-slate-200">{message}</p>
        {children ? <div className="mt-4">{children}</div> : null}
      </section>
    </main>
  );
}

/** Full-page neutral status (loading / redirecting). */
export function FullPageMessage({ message, children }: { message: string; children?: ReactNode }) {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div role="status" aria-live="polite" className="text-center text-slate-700 dark:text-slate-300">
        <p>{message}</p>
        {children ? <div className="mt-4">{children}</div> : null}
      </div>
    </main>
  );
}

export const buttonClass =
  "inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-900 hover:bg-slate-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700 dark:focus-visible:outline-sky-400";
