import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { PMConsoleTargetsResponse } from "../../../types";
import { TargetSidebar } from "./TargetSidebar";

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}</output>;
}

const targets: PMConsoleTargetsResponse = {
  generated_at: "2025-01-02T03:05:00Z",
  targets: [
    { target_key: "btc", implemented_views: ["position", "thesis"], default_view: "position" },
    { target_key: "gold", implemented_views: ["thesis"], default_view: "thesis" },
    { target_key: "sox", implemented_views: ["operator", "position"], default_view: "position" },
  ],
};

describe("TargetSidebar target selector", () => {
  it("keeps the current view when supported and falls back to default_view otherwise", () => {
    render(
      <MemoryRouter initialEntries={["/targets/btc/thesis"]}>
        <TargetSidebar
          activeTarget="btc"
          activeView="thesis"
          targetsQuery={{ isLoading: false, isError: false, data: targets }}
        />
        <LocationProbe />
      </MemoryRouter>,
    );

    const selector = screen.getByRole("combobox", { name: "Select target" });
    expect(selector).toHaveValue("btc");
    expect(selector.querySelectorAll("option:not([disabled])")).toHaveLength(3);
    expect(screen.getByRole("link", { name: "PM Overview" })).toHaveAttribute(
      "href",
      "/overview",
    );
    expect(screen.queryByText(/Workspace-backed PM daily pages/i)).not.toBeInTheDocument();

    fireEvent.change(selector, { target: { value: "gold" } });
    expect(screen.getByTestId("location")).toHaveTextContent("/targets/gold/thesis");

    fireEvent.change(selector, { target: { value: "sox" } });
    expect(screen.getByTestId("location")).toHaveTextContent("/targets/sox/position");
  });

  it("shows Operator only when the backend declares the target view", () => {
    render(
      <MemoryRouter initialEntries={["/targets/sox/position"]}>
        <TargetSidebar
          activeTarget="sox"
          activeView="position"
          targetsQuery={{ isLoading: false, isError: false, data: targets }}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Operator" })).toHaveAttribute(
      "href",
      "/targets/sox/operator",
    );
    expect(screen.queryByRole("link", { name: "Operator" })).not.toHaveAttribute(
      "href",
      "/targets/gold/operator",
    );
  });

  it("keeps target navigation in the approved operational order", () => {
    const targetViews: PMConsoleTargetsResponse = {
      generated_at: "2025-01-02T03:05:00Z",
      targets: [
        {
          target_key: "sox",
          implemented_views: ["operator", "episodes", "thesis", "pm", "position"],
          default_view: "position",
        },
      ],
    };
    render(
      <MemoryRouter initialEntries={["/targets/sox/position"]}>
        <TargetSidebar
          activeTarget="sox"
          activeView="position"
          targetsQuery={{ isLoading: false, isError: false, data: targetViews }}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("navigation", { name: "Target views" })).toHaveTextContent(
      "OperatorEpisodesThesis EvolutionPM TrajectoryPosition Management",
    );
  });
});
