import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import {
  makeCriticReports,
  makeEquity,
  makeReviews,
  makeSkeptic,
  makeWinRate,
} from "../test/fixtures";
import {
  createTestClient,
  jsonResponse,
  renderWithProviders,
  resetStubApi,
  stubApi,
} from "../test/render";
import AnalyticsPage from "./AnalyticsPage";

const firstReport = () => {
  const report = makeCriticReports().items[0];
  if (!report) throw new Error("fixture has no report");
  return report;
};

const SINCE_90 = "since=2026-07-27T00%3A00%3A00Z";
const REPORT_ID = "99999999-9999-4999-8999-999999999999";

function Where() {
  const loc = useLocation();
  return <output data-testid="where">{`${loc.pathname}${loc.search}`}</output>;
}

function renderPage(route = "/analytics", opts: Parameters<typeof renderWithProviders>[1] = {}) {
  return renderWithProviders(
    <>
      <AnalyticsPage />
      <Where />
    </>,
    { route, ...opts },
  );
}

function routes(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    "/analytics/win-rate": makeWinRate(),
    "/analytics/skeptic-value": makeSkeptic(),
    "/analytics/equity-curve": makeEquity(),
    "/critic/reports": makeCriticReports(),
    "/critic/reviews": makeReviews(),
    ...overrides,
  };
}

const section = (name: RegExp) => {
  const el = screen.getByRole("heading", { name }).closest("section");
  if (!el) throw new Error(`no section ${String(name)}`);
  return el as HTMLElement;
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-25T10:00:00Z"));
});

afterEach(() => {
  resetStubApi();
  vi.useRealTimers();
});

describe("AnalyticsPage", () => {
  it("requests the 90-day default for every panel and re-requests when the period or key changes", async () => {
    const requested = stubApi(routes());
    renderPage();
    await screen.findByRole("table", { name: /win rate by tag/i });
    expect(requested).toEqual(
      expect.arrayContaining([
        `/api/analytics/win-rate?${SINCE_90}&by=tag`,
        `/api/analytics/skeptic-value?${SINCE_90}`,
        `/api/analytics/equity-curve?${SINCE_90}`,
        "/api/critic/reports?limit=5",
        "/api/critic/reviews?limit=10",
      ]),
    );

    await userEvent.click(screen.getByRole("radio", { name: "Session" }));
    await waitFor(() => expect(requested).toContain(`/api/analytics/win-rate?${SINCE_90}&by=session`));
    await userEvent.click(screen.getByRole("radio", { name: "Week" }));
    await waitFor(() => expect(requested).toContain(`/api/analytics/win-rate?${SINCE_90}&by=time`));

    await userEvent.click(screen.getByRole("radio", { name: "7 days" }));
    await waitFor(() =>
      expect(requested).toContain("/api/analytics/skeptic-value?since=2026-10-18T00%3A00%3A00Z"),
    );
    expect(screen.getByTestId("where")).toHaveTextContent("/analytics?days=7");
  });

  it("shows win rate with its sample size, a dash for no rate and a low-sample tag", async () => {
    stubApi(routes());
    renderPage();

    const table = await screen.findByRole("table", { name: /win rate by tag/i });
    const row = (label: string) => within(table).getByText(label).closest("tr") as HTMLElement;
    expect(within(row("htf-bias")).getByText("62.5%")).toBeInTheDocument();
    expect(within(row("htf-bias")).getByText("5 / 3")).toBeInTheDocument();
    expect(within(row("htf-bias")).getByText("10")).toBeInTheDocument();
    expect(within(row("htf-bias")).queryByText(/low sample/i)).not.toBeInTheDocument();
    expect(within(row("liquidity-sweep")).getByText(/low sample/i)).toBeInTheDocument();
    expect(within(row("unresolved-tag")).getByText("—")).toBeInTheDocument();
    expect(screen.getByText("Outcomes: 9 replayed, 3 forecaster-judged.")).toBeInTheDocument();
  });

  it("shows the three Skeptic bars, the counterfactual caption and the excluded counts", async () => {
    stubApi(routes());
    renderPage();

    const skeptic = await screen.findByRole("table", { name: /skeptic value/i });
    for (const label of ["Judge respected the objection (veto)", "Overruled with a caveat", "Overruled"]) {
      expect(within(skeptic).getByText(label)).toBeInTheDocument();
    }
    expect(within(skeptic).getByText(/counterfactual/i)).toBeInTheDocument();
    expect(screen.getByText("2 runs excluded (no macro data) · 1 with no ruling")).toBeInTheDocument();
  });

  it("starts with only the published curve; counterfactual curves appear when ticked", async () => {
    stubApi(routes());
    renderPage();

    const equity = await waitForSection(/equity curve/i);
    const legend = await within(equity).findByRole("list", { name: "Legend" });
    expect(legend).toHaveTextContent("Published");
    expect(legend).toHaveTextContent("+5.00 R");
    expect(legend).not.toHaveTextContent("Vetoed");
    expect(within(equity).getByText(/hypothetical/i)).toBeInTheDocument();

    await userEvent.click(within(equity).getByRole("checkbox", { name: /vetoed/i }));
    expect(within(equity).getByRole("list", { name: "Legend" })).toHaveTextContent("Vetoed (counterfactual)");
    expect(within(equity).getByRole("list", { name: "Legend" })).not.toHaveTextContent("Gate-rejected");
  });

  it("shows empty states for every section when there is no data yet", async () => {
    stubApi(
      routes({
        "/analytics/win-rate": makeWinRate({ buckets: [] }),
        "/analytics/skeptic-value": makeSkeptic({
          respected: { n: 0, wins: 0, losses: 0, win_rate: null },
          overruled_with_caveat: { n: 0, wins: 0, losses: 0, win_rate: null },
          overruled: { n: 0, wins: 0, losses: 0, win_rate: null },
          no_macro_data_excluded: 0,
          unruled: 0,
        }),
        "/analytics/equity-curve": makeEquity({ published: [], vetoed: [], gate_rejected: [] }),
        "/critic/reports": { items: [] },
        "/critic/reviews": { items: [], next_cursor: null },
      }),
    );
    const { container } = renderPage();

    expect(await screen.findByText("No published decisions in this period.")).toBeInTheDocument();
    expect(screen.getByText("No objections were judged in this period.")).toBeInTheDocument();
    expect(screen.getByText("No resolved outcomes in this period.")).toBeInTheDocument();
    expect(screen.getByText("No Critic reports yet — the first backfill is pending.")).toBeInTheDocument();
    expect(screen.getByText("No reviews yet.")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/NaN|Infinity|undefined/);
  });

  it("lists Critic reports with an https-only PR link and loads the report text on demand", async () => {
    const requested = stubApi(
      routes({
        [`/critic/reports/${REPORT_ID}`]: {
          id: REPORT_ID,
          period_start: "2026-10-01T00:00:00Z",
          period_end: "2026-10-15T00:00:00Z",
          report: {},
          markdown: "# Report\n<img src=x onerror=alert(1)>",
          pr_url: null,
          created_at: "2026-10-16T22:30:00Z",
        },
      }),
    );
    const { container } = renderPage();

    const critic = await waitForSection(/critic/i);
    expect(await within(critic).findByText(/most gate rejections were RULE_2_MIN_RR/i)).toBeInTheDocument();
    expect(within(critic).getByRole("link", { name: /proposed-rules pr/i })).toHaveAttribute(
      "href",
      "https://github.com/pramodkavi/Trading-Multi-Agent/pull/30",
    );
    expect(requested.some((u) => u.includes(`/critic/reports/${REPORT_ID}`))).toBe(false);

    await userEvent.click(within(critic).getByRole("button", { name: /show report/i }));
    const pre = await within(critic).findByText(/# Report/);
    expect(pre.tagName).toBe("PRE");
    expect(container.querySelector("img[src='x']")).toBeNull();
    expect(requested.filter((u) => u.includes(`/critic/reports/${REPORT_ID}`))).toHaveLength(1);
  });

  it("renders no link for a PR URL that is not https", async () => {
    stubApi(
      routes({
        "/critic/reports": makeCriticReports({
          items: [{ ...firstReport(), pr_url: "javascript:alert(1)" }],
        }),
      }),
    );
    renderPage();
    const critic = await waitForSection(/critic/i);
    await within(critic).findByText(/most gate rejections/i);
    expect(within(critic).queryByRole("link")).not.toBeInTheDocument();
  });

  it("lists review grades with Yes / No / dash cells", async () => {
    stubApi(routes());
    renderPage();

    const table = await screen.findByRole("table", { name: /latest decision reviews/i });
    const row = within(table).getByText("over_cautious_veto").closest("tr") as HTMLElement;
    expect(within(row).getByText("The veto ignored a clean OB retest.")).toBeInTheDocument();
    const cells = within(row).getAllByRole("cell").map((c) => c.textContent);
    expect(cells.slice(-2)).toEqual(["No", "Yes"]);
  });

  it("confines one failing panel to its own section", async () => {
    stubApi(routes({ "/analytics/skeptic-value": () => jsonResponse({ reason: "db_error" }, 502) }));
    renderPage();

    expect(await screen.findByText(/could not load skeptic value/i)).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: /win rate by tag/i })).toBeInTheDocument();
  });

  it("keeps a panel's last good data when a background refresh fails", async () => {
    let calls = 0;
    stubApi(
      routes({
        "/analytics/win-rate": () =>
          calls++ === 0 ? jsonResponse(makeWinRate()) : jsonResponse({ reason: "db_error" }, 502),
      }),
    );
    renderPage("/analytics", { client: createTestClient({ refetchIntervalMs: 40 }) });

    await screen.findByRole("table", { name: /win rate by tag/i });
    expect(await screen.findByText(/could not refresh win rate/i)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /win rate by tag/i })).toBeInTheDocument();
  });

  it("renders hostile and very long strings as wrapped text", async () => {
    const hostile = "<img src=x onerror=alert(1)>";
    const long = "x".repeat(500);
    stubApi(
      routes({
        "/analytics/win-rate": makeWinRate({
          buckets: [{ key: hostile, wins: 1, losses: 0, decisive: 1, win_rate: 1, n: 1 }],
        }),
        "/critic/reports": makeCriticReports({
          items: [{ ...firstReport(), summary: long }],
        }),
      }),
    );
    const { container } = renderPage();

    expect(await screen.findByText(hostile)).toBeInTheDocument();
    expect(container.querySelector("img[src='x']")).toBeNull();
    expect(await screen.findByText(long)).toHaveClass("break-words");
  });
});

async function waitForSection(name: RegExp): Promise<HTMLElement> {
  await screen.findByRole("heading", { name });
  return section(name);
}
