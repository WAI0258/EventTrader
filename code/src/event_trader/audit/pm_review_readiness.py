"""Read-only PMReview runtime readiness and lifecycle health report."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.audit.pm_lifecycle_summary import build_pm_runtime_lifecycle_summary
from event_trader.contracts._validators import validate_target_key
from event_trader.execution.store import ExecutionRecordStore
from event_trader.migrations import (
    PMReviewWorkspaceMigrationError,
    validate_pm_review_workspace_ready,
)
from event_trader.migrations.cutover_baseline import (
    CUTOVER_RUNTIME_SCHEMA_VERSION,
    DEFAULT_CUTOVER_MIGRATION_ID,
)
from event_trader.pm_review import is_terminal_pm_review_policy_skip_reason
from event_trader.pm_review.store import (
    CandidateReviewAnchorStore,
    PMPositionReviewTriggerStore,
    PMReviewDispatchConsiderationStore,
    PMReviewFailureStore,
    PMReviewRequestStore,
)
from event_trader.portfolio.store import PMDecisionStore, PortfolioStateStore
from event_trader.storage import WorkspaceLayout
from event_trader.validation.state_change_store import read_state_changes


class PMReviewReadinessReportError(ValueError):
    """Raised when PMReview readiness report inputs are invalid."""


_COUNT_KEYS = (
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
    "pm_review_failure_count",
    "candidate_anchor_count",
    "pm_decision_count",
    "execution_record_count",
    "pm_sidecar_view_state_change_count",
)
_STUCK_KEYS = (
    "request_without_pm_decision",
    "execution_without_sidecar",
    "candidate_request_missing_anchor",
    "candidate_request_expired_anchor",
    "request_without_pm_decision_with_failure",
    "request_without_pm_decision_without_failure",
    "request_without_pm_decision_retryable_failure",
)


@dataclass(frozen=True, slots=True)
class PMReviewReadinessTargetReport:
    target_key: str
    baseline_source_type: str | None
    market_setup_dashboard_present: bool
    portfolio_state_present: bool
    counts: Mapping[str, int]
    pm_review_dispatch_skipped_by_reason: Mapping[str, int]
    stuck_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PMReviewReadinessReportError),
        )
        if self.baseline_source_type is not None:
            object.__setattr__(
                self,
                "baseline_source_type",
                _non_blank(self.baseline_source_type, "baseline_source_type"),
            )
        if not isinstance(self.market_setup_dashboard_present, bool):
            raise PMReviewReadinessReportError(
                "market_setup_dashboard_present must be a boolean."
            )
        if not isinstance(self.portfolio_state_present, bool):
            raise PMReviewReadinessReportError("portfolio_state_present must be a boolean.")
        object.__setattr__(self, "counts", _validate_count_map(self.counts, _COUNT_KEYS))
        object.__setattr__(
            self,
            "pm_review_dispatch_skipped_by_reason",
            _validate_dynamic_count_map(self.pm_review_dispatch_skipped_by_reason),
        )
        object.__setattr__(
            self,
            "stuck_counts",
            _validate_count_map(self.stuck_counts, _STUCK_KEYS),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "baseline_source_type": self.baseline_source_type,
            "market_setup_dashboard_present": self.market_setup_dashboard_present,
            "portfolio_state_present": self.portfolio_state_present,
            "counts": dict(self.counts),
            "pm_review_dispatch_skipped_by_reason": dict(self.pm_review_dispatch_skipped_by_reason),
            "stuck_counts": dict(self.stuck_counts),
        }


@dataclass(frozen=True, slots=True)
class PMReviewReadinessReport:
    workspace_ready: bool
    workspace_ready_error: str | None
    schema_version: str | None
    migration_id: str | None
    baseline_source_types_by_target: Mapping[str, str]
    target_reports: tuple[PMReviewReadinessTargetReport, ...]
    totals: Mapping[str, int]
    pm_review_dispatch_skipped_by_reason_totals: Mapping[str, int]
    stuck_totals: Mapping[str, int]

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_ready, bool):
            raise PMReviewReadinessReportError("workspace_ready must be a boolean.")
        if self.workspace_ready_error is not None:
            object.__setattr__(
                self,
                "workspace_ready_error",
                _non_blank(self.workspace_ready_error, "workspace_ready_error"),
            )
        if self.schema_version is not None:
            object.__setattr__(
                self,
                "schema_version",
                _non_blank(self.schema_version, "schema_version"),
            )
        if self.migration_id is not None:
            object.__setattr__(
                self,
                "migration_id",
                _non_blank(self.migration_id, "migration_id"),
            )
        object.__setattr__(
            self,
            "baseline_source_types_by_target",
            {
                validate_target_key(target_key, error_type=PMReviewReadinessReportError): _non_blank(
                    source_type,
                    "baseline_source_type",
                )
                for target_key, source_type in self.baseline_source_types_by_target.items()
            },
        )
        for report in self.target_reports:
            if not isinstance(report, PMReviewReadinessTargetReport):
                raise PMReviewReadinessReportError(
                    "target_reports must contain PMReviewReadinessTargetReport values."
                )
        object.__setattr__(self, "totals", _validate_count_map(self.totals, _COUNT_KEYS))
        object.__setattr__(
            self,
            "pm_review_dispatch_skipped_by_reason_totals",
            _validate_dynamic_count_map(self.pm_review_dispatch_skipped_by_reason_totals),
        )
        object.__setattr__(
            self,
            "stuck_totals",
            _validate_count_map(self.stuck_totals, _STUCK_KEYS),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "workspace_ready": self.workspace_ready,
            "workspace_ready_error": self.workspace_ready_error,
            "schema_version": self.schema_version,
            "migration_id": self.migration_id,
            "baseline_source_types_by_target": dict(self.baseline_source_types_by_target),
            "target_reports": [report.to_json_payload() for report in self.target_reports],
            "totals": dict(self.totals),
            "pm_review_dispatch_skipped_by_reason_totals": dict(
                self.pm_review_dispatch_skipped_by_reason_totals
            ),
            "stuck_totals": dict(self.stuck_totals),
        }


def build_pm_review_readiness_report(
    *,
    layout: WorkspaceLayout,
    target_keys: tuple[str, ...],
    migration_id: str = DEFAULT_CUTOVER_MIGRATION_ID,
    expected_schema_version: str = CUTOVER_RUNTIME_SCHEMA_VERSION,
) -> PMReviewReadinessReport:
    """Build a deterministic, read-only PMReview runtime readiness report."""

    if not isinstance(layout, WorkspaceLayout):
        raise PMReviewReadinessReportError("layout must be a WorkspaceLayout instance.")
    normalized_targets = _normalize_target_keys(target_keys)
    schema_payload = _read_json_object_optional(_schema_path(layout))
    baseline_payload = _read_json_object_optional(_baseline_path(layout, migration_id))
    workspace_ready = True
    workspace_ready_error: str | None = None
    try:
        validate_pm_review_workspace_ready(
            layout=layout,
            target_keys=normalized_targets,
            migration_id=migration_id,
            expected_schema_version=expected_schema_version,
        )
    except PMReviewWorkspaceMigrationError as exc:
        workspace_ready = False
        workspace_ready_error = str(exc)

    baseline_source_types = _baseline_source_types_by_target(baseline_payload)
    target_reports = tuple(
        _build_target_report(
            layout=layout,
            target_key=target_key,
            baseline_source_type=baseline_source_types.get(target_key),
        )
        for target_key in normalized_targets
    )
    return PMReviewReadinessReport(
        workspace_ready=workspace_ready,
        workspace_ready_error=workspace_ready_error,
        schema_version=_optional_text(schema_payload, "schema_version"),
        migration_id=_optional_text(schema_payload, "migration_id"),
        baseline_source_types_by_target=baseline_source_types,
        target_reports=target_reports,
        totals=_sum_count_maps(report.counts for report in target_reports),
        pm_review_dispatch_skipped_by_reason_totals=_sum_dynamic_count_maps(
            report.pm_review_dispatch_skipped_by_reason for report in target_reports
        ),
        stuck_totals=_sum_count_maps(report.stuck_counts for report in target_reports),
    )


def render_pm_review_readiness_markdown(report: PMReviewReadinessReport) -> str:
    """Render a concise operator-facing readiness report."""

    if not isinstance(report, PMReviewReadinessReport):
        raise PMReviewReadinessReportError(
            "report must be a PMReviewReadinessReport instance."
        )
    lines = [
        "# PMReview Runtime Readiness",
        "",
        f"- Workspace Ready: `{report.workspace_ready}`",
        f"- Workspace Ready Error: `{report.workspace_ready_error or 'None'}`",
        f"- Schema Version: `{report.schema_version or 'missing'}`",
        f"- Migration ID: `{report.migration_id or 'missing'}`",
        "- Baseline Source Types By Target: "
        f"`{json.dumps(dict(report.baseline_source_types_by_target), sort_keys=True)}`",
        "",
        "## PM Lifecycle Totals",
        f"`{json.dumps(dict(report.totals), sort_keys=True)}`",
        "",
        "## PM Dispatch Skipped Totals By Reason",
        f"`{json.dumps(dict(report.pm_review_dispatch_skipped_by_reason_totals), sort_keys=True)}`",
        "",
        "## Stuck Lifecycle Totals",
        f"`{json.dumps(dict(report.stuck_totals), sort_keys=True)}`",
        "",
        "## Targets",
    ]
    for target_report in report.target_reports:
        lines.extend(
            (
                f"### {target_report.target_key}",
                f"- Baseline Source Type: `{target_report.baseline_source_type or 'missing'}`",
                f"- Market Setup Dashboard Present: `{target_report.market_setup_dashboard_present}`",
                f"- Portfolio State Present: `{target_report.portfolio_state_present}`",
                f"- Counts: `{json.dumps(dict(target_report.counts), sort_keys=True)}`",
                "- PM Dispatch Skipped By Reason: "
                f"`{json.dumps(dict(target_report.pm_review_dispatch_skipped_by_reason), sort_keys=True)}`",
                f"- Stuck Counts: `{json.dumps(dict(target_report.stuck_counts), sort_keys=True)}`",
                "",
            )
        )
    return "\n".join(lines)


def _build_target_report(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    baseline_source_type: str | None,
) -> PMReviewReadinessTargetReport:
    lifecycle_summary = build_pm_runtime_lifecycle_summary(layout=layout, target_key=target_key)
    assessments = tuple(
        item.record for item in AnalysisAssessmentStore(layout).read_records(target_key=target_key)
    )
    requests = tuple(
        item.record for item in PMReviewRequestStore(layout).read_records(target_key=target_key)
    )
    anchors = tuple(
        item.record for item in CandidateReviewAnchorStore(layout).read_records(target_key=target_key)
    )
    failures = tuple(
        item.record for item in PMReviewFailureStore(layout).read_records(target_key=target_key)
    )
    position_review_triggers = tuple(
        item.record
        for item in PMPositionReviewTriggerStore(layout).read_records(target_key=target_key)
    )
    decisions = tuple(item.record for item in PMDecisionStore(layout).read_records(target_key=target_key))
    executions = tuple(
        item.record for item in ExecutionRecordStore(layout).read_records(target_key=target_key)
    )
    state_changes = read_state_changes(layout, target_key)
    portfolio_state = PortfolioStateStore(layout).read(target_key=target_key)
    dispatch_considerations = tuple(
        item.record
        for item in PMReviewDispatchConsiderationStore(layout).read_records(target_key=target_key)
    )

    decisions_by_request = _group_by(
        (decision.pm_review_request_id, decision.decision_id)
        for decision in decisions
        if decision.pm_review_request_id is not None
    )
    failures_by_request = _group_by(
        (failure.pm_review_request_id, failure.failure_id) for failure in failures
    )
    latest_failure_by_request = {
        request_id: sorted(
            (failure for failure in failures if failure.pm_review_request_id == request_id),
            key=lambda failure: (failure.failed_at, failure.failure_id),
        )[-1]
        for request_id in failures_by_request
    }
    retryable_failure_request_ids = {
        request_id
        for request_id, failure in latest_failure_by_request.items()
        if failure.retry_allowed
    }
    sidecar_execution_ids = {
        state_change.execution_record_id
        for state_change in state_changes
        if state_change.source_kind == "pm_execution_sidecar"
        and state_change.execution_record_id is not None
    }
    latest_dispatch_by_request = _latest_dispatch_considerations_by_request(dispatch_considerations)
    terminally_skipped_request_ids = {
        request_id
        for request_id, consideration in latest_dispatch_by_request.items()
        if consideration.outcome == "skipped"
        and is_terminal_pm_review_policy_skip_reason(consideration.skip_reason)
    }
    anchor_by_id = {anchor.anchor_id: anchor for anchor in anchors}
    stuck_counts = dict.fromkeys(_STUCK_KEYS, 0)
    for request in requests:
        if (
            not decisions_by_request.get(request.request_id)
            and request.request_id not in terminally_skipped_request_ids
        ):
            stuck_counts["request_without_pm_decision"] += 1
            if failures_by_request.get(request.request_id):
                stuck_counts["request_without_pm_decision_with_failure"] += 1
            else:
                stuck_counts["request_without_pm_decision_without_failure"] += 1
            if request.request_id in retryable_failure_request_ids:
                stuck_counts["request_without_pm_decision_retryable_failure"] += 1
        if _candidate_request_requires_anchor(request):
            anchor = None if request.candidate_anchor_id is None else anchor_by_id.get(request.candidate_anchor_id)
            if anchor is None:
                stuck_counts["candidate_request_missing_anchor"] += 1
            elif anchor.expires_at <= request.business_at:
                stuck_counts["candidate_request_expired_anchor"] += 1
    for execution in executions:
        if execution.status == "executed" and execution.execution_record_id not in sidecar_execution_ids:
            stuck_counts["execution_without_sidecar"] += 1

    counts = {
        "analysis_assessment_count": len(assessments),
        "pm_review_request_count": len(requests),
        "analysis_origin_pm_review_request_count": lifecycle_summary.analysis_origin_pm_review_request_count,
        "trigger_driven_pm_review_request_count": lifecycle_summary.trigger_driven_pm_review_request_count,
        "pm_position_review_trigger_count": len(position_review_triggers),
        "pm_review_dispatch_considered_count": lifecycle_summary.pm_review_dispatch_considered_count,
        "pm_review_dispatch_skipped_count": lifecycle_summary.pm_review_dispatch_skipped_count,
        "pm_review_request_with_decision_count": lifecycle_summary.pm_review_request_with_decision_count,
        "pm_review_flat_decision_count": lifecycle_summary.pm_review_flat_decision_count,
        "pm_review_non_flat_decision_count": lifecycle_summary.pm_review_non_flat_decision_count,
        "pm_decision_noop_count": lifecycle_summary.pm_decision_noop_count,
        "pm_decision_execution_required_count": (
            lifecycle_summary.pm_decision_execution_required_count
        ),
        "pm_review_failure_count": len(failures),
        "candidate_anchor_count": len(anchors),
        "pm_decision_count": len(decisions),
        "execution_record_count": len(executions),
        "pm_sidecar_view_state_change_count": sum(
            1
            for state_change in state_changes
            if state_change.source_kind == "pm_execution_sidecar"
        ),
    }
    return PMReviewReadinessTargetReport(
        target_key=target_key,
        baseline_source_type=baseline_source_type,
        market_setup_dashboard_present=_market_setup_dashboard_present(
            layout=layout,
            target_key=target_key,
        ),
        portfolio_state_present=portfolio_state is not None,
        counts=counts,
        pm_review_dispatch_skipped_by_reason=lifecycle_summary.pm_review_dispatch_skipped_by_reason,
        stuck_counts=stuck_counts,
    )


def _normalize_target_keys(target_keys: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(target_keys, tuple):
        raise PMReviewReadinessReportError("target_keys must be a tuple.")
    normalized = tuple(
        validate_target_key(target_key, error_type=PMReviewReadinessReportError)
        for target_key in target_keys
    )
    if not normalized:
        raise PMReviewReadinessReportError("target_keys must not be empty.")
    if len(set(normalized)) != len(normalized):
        raise PMReviewReadinessReportError("target_keys must be unique.")
    return normalized


def _schema_path(layout: WorkspaceLayout) -> Path:
    return (layout.runtime_root / "schema.json").resolve(strict=False)


def _baseline_path(layout: WorkspaceLayout, migration_id: str) -> Path:
    return (layout.runtime_root / "migrations" / migration_id / "baseline_portfolio_state.json").resolve(
        strict=False
    )


def _read_json_object_optional(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PMReviewReadinessReportError(
            f"readiness report artifact is unreadable: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise PMReviewReadinessReportError(
            f"readiness report artifact must be a JSON object: {path}"
        )
    return payload


def _baseline_source_types_by_target(
    baseline_payload: Mapping[str, object] | None,
) -> dict[str, str]:
    if baseline_payload is None:
        return {}
    targets = baseline_payload.get("targets")
    if not isinstance(targets, list):
        return {}
    source_types: dict[str, str] = {}
    for item in targets:
        if not isinstance(item, Mapping):
            continue
        target_key = item.get("target_key")
        source_type = item.get("source_type")
        if isinstance(target_key, str) and isinstance(source_type, str):
            source_types[target_key] = source_type
    return source_types


def _optional_text(payload: Mapping[str, object] | None, field_name: str) -> str | None:
    if payload is None:
        return None
    value = payload.get(field_name)
    if value is None:
        return None
    return _non_blank(value, field_name)


def _market_setup_dashboard_present(
    *,
    layout: WorkspaceLayout,
    target_key: str,
) -> bool:
    thesis_path = (layout.targets_root / target_key / "thesis.md").resolve(strict=False)
    if not thesis_path.exists() or not thesis_path.is_file():
        return False
    try:
        content = thesis_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PMReviewReadinessReportError(
            f"failed to read Market Setup Dashboard page: {thesis_path}"
        ) from exc
    return "## Market Setup Dashboard" in content


def _candidate_request_requires_anchor(request) -> bool:
    return request.source == "flat_candidate" or "flat_candidate_setup" in request.review_reasons


def _group_by(pairs) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for key, value in pairs:
        grouped.setdefault(key, []).append(value)
    return {key: tuple(values) for key, values in grouped.items()}


def _latest_dispatch_considerations_by_request(considerations) -> dict[str, object]:
    latest_by_request: dict[str, object] = {}
    for consideration in considerations:
        existing = latest_by_request.get(consideration.request_id)
        if existing is None or (
            consideration.dispatch_run_until,
            consideration.consideration_id,
        ) > (
            existing.dispatch_run_until,
            existing.consideration_id,
        ):
            latest_by_request[consideration.request_id] = consideration
    return latest_by_request


def _sum_count_maps(maps) -> dict[str, int]:
    totals: dict[str, int] = {}
    for count_map in maps:
        for key, value in count_map.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def _sum_dynamic_count_maps(maps) -> dict[str, int]:
    totals: dict[str, int] = {}
    for count_map in maps:
        for key, value in count_map.items():
            totals[key] = totals.get(key, 0) + value
    return {key: totals[key] for key in sorted(totals)}


def _validate_count_map(
    value: Mapping[str, object],
    expected_keys: tuple[str, ...],
) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise PMReviewReadinessReportError("count map must be a mapping.")
    normalized: dict[str, int] = {}
    for key in expected_keys:
        raw = value.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise PMReviewReadinessReportError(f"{key} must be a non-negative integer.")
        normalized[key] = raw
    return normalized


def _validate_dynamic_count_map(value: Mapping[str, object]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise PMReviewReadinessReportError("count map must be a mapping.")
    normalized: dict[str, int] = {}
    for key, raw in value.items():
        normalized_key = _non_blank(key, "count key")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise PMReviewReadinessReportError(f"{normalized_key} must be a non-negative integer.")
        normalized[normalized_key] = raw
    return {key: normalized[key] for key in sorted(normalized)}


def _non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PMReviewReadinessReportError(f"{field_name} must be a non-blank string.")
    return value.strip()


__all__ = [
    "PMReviewReadinessReport",
    "PMReviewReadinessReportError",
    "PMReviewReadinessTargetReport",
    "build_pm_review_readiness_report",
    "render_pm_review_readiness_markdown",
]
