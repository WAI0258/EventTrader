"""Deterministic replay acceptance report generation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

from event_trader.audit.ceau_acceptance import (
    CEAUAcceptanceSummary,
    CEAUAcceptanceSummaryError,
    build_ceau_acceptance_summary,
    build_unavailable_ceau_acceptance_summary,
    parse_ceau_acceptance_summary,
    render_ceau_acceptance_summary_markdown,
)
from event_trader.audit.trace_bundle import (
    REQUIRED_TRACE_ARTIFACT_TYPES,
    EvidencePack,
    TraceArtifact,
    TraceBundle,
    TraceBundleError,
    build_episode_trace_bundle,
    build_evidence_pack,
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
from event_trader.storage import WorkspaceLayout

ReplayAcceptanceStatus = Literal["passed", "failed"]
_REPORT_DIR_NAME = "replay_acceptance"


class ReplayAcceptanceError(ValueError):
    """Raised when replay acceptance report inputs or payloads are malformed."""


@dataclass(frozen=True, slots=True)
class ReplayAcceptanceCheck:
    check_id: str
    status: ReplayAcceptanceStatus
    summary: str
    evidence_paths: tuple[Path, ...] = ()
    missing_artifact_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_id", _non_blank(self.check_id, "check_id"))
        object.__setattr__(self, "summary", _non_blank(self.summary, "summary"))
        if self.status not in {"passed", "failed"}:
            raise ReplayAcceptanceError("check status must be passed or failed.")
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
class ReplayAcceptanceReport:
    report_id: str
    status: ReplayAcceptanceStatus
    run_id: str
    target_key: str
    commit_hash: str
    config_path: Path
    config_audit: KernelConfigAuditIdentity
    replay_command: str
    generated_at: datetime
    visibility_audit_path: Path
    visibility_audit_status: str | None
    context_packet_count: int
    ceau_acceptance_summary: CEAUAcceptanceSummary
    trace_bundle: TraceBundle
    evidence_pack: EvidencePack
    checks: tuple[ReplayAcceptanceCheck, ...]
    missing_artifact_types: tuple[str, ...]
    json_path: Path | None = None
    markdown_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_id", _non_blank(self.report_id, "report_id"))
        if self.status not in {"passed", "failed"}:
            raise ReplayAcceptanceError("report status must be passed or failed.")
        object.__setattr__(self, "run_id", _validate_path_segment(self.run_id, "run_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ReplayAcceptanceError),
        )
        object.__setattr__(self, "commit_hash", _non_blank(self.commit_hash, "commit_hash"))
        object.__setattr__(self, "config_path", _ensure_path(self.config_path, "config_path"))
        if not isinstance(self.config_audit, KernelConfigAuditIdentity):
            raise ReplayAcceptanceError("config_audit must be a KernelConfigAuditIdentity.")
        object.__setattr__(
            self,
            "replay_command",
            _non_blank(self.replay_command, "replay_command"),
        )
        if not isinstance(self.generated_at, datetime):
            raise ReplayAcceptanceError("generated_at must be a datetime.")
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
        if (
            isinstance(self.context_packet_count, bool)
            or not isinstance(self.context_packet_count, int)
            or self.context_packet_count < 0
        ):
            raise ReplayAcceptanceError("context_packet_count must be non-negative.")
        if not isinstance(self.trace_bundle, TraceBundle):
            raise ReplayAcceptanceError("trace_bundle must be a TraceBundle.")
        if not isinstance(self.evidence_pack, EvidencePack):
            raise ReplayAcceptanceError("evidence_pack must be an EvidencePack.")
        for check in self.checks:
            if not isinstance(check, ReplayAcceptanceCheck):
                raise ReplayAcceptanceError("checks must contain ReplayAcceptanceCheck values.")
        if not isinstance(self.ceau_acceptance_summary, CEAUAcceptanceSummary):
            raise ReplayAcceptanceError(
                "ceau_acceptance_summary must be a CEAUAcceptanceSummary."
            )
        object.__setattr__(
            self,
            "missing_artifact_types",
            tuple(
                _non_blank(item, "missing_artifact_types") for item in self.missing_artifact_types
            ),
        )
        if self.json_path is not None:
            object.__setattr__(self, "json_path", _ensure_path(self.json_path, "json_path"))
        if self.markdown_path is not None:
            object.__setattr__(
                self,
                "markdown_path",
                _ensure_path(self.markdown_path, "markdown_path"),
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
            "context_packet_count": self.context_packet_count,
            "ceau_acceptance_summary": self.ceau_acceptance_summary.to_json_payload(),
            "trace_bundle": self.trace_bundle.to_json_payload(),
            "evidence_pack": self.evidence_pack.to_json_payload(),
            "checks": [check.to_json_payload() for check in self.checks],
            "missing_artifact_types": list(self.missing_artifact_types),
            "json_path": None if self.json_path is None else str(self.json_path),
            "markdown_path": None if self.markdown_path is None else str(self.markdown_path),
        }


@dataclass(frozen=True, slots=True)
class PersistedReplayAcceptanceReport:
    report: ReplayAcceptanceReport
    json_path: Path
    markdown_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.report, ReplayAcceptanceReport):
            raise ReplayAcceptanceError("report must be a ReplayAcceptanceReport.")
        object.__setattr__(self, "json_path", _ensure_path(self.json_path, "json_path"))
        object.__setattr__(
            self,
            "markdown_path",
            _ensure_path(self.markdown_path, "markdown_path"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return self.report.to_json_payload()


def build_replay_acceptance_report(
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
) -> ReplayAcceptanceReport:
    if not isinstance(layout, WorkspaceLayout):
        raise ReplayAcceptanceError("layout must be a WorkspaceLayout instance.")
    normalized_run_id = _validate_path_segment(run_id, "run_id")
    normalized_target_key = validate_target_key(
        target_key,
        error_type=ReplayAcceptanceError,
    )
    normalized_event_id = _non_blank(event_id, "event_id")
    normalized_view_episode_id = _non_blank(view_episode_id, "view_episode_id")
    normalized_decision_episode_id = _non_blank(
        decision_episode_id,
        "decision_episode_id",
    )
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
    context_packet_count, context_check = _build_context_packet_check(
        layout=layout,
        run_id=normalized_run_id,
        target_key=normalized_target_key,
    )
    trace_bundle, trace_check = _build_trace_bundle_check(
        layout=layout,
        target_key=normalized_target_key,
        event_id=normalized_event_id,
        view_episode_id=normalized_view_episode_id,
        decision_episode_id=normalized_decision_episode_id,
        created_at=report_generated_at,
    )
    missing_artifact_types = _missing_required_artifact_types(trace_bundle)
    missing_artifact_check = ReplayAcceptanceCheck(
        check_id="required_trace_artifacts",
        status="passed" if not missing_artifact_types else "failed",
        summary=(
            "All required trace artifact types are present."
            if not missing_artifact_types
            else "Required trace artifact types are missing."
        ),
        evidence_paths=tuple(
            artifact.path for artifact in trace_bundle.artifacts if artifact.status == "available"
        ),
        missing_artifact_types=missing_artifact_types,
    )
    evidence_pack, evidence_check = _build_evidence_pack_check(
        trace_bundle=trace_bundle,
        as_of_at=report_generated_at,
    )
    checks = (
        _build_metadata_check(
            config_path=normalized_config_path,
            config_audit=config_audit,
        ),
        visibility_check,
        context_check,
        trace_check,
        missing_artifact_check,
        evidence_check,
    )
    ceau_acceptance_summary = _build_ceau_acceptance_summary(
        layout=layout,
        target_key=normalized_target_key,
        run_id=normalized_run_id,
    )
    status: ReplayAcceptanceStatus = (
        "failed" if any(check.status == "failed" for check in checks) else "passed"
    )
    report_id = _report_id(
        run_id=normalized_run_id,
        target_key=normalized_target_key,
        commit_hash=normalized_commit_hash,
        trace_bundle_id=trace_bundle.bundle_id,
    )
    return ReplayAcceptanceReport(
        report_id=report_id,
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
        context_packet_count=context_packet_count,
        ceau_acceptance_summary=ceau_acceptance_summary,
        trace_bundle=trace_bundle,
        evidence_pack=evidence_pack,
        checks=checks,
        missing_artifact_types=missing_artifact_types,
    )


def write_replay_acceptance_report(
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
) -> PersistedReplayAcceptanceReport:
    report = build_replay_acceptance_report(
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
    json_path, markdown_path = replay_acceptance_report_paths(
        layout,
        run_id=report.run_id,
        target_key=report.target_key,
    )
    persisted_report = replace(
        report,
        json_path=json_path,
        markdown_path=markdown_path,
    )
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
        markdown_path.write_text(
            _render_markdown(persisted_report),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ReplayAcceptanceError(f"Failed to write replay acceptance report: {exc}") from exc
    return PersistedReplayAcceptanceReport(
        report=persisted_report,
        json_path=json_path,
        markdown_path=markdown_path,
    )


def load_replay_acceptance_report(path: Path) -> PersistedReplayAcceptanceReport:
    json_path = _ensure_path(path, "path")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReplayAcceptanceError(
            f"Failed to read replay acceptance report {json_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ReplayAcceptanceError(
            f"Replay acceptance report is invalid JSON: {json_path}"
        ) from exc
    report = _parse_report_payload(payload)
    markdown_path = report.markdown_path or json_path.with_suffix(".md")
    return PersistedReplayAcceptanceReport(
        report=report,
        json_path=json_path,
        markdown_path=markdown_path,
    )


def replay_acceptance_report_paths(
    layout: WorkspaceLayout,
    *,
    run_id: str,
    target_key: str,
) -> tuple[Path, Path]:
    if not isinstance(layout, WorkspaceLayout):
        raise ReplayAcceptanceError("layout must be a WorkspaceLayout instance.")
    normalized_run_id = _validate_path_segment(run_id, "run_id")
    normalized_target_key = validate_target_key(
        target_key,
        error_type=ReplayAcceptanceError,
    )
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
) -> tuple[str | None, ReplayAcceptanceCheck]:
    try:
        payload = load_replay_visibility_audit(layout=layout, run_id=run_id)
    except ReplayVisibilityAuditError as exc:
        return None, ReplayAcceptanceCheck(
            check_id="replay_visibility_audit",
            status="failed",
            summary=f"Replay visibility audit is missing or unreadable: {exc}",
            evidence_paths=(visibility_path,),
        )
    status = payload.get("status")
    normalized_status = status if isinstance(status, str) else None
    return normalized_status, ReplayAcceptanceCheck(
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
) -> ReplayAcceptanceCheck:
    config_exists = config_path.exists() and config_path.is_file()
    config_valid = config_exists and config_audit.load_error is None
    return ReplayAcceptanceCheck(
        check_id="acceptance_metadata",
        status="passed" if config_valid else "failed",
        summary=(
            "Acceptance metadata is complete."
            if config_valid
            else (
                "Acceptance config path is missing or not a file."
                if not config_exists
                else f"Acceptance config audit identity failed: {config_audit.load_error}"
            )
        ),
        evidence_paths=(config_path,),
    )


def _build_context_packet_check(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
) -> tuple[int, ReplayAcceptanceCheck]:
    try:
        packets = FileBackedContextPacketStore(
            layout,
            runtime_scope="replay",
            run_id=run_id,
        ).read_all_packets()
    except Exception as exc:
        return 0, ReplayAcceptanceCheck(
            check_id="replay_context_packets",
            status="failed",
            summary=f"Replay context packets could not be read: {exc}",
        )
    count = sum(1 for record in packets if record.packet.target_key == target_key)
    return count, ReplayAcceptanceCheck(
        check_id="replay_context_packets",
        status="passed" if count > 0 else "failed",
        summary=(
            f"Found {count} replay context packet(s) for target {target_key}."
            if count > 0
            else f"No replay context packets were found for target {target_key}."
        ),
    )


def _build_trace_bundle_check(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
    view_episode_id: str,
    decision_episode_id: str,
    created_at: datetime,
) -> tuple[TraceBundle, ReplayAcceptanceCheck]:
    try:
        bundle = build_episode_trace_bundle(
            layout=layout,
            target_key=target_key,
            event_ids=(event_id,),
            view_episode_id=view_episode_id,
            decision_episode_ids=(decision_episode_id,),
            created_at=created_at,
        )
    except Exception as exc:
        bundle = build_trace_bundle(
            target_key=target_key,
            episode_id=view_episode_id,
            artifact_refs=(),
            created_at=created_at,
            event_ids=(event_id,),
            decision_episode_ids=(decision_episode_id,),
            view_episode_id=view_episode_id,
        )
        return bundle, ReplayAcceptanceCheck(
            check_id="trace_bundle",
            status="failed",
            summary=f"Trace bundle could not be generated: {exc}",
        )
    return bundle, ReplayAcceptanceCheck(
        check_id="trace_bundle",
        status="passed",
        summary="Trace bundle was generated from runtime artifacts.",
    )


def _build_evidence_pack_check(
    *,
    trace_bundle: TraceBundle,
    as_of_at: datetime,
) -> tuple[EvidencePack, ReplayAcceptanceCheck]:
    try:
        pack = build_evidence_pack(bundle=trace_bundle, as_of_at=as_of_at)
    except TraceBundleError as exc:
        pack = EvidencePack(
            pack_id=f"evidence-pack:{trace_bundle.bundle_id}:failed",
            target_key=trace_bundle.target_key,
            episode_id=trace_bundle.episode_id,
            artifacts=(),
            as_of_at=as_of_at,
        )
        return pack, ReplayAcceptanceCheck(
            check_id="evidence_pack_visibility",
            status="failed",
            summary=f"Evidence pack could not be generated without future context: {exc}",
        )
    future_artifacts = tuple(
        artifact
        for artifact in pack.artifacts
        if artifact.visible_at is not None and artifact.visible_at > as_of_at
    )
    return pack, ReplayAcceptanceCheck(
        check_id="evidence_pack_visibility",
        status="passed" if not future_artifacts else "failed",
        summary=(
            "Evidence pack contains no future-visible artifacts."
            if not future_artifacts
            else "Evidence pack contains future-visible artifacts."
        ),
        evidence_paths=tuple(artifact.path for artifact in pack.artifacts),
        missing_artifact_types=tuple(artifact.artifact_type for artifact in future_artifacts),
    )


def _missing_required_artifact_types(bundle: TraceBundle) -> tuple[str, ...]:
    missing = {artifact.artifact_type for artifact in bundle.missing_artifacts}
    return tuple(
        artifact_type for artifact_type in REQUIRED_TRACE_ARTIFACT_TYPES if artifact_type in missing
    )


def _report_id(
    *,
    run_id: str,
    target_key: str,
    commit_hash: str,
    trace_bundle_id: str,
) -> str:
    digest = sha256(f"{run_id}|{target_key}|{commit_hash}|{trace_bundle_id}".encode()).hexdigest()
    return f"replay-acceptance:{run_id}:{target_key}:{digest}"


def _render_markdown(report: ReplayAcceptanceReport) -> str:
    lines = [
        f"# Replay Acceptance: {report.run_id} / {report.target_key} ({report.status})",
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
        "## Stream Routing Yield",
        "",
    ]
    lines.extend(render_ceau_acceptance_summary_markdown(report.ceau_acceptance_summary).splitlines())
    lines.extend(
        [
            "",
            "## Checks",
            "",
            "| Check | Status | Summary |",
            "| --- | --- | --- |",
        ]
    )
    for check in report.checks:
        lines.append(
            f"| `{_escape_table(check.check_id)}` | `{check.status}` | "
            f"{_escape_table(check.summary)} |"
        )
    lines.extend(
        [
            "",
            "## Trace",
            "",
            f"- Trace Bundle ID: `{report.trace_bundle.bundle_id}`",
            f"- Evidence Pack ID: `{report.evidence_pack.pack_id}`",
            f"- Context Packets: `{report.context_packet_count}`",
            f"- Visibility Audit: `{report.visibility_audit_path}`",
            f"- Visibility Status: `{report.visibility_audit_status}`",
            "",
            "## Missing Artifacts",
            "",
        ]
    )
    if report.missing_artifact_types:
        lines.extend(f"- `{artifact_type}`" for artifact_type in report.missing_artifact_types)
    else:
        lines.append("- None")
    lines.extend(["", "## Generated Artifacts", ""])
    if report.json_path is not None:
        lines.append(f"- JSON: `{report.json_path}`")
    if report.markdown_path is not None:
        lines.append(f"- Markdown: `{report.markdown_path}`")
    lines.append("")
    return "\n".join(lines)


def _parse_report_payload(payload: object) -> ReplayAcceptanceReport:
    data = _require_mapping(payload, "report")
    return ReplayAcceptanceReport(
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
        context_packet_count=_require_int(data, "context_packet_count"),
        ceau_acceptance_summary=parse_ceau_acceptance_summary(
            data.get("ceau_acceptance_summary"),
        ),
        trace_bundle=_parse_trace_bundle(data.get("trace_bundle")),
        evidence_pack=_parse_evidence_pack(data.get("evidence_pack")),
        checks=tuple(_parse_check(item) for item in _require_list(data, "checks")),
        missing_artifact_types=tuple(
            _require_text_value(item, "missing_artifact_types")
            for item in _require_list(data, "missing_artifact_types")
        ),
        json_path=_optional_path(data, "json_path"),
        markdown_path=_optional_path(data, "markdown_path"),
    )


def _parse_check(payload: object) -> ReplayAcceptanceCheck:
    data = _require_mapping(payload, "check")
    return ReplayAcceptanceCheck(
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


def _parse_config_audit(data: Mapping[str, object]) -> KernelConfigAuditIdentity:
    payload = data.get("config_audit")
    if payload is None:
        return KernelConfigAuditIdentity(
            config_path=Path(_require_text(data, "config_path")),
            config_hash=None,
            load_error="config_audit missing from legacy replay acceptance report.",
        )
    try:
        return KernelConfigAuditIdentity.from_json_payload(payload)
    except ValueError as exc:
        raise ReplayAcceptanceError(f"config_audit is invalid: {exc}") from exc


def _parse_trace_bundle(payload: object) -> TraceBundle:
    data = _require_mapping(payload, "trace_bundle")
    return TraceBundle(
        bundle_id=_require_text(data, "bundle_id"),
        target_key=_require_text(data, "target_key"),
        episode_id=_require_text(data, "episode_id"),
        event_ids=tuple(
            _require_text_value(item, "event_ids") for item in _require_list(data, "event_ids")
        ),
        decision_episode_ids=tuple(
            _require_text_value(item, "decision_episode_ids")
            for item in _require_list(data, "decision_episode_ids")
        ),
        view_episode_id=_require_text(data, "view_episode_id"),
        artifacts=tuple(_parse_trace_artifact(item) for item in _require_list(data, "artifacts")),
        missing_artifacts=tuple(
            _parse_trace_artifact(item) for item in _require_list(data, "missing_artifacts")
        ),
        created_at=_parse_datetime(data.get("created_at"), "created_at"),
    )


def _parse_evidence_pack(payload: object) -> EvidencePack:
    data = _require_mapping(payload, "evidence_pack")
    return EvidencePack(
        pack_id=_require_text(data, "pack_id"),
        target_key=_require_text(data, "target_key"),
        episode_id=_require_text(data, "episode_id"),
        artifacts=tuple(_parse_trace_artifact(item) for item in _require_list(data, "artifacts")),
        as_of_at=_parse_datetime(data.get("as_of_at"), "as_of_at"),
    )


def _parse_trace_artifact(payload: object) -> TraceArtifact:
    data = _require_mapping(payload, "trace_artifact")
    return TraceArtifact(
        artifact_type=_require_text(data, "artifact_type"),
        artifact_id=_require_text(data, "artifact_id"),
        path=Path(_require_text(data, "path")),
        line=_optional_positive_int(data, "line"),
        sha256=_optional_text(data, "sha256"),
        status=_parse_trace_artifact_status(data.get("status")),
        visible_at=_optional_datetime(data, "visible_at"),
    )


def _build_ceau_acceptance_summary(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_id: str,
) -> CEAUAcceptanceSummary:
    try:
        return build_ceau_acceptance_summary(
            layout=layout,
            target_key=target_key,
            run_id=run_id,
        )
    except CEAUAcceptanceSummaryError as exc:
        return build_unavailable_ceau_acceptance_summary(
            f"failed to build CEAU acceptance summary: {exc}"
        )


def _parse_status(value: object) -> ReplayAcceptanceStatus:
    if value not in {"passed", "failed"}:
        raise ReplayAcceptanceError("status must be passed or failed.")
    return cast(ReplayAcceptanceStatus, value)


def _parse_trace_artifact_status(value: object) -> Literal["available", "missing"]:
    if value not in {"available", "missing"}:
        raise ReplayAcceptanceError("trace artifact status must be available or missing.")
    return cast(Literal["available", "missing"], value)


def _validate_path_segment(value: object, field_name: str) -> str:
    normalized = _non_blank(value, field_name)
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ReplayAcceptanceError(f"{field_name} must be a clean path segment.")
    if any(ch.isspace() for ch in normalized):
        raise ReplayAcceptanceError(f"{field_name} must not include whitespace.")
    return normalized


def _non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayAcceptanceError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _ensure_path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise ReplayAcceptanceError(f"{field_name} must be a Path.")
    return value.expanduser().resolve(strict=False)


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReplayAcceptanceError(f"{field_name} must be a JSON object.")
    return value


def _require_list(data: Mapping[str, object], field_name: str) -> list[object]:
    value = data.get(field_name)
    if not isinstance(value, list):
        raise ReplayAcceptanceError(f"{field_name} must be a JSON array.")
    return value


def _require_text(data: Mapping[str, object], field_name: str) -> str:
    return _require_text_value(data.get(field_name), field_name)


def _require_text_value(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayAcceptanceError(f"{field_name} must be a non-blank string.")
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
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReplayAcceptanceError(f"{field_name} must be a non-negative integer.")
    return value


def _optional_positive_int(data: Mapping[str, object], field_name: str) -> int | None:
    value = data.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReplayAcceptanceError(f"{field_name} must be a positive integer.")
    return value


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReplayAcceptanceError(f"{field_name} must be an ISO datetime string.")
    return datetime.fromisoformat(value)


def _optional_datetime(data: Mapping[str, object], field_name: str) -> datetime | None:
    value = data.get(field_name)
    if value is None:
        return None
    return _parse_datetime(value, field_name)


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


__all__ = [
    "PersistedReplayAcceptanceReport",
    "ReplayAcceptanceCheck",
    "ReplayAcceptanceError",
    "ReplayAcceptanceReport",
    "build_replay_acceptance_report",
    "load_replay_acceptance_report",
    "replay_acceptance_report_paths",
    "write_replay_acceptance_report",
]
