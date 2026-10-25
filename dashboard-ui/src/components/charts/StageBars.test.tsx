import { render, screen, within } from "@testing-library/react";
import { funnelStages } from "../../lib/funnel";
import { makeFunnelGroup } from "../../test/fixtures";
import { StageBars } from "./StageBars";

const row = (label: string) => screen.getByText(label).closest("[data-stage]") as HTMLElement;

describe("StageBars", () => {
  it("renders every stage with its count and a bar proportional to the largest", () => {
    render(<StageBars stages={funnelStages(makeFunnelGroup("total"))} />);
    expect(within(row("Analyzer proposals")).getByText("30")).toBeInTheDocument();
    const widths = Object.fromEntries(
      ["Scans", "Symbols scanned", "Analyzer proposals", "Published"].map((label) => [
        label,
        (row(label).querySelector("[data-bar]") as HTMLElement).style.width,
      ]),
    );
    expect(widths["Symbols scanned"]).toBe("100%");
    expect(widths["Scans"]).toBe("25%");
    expect(widths["Published"]).toBe("1.875%");
  });

  it("shows the ratio to the previous stage with one decimal, and a dash when there is none", () => {
    render(<StageBars stages={funnelStages(makeFunnelGroup("total"))} />);
    expect(within(row("Analyzer proposals")).getByText("18.8% of previous")).toBeInTheDocument();
    expect(within(row("Scans")).getByText("—")).toBeInTheDocument();
    expect(within(row("Symbols scanned")).getByText("—")).toBeInTheDocument();
  });

  it("draws zero-width bars and no NaN when everything is zero", () => {
    const empty = makeFunnelGroup("total", {
      scans: 0,
      symbols_scanned: 0,
      proposals: 0,
      gate_passed: 0,
      published: 0,
    });
    const { container } = render(<StageBars stages={funnelStages(empty)} />);
    for (const bar of container.querySelectorAll<HTMLElement>("[data-bar]")) {
      expect(bar.style.width).toBe("0%");
    }
    expect(container.textContent).not.toMatch(/NaN|Infinity|undefined/);
  });
});
