"""Read-only runtime repair inspection for PM/execution lifecycle truth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.contracts._validators import validate_event_id, validate_target_key
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.price_level_role import MaterialLevelRole
from event_trader.contracts.view_state_change import ViewStateChange
from event_trader.execution.contracts import ExecutionRecord
from event_trader.execution.engine import PaperExecutionEngine
from event_trader.execution.store import ExecutionIntentStore, ExecutionRecordStore
from event_trader.operator.repair.artifacts import (
    read_jsonl_records,
    rewrite_jsonl_removing_lines,
)
from event_trader.pm_review.contracts import (
    PMPositionReviewTriggers,
    PMReviewRequest,
    is_terminal_pm_review_policy_skip_reason,
)
from event_trader.pm_review.store import (
    PMPositionReviewTriggerStore,
    PMReviewDispatchConsiderationStore,
    PMReviewRequestStore,
)
from event_trader.portfolio.pm_execution_flow import (
    PMExecutionFlowError,
    execute_pm_decision,
    materialize_view_state_change_from_execution,
)
from event_trader.portfolio.store import PMDecisionStore, PortfolioStateStore
from event_trader.storage import WorkspaceLayout
from event_trader.validation.state_change_store import (
    StateChangeStoreError,
    read_state_changes,
)

PMExecutionRecoveryAction = Literal["no_action", "resume", "finalize", "block"]
PMExecutionRecoveryApplyStatus = Literal["dry_run", "applied", "skipped", "blocked"]
_RETRYABLE_MARKET_DATA_REJECTION_PREFIX = (
    "market data unavailable for paper execution:"
)

_PM_TRIGGER_REVIEW_ROLES: frozenset[MaterialLevelRole] = frozenset(
    {
        "entry",
        "add",
        "hold_boundary",
        "de_risk_or_take_profit",
        "exit",
        "reverse_or_cover",
    }
)


class PMExecutionRecoveryError(ValueError):
    """Raised when PM/execution recovery inspection cannot prove runtime truth."""


@dataclass(frozen=True, slots=True)
class PMExecutionRecoveryDecision:
    """One operator-facing PM/execution lifecycle recovery decision."""

    target_key: str
    request_id: str | None
    decision_id: str | None
    intent_id: str | None
    execution_record_id: str | None
    portfolio_state_source_execution_record_id: str | None
    view_state_change_id: str | None
    business_at: str | None
    source_event_ids: tuple[str, ...]
    current_lifecycle_state: str
    durable_artifacts_committed: tuple[str, ...]
    missing_artifacts: tuple[str, ...]
    planned_action: PMExecutionRecoveryAction
    blocker_reason: str | None
    authoritative_artifact_kind: str
    authoritative_artifact_id: str | None
    authoritative_artifact_path: Path | None
    execution_required: bool | None
    terminal_policy_skip_reason: str | None = None
    recovery_surface: str = "runtime_repair scan-pm-execution-recovery"

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "intent_id": self.intent_id,
            "execution_record_id": self.execution_record_id,
            "portfolio_state_source_execution_record_id": (
                self.portfolio_state_source_execution_record_id
            ),
            "view_state_change_id": self.view_state_change_id,
            "business_at": self.business_at,
            "source_event_ids": list(self.source_event_ids),
            "current_lifecycle_state": self.current_lifecycle_state,
            "durable_artifacts_committed": list(self.durable_artifacts_committed),
            "missing_artifacts": list(self.missing_artifacts),
            "planned_action": self.planned_action,
            "blocker_reason": self.blocker_reason,
            "authoritative_artifact_kind": self.authoritative_artifact_kind,
            "authoritative_artifact_id": self.authoritative_artifact_id,
            "authoritative_artifact_path": (
                None
                if self.authoritative_artifact_path is None
                else _relative_path(
                    self.authoritative_artifact_path,
                    workspace_root=workspace_root,
                )
            ),
            "execution_required": self.execution_required,
            "terminal_policy_skip_reason": self.terminal_policy_skip_reason,
            "recovery_surface": self.recovery_surface,
        }


@dataclass(frozen=True, slots=True)
class PMExecutionRecoveryReport:
    """Operator-facing read-only PM/execution recovery report."""

    decision_count: int
    action_counts: dict[str, int]
    decisions: tuple[PMExecutionRecoveryDecision, ...]

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "decision_count": self.decision_count,
            "action_counts": dict(self.action_counts),
            "decisions": [
                decision.to_json_payload(workspace_root=workspace_root)
                for decision in self.decisions
            ],
        }


@dataclass(frozen=True, slots=True)
class PMExecutionRecoveryApplyDecision:
    """One write receipt or dry-run receipt for PM execution recovery."""

    target_key: str
    decision_id: str | None
    current_lifecycle_state: str
    status: PMExecutionRecoveryApplyStatus
    execution_record_id: str | None
    portfolio_state_source_execution_record_id: str | None
    blocker_reason: str | None

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "decision_id": self.decision_id,
            "current_lifecycle_state": self.current_lifecycle_state,
            "status": self.status,
            "execution_record_id": self.execution_record_id,
            "portfolio_state_source_execution_record_id": (
                self.portfolio_state_source_execution_record_id
            ),
            "blocker_reason": self.blocker_reason,
        }


@dataclass(frozen=True, slots=True)
class PMExecutionRecoveryApplyReport:
    """Operator-facing PM execution recovery apply report."""

    mode: Literal["dry_run", "apply"]
    inspected_count: int
    applied_count: int
    blocked_count: int
    decisions: tuple[PMExecutionRecoveryApplyDecision, ...]

    def to_json_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "inspected_count": self.inspected_count,
            "applied_count": self.applied_count,
            "blocked_count": self.blocked_count,
            "decisions": [decision.to_json_payload() for decision in self.decisions],
        }


@dataclass(frozen=True, slots=True)
class _TriggerProof:
    expected_trigger: PMPositionReviewTriggers | None
    blocker_reason: str | None


def plan_pm_execution_recovery(
    *,
    layout: WorkspaceLayout,
    target_key: str | None = None,
    event_id: str | None = None,
) -> PMExecutionRecoveryReport:
    """Plan lifecycle-aware PM/execution recovery from active durable truth."""

    if not isinstance(layout, WorkspaceLayout):
        raise PMExecutionRecoveryError("layout must be a WorkspaceLayout instance.")
    selected_target_key = _validate_optional_target_key(target_key)
    selected_event_id = _validate_optional_event_id(event_id)

    candidate_target_keys = (
        (selected_target_key,)
        if selected_target_key is not None
        else _pm_recovery_target_keys(layout)
    )
    decisions: list[PMExecutionRecoveryDecision] = []
    seen_keys: set[tuple[str, str | None, str | None, str]] = set()

    for normalized_target_key in candidate_target_keys:
        request_store = PMReviewRequestStore(layout)
        dispatch_store = PMReviewDispatchConsiderationStore(layout)
        trigger_store = PMPositionReviewTriggerStore(layout)
        decision_store = PMDecisionStore(layout)
        intent_store = ExecutionIntentStore(layout)
        execution_store = ExecutionRecordStore(layout)
        assessment_store = AnalysisAssessmentStore(layout)

        requests = request_store.read_records(target_key=normalized_target_key)
        dispatches = dispatch_store.read_records(target_key=normalized_target_key)
        triggers = trigger_store.read_records(target_key=normalized_target_key)
        persisted_decisions = decision_store.read_records(target_key=normalized_target_key)
        intents = intent_store.read_records(target_key=normalized_target_key)
        execution_records = execution_store.read_records(target_key=normalized_target_key)
        assessments = assessment_store.read_records(target_key=normalized_target_key)
        portfolio_state = PortfolioStateStore(layout).read(target_key=normalized_target_key)
        portfolio_state_path = (
            None
            if portfolio_state is None
            else PortfolioStateStore(layout).path_for(normalized_target_key)
        )
        state_changes = _read_target_state_changes(
            layout=layout,
            target_key=normalized_target_key,
        )

        dispatch_by_request = _latest_dispatch_by_request(dispatches)
        trigger_by_request = _latest_trigger_by_request(triggers)
        assessments_by_id = _assessments_by_id(assessments)
        decisions_by_request = _decisions_by_request(persisted_decisions)
        intents_by_decision = _intents_by_decision(intents)
        records_by_decision = _records_by_decision(execution_records)
        state_changes_by_execution = _state_changes_by_execution_record_id(state_changes)
        ambiguous_state_change_execution_ids = _ambiguous_state_change_execution_ids(
            state_changes
        )
        request_lookup = {item.record.request_id: item for item in requests}

        for persisted_request in requests:
            request = persisted_request.record
            if selected_event_id is not None and selected_event_id not in request.source_event_ids:
                continue
            decision = _decision_for_request(
                persisted_request=persisted_request,
                persisted_decisions=decisions_by_request.get(request.request_id, ()),
                dispatch=dispatch_by_request.get(request.request_id),
                trigger=trigger_by_request.get(request.request_id),
                intents_by_decision=intents_by_decision,
                records_by_decision=records_by_decision,
                portfolio_state=portfolio_state,
                portfolio_state_path=portfolio_state_path,
                state_changes_by_execution=state_changes_by_execution,
                ambiguous_state_change_execution_ids=ambiguous_state_change_execution_ids,
                assessments_by_id=assessments_by_id,
            )
            decision_key = (
                decision.target_key,
                decision.request_id,
                decision.decision_id,
                decision.current_lifecycle_state,
            )
            seen_keys.add(decision_key)
            decisions.append(decision)

        for persisted_decision in persisted_decisions:
            decision = persisted_decision.record
            if (
                decision.pm_review_request_id is not None
                and decision.pm_review_request_id in request_lookup
            ):
                continue
            if selected_event_id is not None and selected_event_id not in decision.source_event_ids:
                continue
            recovery_decision = _decision_without_request(
                persisted_decision=persisted_decision,
                trigger=None,
                intents_by_decision=intents_by_decision,
                records_by_decision=records_by_decision,
                portfolio_state=portfolio_state,
                portfolio_state_path=portfolio_state_path,
                state_changes_by_execution=state_changes_by_execution,
                ambiguous_state_change_execution_ids=ambiguous_state_change_execution_ids,
                assessments_by_id=assessments_by_id,
            )
            decision_key = (
                recovery_decision.target_key,
                recovery_decision.request_id,
                recovery_decision.decision_id,
                recovery_decision.current_lifecycle_state,
            )
            if decision_key in seen_keys:
                continue
            seen_keys.add(decision_key)
            decisions.append(recovery_decision)

        decision_ids = {item.record.decision_id for item in persisted_decisions}
        for persisted_intent in intents:
            if persisted_intent.record.pm_decision_id in decision_ids:
                continue
            recovery_decision = PMExecutionRecoveryDecision(
                target_key=normalized_target_key,
                request_id=None,
                decision_id=persisted_intent.record.pm_decision_id,
                intent_id=persisted_intent.record.intent_id,
                execution_record_id=None,
                portfolio_state_source_execution_record_id=None,
                view_state_change_id=None,
                business_at=persisted_intent.record.business_at.isoformat(),
                source_event_ids=(),
                current_lifecycle_state="execution_intent_without_pm_decision",
                durable_artifacts_committed=("execution_intent",),
                missing_artifacts=("pm_decision",),
                planned_action="block",
                blocker_reason=(
                    "execution intent exists without the authoritative PMDecision "
                    "needed to prove decision-first lineage."
                ),
                authoritative_artifact_kind="execution_intent",
                authoritative_artifact_id=persisted_intent.record.intent_id,
                authoritative_artifact_path=persisted_intent.path,
                execution_required=True,
            )
            decision_key = (
                recovery_decision.target_key,
                recovery_decision.request_id,
                recovery_decision.decision_id,
                recovery_decision.current_lifecycle_state,
            )
            if decision_key in seen_keys:
                continue
            seen_keys.add(decision_key)
            decisions.append(recovery_decision)

        for persisted_record in execution_records:
            if persisted_record.record.pm_decision_id in decision_ids:
                continue
            recovery_decision = PMExecutionRecoveryDecision(
                target_key=normalized_target_key,
                request_id=None,
                decision_id=persisted_record.record.pm_decision_id,
                intent_id=persisted_record.record.intent_id,
                execution_record_id=persisted_record.record.execution_record_id,
                portfolio_state_source_execution_record_id=None,
                view_state_change_id=None,
                business_at=persisted_record.record.business_at.isoformat(),
                source_event_ids=(),
                current_lifecycle_state="execution_record_without_pm_decision",
                durable_artifacts_committed=("execution_record",),
                missing_artifacts=("pm_decision",),
                planned_action="block",
                blocker_reason=(
                    "execution record exists without the authoritative PMDecision "
                    "needed to prove decision-first lineage."
                ),
                authoritative_artifact_kind="execution_record",
                authoritative_artifact_id=persisted_record.record.execution_record_id,
                authoritative_artifact_path=persisted_record.path,
                execution_required=True,
            )
            decision_key = (
                recovery_decision.target_key,
                recovery_decision.request_id,
                recovery_decision.decision_id,
                recovery_decision.current_lifecycle_state,
            )
            if decision_key in seen_keys:
                continue
            seen_keys.add(decision_key)
            decisions.append(recovery_decision)

        if (
            portfolio_state is not None
            and portfolio_state.source_pm_decision_id not in decision_ids
        ):
            recovery_decision = PMExecutionRecoveryDecision(
                target_key=normalized_target_key,
                request_id=None,
                decision_id=portfolio_state.source_pm_decision_id,
                intent_id=None,
                execution_record_id=portfolio_state.source_execution_record_id,
                portfolio_state_source_execution_record_id=portfolio_state.source_execution_record_id,
                view_state_change_id=None,
                business_at=portfolio_state.updated_at.isoformat(),
                source_event_ids=(),
                current_lifecycle_state="portfolio_state_without_pm_decision",
                durable_artifacts_committed=("portfolio_state",),
                missing_artifacts=("pm_decision",),
                planned_action="block",
                blocker_reason=(
                    "portfolio state exists without the authoritative PMDecision "
                    "needed to prove decision-first lineage."
                ),
                authoritative_artifact_kind="portfolio_state",
                authoritative_artifact_id=portfolio_state.source_execution_record_id,
                authoritative_artifact_path=PortfolioStateStore(layout).path_for(normalized_target_key),
                execution_required=True,
            )
            decision_key = (
                recovery_decision.target_key,
                recovery_decision.request_id,
                recovery_decision.decision_id,
                recovery_decision.current_lifecycle_state,
            )
            if decision_key not in seen_keys:
                decisions.append(recovery_decision)

    sorted_decisions = tuple(
        sorted(
            decisions,
            key=lambda item: (
                "" if item.business_at is None else item.business_at,
                item.target_key,
                "" if item.request_id is None else item.request_id,
                "" if item.decision_id is None else item.decision_id,
                item.current_lifecycle_state,
            ),
        )
    )
    action_counts: dict[str, int] = {}
    for decision in sorted_decisions:
        action_counts[decision.planned_action] = (
            action_counts.get(decision.planned_action, 0) + 1
        )
    return PMExecutionRecoveryReport(
        decision_count=len(sorted_decisions),
        action_counts=action_counts,
        decisions=sorted_decisions,
    )


def apply_pm_execution_recovery(
    *,
    layout: WorkspaceLayout,
    execution_engine: PaperExecutionEngine,
    target_key: str,
    event_id: str | None = None,
    apply: bool = False,
) -> PMExecutionRecoveryApplyReport:
    """Apply safe PMDecision-to-execution recovery without rerunning analysis."""

    if not isinstance(layout, WorkspaceLayout):
        raise PMExecutionRecoveryError("layout must be a WorkspaceLayout instance.")
    if not isinstance(execution_engine, PaperExecutionEngine):
        raise PMExecutionRecoveryError(
            "execution_engine must be a PaperExecutionEngine instance."
        )
    normalized_target_key = validate_target_key(
        target_key,
        error_type=PMExecutionRecoveryError,
    )
    selected_event_id = _validate_optional_event_id(event_id)
    plan = plan_pm_execution_recovery(
        layout=layout,
        target_key=normalized_target_key,
        event_id=selected_event_id,
    )
    decisions_by_id = {
        item.record.decision_id: item.record
        for item in PMDecisionStore(layout).read_records(target_key=normalized_target_key)
    }
    receipts: list[PMExecutionRecoveryApplyDecision] = []
    backup_root: Path | None = None
    for planned in plan.decisions:
        retryable_rejected_record = _retryable_market_data_rejected_record(
            layout=layout,
            planned=planned,
        )
        can_resume_missing_execution = (
            planned.planned_action == "resume"
            and planned.current_lifecycle_state == "decision_pending_execution_intent"
            and planned.decision_id is not None
        )
        can_retry_rejected_market_data_record = (
            retryable_rejected_record is not None and planned.decision_id is not None
        )
        if not can_resume_missing_execution and not can_retry_rejected_market_data_record:
            planned_status: PMExecutionRecoveryApplyStatus = "skipped"
            if planned.planned_action == "resume":
                planned_status = "blocked"
            receipts.append(
                PMExecutionRecoveryApplyDecision(
                    target_key=planned.target_key,
                    decision_id=planned.decision_id,
                    current_lifecycle_state=planned.current_lifecycle_state,
                    status=planned_status,
                    execution_record_id=planned.execution_record_id,
                    portfolio_state_source_execution_record_id=(
                        planned.portfolio_state_source_execution_record_id
                    ),
                    blocker_reason=planned.blocker_reason,
                )
            )
            continue

        decision = decisions_by_id.get(planned.decision_id)
        if decision is None:
            receipts.append(
                PMExecutionRecoveryApplyDecision(
                    target_key=planned.target_key,
                    decision_id=planned.decision_id,
                    current_lifecycle_state=planned.current_lifecycle_state,
                    status="blocked",
                    execution_record_id=None,
                    portfolio_state_source_execution_record_id=None,
                    blocker_reason="planned PMDecision is no longer readable.",
                )
            )
            continue

        blocker = _pm_execution_recovery_apply_blocker(
            layout=layout,
            decision=decision,
        )
        if blocker is not None:
            receipts.append(
                PMExecutionRecoveryApplyDecision(
                    target_key=planned.target_key,
                    decision_id=planned.decision_id,
                    current_lifecycle_state=planned.current_lifecycle_state,
                    status="blocked",
                    execution_record_id=None,
                    portfolio_state_source_execution_record_id=None,
                    blocker_reason=blocker,
                )
            )
            continue

        if not apply:
            receipts.append(
                PMExecutionRecoveryApplyDecision(
                    target_key=planned.target_key,
                    decision_id=planned.decision_id,
                    current_lifecycle_state=planned.current_lifecycle_state,
                    status="dry_run",
                    execution_record_id=planned.execution_record_id,
                    portfolio_state_source_execution_record_id=None,
                    blocker_reason=None,
                )
            )
            continue

        if retryable_rejected_record is not None:
            retry_blocker = _retryable_execution_replacement_blocker(
                decision=decision,
                execution_engine=execution_engine,
                existing_execution_record_id=retryable_rejected_record.record.execution_record_id,
            )
            if retry_blocker is not None:
                receipts.append(
                    PMExecutionRecoveryApplyDecision(
                        target_key=planned.target_key,
                        decision_id=planned.decision_id,
                        current_lifecycle_state=planned.current_lifecycle_state,
                        status="blocked",
                        execution_record_id=retryable_rejected_record.record.execution_record_id,
                        portfolio_state_source_execution_record_id=None,
                        blocker_reason=retry_blocker,
                    )
                )
                continue
            if backup_root is None:
                backup_root = _pm_execution_recovery_backup_root(
                    layout=layout,
                    target_key=normalized_target_key,
                    created_at=datetime.now(UTC),
                )
                backup_root.mkdir(parents=True, exist_ok=False)
            _remove_retryable_rejected_execution_record(
                layout=layout,
                target_key=normalized_target_key,
                execution_record_id=retryable_rejected_record.record.execution_record_id,
                backup_root=backup_root,
            )

        result = execute_pm_decision(
            layout=layout,
            decision=decision,
            execution_engine=execution_engine,
            execution_observed_at=datetime.now(UTC),
        )
        execution_record_id = (
            None
            if result.execution_record is None
            else result.execution_record.execution_record_id
        )
        portfolio_state_source_execution_record_id = (
            None
            if result.portfolio_state is None
            else result.portfolio_state.source_execution_record_id
        )
        applied = (
            result.execution_record is not None
            and result.execution_record.status == "executed"
            and result.portfolio_state is not None
        )
        receipts.append(
            PMExecutionRecoveryApplyDecision(
                target_key=planned.target_key,
                decision_id=planned.decision_id,
                current_lifecycle_state=planned.current_lifecycle_state,
                status="applied" if applied else "blocked",
                execution_record_id=execution_record_id,
                portfolio_state_source_execution_record_id=(
                    portfolio_state_source_execution_record_id
                ),
                blocker_reason=(
                    None
                    if applied
                    else "PM execution did not produce an executed record and portfolio state."
                    if result.execution_record is None
                    else result.execution_record.rejection_reason
                    if result.execution_record.status != "executed"
                    else "executed PM record did not update portfolio state."
                ),
            )
        )

    return PMExecutionRecoveryApplyReport(
        mode="apply" if apply else "dry_run",
        inspected_count=len(receipts),
        applied_count=sum(1 for receipt in receipts if receipt.status == "applied"),
        blocked_count=sum(1 for receipt in receipts if receipt.status == "blocked"),
        decisions=tuple(receipts),
    )


def _retryable_market_data_rejected_record(
    *,
    layout: WorkspaceLayout,
    planned: PMExecutionRecoveryDecision,
):
    if (
        planned.current_lifecycle_state != "execution_record_rejected_terminal"
        or planned.execution_record_id is None
    ):
        return None
    for persisted in ExecutionRecordStore(layout).read_records(
        target_key=planned.target_key,
    ):
        record = persisted.record
        if record.execution_record_id != planned.execution_record_id:
            continue
        if record.status != "rejected":
            return None
        if not _is_retryable_market_data_rejection(record.rejection_reason):
            return None
        return persisted
    return None


def _is_retryable_market_data_rejection(reason: str | None) -> bool:
    return (
        isinstance(reason, str)
        and reason.startswith(_RETRYABLE_MARKET_DATA_REJECTION_PREFIX)
    )


def _retryable_execution_replacement_blocker(
    *,
    decision,
    execution_engine: PaperExecutionEngine,
    existing_execution_record_id: str,
) -> str | None:
    intent = execution_engine.build_intent(decision=decision)
    candidate = execution_engine.execute(
        intent=intent,
        observed_at=datetime.now(UTC),
    )
    if not isinstance(candidate, ExecutionRecord):
        return "retryable PM execution is still awaiting observable market data."
    if candidate.execution_record_id != existing_execution_record_id:
        return (
            "retryable rejected ExecutionRecord id does not match the rebuilt "
            "execution record id."
        )
    if candidate.status != "executed":
        return (
            candidate.rejection_reason
            or "retryable PM execution still cannot produce an executed record."
        )
    return None


def _remove_retryable_rejected_execution_record(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    execution_record_id: str,
    backup_root: Path,
) -> None:
    matches = tuple(
        persisted
        for persisted in ExecutionRecordStore(layout).read_records(target_key=target_key)
        if persisted.record.execution_record_id == execution_record_id
    )
    if len(matches) != 1:
        raise PMExecutionRecoveryError(
            "retryable rejected ExecutionRecord must have exactly one persisted row."
        )
    persisted = matches[0]
    if not _is_retryable_market_data_rejection(persisted.record.rejection_reason):
        raise PMExecutionRecoveryError(
            "only market-data-unavailable rejected ExecutionRecord rows may be replaced."
        )
    records = read_jsonl_records(persisted.path)
    removed_count = rewrite_jsonl_removing_lines(
        path=persisted.path,
        records=records,
        line_numbers={persisted.line_number},
        backup_root=backup_root,
        layout=layout,
    )
    if removed_count != 1:
        raise PMExecutionRecoveryError(
            "failed to remove retryable rejected ExecutionRecord row."
        )


def _pm_execution_recovery_backup_root(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    created_at: datetime,
) -> Path:
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        layout.runtime_root
        / "repair"
        / "backups"
        / "pm_execution_recovery"
        / target_key
        / stamp
    ).resolve(strict=False)


def _decision_for_request(
    *,
    persisted_request,
    persisted_decisions,
    dispatch,
    trigger,
    intents_by_decision,
    records_by_decision,
    portfolio_state,
    portfolio_state_path,
    state_changes_by_execution,
    ambiguous_state_change_execution_ids,
    assessments_by_id,
) -> PMExecutionRecoveryDecision:
    request = persisted_request.record
    if len(persisted_decisions) > 1:
        return PMExecutionRecoveryDecision(
            target_key=request.target_key,
            request_id=request.request_id,
            decision_id=None,
            intent_id=None,
            execution_record_id=None,
            portfolio_state_source_execution_record_id=None,
            view_state_change_id=None,
            business_at=request.business_at.isoformat(),
            source_event_ids=request.source_event_ids,
            current_lifecycle_state="request_decision_lineage_ambiguous",
            durable_artifacts_committed=("pm_review_request",),
            missing_artifacts=(),
            planned_action="block",
            blocker_reason=(
                "multiple PMDecision rows reference the same PMReviewRequest; "
                "active lineage is ambiguous."
            ),
            authoritative_artifact_kind="pm_review_request",
            authoritative_artifact_id=request.request_id,
            authoritative_artifact_path=persisted_request.path,
            execution_required=None,
        )
    if not persisted_decisions:
        if (
            dispatch is not None
            and dispatch.record.outcome == "skipped"
            and is_terminal_pm_review_policy_skip_reason(dispatch.record.skip_reason)
        ):
            return PMExecutionRecoveryDecision(
                target_key=request.target_key,
                request_id=request.request_id,
                decision_id=None,
                intent_id=None,
                execution_record_id=None,
                portfolio_state_source_execution_record_id=None,
                view_state_change_id=None,
                business_at=request.business_at.isoformat(),
                source_event_ids=request.source_event_ids,
                current_lifecycle_state="request_terminal_policy_skip",
                durable_artifacts_committed=(
                    "pm_review_request",
                    "pm_review_dispatch_consideration",
                ),
                missing_artifacts=(),
                planned_action="no_action",
                blocker_reason=None,
                authoritative_artifact_kind="pm_review_dispatch_consideration",
                authoritative_artifact_id=dispatch.record.consideration_id,
                authoritative_artifact_path=dispatch.path,
                execution_required=None,
                terminal_policy_skip_reason=dispatch.record.skip_reason,
            )
        return PMExecutionRecoveryDecision(
            target_key=request.target_key,
            request_id=request.request_id,
            decision_id=None,
            intent_id=None,
            execution_record_id=None,
            portfolio_state_source_execution_record_id=None,
            view_state_change_id=None,
            business_at=request.business_at.isoformat(),
            source_event_ids=request.source_event_ids,
            current_lifecycle_state="request_pending_decision",
            durable_artifacts_committed=("pm_review_request",),
            missing_artifacts=("pm_decision",),
            planned_action="resume",
            blocker_reason=None,
            authoritative_artifact_kind="pm_review_request",
            authoritative_artifact_id=request.request_id,
            authoritative_artifact_path=persisted_request.path,
            execution_required=None,
        )
    return _decision_from_persisted_decision(
        request=request,
        persisted_request_path=persisted_request.path,
        persisted_decision=persisted_decisions[0],
        trigger=trigger,
        intents_by_decision=intents_by_decision,
        records_by_decision=records_by_decision,
        portfolio_state=portfolio_state,
        portfolio_state_path=portfolio_state_path,
        state_changes_by_execution=state_changes_by_execution,
        ambiguous_state_change_execution_ids=ambiguous_state_change_execution_ids,
        assessments_by_id=assessments_by_id,
    )


def _pm_execution_recovery_apply_blocker(
    *,
    layout: WorkspaceLayout,
    decision,
) -> str | None:
    portfolio_state = PortfolioStateStore(layout).read(target_key=decision.target_key)
    current_weight = 0.0 if portfolio_state is None else portfolio_state.target_weight
    if _same_weight(
        current_weight,
        decision.actual_target_weight_before_decision,
    ):
        return None
    return (
        "current portfolio target_weight no longer matches "
        "PMDecision.actual_target_weight_before_decision; rerun PMReview for this "
        "request with the recovered active exposure instead of executing a stale "
        "decision."
    )


def _same_weight(left: float, right: float) -> bool:
    return abs(left - right) <= 1e-12


def _decision_without_request(
    *,
    persisted_decision,
    trigger,
    intents_by_decision,
    records_by_decision,
    portfolio_state,
    portfolio_state_path,
    state_changes_by_execution,
    ambiguous_state_change_execution_ids,
    assessments_by_id,
) -> PMExecutionRecoveryDecision:
    return _decision_from_persisted_decision(
        request=None,
        persisted_request_path=None,
        persisted_decision=persisted_decision,
        trigger=trigger,
        intents_by_decision=intents_by_decision,
        records_by_decision=records_by_decision,
        portfolio_state=portfolio_state,
        portfolio_state_path=portfolio_state_path,
        state_changes_by_execution=state_changes_by_execution,
        ambiguous_state_change_execution_ids=ambiguous_state_change_execution_ids,
        assessments_by_id=assessments_by_id,
    )


def _decision_from_persisted_decision(
    *,
    request: PMReviewRequest | None,
    persisted_request_path: Path | None,
    persisted_decision,
    trigger,
    intents_by_decision,
    records_by_decision,
    portfolio_state,
    portfolio_state_path: Path | None,
    state_changes_by_execution,
    ambiguous_state_change_execution_ids,
    assessments_by_id,
) -> PMExecutionRecoveryDecision:
    decision = persisted_decision.record
    source_event_ids = (
        request.source_event_ids if request is not None else decision.source_event_ids
    )
    durable_artifacts = ["pm_decision"]
    if request is not None:
        durable_artifacts.insert(0, "pm_review_request")
    if trigger is not None:
        durable_artifacts.append("pm_position_review_triggers")

    persisted_intents = intents_by_decision.get(decision.decision_id, ())
    if len(persisted_intents) > 1:
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=None,
            execution_record_id=None,
            portfolio_state_source_execution_record_id=None,
            view_state_change_id=None,
            business_at=decision.business_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="decision_execution_lineage_ambiguous",
            durable_artifacts_committed=tuple(durable_artifacts),
            missing_artifacts=(),
            planned_action="block",
            blocker_reason=(
                "multiple ExecutionIntent rows reference the same PMDecision; "
                "active execution lineage is ambiguous."
            ),
            authoritative_artifact_kind="pm_decision",
            authoritative_artifact_id=decision.decision_id,
            authoritative_artifact_path=persisted_decision.path,
            execution_required=decision.execution_required,
        )

    persisted_records = records_by_decision.get(decision.decision_id, ())
    if len(persisted_records) > 1:
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=(
                None if not persisted_intents else persisted_intents[0].record.intent_id
            ),
            execution_record_id=None,
            portfolio_state_source_execution_record_id=None,
            view_state_change_id=None,
            business_at=decision.business_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="decision_execution_record_lineage_ambiguous",
            durable_artifacts_committed=tuple(
                durable_artifacts + (["execution_intent"] if persisted_intents else [])
            ),
            missing_artifacts=(),
            planned_action="block",
            blocker_reason=(
                "multiple ExecutionRecord rows reference the same PMDecision; "
                "duplicate execution risk cannot be ruled out."
            ),
            authoritative_artifact_kind="pm_decision",
            authoritative_artifact_id=decision.decision_id,
            authoritative_artifact_path=persisted_decision.path,
            execution_required=decision.execution_required,
        )

    if not decision.execution_required:
        trigger_decision = _trigger_follow_up_decision(
            request=request,
            decision=decision,
            persisted_decision_path=persisted_decision.path,
            persisted_request_path=persisted_request_path,
            trigger=trigger,
            durable_artifacts=tuple(durable_artifacts),
            source_event_ids=source_event_ids,
            assessments_by_id=assessments_by_id,
        )
        if trigger_decision is not None:
            return trigger_decision
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=None,
            execution_record_id=None,
            portfolio_state_source_execution_record_id=None,
            view_state_change_id=None,
            business_at=decision.business_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="decision_no_execution_required",
            durable_artifacts_committed=tuple(durable_artifacts),
            missing_artifacts=(),
            planned_action="no_action",
            blocker_reason=None,
            authoritative_artifact_kind="pm_decision",
            authoritative_artifact_id=decision.decision_id,
            authoritative_artifact_path=persisted_decision.path,
            execution_required=False,
        )

    if not persisted_intents:
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=None,
            execution_record_id=None,
            portfolio_state_source_execution_record_id=None,
            view_state_change_id=None,
            business_at=decision.business_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="decision_pending_execution_intent",
            durable_artifacts_committed=tuple(durable_artifacts),
            missing_artifacts=("execution_intent",),
            planned_action="resume",
            blocker_reason=None,
            authoritative_artifact_kind="pm_decision",
            authoritative_artifact_id=decision.decision_id,
            authoritative_artifact_path=persisted_decision.path,
            execution_required=True,
        )

    persisted_intent = persisted_intents[0]
    durable_with_intent = tuple(durable_artifacts + ["execution_intent"])
    if not persisted_records:
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=persisted_intent.record.intent_id,
            execution_record_id=None,
            portfolio_state_source_execution_record_id=None,
            view_state_change_id=None,
            business_at=decision.business_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="execution_intent_pending_record",
            durable_artifacts_committed=durable_with_intent,
            missing_artifacts=("execution_record",),
            planned_action="block",
            blocker_reason=(
                "ExecutionIntent exists, but duplicate execution risk cannot be "
                "ruled out from persisted truth alone."
            ),
            authoritative_artifact_kind="execution_intent",
            authoritative_artifact_id=persisted_intent.record.intent_id,
            authoritative_artifact_path=persisted_intent.path,
            execution_required=True,
        )

    persisted_record = persisted_records[0]
    durable_with_record = tuple(durable_with_intent + ("execution_record",))
    record = persisted_record.record
    if record.status != "executed":
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=record.intent_id,
            execution_record_id=record.execution_record_id,
            portfolio_state_source_execution_record_id=None,
            view_state_change_id=None,
            business_at=record.business_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="execution_record_rejected_terminal",
            durable_artifacts_committed=durable_with_record,
            missing_artifacts=(),
            planned_action="no_action",
            blocker_reason=None,
            authoritative_artifact_kind="execution_record",
            authoritative_artifact_id=record.execution_record_id,
            authoritative_artifact_path=persisted_record.path,
            execution_required=True,
        )

    if portfolio_state is None:
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=record.intent_id,
            execution_record_id=record.execution_record_id,
            portfolio_state_source_execution_record_id=None,
            view_state_change_id=None,
            business_at=record.executed_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="execution_record_pending_portfolio_state",
            durable_artifacts_committed=durable_with_record,
            missing_artifacts=("portfolio_state",),
            planned_action="finalize",
            blocker_reason=None,
            authoritative_artifact_kind="execution_record",
            authoritative_artifact_id=record.execution_record_id,
            authoritative_artifact_path=persisted_record.path,
            execution_required=True,
        )

    if (
        portfolio_state.source_pm_decision_id != decision.decision_id
        or portfolio_state.source_execution_record_id != record.execution_record_id
        or portfolio_state.decision_episode_id != decision.decision_episode_id
    ):
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=record.intent_id,
            execution_record_id=record.execution_record_id,
            portfolio_state_source_execution_record_id=portfolio_state.source_execution_record_id,
            view_state_change_id=None,
            business_at=portfolio_state.updated_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="portfolio_state_lineage_unproven",
            durable_artifacts_committed=tuple(durable_with_record + ("portfolio_state",)),
            missing_artifacts=(),
            planned_action="block",
            blocker_reason=(
                "PortfolioState exists, but its PM/execution lineage does not match "
                "the persisted PMDecision and ExecutionRecord."
            ),
            authoritative_artifact_kind="portfolio_state",
            authoritative_artifact_id=portfolio_state.source_execution_record_id,
            authoritative_artifact_path=portfolio_state_path,
            execution_required=True,
        )

    if record.execution_record_id in ambiguous_state_change_execution_ids:
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=record.intent_id,
            execution_record_id=record.execution_record_id,
            portfolio_state_source_execution_record_id=portfolio_state.source_execution_record_id,
            view_state_change_id=None,
            business_at=portfolio_state.updated_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="portfolio_state_view_state_change_lineage_ambiguous",
            durable_artifacts_committed=tuple(durable_with_record + ("portfolio_state",)),
            missing_artifacts=(),
            planned_action="block",
            blocker_reason=(
                "multiple ViewStateChange rows reference the same ExecutionRecord; "
                "sidecar lineage is ambiguous."
            ),
            authoritative_artifact_kind="portfolio_state",
            authoritative_artifact_id=portfolio_state.source_execution_record_id,
            authoritative_artifact_path=portfolio_state_path,
            execution_required=True,
        )

    state_change = state_changes_by_execution.get(record.execution_record_id)
    missing_artifacts: list[str] = []
    blocker_reason: str | None = None
    if state_change is None:
        try:
            materialize_view_state_change_from_execution(
                decision=decision,
                execution_record=record,
            )
        except PMExecutionFlowError as exc:
            blocker_reason = (
                "PortfolioState exists, but ViewStateChange lineage cannot be "
                f"proven from the persisted PMDecision and ExecutionRecord: {exc}"
            )
        else:
            missing_artifacts.append("view_state_change")
    trigger_decision = _trigger_follow_up_decision(
        request=request,
        decision=decision,
        persisted_decision_path=persisted_decision.path,
        persisted_request_path=persisted_request_path,
        trigger=trigger,
        durable_artifacts=tuple(durable_with_record + ("portfolio_state",)),
        source_event_ids=source_event_ids,
        assessments_by_id=assessments_by_id,
        state_change=state_change,
        blocker_reason=blocker_reason,
        missing_artifacts=tuple(missing_artifacts),
        authoritative_path=persisted_record.path,
        authoritative_kind="execution_record",
        authoritative_id=record.execution_record_id,
    )
    if trigger_decision is not None:
        return trigger_decision
    if blocker_reason is not None:
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=record.intent_id,
            execution_record_id=record.execution_record_id,
            portfolio_state_source_execution_record_id=portfolio_state.source_execution_record_id,
            view_state_change_id=None,
            business_at=portfolio_state.updated_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="portfolio_state_pending_view_state_change",
            durable_artifacts_committed=tuple(durable_with_record + ("portfolio_state",)),
            missing_artifacts=tuple(missing_artifacts),
            planned_action="block",
            blocker_reason=blocker_reason,
            authoritative_artifact_kind="portfolio_state",
            authoritative_artifact_id=portfolio_state.source_execution_record_id,
            authoritative_artifact_path=portfolio_state_path,
            execution_required=True,
        )
    if state_change is None:
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=record.intent_id,
            execution_record_id=record.execution_record_id,
            portfolio_state_source_execution_record_id=portfolio_state.source_execution_record_id,
            view_state_change_id=None,
            business_at=portfolio_state.updated_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="portfolio_state_pending_view_state_change",
            durable_artifacts_committed=tuple(durable_with_record + ("portfolio_state",)),
            missing_artifacts=("view_state_change",),
            planned_action="finalize",
            blocker_reason=None,
            authoritative_artifact_kind="portfolio_state",
            authoritative_artifact_id=portfolio_state.source_execution_record_id,
            authoritative_artifact_path=portfolio_state_path,
            execution_required=True,
        )
    return PMExecutionRecoveryDecision(
        target_key=decision.target_key,
        request_id=None if request is None else request.request_id,
        decision_id=decision.decision_id,
        intent_id=record.intent_id,
        execution_record_id=record.execution_record_id,
        portfolio_state_source_execution_record_id=portfolio_state.source_execution_record_id,
        view_state_change_id=state_change.record.state_change_id,
        business_at=portfolio_state.updated_at.isoformat(),
        source_event_ids=source_event_ids,
        current_lifecycle_state="complete",
        durable_artifacts_committed=tuple(
            durable_with_record + ("portfolio_state", "view_state_change")
        ),
        missing_artifacts=(),
        planned_action="no_action",
        blocker_reason=None,
        authoritative_artifact_kind="view_state_change",
        authoritative_artifact_id=state_change.record.state_change_id,
        authoritative_artifact_path=state_change.path,
        execution_required=True,
    )


def _trigger_follow_up_decision(
    *,
    request: PMReviewRequest | None,
    decision,
    persisted_decision_path: Path,
    persisted_request_path: Path | None,
    trigger,
    durable_artifacts: tuple[str, ...],
    source_event_ids: tuple[str, ...],
    assessments_by_id,
    state_change=None,
    blocker_reason: str | None = None,
    missing_artifacts: tuple[str, ...] = (),
    authoritative_path: Path | None = None,
    authoritative_kind: str | None = None,
    authoritative_id: str | None = None,
) -> PMExecutionRecoveryDecision | None:
    if decision.requested_state == "flat":
        return None
    if trigger is not None:
        return None

    proof = _prove_expected_trigger_truth(
        request=request,
        decision=decision,
        assessments_by_id=assessments_by_id,
    )
    if proof.blocker_reason is not None:
        return PMExecutionRecoveryDecision(
            target_key=decision.target_key,
            request_id=None if request is None else request.request_id,
            decision_id=decision.decision_id,
            intent_id=None,
            execution_record_id=(
                authoritative_id if authoritative_kind == "execution_record" else None
            ),
            portfolio_state_source_execution_record_id=None,
            view_state_change_id=(
                None if state_change is None else state_change.record.state_change_id
            ),
            business_at=decision.business_at.isoformat(),
            source_event_ids=source_event_ids,
            current_lifecycle_state="portfolio_state_pending_trigger_truth"
            if state_change is not None or authoritative_kind == "execution_record"
            else "decision_pending_trigger_truth",
            durable_artifacts_committed=durable_artifacts,
            missing_artifacts=tuple((*missing_artifacts, "pm_position_review_triggers")),
            planned_action="block",
            blocker_reason=blocker_reason or proof.blocker_reason,
            authoritative_artifact_kind=(
                "pm_decision" if authoritative_kind is None else authoritative_kind
            ),
            authoritative_artifact_id=(
                decision.decision_id if authoritative_id is None else authoritative_id
            ),
            authoritative_artifact_path=(
                persisted_decision_path if authoritative_path is None else authoritative_path
            ),
            execution_required=decision.execution_required,
        )
    if proof.expected_trigger is None:
        return None
    return PMExecutionRecoveryDecision(
        target_key=decision.target_key,
        request_id=None if request is None else request.request_id,
        decision_id=decision.decision_id,
        intent_id=None,
        execution_record_id=authoritative_id if authoritative_kind == "execution_record" else None,
        portfolio_state_source_execution_record_id=None,
        view_state_change_id=None if state_change is None else state_change.record.state_change_id,
        business_at=decision.business_at.isoformat(),
        source_event_ids=source_event_ids,
        current_lifecycle_state="portfolio_state_pending_trigger_truth"
        if state_change is not None or authoritative_kind == "execution_record"
        else "decision_pending_trigger_truth",
        durable_artifacts_committed=durable_artifacts,
        missing_artifacts=tuple((*missing_artifacts, "pm_position_review_triggers")),
        planned_action="finalize",
        blocker_reason=None,
        authoritative_artifact_kind=(
            "pm_decision" if authoritative_kind is None else authoritative_kind
        ),
        authoritative_artifact_id=(
            decision.decision_id if authoritative_id is None else authoritative_id
        ),
        authoritative_artifact_path=(
            persisted_decision_path if authoritative_path is None else authoritative_path
        ),
        execution_required=decision.execution_required,
    )


def _prove_expected_trigger_truth(
    *,
    request: PMReviewRequest | None,
    decision,
    assessments_by_id,
) -> _TriggerProof:
    if request is None:
        return _TriggerProof(
            expected_trigger=None,
            blocker_reason=(
                "non-flat PMDecision is missing its authoritative PMReviewRequest, "
                "so PM trigger truth cannot be proven."
            ),
        )
    if request.source_assessment_id is None:
        return _TriggerProof(
            expected_trigger=None,
            blocker_reason=(
                "non-flat PMDecision is missing a linked AnalysisAssessment, so PM "
                "trigger truth cannot be proven."
            ),
        )
    matches = assessments_by_id.get(request.source_assessment_id, ())
    if not matches:
        return _TriggerProof(
            expected_trigger=None,
            blocker_reason=(
                "linked AnalysisAssessment is missing for the non-flat PMDecision, "
                "so PM trigger truth cannot be proven."
            ),
        )
    if len(matches) > 1:
        return _TriggerProof(
            expected_trigger=None,
            blocker_reason=(
                "linked AnalysisAssessment id is ambiguous, so PM trigger truth "
                "cannot be proven safely."
            ),
        )
    return _TriggerProof(
        expected_trigger=_expected_trigger_record(
            request=request,
            decision=decision,
            assessment=matches[0].record,
        ),
        blocker_reason=None,
    )


def _expected_trigger_record(
    *,
    request: PMReviewRequest,
    decision,
    assessment: AnalysisAssessment,
) -> PMPositionReviewTriggers | None:
    review_level_ids: list[str] = []
    invalidation_level_ids: list[str] = []
    trigger_reason_codes: list[str] = []
    path_context_required = False
    market_confirmation_required = False
    for level in assessment.price_level_roles:
        level_role: MaterialLevelRole
        if decision.requested_state in {"weak_long", "strong_long"}:
            level_role = level.role_if_already_long
        else:
            level_role = level.role_if_already_short
        if level_role == "invalidates":
            invalidation_level_ids.append(level.level_id)
        elif level_role in _PM_TRIGGER_REVIEW_ROLES:
            review_level_ids.append(level.level_id)
        else:
            continue
        path_context_required = path_context_required or level.path_context_required
        market_confirmation_required = True
        trigger_reason_codes.append(f"level_role_{level_role}")
    if review_level_ids:
        trigger_reason_codes.append("review_levels_present")
    if invalidation_level_ids:
        trigger_reason_codes.append("invalidation_levels_present")
    if path_context_required:
        trigger_reason_codes.append("path_context_required")
    if market_confirmation_required:
        trigger_reason_codes.append("market_confirmation_required")
    if not review_level_ids and not invalidation_level_ids:
        return None
    return PMPositionReviewTriggers(
        pm_review_request_id=request.request_id,
        target_key=request.target_key,
        business_at=request.business_at,
        requested_state=decision.requested_state,
        review_trigger_level_ids=_stable_unique_tuple(review_level_ids),
        invalidation_trigger_level_ids=_stable_unique_tuple(invalidation_level_ids),
        path_context_required=path_context_required,
        market_confirmation_required=market_confirmation_required,
        trigger_reason_codes=_stable_unique_tuple(trigger_reason_codes),
    )


def _pm_recovery_target_keys(layout: WorkspaceLayout) -> tuple[str, ...]:
    roots = (
        layout.runtime_root / "pm_review" / "requests",
        layout.runtime_root / "portfolio" / "pm-decisions",
        layout.runtime_root / "execution" / "intents",
        layout.runtime_root / "execution" / "records",
        layout.runtime_root / "validation" / "state-changes",
        layout.runtime_root / "pm_review" / "position_review_triggers",
    )
    target_keys: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(item for item in root.iterdir() if item.is_dir()):
            target_keys.add(
                validate_target_key(path.name, error_type=PMExecutionRecoveryError)
            )
    state_root = layout.runtime_root / "portfolio" / "state"
    if state_root.exists():
        for path in sorted(state_root.glob("*.json")):
            target_keys.add(
                validate_target_key(path.stem, error_type=PMExecutionRecoveryError)
            )
    return tuple(sorted(target_keys))


def _latest_dispatch_by_request(dispatches) -> dict[str, object]:
    by_request: dict[str, list[object]] = {}
    for item in dispatches:
        by_request.setdefault(item.record.request_id, []).append(item)
    return {
        request_id: sorted(
            items,
            key=lambda item: (
                item.record.dispatch_run_until,
                item.path.as_posix(),
                item.line_number,
            ),
        )[-1]
        for request_id, items in by_request.items()
    }


def _latest_trigger_by_request(triggers) -> dict[str, object]:
    by_request: dict[str, list[object]] = {}
    for item in triggers:
        by_request.setdefault(item.record.pm_review_request_id, []).append(item)
    return {
        request_id: sorted(
            items,
            key=lambda item: (
                item.record.business_at,
                item.path.as_posix(),
                item.line_number,
            ),
        )[-1]
        for request_id, items in by_request.items()
    }


def _assessments_by_id(assessments) -> dict[str, tuple[object, ...]]:
    by_id: dict[str, list[object]] = {}
    for item in assessments:
        by_id.setdefault(item.record.assessment_id, []).append(item)
    return {assessment_id: tuple(items) for assessment_id, items in by_id.items()}


def _decisions_by_request(persisted_decisions) -> dict[str, tuple[object, ...]]:
    by_request: dict[str, list[object]] = {}
    for item in persisted_decisions:
        request_id = item.record.pm_review_request_id
        if request_id is None:
            continue
        by_request.setdefault(request_id, []).append(item)
    return {
        request_id: tuple(
            sorted(
                items,
                key=lambda item: (
                    item.record.business_at,
                    item.path.as_posix(),
                    item.line_number,
                ),
            )
        )
        for request_id, items in by_request.items()
    }


def _intents_by_decision(intents) -> dict[str, tuple[object, ...]]:
    by_decision: dict[str, list[object]] = {}
    for item in intents:
        by_decision.setdefault(item.record.pm_decision_id, []).append(item)
    return {
        decision_id: tuple(
            sorted(
                items,
                key=lambda item: (
                    item.record.business_at,
                    item.path.as_posix(),
                    item.line_number,
                ),
            )
        )
        for decision_id, items in by_decision.items()
    }


def _records_by_decision(records) -> dict[str, tuple[object, ...]]:
    by_decision: dict[str, list[object]] = {}
    for item in records:
        by_decision.setdefault(item.record.pm_decision_id, []).append(item)
    return {
        decision_id: tuple(
            sorted(
                items,
                key=lambda item: (
                    item.record.recorded_at,
                    item.path.as_posix(),
                    item.line_number,
                ),
            )
        )
        for decision_id, items in by_decision.items()
    }


def _state_changes_by_execution_record_id(state_changes) -> dict[str, object]:
    by_execution_record_id: dict[str, list[object]] = {}
    for item in state_changes:
        if item.record.execution_record_id is None:
            continue
        by_execution_record_id.setdefault(item.record.execution_record_id, []).append(item)
    resolved: dict[str, object] = {}
    for execution_record_id, items in by_execution_record_id.items():
        if len(items) == 1:
            resolved[execution_record_id] = items[0]
    return resolved


def _ambiguous_state_change_execution_ids(state_changes) -> set[str]:
    by_execution_record_id: dict[str, int] = {}
    for item in state_changes:
        if item.record.execution_record_id is None:
            continue
        by_execution_record_id[item.record.execution_record_id] = (
            by_execution_record_id.get(item.record.execution_record_id, 0) + 1
        )
    return {
        execution_record_id
        for execution_record_id, count in by_execution_record_id.items()
        if count > 1
    }


def _read_target_state_changes(
    *,
    layout: WorkspaceLayout,
    target_key: str,
):
    try:
        records = read_state_changes(layout, target_key)
    except StateChangeStoreError as exc:
        raise PMExecutionRecoveryError(str(exc)) from exc
    path = (
        layout.runtime_root / "validation" / "state-changes" / target_key
    ).resolve(strict=False)
    persisted: list[object] = []
    if not path.exists():
        return ()
    state_change_by_id = {record.state_change_id: record for record in records}
    for shard_path in sorted(path.glob("*.jsonl")):
        with shard_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                normalized = line.strip()
                if not normalized:
                    continue
                state_change = state_change_by_id.get(
                    _state_change_id_from_line(normalized)
                )
                if state_change is None:
                    continue
                persisted.append(
                    _PersistedStateChange(
                        record=state_change,
                        path=shard_path.resolve(strict=False),
                        line_number=line_number,
                    )
                )
    return tuple(persisted)


@dataclass(frozen=True, slots=True)
class _PersistedStateChange:
    record: ViewStateChange
    path: Path
    line_number: int


def _state_change_id_from_line(line: str) -> str:
    marker = '"state_change_id":"'
    start = line.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    end = line.find('"', start)
    if end == -1:
        return ""
    return line[start:end]


def _stable_unique_tuple(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _validate_optional_target_key(value: str | None) -> str | None:
    if value is None:
        return None
    return validate_target_key(value, error_type=PMExecutionRecoveryError)


def _validate_optional_event_id(value: str | None) -> str | None:
    if value is None:
        return None
    return validate_event_id(value, error_type=PMExecutionRecoveryError)


def _relative_path(path: Path, *, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "PMExecutionRecoveryApplyDecision",
    "PMExecutionRecoveryApplyReport",
    "PMExecutionRecoveryDecision",
    "PMExecutionRecoveryError",
    "PMExecutionRecoveryReport",
    "apply_pm_execution_recovery",
    "plan_pm_execution_recovery",
]
