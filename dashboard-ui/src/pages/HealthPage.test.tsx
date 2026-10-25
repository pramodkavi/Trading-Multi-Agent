import { screen, waitFor, within } from "@testing-library/react";
import {
  CREDIT_ERROR,
  healthRoutes,
  makeAgents,
  makeAlarms,
  makeScanRow,
  makeScans,
} from "../test/fixtures";
import {
  createTestClient,
  jsonResponse,
  renderWithProviders,
  resetStubApi,
  stubApi,
} from "../test/render";
import HealthPage from "./HealthPage";

afterEach(() => {
  resetStubApi();
});

function panel(name: RegExp): HTMLElement {
  const section = screen.getByRole("heading", { name }).closest("section");
  if (!section) throw new Error(`no section for ${String(name)}`);
  return section;
}

describe("HealthPage", () => {
  it("renders every panel from a healthy system", async () => {
    stubApi(
      healthRoutes({
        "/system/scans": makeScans([
          makeScanRow({ id: "a" }),
          makeScanRow({ id: "b", window: "NY", started_at: "2026-10-25T13:03:00Z" }),
        ]),
      }),
    );
    renderWithProviders(<HealthPage />);

    expect(await screen.findAllByTestId("scan-row")).toHaveLength(2);
    for (const name of [
      /database/i,
      /scan history/i,
      /alarms/i,
      /agent usage/i,
      /forecaster sweeps/i,
      /lambda metrics/i,
      /configuration/i,
    ]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
    expect(await screen.findByText(/database ok/i)).toBeInTheDocument();
  });

  it("flags a credit-balance scan failure and keeps the full error in the title", async () => {
    stubApi(
      healthRoutes({
        "/system/scans": makeScans([
          makeScanRow({ id: "x", status: "FAILED", error_message: CREDIT_ERROR }),
        ]),
      }),
    );
    renderWithProviders(<HealthPage />);

    const row = await screen.findByTestId("scan-row");
    expect(within(row).getByText("credits")).toBeInTheDocument();
    const error = within(row).getByTestId("scan-error");
    expect(error.textContent?.length).toBeLessThanOrEqual(120);
    expect(error).toHaveAttribute("title", CREDIT_ERROR);
  });

  it("shows an amber note when alarms are unavailable and still renders other panels", async () => {
    stubApi(
      healthRoutes({ "/system/alarms": makeAlarms({ alarms: null, note: "cloudwatch access denied" }) }),
    );
    renderWithProviders(<HealthPage />);

    const note = await within(await waitForPanel(/alarms/i)).findByText("cloudwatch access denied");
    expect(note.closest("[data-tone]")).toHaveAttribute("data-tone", "warn");
    expect(await screen.findAllByTestId("scan-row")).toHaveLength(1);
  });

  it("confines a failing metrics call to its own panel", async () => {
    stubApi(healthRoutes({ "/system/metrics": () => jsonResponse({ reason: "db_error" }, 502) }));
    renderWithProviders(<HealthPage />);

    const metrics = await waitForPanel(/lambda metrics/i);
    expect(await within(metrics).findByText(/could not load lambda metrics/i)).toBeInTheDocument();
    expect(await screen.findAllByTestId("scan-row")).toHaveLength(1);
  });

  it("shows unknown error counts as a dash, never as zero", async () => {
    stubApi(healthRoutes());
    renderWithProviders(<HealthPage />);

    const usage = await waitForPanel(/agent usage/i);
    const skeptic = (await within(usage).findByText("skeptic")).closest("tr");
    const scan = within(usage).getByText("scan").closest("tr");
    expect(within(skeptic as HTMLElement).getByTestId("usage-errors")).toHaveTextContent("—");
    expect(within(scan as HTMLElement).getByTestId("usage-errors")).toHaveTextContent("3");
  });

  it("shows LLM cost per day to four decimals with a matching total", async () => {
    stubApi(healthRoutes());
    renderWithProviders(<HealthPage />);

    const usage = await waitForPanel(/agent usage/i);
    expect(await within(usage).findByText("$0.5000")).toBeInTheDocument();
    expect(within(usage).getByText("$0.2500")).toBeInTheDocument();
    expect(within(usage).getByText("$0.7500")).toBeInTheDocument();
  });

  it("fetches Lambda metrics once while the other panels keep polling", async () => {
    const requested = stubApi(healthRoutes());
    renderWithProviders(<HealthPage />, {
      client: createTestClient({ refetchIntervalMs: 40 }),
    });

    await waitFor(() =>
      expect(requested.filter((u) => u.startsWith("/api/system/scans")).length).toBeGreaterThanOrEqual(3),
    );
    expect(requested.filter((u) => u.startsWith("/api/system/metrics"))).toHaveLength(1);
  });

  it("turns the newest scan badge red on a forced failure and green when it recovers", async () => {
    stubApi(
      healthRoutes({
        "/system/scans": makeScans([makeScanRow({ status: "FAILED", error_message: "ReadTimeout" })]),
      }),
    );
    const first = renderWithProviders(<HealthPage />);
    const failedRow = await screen.findByTestId("scan-row");
    expect(within(failedRow).getByText("FAILED").closest("[data-tone]")).toHaveAttribute(
      "data-tone",
      "bad",
    );
    first.unmount();
    resetStubApi();

    stubApi(healthRoutes({ "/system/scans": makeScans([makeScanRow({ status: "SUCCESS" })]) }));
    renderWithProviders(<HealthPage />);
    const okRow = await screen.findByTestId("scan-row");
    expect(within(okRow).getByText("SUCCESS").closest("[data-tone]")).toHaveAttribute(
      "data-tone",
      "ok",
    );
  });

  it("uses the default agent window when none is passed", async () => {
    const requested = stubApi(healthRoutes({ "/system/agents": makeAgents() }));
    renderWithProviders(<HealthPage />);
    await screen.findByText("$0.7500");
    expect(requested).toContain("/api/system/agents?days=7");
  });
});

/** Wait for a panel's heading to exist, then return its section. */
async function waitForPanel(name: RegExp): Promise<HTMLElement> {
  await screen.findByRole("heading", { name });
  return panel(name);
}
