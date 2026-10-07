// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  PMConsolePMDecisionDetail,
  PMConsolePMDecisionDetailResponse,
  PMConsolePMDecisionSummary,
  PMConsolePMDecisionsResponse,
} from "../../../types";
import { PMEvolutionPage } from "./PMEvolutionPage";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const pmState = vi.hoisted(() => ({
  list: undefined as PMConsolePMDecisionsResponse | undefined,
  details: {} as Record<string, PMConsolePMDecisionDetailResponse>,
}));

vi.mock("../hooks", () => ({
  usePmConsolePmDecisionsQuery: () => ({
    isLoading: false,
    isError: false,
    data: pmState.list,
  }),
  usePmConsolePmDecisionDetailQuery: (_target: string, decisionId: string) => ({
    isLoading: false,
    isError: !pmState.details[decisionId],
    data: pmState.details[decisionId],
  }),
}));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.search}</output>;
}

function decisionSummary(index: number): PMConsolePMDecisionSummary {
  return {
    decision_id: `decision-${index}`,
    decision_episode_id: `episode-${index}`,
    target_key: "btc",
    business_at: `2025-01-${String(index).padStart(2, "0")}T03:04:05Z`,
    decision_available_at: `2025-01-${String(index).padStart(2, "0")}T03:04:05Z`,
    actual_state_before_decision: "flat",
    actual_target_weight_before_decision: 0,
    requested_state: "long",
    requested_target_weight: 0.8,
    execution_required: index % 2 === 0,
    outcome_status: index % 2 === 0 ? "executed" : "no_op",
    pm_review_request_id: `review-${index}`,
    review_reasons: ["new evidence"],
    analysis_conditional_state: "flat",
    analysis_conditional_target_weight: 0.42,
  };
}

function decisionDetail(summary: PMConsolePMDecisionSummary): PMConsolePMDecisionDetailResponse {
  const detail: PMConsolePMDecisionDetail = {
    summary,
    review_source: "pm-review",
    review_reasons: summary.review_reasons,
    rationale_md: `Persisted rationale for ${summary.decision_id}.`,
    fallback_state_if_clamped: null,
    fallback_rationale_md: null,
    source_event_ids: [`event-${summary.decision_id}`],
    review_request: {
      reference_id: summary.pm_review_request_id,
      status: { code: "available", explanation: "PM review is linked." },
      href: null,
    },
    execution: summary.execution_required
      ? {
          execution_record_id: `execution-${summary.decision_id}`,
          business_at: summary.business_at,
          status: "executed",
          pm_decision_id: summary.decision_id,
          requested_target_weight: summary.requested_target_weight,
          executed_at: summary.business_at,
          target_weight: summary.requested_target_weight,
          adjusted_price: 101.25,
          rejection_reason: null,
        }
      : null,
    execution_status: {
      code: summary.execution_required ? "executed" : "no_op",
      explanation: summary.execution_required
        ? "Execution is linked."
        : "No execution was required.",
    },
    analysis: {
      reference_id: `assessment-${summary.decision_id}`,
      status: { code: "available", explanation: "Analysis is linked." },
      href: null,
    },
    analysis_assessment: {
      assessment_id: `assessment-${summary.decision_id}`,
      business_at: summary.business_at,
      as_if_flat_state: "flat",
      target_weight: 0.42,
      as_if_flat_rationale_md: "Exposure-blind AnalysisAssessment rationale.",
      source_event_ids: [`analysis-event-${summary.decision_id}`],
      thesis_anchor: {
        reference_id: `revision-${summary.decision_id}`,
        status: { code: "available", explanation: "Thesis is linked." },
        href: `/targets/btc/thesis?revision=revision-${summary.decision_id}`,
      },
    },
    thesis_revision: {
      reference_id: `revision-${summary.decision_id}`,
      status: { code: "available", explanation: "Thesis is linked." },
      href: `/targets/btc/thesis?revision=revision-${summary.decision_id}`,
    },
  };

  return {
    generated_at: "2025-01-20T03:05:00Z",
    target_key: "btc",
    decision: detail,
  };
}

function renderPage(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <LocationProbe />
      <Routes>
        <Route path="/targets/:target/pm" element={<PMEvolutionPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  const decisions = Array.from({ length: 13 }, (_, index) => decisionSummary(index + 1));
  pmState.list = {
    generated_at: "2025-01-20T03:05:00Z",
    target_key: "btc",
    decisions,
  };
  pmState.details = Object.fromEntries(
    decisions.map((summary) => [summary.decision_id, decisionDetail(summary)]),
  );
});

describe("PM Trajectory presentation", () => {
  it("keeps the deep-linked decision selected and preserves decision and page params", () => {
    renderPage("/targets/btc/pm?decision=decision-7&page=2");

    expect(screen.getByText("PM Trajectory", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Persisted rationale for decision-7.", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Page 2 of 3", { exact: true })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.getByTestId("location")).toHaveTextContent("decision=decision-7&page=3");
  });

  it("keeps the selected decision visible when selection changes on a later page", () => {
    renderPage("/targets/btc/pm?page=2");

    fireEvent.click(screen.getByRole("button", { name: /Jan 7, 2025, 11:04/ }));

    expect(screen.getByTestId("location")).toHaveTextContent("decision=decision-7");
    expect(screen.getByTestId("location")).toHaveTextContent("page=2");
    expect(screen.getByText("Persisted rationale for decision-7.", { exact: true })).toBeInTheDocument();
  });

  it("uses the latest available decision when a stale decision id is supplied", () => {
    renderPage("/targets/btc/pm?decision=missing-decision");

    expect(screen.getByText("Persisted rationale for decision-13.", { exact: true })).toBeInTheDocument();
    expect(screen.queryByText("Persisted rationale for missing-decision.", { exact: true })).not.toBeInTheDocument();
  });
});
