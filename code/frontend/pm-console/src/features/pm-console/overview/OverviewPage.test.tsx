import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PMConsoleOverviewResponse } from "../../../types";
import { OverviewPage } from "./OverviewPage";

const useOverviewQuery = vi.fn();

vi.mock("../hooks", () => ({
  usePmConsoleOverviewQuery: () => useOverviewQuery(),
}));

const overview: PMConsoleOverviewResponse = {
  generated_at: "2026-08-27T03:00:00Z",
  mounted_target_count: 2,
  reload_error: null,
  targets: [
    {
      target_key: "btc",
      runtime_mode: "replay",
      workspace_root: "D:/workspaces/btc",
      workspace_name: "btc",
      implemented_views: ["operator", "thesis", "position"],
      default_view: "position",
      readiness: { code: "ready", explanation: "ready" },
      problem_codes: [],
      current_portfolio_state: null,
      latest_pm_decision: null,
      latest_execution: null,
      execution_status: { code: "no_decision", explanation: "none" },
      latest_thesis_revision: null,
      operator_status: { code: "ready", explanation: "present" },
    },
    {
      target_key: "sox",
      runtime_mode: "live",
      workspace_root: "D:/workspaces/sox",
      workspace_name: "sox",
      implemented_views: ["operator", "pm"],
      default_view: "pm",
      readiness: { code: "action_required", explanation: "pending" },
      problem_codes: ["pending_execution"],
      current_portfolio_state: null,
      latest_pm_decision: null,
      latest_execution: null,
      execution_status: { code: "pending", explanation: "pending" },
      latest_thesis_revision: null,
      operator_status: { code: "ready", explanation: "present" },
    },
  ],
};

describe("OverviewPage", () => {
  beforeEach(() => {
    useOverviewQuery.mockReturnValue({
      isLoading: false,
      isError: false,
      data: overview,
    });
  });

  it("renders mounted targets and deep-links to each backend default view", () => {
    render(
      <MemoryRouter initialEntries={["/overview"]}>
        <OverviewPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "BTC" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "SOX" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open btc position view" })).toHaveAttribute(
      "href",
      "/targets/btc/position",
    );
    expect(screen.getByRole("link", { name: "Open sox pm view" })).toHaveAttribute(
      "href",
      "/targets/sox/pm",
    );
  });

  it("filters rows by explicit readiness and target text", () => {
    render(
      <MemoryRouter initialEntries={["/overview"]}>
        <OverviewPage />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Action required" }));
    expect(screen.queryByRole("heading", { name: "BTC" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "SOX" })).toBeInTheDocument();

    fireEvent.change(screen.getByRole("searchbox", { name: "Filter targets" }), {
      target: { value: "does-not-exist" },
    });
    expect(screen.getByText("No targets match the current filter.")).toBeInTheDocument();
  });
});
