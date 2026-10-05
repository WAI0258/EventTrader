"""Lean operator inspection surface for current failures and next-look locations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from event_trader.contracts._validators import normalize_content, validate_target_key
from event_trader.projection.report_artifacts import current_state_artifact_path
from event_trader.storage import WorkspaceLayout

from .live_monitor import load_live_monitor
from .read_models import LiveMonitorSnapshot, OperatorReadError

InspectionSubsystem = Literal[
    "feed",
    "admission",
    "ledger",
    "checker",
    "analysis",
    "projection",
]

_CANONICAL_SUBSYSTEM_ORDER: tuple[InspectionSubsystem, ...] = (
    "feed",
    "admission",
    "ledger",
    "checker",
    "analysis",
    "projection",
)


@dataclass(frozen=True, slots=True)
class ExplicitSubsystemFailure:
    """One explicit current failure signal supplied to the operator surface."""

    subsystem: str
    reason: str
    artifact_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "subsystem", _normalize_subsystem(self.subsystem))
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
class SubsystemInspectionSurface:
    """Read-only inspection view for one subsystem boundary."""

    subsystem: str
    failing: bool
    failure_reason: str | None
    failure_artifact_path: Path | None
    last_valid_summary: str
    last_valid_artifact_path: Path | None
    next_look_location: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "subsystem", _normalize_subsystem(self.subsystem))
        if not isinstance(self.failing, bool):
            raise OperatorReadError("failing must be a boolean.")
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
        if self.failure_artifact_path is not None and not isinstance(
            self.failure_artifact_path,
            Path,
        ):
            raise OperatorReadError(
                "failure_artifact_path must be a pathlib.Path when provided."
            )
        object.__setattr__(
            self,
            "last_valid_summary",
            normalize_content(
                self.last_valid_summary,
                field_name="last_valid_summary",
                error_type=OperatorReadError,
            ),
        )
        if self.last_valid_artifact_path is not None and not isinstance(
            self.last_valid_artifact_path,
            Path,
        ):
            raise OperatorReadError(
                "last_valid_artifact_path must be a pathlib.Path when provided."
            )
        object.__setattr__(
            self,
            "next_look_location",
            normalize_content(
                self.next_look_location,
                field_name="next_look_location",
                error_type=OperatorReadError,
            ),
        )
        if self.failing and self.failure_reason is None:
            raise OperatorReadError("failing subsystem surfaces must include failure_reason.")
        if not self.failing:
            if self.failure_reason is not None:
                raise OperatorReadError(
                    "healthy subsystem surfaces must not include failure_reason."
                )
            if self.failure_artifact_path is not None:
                raise OperatorReadError(
                    "healthy subsystem surfaces must not include failure_artifact_path."
                )


@dataclass(frozen=True, slots=True)
class OperatorInspectionSnapshot:
    """Current operator inspection snapshot over live state plus explicit failures."""

    target_key: str
    live_monitor: LiveMonitorSnapshot
    subsystem_surfaces: tuple[SubsystemInspectionSurface, ...]
    failing_subsystems: tuple[str, ...]

    def __post_init__(self) -> None:
        validated_target_key = validate_target_key(
            self.target_key,
            error_type=OperatorReadError,
        )
        if not isinstance(self.live_monitor, LiveMonitorSnapshot):
            raise OperatorReadError(
                "live_monitor must be a LiveMonitorSnapshot instance."
            )
        if self.live_monitor.target_key != validated_target_key:
            raise OperatorReadError(
                "live_monitor.target_key must match OperatorInspectionSnapshot.target_key."
            )
        if not isinstance(self.subsystem_surfaces, tuple):
            raise OperatorReadError(
                "subsystem_surfaces must be a tuple of SubsystemInspectionSurface instances."
            )

        normalized_surfaces: list[SubsystemInspectionSurface] = []
        seen_subsystems: set[str] = set()
        expected_order = list(_CANONICAL_SUBSYSTEM_ORDER)
        observed_order: list[str] = []
        for surface in self.subsystem_surfaces:
            if not isinstance(surface, SubsystemInspectionSurface):
                raise OperatorReadError(
                    "subsystem_surfaces must contain only SubsystemInspectionSurface instances."
                )
            if surface.subsystem in seen_subsystems:
                raise OperatorReadError(
                    "subsystem_surfaces must not contain duplicate subsystems."
                )
            seen_subsystems.add(surface.subsystem)
            observed_order.append(surface.subsystem)
            normalized_surfaces.append(surface)

        if observed_order != expected_order:
            raise OperatorReadError(
                "subsystem_surfaces must stay in canonical subsystem order: "
                f"{', '.join(expected_order)}."
            )

        normalized_failing_subsystems = _normalize_failing_subsystems(
            self.failing_subsystems
        )
        expected_failing_subsystems = tuple(
            surface.subsystem for surface in normalized_surfaces if surface.failing
        )
        if normalized_failing_subsystems != expected_failing_subsystems:
            raise OperatorReadError(
                "failing_subsystems must equal the canonical ordered set of failing "
                "subsystem surfaces."
            )

        object.__setattr__(self, "target_key", validated_target_key)
        object.__setattr__(self, "subsystem_surfaces", tuple(normalized_surfaces))
        object.__setattr__(self, "failing_subsystems", normalized_failing_subsystems)


def load_operator_inspection(
    *,
    target_key: str,
    layout: WorkspaceLayout,
    failures: tuple[ExplicitSubsystemFailure, ...] = (),
) -> OperatorInspectionSnapshot:
    """Load current operator inspection data for one target and its failures."""
    if not isinstance(layout, WorkspaceLayout):
        raise OperatorReadError("layout must be a WorkspaceLayout instance.")

    validated_target_key = validate_target_key(
        target_key,
        error_type=OperatorReadError,
    )
    normalized_failures = _normalize_failures(failures)
    live_monitor = load_live_monitor(target_key=validated_target_key, layout=layout)
    ledger_summary, ledger_artifact_path = _ledger_last_valid_state(
        layout,
        target_key=validated_target_key,
    )
    projection_artifact_path = Path("runtime") / current_state_artifact_path(
        validated_target_key
    )
    checker_artifact_path = (
        Path("runtime") / "checker_decisions" / validated_target_key
    )
    analysis_log_path = _research_memory_relative_path(
        live_monitor.source_pages.log.page_path
    )

    failure_by_subsystem = {
        failure.subsystem: failure for failure in normalized_failures
    }
    subsystem_surfaces = tuple(
        _build_subsystem_surface(
            subsystem=subsystem,
            target_key=validated_target_key,
            failure=failure_by_subsystem.get(subsystem),
            live_monitor=live_monitor,
            ledger_summary=ledger_summary,
            ledger_artifact_path=ledger_artifact_path,
            checker_artifact_path=checker_artifact_path,
            analysis_log_path=analysis_log_path,
            projection_artifact_path=projection_artifact_path,
        )
        for subsystem in _CANONICAL_SUBSYSTEM_ORDER
    )

    return OperatorInspectionSnapshot(
        target_key=validated_target_key,
        live_monitor=live_monitor,
        subsystem_surfaces=subsystem_surfaces,
        failing_subsystems=tuple(
            surface.subsystem for surface in subsystem_surfaces if surface.failing
        ),
    )



def render_operator_inspection(snapshot: OperatorInspectionSnapshot) -> str:
    """Render a compact markdown inspection view for operators."""
    if not isinstance(snapshot, OperatorInspectionSnapshot):
        raise OperatorReadError(
            "snapshot must be an OperatorInspectionSnapshot instance."
        )

    projection_generated_at = (
        snapshot.live_monitor.projection.generated_at.isoformat()
        if snapshot.live_monitor.projection.generated_at is not None
        else "None"
    )
    latest_log_anchor = snapshot.live_monitor.latest_log_anchor or "None"
    failing_subsystems = (
        ", ".join(f"`{subsystem}`" for subsystem in snapshot.failing_subsystems)
        if snapshot.failing_subsystems
        else "None"
    )
    lines = [
        "# Operator Inspection",
        "",
        "## Research Supervision Summary",
        "",
        f"- Target Key: `{snapshot.target_key}`",
        f"- Failing Subsystems: {failing_subsystems}",
        f"- Latest Log Anchor: `{latest_log_anchor}`",
        f"- Current Reading Path Count: {len(snapshot.live_monitor.current_page_set)}",
        "",
        "## Current Reading Path",
        "",
    ]
    lines.extend(
        f"- `{page_path}`" for page_path in snapshot.live_monitor.current_page_set
    )
    lines.extend(["", "## Next Look Summary", ""])
    lines.extend(_next_look_summary_lines(snapshot))
    lines.extend(
        [
            "",
            "## Artifact Metadata",
            "",
            f"- Projection Generated At: `{projection_generated_at}`",
            "",
            "## Subsystem Inspection",
            "",
        ]
    )

    for surface in snapshot.subsystem_surfaces:
        failure_reason = surface.failure_reason or "None"
        failure_artifact = (
            f"`{surface.failure_artifact_path.as_posix()}`"
            if surface.failure_artifact_path is not None
            else "None"
        )
        last_valid_artifact = (
            f"`{surface.last_valid_artifact_path.as_posix()}`"
            if surface.last_valid_artifact_path is not None
            else "None"
        )
        status = "failing" if surface.failing else "ok"
        lines.extend(
            [
                f"### {surface.subsystem.title()}",
                "",
                f"- Status: {status}",
                f"- Failure Reason: {failure_reason}",
                f"- Failure Artifact: {failure_artifact}",
                f"- Last Valid State: {surface.last_valid_summary}",
                f"- Last Valid Artifact: {last_valid_artifact}",
                f"- Operator Next Look: {surface.next_look_location}",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _next_look_summary_lines(
    snapshot: OperatorInspectionSnapshot,
) -> list[str]:
    failing_surfaces = [
        surface for surface in snapshot.subsystem_surfaces if surface.failing
    ]
    if not failing_surfaces:
        return [
            "- No active subsystem failures.",
            "- Continue from the current reading path and latest research change.",
        ]

    return [
        f"- `{surface.subsystem}`: {surface.next_look_location}"
        for surface in failing_surfaces
    ]



def _build_subsystem_surface(
    *,
    subsystem: InspectionSubsystem,
    target_key: str,
    failure: ExplicitSubsystemFailure | None,
    live_monitor: LiveMonitorSnapshot,
    ledger_summary: str,
    ledger_artifact_path: Path | None,
    checker_artifact_path: Path,
    analysis_log_path: Path,
    projection_artifact_path: Path,
) -> SubsystemInspectionSurface:
    if subsystem in {"feed", "admission", "ledger"}:
        last_valid_summary = ledger_summary
        last_valid_artifact_path = ledger_artifact_path
    elif subsystem == "checker":
        last_valid_summary, last_valid_artifact_path = _checker_last_valid_state(
            live_monitor,
            checker_artifact_path=checker_artifact_path,
        )
    elif subsystem == "analysis":
        last_valid_summary, last_valid_artifact_path = _analysis_last_valid_state(
            live_monitor,
            analysis_log_path=analysis_log_path,
        )
    else:
        last_valid_summary, last_valid_artifact_path = _projection_last_valid_state(
            live_monitor,
            projection_artifact_path=projection_artifact_path,
        )

    return SubsystemInspectionSurface(
        subsystem=subsystem,
        failing=failure is not None,
        failure_reason=None if failure is None else failure.reason,
        failure_artifact_path=None if failure is None else failure.artifact_path,
        last_valid_summary=last_valid_summary,
        last_valid_artifact_path=last_valid_artifact_path,
        next_look_location=_next_look_location(
            subsystem=subsystem,
            target_key=target_key,
            failure=failure,
            checker_artifact_path=checker_artifact_path,
            analysis_log_path=analysis_log_path,
            projection_artifact_path=projection_artifact_path,
            ledger_artifact_path=ledger_artifact_path,
        ),
    )



def _normalize_subsystem(value: str) -> InspectionSubsystem:
    if not isinstance(value, str):
        raise OperatorReadError("subsystem must be a string.")
    normalized = value.strip()
    if normalized != value:
        raise OperatorReadError(
            "subsystem must not include leading or trailing whitespace."
        )
    if normalized not in _CANONICAL_SUBSYSTEM_ORDER:
        allowed = ", ".join(_CANONICAL_SUBSYSTEM_ORDER)
        raise OperatorReadError(f"subsystem must be one of: {allowed}.")
    return normalized



def _normalize_failures(
    failures: tuple[ExplicitSubsystemFailure, ...],
) -> tuple[ExplicitSubsystemFailure, ...]:
    if not isinstance(failures, tuple):
        raise OperatorReadError(
            "failures must be a tuple of ExplicitSubsystemFailure instances."
        )

    normalized: list[ExplicitSubsystemFailure] = []
    seen: set[str] = set()
    for failure in failures:
        if not isinstance(failure, ExplicitSubsystemFailure):
            raise OperatorReadError(
                "failures must contain only ExplicitSubsystemFailure instances."
            )
        if failure.subsystem in seen:
            raise OperatorReadError("failures must not contain duplicate subsystems.")
        seen.add(failure.subsystem)
        normalized.append(failure)

    return tuple(normalized)



def _normalize_failing_subsystems(
    failing_subsystems: tuple[str, ...],
) -> tuple[str, ...]:
    if not isinstance(failing_subsystems, tuple):
        raise OperatorReadError("failing_subsystems must be a tuple of subsystem names.")

    normalized: list[str] = []
    seen: set[str] = set()
    for subsystem in failing_subsystems:
        normalized_subsystem = _normalize_subsystem(subsystem)
        if normalized_subsystem in seen:
            raise OperatorReadError(
                "failing_subsystems must not contain duplicate subsystem names."
            )
        seen.add(normalized_subsystem)
        normalized.append(normalized_subsystem)
    return tuple(normalized)



def _ledger_last_valid_state(
    layout: WorkspaceLayout,
    *,
    target_key: str,
) -> tuple[str, Path | None]:
    target_root = layout.ledger_root / target_key
    partitions = sorted(path for path in target_root.glob("*.jsonl") if path.is_file())
    if not partitions:
        return "No admitted evidence ledger partition exists yet.", None

    latest_partition = partitions[-1]
    try:
        lines = latest_partition.read_text(encoding="utf-8").splitlines()
        line_count = sum(1 for line in lines if line.strip())
    except OSError as exc:
        raise OperatorReadError(
            "Failed to read latest ledger partition for operator inspection "
            f"target_key={target_key!r}: {exc}"
        ) from exc

    relative_path = latest_partition.relative_to(layout.root)
    return (
        f"Latest durable ledger partition `{relative_path.as_posix()}` has {line_count} record(s).",
        relative_path,
    )



def _checker_last_valid_state(
    live_monitor: LiveMonitorSnapshot,
    *,
    checker_artifact_path: Path,
) -> tuple[str, Path | None]:
    _ = live_monitor
    return (
        "Checker decisions are recorded under "
        f"`{checker_artifact_path.as_posix()}` by evidence business month.",
        checker_artifact_path,
    )



def _analysis_last_valid_state(
    live_monitor: LiveMonitorSnapshot,
    *,
    analysis_log_path: Path,
) -> tuple[str, Path]:
    latest_log_anchor = live_monitor.latest_log_anchor or "None"
    return (
        "Latest committed research-memory state is anchored by "
        f"`{latest_log_anchor}` across {len(live_monitor.current_page_set)} visible page(s).",
        analysis_log_path,
    )



def _projection_last_valid_state(
    live_monitor: LiveMonitorSnapshot,
    *,
    projection_artifact_path: Path,
) -> tuple[str, Path | None]:
    projection = live_monitor.projection
    if projection.exists and projection.generated_at is not None:
        return (
            "Projection artifact "
            f"`{projection_artifact_path.as_posix()}` was generated at "
            f"`{projection.generated_at.isoformat()}`.",
            projection_artifact_path,
        )
    if projection.exists:
        return (
            "Projection artifact exists but no valid Generated At timestamp is currently "
            "available.",
            projection_artifact_path,
        )
    return "No projection artifact exists yet.", None



def _next_look_location(
    *,
    subsystem: InspectionSubsystem,
    target_key: str,
    failure: ExplicitSubsystemFailure | None,
    checker_artifact_path: Path,
    analysis_log_path: Path,
    projection_artifact_path: Path,
    ledger_artifact_path: Path | None,
) -> str:
    if failure is not None and failure.artifact_path is not None:
        return f"`{failure.artifact_path.as_posix()}`"

    if subsystem == "feed":
        return "feed-layer error output"
    if subsystem == "admission":
        return "admission validation boundary (no runtime event should exist)"
    if subsystem == "ledger":
        if ledger_artifact_path is not None:
            return f"`{ledger_artifact_path.as_posix()}`"
        return f"`ledger/{target_key}/`"
    if subsystem == "checker":
        return f"`{checker_artifact_path.as_posix()}`"
    if subsystem == "analysis":
        return f"`{analysis_log_path.as_posix()}`"
    return f"`{projection_artifact_path.as_posix()}`"



def _research_memory_relative_path(page_path: str) -> Path:
    return Path("research_memory") / Path(page_path)


__all__ = [
    "ExplicitSubsystemFailure",
    "InspectionSubsystem",
    "OperatorInspectionSnapshot",
    "SubsystemInspectionSurface",
    "load_operator_inspection",
    "render_operator_inspection",
]
