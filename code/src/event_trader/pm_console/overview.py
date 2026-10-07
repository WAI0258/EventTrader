"""Compact, market-data-free PM Console overview read model."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from event_trader.execution.contracts import ExecutionRecord
from event_trader.execution.store import ExecutionRecordStore
from event_trader.operator_context import OperatorContextService
from event_trader.pm_console.schemas import (
    PMConsoleCurrentPortfolioStateDTO,
    PMConsoleExecutionDTO,
    PMConsoleOverviewResponseDTO,
    PMConsoleOverviewTargetDTO,
    PMConsolePMDecisionSummaryDTO,
    PMConsoleStatusDTO,
    PMConsoleThesisRevisionSummaryDTO,
)
from event_trader.pm_console.thesis import (
    default_pm_console_target_view,
    map_pm_console_target_views,
)
from event_trader.portfolio.contracts import PMDecision, PortfolioState
from event_trader.portfolio.store import PMDecisionStore, PortfolioStateStore
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.store import (
    PersistedThesisRevision,
    ThesisRevisionStore,
    sort_persisted_thesis_revisions,
)


def build_overview_target(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    runtime_mode: str,
) -> PMConsoleOverviewTargetDTO:
    """Read one target's persisted PM facts without constructing market surfaces."""

    errors: list[str] = []
    problem_codes: list[str] = []

    portfolio_state: PortfolioState | None = None
    try:
        portfolio_state = PortfolioStateStore(layout).read(target_key=target_key)
    except Exception as exc:  # isolate one persisted surface to this target row
        errors.append(f"portfolio_state_read_error: {exc}")
        problem_codes.append("portfolio_state_read_error")

    decisions: tuple[PMDecision, ...] = ()
    try:
        decisions = tuple(
            persisted.record
            for persisted in PMDecisionStore(layout).read_records(target_key=target_key)
        )
    except Exception as exc:  # isolate one persisted surface to this target row
        errors.append(f"pm_decision_read_error: {exc}")
        problem_codes.append("pm_decision_read_error")

    executions: tuple[ExecutionRecord, ...] = ()
    try:
        executions = tuple(
            persisted.record
            for persisted in ExecutionRecordStore(layout).read_records(target_key=target_key)
        )
    except Exception as exc:  # isolate one persisted surface to this target row
        errors.append(f"execution_read_error: {exc}")
        problem_codes.append("execution_read_error")

    latest_revision: PersistedThesisRevision | None = None
    try:
        revisions = sort_persisted_thesis_revisions(
            ThesisRevisionStore(layout).read_records(target_key=target_key)
        )
        latest_revision = revisions[-1] if revisions else None
    except Exception as exc:  # isolate one persisted surface to this target row
        errors.append(f"thesis_revision_read_error: {exc}")
        problem_codes.append("thesis_revision_read_error")

    operator_status = PMConsoleStatusDTO(
        code="unavailable",
        explanation="Operator context could not be read.",
    )
    try:
        operator_status = _operator_status(OperatorContextService(layout).read(target_key))
    except Exception as exc:  # isolate one persisted surface to this target row
        errors.append(f"operator_context_read_error: {exc}")
        problem_codes.append("operator_context_read_error")

    latest_decision = _latest_decision(decisions)
    matching_executions = _matching_executions(executions, latest_decision)
    latest_execution = _latest_matching_execution(matching_executions)
    execution_status = _execution_status(
        latest_decision,
        matching_executions,
        latest_execution,
    )
    if latest_decision is None:
        problem_codes.append("missing_pm_decision")
    if portfolio_state is None:
        problem_codes.append("missing_portfolio_state")
    if latest_revision is None:
        problem_codes.append("missing_thesis_revision")
    if operator_status.code in {"missing", "empty"}:
        problem_codes.append("missing_operator_context")
    if execution_status.code == "pending":
        problem_codes.append("pending_execution")
    if execution_status.code == "ambiguous":
        problem_codes.append("ambiguous_execution_records")

    try:
        views = map_pm_console_target_views(layout).get(target_key, ("operator",))
    except Exception:
        views = ("operator",)
    readiness = _readiness(
        errors=errors,
        problem_codes=problem_codes,
        execution_status=execution_status,
    )
    if errors:
        readiness = PMConsoleStatusDTO(
            code="unavailable",
            explanation="One or more persisted PM Console read surfaces failed.",
        )

    return PMConsoleOverviewTargetDTO(
        target_key=target_key,
        runtime_mode=runtime_mode,
        workspace_root=str(layout.root),
        workspace_name=layout.root.name or str(layout.root),
        implemented_views=views,
        default_view=default_pm_console_target_view(views),
        readiness=readiness,
        problem_codes=tuple(dict.fromkeys(problem_codes)),
        current_portfolio_state=(
            None if portfolio_state is None else _portfolio_state_dto(portfolio_state)
        ),
        latest_pm_decision=(
            None if latest_decision is None else _decision_dto(latest_decision, execution_status)
        ),
        latest_execution=(
            None if latest_execution is None else _execution_dto(latest_execution)
        ),
        execution_status=execution_status,
        latest_thesis_revision=(
            None if latest_revision is None else _thesis_revision_dto(latest_revision)
        ),
        operator_status=operator_status,
    )


def build_unavailable_overview_target(
    *,
    target_key: str,
    layout: WorkspaceLayout,
    runtime_mode: str,
    error: Exception,
) -> PMConsoleOverviewTargetDTO:
    """Create a truthful fallback row for an unexpected target-level failure."""

    try:
        views = map_pm_console_target_views(layout).get(target_key, ("operator",))
    except Exception:
        views = ("operator",)
    return PMConsoleOverviewTargetDTO(
        target_key=target_key,
        runtime_mode=runtime_mode,
        workspace_root=str(layout.root),
        workspace_name=layout.root.name or str(layout.root),
        implemented_views=views,
        default_view=default_pm_console_target_view(views),
        readiness=PMConsoleStatusDTO(
            code="unavailable",
            explanation="The target overview could not be assembled.",
        ),
        problem_codes=("overview_read_error",),
        current_portfolio_state=None,
        latest_pm_decision=None,
        latest_execution=None,
        execution_status=PMConsoleStatusDTO(
            code="no_decision",
            explanation="Execution status is unavailable because the target overview read failed.",
        ),
        latest_thesis_revision=None,
        operator_status=PMConsoleStatusDTO(
            code="unavailable",
            explanation=f"Target overview read failed: {error}",
        ),
    )


def build_overview_response(
    targets: Iterable[PMConsoleOverviewTargetDTO],
    *,
    mounted_target_count: int,
    reload_error: str | None = None,
) -> PMConsoleOverviewResponseDTO:
    """Build the stable, sorted overview response envelope."""

    return PMConsoleOverviewResponseDTO(
        generated_at=datetime.now(UTC).isoformat(),
        mounted_target_count=mounted_target_count,
        targets=tuple(sorted(targets, key=lambda target: target.target_key)),
        reload_error=reload_error,
    )


def _latest_decision(decisions: Iterable[PMDecision]) -> PMDecision | None:
    ordered = sorted(
        decisions,
        key=lambda decision: (
            decision.business_at,
            decision.decision_available_at,
            decision.decision_id,
        ),
    )
    return ordered[-1] if ordered else None


def _matching_executions(
    executions: Iterable[ExecutionRecord],
    decision: PMDecision | None,
) -> tuple[ExecutionRecord, ...]:
    if decision is None:
        return ()
    return tuple(
        execution
        for execution in executions
        if execution.pm_decision_id == decision.decision_id
    )


def _latest_matching_execution(
    matching_executions: Iterable[ExecutionRecord],
) -> ExecutionRecord | None:
    matching = tuple(matching_executions)
    if len(matching) != 1:
        return None
    return matching[0]


def _execution_status(
    decision: PMDecision | None,
    matching_executions: Iterable[ExecutionRecord],
    execution: ExecutionRecord | None,
) -> PMConsoleStatusDTO:
    if decision is None:
        return PMConsoleStatusDTO("no_decision", "No PM decision is persisted for this target.")
    if len(tuple(matching_executions)) > 1:
        return PMConsoleStatusDTO(
            "ambiguous",
            "Multiple persisted execution records reference the latest PM decision.",
        )
    if execution is not None:
        return PMConsoleStatusDTO(
            execution.status,
            "The latest PM decision has a matching terminal execution record.",
        )
    if not decision.execution_required:
        return PMConsoleStatusDTO("no_op", "The latest PM decision did not require execution.")
    return PMConsoleStatusDTO(
        "pending",
        "The latest PM decision requires execution but has no matching execution record.",
    )


def _readiness(
    *,
    errors: list[str],
    problem_codes: list[str],
    execution_status: PMConsoleStatusDTO,
) -> PMConsoleStatusDTO:
    if errors:
        return PMConsoleStatusDTO("unavailable", "One or more persisted PM Console reads failed.")
    if execution_status.code == "pending":
        return PMConsoleStatusDTO(
            "action_required",
            "A persisted PM decision has an unresolved execution obligation.",
        )
    if any(
        code.startswith("missing_") or code == "ambiguous_execution_records"
        for code in problem_codes
    ):
        return PMConsoleStatusDTO(
            "incomplete",
            "The target has persisted PM data, but its overview snapshot or setup is incomplete.",
        )
    return PMConsoleStatusDTO("ready", "Persisted PM state and setup facts are available.")


def _portfolio_state_dto(state: PortfolioState) -> PMConsoleCurrentPortfolioStateDTO:
    return PMConsoleCurrentPortfolioStateDTO(
        status=PMConsoleStatusDTO("available", "Current PortfolioState is persisted."),
        state=state.state,
        target_weight=state.target_weight,
        updated_at=state.updated_at.isoformat(),
        source_pm_decision_id=state.source_pm_decision_id,
        source_execution_record_id=state.source_execution_record_id,
        decision_episode_id=state.decision_episode_id,
    )


def _decision_dto(
    decision: PMDecision,
    execution_status: PMConsoleStatusDTO,
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
        outcome_status=execution_status.code,
        pm_review_request_id=decision.pm_review_request_id,
    )


def _execution_dto(execution: ExecutionRecord) -> PMConsoleExecutionDTO:
    return PMConsoleExecutionDTO(
        execution_record_id=execution.execution_record_id,
        business_at=execution.business_at.isoformat(),
        status=execution.status,
        pm_decision_id=execution.pm_decision_id,
        requested_target_weight=execution.requested_target_weight,
        executed_at=(None if execution.executed_at is None else execution.executed_at.isoformat()),
        target_weight=execution.target_weight,
        adjusted_price=execution.adjusted_price,
        rejection_reason=execution.rejection_reason,
    )


def _thesis_revision_dto(
    persisted: PersistedThesisRevision,
) -> PMConsoleThesisRevisionSummaryDTO:
    revision = persisted.record
    return PMConsoleThesisRevisionSummaryDTO(
        revision_id=revision.revision_id,
        target_key=revision.target_key,
        business_at=revision.business_at.isoformat(),
        committed_at=revision.committed_at.isoformat(),
        source=revision.source,
        source_event_count=len(revision.source_event_ids),
        changed_claim_count=len(revision.changed_claim_ids),
        changed_section_count=sum(
            1 for diff in revision.diffs_from_previous if diff.unified_diff_md
        ),
        previous_revision_id=revision.previous_revision_id,
        is_baseline=revision.source == "migration_baseline",
    )


def _operator_status(document) -> PMConsoleStatusDTO:
    explanations = {
        "ready": "Operator context is present for this target.",
        "empty": "Operator context is present but empty.",
        "missing": "No operator context page is present for this target.",
    }
    return PMConsoleStatusDTO(document.status, explanations[document.status])


__all__ = [
    "build_overview_response",
    "build_overview_target",
    "build_unavailable_overview_target",
]
