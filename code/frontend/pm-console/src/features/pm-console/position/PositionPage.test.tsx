import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useSearchParams } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  PMConsoleAnalysisAssessment,
  PMConsoleLine,
  PMConsoleMarker,
  PMConsolePMDecisionDetailResponse,
  PMConsolePositionResponse,
} from "../../../types";
import { PositionPage } from "./PositionPage";

const chartMock = vi.hoisted(() => {
  const makeSeries = () => ({
    setData: vi.fn(),
    setMarkers: vi.fn(),
    priceToCoordinate: vi.fn((value: number) => value),
  });

  return {
    createChart: vi.fn(() => ({
      addLineSeries: vi.fn(() => makeSeries()),
      timeScale: vi.fn(() => ({ fitContent: vi.fn() })),
      subscribeCrosshairMove: vi.fn(),
      unsubscribeCrosshairMove: vi.fn(),
      subscribeClick: vi.fn(),
      unsubscribeClick: vi.fn(),
      applyOptions: vi.fn(),
      remove: vi.fn(),
    })),
  };
});

vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  LineStyle: { Dashed: 2 },
  TickMarkType: {
    Year: 0,
    Month: 1,
    DayOfMonth: 2,
    Time: 3,
    TimeWithSeconds: 4,
  },
  createChart: chartMock.createChart,
}));

const positionModel = vi.hoisted(() => ({ current: undefined as unknown }));
const detailQueries = vi.hoisted(() => ({
  decision: undefined as unknown,
  analysis: undefined as unknown,
}));

vi.mock("./usePositionPageModel", () => ({
  usePositionPageModel: () => positionModel.current,
}));

vi.mock("../hooks", () => ({
  usePmConsolePmDecisionDetailQuery: () => detailQueries.decision,
  usePmConsoleAnalysisAssessmentQuery: () => detailQueries.analysis,
}));

function SearchParamProbe() {
  const [searchParams] = useSearchParams();
  return <output data-testid="selected-marker">{searchParams.get("marker") ?? "none"}</output>;
}

const readyStatus = { code: "ready", explanation: "Persisted and available." };

function line(
  key: PMConsoleLine["key"],
  label: string,
  status = readyStatus,
): PMConsoleLine {
  return {
    key,
    label,
    role: key === "pm_pipeline" ? "actual" : key === "buy_hold" ? "baseline" : "analysis",
    status,
    explanation: status.explanation,
  };
}

function marker(
  markerId: string,
  kind: PMConsoleMarker["kind"],
): PMConsoleMarker {
  return {
    marker_id: markerId,
    kind,
    lane: kind === "pm_decision" ? "pm" : "analysis",
    label: kind === "pm_decision" ? "PM decision" : "Analysis assessment",
    time: "2025-01-02T03:04:05Z",
    business_at: "2025-01-02T03:04:05Z",
    state: kind === "pm_decision" ? "long" : "flat",
    target_weight: kind === "pm_decision" ? 0.8 : 0.42,
    price: 101.25,
    line_value: 1.02,
    shape: "circle",
    position: "aboveBar",
    text: kind === "pm_decision" ? "PM" : "AN",
    source_id: markerId,
    pm_decision_id: kind === "pm_decision" ? "decision-1" : null,
    execution_record_id: kind === "pm_decision" ? "execution-1" : null,
    analysis_assessment_id: kind === "analysis_assessment" ? "assessment-1" : null,
  };
}

function positionData(overrides: Partial<PMConsolePositionResponse> = {}): PMConsolePositionResponse {
  return {
    target_key: "btc",
    generated_at: "2025-01-02T03:05:00Z",
    current_portfolio_state: {
      status: readyStatus,
      state: "long",
      target_weight: 0.8,
      updated_at: "2025-01-02T03:04:05Z",
      source_pm_decision_id: "decision-1",
      source_execution_record_id: "execution-1",
      decision_episode_id: "episode-1",
    },
    market_data_status: readyStatus,
    comparison_status: readyStatus,
    market_symbol: "BTC",
    price_display_decimals: 2,
    bar_granularity: "1h",
    market_data_path: "workspace/market/btc.csv",
    base_value: 1,
    lines: [
      line("pm_pipeline", "PM pipeline / PortfolioState"),
      line("buy_hold", "Buy & Hold BTC"),
      line("analysis_direct", "Analysis Direct / Assessment mapping"),
    ],
    points: [
      {
        time: "2025-01-02T03:00:00Z",
        price: 100,
        pm_pipeline_value: 1,
        buy_hold_value: 1,
        analysis_direct_value: 1,
        pm_target_weight: 0.8,
        analysis_direct_target_weight: 0.42,
        pm_state: "long",
        analysis_direct_state: "flat",
      },
    ],
    markers: [marker("pm-1", "pm_decision"), marker("an-1", "analysis_assessment")],
    latest_pm_decision: {
      decision_id: "decision-1",
      business_at: "2025-01-02T03:04:05Z",
      requested_state: "long",
      requested_target_weight: 0.8,
      execution_required: true,
      pm_review_request_id: "review-1",
    },
    latest_execution: {
      execution_record_id: "execution-1",
      business_at: "2025-01-02T03:04:05Z",
      status: "executed",
      pm_decision_id: "decision-1",
      requested_target_weight: 0.8,
      executed_at: "2025-01-02T03:04:06Z",
      target_weight: 0.8,
      adjusted_price: 101.25,
      rejection_reason: null,
    },
    notes: [],
    audit: null,
    ...overrides,
  };
}

function decisionDetail(adjustedPrice: number | null): PMConsolePMDecisionDetailResponse {
  return {
    generated_at: "2025-01-02T03:05:00Z",
    target_key: "btc",
    decision: {
      summary: {
        decision_id: "decision-1",
        decision_episode_id: "episode-1",
        target_key: "btc",
        business_at: "2025-01-02T03:04:05Z",
        decision_available_at: "2025-01-02T03:04:05Z",
        actual_state_before_decision: "flat",
        actual_target_weight_before_decision: 0,
        requested_state: "long",
        requested_target_weight: 0.8,
        execution_required: true,
        outcome_status: "executed",
        pm_review_request_id: "review-1",
        review_reasons: ["new evidence"],
        analysis_conditional_state: "flat",
        analysis_conditional_target_weight: 0.42,
      },
      review_source: "pm-review",
      review_reasons: ["new evidence"],
      rationale_md: "PM rationale with execution context.",
      fallback_state_if_clamped: null,
      fallback_rationale_md: null,
      source_event_ids: ["event-1"],
      review_request: { reference_id: "review-1", status: readyStatus, href: null },
      execution: {
        execution_record_id: "execution-1",
        business_at: "2025-01-02T03:04:05Z",
        status: "executed",
        pm_decision_id: "decision-1",
        requested_target_weight: 0.8,
        executed_at: "2025-01-02T03:04:06Z",
        target_weight: 0.8,
        adjusted_price: adjustedPrice,
        rejection_reason: null,
      },
      execution_status: { code: "executed", explanation: "Execution is persisted." },
      analysis: { reference_id: "assessment-1", status: readyStatus, href: null },
      analysis_assessment: null,
      thesis_revision: {
        reference_id: "revision-1",
        status: { code: "effective_thesis_anchor", explanation: "Thesis is linked." },
        href: "/targets/btc/thesis?revision=revision-1",
      },
    },
  };
}

const analysisDetail: PMConsoleAnalysisAssessment = {
  assessment_id: "assessment-1",
  business_at: "2025-01-02T03:04:05Z",
  as_if_flat_state: "flat",
  target_weight: 0.42,
  as_if_flat_rationale_md: "Canonical analysis rationale.",
  source_event_ids: ["analysis-event-1"],
  thesis_anchor: {
    reference_id: "revision-1",
    status: { code: "direct_thesis_anchor", explanation: "Direct Thesis anchor." },
    href: "/targets/btc/thesis?revision=revision-1",
  },
};

function setReadyState(data = positionData()) {
  positionModel.current = {
    state: "ready",
    data,
    pmLine: data.lines.find((item) => item.key === "pm_pipeline"),
    buyHoldLine: data.lines.find((item) => item.key === "buy_hold"),
    analysisDirectLine: data.lines.find((item) => item.key === "analysis_direct"),
    pmReturn: 0.02,
    buyHoldReturn: 0.02,
    analysisDirectReturn: 0.02,
    pmMaxDrawdown: 0,
    interpretation: "Long at 80%.",
  };
}

function renderPosition(initialEntry = "/targets/btc/position?marker=pm-1") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <SearchParamProbe />
      <Routes>
        <Route path="/targets/:target/position" element={<PositionPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  setReadyState();
  detailQueries.decision = {
    isLoading: false,
    isError: false,
    data: decisionDetail(101.25),
  };
  detailQueries.analysis = {
    isLoading: false,
    isError: false,
    data: analysisDetail,
  };
});

describe("Position Management presentation", () => {
  it("renders exposure and all performance measures as peer summary items", () => {
    renderPosition("/targets/btc/position");

    const summary = screen.getByRole("region", { name: "Position summary" });
    expect(summary.querySelectorAll(":scope > article")).toHaveLength(5);
    expect(summary.querySelector(".position-performance-metrics")).toBeNull();
    expect(screen.getByText("Execution path", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Assessment mapping", { exact: true })).toBeInTheDocument();
  });

  it("does not render the aggregate comparison status", () => {
    renderPosition("/targets/btc/position");

    expect(screen.queryByText("Comparison", { exact: true })).not.toBeInTheDocument();
  });

  it("keeps the unselected chart state in-flow without an inspection drawer", () => {
    renderPosition("/targets/btc/position");

    const workspace = screen.getByRole("region", {
      name: "Position chart and selected decision",
    });
    expect(screen.getByText(/Select a PM or Analysis marker to inspect/i)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Selected decision detail" })).toBeNull();
    expect(workspace.querySelector(".position-inspection-drawer")).toBeNull();
  });

  it("renders selected marker detail as a full-width in-flow inspection drawer", () => {
    renderPosition();

    const drawer = screen.getByRole("region", { name: "Selected decision detail" });
    expect(drawer).toHaveClass("position-inspection-drawer");
    expect(drawer.parentElement).toHaveClass("position-workspace");
    expect(drawer.previousElementSibling).toHaveClass("position-chart-card");
    expect(screen.getByText("PM rationale with execution context.", { exact: true })).toBeInTheDocument();
  });

  it("allows the full retained horizon to fit at normal chart widths", () => {
    renderPosition();

    const lastCreateChartCall = chartMock.createChart.mock.calls.at(-1) as
      | [unknown, unknown]
      | undefined;
    expect(lastCreateChartCall?.[1]).toMatchObject({
      timeScale: { minBarSpacing: 0.1 },
    });
  });

  it.each([
    [101.25, "101.25"],
    [null, "Not recorded"],
  ])("shows the execution-adjusted price as %s", (adjustedPrice, expected) => {
    detailQueries.decision = {
      isLoading: false,
      isError: false,
      data: decisionDetail(adjustedPrice),
    };

    renderPosition();

    expect(screen.getByText(expected, { exact: true })).toBeInTheDocument();
  });

  it("keeps the canonical Analysis Direct state, weight, and Thesis link visible", () => {
    renderPosition("/targets/btc/position?marker=an-1");

    expect(screen.getByRole("heading", { name: /flat 42%/i })).toBeInTheDocument();
    expect(screen.getByText("Canonical analysis rationale.", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("Analysis Direct; not execution", { exact: true })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open linked Thesis revision" })).toHaveAttribute(
      "href",
      "/targets/btc/thesis?revision=revision-1",
    );
  });

  it("retains the selected marker in the URL and allows choosing another marker at the same timestamp", () => {
    renderPosition();

    expect(screen.getByTestId("selected-marker")).toHaveTextContent("pm-1");
    expect(screen.getByText("PM rationale with execution context.", { exact: true })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Select Analysis marker/ }));

    expect(screen.getByTestId("selected-marker")).toHaveTextContent("an-1");
    expect(screen.getByText("Canonical analysis rationale.", { exact: true })).toBeInTheDocument();
  });

  it("keeps missing and partial data explicit in the supporting state", () => {
    const partialStatus = {
      code: "partial_market_bars",
      explanation: "Some market bars are unavailable in the requested window.",
    };
    const missingPortfolioStatus = {
      code: "missing_portfolio_state",
      explanation: "No persisted PortfolioState is available.",
    };
    const data = positionData({
      current_portfolio_state: {
        status: missingPortfolioStatus,
        state: null,
        target_weight: null,
        updated_at: null,
        source_pm_decision_id: null,
        source_execution_record_id: null,
        decision_episode_id: null,
      },
      market_data_status: partialStatus,
      comparison_status: partialStatus,
      market_data_path: null,
      lines: [
        line("pm_pipeline", "PM pipeline / PortfolioState", missingPortfolioStatus),
        line("buy_hold", "Buy & Hold BTC", partialStatus),
        line("analysis_direct", "Analysis Direct / Assessment mapping", partialStatus),
      ],
      points: [],
      markers: [],
      notes: ["Market bars are incomplete for the requested analysis window."],
    });
    setReadyState(data);

    renderPosition("/targets/btc/position");

    expect(screen.getAllByText("partial_market_bars").length).toBeGreaterThan(0);
    expect(screen.getAllByText("missing_portfolio_state").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Some market bars are unavailable in the requested window.").length).toBeGreaterThan(0);
    expect(
      screen.queryByText("Market bars are incomplete for the requested analysis window."),
    ).not.toBeInTheDocument();
    expect(screen.getByText("No chart points are available for this target.")).toBeInTheDocument();
  });
});
