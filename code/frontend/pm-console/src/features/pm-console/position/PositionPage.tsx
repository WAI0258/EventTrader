import { useCallback, useMemo } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { MarkdownContent } from "../../../components/MarkdownContent";
import { StatusPill } from "../../../components/StatusPill";
import type { PMConsoleMarker, PMConsolePMDecisionDetail, PMConsoleStatus } from "../../../types";
import { PositionChartSection } from "./components/PositionChartSection";
import { PositionHero } from "./components/PositionHero";
import { PositionSummary } from "./components/PositionSummary";
import { PositionSupportingState } from "./components/PositionSupportingState";
import {
  usePmConsoleAnalysisAssessmentQuery,
  usePmConsolePmDecisionDetailQuery,
} from "../hooks";
import { usePositionPageModel } from "./usePositionPageModel";
import { formatStateLabel, formatTimestamp, formatWeight } from "./presentation";
import { useLocale } from "../locale/LocaleProvider";

export function PositionPage() {
  const { t } = useLocale();
  const { target } = useParams();
  const targetKey = target ?? "";
  const model = usePositionPageModel(targetKey);

  if (model.state === "missing_target") {
    return <div className="route-empty">{t("No target selected.")}</div>;
  }

  if (model.state === "loading") {
    return <div className="route-empty">{t("Loading position view...")}</div>;
  }

  if (model.state === "error") {
    return (
      <div className="route-empty">
        <p>{t("Position data could not be loaded for")} `{targetKey}`.</p>
      </div>
    );
  }

  if (model.state !== "ready") {
    return <div className="route-empty">{t("Position view is unavailable.")}</div>;
  }

  return <PositionPageReady targetKey={targetKey} model={model} />;
}

function PositionPageReady({
  targetKey,
  model,
}: {
  targetKey: string;
  model: Extract<ReturnType<typeof usePositionPageModel>, { state: "ready" }>;
}) {
  const { t } = useLocale();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedMarkerId = searchParams.get("marker");
  const selectedMarker =
    model.data.markers.find((marker) => marker.marker_id === selectedMarkerId) ?? null;
  const hasSelectedMarker = selectedMarkerId !== null;
  const selectedMarkers = useMemo(() => {
    if (!selectedMarker) {
      return [];
    }
    return model.data.markers.filter(
      (marker) => marker.time === selectedMarker.time,
    );
  }, [model.data.markers, selectedMarker]);
  const onMarkerSelect = useCallback(
    (markers: typeof model.data.markers) => {
      const marker = markers[0];
      if (marker) {
        const next = new URLSearchParams(searchParams);
        next.set("marker", marker.marker_id);
        setSearchParams(next);
      }
    },
    [searchParams, setSearchParams],
  );
  const selectedDecisionId = selectedMarker?.pm_decision_id ?? "";
  const decisionQuery = usePmConsolePmDecisionDetailQuery(
    targetKey,
    selectedDecisionId,
  );
  const selectedAssessmentId = selectedMarker?.analysis_assessment_id ?? "";
  const analysisQuery = usePmConsoleAnalysisAssessmentQuery(
    targetKey,
    selectedAssessmentId,
  );

  const {
    data,
    pmLine,
    buyHoldLine,
    analysisDirectLine,
    pmReturn,
    buyHoldReturn,
    analysisDirectReturn,
    pmMaxDrawdown,
    interpretation,
  } = model;

  return (
    <div className="page">
      <PositionHero data={data} />
      <PositionSummary
        currentExposureExplanation={data.current_portfolio_state.status.explanation}
        currentExposureState={data.current_portfolio_state.state}
        currentExposureWeight={data.current_portfolio_state.target_weight}
        currentExposureStatus={data.current_portfolio_state.status}
        interpretation={interpretation}
        pmReturn={pmReturn}
        buyHoldReturn={buyHoldReturn}
        analysisDirectReturn={analysisDirectReturn}
        pmMaxDrawdown={pmMaxDrawdown}
      />
      <section className="position-workspace" aria-label={t("Position chart and selected decision")}>
        <PositionChartSection
          marketSymbol={data.market_symbol}
          targetKey={data.target_key}
          barGranularity={data.bar_granularity}
          priceDisplayDecimals={data.price_display_decimals}
          points={data.points}
          markers={data.markers}
          selectedMarkers={selectedMarkers}
          showSelectionHint={!hasSelectedMarker}
          onMarkerSelect={onMarkerSelect}
        />
        {hasSelectedMarker ? (
          <section className="position-inspection-drawer" aria-label="Selected decision detail">
            <MarkerDetailPanel
              marker={selectedMarker}
              markerNotFound={selectedMarker === null}
              decisionQuery={decisionQuery}
              analysisQuery={analysisQuery}
            />
          </section>
        ) : null}
      </section>
      <PositionSupportingState
        data={data}
        pmLine={pmLine}
        buyHoldLine={buyHoldLine}
        analysisDirectLine={analysisDirectLine}
        pmReturn={pmReturn}
        buyHoldReturn={buyHoldReturn}
        analysisDirectReturn={analysisDirectReturn}
      />
    </div>
  );
}

function MarkerDetailPanel({
  marker,
  markerNotFound,
  decisionQuery,
  analysisQuery,
}: {
  marker: PMConsoleMarker | null;
  markerNotFound: boolean;
  decisionQuery: ReturnType<typeof usePmConsolePmDecisionDetailQuery>;
  analysisQuery: ReturnType<typeof usePmConsoleAnalysisAssessmentQuery>;
}) {
  const { t } = useLocale();
  if (markerNotFound) {
    return (
      <div className="marker-detail-panel status-error">
        {t("The selected chart marker is not available in this workspace snapshot.")}
      </div>
    );
  }
  if (!marker) {
    return (
      <div className="marker-detail-placeholder">
        <p className="eyebrow">{t("Decision detail")}</p>
        <h4>{t("Select a PM or Analysis marker")}</h4>
        <p className="muted">{t("Click a marker on the chart to inspect the persisted rationale, execution record, and linked context.")}</p>
      </div>
    );
  }
  if (marker.kind === "pm_decision") {
    if (decisionQuery.isLoading) {
      return <div className="marker-detail-panel muted">{t("Loading PM decision...")}</div>;
    }
    if (decisionQuery.isError || !decisionQuery.data) {
      return (
        <div className="marker-detail-panel status-error">
          {t("PM decision detail is unavailable for this marker.")}
        </div>
      );
    }
    const detail = decisionQuery.data.decision;
    return (
      <section className="marker-detail-panel">
        <div className="card-header-inline">
          <div>
            <p className="eyebrow">{t("Selected PM decision")}</p>
            <h4>{detail.summary.requested_state} {t("decision")}</h4>
          </div>
          <span className="status-pill ready">{detail.summary.outcome_status}</span>
        </div>
        <div className="decision-transition-row">
          <span>
            {formatStateLabel(detail.summary.actual_state_before_decision)} {formatWeight(detail.summary.actual_target_weight_before_decision)}
          </span>
          <span aria-hidden="true">→</span>
          <strong>
            {formatStateLabel(detail.summary.requested_state)} {formatWeight(detail.summary.requested_target_weight)}
          </strong>
        </div>
        <div className="pm-detail-grid">
          <DetailField label={t("Decision time")} value={formatTimestamp(detail.summary.business_at)} />
          <DetailField label={t("PMReview source")} value={detail.review_source ?? t("Not recorded")} />
          <DetailField
            label={t("PMReview reasons")}
            value={detail.review_reasons.length ? detail.review_reasons.join(", ") : t("None recorded")}
          />
        </div>
        <div className="markdown-detail">
          <h5>{t("PM rationale")}</h5>
          <MarkdownContent content={detail.rationale_md} className="marker-markdown" />
        </div>
        <ExecutionDetail detail={detail} />
        <DiagnosticStatusGrid
          statuses={[
            { label: t("Execution"), status: detail.execution_status },
            { label: t("Analysis link"), status: detail.analysis.status },
            { label: t("Thesis link"), status: detail.thesis_revision.status },
          ]}
        />
        {detail.fallback_rationale_md ? (
          <div className="markdown-detail">
            <h5>{t("Fallback")}: {detail.fallback_state_if_clamped}</h5>
            <MarkdownContent
              content={detail.fallback_rationale_md}
              className="marker-markdown"
            />
          </div>
        ) : null}
        <div className="detail-link-row">
          <Link
            className="pm-detail-link"
            to={`/targets/${encodeURIComponent(detail.summary.target_key)}/pm?decision=${encodeURIComponent(detail.summary.decision_id)}`}
          >
            {t("Open PM Trajectory")}
          </Link>
          {detail.thesis_revision.href ? (
            <Link className="thesis-detail-link" to={detail.thesis_revision.href}>
              {t("Open linked Thesis revision")}
            </Link>
          ) : null}
        </div>
      </section>
    );
  }
  if (marker.kind === "analysis_assessment") {
    if (analysisQuery.isLoading) {
      return <div className="marker-detail-panel muted">{t("Loading Analysis reasoning...")}</div>;
    }
    if (analysisQuery.isError || !analysisQuery.data) {
      return (
        <div className="marker-detail-panel status-error">
          {t("Analysis detail is unavailable for this marker.")}
        </div>
      );
    }
    const detail = analysisQuery.data;
    return (
      <section className="marker-detail-panel">
        <div className="card-header-inline">
          <div>
            <p className="eyebrow">{t("Selected Analysis Direct mapping")}</p>
            <h4>{detail.as_if_flat_state} {formatWeight(detail.target_weight)}</h4>
          </div>
          <span className="status-pill ready">{t("Assessment")}</span>
        </div>
        <div className="pm-detail-grid">
          <DetailField label={t("Assessment time")} value={formatTimestamp(detail.business_at)} />
          <DetailField label={t("Assessment ID")} value={detail.assessment_id} />
          <DetailField label={t("Mapping")} value={t("Analysis Direct; not execution")} />
        </div>
        <p className="muted">{t("Canonical AnalysisAssessment state and target weight. This line is a comparison mapping, not an executed position.")}</p>
        <div className="markdown-detail">
          <h5>{t("Analysis rationale")}</h5>
          <MarkdownContent content={detail.as_if_flat_rationale_md} className="marker-markdown" />
        </div>
        {detail.thesis_anchor.href ? (
          <div className="detail-link-row">
            <Link className="thesis-detail-link" to={detail.thesis_anchor.href}>
              {t("Open linked Thesis revision")}
            </Link>
          </div>
        ) : (
          <DiagnosticStatusGrid
            statuses={[{ label: t("Thesis relationship"), status: detail.thesis_anchor.status }]}
          />
        )}
      </section>
    );
  }
  return null;
}

function ExecutionDetail({ detail }: { detail: PMConsolePMDecisionDetail }) {
  const { t } = useLocale();
  const execution = detail.execution;
  return (
    <section className="execution-detail" aria-label={t("Execution record")}>
      <div className="detail-section-heading">
        <div>
          <p className="eyebrow">{t("Execution record")}</p>
          <h5>{t("Persisted execution outcome")}</h5>
        </div>
        <StatusPill status={detail.execution_status} />
      </div>
      {execution ? (
        <div className="pm-detail-grid">
          <DetailField label={t("Record ID")} value={execution.execution_record_id} />
          <DetailField label={t("Execution status")} value={execution.status} />
          <DetailField label={t("Executed at")} value={formatTimestamp(execution.executed_at)} />
          <DetailField label={t("Executed target weight")} value={formatWeight(execution.target_weight)} />
          <DetailField
            label={t("Adjusted execution price")}
            value={execution.adjusted_price === null || execution.adjusted_price === undefined ? t("Not recorded") : String(execution.adjusted_price)}
          />
          <DetailField label={t("Rejection reason")} value={execution.rejection_reason ?? t("None recorded")} />
        </div>
      ) : (
        <p className="muted">{t("No execution record is persisted for this decision.")}</p>
      )}
    </section>
  );
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div className="detail-block">
      <span className="eyebrow">{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function DetailStatus({ label, status }: { label: string; status: { code: string; explanation: string } }) {
  return (
    <div className="detail-status">
      <span className="eyebrow">{label}</span>
      <strong>{formatStatusCode(status.code)}</strong>
      <span>{status.explanation}</span>
    </div>
  );
}

function DiagnosticStatusGrid({
  statuses,
}: {
  statuses: Array<{ label: string; status: PMConsoleStatus }>;
}) {
  const diagnostics = statuses.filter(({ status }) => isDiagnosticStatus(status.code));
  if (diagnostics.length === 0) {
    return null;
  }
  return (
    <div className="marker-detail-grid">
      {diagnostics.map(({ label, status }) => (
        <DetailStatus key={label} label={label} status={status} />
      ))}
    </div>
  );
}

function isDiagnosticStatus(code: string) {
  return !new Set([
    "available",
    "direct_thesis_anchor",
    "effective_thesis_anchor",
    "executed",
    "no_op",
    "rejected",
  ]).has(code);
}

function formatStatusCode(value: string) {
  const labels: Record<string, string> = {
    direct_thesis_anchor: "Direct Thesis link",
    effective_thesis_anchor: "Effective Thesis link",
    integrity_missing_thesis_anchor: "Thesis link missing",
    missing_analysis_assessment: "Analysis record missing",
    missing_pm_review_request: "PM review missing",
    missing_execution: "Execution not recorded",
  };
  return labels[value] ?? value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
