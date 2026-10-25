import { Link } from "react-router-dom";
import type { Problem } from "../lib/problems";

const STYLE = {
  bad: "border-red-600/50 bg-red-50 text-red-900 dark:border-red-400/50 dark:bg-red-950 dark:text-red-100",
  warn: "border-amber-600/50 bg-amber-50 text-amber-950 dark:border-amber-400/50 dark:bg-amber-950 dark:text-amber-100",
} as const;

/** The prominent failure banner. Red if any problem is `bad`, else amber; nothing when healthy. */
export function AlertBanner({ problems }: { problems: Problem[] }) {
  if (problems.length === 0) return null;
  const tone = problems.some((p) => p.tone === "bad") ? "bad" : "warn";
  return (
    <section role="alert" data-tone={tone} className={`rounded-lg border-2 p-4 ${STYLE[tone]}`}>
      <ul className="space-y-2">
        {problems.map((p) => (
          <li key={p.id}>
            <p className="font-semibold">{p.title}</p>
            <p className="break-words text-sm">{p.detail}</p>
          </li>
        ))}
      </ul>
      <Link to="/health" className="mt-3 inline-block text-sm font-medium underline">
        View system health
      </Link>
    </section>
  );
}
