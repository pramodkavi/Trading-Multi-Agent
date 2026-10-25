import { screen } from "@testing-library/react";
import type { Problem } from "../lib/problems";
import { renderWithProviders } from "../test/render";
import { AlertBanner } from "./AlertBanner";

const bad: Problem = { id: "credits", tone: "bad", title: "Credits low", detail: "Top up." };
const warn: Problem = { id: "forecaster", tone: "warn", title: "Forecaster stale", detail: "2 stale" };

describe("AlertBanner", () => {
  it("renders nothing when there are no problems", () => {
    renderWithProviders(<AlertBanner problems={[]} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("is red when any problem is bad and lists every problem", () => {
    renderWithProviders(<AlertBanner problems={[warn, bad]} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-tone", "bad");
    expect(screen.getByText("Credits low")).toBeInTheDocument();
    expect(screen.getByText("Forecaster stale")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /system health/i })).toHaveAttribute("href", "/health");
  });

  it("is amber when every problem is a warning", () => {
    renderWithProviders(<AlertBanner problems={[warn]} />);
    expect(screen.getByRole("alert")).toHaveAttribute("data-tone", "warn");
  });
});
