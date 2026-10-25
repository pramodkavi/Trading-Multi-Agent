import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DataTable } from "./DataTable";

describe("DataTable", () => {
  it("is collapsed by default and holds the exact cells", () => {
    const { container } = render(
      <DataTable caption="Rejections" columns={["Group", "RULE_1"]} rows={[["10-25", 4]]} />,
    );
    expect(container.querySelector("details")).not.toHaveAttribute("open");
    const table = screen.getByRole("table", { hidden: true });
    expect(table).toHaveAccessibleName("Rejections");
    expect(screen.getByRole("columnheader", { name: "RULE_1", hidden: true })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "4", hidden: true })).toBeInTheDocument();
  });

  it("opens from its summary", async () => {
    const { container } = render(<DataTable caption="c" columns={["a"]} rows={[[1]]} />);
    await userEvent.click(screen.getByText("Show data table"));
    expect(container.querySelector("details")).toHaveAttribute("open");
  });

  it("renders hostile cell text as text, not HTML", () => {
    const hostile = "<img src=x onerror=alert(1)>";
    const { container } = render(<DataTable caption="c" columns={["a"]} rows={[[hostile]]} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("cell", { hidden: true })).toHaveTextContent(hostile);
  });
});
