import { NavLink } from "react-router-dom";

const NAV_ITEMS = [
  { to: "/", label: "Overview" },
  { to: "/signals", label: "Signals" },
  { to: "/funnel", label: "Funnel" },
  { to: "/analytics", label: "Analytics" },
  { to: "/markets", label: "Markets" },
  { to: "/health", label: "Health" },
] as const;

const linkBase =
  "rounded-md px-3 py-2 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 dark:focus-visible:outline-sky-400";
const linkIdle =
  "text-slate-700 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-white";
const linkActive = "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900";

/** Main navigation; `vertical` is the < md (768 px) menu variant. */
export function Nav({ vertical = false, onNavigate }: { vertical?: boolean; onNavigate?: () => void }) {
  return (
    <nav aria-label={vertical ? "Menu" : "Main"}>
      <ul className={vertical ? "flex flex-col gap-1" : "flex items-center gap-1"}>
        {NAV_ITEMS.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              end={item.to === "/"}
              onClick={onNavigate}
              className={({ isActive }) =>
                `${linkBase} ${vertical ? "block" : ""} ${isActive ? linkActive : linkIdle}`
              }
            >
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
