"""Typed read-side models for operator inspection surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from event_trader.contracts import PageReadResult, resolve_page_ref
from event_trader.contracts._validators import (
    normalize_content,
    validate_target_key,
    validate_timestamp,
)


class OperatorReadError(ValueError):
    """Raised when operator read models or inputs are malformed."""


@dataclass(frozen=True, slots=True)
class LiveMonitorSourcePages:
    """Canonical target pages directly read by the live-monitor surface."""

    target_key: str
    index: PageReadResult
    log: PageReadResult

    def __post_init__(self) -> None:
        validated_target_key = validate_target_key(
            self.target_key,
            error_type=OperatorReadError,
        )
        expected_page_paths = (
            f"targets/{validated_target_key}/index.md",
            f"targets/{validated_target_key}/log.md",
        )
        pages = (self.index, self.log)

        for expected_page_path, page in zip(expected_page_paths, pages, strict=True):
            if not isinstance(page, PageReadResult):
                raise OperatorReadError(
                    "LiveMonitorSourcePages must contain only PageReadResult instances."
                )
            if page.page_path != expected_page_path:
                raise OperatorReadError(
                    "LiveMonitorSourcePages must use the canonical live-monitor page "
                    f"set for target_key={validated_target_key!r}; expected "
                    f"{expected_page_path!r}, got {page.page_path!r}."
                )

        object.__setattr__(self, "target_key", validated_target_key)

    @property
    def page_paths(self) -> tuple[str, str]:
        """Return the canonical ordered logical page paths used by live monitoring."""
        return (self.index.page_path, self.log.page_path)


@dataclass(frozen=True, slots=True)
class ProjectionArtifactSnapshot:
    """Lean projection state surfaced to live monitoring."""

    target_key: str
    artifact_path: Path
    exists: bool
    generated_at: datetime | None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=OperatorReadError),
        )
        if not isinstance(self.artifact_path, Path):
            raise OperatorReadError("artifact_path must be a pathlib.Path.")
        if not isinstance(self.exists, bool):
            raise OperatorReadError("exists must be a boolean.")
        if self.generated_at is not None:
            object.__setattr__(
                self,
                "generated_at",
                validate_timestamp(
                    self.generated_at,
                    field_name="generated_at",
                    error_type=OperatorReadError,
                ),
            )
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                normalize_content(
                    self.failure_reason,
                    field_name="failure_reason",
                    error_type=OperatorReadError,
                ),
            )


@dataclass(frozen=True, slots=True)
class FailureFlag:
    """Explicit active failure signal surfaced to operators."""

    subsystem: str
    reason: str
    artifact_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.subsystem, str):
            raise OperatorReadError("subsystem must be a string.")
        normalized_subsystem = self.subsystem.strip()
        if not normalized_subsystem:
            raise OperatorReadError("subsystem must not be blank.")
        if normalized_subsystem != self.subsystem:
            raise OperatorReadError(
                "subsystem must not include leading or trailing whitespace."
            )
        if any(ch in normalized_subsystem for ch in ("\n", "\r")):
            raise OperatorReadError("subsystem must stay on one line.")
        object.__setattr__(self, "subsystem", normalized_subsystem)
        object.__setattr__(
            self,
            "reason",
            normalize_content(
                self.reason,
                field_name="reason",
                error_type=OperatorReadError,
            ),
        )
        if self.artifact_path is not None and not isinstance(self.artifact_path, Path):
            raise OperatorReadError("artifact_path must be a pathlib.Path when provided.")


@dataclass(frozen=True, slots=True)
class LiveMonitorSnapshot:
    """Read-only live monitor summary for one target."""

    target_key: str
    source_pages: LiveMonitorSourcePages
    current_page_set: tuple[str, ...]
    latest_log_anchor: str | None
    projection: ProjectionArtifactSnapshot
    active_failure_flags: tuple[FailureFlag, ...] = ()

    def __post_init__(self) -> None:
        validated_target_key = validate_target_key(
            self.target_key,
            error_type=OperatorReadError,
        )
        if not isinstance(self.source_pages, LiveMonitorSourcePages):
            raise OperatorReadError(
                "source_pages must be a LiveMonitorSourcePages instance."
            )
        if self.source_pages.target_key != validated_target_key:
            raise OperatorReadError(
                "source_pages.target_key must match LiveMonitorSnapshot.target_key."
            )
        if not isinstance(self.projection, ProjectionArtifactSnapshot):
            raise OperatorReadError(
                "projection must be a ProjectionArtifactSnapshot instance."
            )
        if self.projection.target_key != validated_target_key:
            raise OperatorReadError(
                "projection.target_key must match LiveMonitorSnapshot.target_key."
            )
        object.__setattr__(
            self,
            "target_key",
            validated_target_key,
        )
        object.__setattr__(
            self,
            "current_page_set",
            _normalize_current_page_set(self.current_page_set),
        )
        if self.latest_log_anchor is not None:
            object.__setattr__(
                self,
                "latest_log_anchor",
                _normalize_one_line_text(
                    self.latest_log_anchor,
                    field_name="latest_log_anchor",
                ),
            )
        object.__setattr__(
            self,
            "active_failure_flags",
            _normalize_failure_flags(self.active_failure_flags),
        )


def _normalize_current_page_set(current_page_set: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(current_page_set, tuple):
        raise OperatorReadError("current_page_set must be a tuple of logical page paths.")
    if not current_page_set:
        raise OperatorReadError("current_page_set must not be empty.")

    normalized_paths: list[str] = []
    seen: set[str] = set()
    for page_path in current_page_set:
        if not isinstance(page_path, str):
            raise OperatorReadError(
                "current_page_set must contain only logical page-path strings."
            )
        resolved = resolve_page_ref(page_path).page_path
        if resolved in seen:
            raise OperatorReadError("current_page_set must not contain duplicates.")
        seen.add(resolved)
        normalized_paths.append(resolved)

    return tuple(normalized_paths)


def _normalize_one_line_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise OperatorReadError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise OperatorReadError(f"{field_name} must not be blank.")
    if any(ch in normalized for ch in ("\n", "\r")):
        raise OperatorReadError(f"{field_name} must stay on one line.")
    return normalized


def _normalize_failure_flags(
    active_failure_flags: tuple[FailureFlag, ...],
) -> tuple[FailureFlag, ...]:
    if not isinstance(active_failure_flags, tuple):
        raise OperatorReadError(
            "active_failure_flags must be a tuple of FailureFlag instances."
        )

    normalized: list[FailureFlag] = []
    seen: set[tuple[str, str]] = set()
    for flag in active_failure_flags:
        if not isinstance(flag, FailureFlag):
            raise OperatorReadError(
                "active_failure_flags must contain only FailureFlag instances."
            )
        key = (flag.subsystem, flag.reason)
        if key in seen:
            raise OperatorReadError(
                "active_failure_flags must not contain duplicate subsystem/reason pairs."
            )
        seen.add(key)
        normalized.append(flag)

    return tuple(normalized)


__all__ = [
    "FailureFlag",
    "LiveMonitorSnapshot",
    "LiveMonitorSourcePages",
    "OperatorReadError",
    "ProjectionArtifactSnapshot",
]
