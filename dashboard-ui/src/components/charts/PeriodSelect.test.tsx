import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PeriodSelect } from "./PeriodSelect";

describe("PeriodSelect", () => {
  it("offers 7, 30 and 90 days and marks the current one", () => {
    render(<PeriodSelect value={30} onChange={() => undefined} />);
    expect(screen.getAllByRole("radio")).toHaveLength(3);
    expect(screen.getByRole("radio", { name: "30 days" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "7 days" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("radiogroup", { name: /period/i })).toBeInTheDocument();
  });

  it("reports the chosen period", async () => {
    const onChange = vi.fn();
    render(<PeriodSelect value={30} onChange={onChange} />);
    await userEvent.click(screen.getByRole("radio", { name: "7 days" }));
    expect(onChange).toHaveBeenCalledWith(7);
  });
});
