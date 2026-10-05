"""Release Readiness manifest over deterministic audit reports."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Literal, cast

from event_trader.audit.ceau_acceptance import (
    CEAUAcceptanceSummary,
    build_unavailable_ceau_acceptance_summary,
    parse_ceau_acceptance_summary,
    render_ceau_acceptance_summary_markdown,
)
from event_trader.audit.live_smoke import (
    LiveSmokeTraceBundleError,
    live_smoke_trace_bundle_paths,
    load_live_smoke_trace_bundle,
)
from event_trader.audit.replay_acceptance import (
    ReplayAcceptanceError,
    load_replay_acceptance_report,
    replay_acceptance_report_paths,
)
from event_trader.audit.target_replay_report import (
    TargetReplayReportError,
    load_target_replay_report,
    target_replay_report_paths,
)
from event_trader.config import KernelConfigAuditIdentity, load_kernel_config_audit_identity
from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout

ReleaseReadinessStatus = Literal["passed", "failed"]
_REPORT_DIR_NAME = "release_readiness"


class ReleaseReadinessReportError(ValueError):
    """Raised when Release Readiness report inputs or payloads are malformed."""


@dataclass(frozen=True, slots=True)
class ReleaseReadinessChildReport:
    report_type: str
    status: ReleaseReadinessStatus
    path: Path
    summary: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_type", _non_blank(self.report_type, "report_type"))
        if self.status not in {"passed", "failed"}:
            raise ReleaseReadinessReportError("child status must be passed or failed.")
        object.__setattr__(self, "path", _ensure_path(self.path, "path"))
        object.__setattr__(self, "summary", _non_blank(self.summary, "summary"))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "report_type": self.report_type,
            "status": self.status,
            "path": str(self.path),
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class ReleaseReadinessReport:
    report_id: str
    status: ReleaseReadinessStatus
    run_id: str
    target_key: str
    commit_hash: str
    config_path: Path
    config_audit: KernelConfigAuditIdentity
    replay_command: str
    git_status_summary: str
    generated_at: datetime
    child_reports: tuple[ReleaseReadinessChildReport, ...]
    workbench_efficiency_summary: dict[str, object]
    ceau_acceptance_summary: CEAUAcceptanceSummary
    json_path: Path | None = None
    markdown_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_id", _non_blank(self.report_id, "report_id"))
        if self.status not in {"passed", "failed"}:
            raise ReleaseReadinessReportError("status must be passed or failed.")
        object.__setattr__(self, "run_id", _validate_path_segment(self.run_id, "run_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ReleaseReadinessReportError),
        )
        object.__setattr__(self, "commit_hash", _non_blank(self.commit_hash, "commit_hash"))
        object.__setattr__(self, "config_path", _ensure_path(self.config_path, "config_path"))
        if not isinstance(self.config_audit, KernelConfigAuditIdentity):
            raise ReleaseReadinessReportError("config_audit must be a KernelConfigAuditIdentity.")
        object.__setattr__(
            self, "replay_command", _non_blank(self.replay_command, "replay_command")
        )
        object.__setattr__(
            self,
            "git_status_summary",
            self.git_status_summary if isinstance(self.git_status_summary, str) else "",
        )
        if not isinstance(self.generated_at, datetime):
            raise ReleaseReadinessReportError("generated_at must be a datetime.")
        for child in self.child_reports:
            if not isinstance(child, ReleaseReadinessChildReport):
                raise ReleaseReadinessReportError(
                    "child_reports must contain ReleaseReadinessChildReport values."
                )
        if not isinstance(self.workbench_efficiency_summary, dict):
            raise ReleaseReadinessReportError(
                "workbench_efficiency_summary must be a dict."
            )
        if not isinstance(self.ceau_acceptance_summary, CEAUAcceptanceSummary):
            raise ReleaseReadinessReportError(
                "ceau_acceptance_summary must be a CEAUAcceptanceSummary."
            )
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
            "git_status_summary": self.git_status_summary,
            "generated_at": self.generated_at.isoformat(),
            "child_reports": [child.to_json_payload() for child in self.child_reports],
            "workbench_efficiency_summary": dict(self.workbench_efficiency_summary),
            "ceau_acceptance_summary": self.ceau_acceptance_summary.to_json_payload(),
            "json_path": None if self.json_path is None else str(self.json_path),
            "markdown_path": None if self.markdown_path is None else str(self.markdown_path),
        }


@dataclass(frozen=True, slots=True)
class PersistedReleaseReadinessReport:
    report: ReleaseReadinessReport
    json_path: Path
    markdown_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.report, ReleaseReadinessReport):
            raise ReleaseReadinessReportError("report must be ReleaseReadinessReport.")
        object.__setattr__(self, "json_path", _ensure_path(self.json_path, "json_path"))
        object.__setattr__(self, "markdown_path", _ensure_path(self.markdown_path, "markdown_path"))

    def to_json_payload(self) -> dict[str, object]:
        return self.report.to_json_payload()


def build_release_readiness_report(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
    view_episode_id: str,
    commit_hash: str,
    config_path: Path,
    replay_command: str,
    git_status_summary: str = "",
    generated_at: datetime | None = None,
) -> ReleaseReadinessReport:
    if not isinstance(layout, WorkspaceLayout):
        raise ReleaseReadinessReportError("layout must be a WorkspaceLayout instance.")
    normalized_run_id = _validate_path_segment(run_id, "run_id")
    normalized_target_key = validate_target_key(target_key, error_type=ReleaseReadinessReportError)
    normalized_view_episode_id = _non_blank(view_episode_id, "view_episode_id")
    normalized_commit_hash = _non_blank(commit_hash, "commit_hash")
    normalized_replay_command = _non_blank(replay_command, "replay_command")
    normalized_config_path = _ensure_path(config_path, "config_path")
    config_audit = load_kernel_config_audit_identity(normalized_config_path)
    report_generated_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    child_reports = (
        _load_child(
            report_type="replay_acceptance",
            path=replay_acceptance_report_paths(
                layout,
                run_id=normalized_run_id,
                target_key=normalized_target_key,
            )[0],
            loader=lambda path: load_replay_acceptance_report(path).report.status,
        ),
        _load_child(
            report_type="target_replay",
            path=target_replay_report_paths(
                layout,
                run_id=normalized_run_id,
                target_key=normalized_target_key,
            )[0],
            loader=lambda path: load_target_replay_report(path).report.status,
        ),
        _load_child(
            report_type="live_smoke",
            path=live_smoke_trace_bundle_paths(
                layout,
                target_key=normalized_target_key,
                view_episode_id=normalized_view_episode_id,
            )[0],
            loader=lambda path: load_live_smoke_trace_bundle(path).report.status,
        ),
    )
    config_check_failed = (
        not (normalized_config_path.exists() and normalized_config_path.is_file())
        or config_audit.load_error is not None
    )
    git_status_check_failed = bool(git_status_summary) and not _git_status_is_clean(
        git_status_summary
    )
    status: ReleaseReadinessStatus = (
        "failed"
        if config_check_failed
        or git_status_check_failed
        or any(child.status == "failed" for child in child_reports)
        else "passed"
    )
    ceau_acceptance_summary = _build_ceau_acceptance_summary(
        layout=layout,
        target_key=normalized_target_key,
        run_id=normalized_run_id,
    )
    return ReleaseReadinessReport(
        report_id=_report_id(
            run_id=normalized_run_id,
            target_key=normalized_target_key,
            commit_hash=normalized_commit_hash,
        ),
        status=status,
        run_id=normalized_run_id,
        target_key=normalized_target_key,
        commit_hash=normalized_commit_hash,
        config_path=normalized_config_path,
        config_audit=config_audit,
        replay_command=normalized_replay_command,
        git_status_summary=git_status_summary,
        generated_at=report_generated_at,
        child_reports=child_reports,
        ceau_acceptance_summary=ceau_acceptance_summary,
        workbench_efficiency_summary=_build_workbench_efficiency_summary(
            layout=layout,
            run_id=normalized_run_id,
            target_key=normalized_target_key,
        ),
    )


def write_release_readiness_report(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
    view_episode_id: str,
    commit_hash: str,
    config_path: Path,
    replay_command: str,
    git_status_summary: str = "",
    generated_at: datetime | None = None,
) -> PersistedReleaseReadinessReport:
    report = build_release_readiness_report(
        layout=layout,
        run_id=run_id,
        target_key=target_key,
        view_episode_id=view_episode_id,
        commit_hash=commit_hash,
        config_path=config_path,
        replay_command=replay_command,
        git_status_summary=git_status_summary,
        generated_at=generated_at,
    )
    json_path, markdown_path = release_readiness_report_paths(
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
        raise ReleaseReadinessReportError(
            f"Failed to write Release Readiness report: {exc}"
        ) from exc
    return PersistedReleaseReadinessReport(
        report=persisted_report,
        json_path=json_path,
        markdown_path=markdown_path,
    )


def load_release_readiness_report(path: Path) -> PersistedReleaseReadinessReport:
    json_path = _ensure_path(path, "path")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseReadinessReportError(
            f"Failed to read Release Readiness report {json_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ReleaseReadinessReportError(
            f"Release Readiness report is invalid JSON: {json_path}"
        ) from exc
    report = _parse_report_payload(payload)
    return PersistedReleaseReadinessReport(
        report=report,
        json_path=json_path,
        markdown_path=report.markdown_path or json_path.with_suffix(".md"),
    )


def release_readiness_report_paths(
    layout: WorkspaceLayout,
    *,
    run_id: str,
    target_key: str,
) -> tuple[Path, Path]:
    if not isinstance(layout, WorkspaceLayout):
        raise ReleaseReadinessReportError("layout must be a WorkspaceLayout instance.")
    normalized_run_id = _validate_path_segment(run_id, "run_id")
    normalized_target_key = validate_target_key(target_key, error_type=ReleaseReadinessReportError)
    root = layout.runtime_root / "audit" / _REPORT_DIR_NAME / normalized_target_key
    return (
        (root / f"{normalized_run_id}.json").resolve(strict=False),
        (root / f"{normalized_run_id}.md").resolve(strict=False),
    )


def _build_workbench_efficiency_summary(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
) -> dict[str, object]:
    records, ignored_count = _load_analysis_outcome_records(
        layout=layout,
        run_id=run_id,
        target_key=target_key,
    )
    successful_records = [
        record
        for record in records
        if record.get("grounding_coverage_status") == "passed"
    ]
    return {
        "successful_analysis_count": len(successful_records),
        "analysis_outcome_count": len(records),
        "ignored_analysis_outcome_count": ignored_count,
        "median_tool_calls": _median_int(
            record.get("tool_call_count") for record in successful_records
        ),
        "median_market_tool_calls": _median_int(
            record.get("market_tool_call_count") for record in successful_records
        ),
        "compiled_evidence_grounding_count": _sum_int(
            record.get("compiled_evidence_grounding_count") for record in records
        ),
        "compiled_memory_grounding_count": _sum_int(
            record.get("compiled_memory_grounding_count") for record in records
        ),
        "compiled_market_grounding_count": _sum_int(
            record.get("compiled_market_grounding_count") for record in records
        ),
        "grounding_failure_count": sum(
            1 for record in records if record.get("grounding_coverage_status") == "failed"
        ),
        "market_lane_required_count": sum(
            1
            for record in records
            if _market_lane_required(record.get("grounding_coverage_receipt"))
        ),
        "market_lane_unavailable_or_stale_count": sum(
            1
            for record in records
            if _market_lane_unavailable_or_stale(record)
        ),
    }


def _load_analysis_outcome_records(
    *,
    layout: WorkspaceLayout,
    run_id: str,
    target_key: str,
) -> tuple[list[dict[str, object]], int]:
    outcome_root = layout.runtime_root / "analysis_outcomes" / target_key
    if not outcome_root.exists():
        return [], 0
    records: list[dict[str, object]] = []
    ignored_count = 0
    for path in sorted(outcome_root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ReleaseReadinessReportError(
                f"Failed to read analysis outcome records {path}: {exc}"
            ) from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReleaseReadinessReportError(
                    f"Analysis outcome record is invalid JSON: {path}"
                ) from exc
            if isinstance(payload, dict):
                if payload.get("context_packet_run_id") == run_id:
                    records.append(payload)
                else:
                    ignored_count += 1
    return records, ignored_count


def _median_int(values: Iterable[object]) -> int | None:
    normalized = [value for value in values if isinstance(value, int)]
    if not normalized:
        return None
    return int(median(normalized))


def _sum_int(values: Iterable[object]) -> int:
    return sum(value for value in values if isinstance(value, int))


def _market_lane_required(receipt: object) -> bool:
    if not isinstance(receipt, dict):
        return False
    return receipt.get("market_grounding") in {"compiled", "tool_read", "missing"}


def _market_lane_unavailable_or_stale(record: dict[str, object]) -> bool:
    receipt = record.get("grounding_coverage_receipt")
    if not isinstance(receipt, dict) or receipt.get("market_grounding") != "compiled":
        return False
    audit = record.get("market_context_audit")
    if isinstance(audit, dict):
        statuses = tuple(_flatten_json_strings(audit))
        if any("stale" in value or "unavailable" in value for value in statuses):
            return True
    return False


def _flatten_json_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, dict):
        flattened: list[str] = []
        for item in value.values():
            flattened.extend(_flatten_json_strings(item))
        return tuple(flattened)
    if isinstance(value, list | tuple):
        flattened = []
        for item in value:
            flattened.extend(_flatten_json_strings(item))
        return tuple(flattened)
    return (str(value).lower(),)


def _load_child(*, report_type: str, path: Path, loader) -> ReleaseReadinessChildReport:
    try:
        status = loader(path)
    except (
        ReplayAcceptanceError,
        TargetReplayReportError,
        LiveSmokeTraceBundleError,
        OSError,
        ValueError,
    ) as exc:
        return ReleaseReadinessChildReport(
            report_type=report_type,
            status="failed",
            path=path,
            summary=f"{report_type} report missing or unreadable: {exc}",
        )
    return ReleaseReadinessChildReport(
        report_type=report_type,
        status=status,
        path=path,
        summary=f"{report_type} report status is {status}.",
    )


def _report_id(*, run_id: str, target_key: str, commit_hash: str) -> str:
    digest = sha256(f"{run_id}|{target_key}|{commit_hash}".encode()).hexdigest()
    return f"release_readiness:{run_id}:{target_key}:{digest}"


def _render_markdown(report: ReleaseReadinessReport) -> str:
    lines = [
        f"# Release Readiness: {report.run_id} / {report.target_key} ({report.status})",
        "",
        f"- Report ID: `{report.report_id}`",
        f"- Generated At: `{report.generated_at.isoformat()}`",
        f"- Pinned Commit: `{report.commit_hash}`",
        f"- Config: `{report.config_path}`",
        f"- Config Hash: `{report.config_audit.config_hash}`",
        f"- Git Status Summary: `{report.git_status_summary}`",
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
            "## Child Reports",
            "",
            "| Report | Status | Path | Summary |",
            "| --- | --- | --- | --- |",
        ]
    )
    for child in report.child_reports:
        lines.append(
            f"| `{child.report_type}` | `{child.status}` | `{child.path}` | {child.summary} |"
        )
    summary = report.workbench_efficiency_summary
    lines.extend(
        [
            "",
            "## Workbench Efficiency Summary",
            "",
            f"- Successful Analysis Count: `{summary.get('successful_analysis_count', 0)}`",
            f"- Ignored Non-Run Analysis Outcomes: "
            f"`{summary.get('ignored_analysis_outcome_count', 0)}`",
            f"- Median Tool Calls: `{summary.get('median_tool_calls')}`",
            f"- Median Market Tool Calls: `{summary.get('median_market_tool_calls')}`",
            f"- Compiled Evidence Grounding Count: "
            f"`{summary.get('compiled_evidence_grounding_count', 0)}`",
            f"- Compiled Memory Grounding Count: "
            f"`{summary.get('compiled_memory_grounding_count', 0)}`",
            f"- Compiled Market Grounding Count: "
            f"`{summary.get('compiled_market_grounding_count', 0)}`",
            f"- Grounding Failure Count: `{summary.get('grounding_failure_count', 0)}`",
            f"- Market Lane Required Count: `{summary.get('market_lane_required_count', 0)}`",
            f"- Market Lane Unavailable/Stale Count: "
            f"`{summary.get('market_lane_unavailable_or_stale_count', 0)}`",
        ]
    )
    lines.extend(["", "## Generated Artifacts", ""])
    if report.json_path is not None:
        lines.append(f"- JSON: `{report.json_path}`")
    if report.markdown_path is not None:
        lines.append(f"- Markdown: `{report.markdown_path}`")
    lines.append("")
    return "\n".join(lines)


def _parse_report_payload(payload: object) -> ReleaseReadinessReport:
    data = _require_mapping(payload, "report")
    return ReleaseReadinessReport(
        report_id=_require_text(data, "report_id"),
        status=_parse_status(data.get("status")),
        run_id=_require_text(data, "run_id"),
        target_key=_require_text(data, "target_key"),
        commit_hash=_require_text(data, "commit_hash"),
        config_path=Path(_require_text(data, "config_path")),
        config_audit=_parse_config_audit(data),
        replay_command=_require_text(data, "replay_command"),
        git_status_summary=_require_text_value(
            data.get("git_status_summary", ""), "git_status_summary"
        ),
        generated_at=_parse_datetime(data.get("generated_at"), "generated_at"),
        child_reports=tuple(_parse_child(item) for item in _require_list(data, "child_reports")),
        ceau_acceptance_summary=parse_ceau_acceptance_summary(
            data.get("ceau_acceptance_summary")
        ),
        workbench_efficiency_summary=dict(
            _optional_mapping(data, "workbench_efficiency_summary")
        ),
        json_path=_optional_path(data, "json_path"),
        markdown_path=_optional_path(data, "markdown_path"),
    )


def _parse_config_audit(data: Mapping[str, object]) -> KernelConfigAuditIdentity:
    payload = data.get("config_audit")
    if payload is None:
        return KernelConfigAuditIdentity(
            config_path=Path(_require_text(data, "config_path")),
            config_hash=None,
            load_error="config_audit missing from legacy Release Readiness report.",
        )
    try:
        return KernelConfigAuditIdentity.from_json_payload(payload)
    except ValueError as exc:
        raise ReleaseReadinessReportError(f"config_audit is invalid: {exc}") from exc


def _parse_child(payload: object) -> ReleaseReadinessChildReport:
    data = _require_mapping(payload, "child_report")
    return ReleaseReadinessChildReport(
        report_type=_require_text(data, "report_type"),
        status=_parse_status(data.get("status")),
        path=Path(_require_text(data, "path")),
        summary=_require_text(data, "summary"),
    )


def _build_ceau_acceptance_summary(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_id: str,
) -> CEAUAcceptanceSummary:
    try:
        report_path = replay_acceptance_report_paths(
            layout,
            run_id=run_id,
            target_key=target_key,
        )[0]
        return load_replay_acceptance_report(report_path).report.ceau_acceptance_summary
    except (ReplayAcceptanceError, OSError, ValueError) as exc:
        return build_unavailable_ceau_acceptance_summary(
            f"failed to load replay acceptance CEAU summary: {exc}"
        )


def _validate_path_segment(value: object, field_name: str) -> str:
    normalized = _non_blank(value, field_name)
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ReleaseReadinessReportError(f"{field_name} must be a clean path segment.")
    if any(ch.isspace() for ch in normalized):
        raise ReleaseReadinessReportError(f"{field_name} must not include whitespace.")
    return normalized


def _git_status_is_clean(summary: str) -> bool:
    return summary.strip().lower() in {
        "clean",
        "clean git status",
        "git status clean",
    }


def _non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseReadinessReportError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _ensure_path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise ReleaseReadinessReportError(f"{field_name} must be a Path.")
    return value.expanduser().resolve(strict=False)


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReleaseReadinessReportError(f"{field_name} must be a JSON object.")
    return value


def _optional_mapping(data: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    value = data.get(field_name)
    if value is None:
        return {}
    return _require_mapping(value, field_name)


def _require_list(data: Mapping[str, object], field_name: str) -> list[object]:
    value = data.get(field_name)
    if not isinstance(value, list):
        raise ReleaseReadinessReportError(f"{field_name} must be a JSON array.")
    return value


def _require_text(data: Mapping[str, object], field_name: str) -> str:
    return _require_text_value(data.get(field_name), field_name)


def _require_text_value(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ReleaseReadinessReportError(f"{field_name} must be a string.")
    return value


def _optional_path(data: Mapping[str, object], field_name: str) -> Path | None:
    value = data.get(field_name)
    if value is None:
        return None
    return Path(_require_text_value(value, field_name))


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseReadinessReportError(f"{field_name} must be an ISO datetime string.")
    return datetime.fromisoformat(value)


def _parse_status(value: object) -> ReleaseReadinessStatus:
    if value not in {"passed", "failed"}:
        raise ReleaseReadinessReportError("status must be passed or failed.")
    return cast(ReleaseReadinessStatus, value)


__all__ = [
    "PersistedReleaseReadinessReport",
    "ReleaseReadinessChildReport",
    "ReleaseReadinessReport",
    "ReleaseReadinessReportError",
    "build_release_readiness_report",
    "load_release_readiness_report",
    "release_readiness_report_paths",
    "write_release_readiness_report",
]
