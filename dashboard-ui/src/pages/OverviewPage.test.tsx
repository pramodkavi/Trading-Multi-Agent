import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SignalsPage } from "../api/types";
import {
  CREDIT_ERROR,
  makeOutcome,
  makeOverview,
  makeSetup,
  makeWindow,
} from "../test/fixtures";
import {
  createTestClient,
  jsonResponse,
  renderWithProviders,
  resetStubApi,
  stubApi,
} from "../test/render";
import OverviewPage from "./OverviewPage";

const NO_SIGNALS: SignalsPage = { items: [], next_cursor: null };

afterEach(() => {
  resetStubApi();
  vi.useRealTimers();
});

function failedOverview(error: string | null) {
  const failed = makeOutcome({ status: "FAILED", error });
  return makeOverview({
    health: { ...makeOverview().health, last_scan: failed },
    windows_today: [
      makeWindow("LONDON", "2026-10-25T08:03:00Z", failed),
      makeWindow("NY", "2026-10-25T13:03:00Z", null),
    ],
  });
}

describe("OverviewPage", () => {
  it("shows a healthy system with no banner, four windows and the risk budget", async () => {
    stubApi({ "/overview": makeOverview(), "/signals": NO_SIGNALS });
    renderWithProviders(<OverviewPage />);

    expect(await screen.findByText("Open setups 1 / 3")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const strip = screen.getByTestId("health-strip");
    for (const badge of strip.querySelectorAll("[data-tone]")) {
      expect(badge).toHaveAttribute("data-tone", "ok");
    }
    expect(screen.getAllByTestId("window-card")).toHaveLength(4);
    expect(screen.getByText("Published 24 h 1 / 5")).toBeInTheDocument();
  });

  it("names the Anthropic credit balance when the failed scan error says so", async () => {
    stubApi({ "/overview": failedOverview(CREDIT_ERROR), "/signals": NO_SIGNALS });
    renderWithProviders(<OverviewPage />);

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveAttribute("data-tone", "bad");
    expect(within(banner).getByText("Anthropic credit balance too low")).toBeInTheDocument();
    expect(within(banner).queryByText(/scan window failed/i)).not.toBeInTheDocument();
    const [londonCard] = screen.getAllByTestId("window-card");
    expect(within(londonCard as HTMLElement).getByText("failed")).toBeInTheDocument();
  });

  it("uses the generic failed-scans wording for a non-credit failure", async () => {
    stubApi({ "/overview": failedOverview("ReadTimeout"), "/signals": NO_SIGNALS });
    renderWithProviders(<OverviewPage />);

    const banner = await screen.findByRole("alert");
    expect(within(banner).getByText("1 scan window failed today")).toBeInTheDocument();
    expect(within(banner).queryByText(/credit balance/i)).not.toBeInTheDocument();
  });

  it("shows alarm status as unavailable (amber), never ok, when CloudWatch is down", async () => {
    const base = makeOverview();
    stubApi({
      "/overview": makeOverview({
        health: { ...base.health, alarms: null, alarms_note: "cloudwatch unavailable" },
      }),
      "/signals": NO_SIGNALS,
    });
    renderWithProviders(<OverviewPage />);

    const badge = (await screen.findByText("Alarms unavailable")).closest("[data-tone]");
    expect(badge).toHaveAttribute("data-tone", "warn");
    expect(within(screen.getByRole("alert")).getByText("Alarm status unavailable")).toBeInTheDocument();
  });

  it("marks an unfired window pending, then missed once 15 minutes late", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-25T13:00:00Z"));
    const overview = makeOverview({
      windows_today: [makeWindow("NY", "2026-10-25T13:03:00Z", null)],
    });
    stubApi({ "/overview": overview, "/signals": NO_SIGNALS });
    const first = renderWithProviders(<OverviewPage />);
    expect((await screen.findByText("not run yet")).closest("[data-tone]")).toHaveAttribute(
      "data-tone",
      "neutral",
    );
    first.unmount();

    vi.setSystemTime(new Date("2026-10-25T13:20:00Z"));
    renderWithProviders(<OverviewPage />);
    expect((await screen.findByText("missed")).closest("[data-tone]")).toHaveAttribute(
      "data-tone",
      "warn",
    );
  });

  it("renders a long hostile scan error as truncated plain text", async () => {
    const hostile = `<script>alert(1)</script>${"x".repeat(500)}`;
    stubApi({ "/overview": failedOverview(hostile), "/signals": NO_SIGNALS });
    const { container } = renderWithProviders(<OverviewPage />);

    const error = await screen.findByTestId("window-error");
    expect(error.textContent?.length).toBeLessThanOrEqual(141);
    expect(error).toHaveAttribute("title", hostile);
    expect(container.querySelector("script")).toBeNull();
  });

  it("shows the waking state while Aurora resumes, then the content", async () => {
    let calls = 0;
    stubApi({
      "/overview": () =>
        calls++ === 0 ? jsonResponse({ reason: "db_resuming" }, 503) : jsonResponse(makeOverview()),
      "/signals": NO_SIGNALS,
    });
    renderWithProviders(<OverviewPage />, {
      client: createTestClient({ retry: 2, retryDelayMs: 300 }),
    });

    expect(await screen.findByText(/waking the database/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(await screen.findByText("Open setups 1 / 3")).toBeInTheDocument();
  });

  it("offers a retry when the overview cannot load", async () => {
    stubApi({ "/overview": () => jsonResponse({ reason: "db_error" }, 502), "/signals": NO_SIGNALS });
    renderWithProviders(<OverviewPage />);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/could not load the overview/i)).toBeInTheDocument();
    await userEvent.click(within(alert).getByRole("button", { name: /retry/i }));
  });

  it("handles no open setups and an unknown or negative unrealised R", async () => {
    stubApi({ "/overview": makeOverview({ open_setups: [] }), "/signals": NO_SIGNALS });
    const first = renderWithProviders(<OverviewPage />);
    expect(await screen.findByText("No open setups.")).toBeInTheDocument();
    first.unmount();
    resetStubApi();

    stubApi({
      "/overview": makeOverview({
        open_setups: [
          makeSetup({ setup_id: "a", unrealised_r: null, unrealised_r_note: "price unavailable" }),
          makeSetup({ setup_id: "b", symbol: "ETHUSDT", unrealised_r: -0.4 }),
        ],
      }),
      "/signals": NO_SIGNALS,
    });
    renderWithProviders(<OverviewPage />);

    expect(await screen.findByText("price unavailable")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("NaN");
    const negative = screen.getByText("−0.40 R");
    expect(negative.className).toMatch(/text-red/);
  });

  it("counts down to the next scan on the client clock", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-25T10:00:00Z"));
    const base = makeOverview();
    stubApi({
      "/overview": makeOverview({
        risk: { ...base.risk, next_scan_at: "2026-10-25T10:02:05Z" },
      }),
      "/signals": NO_SIGNALS,
    });
    renderWithProviders(<OverviewPage />);

    expect(await screen.findByText("Next scan in 2 m 05 s")).toBeInTheDocument();
    act(() => {
      vi.setSystemTime(new Date("2026-10-25T10:00:05Z"));
    });
    expect(await screen.findByText("Next scan in 2 m 00 s", {}, { timeout: 3000 })).toBeInTheDocument();
  });

  it("lists the latest signals as plain text", async () => {
    stubApi({
      "/overview": makeOverview(),
      "/signals": {
        items: [
          {
            id: "s1",
            scan_id: "scan-1",
            symbol: "SOLUSDT",
            strategy: "smc",
            direction: "SHORT",
            status: "PUBLISHED",
            created_at: "2026-10-25T08:03:00Z",
            decision_kind: "GATE_REJECTED",
            judge_ruling: null,
            session: "LONDON",
          },
        ],
        next_cursor: null,
      } satisfies SignalsPage,
    });
    renderWithProviders(<OverviewPage />);

    const list = await screen.findByTestId("latest-signals");
    expect(within(list).getByText("SOLUSDT")).toBeInTheDocument();
    expect(within(list).getByText("GATE_REJECTED")).toBeInTheDocument();
    expect(within(list).queryByRole("link")).not.toBeInTheDocument();
  });
});
