"""Workspace-backed PM decision read models for the PM Console."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote

from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.contracts.view_state_change import canonical_view_state_target_weight
from event_trader.execution.store import ExecutionRecordStore
from event_trader.pm_console.schemas import (
    PMConsoleAnalysisAssessmentDTO,
    PMConsoleExecutionDTO,
    PMConsolePMDecisionDetailDTO,
    PMConsolePMDecisionDetailResponseDTO,
    PMConsolePMDecisionsResponseDTO,
    PMConsolePMDecisionSummaryDTO,
    PMConsoleReferenceDTO,
    PMConsoleStatusDTO,
)
from event_trader.pm_console.thesis_anchor import resolve_thesis_anchor
from event_trader.pm_review.store import PMReviewRequestStore
from event_trader.portfolio.store import PMDecisionStore
from event_trader.storage import WorkspaceLayout


def build_pm_decisions_response(
    layout: WorkspaceLayout,
    *,
    target_key: str,
) -> PMConsolePMDecisionsResponseDTO:
    decisions = _read_decisions(layout, target_key=target_key)
    executions = _read_executions(layout, target_key=target_key)
    requests_by_id = {
        item.record.request_id: item.record
        for item in PMReviewRequestStore(layout).read_records(target_key=target_key)
    }
    assessments_by_id = {
        item.record.assessment_id: item.record
        for item in AnalysisAssessmentStore(layout).read_records(target_key=target_key)
    }
    summaries = tuple(
        _summary(
            persisted.record,
            outcome_status=_execution_status(persisted.record, executions),
            review_reasons=(
                ()
                if (request := requests_by_id.get(persisted.record.pm_review_request_id)) is None
                else tuple(request.review_reasons)
            ),
            assessment=(
                None
                if request is None or request.source_assessment_id is None
                else assessments_by_id.get(request.source_assessment_id)
            ),
        )
        for persisted in decisions
    )
    return PMConsolePMDecisionsResponseDTO(
        generated_at=datetime.now(UTC).isoformat(),
        target_key=target_key,
        decisions=summaries,
    )


def build_pm_decision_detail_response(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    decision_id: str,
) -> PMConsolePMDecisionDetailResponseDTO | None:
    decisions = _read_decisions(layout, target_key=target_key)
    decision = next(
        (item.record for item in decisions if item.record.decision_id == decision_id),
        None,
    )
    if decision is None:
        return None

    requests = PMReviewRequestStore(layout).read_records(target_key=target_key)
    request = next(
        (
            item.record
            for item in requests
            if item.record.request_id == decision.pm_review_request_id
        ),
        None,
    )
    executions = _read_executions(layout, target_key=target_key)
    matching_executions = tuple(
        item.record for item in executions if item.record.pm_decision_id == decision_id
    )
    execution = matching_executions[0] if len(matching_executions) == 1 else None
    execution_status = _execution_status_from_records(
        execution_required=decision.execution_required,
        matching_executions=matching_executions,
    )

    analysis_id = None if request is None else request.source_assessment_id
    assessment, assessment_status = _find_assessment(
        layout,
        target_key=target_key,
        assessment_id=analysis_id,
    )
    thesis_reference = (
        resolve_thesis_anchor(layout, target_key=target_key, assessment=assessment).reference
        if assessment is not None
        else _missing_thesis_anchor_reference()
    )

    summary = _summary(decision, outcome_status=execution_status.code)
    detail = PMConsolePMDecisionDetailDTO(
        summary=summary,
        review_source=None if request is None else request.source,
        review_reasons=() if request is None else tuple(request.review_reasons),
        rationale_md=decision.rationale_md,
        fallback_state_if_clamped=decision.fallback_state_if_clamped,
        fallback_rationale_md=decision.fallback_rationale_md,
        source_event_ids=tuple(decision.source_event_ids),
        review_request=_reference(
            reference_id=None if request is None else request.request_id,
            status=(
                _status("available", "Persisted PMReviewRequest is available.")
                if request is not None
                else _status(
                    "missing_review_request",
                    "The PMDecision references no available persisted PMReviewRequest.",
                )
            ),
            href=(
                f"/targets/{quote(target_key, safe='')}/pm?decision={quote(decision_id, safe='')}"
                if request is not None
                else None
            ),
        ),
        execution=(None if execution is None else _execution_dto(execution)),
        execution_status=execution_status,
        analysis=_reference(
            reference_id=analysis_id,
            status=assessment_status,
            href=None,
        ),
        analysis_assessment=(
            None
            if assessment is None
            else _assessment_dto(
                layout,
                target_key=target_key,
                assessment=assessment,
            )
        ),
        thesis_revision=thesis_reference,
    )
    return PMConsolePMDecisionDetailResponseDTO(
        generated_at=datetime.now(UTC).isoformat(),
        target_key=target_key,
        decision=detail,
    )


def list_pm_decision_targets(layout: WorkspaceLayout) -> tuple[str, ...]:
    root = layout.runtime_root / "portfolio" / "pm-decisions"
    if not root.is_dir():
        return ()
    return tuple(sorted(path.name for path in root.iterdir() if path.is_dir()))


def _read_decisions(layout: WorkspaceLayout, *, target_key: str):
    return tuple(
        sorted(
            PMDecisionStore(layout).read_records(target_key=target_key),
            key=lambda item: (
                item.record.business_at,
                item.record.decision_available_at,
                item.record.decision_id,
            ),
        )
    )


def _read_executions(layout: WorkspaceLayout, *, target_key: str):
    return tuple(
        sorted(
            ExecutionRecordStore(layout).read_records(target_key=target_key),
            key=lambda item: (
                item.record.business_at,
                item.record.recorded_at,
                item.record.execution_record_id,
            ),
        )
    )


def _summary(
    decision,
    *,
    outcome_status: str,
    review_reasons: tuple[str, ...] = (),
    assessment=None,
) -> PMConsolePMDecisionSummaryDTO:
    return PMConsolePMDecisionSummaryDTO(
        decision_id=decision.decision_id,
        decision_episode_id=decision.decision_episode_id,
        target_key=decision.target_key,
        business_at=decision.business_at.isoformat(),
        decision_available_at=decision.decision_available_at.isoformat(),
        actual_state_before_decision=decision.actual_state_before_decision,
        actual_target_weight_before_decision=decision.actual_target_weight_before_decision,
        requested_state=decision.requested_state,
        requested_target_weight=decision.requested_target_weight,
        execution_required=decision.execution_required,
        outcome_status=outcome_status,
        pm_review_request_id=decision.pm_review_request_id,
        review_reasons=review_reasons,
        analysis_conditional_state=(None if assessment is None else assessment.as_if_flat_state),
        analysis_conditional_target_weight=(
            None
            if assessment is None
            else canonical_view_state_target_weight(assessment.as_if_flat_state)
        ),
    )


def _execution_status(decision, executions) -> str:
    matching = tuple(
        item.record for item in executions if item.record.pm_decision_id == decision.decision_id
    )
    return _execution_status_from_records(
        execution_required=decision.execution_required,
        matching_executions=matching,
    ).code


def _execution_status_from_records(
    *,
    execution_required: bool,
    matching_executions,
) -> PMConsoleStatusDTO:
    if len(matching_executions) > 1:
        return _status(
            "ambiguous_multiple_execution_records",
            "Multiple persisted execution records reference this PMDecision.",
        )
    if matching_executions:
        record = matching_executions[0]
        return _status(
            "executed" if record.status == "executed" else "rejected",
            f"Persisted execution record status: {record.status}.",
        )
    if execution_required:
        return _status(
            "missing_execution_record",
            "The PMDecision requires execution but no execution record is available.",
        )
    return _status("no_op", "The PMDecision did not require an execution record.")


def _find_assessment(layout: WorkspaceLayout, *, target_key: str, assessment_id: str | None):
    if assessment_id is None:
        return None, _status(
            "unavailable_no_analysis_link",
            "This PMReview has no deterministic source AnalysisAssessment link.",
        )
    matches = tuple(
        item.record
        for item in AnalysisAssessmentStore(layout).read_records(target_key=target_key)
        if item.record.assessment_id == assessment_id
    )
    if len(matches) > 1:
        return None, _status(
            "ambiguous_analysis_link",
            "Multiple persisted AnalysisAssessment records match this source ID.",
        )
    if not matches:
        return None, _status(
            "missing_analysis_assessment",
            "The PMReview source AnalysisAssessment is not available in the workspace.",
        )
    return matches[0], _status("available", "Persisted AnalysisAssessment is available.")


def _assessment_dto(layout, *, target_key: str, assessment) -> PMConsoleAnalysisAssessmentDTO:
    from event_trader.contracts.view_state_change import canonical_view_state_target_weight

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
        thesis_anchor=resolve_thesis_anchor(
            layout,
            target_key=target_key,
            assessment=assessment,
        ).reference,
    )


def _missing_thesis_anchor_reference() -> object:
    from event_trader.pm_console.schemas import PMConsoleReferenceDTO

    return PMConsoleReferenceDTO(
        reference_id=None,
        href=None,
        status=_status(
            "integrity_missing_thesis_anchor",
            "This PM decision has no readable source AnalysisAssessment for Thesis anchoring.",
        ),
    )


def _execution_dto(record) -> PMConsoleExecutionDTO:
    return PMConsoleExecutionDTO(
        execution_record_id=record.execution_record_id,
        business_at=record.business_at.isoformat(),
        status=record.status,
        pm_decision_id=record.pm_decision_id,
        requested_target_weight=record.requested_target_weight,
        executed_at=None if record.executed_at is None else record.executed_at.isoformat(),
        target_weight=record.target_weight,
        adjusted_price=record.adjusted_price,
        rejection_reason=record.rejection_reason,
    )


def _reference(*, reference_id, status, href=None) -> PMConsoleReferenceDTO:
    return PMConsoleReferenceDTO(reference_id=reference_id, status=status, href=href)


def _status(code: str, explanation: str) -> PMConsoleStatusDTO:
    return PMConsoleStatusDTO(code=code, explanation=explanation)


__all__ = [
    "build_pm_decision_detail_response",
    "build_pm_decisions_response",
    "list_pm_decision_targets",
]
