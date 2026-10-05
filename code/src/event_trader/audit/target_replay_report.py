"""Full target replay audit report over existing runtime artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from event_trader.audit.pm_lifecycle_summary import build_pm_runtime_lifecycle_summary
from event_trader.audit.trace_bundle import (
    REQUIRED_TRACE_ARTIFACT_TYPES,
    TraceBundle,
    build_episode_trace_bundle,
    build_trace_bundle,
)
from event_trader.config import KernelConfigAuditIdentity, load_kernel_config_audit_identity
from event_trader.context_assembly import (
    FileBackedContextPacketStore,
    ReplayVisibilityAuditError,
    load_replay_visibility_audit,
    replay_visibility_audit_path,
)
from event_trader.contracts._validators import validate_target_key
from event_trader.decision_memory import (
    FileBackedDecisionEpisodeStore,
    project_decision_episodes,
)
from event_trader.execution import ExecutionRecordStore
from event_trader.portfolio import PMDecisionStore, PortfolioStateStore
from event_trader.projection.report_artifacts import current_state_artifact_path
from event_trader.replay.checkpoint import replay_checkpoint_path
from event_trader.storage import WorkspaceLayout
from event_trader.validation import read_state_changes

TargetReplayStatus = Literal["passed", "failed"]
_REPORT_DIR_NAME = "target_replay"
_TEXT_QUALITY_MARKERS = (
    ("\ufffd", "replacement_character"),
    ("\u00c3", "latin1_utf8_mojibake"),
    ("\u00c2", "latin1_utf8_mojibake"),
    ("\u00e2\u20ac", "smart_punctuation_mojibake"),
    ("\u8c29", "cjk_mojibake_marker"),
    ("\u748b", "cjk_mojibake_marker"),
)


class TargetReplayReportError(ValueError):
    """Raised when target replay report inputs or payloads are malformed."""


@dataclass(frozen=True, slots=True)
class TargetReplayCheck:
    check_id: str
    status: TargetReplayStatus
    summary: str
    evidence_paths: tuple[Path, ...] = ()
    missing_artifact_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", _non_blank(self.check_id, "check_id"))
        if self.status not in {"passed", "failed"}:
            raise TargetReplayReportError("check status must be passed or failed.")
        object.__setattr__(self, "summary", _non_blank(self.summary, "summary"))
        object.__setattr__(
            self,
            "evidence_paths",
            tuple(_ensure_path(path, "evidence_paths") for path in self.evidence_paths),
        )
        object.__setattr__(
            self,
            "missing_artifact_types",
            tuple(
                _non_blank(item, "missing_artifact_types") for item in self.missing_artifact_types
            ),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "summary": self.summary,
            "evidence_paths": [str(path) for path in self.evidence_paths],
            "missing_artifact_types": list(self.missing_artifact_types),
        }


@dataclass(frozen=True, slots=True)
class FailureRepairSummary:
    status: TargetReplayStatus
    analysis_failed_count: int
    unrepaired_analysis_failed_count: int
    open_attention_count: int
    unrepaired_open_attention_count: int
    repair_record_count: int
    validation_mark_count: int
    reflection_record_count: int
    repair_report_paths: tuple[Path, ...]
    issue_summaries: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed"}:
            raise TargetReplayReportError("failure summary status must be passed or failed.")
        for field_name in (
            "analysis_failed_count",
            "unrepaired_analysis_failed_count",
            "open_attention_count",
            "unrepaired_open_attention_count",
            "repair_record_count",
            "validation_mark_count",
            "reflection_record_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TargetReplayReportError(f"{field_name} must be non-negative.")
        object.__setattr__(
            self,
            "repair_report_paths",
            tuple(_ensure_path(path, "repair_report_paths") for path in self.repair_report_paths),
        )
        object.__setattr__(
            self,
            "issue_summaries",
            tuple(_non_blank(item, "issue_summaries") for item in self.issue_summaries),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "analysis_failed_count": self.analysis_failed_count,
            "unrepaired_analysis_failed_count": self.unrepaired_analysis_failed_count,
            "open_attention_count": self.open_attention_count,
            "unrepaired_open_attention_count": self.unrepaired_open_attention_count,
            "repair_record_count": self.repair_record_count,
            "validation_mark_count": self.validation_mark_count,
            "reflection_record_count": self.reflection_record_count,
            "repair_report_paths": [str(path) for path in self.repair_report_paths],
            "issue_summaries": list(self.issue_summaries),
        }


@dataclass(frozen=True, slots=True)
class TargetReplayReport:
    report_id: str
    status: TargetReplayStatus
    run_id: str
    target_key: str
    commit_hash: str
    config_path: Path
    config_audit: KernelConfigAuditIdentity
    replay_command: str
    generated_at: datetime
    visibility_audit_path: Path
    visibility_audit_status: str | None
    context_packet_counts_by_stage: Mapping[str, int]
    trace_bundle_id: str
    trace_status: TargetReplayStatus
    missing_artifact_types: tuple[str, ...]
    validation_state_change_count: int
    view_state_change_count: int
    view_state_change_source_counts: Mapping[str, int]
    validation_episode_artifact_count: int
    decision_episode_count: int
    decision_episode_status_counts: Mapping[str, int]
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
    pm_decision_count: int
    execution_record_count: int
    execution_source_counts: Mapping[str, int]
    portfolio_state_update_count: int
    portfolio_state_present: bool
    projection_artifact_present: bool
    counterfactual_report_count: int
    replay_event_checkpoint_path: Path
    replay_event_count: int
    failure_repair_summary: FailureRepairSummary
    checks: tuple[TargetReplayCheck, ...]
    json_path: Path | None = None
    markdown_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_id", _non_blank(self.report_id, "report_id"))
        if self.status not in {"passed", "failed"}:
            raise TargetReplayReportError("report status must be passed or failed.")
        object.__setattr__(self, "run_id", _validate_path_segment(self.run_id, "run_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=TargetReplayReportError),
        )
        object.__setattr__(self, "commit_hash", _non_blank(self.commit_hash, "commit_hash"))
        object.__setattr__(self, "config_path", _ensure_path(self.config_path, "config_path"))
        if not isinstance(self.config_audit, KernelConfigAuditIdentity):
            raise TargetReplayReportError("config_audit must be a KernelConfigAuditIdentity.")
        object.__setattr__(
            self, "replay_command", _non_blank(self.replay_command, "replay_command")
        )
        if not isinstance(self.generated_at, datetime):
            raise TargetReplayReportError("generated_at must be a datetime.")
        object.__setattr__(
            self,
            "visibility_audit_path",
            _ensure_path(self.visibility_audit_path, "visibility_audit_path"),
        )
        if self.visibility_audit_status is not None:
            object.__setattr__(
                self,
                "visibility_audit_status",
                _non_blank(self.visibility_audit_status, "visibility_audit_status"),
            )
        object.__setattr__(
            self,
            "context_packet_counts_by_stage",
            _validate_counts(self.context_packet_counts_by_stage, "context_packet_counts_by_stage"),
        )
        object.__setattr__(
            self, "trace_bundle_id", _non_blank(self.trace_bundle_id, "trace_bundle_id")
        )
        if self.trace_status not in {"passed", "failed"}:
            raise TargetReplayReportError("trace_status must be passed or failed.")
        object.__setattr__(
            self,
            "missing_artifact_types",
            tuple(
                _non_blank(item, "missing_artifact_types") for item in self.missing_artifact_types
            ),
        )
        for field_name in (
            "validation_state_change_count",
            "view_state_change_count",
            "validation_episode_artifact_count",
            "decision_episode_count",
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
            "pm_decision_count",
            "execution_record_count",
            "portfolio_state_update_count",
            "counterfactual_report_count",
            "replay_event_count",
        ):
            _validate_non_negative_int(getattr(self, field_name), field_name)
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
            "pm_review_dispatch_skipped_by_reason",
            _validate_counts(
                self.pm_review_dispatch_skipped_by_reason,
                "pm_review_dispatch_skipped_by_reason",
            ),
        )
        object.__setattr__(
            self,
            "decision_episode_status_counts",
            _validate_counts(self.decision_episode_status_counts, "decision_episode_status_counts"),
        )
        object.__setattr__(
            self,
            "execution_source_counts",
            _validate_counts(self.execution_source_counts, "execution_source_counts"),
        )
        if not isinstance(self.portfolio_state_present, bool):
            raise TargetReplayReportError("portfolio_state_present must be a bool.")
        if not isinstance(self.projection_artifact_present, bool):
            raise TargetReplayReportError("projection_artifact_present must be a bool.")
        object.__setattr__(
            self,
            "replay_event_checkpoint_path",
            _ensure_path(self.replay_event_checkpoint_path, "replay_event_checkpoint_path"),
        )
        if not isinstance(self.failure_repair_summary, FailureRepairSummary):
            raise TargetReplayReportError("failure_repair_summary must be FailureRepairSummary.")
        for check in self.checks:
            if not isinstance(check, TargetReplayCheck):
                raise TargetReplayReportError("checks must contain TargetReplayCheck values.")
        if self.json_path is not None:
            object.__setattr__(self, "json_path", _ensure_path(self.json_path, "json_path"))
        if self.markdown_path is not None:
            object.__setattr__(
                self, "markdown_path", _ensure_path(self.markdown_path, "markdown_path")
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "status": self.status,
            "run_id": self.run_id,
            "target_key": self.target_key,
            "commit_hash": self.commit_hash,
            "config_path": str(self.config_path),
            "config_audit": self.config_audit.to_json_payload(),
            "replay_command": self.replay_command,
            "generated_at": self.generated_at.isoformat(),
            "visibility_audit_path": str(self.visibility_audit_path),
            "visibility_audit_status": self.visibility_audit_status,
            "context_packet_counts_by_stage": dict(self.context_packet_counts_by_stage),
            "trace_bundle_id": self.trace_bundle_id,
            "trace_status": self.trace_status,
            "missing_artifact_types": list(self.missing_artifact_types),
            "validation_state_change_count": self.validation_state_change_count,
            "view_state_change_count": self.view_state_change_count,
            "view_state_change_source_counts": dict(self.view_state_change_source_counts),
            "validation_episode_artifact_count": self.validation_episode_artifact_count,
            "decision_episode_count": self.decision_episode_count,
            "decision_episode_status_counts": dict(self.decision_episode_status_counts),
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
            "pm_decision_count": self.pm_decision_count,
            "execution_record_count": self.execution_record_count,
            "execution_source_counts": dict(self.execution_source_counts),
            "portfolio_state_update_count": self.portfolio_state_update_count,
            "portfolio_state_present": self.portfolio_state_present,
            "projection_artifact_present": self.projection_artifact_present,
            "counterfactual_report_count": self.counterfactual_report_count,
            "replay_event_checkpoint_path": str(self.replay_event_checkpoint_path),
            "replay_event_count": self.replay_event_count,
            "failure_repair_summary": self.failure_repair_summary.to_json_payload(),
            "checks": [check.to_json_payload() for check in self.checks],
            "json_path": None if self.json_path is None else str(self.json_path),
            "markdown_path": None if self.markdown_path is None else str(self.markdown_path),
        }


@dataclass(frozen=True, slots=True)
class PersistedTargetReplayReport:
    report: TargetReplayReport
    json_path: Path
    markdown_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.report, TargetReplayReport):
            raise TargetReplayReportError("report must be a TargetReplayReport.")
        object.__setattr__(self, "json_path", _ensure_path(self.json_path, "json_path"))
        object.__setattr__(self, "markdown_path", _ensure_path(self.markdown_path, "markdown_path"))

    def to_json_payload(self) -> dict[str, object]:
        return self.report.to_json_payload()


def build_target_replay_report(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
    event_id: str,
    view_episode_id: str,
    decision_episode_id: str,
    commit_hash: str,
    config_path: Path,
    replay_command: str,
    generated_at: datetime | None = None,
) -> TargetReplayReport:
    if not isinstance(layout, WorkspaceLayout):
        raise TargetReplayReportError("layout must be a WorkspaceLayout instance.")
    normalized_run_id = _validate_path_segment(run_id, "run_id")
    normalized_target_key = validate_target_key(target_key, error_type=TargetReplayReportError)
    normalized_event_id = _non_blank(event_id, "event_id")
    normalized_view_episode_id = _non_blank(view_episode_id, "view_episode_id")
    normalized_decision_episode_id = _non_blank(decision_episode_id, "decision_episode_id")
    normalized_commit_hash = _non_blank(commit_hash, "commit_hash")
    normalized_replay_command = _non_blank(replay_command, "replay_command")
    normalized_config_path = _ensure_path(config_path, "config_path")
    config_audit = load_kernel_config_audit_identity(normalized_config_path)
    report_generated_at = (generated_at or datetime.now(UTC)).astimezone(UTC)

    visibility_path = replay_visibility_audit_path(layout, run_id=normalized_run_id)
    visibility_status, visibility_check = _build_visibility_check(
        layout=layout,
        run_id=normalized_run_id,
        visibility_path=visibility_path,
    )
    context_counts, context_check = _build_context_packet_check(
        layout=layout,
        run_id=normalized_run_id,
        target_key=normalized_target_key,
    )
    trace_bundle = _build_trace_bundle(
        layout=layout,
        target_key=normalized_target_key,
        event_id=normalized_event_id,
        view_episode_id=normalized_view_episode_id,
        decision_episode_id=normalized_decision_episode_id,
        created_at=report_generated_at,
    )
    missing_artifact_types = _missing_required_artifact_types(trace_bundle)
    trace_status: TargetReplayStatus = "passed" if not missing_artifact_types else "failed"
    trace_check = TargetReplayCheck(
        check_id="trace_artifacts",
        status=trace_status,
        summary=(
            "All required trace artifact types are present."
            if trace_status == "passed"
            else "Required trace artifact types are missing."
        ),
        evidence_paths=tuple(
            artifact.path for artifact in trace_bundle.artifacts if artifact.status == "available"
        ),
        missing_artifact_types=missing_artifact_types,
    )
    decision_records = FileBackedDecisionEpisodeStore(layout).read_records(
        target_key=normalized_target_key
    )
    decision_projections = project_decision_episodes(decision_records)
    failure_summary = _build_failure_repair_summary(
        projections=decision_projections,
        repair_report_paths=_repair_report_paths(layout, target_key=normalized_target_key),
    )
    failure_check = TargetReplayCheck(
        check_id="failure_repair_lineage",
        status=failure_summary.status,
        summary=(
            "No unrepaired failed or open decision episodes found."
            if failure_summary.status == "passed"
            else "Unrepaired failed or open decision episodes found."
        ),
        evidence_paths=failure_summary.repair_report_paths,
    )
    metadata_check = _build_metadata_check(
        config_path=normalized_config_path,
        config_audit=config_audit,
    )
    state_changes = read_state_changes(
        layout,
        normalized_target_key,
    )
    pm_lifecycle_summary = build_pm_runtime_lifecycle_summary(
        layout=layout,
        target_key=normalized_target_key,
    )
    episode_artifact_count = _count_existing_files(
        layout.runtime_root / "validation" / "episodes" / normalized_target_key,
        "*.json",
    )
    pm_decisions = PMDecisionStore(layout).read_records(
        target_key=normalized_target_key,
    )
    execution_records = ExecutionRecordStore(layout).read_records(
        target_key=normalized_target_key,
    )
    portfolio_state = PortfolioStateStore(layout).read(target_key=normalized_target_key)
    portfolio_state_present = portfolio_state is not None
    projection_artifact = layout.runtime_root / current_state_artifact_path(normalized_target_key)
    projection_artifact_present = projection_artifact.exists() and projection_artifact.is_file()
    counterfactual_count = _count_existing_files(
        layout.runtime_root / "evaluation" / "counterfactuals" / normalized_target_key,
        "*.json",
    )
    checkpoint_path = replay_checkpoint_path(layout, normalized_target_key)
    replay_event_count = _count_jsonl_lines(checkpoint_path, run_key=normalized_run_id)
    execution_semantics_check = _build_execution_semantics_check(
        state_changes=state_changes,
        pm_decisions=pm_decisions,
        execution_records=execution_records,
    )
    projection_state_check = _build_projection_state_check(
        validation_state_change_count=len(state_changes),
        pm_decisions=pm_decisions,
        execution_records=execution_records,
        portfolio_state=portfolio_state,
        projection_artifact_present=projection_artifact_present,
    )
    counterfactual_check = _build_counterfactual_check(
        trace_status=trace_status,
        counterfactual_report_count=counterfactual_count,
    )
    text_quality_check = _build_text_quality_check(
        layout=layout,
        target_key=normalized_target_key,
        projection_artifact=projection_artifact,
    )
    checks = (
        metadata_check,
        visibility_check,
        context_check,
        trace_check,
        failure_check,
        execution_semantics_check,
        projection_state_check,
        counterfactual_check,
        text_quality_check,
    )
    status: TargetReplayStatus = (
        "failed" if any(check.status == "failed" for check in checks) else "passed"
    )
    return TargetReplayReport(
        report_id=_report_id(
            run_id=normalized_run_id,
            target_key=normalized_target_key,
            commit_hash=normalized_commit_hash,
            trace_bundle_id=trace_bundle.bundle_id,
        ),
        status=status,
        run_id=normalized_run_id,
        target_key=normalized_target_key,
        commit_hash=normalized_commit_hash,
        config_path=normalized_config_path,
        config_audit=config_audit,
        replay_command=normalized_replay_command,
        generated_at=report_generated_at,
        visibility_audit_path=visibility_path,
        visibility_audit_status=visibility_status,
        context_packet_counts_by_stage=context_counts,
        trace_bundle_id=trace_bundle.bundle_id,
        trace_status=trace_status,
        missing_artifact_types=missing_artifact_types,
        validation_state_change_count=len(state_changes),
        view_state_change_count=pm_lifecycle_summary.view_state_change_count,
        view_state_change_source_counts=pm_lifecycle_summary.view_state_change_source_counts,
        validation_episode_artifact_count=episode_artifact_count,
        decision_episode_count=len(decision_projections),
        decision_episode_status_counts=_status_counts(decision_projections),
        analysis_assessment_count=pm_lifecycle_summary.analysis_assessment_count,
        pm_review_request_count=pm_lifecycle_summary.pm_review_request_count,
        analysis_origin_pm_review_request_count=(
            pm_lifecycle_summary.analysis_origin_pm_review_request_count
        ),
        trigger_driven_pm_review_request_count=(
            pm_lifecycle_summary.trigger_driven_pm_review_request_count
        ),
        pm_position_review_trigger_count=(
            pm_lifecycle_summary.pm_position_review_trigger_count
        ),
        pm_review_dispatch_considered_count=(
            pm_lifecycle_summary.pm_review_dispatch_considered_count
        ),
        pm_review_dispatch_skipped_count=pm_lifecycle_summary.pm_review_dispatch_skipped_count,
        pm_review_dispatch_skipped_by_reason=(
            pm_lifecycle_summary.pm_review_dispatch_skipped_by_reason
        ),
        pm_review_request_with_decision_count=(
            pm_lifecycle_summary.pm_review_request_with_decision_count
        ),
        pm_review_flat_decision_count=(
            pm_lifecycle_summary.pm_review_flat_decision_count
        ),
        pm_review_non_flat_decision_count=(
            pm_lifecycle_summary.pm_review_non_flat_decision_count
        ),
        pm_decision_count=pm_lifecycle_summary.pm_decision_count,
        execution_record_count=pm_lifecycle_summary.execution_count,
        execution_source_counts=pm_lifecycle_summary.execution_source_counts,
        portfolio_state_update_count=pm_lifecycle_summary.portfolio_state_update_count,
        portfolio_state_present=portfolio_state_present,
        projection_artifact_present=projection_artifact_present,
        counterfactual_report_count=counterfactual_count,
        replay_event_checkpoint_path=checkpoint_path,
        replay_event_count=replay_event_count,
        failure_repair_summary=failure_summary,
        checks=checks,
    )


def write_target_replay_report(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
    event_id: str,
    view_episode_id: str,
    decision_episode_id: str,
    commit_hash: str,
    config_path: Path,
    replay_command: str,
    generated_at: datetime | None = None,
) -> PersistedTargetReplayReport:
    report = build_target_replay_report(
        layout=layout,
        run_id=run_id,
        target_key=target_key,
        event_id=event_id,
        view_episode_id=view_episode_id,
        decision_episode_id=decision_episode_id,
        commit_hash=commit_hash,
        config_path=config_path,
        replay_command=replay_command,
        generated_at=generated_at,
    )
    json_path, markdown_path = target_replay_report_paths(
        layout,
        run_id=report.run_id,
        target_key=report.target_key,
    )
    persisted_report = replace(report, json_path=json_path, markdown_path=markdown_path)
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(
                persisted_report.to_json_payload(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(_render_markdown(persisted_report), encoding="utf-8")
    except OSError as exc:
        raise TargetReplayReportError(f"Failed to write target replay report: {exc}") from exc
    return PersistedTargetReplayReport(
        report=persisted_report,
        json_path=json_path,
        markdown_path=markdown_path,
    )


def load_target_replay_report(path: Path) -> PersistedTargetReplayReport:
    json_path = _ensure_path(path, "path")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TargetReplayReportError(
            f"Failed to read target replay report {json_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise TargetReplayReportError(f"Target replay report is invalid JSON: {json_path}") from exc
    report = _parse_report_payload(payload)
    return PersistedTargetReplayReport(
        report=report,
        json_path=json_path,
        markdown_path=report.markdown_path or json_path.with_suffix(".md"),
    )


def target_replay_report_paths(
    layout: WorkspaceLayout,
    *,
    run_id: str,
    target_key: str,
) -> tuple[Path, Path]:
    if not isinstance(layout, WorkspaceLayout):
        raise TargetReplayReportError("layout must be a WorkspaceLayout instance.")
    normalized_run_id = _validate_path_segment(run_id, "run_id")
    normalized_target_key = validate_target_key(target_key, error_type=TargetReplayReportError)
    root = layout.runtime_root / "audit" / _REPORT_DIR_NAME / normalized_run_id
    return (
        (root / f"{normalized_target_key}.json").resolve(strict=False),
        (root / f"{normalized_target_key}.md").resolve(strict=False),
    )


def _build_visibility_check(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    visibility_path: Path,
) -> tuple[str | None, TargetReplayCheck]:
    try:
        payload = load_replay_visibility_audit(layout=layout, run_id=run_id)
    except ReplayVisibilityAuditError as exc:
        return None, TargetReplayCheck(
            check_id="replay_visibility_audit",
            status="failed",
            summary=f"Replay visibility audit is missing or unreadable: {exc}",
            evidence_paths=(visibility_path,),
        )
    status = payload.get("status")
    normalized_status = status if isinstance(status, str) else None
    return normalized_status, TargetReplayCheck(
        check_id="replay_visibility_audit",
        status="passed" if normalized_status == "passed" else "failed",
        summary=(
            "Replay visibility audit passed."
            if normalized_status == "passed"
            else f"Replay visibility audit status is {normalized_status!r}."
        ),
        evidence_paths=(visibility_path,),
    )


def _build_metadata_check(
    *,
    config_path: Path,
    config_audit: KernelConfigAuditIdentity,
) -> TargetReplayCheck:
    config_exists = config_path.exists() and config_path.is_file()
    config_valid = config_exists and config_audit.load_error is None
    return TargetReplayCheck(
        check_id="target_replay_metadata",
        status="passed" if config_valid else "failed",
        summary=(
            "Target replay metadata is complete."
            if config_valid
            else (
                "Target replay config path is missing or not a file."
                if not config_exists
                else f"Target replay config audit identity failed: {config_audit.load_error}"
            )
        ),
        evidence_paths=(config_path,),
    )


def _build_context_packet_check(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
) -> tuple[dict[str, int], TargetReplayCheck]:
    try:
        packets = FileBackedContextPacketStore(
            layout,
            runtime_scope="replay",
            run_id=run_id,
        ).read_all_packets()
    except Exception as exc:
        return {}, TargetReplayCheck(
            check_id="target_replay_context_packets",
            status="failed",
            summary=f"Replay context packets could not be read: {exc}",
        )
    counts = {"analysis": 0, "checker": 0}
    for record in packets:
        if record.packet.target_key == target_key and record.packet.stage in counts:
            counts[record.packet.stage] += 1
    total_count = sum(counts.values())
    return counts, TargetReplayCheck(
        check_id="target_replay_context_packets",
        status="passed" if total_count else "failed",
        summary=(
            f"Found {total_count} replay context packet(s) for target {target_key}."
            if total_count
            else f"No replay context packets were found for target {target_key}."
        ),
    )


def _build_execution_semantics_check(
    *,
    state_changes,
    pm_decisions,
    execution_records,
) -> TargetReplayCheck:
    requires_execution = bool(state_changes or pm_decisions)
    if not requires_execution:
        return TargetReplayCheck(
            check_id="target_replay_execution_semantics",
            status="passed",
            summary="No validation or PM decision activity requires execution semantics.",
        )
    pm_by_id = {persisted.record.decision_id: persisted.record for persisted in pm_decisions}
    executions = tuple(persisted.record for persisted in execution_records)
    issues: list[str] = []
    for state_change in state_changes:
        state_executions = _executions_for_pm_sidecar(
            state_change=state_change,
            executions=executions,
            pm_by_id=pm_by_id,
        )
        if not state_executions:
            issues.append(
                "PM execution sidecar missing PMDecision-linked execution record: "
                f"{state_change.state_change_id}"
            )
            continue
        for execution in state_executions:
            _collect_execution_lineage_issues(
                execution=execution,
                pm_by_id=pm_by_id,
                issues=issues,
            )
    return TargetReplayCheck(
        check_id="target_replay_execution_semantics",
        status="failed" if issues else "passed",
        summary=(
            "Validation and PM decision activity has linked decision and execution evidence."
            if not issues
            else "Execution semantics inconsistency: " + "; ".join(issues)
        ),
    )


def _executions_for_pm_sidecar(
    *,
    state_change,
    executions: tuple[Any, ...],
    pm_by_id: Mapping[str, Any],
) -> tuple[Any, ...]:
    if state_change.execution_record_id is None:
        return ()
    for execution in executions:
        if execution.execution_record_id != state_change.execution_record_id:
            continue
        pm = pm_by_id.get(execution.pm_decision_id)
        if pm is None:
            continue
        if execution.pm_decision_id != state_change.pm_decision_id:
            continue
        return (execution,)
    return ()


def _collect_execution_lineage_issues(
    *,
    execution,
    pm_by_id: Mapping[str, Any],
    issues: list[str],
) -> None:
    pm = pm_by_id.get(execution.pm_decision_id)
    if pm is None:
        issues.append(f"execution missing PM decision: {execution.execution_record_id}")
        return
    if pm.decision_episode_id != execution.decision_episode_id:
        issues.append(f"PM decision episode mismatch: {pm.decision_id}")


def _build_projection_state_check(
    *,
    validation_state_change_count: int,
    pm_decisions,
    execution_records,
    portfolio_state,
    projection_artifact_present: bool,
) -> TargetReplayCheck:
    execution_records_by_id = {
        persisted.record.execution_record_id: persisted.record for persisted in execution_records
    }
    pm_decisions_by_id = {
        persisted.record.decision_id: persisted.record for persisted in pm_decisions
    }
    executed_records = tuple(
        execution
        for execution in execution_records_by_id.values()
        if execution.status == "executed"
    )
    has_activity = any(
        count > 0
        for count in (
            validation_state_change_count,
            len(pm_decisions_by_id),
            len(execution_records_by_id),
        )
    )
    if not has_activity:
        return TargetReplayCheck(
            check_id="target_replay_projection_state",
            status="passed",
            summary="No validation, portfolio, or execution activity requires projection state.",
        )
    issues = []
    if not projection_artifact_present:
        issues.append("missing projection artifact")
    if executed_records and portfolio_state is None:
        issues.append("executed record missing portfolio state")
    if portfolio_state is not None:
        state_pm = pm_decisions_by_id.get(portfolio_state.source_pm_decision_id)
        state_execution = execution_records_by_id.get(portfolio_state.source_execution_record_id)
        if state_pm is None:
            issues.append("portfolio state source PM decision is missing")
        if state_execution is None:
            issues.append("portfolio state source execution record is missing")
        elif state_execution.status != "executed":
            issues.append("portfolio state source execution record is not executed")
        if state_pm is not None and state_execution is not None:
            if state_execution.pm_decision_id != state_pm.decision_id:
                issues.append("portfolio execution/PM decision source mismatch")
            if state_execution.decision_episode_id != state_pm.decision_episode_id:
                issues.append("portfolio execution/PM decision episode mismatch")
        if state_execution is not None:
            if state_execution.decision_episode_id != portfolio_state.decision_episode_id:
                issues.append("portfolio state episode mismatch")
            if state_execution.target_weight != portfolio_state.target_weight:
                issues.append("portfolio state target weight mismatch")
        latest_executed = max(
            executed_records,
            key=lambda item: (item.executed_at or item.business_at, item.execution_record_id),
            default=None,
        )
        if (
            latest_executed is not None
            and portfolio_state.source_execution_record_id
            != latest_executed.execution_record_id
        ):
            issues.append("portfolio state does not point to latest executed record")
    return TargetReplayCheck(
        check_id="target_replay_projection_state",
        status="failed" if issues else "passed",
        summary=(
            "Projection artifact and portfolio state are consistent with execution truth."
            if not issues
            else "Projection/state inconsistency: " + "; ".join(issues)
        ),
    )


def _build_counterfactual_check(
    *,
    trace_status: TargetReplayStatus,
    counterfactual_report_count: int,
) -> TargetReplayCheck:
    if trace_status != "passed":
        return TargetReplayCheck(
            check_id="target_replay_counterfactual",
            status="passed",
            summary="Trace artifact checks already record missing counterfactual evidence.",
        )
    return TargetReplayCheck(
        check_id="target_replay_counterfactual",
        status="passed" if counterfactual_report_count > 0 else "failed",
        summary=(
            "Counterfactual report evidence is present."
            if counterfactual_report_count > 0
            else "Trace passed but no counterfactual report file was found."
        ),
    )


def _build_text_quality_check(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    projection_artifact: Path,
) -> TargetReplayCheck:
    scanned_paths = _target_text_quality_paths(
        layout=layout,
        target_key=target_key,
        projection_artifact=projection_artifact,
    )
    issue_paths: list[Path] = []
    issue_count = 0
    samples: list[str] = []
    for path in scanned_paths:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            issue_count += 1
            issue_paths.append(path)
            if len(samples) < 3:
                samples.append(f"{path}: invalid utf-8")
            continue
        path_issue_count, path_samples = _scan_text_quality_issues(path, content)
        if path_issue_count == 0:
            continue
        issue_count += path_issue_count
        issue_paths.append(path)
        samples.extend(path_samples[: max(0, 3 - len(samples))])
    if issue_count == 0:
        return TargetReplayCheck(
            check_id="text_quality",
            status="passed",
            summary=f"No mojibake markers found in {len(scanned_paths)} target text artifacts.",
        )
    sample_text = "; ".join(samples)
    return TargetReplayCheck(
        check_id="text_quality",
        status="failed",
        summary=(
            f"Found {issue_count} mojibake markers across {len(issue_paths)} "
            f"target text artifacts. Samples: {sample_text}"
        ),
        evidence_paths=tuple(issue_paths),
    )


def _target_text_quality_paths(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    projection_artifact: Path,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    target_memory_root = layout.research_memory_root / "targets" / target_key
    if target_memory_root.exists():
        paths.extend(
            path for path in sorted(target_memory_root.rglob("*.md")) if path.is_file()
        )
    if projection_artifact.exists() and projection_artifact.is_file():
        paths.append(projection_artifact)
    return tuple(dict.fromkeys(paths))


def _scan_text_quality_issues(path: Path, content: str) -> tuple[int, list[str]]:
    issue_count = 0
    samples: list[str] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        line_labels = tuple(
            label for marker, label in _TEXT_QUALITY_MARKERS if marker in line
        )
        if not line_labels:
            continue
        issue_count += len(line_labels)
        if len(samples) < 3:
            labels = ",".join(dict.fromkeys(line_labels))
            samples.append(f"{path}:{line_number} {labels}")
    return issue_count, samples


def _build_trace_bundle(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
    view_episode_id: str,
    decision_episode_id: str,
    created_at: datetime,
) -> TraceBundle:
    try:
        return build_episode_trace_bundle(
            layout=layout,
            target_key=target_key,
            event_ids=(event_id,),
            view_episode_id=view_episode_id,
            decision_episode_ids=(decision_episode_id,),
            created_at=created_at,
        )
    except Exception:
        return build_trace_bundle(
            target_key=target_key,
            episode_id=view_episode_id,
            artifact_refs=(),
            created_at=created_at,
            event_ids=(event_id,),
            decision_episode_ids=(decision_episode_id,),
            view_episode_id=view_episode_id,
        )


def _build_failure_repair_summary(
    *,
    projections,
    repair_report_paths: tuple[Path, ...],
) -> FailureRepairSummary:
    analysis_failed_count = 0
    unrepaired_failed_count = 0
    open_attention_count = 0
    unrepaired_open_count = 0
    repair_record_count = 0
    validation_mark_count = 0
    reflection_record_count = 0
    issues: list[str] = []
    for projection in projections:
        repair_record_count += len(projection.repair_records)
        validation_mark_count += len(projection.validation_marks)
        reflection_record_count += len(projection.reflection_records)
        if projection.status == "analysis_failed":
            analysis_failed_count += 1
            if not projection.repair_records and not _has_explicit_failed_lineage(projection):
                unrepaired_failed_count += 1
                issues.append(f"analysis_failed without repair lineage: {projection.episode_id}")
        if projection.status == "open_attention":
            open_attention_count += 1
            if not projection.repair_records:
                unrepaired_open_count += 1
                issues.append(f"open_attention without completed lineage: {projection.episode_id}")
    status: TargetReplayStatus = "failed" if issues else "passed"
    return FailureRepairSummary(
        status=status,
        analysis_failed_count=analysis_failed_count,
        unrepaired_analysis_failed_count=unrepaired_failed_count,
        open_attention_count=open_attention_count,
        unrepaired_open_attention_count=unrepaired_open_count,
        repair_record_count=repair_record_count,
        validation_mark_count=validation_mark_count,
        reflection_record_count=reflection_record_count,
        repair_report_paths=repair_report_paths,
        issue_summaries=tuple(issues),
    )


def _has_explicit_failed_lineage(projection) -> bool:
    record = projection.analysis_record
    if record is None:
        return False
    marker = record.payload.get("explicit_failed_lineage")
    if marker is True:
        return True
    lineage = record.payload.get("failure_lineage")
    return isinstance(lineage, str) and bool(lineage.strip())


def _missing_required_artifact_types(bundle: TraceBundle) -> tuple[str, ...]:
    missing = {artifact.artifact_type for artifact in bundle.missing_artifacts}
    return tuple(
        artifact_type for artifact_type in REQUIRED_TRACE_ARTIFACT_TYPES if artifact_type in missing
    )


def _status_counts(projections) -> dict[str, int]:
    counts: dict[str, int] = {}
    for projection in projections:
        counts[projection.status] = counts.get(projection.status, 0) + 1
    return counts


def _repair_report_paths(layout: WorkspaceLayout, *, target_key: str) -> tuple[Path, ...]:
    replay_root = layout.runtime_root / "repair" / "replay" / target_key
    return tuple(
        path.resolve(strict=False)
        for path in sorted(
            replay_root.glob("*.json"),
            key=lambda path: path.as_posix(),
        )
    )


def _count_existing_files(root: Path, pattern: str) -> int:
    if not root.exists():
        return 0
    if root.is_file():
        return 1
    return sum(1 for path in root.glob(pattern) if path.is_file())


def _count_jsonl_lines(path: Path, *, run_key: str | None = None) -> int:
    if not path.exists() or not path.is_file():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            normalized = line.strip()
            if not normalized:
                continue
            if run_key is None:
                count += 1
                continue
            try:
                payload = json.loads(normalized)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping) and payload.get("run_key") == run_key:
                count += 1
    return count


def _report_id(
    *,
    run_id: str,
    target_key: str,
    commit_hash: str,
    trace_bundle_id: str,
) -> str:
    digest = sha256(f"{run_id}|{target_key}|{commit_hash}|{trace_bundle_id}".encode()).hexdigest()
    return f"target-replay:{run_id}:{target_key}:{digest}"


def _render_markdown(report: TargetReplayReport) -> str:
    lines = [
        f"# Target Replay Report: {report.run_id} / {report.target_key} ({report.status})",
        "",
        f"- Report ID: `{report.report_id}`",
        f"- Generated At: `{report.generated_at.isoformat()}`",
        f"- Pinned Commit: `{report.commit_hash}`",
        f"- Config: `{report.config_path}`",
        f"- Config Hash: `{report.config_audit.config_hash}`",
        "- Replay Command:",
        "",
        "```text",
        report.replay_command,
        "```",
        "",
        "## Checks",
        "",
        "| Check | Status | Summary |",
        "| --- | --- | --- |",
    ]
    for check in report.checks:
        lines.append(
            f"| `{_escape_table(check.check_id)}` | `{check.status}` | "
            f"{_escape_table(check.summary)} |"
        )
    lines.extend(
        [
            "",
            "## Counts",
            "",
            f"- Context packets: `{dict(report.context_packet_counts_by_stage)}`",
            f"- Decision episodes: `{report.decision_episode_count}`",
            f"- State changes: `{report.validation_state_change_count}`",
            f"- View state changes: `{report.view_state_change_count}` "
            f"`{dict(report.view_state_change_source_counts)}`",
            f"- Episode artifacts: `{report.validation_episode_artifact_count}`",
            f"- Analysis assessments: `{report.analysis_assessment_count}`",
            f"- PM review requests: `{report.pm_review_request_count}`",
            (
                "- Analysis-origin PM review requests: "
                f"`{report.analysis_origin_pm_review_request_count}`"
            ),
            (
                "- Trigger-driven PM review requests: "
                f"`{report.trigger_driven_pm_review_request_count}`"
            ),
            (
                "- Persisted PM position review triggers: "
                f"`{report.pm_position_review_trigger_count}`"
            ),
            (
                "- PM dispatch considered/skipped: "
                f"`{report.pm_review_dispatch_considered_count}` / "
                f"`{report.pm_review_dispatch_skipped_count}` "
                f"`{dict(report.pm_review_dispatch_skipped_by_reason)}`"
            ),
            (
                "- PM decision outcomes: "
                f"`{report.pm_review_request_with_decision_count}` request-backed decisions, "
                f"`{report.pm_review_flat_decision_count}` flat, "
                f"`{report.pm_review_non_flat_decision_count}` non-flat"
            ),
            f"- PM decisions: `{report.pm_decision_count}`",
            f"- Execution records: `{report.execution_record_count}` "
            f"`{dict(report.execution_source_counts)}`",
            f"- Portfolio state updates: `{report.portfolio_state_update_count}`",
            f"- Counterfactual reports: `{report.counterfactual_report_count}`",
            "",
            "## Trace",
            "",
            f"- Trace Bundle ID: `{report.trace_bundle_id}`",
            f"- Trace Status: `{report.trace_status}`",
            "",
            "## Missing Artifacts",
            "",
        ]
    )
    lines.extend(
        [f"- `{item}`" for item in report.missing_artifact_types]
        if report.missing_artifact_types
        else ["- None"]
    )
    lines.extend(["", "## Failure And Repair", ""])
    if report.failure_repair_summary.issue_summaries:
        lines.extend(f"- {item}" for item in report.failure_repair_summary.issue_summaries)
    else:
        lines.append("- None")
    lines.extend(["", "## Generated Artifacts", ""])
    if report.json_path is not None:
        lines.append(f"- JSON: `{report.json_path}`")
    if report.markdown_path is not None:
        lines.append(f"- Markdown: `{report.markdown_path}`")
    lines.append("")
    return "\n".join(lines)


def _parse_report_payload(payload: object) -> TargetReplayReport:
    data = _require_mapping(payload, "report")
    return TargetReplayReport(
        report_id=_require_text(data, "report_id"),
        status=_parse_status(data.get("status")),
        run_id=_require_text(data, "run_id"),
        target_key=_require_text(data, "target_key"),
        commit_hash=_require_text(data, "commit_hash"),
        config_path=Path(_require_text(data, "config_path")),
        config_audit=_parse_config_audit(data),
        replay_command=_require_text(data, "replay_command"),
        generated_at=_parse_datetime(data.get("generated_at"), "generated_at"),
        visibility_audit_path=Path(_require_text(data, "visibility_audit_path")),
        visibility_audit_status=_optional_text(data, "visibility_audit_status"),
        context_packet_counts_by_stage=_validate_counts(
            _require_mapping(
                data.get("context_packet_counts_by_stage"),
                "context_packet_counts_by_stage",
            ),
            "context_packet_counts_by_stage",
        ),
        trace_bundle_id=_require_text(data, "trace_bundle_id"),
        trace_status=_parse_status(data.get("trace_status")),
        missing_artifact_types=tuple(
            _require_text_value(item, "missing_artifact_types")
            for item in _require_list(data, "missing_artifact_types")
        ),
        validation_state_change_count=_require_int(data, "validation_state_change_count"),
        view_state_change_count=_optional_int(
            data,
            "view_state_change_count",
            default=_require_int(data, "validation_state_change_count"),
        ),
        view_state_change_source_counts=_validate_counts(
            _optional_mapping(data, "view_state_change_source_counts"),
            "view_state_change_source_counts",
        ),
        validation_episode_artifact_count=_require_int(data, "validation_episode_artifact_count"),
        decision_episode_count=_require_int(data, "decision_episode_count"),
        decision_episode_status_counts=_validate_counts(
            _require_mapping(
                data.get("decision_episode_status_counts"),
                "decision_episode_status_counts",
            ),
            "decision_episode_status_counts",
        ),
        analysis_assessment_count=_optional_int(data, "analysis_assessment_count"),
        pm_review_request_count=_optional_int(data, "pm_review_request_count"),
        analysis_origin_pm_review_request_count=_optional_int(
            data,
            "analysis_origin_pm_review_request_count",
        ),
        trigger_driven_pm_review_request_count=_optional_int(
            data,
            "trigger_driven_pm_review_request_count",
        ),
        pm_position_review_trigger_count=_optional_int(
            data,
            "pm_position_review_trigger_count",
        ),
        pm_review_dispatch_considered_count=_optional_int(
            data,
            "pm_review_dispatch_considered_count",
        ),
        pm_review_dispatch_skipped_count=_optional_int(
            data,
            "pm_review_dispatch_skipped_count",
        ),
        pm_review_dispatch_skipped_by_reason=_validate_counts(
            _optional_mapping(data, "pm_review_dispatch_skipped_by_reason"),
            "pm_review_dispatch_skipped_by_reason",
        ),
        pm_review_request_with_decision_count=_optional_int(
            data,
            "pm_review_request_with_decision_count",
        ),
        pm_review_flat_decision_count=_optional_int(data, "pm_review_flat_decision_count"),
        pm_review_non_flat_decision_count=_optional_int(
            data,
            "pm_review_non_flat_decision_count",
        ),
        pm_decision_count=_require_int(data, "pm_decision_count"),
        execution_record_count=_require_int(data, "execution_record_count"),
        execution_source_counts=_validate_counts(
            _optional_mapping(data, "execution_source_counts"),
            "execution_source_counts",
        ),
        portfolio_state_update_count=_optional_int(data, "portfolio_state_update_count"),
        portfolio_state_present=_require_bool(data, "portfolio_state_present"),
        projection_artifact_present=_require_bool(data, "projection_artifact_present"),
        counterfactual_report_count=_require_int(data, "counterfactual_report_count"),
        replay_event_checkpoint_path=Path(_require_text(data, "replay_event_checkpoint_path")),
        replay_event_count=_require_int(data, "replay_event_count"),
        failure_repair_summary=_parse_failure_summary(data.get("failure_repair_summary")),
        checks=tuple(_parse_check(item) for item in _require_list(data, "checks")),
        json_path=_optional_path(data, "json_path"),
        markdown_path=_optional_path(data, "markdown_path"),
    )


def _parse_failure_summary(payload: object) -> FailureRepairSummary:
    data = _require_mapping(payload, "failure_repair_summary")
    return FailureRepairSummary(
        status=_parse_status(data.get("status")),
        analysis_failed_count=_require_int(data, "analysis_failed_count"),
        unrepaired_analysis_failed_count=_require_int(data, "unrepaired_analysis_failed_count"),
        open_attention_count=_require_int(data, "open_attention_count"),
        unrepaired_open_attention_count=_require_int(data, "unrepaired_open_attention_count"),
        repair_record_count=_require_int(data, "repair_record_count"),
        validation_mark_count=_require_int(data, "validation_mark_count"),
        reflection_record_count=_require_int(data, "reflection_record_count"),
        repair_report_paths=tuple(
            Path(_require_text_value(item, "repair_report_paths"))
            for item in _require_list(data, "repair_report_paths")
        ),
        issue_summaries=tuple(
            _require_text_value(item, "issue_summaries")
            for item in _require_list(data, "issue_summaries")
        ),
    )


def _parse_config_audit(data: Mapping[str, object]) -> KernelConfigAuditIdentity:
    payload = data.get("config_audit")
    if payload is None:
        return KernelConfigAuditIdentity(
            config_path=Path(_require_text(data, "config_path")),
            config_hash=None,
            load_error="config_audit missing from legacy target replay report.",
        )
    try:
        return KernelConfigAuditIdentity.from_json_payload(payload)
    except ValueError as exc:
        raise TargetReplayReportError(f"config_audit is invalid: {exc}") from exc


def _parse_check(payload: object) -> TargetReplayCheck:
    data = _require_mapping(payload, "check")
    return TargetReplayCheck(
        check_id=_require_text(data, "check_id"),
        status=_parse_status(data.get("status")),
        summary=_require_text(data, "summary"),
        evidence_paths=tuple(
            Path(_require_text_value(item, "evidence_paths"))
            for item in _require_list(data, "evidence_paths")
        ),
        missing_artifact_types=tuple(
            _require_text_value(item, "missing_artifact_types")
            for item in _require_list(data, "missing_artifact_types")
        ),
    )


def _validate_path_segment(value: object, field_name: str) -> str:
    normalized = _non_blank(value, field_name)
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise TargetReplayReportError(f"{field_name} must be a clean path segment.")
    if any(ch.isspace() for ch in normalized):
        raise TargetReplayReportError(f"{field_name} must not include whitespace.")
    return normalized


def _non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TargetReplayReportError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _ensure_path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise TargetReplayReportError(f"{field_name} must be a Path.")
    return value.expanduser().resolve(strict=False)


def _validate_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TargetReplayReportError(f"{field_name} must be a non-negative integer.")
    return value


def _validate_counts(value: Mapping[str, object], field_name: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise TargetReplayReportError(f"{field_name} must be a mapping.")
    return {
        _non_blank(key, field_name): _validate_non_negative_int(count, field_name)
        for key, count in value.items()
    }


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TargetReplayReportError(f"{field_name} must be a JSON object.")
    return value


def _optional_mapping(data: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    value = data.get(field_name)
    if value is None:
        return {}
    return _require_mapping(value, field_name)


def _require_list(data: Mapping[str, object], field_name: str) -> list[object]:
    value = data.get(field_name)
    if not isinstance(value, list):
        raise TargetReplayReportError(f"{field_name} must be a JSON array.")
    return value


def _require_text(data: Mapping[str, object], field_name: str) -> str:
    return _require_text_value(data.get(field_name), field_name)


def _require_text_value(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TargetReplayReportError(f"{field_name} must be a non-blank string.")
    return value


def _optional_text(data: Mapping[str, object], field_name: str) -> str | None:
    value = data.get(field_name)
    if value is None:
        return None
    return _require_text_value(value, field_name)


def _optional_path(data: Mapping[str, object], field_name: str) -> Path | None:
    value = data.get(field_name)
    if value is None:
        return None
    return Path(_require_text_value(value, field_name))


def _require_int(data: Mapping[str, object], field_name: str) -> int:
    return _validate_non_negative_int(data.get(field_name), field_name)


def _optional_int(
    data: Mapping[str, object],
    field_name: str,
    *,
    default: int = 0,
) -> int:
    value = data.get(field_name)
    if value is None:
        return default
    return _validate_non_negative_int(value, field_name)


def _require_bool(data: Mapping[str, object], field_name: str) -> bool:
    value = data.get(field_name)
    if not isinstance(value, bool):
        raise TargetReplayReportError(f"{field_name} must be a bool.")
    return value


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TargetReplayReportError(f"{field_name} must be an ISO datetime string.")
    return datetime.fromisoformat(value)


def _parse_status(value: object) -> TargetReplayStatus:
    if value not in {"passed", "failed"}:
        raise TargetReplayReportError("status must be passed or failed.")
    return cast(TargetReplayStatus, value)


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


__all__ = [
    "FailureRepairSummary",
    "PersistedTargetReplayReport",
    "TargetReplayCheck",
    "TargetReplayReport",
    "TargetReplayReportError",
    "build_target_replay_report",
    "load_target_replay_report",
    "target_replay_report_paths",
    "write_target_replay_report",
]
