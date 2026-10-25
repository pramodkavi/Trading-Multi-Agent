import { render, screen } from "@testing-library/react";
import { rejectionSeries } from "../../lib/funnel";
import { makeFunnelGroup } from "../../test/fixtures";
import { RejectionChart } from "./RejectionChart";

describe("RejectionChart", () => {
  it("shows an empty state, and no chart, when there are no rejections", () => {
    render(<RejectionChart series={{ rules: [], rows: [] }} groupBy="day" />);
    expect(screen.getByText("No rejections in this period.")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("names every rule in a legend and repeats the exact numbers in a data table", () => {
    const groups = [
      makeFunnelGroup("2026-10-24", { rejections_by_rule: { RULE_2_MIN_RR: 6, RULE_3_PREMIUM_DISCOUNT: 3 } }),
      makeFunnelGroup("2026-10-25", { rejections_by_rule: { RULE_2_MIN_RR: 4 } }),
    ];
    render(<RejectionChart series={rejectionSeries(groups)} groupBy="day" />);

    const legend = screen.getByRole("list", { name: /legend/i });
    expect(legend).toHaveTextContent("RULE_2_MIN_RR");
    expect(legend).toHaveTextContent("RULE_3_PREMIUM_DISCOUNT");
    expect(screen.getByRole("img", { name: /rejections by rule/i })).toBeInTheDocument();

    const table = screen.getByRole("table", { hidden: true });
    expect(table).toHaveTextContent("10-24");
    const cells = screen.getAllByRole("cell", { hidden: true }).map((c) => c.textContent);
    expect(cells).toEqual(["10-24", "6", "3", "10-25", "4", "0"]);
  });

  it("folds extra rules into Other and lists it in the legend and table", () => {
    const rules = Object.fromEntries(Array.from({ length: 8 }, (_, i) => [`R${i + 1}`, 8 - i]));
    render(<RejectionChart series={rejectionSeries([makeFunnelGroup("a", { rejections_by_rule: rules })])} groupBy="symbol" />);
    expect(screen.getByRole("list", { name: /legend/i })).toHaveTextContent("Other");
    expect(screen.getByRole("columnheader", { name: "Other", hidden: true })).toBeInTheDocument();
  });

  it("renders a hostile rule name as text", () => {
    const hostile = "<img src=x onerror=alert(1)>";
    const { container } = render(
      <RejectionChart
        series={rejectionSeries([makeFunnelGroup("a", { rejections_by_rule: { [hostile]: 2 } })])}
        groupBy="symbol"
      />,
    );
    expect(container.querySelector("img[src='x']")).toBeNull();
    expect(screen.getByRole("list", { name: /legend/i })).toHaveTextContent(hostile);
  });
});
