"""File-backed outcome-context port for shared reflection anchors."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from event_trader.analysis import FileBackedResearchMemoryReader
from event_trader.contracts.ports import OutcomeContextPort
from event_trader.storage import WorkspaceLayout

from .anchors import ReflectionLogEntry, parse_reflection_log_entries
from .contracts import OutcomeContextPacket, ReviewAnchorIdentity, ReviewCoverage


class ReflectionOutcomeContextError(ValueError):
    """Raised when canonical outcome context cannot be recovered."""


@dataclass(frozen=True, slots=True)
class FileBackedReflectionOutcomeContextPort(OutcomeContextPort):
    """Read shared reflection outcome context from later canonical log entries."""

    layout: WorkspaceLayout
    _research_memory: FileBackedResearchMemoryReader = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.layout, WorkspaceLayout):
            raise ReflectionOutcomeContextError(
                "layout must be a WorkspaceLayout instance."
            )
        object.__setattr__(
            self,
            "_research_memory",
            FileBackedResearchMemoryReader(self.layout),
        )

    def read_outcome_context(
        self,
        anchor: ReviewAnchorIdentity,
        coverage: ReviewCoverage,
    ) -> OutcomeContextPacket:
        if not isinstance(anchor, ReviewAnchorIdentity):
            raise ReflectionOutcomeContextError(
                "anchor must be a ReviewAnchorIdentity instance."
            )
        if not isinstance(coverage, ReviewCoverage):
            raise ReflectionOutcomeContextError(
                "coverage must be a ReviewCoverage instance."
            )

        page = self._research_memory.read_page(anchor.log_page_path)
        entries = parse_reflection_log_entries(
            log_page_path=page.page_path,
            content_md=page.content_md,
        )
        later_entries = _later_entries(
            entries=entries,
            anchor=anchor,
            coverage=coverage,
        )
        if not later_entries:
            raise ReflectionOutcomeContextError(
                "No later canonical log entries exist inside the requested "
                "reflection window."
            )

        observed_at = later_entries[-1].logged_at
        if observed_at is None:
            raise ReflectionOutcomeContextError(
                "Later canonical log entries must carry logged_at timestamps."
            )

        return OutcomeContextPacket(
            anchor=anchor,
            coverage=coverage,
            observed_at=observed_at,
            summary_md=_render_outcome_summary(later_entries),
        )


def _later_entries(
    *,
    entries: tuple[ReflectionLogEntry, ...],
    anchor: ReviewAnchorIdentity,
    coverage: ReviewCoverage,
) -> tuple[ReflectionLogEntry, ...]:
    window_end = anchor.logged_at + timedelta(hours=max(coverage.horizons_hours))
    return tuple(
        entry
        for entry in entries
        if entry.is_canonical
        and entry.logged_at is not None
        and entry.anchor_id != f"{anchor.log_page_path}#{anchor.entry_key}"
        and anchor.logged_at < entry.logged_at <= window_end
    )


def _render_outcome_summary(entries: tuple[ReflectionLogEntry, ...]) -> str:
    lines = [
        "Later canonical log entries observed inside the requested reflection window:",
        "",
    ]
    for entry in entries:
        if entry.logged_at is None:
            raise ReflectionOutcomeContextError(
                "Later canonical log entries must carry logged_at timestamps."
            )
        lines.extend(
            [
                f"## {entry.heading_text}",
                "",
                f"- Logged At: `{entry.logged_at.isoformat()}`",
                f"- Anchor ID: `{entry.anchor_id}`",
                "",
                entry.body_md,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "FileBackedReflectionOutcomeContextPort",
    "ReflectionOutcomeContextError",
]
