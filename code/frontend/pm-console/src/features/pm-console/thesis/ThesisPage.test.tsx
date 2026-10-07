import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes, useSearchParams } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type {
  PMConsoleThesisRevisionDetail,
  PMConsoleThesisRevisionSummary,
} from "../../../types";
import { ThesisPage } from "./ThesisPage";
import {
  filterThesisRevisions,
  getTimelinePageCount,
  paginateThesisRevisions,
} from "./useThesisPageModel";

const modelState = vi.hoisted(() => ({ current: undefined as unknown }));
const translationMutation = vi.hoisted(() => ({ mutateAsync: vi.fn() }));

vi.mock("./useThesisPageModel", async () => {
  const actual = await vi.importActual<typeof import("./useThesisPageModel")>(
    "./useThesisPageModel",
  );
  return {
    ...actual,
    useThesisPageModel: () => {
      const [searchParams] = useSearchParams();
      const model = modelState.current as Record<string, unknown>;
      const compareRevisionId = searchParams.get("compare") ?? "";
      return {
        ...model,
        compareRevisionId,
        compareRevision: compareRevisionId ? model.compareRevision : null,
        compareSummary: compareRevisionId ? model.compareSummary : null,
        comparisonSections: compareRevisionId ? model.comparisonSections : [],
      };
    },
  };
});

vi.mock("../hooks", () => ({
  usePmConsoleThesisQuickBriefMutation: () => ({ mutateAsync: vi.fn() }),
  usePmConsoleThesisRevisionTranslationMutation: () => translationMutation,
}));

function summary(
  revisionId: string,
  businessAt: string,
  changedSectionCount: number,
  source: PMConsoleThesisRevisionSummary["source"] = "analysis_commit",
): PMConsoleThesisRevisionSummary {
  return {
    revision_id: revisionId,
    target_key: "btc",
    business_at: businessAt,
    committed_at: businessAt,
    source,
    source_event_count: 1,
    changed_claim_count: 0,
    changed_section_count: changedSectionCount,
    previous_revision_id: null,
    is_baseline: source === "migration_baseline",
  };
}

function revision(
  revisionId: string,
  businessAt: string,
  overrides: Partial<PMConsoleThesisRevisionDetail> = {},
): PMConsoleThesisRevisionDetail {
  return {
    revision_id: revisionId,
    target_key: "btc",
    business_at: businessAt,
    committed_at: businessAt,
    source: "analysis_commit",
    analysis_assessment_id: "assessment-long-id-1234567890",
    context_packet_id: "context-long-id-1234567890",
    context_packet_hash: "context-hash-long-id-1234567890",
    source_event_ids: ["event-long-id-1234567890"],
    research_memory_write_receipt_ids: ["receipt-long-id-1234567890"],
    changed_claim_ids: ["claim-changed-1234567890"],
    key_claim_ids: ["claim-key-1234567890"],
    contested_prior_claim_ids: [],
    price_level_role_ids: ["support-role-1"],
    price_levels: [
      {
        level_id: "price-level-long-id-1234567890",
        display_role: "support",
        role_if_flat: "support",
        value: 100,
        lower: null,
        upper: null,
        instrument_basis: "BTCUSDT",
      },
    ],
    previous_revision_id: "previous-revision-1234567890",
    canonical_bundle_sha256: "bundle-sha256-long-id-1234567890",
    is_baseline: false,
    sections: [
      {
        page_path: "thesis",
        page_name: "Thesis",
        section_name: "Summary",
        content_md: "# Current thesis\n\nThe current view.",
        content_sha256: "current-section-sha",
        changed_in_revision: true,
      },
    ],
    diffs_from_previous: [],
    linked_pm_decisions: [],
    ...overrides,
  };
}

function readyModel() {
  const selected = revision("selected-1", "2025-01-02T03:04:05Z");
  const compare = revision("base-1", "2025-01-01T03:04:05Z", {
    analysis_assessment_id: null,
    context_packet_id: null,
    context_packet_hash: null,
    source_event_ids: [],
    research_memory_write_receipt_ids: [],
    changed_claim_ids: [],
    key_claim_ids: [],
    price_level_role_ids: [],
    price_levels: [],
  });
  const other = summary("other-1", "2025-01-03T03:04:05Z", 4);
  const selectedSummary = summary("selected-1", selected.business_at, 2);
  const compareSummary = summary("base-1", compare.business_at, 1);
  return {
    state: "ready" as const,
    targetKey: "btc",
    list: {
      generated_at: "2025-01-03T03:05:00Z",
      target_key: "btc",
      revisions: [selectedSummary, compareSummary, other],
      latest_revision: selectedSummary,
    },
    detail: {
      generated_at: "2025-01-03T03:05:00Z",
      target_key: "btc",
      revision: selected,
    },
    orderedRevisions: [other, selectedSummary, compareSummary],
    filteredRevisions: [other, selectedSummary, compareSummary],
    filteredRevisionCount: 3,
    pagedRevisions: [other, selectedSummary, compareSummary],
    activePage: 1,
    pageCount: 1,
    pageSize: 6,
    selectedRevisionId: "selected-1",
    selectedSummary,
    compareRevisionId: "base-1",
    compareSummary,
    compareRevision: compare,
    compareRevisionNotFound: false,
    changedSections: selected.sections,
    changedDiffs: [],
    comparisonSections: [
      {
        sectionId: "thesis:Summary",
        pageName: "Thesis",
        sectionName: "Summary",
        beforeContent: "# Previous thesis",
        afterContent: "# Current thesis",
      },
    ],
  };
}

function SearchParamProbe() {
  const [searchParams] = useSearchParams();
  return (
    <output data-testid="search-params">{searchParams.toString()}</output>
  );
}

function renderThesis(initialEntry = "/targets/btc/thesis?revision=selected-1&compare=base-1") {
  modelState.current = readyModel();
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/targets/:target"
          element={
            <Outlet
              context={{
                contextQuery: {
                  data: {
                    thesis_translation: {
                      locale: "zh-CN",
                      available: true,
                      reason_code: null,
                      reason_message: null,
                    },
                  },
                },
              }}
            />
          }
        >
          <Route
            path="thesis"
            element={
              <>
                <ThesisPage />
                <SearchParamProbe />
              </>
            }
          />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Thesis Evolution refinement", () => {
  it("filters loaded revisions by date, source, and changed-section count and clamps pages", () => {
    const revisions = [
      summary("analysis-1", "2025-01-03T00:00:00Z", 4),
      summary("baseline-1", "2024-12-03T00:00:00Z", 0, "migration_baseline"),
      summary("analysis-2", "2025-02-03T00:00:00Z", 2),
    ];

    expect(filterThesisRevisions(revisions, "analysis 4")).toHaveLength(1);
    expect(filterThesisRevisions(revisions, "2024-12 baseline")).toHaveLength(1);
    const manyRevisions = Array.from({ length: 13 }, (_, index) =>
      summary(`analysis-${index}`, `2025-03-${String(index + 1).padStart(2, "0")}T00:00:00Z`, index),
    );
    expect(getTimelinePageCount(manyRevisions.length)).toBe(3);
    expect(paginateThesisRevisions(manyRevisions, 99)).toEqual([manyRevisions[12]]);
  });

  it("preserves compare URL state and keeps existing reader controls", () => {
    renderThesis();

    expect(screen.getByText("Selected vs compare base")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate brief" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export memo" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /2025-01-03/ }));
    expect(screen.getByTestId("search-params")).toHaveTextContent(
      "revision=other-1&compare=base-1",
    );

    cleanup();
    renderThesis("/targets/btc/thesis?revision=selected-1");
    expect(screen.getByRole("button", { name: "Translate to Chinese" })).toBeInTheDocument();
    expect(screen.queryByText("zh-CN Ready")).not.toBeInTheDocument();
    expect(screen.getByText("Translation available")).toBeInTheDocument();
  });

  it("advertises translated content only after a successful translation response", async () => {
    let resolveTranslation: (value: unknown) => void = () => undefined;
    translationMutation.mutateAsync.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveTranslation = resolve;
        }),
    );
    const translationPayload = {
      generated_at: "2025-01-03T03:06:00Z",
      target_key: "btc",
      revision_id: "selected-1",
      locale: "zh-CN",
      canonical_bundle_sha256: "bundle-sha256-long-id-1234567890",
      sections: [
        {
          section_id: "thesis:Summary",
          source_content_sha256: "current-section-sha",
          page_path: "thesis",
          page_name: "Thesis",
          section_name: "Summary",
          translated_content_md: "# 当前 thesis",
        },
      ],
      translator: {
        provider: "test-provider",
        model: "test-model",
        prompt_version: "test-prompt",
      },
      verification: { passed: true, failures: [] },
    };

    renderThesis("/targets/btc/thesis?revision=selected-1");
    expect(screen.queryByText("AI translation · session")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Translate to Chinese" }));

    expect(await screen.findByRole("button", { name: "Translating..." })).toBeDisabled();
    resolveTranslation(translationPayload);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "View Original" })).toBeInTheDocument();
    });
    expect(screen.getByText("AI translation · session")).toBeInTheDocument();
  });

  it("keeps audit identifiers accessible inside semantic trace groups", () => {
    renderThesis();

    expect(screen.getByText("assessment-long-id-1234567890")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy Analysis assessment ID" })).toBeInTheDocument();
    expect(screen.getByText("claim-changed-1234567890")).toBeInTheDocument();
    expect(screen.getByText("No PM decision is linked to this revision.")).toBeInTheDocument();
    expect(screen.getByText("Research memory write receipts")).toBeInTheDocument();
  });
});
