import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import {
  makeEvaluation,
  makeLifecycle,
  makeRow,
  makeStep,
  PAST_SIGNAL_ID,
  SIGNAL_ID,
} from "../test/fixtures";
import { jsonResponse, renderWithProviders, resetStubApi, stubApi } from "../test/render";
import SignalDetailPage from "./SignalDetailPage";

function renderDetail(id: string = SIGNAL_ID) {
  return renderWithProviders(
    <Routes>
      <Route path="/signals/:id" element={<SignalDetailPage />} />
      <Route path="/signals" element={<p>journal</p>} />
    </Routes>,
    { route: `/signals/${id}` },
  );
}

const stepTitles = () => screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);

afterEach(() => {
  resetStubApi();
});

describe("SignalDetailPage", () => {
  it("renders the full chain in order with the header facts", async () => {
    stubApi({ [`/signals/${SIGNAL_ID}`]: makeLifecycle() });
    renderDetail();

    expect(await screen.findByRole("heading", { level: 1, name: /BTCUSDT/ })).toBeInTheDocument();
    expect(stepTitles()).toEqual([
      "Analyzer proposal",
      "Historian",
      "Skeptic",
      "Judge",
      "Forecaster: STILL_VALID",
      "Forecaster: INVALIDATED",
      "Outcome (replay)",
      "Critic review",
    ]);
    expect(screen.getByText(SIGNAL_ID)).toBeInTheDocument();
  });

  it("shows a gate-rejected decision as analyzer + gate with the violated rule", async () => {
    const base = makeLifecycle();
    stubApi({
      [`/signals/${SIGNAL_ID}`]: makeLifecycle({
        signal: {
          ...base.signal,
          ...makeRow({
            id: SIGNAL_ID,
            decision_kind: "GATE_REJECTED",
            violated_rule: "RULE_2_MIN_RR",
            skip_details: "R:R 1.2 < 3",
            outcome: null,
          }),
        },
        steps: [makeStep("analyzer", { direction: "LONG", entry_price: 100 }, 1)],
        evaluations: [],
        resolution: null,
        review: null,
        setup: null,
      }),
    });
    renderDetail();

    await screen.findByRole("heading", { level: 1 });
    expect(stepTitles()).toEqual(["Analyzer proposal", "Risk gate rejected"]);
    expect(screen.getByText(/RULE_2_MIN_RR: R:R 1.2 < 3/)).toBeInTheDocument();
  });

  it("links each Historian match to its own detail page", async () => {
    stubApi({ [`/signals/${SIGNAL_ID}`]: makeLifecycle() });
    renderDetail();

    const link = await screen.findByRole("link", { name: /ETHUSDT/ });
    expect(link).toHaveAttribute("href", `/signals/${PAST_SIGNAL_ID}`);
  });

  it("shows a NoMacroData Skeptic as an amber 'Macro data unavailable'", async () => {
    const l = makeLifecycle();
    l.steps[2] = makeStep("skeptic", { no_macro_data: true }, 3);
    stubApi({ [`/signals/${SIGNAL_ID}`]: l });
    renderDetail();

    const text = await screen.findByText("Macro data unavailable");
    expect(text.closest("[data-testid='timeline-step']")).toHaveAttribute("data-tone", "warn");
  });

  it("fetches a step's raw output only when it is opened, and only once", async () => {
    const requested = stubApi({
      [`/signals/${SIGNAL_ID}`]: makeLifecycle(),
      [`/signals/${SIGNAL_ID}/runs/run-analyzer-1`]: {
        id: "run-analyzer-1",
        output: { entry_price: 100, confluence_narrative: "OB retest" },
      },
    });
    renderDetail();
    const [analyzer] = await screen.findAllByTestId("timeline-step");
    expect(requested.filter((u) => u.includes("/runs/"))).toEqual([]);

    await userEvent.click(within(analyzer as HTMLElement).getByRole("button", { name: /raw output/i }));

    expect(await within(analyzer as HTMLElement).findByText(/OB retest/)).toBeInTheDocument();
    await userEvent.click(within(analyzer as HTMLElement).getByRole("button", { name: /raw output/i }));
    await userEvent.click(within(analyzer as HTMLElement).getByRole("button", { name: /raw output/i }));
    expect(requested.filter((u) => u.includes("/runs/"))).toHaveLength(1);
  });

  it("answers an unknown id with 'Signal not found' and a way back", async () => {
    stubApi({ [`/signals/${SIGNAL_ID}`]: () => jsonResponse({ reason: "not_found" }, 404) });
    renderDetail();

    expect(await screen.findByText("Signal not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to signals/i })).toHaveAttribute("href", "/signals");
  });

  it("does not call the API for an id that is not a UUID", async () => {
    const requested = stubApi({});
    renderDetail("not-a-uuid");

    expect(await screen.findByText("Signal not found")).toBeInTheDocument();
    expect(requested).toEqual([]);
  });

  it("shows an Forecaster ERROR evaluation with its error text in a bad tone", async () => {
    stubApi({
      [`/signals/${SIGNAL_ID}`]: makeLifecycle({
        evaluations: [makeEvaluation({ status: "ERROR", reasoning: null, error: "credit balance is too low" })],
      }),
    });
    renderDetail();

    const text = await screen.findByText(/credit balance is too low/);
    expect(text.closest("[data-testid='timeline-step']")).toHaveAttribute("data-tone", "bad");
  });

  it("renders missing values without null / undefined / NaN", async () => {
    const base = makeLifecycle();
    stubApi({
      [`/signals/${SIGNAL_ID}`]: makeLifecycle({
        signal: { ...base.signal, outcome: null, direction: null, session: null, judge_ruling: null },
        resolution: null,
        review: null,
        setup: null,
        evaluations: [],
      }),
    });
    renderDetail();

    await screen.findByRole("heading", { level: 1 });
    await waitFor(() => expect(document.body.textContent).not.toMatch(/null|undefined|NaN/));
  });

  it("renders hostile reasoning text as text, not HTML", async () => {
    const hostile = "<img src=x onerror=alert(1)> done";
    stubApi({
      [`/signals/${SIGNAL_ID}`]: makeLifecycle({
        evaluations: [makeEvaluation({ reasoning: hostile })],
      }),
    });
    const { container } = renderDetail();

    expect(await screen.findByText(hostile)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });
});
