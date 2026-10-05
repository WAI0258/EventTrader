"""Read-only PM lifecycle summary for downstream audit/reporting."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.contracts._validators import validate_target_key
from event_trader.execution.contracts import classify_execution_source
from event_trader.execution.store import ExecutionRecordStore
from event_trader.pm_review.store import (
    PMPositionReviewTriggerStore,
    PMReviewDispatchConsiderationStore,
    PMReviewRequestStore,
)
from event_trader.portfolio.store import PMDecisionStore, PortfolioStateStore
from event_trader.storage import WorkspaceLayout
from event_trader.validation.state_change_store import (
    classify_view_state_change_source,
    read_state_changes,
)


class PMLifecycleSummaryError(ValueError):
    """Raised when PM lifecycle reporting inputs are invalid."""


@dataclass(frozen=True, slots=True)
class PMRuntimeLifecycleSummary:
    analysis_assessment_count: int
    pm_review_request_count: int
    analysis_origin_pm_review_request_count: int
    trigger_driven_pm_review_request_count: int
    pm_position_review_trigger_count: int
    pm_review_dispatch_considered_count: int
    pm_review_dispatch_skipped_count: int
    pm_review_dispatch_skipped_by_reason: Mapping[str, int]
    pm_review_request_with_decision_count: int
    pm_review_flat_decision_count: int
    pm_review_non_flat_decision_count: int
    pm_decision_noop_count: int
    pm_decision_execution_required_count: int
    pm_decision_count: int
    execution_count: int
    portfolio_state_update_count: int
    view_state_change_count: int
    execution_source_counts: Mapping[str, int]
    view_state_change_source_counts: Mapping[str, int]
    joined_pm_execution_count: int
    join_issue_count: int
    join_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "analysis_assessment_count",
            "pm_review_request_count",
            "analysis_origin_pm_review_request_count",
            "trigger_driven_pm_review_request_count",
            "pm_position_review_trigger_count",
            "pm_review_dispatch_considered_count",
            "pm_review_dispatch_skipped_count",
            "pm_review_request_with_decision_count",
            "pm_review_flat_decision_count",
            "pm_review_non_flat_decision_count",
            "pm_decision_noop_count",
            "pm_decision_execution_required_count",
            "pm_decision_count",
            "execution_count",
            "portfolio_state_update_count",
            "view_state_change_count",
            "joined_pm_execution_count",
            "join_issue_count",
        ):
            _validate_non_negative_int(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "pm_review_dispatch_skipped_by_reason",
            _validate_counts(
                self.pm_review_dispatch_skipped_by_reason,
                "pm_review_dispatch_skipped_by_reason",
            ),
        )
        object.__setattr__(
            self,
            "execution_source_counts",
            _validate_counts(self.execution_source_counts, "execution_source_counts"),
        )
        object.__setattr__(
            self,
            "view_state_change_source_counts",
            _validate_counts(
                self.view_state_change_source_counts,
                "view_state_change_source_counts",
            ),
        )
        object.__setattr__(
            self,
            "join_issues",
            tuple(_validate_non_blank(item, "join_issues") for item in self.join_issues),
        )
        if self.join_issue_count != len(self.join_issues):
            raise PMLifecycleSummaryError("join_issue_count must match join_issues length.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "analysis_assessment_count": self.analysis_assessment_count,
            "pm_review_request_count": self.pm_review_request_count,
            "analysis_origin_pm_review_request_count": self.analysis_origin_pm_review_request_count,
            "trigger_driven_pm_review_request_count": self.trigger_driven_pm_review_request_count,
            "pm_position_review_trigger_count": self.pm_position_review_trigger_count,
            "pm_review_dispatch_considered_count": self.pm_review_dispatch_considered_count,
            "pm_review_dispatch_skipped_count": self.pm_review_dispatch_skipped_count,
            "pm_review_dispatch_skipped_by_reason": dict(
                self.pm_review_dispatch_skipped_by_reason
            ),
            "pm_review_request_with_decision_count": self.pm_review_request_with_decision_count,
            "pm_review_flat_decision_count": self.pm_review_flat_decision_count,
            "pm_review_non_flat_decision_count": self.pm_review_non_flat_decision_count,
            "pm_decision_noop_count": self.pm_decision_noop_count,
            "pm_decision_execution_required_count": self.pm_decision_execution_required_count,
            "pm_decision_count": self.pm_decision_count,
            "execution_count": self.execution_count,
            "portfolio_state_update_count": self.portfolio_state_update_count,
            "view_state_change_count": self.view_state_change_count,
            "execution_source_counts": dict(self.execution_source_counts),
            "view_state_change_source_counts": dict(self.view_state_change_source_counts),
            "joined_pm_execution_count": self.joined_pm_execution_count,
            "join_issue_count": self.join_issue_count,
            "join_issues": list(self.join_issues),
        }


def build_pm_runtime_lifecycle_summary(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> PMRuntimeLifecycleSummary:
    """Summarize downstream PM lifecycle records without owning or mutating them."""

    if not isinstance(layout, WorkspaceLayout):
        raise PMLifecycleSummaryError("layout must be a WorkspaceLayout instance.")
    normalized_target = validate_target_key(target_key, error_type=PMLifecycleSummaryError)
    assessments = AnalysisAssessmentStore(layout).read_records(target_key=normalized_target)
    requests = PMReviewRequestStore(layout).read_records(target_key=normalized_target)
    position_review_triggers = PMPositionReviewTriggerStore(layout).read_records(
        target_key=normalized_target
    )
    dispatch_considerations = PMReviewDispatchConsiderationStore(layout).read_records(
        target_key=normalized_target
    )
    pm_decisions = PMDecisionStore(layout).read_records(target_key=normalized_target)
    executions = ExecutionRecordStore(layout).read_records(target_key=normalized_target)
    portfolio_state = PortfolioStateStore(layout).read(target_key=normalized_target)
    state_changes = read_state_changes(layout, normalized_target)

    execution_records = tuple(item.record for item in executions)
    state_change_records = tuple(state_changes)
    dispatch_records = tuple(item.record for item in dispatch_considerations)
    skipped_dispatch_counts = _counts(
        record.skip_reason
        for record in dispatch_records
        if record.outcome == "skipped" and record.skip_reason is not None
    )
    execution_source_counts = _counts(
        classify_execution_source(record) for record in execution_records
    )
    pm_execution_records = tuple(
        record
        for record in execution_records
        if classify_execution_source(record) == "pm_decision"
    )
    request_ids_with_decision = {
        item.record.pm_review_request_id
        for item in pm_decisions
        if item.record.pm_review_request_id is not None
    }
    state_change_source_counts = _counts(
        classify_view_state_change_source(record) for record in state_change_records
    )
    join_issues = _pm_execution_join_issues(
        decisions_by_id={item.record.decision_id: item.record for item in pm_decisions},
        execution_records=pm_execution_records,
    )
    joined_pm_execution_count = len(pm_execution_records) - len(join_issues)
    if joined_pm_execution_count < 0:
        joined_pm_execution_count = 0

    return PMRuntimeLifecycleSummary(
        analysis_assessment_count=len(assessments),
        pm_review_request_count=len(requests),
        analysis_origin_pm_review_request_count=sum(
            1 for item in requests if item.record.source == "analysis_event"
        ),
        trigger_driven_pm_review_request_count=sum(
            1 for item in requests if item.record.source == "open_position_material_update"
        ),
        pm_position_review_trigger_count=len(position_review_triggers),
        pm_review_dispatch_considered_count=len(dispatch_records),
        pm_review_dispatch_skipped_count=sum(
            1 for record in dispatch_records if record.outcome == "skipped"
        ),
        pm_review_dispatch_skipped_by_reason=skipped_dispatch_counts,
        pm_review_request_with_decision_count=len(request_ids_with_decision),
        pm_review_flat_decision_count=sum(
            1 for item in pm_decisions if item.record.requested_state == "flat"
        ),
        pm_review_non_flat_decision_count=sum(
            1 for item in pm_decisions if item.record.requested_state != "flat"
        ),
        pm_decision_noop_count=sum(
            1 for item in pm_decisions if item.record.execution_required is False
        ),
        pm_decision_execution_required_count=sum(
            1 for item in pm_decisions if item.record.execution_required is True
        ),
        pm_decision_count=len(pm_decisions),
        execution_count=len(execution_records),
        portfolio_state_update_count=0 if portfolio_state is None else 1,
        view_state_change_count=len(state_change_records),
        execution_source_counts=execution_source_counts,
        view_state_change_source_counts=state_change_source_counts,
        joined_pm_execution_count=joined_pm_execution_count,
        join_issue_count=len(join_issues),
        join_issues=join_issues,
    )


def _pm_execution_join_issues(
    *,
    decisions_by_id: Mapping[str, object],
    execution_records,
) -> tuple[str, ...]:
    issues: list[str] = []
    for record in execution_records:
        if record.pm_decision_id not in decisions_by_id:
            issues.append(f"PM execution missing PM decision: {record.execution_record_id}")
    return tuple(issues)


def _counts(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _validate_counts(value: Mapping[str, object], field_name: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise PMLifecycleSummaryError(f"{field_name} must be a mapping.")
    return {
        _validate_non_blank(key, field_name): _validate_non_negative_int(count, field_name)
        for key, count in value.items()
    }


def _validate_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PMLifecycleSummaryError(f"{field_name} must be a non-negative integer.")
    return value


def _validate_non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PMLifecycleSummaryError(f"{field_name} must be a non-blank string.")
    return value.strip()


__all__ = [
    "PMLifecycleSummaryError",
    "PMRuntimeLifecycleSummary",
    "build_pm_runtime_lifecycle_summary",
]
