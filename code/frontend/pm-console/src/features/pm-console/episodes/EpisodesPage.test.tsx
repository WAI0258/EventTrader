// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  PMConsoleEpisodeDetailResponse,
  PMConsoleEpisodesResponse,
} from "../../../types";
import { EpisodesPage } from "./EpisodesPage";

const state = vi.hoisted(() => ({
  list: undefined as PMConsoleEpisodesResponse | undefined,
  detail: undefined as PMConsoleEpisodeDetailResponse | undefined,
  filters: undefined as
    | {
        offset?: number;
        limit?: number;
        phase?: string;
        query?: string | null;
        anchorEpisodeId?: string | null;
      }
    | undefined,
}));

vi.mock("../hooks", () => ({
  usePmConsoleEpisodesQuery: (_target: string, filters: typeof state.filters) => {
    state.filters = filters;
    return { isLoading: false, isError: false, data: state.list };
  },
  usePmConsoleEpisodeDetailQuery: () => ({
    isLoading: false,
    isError: false,
    data: state.detail,
  }),
}));

function LocationProbe() {
  return <output data-testid="location">{useLocation().search}</output>;
}

function renderPage(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe />
      <Routes>
        <Route path="/targets/:target/episodes" element={<EpisodesPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  const summary = {
    episode_id: "btc:episode:4",
    target_key: "btc",
    phase: "completed" as const,
    direction: "long" as const,
    opened_at: "2026-08-20T10:00:00Z",
    closed_at: "2026-08-21T10:00:00Z",
    close_reason: "state_changed_to_flat",
    segment_count: 1,
    outcome_status: { code: "available", explanation: "Execution-adjusted return." },
    strategy_return: 0.052,
    reflection_status: { code: "recorded", explanation: "Learning recorded." },
  };
  state.list = {
    generated_at: "2026-08-22T10:00:00Z",
    target_key: "btc",
    total: 1,
    offset: 0,
    limit: 6,
    episodes: [summary],
  };
  state.detail = {
    generated_at: "2026-08-22T10:00:00Z",
    target_key: "btc",
    episode: {
      summary,
      segments: [
        {
          segment_id: "btc:segment:4",
          target_weight: 0.5,
          opened_at: summary.opened_at,
          closed_at: summary.closed_at,
          close_reason: "view_closed",
          strategy_return: 0.052,
          outcome_status: summary.outcome_status,
        },
      ],
      transitions: [
        {
          state_change_id: "entry",
          occurred_at: summary.opened_at,
          kind: "entry",
          state: "weak_long",
          previous_target_weight: 0,
          target_weight: 0.5,
          pm_decision_id: "pm-1",
          execution_record_id: "execution-1",
          adjusted_price: 101.25,
          raw_price: 101.2,
          total_cost_bps: 1.5,
          price_basis: "open",
          rationale_md: "Entry rationale.",
        },
        {
          state_change_id: "exit",
          occurred_at: summary.closed_at,
          kind: "exit",
          state: "flat",
          previous_target_weight: 0.5,
          target_weight: 0,
          pm_decision_id: "pm-2",
          execution_record_id: "execution-2",
          adjusted_price: 106.55,
          raw_price: 106.5,
          total_cost_bps: 1.5,
          price_basis: "open",
          rationale_md: "Exit rationale.",
        },
      ],
      reflection: {
        status: summary.reflection_status,
        obligation_count: 1,
        learning_recorded: true,
      },
    },
  };
  state.filters = undefined;
});

describe("Position Episodes presentation", () => {
  it("defaults completed episodes to the exit reader and keeps transition selection in the URL", () => {
    renderPage("/targets/btc/episodes?episode=btc:episode:4");

    expect(screen.getByText("BTC Episodes", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Lifecycle outcome", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Execution price trail", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("101.25", { exact: true })).toBeInTheDocument();
    expect(screen.getAllByText("106.55", { exact: true })).toHaveLength(2);
    expect(screen.getByRole("heading", { name: "Exit rationale" })).toBeInTheDocument();
    expect(screen.queryByText("Decision Queue", { exact: true })).not.toBeInTheDocument();
    expect(state.filters).toMatchObject({
      offset: 0,
      limit: 6,
      phase: "completed",
      anchorEpisodeId: "btc:episode:4",
    });

    fireEvent.click(screen.getByRole("button", { name: /Entry.*101\.25/i }));

    expect(screen.getByRole("heading", { name: "Entry rationale" })).toBeInTheDocument();
    expect(screen.getByText("Raw execution price", { exact: true })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("transition=entry");
  });

  it("does not substitute a market price when the execution price is absent", () => {
    state.detail!.episode.transitions[0].adjusted_price = null;

    renderPage("/targets/btc/episodes?episode=btc:episode:4&transition=entry");

    expect(screen.getAllByText("Not recorded", { exact: true })).toHaveLength(2);
  });

  it("uses the server page offset and clears cross-page selection when paging", () => {
    state.list = {
      ...state.list!,
      total: 7,
      offset: 6,
      limit: 6,
    };

    renderPage("/targets/btc/episodes?page=2");

    expect(screen.getAllByText("Page 2 of 2").length).toBeGreaterThan(0);
    expect(state.filters).toMatchObject({ offset: 6, limit: 6 });

    fireEvent.click(screen.getByRole("button", { name: "Previous" }));

    expect(screen.getByTestId("location")).not.toHaveTextContent("page=");
    expect(screen.getByTestId("location")).not.toHaveTextContent("episode=");
  });
});
