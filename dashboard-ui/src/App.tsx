import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { UserManager, type User } from "oidc-client-ts";
import { useEffect, useState } from "react";
import { AuthProvider } from "react-oidc-context";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { configureApi } from "./api/client";
import { createQueryClient } from "./api/queries";
import { AuthGate } from "./auth/AuthGate";
import { SplitTokenStore } from "./auth/SplitTokenStore";
import { buildOidcSettings, safeReturnPath } from "./auth/oidcSettings";
import { ErrorPage, FullPageMessage } from "./components/ErrorPage";
import { Layout } from "./components/Layout";
import { ConfigError, loadConfig, type DashboardConfig } from "./config";
import AnalyticsPage from "./pages/AnalyticsPage";
import FunnelPage from "./pages/FunnelPage";
import HealthPage from "./pages/HealthPage";
import MarketsPage from "./pages/MarketsPage";
import NotFoundPage from "./pages/NotFoundPage";
import OverviewPage from "./pages/OverviewPage";
import SignalDetailPage from "./pages/SignalDetailPage";
import SignalsPage from "./pages/SignalsPage";

interface Runtime {
  config: DashboardConfig;
  userManager: UserManager;
  queryClient: QueryClient;
}

function createRuntime(config: DashboardConfig): Runtime {
  const userManager = new UserManager(buildOidcSettings(config, new SplitTokenStore()));
  configureApi({ apiBase: config.apiBase, auth: userManager });
  return { config, userManager, queryClient: createQueryClient() };
}

/** Strip `?code=&state=` after the Hosted UI callback and land on the saved path. */
function onSigninCallback(user: User | undefined): void {
  window.history.replaceState({}, document.title, safeReturnPath(user?.state));
}

export function App({ runtime }: { runtime: Runtime }) {
  const { config, userManager, queryClient } = runtime;
  return (
    <AuthProvider userManager={userManager} onSigninCallback={onSigninCallback}>
      <AuthGate>
        <QueryClientProvider client={queryClient}>
          {/* Mounted only after sign-in, so it reads the already-cleaned URL. */}
          <BrowserRouter>
            <Routes>
              <Route element={<Layout config={config} />}>
                <Route index element={<OverviewPage />} />
                <Route path="signals" element={<SignalsPage />} />
                <Route path="signals/:id" element={<SignalDetailPage />} />
                <Route path="funnel" element={<FunnelPage />} />
                <Route path="analytics" element={<AnalyticsPage />} />
                <Route path="markets" element={<MarketsPage />} />
                <Route path="health" element={<HealthPage />} />
                <Route path="*" element={<NotFoundPage />} />
              </Route>
              <Route path="callback" element={<Navigate to="/" replace />} />
            </Routes>
          </BrowserRouter>
        </QueryClientProvider>
      </AuthGate>
    </AuthProvider>
  );
}

type BootState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; runtime: Runtime };

/** Loads `/config.json`, then mounts the app — or a full-page config error. */
export function Boot({ load = loadConfig }: { load?: () => Promise<DashboardConfig> }) {
  const [state, setState] = useState<BootState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    load()
      .then((config) => {
        if (!cancelled) setState({ kind: "ready", runtime: createRuntime(config) });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message =
          err instanceof ConfigError
            ? err.message
            : `Unexpected error loading configuration: ${err instanceof Error ? err.message : String(err)}`;
        setState({ kind: "error", message });
      });
    return () => {
      cancelled = true;
    };
  }, [load]);

  if (state.kind === "loading") return <FullPageMessage message="Loading…" />;
  if (state.kind === "error") {
    return <ErrorPage title="Dashboard configuration error" message={state.message} />;
  }
  return <App runtime={state.runtime} />;
}
