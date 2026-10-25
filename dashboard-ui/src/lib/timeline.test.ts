import { makeEvaluation, makeLifecycle, makeRow, makeStep, PAST_SIGNAL_ID } from "../test/fixtures";
import { buildTimeline } from "./timeline";

const kinds = (l: ReturnType<typeof makeLifecycle>) => buildTimeline(l).map((s) => s.kind);

describe("buildTimeline", () => {
  it("orders a full PUBLISHED chain analyzer → … → review", () => {
    expect(kinds(makeLifecycle())).toEqual([
      "analyzer",
      "historian",
      "skeptic",
      "judge",
      "forecast",
      "forecast",
      "outcome",
      "review",
    ]);
  });

  it("gives agent steps their run id and the rest none", () => {
    const steps = buildTimeline(makeLifecycle());
    expect(steps.find((s) => s.kind === "analyzer")?.runId).toBe("run-analyzer-1");
    expect(steps.find((s) => s.kind === "forecast")?.runId).toBeNull();
    expect(steps.find((s) => s.kind === "outcome")?.runId).toBeNull();
  });

  it("a gate-rejected decision is analyzer + gate only", () => {
    const l = makeLifecycle({
      signal: {
        ...makeLifecycle().signal,
        ...makeRow({
          decision_kind: "GATE_REJECTED",
          violated_rule: "RULE_2_MIN_RR",
          skip_details: "R:R 1.2 < 3",
          outcome: null,
        }),
        payload: {},
        features: {},
        outcome_metadata: null,
      },
      steps: [makeStep("analyzer", { direction: "LONG", entry_price: 100 }, 1)],
      evaluations: [],
      resolution: null,
      review: null,
      setup: null,
    });
    const steps = buildTimeline(l);
    expect(steps.map((s) => s.kind)).toEqual(["analyzer", "gate"]);
    const gate = steps[1];
    expect(gate?.tone).toBe("bad");
    expect(gate?.summary).toContain("RULE_2_MIN_RR");
    expect(gate?.summary).toContain("R:R 1.2 < 3");
  });

  it("shows a NoMacroData skeptic as an amber 'Macro data unavailable'", () => {
    const l = makeLifecycle();
    l.steps[2] = makeStep("skeptic", { no_macro_data: true }, 3);
    const skeptic = buildTimeline(l).find((s) => s.kind === "skeptic");
    expect(skeptic?.summary).toBe("Macro data unavailable");
    expect(skeptic?.tone).toBe("warn");
  });

  it("marks an ERROR evaluation bad and shows its error", () => {
    const l = makeLifecycle({
      evaluations: [makeEvaluation({ status: "ERROR", reasoning: null, error: "credit balance" })],
    });
    const forecast = buildTimeline(l).find((s) => s.kind === "forecast");
    expect(forecast?.tone).toBe("bad");
    expect(forecast?.summary).toContain("credit balance");
  });

  it("emits no outcome or review when neither exists", () => {
    const base = makeLifecycle();
    const l = makeLifecycle({
      signal: { ...base.signal, outcome: null },
      resolution: null,
      review: null,
    });
    expect(kinds(l)).not.toContain("outcome");
    expect(kinds(l)).not.toContain("review");
  });

  it("takes the outcome from the replay when present, else from the signal", () => {
    const withReplay = buildTimeline(makeLifecycle()).find((s) => s.kind === "outcome");
    expect(withReplay?.title).toBe("Outcome (replay)");
    expect(withReplay?.summary).toContain("WIN");
    expect(withReplay?.summary).toContain("+3.00 R");

    const base = makeLifecycle();
    const noReplay = buildTimeline(
      makeLifecycle({ signal: { ...base.signal, outcome: "LOSS" }, resolution: null }),
    ).find((s) => s.kind === "outcome");
    expect(noReplay?.title).toBe("Outcome");
    expect(noReplay?.summary).toBe("LOSS");
    expect(noReplay?.tone).toBe("bad");
  });

  it("keeps the Historian matches in the step details for linking", () => {
    const historian = buildTimeline(makeLifecycle()).find((s) => s.kind === "historian");
    const matches = (historian?.details as { matches: { signal_id: string }[] }).matches;
    expect(matches[0]?.signal_id).toBe(PAST_SIGNAL_ID);
  });

  it("marks a failed review as a warning with its error", () => {
    const base = makeLifecycle();
    const l = makeLifecycle({
      review: { ...(base.review as NonNullable<typeof base.review>), error: "timeout", review: {} },
    });
    const review = buildTimeline(l).find((s) => s.kind === "review");
    expect(review?.tone).toBe("warn");
    expect(review?.summary).toContain("timeout");
  });

  it("carries latency and cost for agent steps and evaluations, null elsewhere", () => {
    const steps = buildTimeline(makeLifecycle());
    const skeptic = steps.find((s) => s.kind === "skeptic");
    expect(skeptic?.latencyMs).toBe(1200);
    expect(skeptic?.costUsd).toBe(0.0123);
    expect(steps.find((s) => s.kind === "analyzer")?.costUsd).toBeNull();
    const forecast = steps.find((s) => s.kind === "forecast");
    expect(forecast?.latencyMs).toBe(900);
    expect(forecast?.costUsd).toBe(0.004);
    expect(steps.find((s) => s.kind === "outcome")?.latencyMs).toBeNull();
  });

  it("ignores critic agent runs (the review step comes from the review row)", () => {
    const l = makeLifecycle();
    l.steps.push(makeStep("critic", { lesson: "x" }, 5));
    expect(kinds(l).filter((k) => k === "review")).toHaveLength(1);
  });
});
