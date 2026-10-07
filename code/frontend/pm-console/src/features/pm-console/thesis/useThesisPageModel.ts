import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import type {
  PMConsoleMarkdownSection,
  PMConsoleThesisRevisionSummary,
} from "../../../types";
import {
  usePmConsoleThesisRevisionDetailQuery,
  usePmConsoleThesisRevisionsQuery,
} from "../hooks";

export const THESIS_TIMELINE_PAGE_SIZE = 6;

export function useThesisPageModel(targetKey: string, timelineQuery = "") {
  const [searchParams] = useSearchParams();
  const revisionsQuery = usePmConsoleThesisRevisionsQuery(targetKey);
  const requestedRevisionId = searchParams.get("revision")?.trim() ?? "";
  const requestedCompareRevisionId = searchParams.get("compare")?.trim() ?? "";
  const requestedPage = Number.parseInt(searchParams.get("page")?.trim() ?? "1", 10);

  const selectedRevisionId = useMemo(() => {
    if (requestedRevisionId.length > 0) {
      return requestedRevisionId;
    }
    return revisionsQuery.data?.latest_revision?.revision_id ?? "";
  }, [requestedRevisionId, revisionsQuery.data?.latest_revision?.revision_id]);

  const detailQuery = usePmConsoleThesisRevisionDetailQuery(
    targetKey,
    selectedRevisionId
  );
  const compareRevisionId =
    requestedCompareRevisionId.length > 0 &&
    requestedCompareRevisionId !== selectedRevisionId
      ? requestedCompareRevisionId
      : "";
  const compareDetailQuery = usePmConsoleThesisRevisionDetailQuery(
    targetKey,
    compareRevisionId
  );

  return useMemo(() => {
    if (!targetKey) {
      return { state: "missing_target" as const };
    }

    if (revisionsQuery.isLoading) {
      return { state: "loading" as const };
    }

    if (revisionsQuery.isError || !revisionsQuery.data) {
      return { state: "error" as const, targetKey };
    }

    if (revisionsQuery.data.revisions.length === 0) {
      return {
        state: "empty" as const,
        targetKey,
        data: revisionsQuery.data,
      };
    }

    if (detailQuery.isLoading || !detailQuery.data) {
      if (detailQuery.isError) {
        return {
          state: "revision_not_found" as const,
          targetKey,
          data: revisionsQuery.data,
          selectedRevisionId,
        };
      }
      return {
        state: "loading_detail" as const,
        targetKey,
        data: revisionsQuery.data,
        selectedRevisionId,
      };
    }

    const orderedRevisions = revisionsQuery.data.revisions.slice().reverse();
    const filteredRevisions = filterThesisRevisions(orderedRevisions, timelineQuery);
    const pageCount = getTimelinePageCount(filteredRevisions.length);
    const activePage =
      Number.isFinite(requestedPage) && requestedPage >= 1
        ? Math.min(requestedPage, pageCount)
        : 1;
    const pagedRevisions = paginateThesisRevisions(filteredRevisions, activePage);

    const selectedSummary =
      orderedRevisions.find(
        (revision) => revision.revision_id === detailQuery.data.revision.revision_id
      ) ?? null;
    const compareSummary =
      compareRevisionId.length === 0
        ? null
        : orderedRevisions.find(
            (revision) => revision.revision_id === compareRevisionId
          ) ?? null;
    const compareRevision =
      compareRevisionId.length === 0 ? null : compareDetailQuery.data?.revision ?? null;
    const compareRevisionNotFound =
      compareRevisionId.length > 0 && compareDetailQuery.isError;
    const changedSections = detailQuery.data.revision.sections.filter(
      (section) => section.changed_in_revision
    );
    const changedDiffs = detailQuery.data.revision.diffs_from_previous.filter(
      (diff) => diff.changed
    );
    const comparisonSections =
      compareRevision === null
        ? []
        : buildComparisonSections(
            compareRevision.sections,
            detailQuery.data.revision.sections
          );

    return {
      state: "ready" as const,
      targetKey,
      list: revisionsQuery.data,
      detail: detailQuery.data,
      orderedRevisions,
      filteredRevisions,
      filteredRevisionCount: filteredRevisions.length,
      pagedRevisions,
      activePage,
      pageCount,
      pageSize: THESIS_TIMELINE_PAGE_SIZE,
      selectedRevisionId,
      selectedSummary,
      compareRevisionId,
      compareSummary,
      compareRevision,
      compareRevisionNotFound,
      changedSections,
      changedDiffs,
      comparisonSections,
    };
  }, [
    compareDetailQuery.data,
    compareDetailQuery.isError,
    compareRevisionId,
    detailQuery.data,
    detailQuery.isError,
    detailQuery.isLoading,
    requestedPage,
    revisionsQuery.data,
    revisionsQuery.isError,
    revisionsQuery.isLoading,
    selectedRevisionId,
    timelineQuery,
    targetKey,
  ]);
}

export function filterThesisRevisions(
  revisions: PMConsoleThesisRevisionSummary[],
  query: string,
) {
  const terms = query
    .trim()
    .toLocaleLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  if (terms.length === 0) {
    return revisions;
  }

  return revisions.filter((revision) => {
    const searchableFields = [
      revision.business_at,
      revision.committed_at,
      revision.source,
      revision.source === "migration_baseline"
        ? "baseline migration"
        : "analysis commit",
      String(revision.changed_section_count),
      `${revision.changed_section_count} changed sections`,
      revision.is_baseline ? "baseline snapshot" : "revision",
    ].map((value) => value.toLocaleLowerCase());
    return terms.every((term) =>
      searchableFields.some((field) => field.includes(term)),
    );
  });
}

export function getTimelinePageCount(revisionCount: number) {
  return Math.max(1, Math.ceil(revisionCount / THESIS_TIMELINE_PAGE_SIZE));
}

export function paginateThesisRevisions(
  revisions: PMConsoleThesisRevisionSummary[],
  page: number,
) {
  const activePage = Math.min(
    Math.max(Number.isFinite(page) && page >= 1 ? page : 1, 1),
    getTimelinePageCount(revisions.length),
  );
  return revisions.slice(
    (activePage - 1) * THESIS_TIMELINE_PAGE_SIZE,
    activePage * THESIS_TIMELINE_PAGE_SIZE,
  );
}

function buildComparisonSections(
  compareSections: PMConsoleMarkdownSection[],
  selectedSections: PMConsoleMarkdownSection[]
): Array<{
  sectionId: string;
  pageName: string;
  sectionName: string;
  beforeContent: string;
  afterContent: string;
}> {
  const compareBySectionId = new Map<string, PMConsoleMarkdownSection>(
    compareSections.map((section) => [
      `${section.page_path}:${section.section_name}`,
      section,
    ])
  );

  return selectedSections
    .map((selectedSection) => {
      const sectionId = `${selectedSection.page_path}:${selectedSection.section_name}`;
      const compareSection = compareBySectionId.get(sectionId);
      if (!compareSection) {
        return null;
      }
      if (compareSection.content_sha256 === selectedSection.content_sha256) {
        return null;
      }
      return {
        sectionId,
        pageName: selectedSection.page_name,
        sectionName: selectedSection.section_name,
        beforeContent: compareSection.content_md,
        afterContent: selectedSection.content_md,
      };
    })
    .filter((section) => section !== null);
}
