"""Live smoke trace bundle audit surface."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

from event_trader.audit.trace_bundle import (
    REQUIRED_TRACE_ARTIFACT_TYPES,
    TraceBundle,
    build_episode_trace_bundle,
    build_trace_bundle,
)
from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout

LiveSmokeStatus = Literal["passed", "failed"]
_REPORT_DIR_NAME = "live_smoke"


class LiveSmokeTraceBundleError(ValueError):
    """Raised when live smoke trace bundle inputs or payloads are malformed."""


@dataclass(frozen=True, slots=True)
class LiveSmokeTraceBundleReport:
    report_id: str
    status: LiveSmokeStatus
    target_key: str
    event_id: str
    view_episode_id: str
    decision_episode_id: str
    generated_at: datetime
    trace_bundle: TraceBundle
    missing_artifact_types: tuple[str, ...]
    json_path: Path | None = None
    markdown_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_id", _non_blank(self.report_id, "report_id"))
        if self.status not in {"passed", "failed"}:
            raise LiveSmokeTraceBundleError("status must be passed or failed.")
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=LiveSmokeTraceBundleError),
        )
        object.__setattr__(self, "event_id", _non_blank(self.event_id, "event_id"))
        object.__setattr__(
            self, "view_episode_id", _non_blank(self.view_episode_id, "view_episode_id")
        )
        object.__setattr__(
            self,
            "decision_episode_id",
            _non_blank(self.decision_episode_id, "decision_episode_id"),
        )
        if not isinstance(self.generated_at, datetime):
            raise LiveSmokeTraceBundleError("generated_at must be a datetime.")
        if not isinstance(self.trace_bundle, TraceBundle):
            raise LiveSmokeTraceBundleError("trace_bundle must be a TraceBundle.")
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
                self, "markdown_path", _ensure_path(self.markdown_path, "markdown_path")
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "status": self.status,
            "target_key": self.target_key,
            "event_id": self.event_id,
            "view_episode_id": self.view_episode_id,
            "decision_episode_id": self.decision_episode_id,
            "generated_at": self.generated_at.isoformat(),
            "trace_bundle": self.trace_bundle.to_json_payload(),
            "missing_artifact_types": list(self.missing_artifact_types),
            "json_path": None if self.json_path is None else str(self.json_path),
            "markdown_path": None if self.markdown_path is None else str(self.markdown_path),
        }


@dataclass(frozen=True, slots=True)
class PersistedLiveSmokeTraceBundle:
    report: LiveSmokeTraceBundleReport
    json_path: Path
    markdown_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.report, LiveSmokeTraceBundleReport):
            raise LiveSmokeTraceBundleError("report must be LiveSmokeTraceBundleReport.")
        object.__setattr__(self, "json_path", _ensure_path(self.json_path, "json_path"))
        object.__setattr__(self, "markdown_path", _ensure_path(self.markdown_path, "markdown_path"))

    def to_json_payload(self) -> dict[str, object]:
        return self.report.to_json_payload()


def build_live_smoke_trace_bundle(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
    view_episode_id: str,
    decision_episode_id: str,
    generated_at: datetime | None = None,
) -> LiveSmokeTraceBundleReport:
    if not isinstance(layout, WorkspaceLayout):
        raise LiveSmokeTraceBundleError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = validate_target_key(target_key, error_type=LiveSmokeTraceBundleError)
    normalized_event_id = _non_blank(event_id, "event_id")
    normalized_view_episode_id = _non_blank(view_episode_id, "view_episode_id")
    normalized_decision_episode_id = _non_blank(decision_episode_id, "decision_episode_id")
    report_generated_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    try:
        bundle = build_episode_trace_bundle(
            layout=layout,
            target_key=normalized_target_key,
            event_ids=(normalized_event_id,),
            view_episode_id=normalized_view_episode_id,
            decision_episode_ids=(normalized_decision_episode_id,),
            created_at=report_generated_at,
        )
    except Exception:
        bundle = build_trace_bundle(
            target_key=normalized_target_key,
            episode_id=normalized_view_episode_id,
            artifact_refs=(),
            created_at=report_generated_at,
            event_ids=(normalized_event_id,),
            decision_episode_ids=(normalized_decision_episode_id,),
            view_episode_id=normalized_view_episode_id,
        )
    missing_artifact_types = _missing_required_artifact_types(bundle)
    status: LiveSmokeStatus = "passed" if not missing_artifact_types else "failed"
    report_id = _report_id(
        target_key=normalized_target_key,
        view_episode_id=normalized_view_episode_id,
        trace_bundle_id=bundle.bundle_id,
    )
    return LiveSmokeTraceBundleReport(
        report_id=report_id,
        status=status,
        target_key=normalized_target_key,
        event_id=normalized_event_id,
        view_episode_id=normalized_view_episode_id,
        decision_episode_id=normalized_decision_episode_id,
        generated_at=report_generated_at,
        trace_bundle=bundle,
        missing_artifact_types=missing_artifact_types,
    )


def write_live_smoke_trace_bundle(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
    view_episode_id: str,
    decision_episode_id: str,
    generated_at: datetime | None = None,
) -> PersistedLiveSmokeTraceBundle:
    report = build_live_smoke_trace_bundle(
        layout=layout,
        target_key=target_key,
        event_id=event_id,
        view_episode_id=view_episode_id,
        decision_episode_id=decision_episode_id,
        generated_at=generated_at,
    )
    json_path, markdown_path = live_smoke_trace_bundle_paths(
        layout,
        target_key=report.target_key,
        view_episode_id=report.view_episode_id,
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
        raise LiveSmokeTraceBundleError(f"Failed to write live smoke trace bundle: {exc}") from exc
    return PersistedLiveSmokeTraceBundle(
        report=persisted_report,
        json_path=json_path,
        markdown_path=markdown_path,
    )


def load_live_smoke_trace_bundle(path: Path) -> PersistedLiveSmokeTraceBundle:
    json_path = _ensure_path(path, "path")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LiveSmokeTraceBundleError(
            f"Failed to read live smoke trace bundle {json_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise LiveSmokeTraceBundleError(
            f"Live smoke trace bundle is invalid JSON: {json_path}"
        ) from exc
    report = _parse_report_payload(payload)
    return PersistedLiveSmokeTraceBundle(
        report=report,
        json_path=json_path,
        markdown_path=report.markdown_path or json_path.with_suffix(".md"),
    )


def live_smoke_trace_bundle_paths(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    view_episode_id: str,
) -> tuple[Path, Path]:
    if not isinstance(layout, WorkspaceLayout):
        raise LiveSmokeTraceBundleError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = validate_target_key(target_key, error_type=LiveSmokeTraceBundleError)
    normalized_view_episode_id = _clean_file_stem(view_episode_id, "view_episode_id")
    root = layout.runtime_root / "audit" / _REPORT_DIR_NAME / normalized_target_key
    return (
        (root / f"{normalized_view_episode_id}.json").resolve(strict=False),
        (root / f"{normalized_view_episode_id}.md").resolve(strict=False),
    )


def _missing_required_artifact_types(bundle: TraceBundle) -> tuple[str, ...]:
    missing = {artifact.artifact_type for artifact in bundle.missing_artifacts}
    return tuple(
        artifact_type for artifact_type in REQUIRED_TRACE_ARTIFACT_TYPES if artifact_type in missing
    )


def _report_id(*, target_key: str, view_episode_id: str, trace_bundle_id: str) -> str:
    digest = sha256(f"{target_key}|{view_episode_id}|{trace_bundle_id}".encode()).hexdigest()
    return f"live-smoke:{target_key}:{digest}"


def _render_markdown(report: LiveSmokeTraceBundleReport) -> str:
    lines = [
        (
            "# Live Smoke Trace Bundle: "
            f"{report.target_key} / {report.view_episode_id} ({report.status})"
        ),
        "",
        f"- Report ID: `{report.report_id}`",
        f"- Generated At: `{report.generated_at.isoformat()}`",
        f"- Trace Bundle ID: `{report.trace_bundle.bundle_id}`",
        "",
        "## Missing Artifacts",
        "",
    ]
    lines.extend(
        [f"- `{item}`" for item in report.missing_artifact_types]
        if report.missing_artifact_types
        else ["- None"]
    )
    lines.extend(["", "## Generated Artifacts", ""])
    if report.json_path is not None:
        lines.append(f"- JSON: `{report.json_path}`")
    if report.markdown_path is not None:
        lines.append(f"- Markdown: `{report.markdown_path}`")
    lines.append("")
    return "\n".join(lines)


def _parse_report_payload(payload: object) -> LiveSmokeTraceBundleReport:
    data = _require_mapping(payload, "report")
    return LiveSmokeTraceBundleReport(
        report_id=_require_text(data, "report_id"),
        status=_parse_status(data.get("status")),
        target_key=_require_text(data, "target_key"),
        event_id=_require_text(data, "event_id"),
        view_episode_id=_require_text(data, "view_episode_id"),
        decision_episode_id=_require_text(data, "decision_episode_id"),
        generated_at=_parse_datetime(data.get("generated_at"), "generated_at"),
        trace_bundle=_parse_trace_bundle(data.get("trace_bundle")),
        missing_artifact_types=tuple(
            _require_text_value(item, "missing_artifact_types")
            for item in _require_list(data, "missing_artifact_types")
        ),
        json_path=_optional_path(data, "json_path"),
        markdown_path=_optional_path(data, "markdown_path"),
    )


def _parse_trace_bundle(payload: object) -> TraceBundle:
    from event_trader.audit.replay_acceptance import _parse_trace_bundle as parse_trace_bundle

    return parse_trace_bundle(payload)


def _clean_file_stem(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise LiveSmokeTraceBundleError(f"{field_name} must be a non-blank string.")
    if any(ch.isspace() for ch in value):
        raise LiveSmokeTraceBundleError(f"{field_name} must not include whitespace.")
    if "/" in value or "\\" in value:
        raise LiveSmokeTraceBundleError(f"{field_name} must be a single file stem.")
    if value in {".", ".."}:
        raise LiveSmokeTraceBundleError(f"{field_name} must be a clean file stem.")
    normalized = value.replace(":", "_")
    if not normalized or "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise LiveSmokeTraceBundleError(f"{field_name} must be a clean file stem.")
    return normalized


def _non_blank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiveSmokeTraceBundleError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _ensure_path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise LiveSmokeTraceBundleError(f"{field_name} must be a Path.")
    return value.expanduser().resolve(strict=False)


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LiveSmokeTraceBundleError(f"{field_name} must be a JSON object.")
    return value


def _require_list(data: Mapping[str, object], field_name: str) -> list[object]:
    value = data.get(field_name)
    if not isinstance(value, list):
        raise LiveSmokeTraceBundleError(f"{field_name} must be a JSON array.")
    return value


def _require_text(data: Mapping[str, object], field_name: str) -> str:
    return _require_text_value(data.get(field_name), field_name)


def _require_text_value(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiveSmokeTraceBundleError(f"{field_name} must be a non-blank string.")
    return value


def _optional_path(data: Mapping[str, object], field_name: str) -> Path | None:
    value = data.get(field_name)
    if value is None:
        return None
    return Path(_require_text_value(value, field_name))


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LiveSmokeTraceBundleError(f"{field_name} must be an ISO datetime string.")
    return datetime.fromisoformat(value)


def _parse_status(value: object) -> LiveSmokeStatus:
    if value not in {"passed", "failed"}:
        raise LiveSmokeTraceBundleError("status must be passed or failed.")
    return cast(LiveSmokeStatus, value)


__all__ = [
    "LiveSmokeTraceBundleError",
    "LiveSmokeTraceBundleReport",
    "PersistedLiveSmokeTraceBundle",
    "build_live_smoke_trace_bundle",
    "live_smoke_trace_bundle_paths",
    "load_live_smoke_trace_bundle",
    "write_live_smoke_trace_bundle",
]
