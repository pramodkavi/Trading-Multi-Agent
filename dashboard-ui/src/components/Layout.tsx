import { useIsFetching, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useAuth } from "react-oidc-context";
import { Outlet } from "react-router-dom";
import { dbProbeKey, healthKey, useDbProbe, useHealth } from "../api/queries";
import { buildSignoutArgs } from "../auth/oidcSettings";
import type { DashboardConfig } from "../config";
import { formatAgo } from "../lib/apiStatus";
import { buttonClass } from "./ErrorPage";
import { Nav } from "./Nav";
import { StatusChip } from "./StatusChip";

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

function MenuIcon({ open }: { open: boolean }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      {open ? <path d="M6 6l12 12M18 6L6 18" /> : <path d="M4 7h16M4 12h16M4 17h16" />}
    </svg>
  );
}

function RefreshIcon({ spinning }: { spinning: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className={`h-4 w-4 ${spinning ? "motion-safe:animate-spin" : ""}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" />
    </svg>
  );
}

export function Layout({ config }: { config: DashboardConfig }) {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const health = useHealth();
  const db = useDbProbe();
  const fetching = useIsFetching() > 0;
  const now = useNow(5_000);
  const [menuOpen, setMenuOpen] = useState(false);

  const email = auth.user?.profile.email ?? auth.user?.profile.sub ?? "";
  const updatedAt = Math.max(health.dataUpdatedAt, db.dataUpdatedAt);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: healthKey });
    void queryClient.invalidateQueries({ queryKey: dbProbeKey });
  };

  const signOut = () => {
    queryClient.clear();
    // signoutRedirect removes the stored user (memory + localStorage stub) before
    // navigating to Cognito's /logout; removeUser is the fallback if it fails.
    auth.signoutRedirect(buildSignoutArgs(config)).catch(() => {
      void auth.removeUser();
    });
  };

  return (
    <div className="min-h-screen">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-10 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:text-slate-900"
      >
        Skip to content
      </a>
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="rounded-md p-2 text-slate-800 hover:bg-slate-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-600 md:hidden dark:text-slate-200 dark:hover:bg-slate-800 dark:focus-visible:outline-sky-400"
              aria-expanded={menuOpen}
              aria-controls="mobile-menu"
              onClick={() => setMenuOpen((open) => !open)}
            >
              <MenuIcon open={menuOpen} />
              <span className="sr-only">{menuOpen ? "Close menu" : "Open menu"}</span>
            </button>
            <span className="text-base font-semibold text-slate-900 dark:text-white">Crypto Signals</span>
          </div>
          <div className="hidden md:block">
            <Nav />
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2 text-sm">
            <StatusChip health={health} db={db} />
            <span className="text-xs text-slate-700 dark:text-slate-300">{formatAgo(updatedAt, now)}</span>
            <button type="button" className={buttonClass} onClick={refresh} disabled={fetching}>
              <RefreshIcon spinning={fetching} />
              <span>Refresh</span>
            </button>
            {email ? (
              <span className="hidden max-w-[16rem] truncate text-slate-700 sm:inline dark:text-slate-300" title={email}>
                {email}
              </span>
            ) : null}
            <button type="button" className={buttonClass} onClick={signOut}>
              Sign out
            </button>
          </div>
        </div>
        {menuOpen ? (
          <div className="border-t border-slate-200 px-4 py-2 md:hidden dark:border-slate-800">
            <Nav vertical onNavigate={() => setMenuOpen(false)} />
          </div>
        ) : null}
      </header>
      <main id="main" className="mx-auto max-w-7xl px-4 py-6">
        <Outlet />
      </main>
      <footer className="mx-auto max-w-7xl px-4 pb-6 text-xs text-slate-700 dark:text-slate-300">
        App {config.version.slice(0, 7)} · signal-only, read-only
      </footer>
    </div>
  );
}
