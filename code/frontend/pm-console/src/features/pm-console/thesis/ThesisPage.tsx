import { useEffect, useState, type ReactNode } from "react";
import { Link, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { PMConsoleApiError } from "../../../api";
import { MarkdownContent } from "../../../components/MarkdownContent";
import type {
  PMConsoleThesisPriceLevel,
  PMConsoleThesisRevisionDetail,
  PMConsoleThesisRevisionSummary,
  PMConsoleTranslatedSection,
  PMConsoleThesisTranslationAvailability,
  PMConsoleThesisTranslationFailure,
  PMConsoleThesisTranslationResponse,
  PMConsoleThesisQuickBriefFailure,
} from "../../../types";
import {
  usePmConsoleThesisQuickBriefMutation,
  usePmConsoleThesisRevisionTranslationMutation,
} from "../hooks";
import type { PmConsoleOutletContext } from "../layout/PmConsoleLayout";
import { useLocale } from "../locale/LocaleProvider";
import { useThesisPageModel } from "./useThesisPageModel";

type ThesisRevisionTranslationState =
  | { status: "loading" }
  | {
      status: "success";
      translatedSectionsBySectionId: Record<
        string,
        {
          translatedContentMd: string;
        }
      >;
      translator: { provider: string; model: string; prompt_version: string };
      generatedAt: string;
    }
  | {
      status: "failure";
      errorCode: string;
      errorMessage: string;
      verificationFailures: string[];
    };

type ThesisQuickBriefState =
  | { status: "generating" }
  | { status: "ready"; briefMd: string; provider: string; model: string; promptVersion: string; generatedAt: string }
  | { status: "failed" | "unavailable"; message: string };

export function ThesisPage() {
  const { t } = useLocale();
  const { target } = useParams();
  const targetKey = target ?? "";
  const { contextQuery } = useOutletContext<PmConsoleOutletContext>();
  const [searchParams, setSearchParams] = useSearchParams();
  const [translationViewRevisionKey, setTranslationViewRevisionKey] = useState<
    string | null
  >(null);
  const [revisionTranslations, setRevisionTranslations] = useState<
    Record<string, ThesisRevisionTranslationState>
  >({});
  const [quickBriefs, setQuickBriefs] = useState<
    Record<string, ThesisQuickBriefState>
  >({});
  const [timelineQuery, setTimelineQuery] = useState("");
  const model = useThesisPageModel(targetKey, timelineQuery);
  const requestedTimelinePage = Number.parseInt(
    searchParams.get("page")?.trim() ?? "1",
    10,
  );
  const quickBriefRevision = model.state === "ready" ? model.detail.revision : null;
  const translationMutation = usePmConsoleThesisRevisionTranslationMutation(
    targetKey,
    model.state === "ready" ? model.detail.revision.revision_id : ""
  );
  const quickBriefMutation = usePmConsoleThesisQuickBriefMutation(
    targetKey,
    quickBriefRevision?.revision_id ?? ""
  );
  const quickBriefKey = quickBriefRevision
    ? buildTranslationRevisionKey(
        quickBriefRevision.revision_id,
        quickBriefRevision.canonical_bundle_sha256
      )
    : null;

  useEffect(() => {
    if (model.state !== "ready") {
      return;
    }
    if (
      !Number.isFinite(requestedTimelinePage) ||
      requestedTimelinePage !== model.activePage
    ) {
      const next = new URLSearchParams(searchParams);
      next.set("page", String(model.activePage));
      setSearchParams(next, { replace: true });
    }
  }, [
    model.state,
    model.state === "ready" ? model.activePage : null,
    requestedTimelinePage,
    searchParams,
    setSearchParams,
  ]);


  if (model.state === "missing_target") {
    return <div className="route-empty">{t("No target selected.")}</div>;
  }

  if (model.state === "loading") {
    return <div className="route-empty">{t("Loading thesis evolution...")}</div>;
  }

  if (model.state === "error") {
    return (
      <div className="route-empty">
        <p>{t("Thesis revisions could not be loaded for")} `{targetKey}`.</p>
      </div>
    );
  }

  if (model.state === "empty") {
    return (
      <div className="page">
        <section className="hero-card">
          <div className="hero-main">
            <p className="eyebrow">{t("Thesis Evolution")}</p>
            <h2>{targetKey.toUpperCase()}</h2>
            <p className="hero-copy">
              {t("No thesis revisions have been persisted for this target yet.")}
            </p>
          </div>
          <div className="hero-status-panel">
            <div className="hero-statuses">
              <div className="metric-badge">
                <span>{t("Latest Revision")}</span>
                <strong>{t("None")}</strong>
              </div>
              <div className="metric-badge">
                <span>{t("Revision Count")}</span>
                <strong>0</strong>
              </div>
              <div className="metric-badge">
                <span>{t("State")}</span>
                <strong>{t("Empty workspace state")}</strong>
              </div>
            </div>
          </div>
        </section>
      </div>
    );
  }

  if (model.state === "loading_detail") {
    return <div className="route-empty">{t("Loading selected thesis revision...")}</div>;
  }

  if (model.state === "revision_not_found") {
    return (
      <div className="page">
        <section className="hero-card">
          <div className="hero-main">
            <p className="eyebrow">{t("Thesis Evolution")}</p>
            <h2>{targetKey.toUpperCase()}</h2>
            <p className="hero-copy">
              {t("Revision")} `{model.selectedRevisionId}` {t("was not found for this target.")}
            </p>
          </div>
        </section>
      </div>
    );
  }

  const revision = model.detail.revision;
  const quickBriefState = quickBriefKey ? quickBriefs[quickBriefKey] ?? null : null;
  const compareMode = model.compareRevision !== null;
  const compareSource = model.compareRevision ?? null;
  const compareSections = model.comparisonSections;
  const requestedSectionPage = Number.parseInt(
    searchParams.get("section")?.trim() ?? "1",
    10
  );
  const readerSections = compareMode
    ? compareSections.map((section) => ({
        sectionId: section.sectionId,
        pageName: section.pageName,
        sectionName: section.sectionName,
        content: section.afterContent,
        contentSha256: null,
      }))
    : revision.sections
        .filter((section) => section.content_md.trim().length > 0)
        .map((section) => ({
          sectionId: `${section.page_path}:${section.section_name}`,
          pageName: section.page_name,
          sectionName: section.section_name,
          content: section.content_md,
          contentSha256: section.content_sha256,
        }));
  const sectionPageCount = Math.max(1, readerSections.length);
  const activeSectionPage =
    Number.isFinite(requestedSectionPage) && requestedSectionPage >= 1
      ? Math.min(requestedSectionPage, sectionPageCount)
      : 1;
  const activeSectionIndex = activeSectionPage - 1;
  const activeReaderSection = readerSections[activeSectionIndex] ?? null;
  const activeCompareSection = compareSections[activeSectionIndex] ?? null;
  const activeTranslationRevisionKey =
    compareMode || activeReaderSection === null
      ? null
      : buildTranslationRevisionKey(
          revision.revision_id,
          revision.canonical_bundle_sha256
        );
  const activeTranslationState =
    activeTranslationRevisionKey === null
      ? null
      : revisionTranslations[activeTranslationRevisionKey] ?? null;
  const translationAvailability =
    compareMode ? null : contextQuery.data?.thesis_translation ?? null;
  const translationUnavailable =
    translationAvailability !== null && !translationAvailability.available;
  const translationUnavailableReason =
    translationUnavailable && translationAvailability !== null
      ? formatTranslationAvailabilityMessage(translationAvailability)
      : null;
  const showTranslatedRevision =
    activeTranslationRevisionKey !== null &&
    translationViewRevisionKey === activeTranslationRevisionKey &&
    activeTranslationState?.status === "success";
  const activeReaderContent =
    showTranslatedRevision && activeTranslationState.status === "success"
      ? activeTranslationState.translatedSectionsBySectionId[
          activeReaderSection?.sectionId ?? ""
        ]?.translatedContentMd ?? activeReaderSection?.content ?? ""
      : activeReaderSection?.content ?? "";
  const translationToggleDisabled =
    activeTranslationState?.status === "loading" ||
    activeTranslationRevisionKey === null ||
    (translationAvailability === null && activeTranslationState?.status !== "success") ||
    (translationUnavailable && !showTranslatedRevision);
  const translationToolbarMessage = compareMode
    ? null
    : translationUnavailableReason ??
      (activeTranslationState?.status === "loading"
        ? "Translating to zh-CN. Showing the original until verification succeeds."
        : activeTranslationState?.status === "failure"
          ? formatTranslationFailureMessage(activeTranslationState)
          : activeTranslationState?.status === "success"
            ? null
            : translationAvailability === null
              ? "Translation availability is not known yet."
              : "No translation has been generated for this revision yet.");

  function setParamPatch(patch: Record<string, string | null>) {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(patch)) {
      if (value === null || value.length === 0) {
        next.delete(key);
      } else {
        next.set(key, value);
      }
    }
    setSearchParams(next);
  }

  function selectRevision(revisionId: string) {
    setParamPatch({
      revision: revisionId,
      compare:
        revisionId === model.compareRevisionId
          ? null
          : model.compareRevisionId || null,
      section: null,
    });
  }

  function setTimelinePage(page: number) {
    setParamPatch({ page: String(page) });
  }

  function toggleCompareBase(revisionId: string) {
    setParamPatch({
      compare: model.compareRevisionId === revisionId ? null : revisionId,
      section: null,
    });
  }

  function setSectionPage(page: number) {
    setParamPatch({ section: String(page) });
  }

  async function handleQuickBriefGenerate() {
    if (!quickBriefRevision || !quickBriefKey) return;
    setQuickBriefs((current) => ({ ...current, [quickBriefKey]: { status: "generating" } }));
    try {
      const payload = await quickBriefMutation.mutateAsync(quickBriefRevision.canonical_bundle_sha256);
      setQuickBriefs((current) => ({ ...current, [quickBriefKey]: { status: "ready", briefMd: payload.brief_md, provider: payload.provider, model: payload.model, promptVersion: payload.prompt_version, generatedAt: payload.generated_at } }));
    } catch (error) {
      setQuickBriefs((current) => ({ ...current, [quickBriefKey]: buildQuickBriefFailureState(error) }));
    }
  }

  function handleQuickBriefExport() {
    if (!quickBriefRevision || !quickBriefState || quickBriefState.status !== "ready") return;
    downloadTextFile(`thesis-quick-brief-${targetKey}-${slugify(revision.revision_id)}.md`, buildGeneratedExport("quick_brief", targetKey, revision, quickBriefState.briefMd, quickBriefState.generatedAt, quickBriefState.provider, quickBriefState.model, quickBriefState.promptVersion));
  }

  function handleTranslationExport(state: Extract<ThesisRevisionTranslationState, { status: "success" }>) {
    if (!activeReaderSection) return;
    const content = Object.entries(state.translatedSectionsBySectionId)
      .map(([sectionId, section]) => `## ${sectionId}\n\n${section.translatedContentMd}`)
      .join("\n\n");
    downloadTextFile(`thesis-translation-${targetKey}-${slugify(revision.revision_id)}.md`, buildGeneratedExport("translation", targetKey, revision, content, state.generatedAt, state.translator.provider, state.translator.model, state.translator.prompt_version));
  }

  async function handleReaderTranslationToggle() {
    if (compareMode || activeReaderSection === null) {
      return;
    }
    if (translationUnavailable && !showTranslatedRevision) {
      return;
    }
    const translationRevisionKey = buildTranslationRevisionKey(
      revision.revision_id,
      revision.canonical_bundle_sha256
    );
    if (translationRevisionKey === null) {
      return;
    }
    if (
      translationViewRevisionKey === translationRevisionKey &&
      activeTranslationState?.status === "success"
    ) {
      setTranslationViewRevisionKey(null);
      return;
    }
    if (activeTranslationState?.status === "success") {
      setTranslationViewRevisionKey(translationRevisionKey);
      return;
    }

    setTranslationViewRevisionKey(translationRevisionKey);
    setRevisionTranslations((current) => ({
      ...current,
      [translationRevisionKey]: { status: "loading" },
    }));

    try {
      const translation = await translationMutation.mutateAsync({
        canonical_bundle_sha256: revision.canonical_bundle_sha256,
        locale: "zh-CN",
      });
      setRevisionTranslations((current) => ({
        ...current,
        [translationRevisionKey]: buildTranslationSuccessState(translation),
      }));
      setTranslationViewRevisionKey(translationRevisionKey);
    } catch (error) {
      setRevisionTranslations((current) => ({
        ...current,
        [translationRevisionKey]: buildTranslationFailureState(error),
      }));
      setTranslationViewRevisionKey(null);
    }
  }

  function handleExportMemo() {
    const memo = compareMode
      ? buildComparisonExportMemo({
          targetKey,
          revision,
          compareSource,
          compareSections,
        })
      : buildSnapshotExportMemo({
          targetKey,
          revision,
        });
    downloadTextFile(
      `thesis-evolution-${targetKey}-${slugify(revision.revision_id)}.md`,
      memo
    );
  }

  return (
    <div className="page thesis-page">
      <section className="hero-card thesis-hero-card">
        <div className="hero-main thesis-hero-main">
          <p className="eyebrow">{t("Thesis Evolution")}</p>
          <h2>{targetKey.toUpperCase()}</h2>
          <div className="hero-meta-row">
            <span className="hero-meta-pill">
              {compareMode ? t("Compare mode") : t("Rendered reader")}
            </span>
            <span className="hero-meta-pill">
              {formatSource(revision.source)}
            </span>
            <span className="hero-meta-pill">
              {formatDateTime(revision.business_at)}
            </span>
          </div>
        </div>
        <div className="hero-status-panel thesis-hero-status-panel">
          <div className="hero-statuses thesis-hero-statuses">
            <div className="metric-badge">
              <span>{t("Revision Count")}</span>
              <strong>{model.list.revisions.length}</strong>
            </div>
            <div className="metric-badge">
              <span>{t("Changed Sections")}</span>
              <strong>{model.changedSections.length}</strong>
            </div>
          </div>
        </div>
      </section>

      <section className="thesis-layout">
        <aside className="side-column thesis-timeline-column">
          <div className="side-panel thesis-summary-panel">
            <div className="card-header">
              <div>
                <p className="eyebrow">{t("Timeline")}</p>
                <h3>{t("Revision History")}</h3>
              </div>
              <span className="timeline-count">
                {model.list.revisions.length} {t("total revisions")}
              </span>
            </div>
            <div className="thesis-locator">
              <label className="thesis-locator-label" htmlFor="thesis-revision-locator">
                {t("Find revisions")}
              </label>
              <input
                id="thesis-revision-locator"
                className="thesis-locator-input"
                type="search"
                value={timelineQuery}
                onChange={(event) => {
                  setTimelineQuery(event.target.value);
                  setParamPatch({ page: "1" });
                }}
                placeholder={t("Date, source, changed sections")}
              />
              <p className="timeline-filter-summary" aria-live="polite">
                {timelineQuery.trim().length > 0
                  ? `${model.filteredRevisionCount} ${t("matching of")} ${model.list.revisions.length}`
                  : `${model.list.revisions.length} ${t("revisions")}`}
              </p>
            </div>
            {model.filteredRevisionCount === 0 ? (
              <p className="timeline-empty" role="status">
                {t("No revisions match")} “{timelineQuery}”。{t("Selected and compare states are preserved.")}
              </p>
            ) : (
              <div className="revision-list">
                {model.pagedRevisions.map((summary) => {
                const isActive = summary.revision_id === revision.revision_id;
                const isCompareBase =
                  model.compareRevisionId.length > 0 &&
                  summary.revision_id === model.compareRevisionId;
                return (
                  <article
                    key={summary.revision_id}
                    className={isActive ? "revision-link active" : "revision-link"}
                  >
                    <button
                      type="button"
                      className="revision-select-button"
                      onClick={() => selectRevision(summary.revision_id)}
                    >
                      <span className="revision-link-top">
                        <strong>{formatDateTime(summary.business_at)}</strong>
                      </span>
                      <span className="revision-link-bottom">
                        {summary.is_baseline
                          ? t("Baseline snapshot")
                          : `${summary.changed_section_count} ${t("changed sections")}`}
                      </span>
                    </button>
                    <div className="revision-actions">
                      {isActive || isCompareBase ? (
                        <span className="revision-badge">
                          {isActive ? t("Selected") : t("Compare base")}
                        </span>
                      ) : (
                        <span className="revision-badge" aria-hidden="true" />
                      )}
                      {!isActive ? (
                        <button
                          type="button"
                          className={
                            isCompareBase
                              ? "thesis-mini-button active"
                              : "thesis-mini-button"
                          }
                          onClick={() => toggleCompareBase(summary.revision_id)}
                        >
                          {isCompareBase ? t("Clear compare") : t("Use as compare")}
                        </button>
                      ) : null}
                    </div>
                  </article>
                );
                })}
              </div>
            )}
            <div className="timeline-pagination">
              <button
                type="button"
                className="thesis-mini-button"
                onClick={() => setTimelinePage(model.activePage - 1)}
                disabled={model.activePage <= 1}
              >
                {t("Previous")}
              </button>
              <span className="timeline-page-label">
                {t("Page")} {model.activePage} {t("of")} {model.pageCount}
              </span>
              <button
                type="button"
                className="thesis-mini-button"
                onClick={() => setTimelinePage(model.activePage + 1)}
                disabled={model.activePage >= model.pageCount}
              >
                {t("Next")}
              </button>
            </div>
            <div className="thesis-action-row">
              <button
                type="button"
                className="thesis-button"
                onClick={handleExportMemo}
              >
                {t("Export memo")}
              </button>
            </div>
          </div>
        </aside>

        <main className="thesis-main-column">
          <div className="side-panel">
            <div className="card-header">
              <div>
                <p className="eyebrow">{t("Selected Revision")}</p>
                <h3>{t("Change Summary")}</h3>
              </div>
            </div>
            <div className="metric-row thesis-summary-row">
              <MetricTag
                label={t("Revision At")}
                value={formatDateTime(revision.business_at)}
              />
              <MetricTag
                label={compareMode ? t("Comparing Against") : t("Reading Mode")}
                value={
                  compareMode && compareSource
                    ? formatCompareBase(compareSource)
                    : t("Rendered original thesis")
                }
              />
              <MetricTag
                label={t("Changed Sections")}
                value={String(model.changedSections.length)}
              />
              <MetricTag
                label={t("Price Level Roles")}
                value={String(revision.price_level_role_ids.length)}
              />
            </div>
            <div className="token-group">
              <TokenRow
                label={t("Price level role IDs")}
                values={revision.price_level_role_ids}
                classifyValue={classifyPriceLevelRoleId}
                compactEmpty
              />
            </div>
            <QuickBriefPanel state={quickBriefState} onGenerate={handleQuickBriefGenerate} onExport={handleQuickBriefExport} />
          </div>

          <div className="side-panel thesis-reader-panel">
            <div className="card-header">
              <div>
                <p className="eyebrow">{compareMode ? t("Compare") : t("Reader")}</p>
                <h3>
                  {compareMode ? t("Selected vs compare base") : t("Rendered thesis")}
                </h3>
              </div>
              {activeReaderSection ? (
                <div className="card-header-inline thesis-reader-header-inline">
                  <span className="card-header-note">
                    {t("Section")} {activeSectionPage} {t("of")} {sectionPageCount}
                  </span>
                  {!compareMode ? (
                    <div className="reader-translation-toolbar">
                      {renderTranslationStatusPill(
                        activeTranslationState,
                        translationAvailability
                      )}
                      <button
                        type="button"
                        className={
                          showTranslatedRevision
                            ? "thesis-mini-button active"
                            : "thesis-mini-button"
                        }
                        onClick={handleReaderTranslationToggle}
                        disabled={translationToggleDisabled}
                      >
                        {showTranslatedRevision
                          ? t("View Original")
                          : activeTranslationState?.status === "loading"
                            ? t("Translating...")
                            : activeTranslationState?.status === "failure"
                              ? t("Retry translation")
                              : t("Translate to Chinese")}
                      </button>
                      {activeTranslationState?.status === "success" ? (
                        <button type="button" className="thesis-mini-button" onClick={() => handleTranslationExport(activeTranslationState)}>
                          {t("Export translation")}
                        </button>
                      ) : null}
                      {translationToolbarMessage ? (
                        <span className="reader-translation-note">
                          {translationToolbarMessage}
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                  <SectionPager
                    page={activeSectionPage}
                    pageCount={sectionPageCount}
                    onPrevious={() => setSectionPage(activeSectionPage - 1)}
                    onNext={() => setSectionPage(activeSectionPage + 1)}
                  />
                </div>
              ) : null}
            </div>
            {model.compareRevisionNotFound ? (
              <p className="panel-copy">
                Compare base `{model.compareRevisionId}` was not found. Clear the
                compare selection or choose another revision.
              </p>
            ) : compareMode ? (
              compareSections.length && activeCompareSection ? (
                <div className="reader-stack">
                  <article className="diff-compare-card">
                    <div className="reader-section-header">
                      <div>
                        <strong>{activeCompareSection.sectionName}</strong>
                        <span>{activeCompareSection.pageName}</span>
                      </div>
                    </div>
                    <div className="diff-compare-grid">
                      <section className="diff-compare-column">
                        <h4>{t("Previous section")}</h4>
                        <DiffColumn
                          lines={buildDiffColumns(
                            normalizeDisplayMarkdown(
                              activeCompareSection.beforeContent
                            ),
                            normalizeDisplayMarkdown(
                              activeCompareSection.afterContent
                            )
                          ).before}
                        />
                      </section>
                      <section className="diff-compare-column">
                        <h4>{t("Current section")}</h4>
                        <DiffColumn
                          lines={buildDiffColumns(
                            normalizeDisplayMarkdown(
                              activeCompareSection.beforeContent
                            ),
                            normalizeDisplayMarkdown(
                              activeCompareSection.afterContent
                            )
                          ).after}
                        />
                      </section>
                    </div>
                  </article>
                </div>
              ) : (
                <p className="panel-copy">
                {t("No stored content changes were recorded against the selected comparison base.")}
                </p>
              )
            ) : activeReaderSection ? (
              <div className="reader-stack">
                <article className="reader-card">
                  <div className="reader-section-header">
                    <div>
                      <strong>{activeReaderSection.sectionName}</strong>
                      <span>{activeReaderSection.pageName}</span>
                    </div>
                  </div>
                  <MarkdownSectionReader
                    content={normalizeDisplayMarkdown(activeReaderContent)}
                  />
                </article>
              </div>
            ) : (
              <p className="panel-copy">
                {t("No stored thesis content is available for this revision.")}
              </p>
            )}
          </div>
        </main>

        <aside className="side-column">
          <div className="side-panel">
            <div className="card-header">
              <div>
                <p className="eyebrow">{t("Linkage")}</p>
                <h3>{t("Analysis Trace")}</h3>
              </div>
            </div>
            <div className="trace-groups">
              <TraceGroup
                label="Analysis assessment"
                value={revision.analysis_assessment_id}
                emptyMessage="No analysis assessment is linked."
              />
              <TraceGroup
                label="Context packet"
                value={revision.context_packet_id}
                emptyMessage="No context packet is linked."
              />
              <TraceGroup
                label="Previous revision"
                value={revision.previous_revision_id}
                emptyMessage="This is the first stored revision."
              />
              <TraceIdListGroup
                label="Source events"
                values={revision.source_event_ids}
                emptyMessage="No source event IDs are stored."
              />
              <TraceClaimGroup revision={revision} />
              <TracePriceLevelGroup
                levels={revision.price_levels}
                roleIds={revision.price_level_role_ids}
              />
            </div>
            <div className="thesis-linked-decisions">
              <div className="card-header-inline">
                <div>
                  <p className="eyebrow">Decision linkage</p>
                  <h4>Linked PM decisions</h4>
                </div>
                <span className="timeline-count">
                  {revision.linked_pm_decisions.length}
                </span>
              </div>
              {revision.linked_pm_decisions.length > 0 ? (
                <div className="linked-decision-list">
                  {revision.linked_pm_decisions.map((decision) => (
                    <Link
                      key={decision.decision_id}
                      className="linked-decision-row"
                      to={`/targets/${encodeURIComponent(targetKey)}/pm?decision=${encodeURIComponent(decision.decision_id)}`}
                    >
                      <span>
                        {formatDateTime(decision.business_at)} · {decision.requested_state} {formatWeight(decision.requested_target_weight)}
                      </span>
                      <small>{decision.outcome_status}</small>
                    </Link>
                  ))}
                </div>
              ) : (
                <p className="panel-copy">No PM decision is linked to this revision.</p>
              )}
            </div>
            <details className="audit-details">
              <summary>Audit details</summary>
              <div className="audit-details-body">
                <MetricTag
                  label="Committed at"
                  value={formatDateTime(revision.committed_at)}
                />
                <TraceGroup
                  label="Context packet hash"
                  value={revision.context_packet_hash}
                  emptyMessage="No context packet hash is stored."
                />
                <TraceGroup
                  label="Canonical bundle SHA-256"
                  value={revision.canonical_bundle_sha256}
                  emptyMessage="No canonical bundle hash is stored."
                />
                <TraceIdListGroup
                  label="Research memory write receipts"
                  values={revision.research_memory_write_receipt_ids}
                  emptyMessage="No research memory write receipts are stored."
                />
              </div>
            </details>
          </div>
        </aside>
      </section>
    </div>
  );
}

function SectionPager({
  page,
  pageCount,
  onPrevious,
  onNext,
}: {
  page: number;
  pageCount: number;
  onPrevious: () => void;
  onNext: () => void;
}) {
  const { t } = useLocale();
  return (
    <div className="reader-toolbar-actions">
      <button
        type="button"
        className="thesis-mini-button"
        onClick={onPrevious}
        disabled={page <= 1}
      >
        {t("Previous")}
      </button>
      <span className="timeline-page-label">
        {page} / {pageCount}
      </span>
      <button
        type="button"
        className="thesis-mini-button"
        onClick={onNext}
        disabled={page >= pageCount}
      >
        {t("Next")}
      </button>
    </div>
  );
}

function MetricTag({ label, value }: { label: string; value: string }) {
  return (
    <div className="mini-definition-card">
      <span className="mini-definition-title">{label}</span>
      <span className="mini-definition-value">{value}</span>
    </div>
  );
}

function TraceGroup({
  label,
  value,
  emptyMessage,
}: {
  label: string;
  value: string | null;
  emptyMessage: string;
}) {
  const { t } = useLocale();
  return (
    <details className="trace-group">
      <summary>
        <span>{label}</span>
        <strong>{value ? shortenIdentifier(value) : t("Not linked")}</strong>
      </summary>
      {value ? <TraceIdRow label={label} value={value} /> : <p className="trace-empty">{emptyMessage}</p>}
    </details>
  );
}

function TraceIdListGroup({
  label,
  values,
  emptyMessage,
}: {
  label: string;
  values: string[];
  emptyMessage: string;
}) {
  return (
    <details className="trace-group">
      <summary>
        <span>{label}</span>
        <strong>{values.length ? `${values.length} stored` : "None"}</strong>
      </summary>
      {values.length ? (
        <div className="trace-id-list">
          {values.map((value) => <TraceIdRow key={value} label={label} value={value} />)}
        </div>
      ) : (
        <p className="trace-empty">{emptyMessage}</p>
      )}
    </details>
  );
}

function TraceClaimGroup({
  revision,
}: {
  revision: PMConsoleThesisRevisionDetail;
}) {
  const groups = [
    { label: "Changed claims", values: revision.changed_claim_ids },
    { label: "Key claims", values: revision.key_claim_ids },
    { label: "Contested prior claims", values: revision.contested_prior_claim_ids },
  ].filter((group) => group.values.length > 0);
  const total = groups.reduce((count, group) => count + group.values.length, 0);

  return (
    <details className="trace-group">
      <summary>
        <span>Claims</span>
        <strong>{total ? `${total} stored` : "None"}</strong>
      </summary>
      {groups.length ? (
        <div className="trace-subgroups">
          {groups.map((group) => (
            <div key={group.label} className="trace-subgroup">
              <span className="trace-subgroup-label">{group.label}</span>
              <div className="trace-id-list">
                {group.values.map((value) => (
                  <TraceIdRow key={value} label={group.label} value={value} />
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="trace-empty">No claim IDs are stored for this revision.</p>
      )}
    </details>
  );
}

function TracePriceLevelGroup({
  levels,
  roleIds,
}: {
  levels: PMConsoleThesisPriceLevel[];
  roleIds: string[];
}) {
  return (
    <details className="trace-group">
      <summary>
        <span>Price levels</span>
        <strong>{levels.length || roleIds.length ? `${levels.length || roleIds.length} stored` : "None"}</strong>
      </summary>
      {levels.length ? (
        <div className="trace-price-level-list">
          {levels.map((level) => (
            <div key={level.level_id} className="trace-price-level">
              <div>
                <strong>{formatPriceLevelRole(level.display_role)}</strong>
                <span>{formatPriceLevelValue(level)}</span>
              </div>
              <TraceIdRow label="Price level" value={level.level_id} />
            </div>
          ))}
        </div>
      ) : roleIds.length ? (
        <div className="trace-id-list">
          {roleIds.map((value) => <TraceIdRow key={value} label="Price level role" value={value} />)}
        </div>
      ) : (
        <p className="trace-empty">No price levels are stored for this revision.</p>
      )}
    </details>
  );
}

function TraceIdRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="trace-id-row">
      <code title={value}>{value}</code>
      <button
        type="button"
        className="trace-copy-button"
        aria-label={`Copy ${label} ID`}
        title={`Copy ${label} ID`}
        onClick={() => void copyTraceId(value)}
      >
        Copy ID
      </button>
    </div>
  );
}

async function copyTraceId(value: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function shortenIdentifier(value: string) {
  if (value.length <= 22) {
    return value;
  }
  return `${value.slice(0, 9)}...${value.slice(-8)}`;
}

function MarkdownSectionReader({ content }: { content: string }) {
  const blocks = parseMarkdownBlocks(content);
  return (
    <div className="reader-prose">
      {blocks.map((block) => {
        switch (block.type) {
          case "heading":
            if (block.level <= 2) {
              return (
                <h4 key={block.key} className="reader-heading-lg">
                  {renderInlineMarkdown(block.text)}
                </h4>
              );
            }
            return (
              <h5 key={block.key} className="reader-heading-sm">
                {renderInlineMarkdown(block.text)}
              </h5>
            );
          case "list":
            return (
              <ul key={block.key} className="reader-list">
                {block.items.map((item, index) => (
                  <li key={`${block.key}-${index}`}>{renderInlineMarkdown(item)}</li>
                ))}
              </ul>
            );
          case "code":
            return (
              <pre key={block.key} className="reader-code-block">
                {block.text}
              </pre>
            );
          case "rule":
            return <hr key={block.key} className="reader-rule" />;
          default:
            return (
              <p key={block.key} className="reader-paragraph">
                {renderInlineMarkdown(block.text)}
              </p>
            );
        }
      })}
    </div>
  );
}

function TokenRow({
  label,
  values,
  classifyValue,
  compactEmpty = false,
}: {
  label: string;
  values: string[];
  classifyValue?: (value: string) => string | null;
  compactEmpty?: boolean;
}) {
  return (
    <div className="token-row">
      <span className="token-row-label">{label}</span>
      {values.length ? (
        <div className="token-list">
          {values.map((value) => (
            <code
              key={value}
              className={
                classifyValue
                  ? `token-chip ${classifyValue(value) ?? ""}`.trim()
                  : "token-chip"
              }
            >
              {value}
            </code>
          ))}
        </div>
      ) : (
        <p className={compactEmpty ? "muted token-row-empty" : "muted"}>None</p>
      )}
    </div>
  );
}

function classifyPriceLevelRoleId(value: string) {
  const normalized = value.toLowerCase();
  if (normalized.includes("support") || normalized.includes("low")) {
    return "token-chip-support";
  }
  if (normalized.includes("resistance") || normalized.includes("high")) {
    return "token-chip-resistance";
  }
  return null;
}

function DiffColumn({
  lines,
}: {
  lines: Array<{ key: string; kind: "same" | "add" | "del"; text: string }>;
}) {
  return (
    <div className="diff-column-block">
      {lines.map((line) => (
        <p key={line.key} className={`diff-line ${line.kind}`}>
          {line.text}
        </p>
      ))}
    </div>
  );
}

function buildDiffColumns(before: string, after: string) {
  const beforeLines = before.split("\n");
  const afterLines = after.split("\n");
  const lcs = buildLcsMatrix(beforeLines, afterLines);
  const beforeOutput: Array<{
    key: string;
    kind: "same" | "add" | "del";
    text: string;
  }> = [];
  const afterOutput: Array<{
    key: string;
    kind: "same" | "add" | "del";
    text: string;
  }> = [];

  let row = beforeLines.length;
  let column = afterLines.length;
  while (row > 0 || column > 0) {
    if (
      row > 0 &&
      column > 0 &&
      beforeLines[row - 1] === afterLines[column - 1]
    ) {
      const text = beforeLines[row - 1] || " ";
      beforeOutput.unshift({ key: `b-same-${row}-${column}`, kind: "same", text });
      afterOutput.unshift({ key: `a-same-${row}-${column}`, kind: "same", text });
      row -= 1;
      column -= 1;
      continue;
    }
    if (
      column > 0 &&
      (row === 0 || lcs[row][column - 1] >= lcs[row - 1][column])
    ) {
      afterOutput.unshift({
        key: `a-add-${row}-${column}`,
        kind: "add",
        text: `+ ${afterLines[column - 1] || " "}`,
      });
      column -= 1;
      continue;
    }
    if (row > 0) {
      beforeOutput.unshift({
        key: `b-del-${row}-${column}`,
        kind: "del",
        text: `- ${beforeLines[row - 1] || " "}`,
      });
      row -= 1;
    }
  }

  return {
    before: beforeOutput,
    after: afterOutput,
  };
}

function buildLcsMatrix(beforeLines: string[], afterLines: string[]) {
  const matrix = Array.from({ length: beforeLines.length + 1 }, () =>
    Array.from({ length: afterLines.length + 1 }, () => 0)
  );
  for (let row = 1; row <= beforeLines.length; row += 1) {
    for (let column = 1; column <= afterLines.length; column += 1) {
      if (beforeLines[row - 1] === afterLines[column - 1]) {
        matrix[row][column] = matrix[row - 1][column - 1] + 1;
      } else {
        matrix[row][column] = Math.max(
          matrix[row - 1][column],
          matrix[row][column - 1]
        );
      }
    }
  }
  return matrix;
}

function QuickBriefPanel({ state, onGenerate, onExport }: { state: ThesisQuickBriefState | null; onGenerate: () => void; onExport: () => void }) {
  const { t } = useLocale();
  return (
    <section className="thesis-quick-brief">
      <div className="card-header-inline">
        <div>
          <p className="eyebrow">{t("Reader aid")}</p>
          <h4>{t("Thesis Quick Brief")}</h4>
        </div>
        <span
          className={
            state?.status === "failed" || state?.status === "unavailable"
              ? "status-pill unavailable"
              : state?.status === "generating"
                ? "status-pill partial"
                : "status-pill ready"
          }
        >
          {state?.status === "generating"
            ? t("Generating")
            : state?.status === "ready"
                ? t("Ready")
                : state?.status === "unavailable"
                  ? t("Unavailable")
                  : state?.status === "failed"
                    ? t("Failed")
                    : t("Pending")}
        </span>
      </div>
      {!state ? (
        <div className="reader-aid-actions"><p className="panel-copy">{t("AI-generated reader aid. Non-canonical and derived from this Thesis revision.")}</p><button type="button" className="thesis-mini-button" onClick={onGenerate}>{t("Generate brief")}</button></div>
      ) : state.status === "generating" ? (
        <p className="panel-copy">
          {state ? t("Generating a compact reader brief. The canonical thesis remains available below.") : t("Preparing a compact reader brief.")}
        </p>
      ) : state.status === "failed" || state.status === "unavailable" ? (
        <p className="panel-copy">{t("Quick brief unavailable:")} {state.message}</p>
      ) : state.status === "ready" ? (
        <><p className="reader-aid-label">{t("AI-generated reader aid · non-canonical")}</p><MarkdownContent content={state.briefMd} className="quick-brief-markdown" /><div className="reader-aid-actions"><button type="button" className="thesis-mini-button" onClick={onGenerate}>{t("Regenerate")}</button><button type="button" className="thesis-mini-button" onClick={onExport}>{t("Export brief")}</button></div></>
      ) : null}
      <p className="quick-brief-note">{t("Reader aid only. It does not modify the canonical thesis or workspace.")}</p>
    </section>
  );
}

function buildQuickBriefFailureState(error: unknown): ThesisQuickBriefState {
  if (error instanceof PMConsoleApiError) {
    const payload = error.payload as PMConsoleThesisQuickBriefFailure | null;
    const message = payload?.error_message ?? error.message;
    const code = payload?.error_code ?? "quick_brief_failed";
    return {
      status: code.includes("unavailable") || code.includes("configured") ? "unavailable" : "failed",
      message,
    };
  }
  return {
    status: "failed",
    message: error instanceof Error ? error.message : "quick brief request failed.",
  };
}

function renderTranslationStatusPill(
  state: ThesisRevisionTranslationState | null,
  availability: PMConsoleThesisTranslationAvailability | null
) {
  if (state === null) {
    if (availability === null) {
      return <span className="status-pill unavailable">Translation status unknown</span>;
    }
    if (availability.available) {
      return <span className="status-pill partial">Translation available</span>;
    }
    return <span className="status-pill unavailable">zh-CN Unavailable</span>;
  }
  if (state.status === "loading") {
    return <span className="status-pill partial">Loading zh-CN</span>;
  }
  if (state.status === "success") {
    return <span className="status-pill ready">AI translation · session</span>;
  }
  return <span className="status-pill unavailable">Failed zh-CN</span>;
}

function formatTranslationAvailabilityMessage(
  availability: PMConsoleThesisTranslationAvailability
) {
  return `zh-CN unavailable: ${
    availability.reason_message ??
    "translator not configured."
  }`;
}

function formatTranslationFailureMessage(
  state: Extract<ThesisRevisionTranslationState, { status: "failure" }>
) {
  const detail =
    state.verificationFailures.length > 0
      ? state.verificationFailures[0]
      : state.errorMessage;
  return `zh-CN failed: ${detail}. Showing original.`;
}

function buildTranslationRevisionKey(
  revisionId: string,
  canonicalBundleSha256: string | null
) {
  if (!canonicalBundleSha256) {
    return null;
  }
  return `${revisionId}:${canonicalBundleSha256}`;
}

function buildTranslationSuccessState(
  payload: PMConsoleThesisTranslationResponse
): Extract<ThesisRevisionTranslationState, { status: "success" }> {
  const translatedSectionsBySectionId: Record<
    string,
    {
      translatedContentMd: string;
    }
  > = {};
  for (const section of payload.sections) {
    translatedSectionsBySectionId[section.section_id] =
      buildTranslatedSectionState(section);
  }
  return {
    status: "success",
    translatedSectionsBySectionId,
    translator: payload.translator,
    generatedAt: payload.generated_at,
  };
}

function buildTranslationFailureState(error: unknown): Extract<
  ThesisRevisionTranslationState,
  { status: "failure" }
> {
  if (error instanceof PMConsoleApiError) {
    const payload = error.payload as PMConsoleThesisTranslationFailure | null;
    const failures = payload?.verification?.failures ?? [];
    return {
      status: "failure",
      errorCode: payload?.error_code ?? "translation_request_failed",
      errorMessage: payload?.error_message ?? error.message,
      verificationFailures: failures,
    };
  }
  return {
    status: "failure",
    errorCode: "translation_request_failed",
    errorMessage:
      error instanceof Error ? error.message : "translation request failed.",
    verificationFailures: [],
  };
}

function buildTranslatedSectionState(section: PMConsoleTranslatedSection) {
  return {
    translatedContentMd: section.translated_content_md,
  };
}

function buildComparisonExportMemo({
  targetKey,
  revision,
  compareSource,
  compareSections,
}: {
  targetKey: string;
  revision: PMConsoleThesisRevisionDetail;
  compareSource: PMConsoleThesisRevisionDetail | PMConsoleThesisRevisionSummary | null;
  compareSections: Array<{
    sectionId: string;
    pageName: string;
    sectionName: string;
    beforeContent: string;
    afterContent: string;
  }>;
}) {
  const lines = [
    `# Thesis Evolution Memo: ${targetKey.toUpperCase()}`,
    "",
    `- Selected revision: \`${revision.revision_id}\``,
    `- Source: ${formatSource(revision.source)}`,
    `- Business at: ${formatDateTime(revision.business_at)}`,
    `- Compare base: \`${compareSource?.revision_id ?? revision.previous_revision_id ?? "none"}\``,
    "",
    "## Claims",
    "",
    `- Changed claim IDs: ${formatList(revision.changed_claim_ids)}`,
    `- Key claim IDs: ${formatList(revision.key_claim_ids)}`,
    `- Contested prior claim IDs: ${formatList(revision.contested_prior_claim_ids)}`,
    `- Price levels: ${formatPriceLevels(revision.price_levels)}`,
    `- Price level role IDs: ${formatList(revision.price_level_role_ids)}`,
    "",
    "## Linkage",
    "",
    `- Analysis assessment ID: \`${revision.analysis_assessment_id ?? "none"}\``,
    `- Context packet ID: \`${revision.context_packet_id ?? "none"}\``,
    `- Context packet hash: \`${revision.context_packet_hash ?? "none"}\``,
    `- Canonical bundle sha256: \`${revision.canonical_bundle_sha256}\``,
    `- Research memory write receipts: ${formatList(
      revision.research_memory_write_receipt_ids
    )}`,
    "",
    "## Section Comparison",
    "",
  ];

  if (compareSections.length === 0) {
    lines.push("- No changed sections against the comparison base.");
  } else {
    for (const section of compareSections) {
      lines.push(`### ${section.pageName} / ${section.sectionName}`);
      lines.push("");
      lines.push("#### Previous section");
      lines.push("");
      lines.push("```md");
      lines.push(normalizeDisplayMarkdown(section.beforeContent) || "(empty)");
      lines.push("```");
      lines.push("");
      lines.push("#### Current section");
      lines.push("");
      lines.push("```md");
      lines.push(normalizeDisplayMarkdown(section.afterContent) || "(empty)");
      lines.push("```");
      lines.push("");
    }
  }

  return lines.join("\n");
}

function buildSnapshotExportMemo({
  targetKey,
  revision,
}: {
  targetKey: string;
  revision: PMConsoleThesisRevisionDetail;
}) {
  const lines = [
    `# Thesis Snapshot Memo: ${targetKey.toUpperCase()}`,
    "",
    `- Selected revision: \`${revision.revision_id}\``,
    `- Source: ${formatSource(revision.source)}`,
    `- Business at: ${formatDateTime(revision.business_at)}`,
    "",
    "## Claims",
    "",
    `- Changed claim IDs: ${formatList(revision.changed_claim_ids)}`,
    `- Key claim IDs: ${formatList(revision.key_claim_ids)}`,
    `- Contested prior claim IDs: ${formatList(revision.contested_prior_claim_ids)}`,
    `- Price levels: ${formatPriceLevels(revision.price_levels)}`,
    `- Price level role IDs: ${formatList(revision.price_level_role_ids)}`,
    "",
    "## Thesis Snapshot",
    "",
  ];

  for (const section of revision.sections) {
    lines.push(`### ${section.page_name} / ${section.section_name}`);
    lines.push("");
    lines.push("```md");
    lines.push(normalizeDisplayMarkdown(section.content_md) || "(empty)");
    lines.push("```");
    lines.push("");
  }

  return lines.join("\n");
}

function downloadTextFile(filename: string, contents: string) {
  const blob = new Blob([contents], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function formatSource(source: "analysis_commit" | "migration_baseline") {
  return source === "migration_baseline" ? "Baseline migration" : "Analysis commit";
}

function formatDateTime(value: string) {
  return value.replace("T", " ").replace("+00:00", " UTC");
}

function formatList(values: string[]) {
  if (values.length === 0) {
    return "None";
  }
  return values.map((value) => `\`${value}\``).join(", ");
}

function formatPriceLevels(levels: PMConsoleThesisPriceLevel[]) {
  if (levels.length === 0) {
    return "None";
  }
  return levels
    .map(
      (level) =>
        `${formatPriceLevelRole(level.display_role)} ${formatPriceLevelValue(level)}`
    )
    .join(", ");
}

function formatPriceLevelRole(role: PMConsoleThesisPriceLevel["display_role"]) {
  switch (role) {
    case "support":
      return "Support";
    case "resistance":
      return "Resistance";
    case "watch":
      return "Watch";
    default:
      return "Level";
  }
}

function formatPriceLevelValue(level: PMConsoleThesisPriceLevel) {
  if (level.value !== null) {
    return formatPrice(level.value);
  }
  if (level.lower !== null && level.upper !== null) {
    return `${formatPrice(level.lower)} - ${formatPrice(level.upper)}`;
  }
  return level.level_id;
}

function parseMarkdownBlocks(content: string) {
  const lines = content.split("\n");
  const blocks: Array<
    | { type: "heading"; level: number; text: string; key: string }
    | { type: "paragraph"; text: string; key: string }
    | { type: "list"; items: string[]; key: string }
    | { type: "code"; text: string; key: string }
    | { type: "rule"; key: string }
  > = [];

  let index = 0;
  while (index < lines.length) {
    const line = lines[index].trimEnd();
    const trimmed = line.trim();

    if (trimmed.length === 0) {
      index += 1;
      continue;
    }

    if (trimmed === "---") {
      blocks.push({ type: "rule", key: `rule-${index}` });
      index += 1;
      continue;
    }

    if (trimmed.startsWith("```")) {
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) {
        index += 1;
      }
      blocks.push({
        type: "code",
        text: codeLines.join("\n"),
        key: `code-${index}`,
      });
      continue;
    }

    const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      blocks.push({
        type: "heading",
        level: headingMatch[1].length,
        text: headingMatch[2],
        key: `heading-${index}`,
      });
      index += 1;
      continue;
    }

    if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      const items: string[] = [];
      while (index < lines.length) {
        const listLine = lines[index].trim();
        if (listLine.startsWith("- ") || listLine.startsWith("* ")) {
          items.push(listLine.slice(2).trim());
          index += 1;
          continue;
        }
        if (listLine.length === 0) {
          index += 1;
        }
        break;
      }
      blocks.push({ type: "list", items, key: `list-${index}` });
      continue;
    }

    const paragraphLines: string[] = [trimmed];
    index += 1;
    while (index < lines.length) {
      const paragraphLine = lines[index].trim();
      if (
        paragraphLine.length === 0 ||
        paragraphLine === "---" ||
        paragraphLine.startsWith("```") ||
        /^(#{1,6})\s+/.test(paragraphLine) ||
        paragraphLine.startsWith("- ") ||
        paragraphLine.startsWith("* ")
      ) {
        break;
      }
      paragraphLines.push(paragraphLine);
      index += 1;
    }
    blocks.push({
      type: "paragraph",
      text: paragraphLines.join(" "),
      key: `paragraph-${index}`,
    });
  }

  return blocks;
}

function renderInlineMarkdown(text: string) {
  const nodes: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let lastIndex = 0;

  for (const match of text.matchAll(pattern)) {
    const matchedText = match[0];
    const start = match.index ?? 0;
    if (start > lastIndex) {
      nodes.push(text.slice(lastIndex, start));
    }
    if (matchedText.startsWith("**") && matchedText.endsWith("**")) {
      nodes.push(
        <strong key={`${start}-strong`}>{matchedText.slice(2, -2)}</strong>
      );
    } else if (matchedText.startsWith("`") && matchedText.endsWith("`")) {
      nodes.push(<code key={`${start}-code`}>{matchedText.slice(1, -1)}</code>);
    }
    lastIndex = start + matchedText.length;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes.length ? nodes : text;
}

function normalizeDisplayMarkdown(content: string) {
  if (!content) {
    return content;
  }

  return content
    .replace(/鈫払/g, " -> B")
    .replace(/鈫\?/g, " -> ")
    .replace(/鈫/g, " -> ")
    .replace(/鈥憈/g, "-t")
    .replace(/鈥攂/g, "-b")
    .replace(/鈥攃/g, "-c")
    .replace(/鈥攄/g, "-d")
    .replace(/鈥攕/g, "-s")
    .replace(/鈥攙/g, "-v")
    .replace(/鈥攚/g, "-w")
    .replace(/鈥\?/g, " - ")
    .replace(/鈥/g, "-");
}

function formatPrice(value: number) {
  return value.toFixed(2);
}

function buildGeneratedExport(
  kind: "quick_brief" | "translation",
  targetKey: string,
  revision: PMConsoleThesisRevisionDetail,
  content: string,
  generatedAt: string,
  provider: string,
  model: string,
  promptVersion: string,
) {
  return [
    "---",
    `generated_content_kind: ${kind}`,
    `target_key: ${targetKey}`,
    `source_revision_id: ${revision.revision_id}`,
    `canonical_source_sha256: ${revision.canonical_bundle_sha256}`,
    `generated_at: ${generatedAt}`,
    `prompt_profile_version: ${promptVersion}`,
    `returned_model: ${model || "unknown"}`,
    `configured_provider: ${provider || "unknown"}`,
    "canonical: false",
    "---",
    "",
    kind === "quick_brief" ? "# AI-generated reader aid" : "# AI-generated translation",
    "",
    content,
    "",
  ].join("\n");
}

function formatWeight(value: number) {
  return `${(value * 100).toFixed(0)}%`;
}

function formatRevisionBadge(
  revision: PMConsoleThesisRevisionDetail | PMConsoleThesisRevisionSummary
) {
  return revision.is_baseline ? "Baseline" : formatDateTime(revision.business_at);
}

function formatCompareBase(
  revision: PMConsoleThesisRevisionDetail | PMConsoleThesisRevisionSummary
) {
  return revision.is_baseline ? "Baseline snapshot" : formatDateTime(revision.business_at);
}

function slugify(value: string) {
  return value.replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
}
