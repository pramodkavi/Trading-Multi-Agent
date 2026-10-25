import { render, screen } from "@testing-library/react";
import { Badge } from "./Badge";

describe("Badge", () => {
  it("renders its label with the tone as data-tone", () => {
    render(<Badge tone="bad">failed</Badge>);
    const badge = screen.getByText("failed").closest("[data-tone]");
    expect(badge).toHaveAttribute("data-tone", "bad");
  });

  it("exposes the title for hover detail", () => {
    render(
      <Badge tone="warn" title="cloudwatch unavailable">
        Alarms
      </Badge>,
    );
    expect(screen.getByTitle("cloudwatch unavailable")).toBeInTheDocument();
  });
});
