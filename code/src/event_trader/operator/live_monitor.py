"""Lean live-monitor surface over current-state operator artifacts."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from event_trader.contracts import PageReadResult, resolve_page_path
from event_trader.contracts._validators import validate_target_key
from event_trader.projection.report_artifacts import current_state_artifact_path
from event_trader.storage import WorkspaceLayout

from .read_models import (
    FailureFlag,
    LiveMonitorSnapshot,
    LiveMonitorSourcePages,
    OperatorReadError,
    ProjectionArtifactSnapshot,
)

_PAGE_PATH_RE = re.compile(r"((?:shared|targets)/[^`\s)]+\.md)")
_SECOND_LEVEL_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)
_GENERATED_AT_RE = re.compile(r"^- Generated At: `([^`]+)`$", re.MULTILINE)
_FAILURE_REASON_RE = re.compile(r"^- Failure Reason: (.+)$", re.MULTILINE)


def load_live_monitor(*, target_key: str, layout: WorkspaceLayout) -> LiveMonitorSnapshot:
    """Load one target's live-monitor snapshot from file-backed current-state artifacts."""
    if not isinstance(layout, WorkspaceLayout):
        raise OperatorReadError("layout must be a WorkspaceLayout instance.")

    validated_target_key = validate_target_key(
        target_key,
        error_type=OperatorReadError,
    )
    index_page = _read_page(layout, f"targets/{validated_target_key}/index.md")
    log_page = _read_page(layout, f"targets/{validated_target_key}/log.md")
    source_pages = LiveMonitorSourcePages(
        target_key=validated_target_key,
        index=index_page,
        log=log_page,
    )
    projection = _read_projection_artifact(layout, target_key=validated_target_key)
    active_failure_flags: list[FailureFlag] = []
    if projection.failure_reason is not None:
        active_failure_flags.append(
            FailureFlag(
                subsystem="projection",
                reason=projection.failure_reason,
                artifact_path=projection.artifact_path,
            )
        )

    return LiveMonitorSnapshot(
        target_key=validated_target_key,
        source_pages=source_pages,
        current_page_set=_collect_current_page_set(source_pages),
        latest_log_anchor=_latest_log_anchor(log_page.content_md),
        projection=projection,
        active_failure_flags=tuple(active_failure_flags),
    )


def render_live_monitor(snapshot: LiveMonitorSnapshot) -> str:
    """Render a compact markdown live-monitor view for one target."""
    if not isinstance(snapshot, LiveMonitorSnapshot):
        raise OperatorReadError("snapshot must be a LiveMonitorSnapshot instance.")

    projection_generated_at = (
        snapshot.projection.generated_at.isoformat()
        if snapshot.projection.generated_at is not None
        else "None"
    )
    latest_log_anchor = snapshot.latest_log_anchor or "None"
    lines = [
        "# Live Monitor",
        "",
        "## Research Supervision Summary",
        "",
        f"- Target Key: `{snapshot.target_key}`",
        f"- Latest Log Anchor: `{latest_log_anchor}`",
        f"- Current Reading Path Count: {len(snapshot.current_page_set)}",
        "",
        "## Current Reading Path",
        "",
    ]

    lines.extend(f"- `{page_path}`" for page_path in snapshot.current_page_set)
    lines.extend(["", "## Active Failure Flags", ""])

    if not snapshot.active_failure_flags:
        lines.extend(["- None", ""])
    else:
        lines.extend(
            f"- `{flag.subsystem}`: {flag.reason}"
            for flag in snapshot.active_failure_flags
        )
        lines.append("")

    lines.extend(
        [
            "## Artifact Metadata",
            "",
            f"- Index Page: `{snapshot.source_pages.index.page_path}`",
            f"- Log Page: `{snapshot.source_pages.log.page_path}`",
            f"- Projection Artifact: `{snapshot.projection.artifact_path.as_posix()}`",
            f"- Projection Artifact Present: {snapshot.projection.exists}",
            f"- Projection Generated At: `{projection_generated_at}`",
            "",
        ]
    )

    return "\n".join(lines).rstrip() + "\n"


def _read_page(layout: WorkspaceLayout, page_path: str) -> PageReadResult:
    absolute_page_path = resolve_page_path(layout, page_path)
    try:
        content_md = absolute_page_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperatorReadError(
            f"Failed to read live-monitor source page {page_path!r}: {exc}"
        ) from exc

    return PageReadResult(page_path=page_path, content_md=content_md)


def _read_projection_artifact(
    layout: WorkspaceLayout,
    *,
    target_key: str,
) -> ProjectionArtifactSnapshot:
    relative_path = Path("runtime") / current_state_artifact_path(target_key)
    absolute_path = (layout.root / relative_path).resolve(strict=False)
    if not absolute_path.exists():
        return ProjectionArtifactSnapshot(
            target_key=target_key,
            artifact_path=relative_path,
            exists=False,
            generated_at=None,
            failure_reason=None,
        )

    try:
        content_md = absolute_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperatorReadError(
            "Failed to read current-state projection artifact for "
            f"target_key={target_key!r}: {exc}"
        ) from exc

    generated_at: datetime | None = None
    failure_reason: str | None = None
    parse_failures: list[str] = []

    generated_at_match = _GENERATED_AT_RE.search(content_md)
    if generated_at_match is None:
        parse_failures.append("Malformed projection artifact header: missing Generated At.")
    else:
        try:
            generated_at = datetime.fromisoformat(generated_at_match.group(1))
        except ValueError as exc:
            parse_failures.append(
                "Malformed projection artifact header: invalid Generated At "
                f"timestamp ({exc})."
            )

    failure_reason_match = _FAILURE_REASON_RE.search(content_md)
    if failure_reason_match is None:
        parse_failures.append(
            "Malformed projection artifact header: missing Failure Reason."
        )
    else:
        parsed_failure_reason = failure_reason_match.group(1).strip()
        if parsed_failure_reason != "None":
            failure_reason = parsed_failure_reason

    if parse_failures:
        combined_parse_failure = " ".join(parse_failures)
        failure_reason = (
            combined_parse_failure
            if failure_reason is None
            else f"{failure_reason} {combined_parse_failure}"
        )

    return ProjectionArtifactSnapshot(
        target_key=target_key,
        artifact_path=relative_path,
        exists=True,
        generated_at=generated_at,
        failure_reason=failure_reason,
    )


def _collect_current_page_set(source_pages: LiveMonitorSourcePages) -> tuple[str, ...]:
    page_paths: list[str] = []
    seen: set[str] = set()

    for page_path in source_pages.page_paths:
        if page_path not in seen:
            seen.add(page_path)
            page_paths.append(page_path)

    for match in _PAGE_PATH_RE.finditer(source_pages.index.content_md):
        page_path = match.group(1)
        try:
            normalized_page_path = resolve_page_path_reference(page_path)
        except OperatorReadError:
            continue
        if normalized_page_path in seen:
            continue
        seen.add(normalized_page_path)
        page_paths.append(normalized_page_path)

    return tuple(page_paths)


def _latest_log_anchor(content_md: str) -> str | None:
    heading_match = _SECOND_LEVEL_HEADING_RE.search(content_md)
    if heading_match is not None:
        return heading_match.group(1).strip()

    lines = content_md.splitlines()
    for line in lines[1:]:
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def resolve_page_path_reference(page_path: str) -> str:
    """Resolve a logical page-path string to its normalized contract path."""
    from event_trader.contracts import resolve_page_ref

    return resolve_page_ref(page_path).page_path


__all__ = [
    "load_live_monitor",
    "render_live_monitor",
]
