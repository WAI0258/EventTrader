"""Read-only AnalysisAssessment detail for Position marker selection."""

from __future__ import annotations

from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.contracts.view_state_change import canonical_view_state_target_weight
from event_trader.pm_console.schemas import PMConsoleAnalysisAssessmentDTO
from event_trader.pm_console.thesis_anchor import resolve_thesis_anchor
from event_trader.storage import WorkspaceLayout


def build_analysis_assessment_detail(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    assessment_id: str,
) -> PMConsoleAnalysisAssessmentDTO | None:
    matches = tuple(
        item.record
        for item in AnalysisAssessmentStore(layout).read_records(target_key=target_key)
        if item.record.assessment_id == assessment_id
    )
    if len(matches) != 1:
        return None
    assessment = matches[0]
    thesis_anchor = resolve_thesis_anchor(
        layout,
        target_key=target_key,
        assessment=assessment,
    )
    return PMConsoleAnalysisAssessmentDTO(
        assessment_id=assessment.assessment_id,
        business_at=assessment.business_at.isoformat(),
        as_if_flat_state=assessment.as_if_flat_state,
        target_weight=canonical_view_state_target_weight(assessment.as_if_flat_state),
        as_if_flat_rationale_md=assessment.as_if_flat_rationale_md,
        source_event_ids=tuple(assessment.source_event_ids),
        if_flat_implication_md=assessment.if_flat_implication_md,
        if_already_long_implication_md=assessment.if_already_long_implication_md,
        if_already_short_implication_md=assessment.if_already_short_implication_md,
        market_setup_dashboard_md=assessment.market_setup_dashboard_md,
        missing_evidence_md=assessment.missing_evidence_md,
        key_claim_ids=tuple(assessment.key_claim_ids),
        contested_prior_claim_ids=tuple(assessment.contested_prior_claim_ids),
        pm_review_reasons=tuple(assessment.pm_review_reasons),
        confidence=assessment.confidence,
        thesis_anchor=thesis_anchor.reference,
    )


__all__ = ["build_analysis_assessment_detail"]
