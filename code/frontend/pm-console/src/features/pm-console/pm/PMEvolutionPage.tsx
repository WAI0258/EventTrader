import { useMemo } from "react";
import type { ReactNode } from "react";
import { BookOpen, ClipboardList, ExternalLink, ShieldCheck } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { MarkdownContent } from "../../../components/MarkdownContent";
import type {
  PMConsolePMDecisionDetail,
  PMConsolePMDecisionSummary,
  PMConsoleReference,
} from "../../../types";
import {
  usePmConsolePmDecisionDetailQuery,
  usePmConsolePmDecisionsQuery,
} from "../hooks";
import { useLocale } from "../locale/LocaleProvider";

const PM_TIMELINE_PAGE_SIZE = 6;

export function PMEvolutionPage() {
  const { t } = useLocale();
  const { target = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const listQuery = usePmConsolePmDecisionsQuery(target);
  const decisions = listQuery.data?.decisions ?? [];
  const requestedDecisionId = searchParams.get("decision")?.trim() ?? "";
  const requestedDecision = decisions.find(
    (decision) => decision.decision_id === requestedDecisionId,
  );
  const selectedId = requestedDecision?.decision_id || decisions.at(-1)?.decision_id || "";
  const requestedPage = Number.parseInt(searchParams.get("page")?.trim() ?? "1", 10);
  const detailQuery = usePmConsolePmDecisionDetailQuery(target, selectedId);

  const orderedDecisions = useMemo(() => decisions.slice().reverse(), [decisions]);
  const pageCount = Math.max(1, Math.ceil(orderedDecisions.length / PM_TIMELINE_PAGE_SIZE));
  const activePage = Number.isFinite(requestedPage) && requestedPage >= 1
    ? Math.min(requestedPage, pageCount)
    : 1;
  const pagedDecisions = orderedDecisions.slice(
    (activePage - 1) * PM_TIMELINE_PAGE_SIZE,
    activePage * PM_TIMELINE_PAGE_SIZE,
  );

  if (!target) return <div className="route-empty">{t("No target selected.")}</div>;
  if (listQuery.isLoading) return <div className="route-empty">{t("Loading PM Trajectory...")}</div>;
  if (listQuery.isError) {
    return <div className="route-empty">{t("PM Trajectory could not be loaded for")} `{target}`.</div>;
  }
  if (!decisions.length) {
    return (
      <div className="page">
        <section className="hero-card thesis-hero-card">
          <div className="hero-main thesis-hero-main">
            <p className="eyebrow">{t("PM Trajectory")}</p>
            <h2>{target.toUpperCase()}</h2>
            <p className="hero-copy">{t("No persisted PM decisions are available for this target.")}</p>
          </div>
        </section>
      </div>
    );
  }
  if (detailQuery.isLoading || !detailQuery.data) {
    return <div className="route-empty">{t("Loading selected PM Trajectory decision...")}</div>;
  }
  if (detailQuery.isError) {
    return <div className="route-empty">{t("Selected PM decision detail is unavailable.")}</div>;
  }

  const detail = detailQuery.data.decision;
  const selectedSummary = detail.summary;

  function setParamPatch(patch: Record<string, string | null>) {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(patch)) {
      if (value === null || value.length === 0) next.delete(key);
      else next.set(key, value);
    }
    setSearchParams(next);
  }

  function selectDecision(decisionId: string) {
    setParamPatch({ decision: decisionId, page: activePage > 1 ? String(activePage) : null });
  }

  return (
    <div className="page pm-trajectory-page">
      <section className="hero-card thesis-hero-card">
        <div className="hero-main thesis-hero-main">
          <p className="eyebrow">{t("PM Trajectory")}</p>
          <h2>{target.toUpperCase()}</h2>
          <div className="hero-meta-row">
            <span className="hero-meta-pill">{t("Decision record")}</span>
            <span className="hero-meta-pill">{formatState(selectedSummary.requested_state)}</span>
            <span className="hero-meta-pill">{formatTimestamp(selectedSummary.business_at)}</span>
          </div>
        </div>
        <div className="hero-status-panel thesis-hero-status-panel">
          <div className="hero-statuses thesis-hero-statuses">
            <MetricBadge label={t("Decision Count")} value={String(decisions.length)} />
            <MetricBadge label={t("Timeline Page")} value={`${activePage}/${pageCount}`} />
            <MetricBadge label={t("Requested Weight")} value={formatWeight(selectedSummary.requested_target_weight)} />
            <MetricBadge label={t("Outcome")} value={formatState(selectedSummary.outcome_status)} />
          </div>
        </div>
      </section>

      <section className="thesis-layout pm-trajectory-layout" aria-label={t("PM Trajectory")}>
        <aside className="side-column thesis-timeline-column">
          <div className="side-panel thesis-summary-panel">
            <div className="card-header">
              <div>
                <p className="eyebrow">{t("Timeline")}</p>
                <h3>{t("Decision History")}</h3>
              </div>
              <span className="timeline-count">{decisions.length} {t("total decisions")}</span>
            </div>
            <div className="revision-list">
              {pagedDecisions.map((decision) => (
                <DecisionTimelineItem
                  key={decision.decision_id}
                  decision={decision}
                  selected={decision.decision_id === selectedId}
                  onSelect={() => selectDecision(decision.decision_id)}
                />
              ))}
            </div>
            <div className="timeline-pagination">
              <button
                type="button"
                className="thesis-mini-button"
                onClick={() => setParamPatch({ page: String(activePage - 1) })}
                disabled={activePage <= 1}
              >
                {t("Previous")}
              </button>
              <span className="timeline-page-label">{t("Page")} {activePage} {t("of")} {pageCount}</span>
              <button
                type="button"
                className="thesis-mini-button"
                onClick={() => setParamPatch({ page: String(activePage + 1) })}
                disabled={activePage >= pageCount}
              >
                {t("Next")}
              </button>
            </div>
          </div>
        </aside>

        <main className="thesis-main-column">
          <DecisionSummary detail={detail} />
          <DecisionRationale detail={detail} />
          <DecisionContext detail={detail} />
        </main>

        <aside className="side-column">
          <DecisionTrace target={target} detail={detail} />
        </aside>
      </section>
    </div>
  );
}

function DecisionTimelineItem({
  decision,
  selected,
  onSelect,
}: {
  decision: PMConsolePMDecisionSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className={selected ? "revision-link active" : "revision-link"}>
      <button type="button" className="revision-select-button" onClick={onSelect}>
        <span className="revision-link-top">
          <strong>{formatTimestamp(decision.business_at)}</strong>
          <span>{formatState(decision.requested_state)} {formatWeight(decision.requested_target_weight)}</span>
        </span>
        <span className="revision-link-bottom">
          {decision.review_reasons.length
            ? decision.review_reasons.map(formatState).join(", ")
            : decision.execution_required ? "Execution requested" : "No execution required"}
        </span>
      </button>
      <div className="revision-actions">
        <span className="revision-badge">{selected ? "Selected" : formatState(decision.outcome_status)}</span>
        <span className={`status-pill ${statusTone(decision.outcome_status)}`}>
          {formatState(decision.outcome_status)}
        </span>
      </div>
    </article>
  );
}

function DecisionSummary({ detail }: { detail: PMConsolePMDecisionDetail }) {
  const { t } = useLocale();
  const summary = detail.summary;
  const agreement = detail.analysis_assessment
    ? detail.analysis_assessment.target_weight === summary.requested_target_weight
    : null;
  return (
    <div className="side-panel">
      <div className="card-header">
        <div>
          <p className="eyebrow">{t("Selected Decision")}</p>
          <h3>{t("Decision Summary")}</h3>
        </div>
        <span className={`status-pill ${statusTone(summary.outcome_status)}`}>
          {formatState(summary.outcome_status)}
        </span>
      </div>
      <div className="metric-row thesis-summary-row">
        <MetricTag label={t("Decision At")} value={formatTimestamp(summary.business_at)} />
        <MetricTag label={t("Actual Before")} value={`${formatState(summary.actual_state_before_decision)} ${formatWeight(summary.actual_target_weight_before_decision)}`} />
        <MetricTag label={t("PM Request")} value={`${formatState(summary.requested_state)} ${formatWeight(summary.requested_target_weight)}`} />
        <MetricTag label={t("Execution")} value={summary.execution_required ? formatState(detail.execution_status.code) : t("Not required")} />
      </div>
      <div className="pm-transition-card">
        <div>
          <span className="eyebrow">{t("Position transition")}</span>
          <strong>{formatState(summary.actual_state_before_decision)} {formatWeight(summary.actual_target_weight_before_decision)}</strong>
        </div>
        <span className="pm-transition-arrow" aria-hidden="true">→</span>
        <div>
          <span className="eyebrow">{t("Requested by PM")}</span>
          <strong>{formatState(summary.requested_state)} {formatWeight(summary.requested_target_weight)}</strong>
        </div>
        <span className={`status-pill ${agreement === null ? "" : agreement ? "ready" : "partial"}`}>
          {agreement === null ? t("No analysis comparison") : agreement ? t("Aligned") : t("Adjusted by PM")}
        </span>
      </div>
    </div>
  );
}

function DecisionRationale({ detail }: { detail: PMConsolePMDecisionDetail }) {
  const { t } = useLocale();
  return (
    <div className="side-panel">
      <div className="card-header">
        <div>
          <p className="eyebrow">{t("Decision Record")}</p>
          <h3>{t("Why This Decision")}</h3>
        </div>
        <span className="card-header-note">{t("Persisted PM rationale")}</span>
      </div>
      <div className="reader-stack">
        <article className="reader-card pm-reader-card">
          <div className="reader-section-header">
            <div>
              <strong>{detail.review_source ?? t("PM review source unavailable")}</strong>
              <span>{detail.review_reasons.length ? detail.review_reasons.map(formatState).join(" · ") : t("No persisted review reasons")}</span>
            </div>
          </div>
          <div className="reader-prose pm-rationale-reader">
            <MarkdownContent content={detail.rationale_md} className="pm-rationale-markdown" />
          </div>
          {detail.fallback_rationale_md ? (
            <div className="pm-fallback-block">
              <p className="eyebrow">{t("Fallback rationale")} · {formatState(detail.fallback_state_if_clamped ?? "unknown")}</p>
              <MarkdownContent content={detail.fallback_rationale_md} className="pm-rationale-markdown" />
            </div>
          ) : null}
        </article>
      </div>
    </div>
  );
}

function DecisionContext({ detail }: { detail: PMConsolePMDecisionDetail }) {
  const { t } = useLocale();
  return (
    <div className="side-panel">
      <div className="card-header">
        <div>
          <p className="eyebrow">{t("Decision Context")}</p>
          <h3>{t("Analysis Conditional View")}</h3>
        </div>
      </div>
      {detail.analysis_assessment ? (
        <div className="reader-stack">
          <article className="reader-card pm-reader-card pm-context-card">
            <div className="reader-section-header">
              <div>
                <strong>{formatState(detail.analysis_assessment.as_if_flat_state)} {formatWeight(detail.analysis_assessment.target_weight)}</strong>
                <span>{t("Persisted AnalysisAssessment at")} {formatTimestamp(detail.analysis_assessment.business_at)}</span>
              </div>
            </div>
            <div className="reader-prose pm-rationale-reader">
              <MarkdownContent content={detail.analysis_assessment.as_if_flat_rationale_md} className="pm-rationale-markdown" />
            </div>
          </article>
        </div>
      ) : (
        <p className="panel-copy">{t("No linked AnalysisAssessment is available for this PM decision.")}</p>
      )}
    </div>
  );
}

function DecisionTrace({ target, detail }: { target: string; detail: PMConsolePMDecisionDetail }) {
  const { t } = useLocale();
  const summary = detail.summary;
  const positionHref = detail.execution?.execution_record_id
    ? `/targets/${encodeURIComponent(target)}/position?marker=pm:${encodeURIComponent(detail.execution.execution_record_id)}`
    : null;
  return (
    <div className="side-panel">
      <div className="card-header">
        <div>
          <p className="eyebrow">{t("Linkage")}</p>
          <h3>{t("Decision Trace")}</h3>
        </div>
      </div>
      <div className="compact-stack">
        <ReferenceCard icon={<BookOpen size={15} />} label={t("Thesis anchor")} reference={detail.thesis_revision} />
        <ReferenceCard icon={<ClipboardList size={15} />} label={t("Analysis evidence")} reference={detail.analysis} />
        <ReferenceCard icon={<ShieldCheck size={15} />} label={t("PM rationale")} reference={detail.review_request} />
        <ReferenceCard icon={<ExternalLink size={15} />} label={t("Execution result")} reference={{ reference_id: detail.execution?.execution_record_id ?? null, href: null, status: detail.execution_status }} />
      </div>
      <div className="pm-trace-actions">
        {detail.thesis_revision.href ? <Link className="icon-text-link" to={detail.thesis_revision.href}><BookOpen size={15} /> {t("Thesis anchor")}</Link> : null}
        {detail.analysis.href ? <Link className="icon-text-link" to={detail.analysis.href}><ClipboardList size={15} /> {t("Analysis record")}</Link> : null}
        {positionHref ? <Link className="icon-text-link" to={positionHref}><ExternalLink size={15} /> {t("Position marker")}</Link> : null}
      </div>
      <details className="audit-details">
        <summary>{t("Audit identifiers")}</summary>
        <div className="audit-details-body">
          <MetricTag label="PM decision" value={summary.decision_id} />
          <MetricTag label="Decision episode" value={summary.decision_episode_id} />
          <MetricTag label="PM review" value={detail.review_request.reference_id ?? "None"} />
          <MetricTag label="Analysis" value={detail.analysis.reference_id ?? "None"} />
          <MetricTag label="Thesis" value={detail.thesis_revision.reference_id ?? "None"} />
          <TokenRow label="Source event IDs" values={detail.source_event_ids} />
        </div>
      </details>
    </div>
  );
}

function ReferenceCard({
  icon,
  label,
  reference,
}: {
  icon: ReactNode;
  label: string;
  reference: PMConsoleReference;
}) {
  return (
    <div className="mini-definition-card pm-reference-card">
      <span className="pm-reference-heading">{icon}<span>{label}</span></span>
      {reference.href && reference.reference_id ? <Link to={reference.href}>{reference.reference_id}</Link> : <strong>{reference.reference_id ?? formatState(reference.status.code)}</strong>}
      <small>{reference.status.explanation}</small>
    </div>
  );
}

function MetricBadge({ label, value }: { label: string; value: string }) {
  return <div className="metric-badge"><span>{label}</span><strong>{value}</strong></div>;
}

function MetricTag({ label, value }: { label: string; value: string }) {
  return <div className="mini-definition-card"><span className="mini-definition-title">{label}</span><span className="mini-definition-value">{value}</span></div>;
}

function TokenRow({ label, values }: { label: string; values: string[] }) {
  return (
    <div className="token-row">
      <span className="token-row-label">{label}</span>
      {values.length ? <div className="token-list">{values.map((value) => <code key={value} className="token-chip">{value}</code>)}</div> : <p className="muted token-row-empty">None</p>}
    </div>
  );
}

function formatWeight(value: number) {
  return `${(value * 100).toFixed(0)}%`;
}

function formatState(value: string) {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatTimestamp(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
    hourCycle: "h23",
  }).format(new Date(value));
}

function statusTone(value: string) {
  const normalized = value.toLowerCase();
  if (normalized.includes("error") || normalized.includes("unavailable") || normalized.includes("failed")) return "unavailable";
  if (normalized.includes("partial") || normalized.includes("adjusted")) return "partial";
  if (normalized.includes("executed") || normalized.includes("ready") || normalized.includes("aligned")) return "ready";
  return "";
}
