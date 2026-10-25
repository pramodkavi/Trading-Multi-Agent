import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SegmentedControl } from "./SegmentedControl";

const OPTIONS = [
  { value: "day", label: "Day" },
  { value: "window", label: "Window" },
] as const;

describe("SegmentedControl", () => {
  it("is a labelled radio group that marks the current option", () => {
    render(<SegmentedControl label="Group by" options={OPTIONS} value="day" onChange={() => undefined} />);
    expect(screen.getByRole("radiogroup", { name: "Group by" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Day" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Window" })).toHaveAttribute("aria-checked", "false");
  });

  it("reports the chosen value", async () => {
    const onChange = vi.fn();
    render(<SegmentedControl label="Group by" options={OPTIONS} value="day" onChange={onChange} />);
    await userEvent.click(screen.getByRole("radio", { name: "Window" }));
    expect(onChange).toHaveBeenCalledWith("window");
  });
});
