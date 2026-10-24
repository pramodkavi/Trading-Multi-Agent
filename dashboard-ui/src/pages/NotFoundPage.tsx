import { Link } from "react-router-dom";

export default function NotFoundPage() {
  return (
    <section>
      <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-100">Page not found</h1>
      <p className="mt-2 text-slate-700 dark:text-slate-300">There is no dashboard page at this address.</p>
      <p className="mt-4">
        <Link
          to="/"
          className="font-medium text-sky-800 underline underline-offset-2 hover:text-sky-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 dark:text-sky-300 dark:hover:text-sky-200 dark:focus-visible:outline-sky-400"
        >
          Back to overview
        </Link>
      </p>
    </section>
  );
}
