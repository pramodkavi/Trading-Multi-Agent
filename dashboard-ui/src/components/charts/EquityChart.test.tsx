import { render, screen } from "@testing-library/react";
import { equityLines } from "../../lib/analytics";
import { makeEquity } from "../../test/fixtures";
import { EquityChart } from "./EquityChart";

const ALL = { published: true, vetoed: true, gate_rejected: true };

describe("EquityChart", () => {
  it("shows an empty state when no curve has a point", () => {
    const empty = equityLines(makeEquity({ published: [], vetoed: [], gate_rejected: [] }));
    render(<EquityChart lines={empty} visible={ALL} />);
    expect(screen.getByText("No resolved outcomes in this period.")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("lists each visible line in the legend with its final R and a data table", () => {
    render(<EquityChart lines={equityLines(makeEquity())} visible={ALL} />);
    const legend = screen.getByRole("list", { name: /legend/i });
    expect(legend).toHaveTextContent("Published");
    expect(legend).toHaveTextContent("+5.00 R");
    expect(legend).toHaveTextContent("Vetoed (counterfactual)");
    expect(screen.getByRole("img", { name: /equity curve/i })).toBeInTheDocument();
    const cells = screen.getAllByRole("cell", { hidden: true }).map((c) => c.textContent);
    expect(cells).toEqual([
      "Published", "3", "+5.00 R",
      "Vetoed (counterfactual)", "2", "+2.00 R",
      "Gate-rejected (counterfactual)", "1", "−1.00 R",
    ]);
  });

  it("leaves a hidden line out of the legend and the table", () => {
    render(<EquityChart lines={equityLines(makeEquity())} visible={{ ...ALL, vetoed: false }} />);
    expect(screen.getByRole("list", { name: /legend/i })).not.toHaveTextContent("Vetoed");
    expect(screen.queryByRole("cell", { name: "Vetoed (counterfactual)", hidden: true })).not.toBeInTheDocument();
  });

  it("shows a dash for a visible line that has no points", () => {
    render(
      <EquityChart
        lines={equityLines(makeEquity({ gate_rejected: [] }))}
        visible={ALL}
      />,
    );
    const cells = screen.getAllByRole("cell", { hidden: true }).map((c) => c.textContent);
    expect(cells.slice(-3)).toEqual(["Gate-rejected (counterfactual)", "0", "—"]);
  });
});
