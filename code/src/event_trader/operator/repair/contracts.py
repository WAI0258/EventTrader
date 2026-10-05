"""Shared operator repair contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

RepairScope = Literal["live", "replay"]
RepairStatus = Literal["dry_run", "applied", "blocked"]
RepairActionKind = Literal["remove_jsonl_records", "delete_directory"]
RepairBoundaryKind = Literal["replay_event", "replay_time"]
RepairLifecycleStage = Literal[
    "orchestration_residue",
    "analysis_truth",
    "pm_execution_truth",
    "reflection_truth",
    "projection",
    "unknown",
]


class OperatorRepairError(ValueError):
    """Raised when an operator repair plan cannot be built or applied safely."""


@dataclass(frozen=True, slots=True)
class RepairAction:
    kind: RepairActionKind
    path: Path
    record_count: int = 0
    line_numbers: tuple[int, ...] = ()

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": _relative_path(self.path, workspace_root),
            "record_count": self.record_count,
            "line_numbers": list(self.line_numbers),
        }


@dataclass(frozen=True, slots=True)
class RepairBoundarySummary:
    kind: RepairBoundaryKind
    repair_replay_at: datetime | None = None
    preserved_event_ids: tuple[str, ...] = ()
    affected_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.repair_replay_at is None:
            return
        if (
            self.repair_replay_at.tzinfo is None
            or self.repair_replay_at.utcoffset() is None
        ):
            raise OperatorRepairError("repair_replay_at must be timezone-aware.")
        object.__setattr__(
            self,
            "repair_replay_at",
            self.repair_replay_at.astimezone(UTC),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "repair_replay_at": (
                None
                if self.repair_replay_at is None
                else self.repair_replay_at.isoformat()
            ),
            "preserved_event_ids": list(self.preserved_event_ids),
            "affected_event_ids": list(self.affected_event_ids),
        }


@dataclass(frozen=True, slots=True)
class RepairStageCount:
    stage: RepairLifecycleStage
    artifact_count: int

    def to_json_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "artifact_count": self.artifact_count,
        }


@dataclass(frozen=True, slots=True)
class RepairTouchedArtifact:
    path: Path
    stage: RepairLifecycleStage
    record_count: int = 0
    line_numbers: tuple[int, ...] = ()

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "path": _relative_path(self.path, workspace_root),
            "stage": self.stage,
            "record_count": self.record_count,
            "line_numbers": list(self.line_numbers),
        }


@dataclass(frozen=True, slots=True)
class RepairBlocker:
    reason: str
    path: Path | None = None

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "reason": self.reason,
            "path": None if self.path is None else _relative_path(self.path, workspace_root),
        }


@dataclass(frozen=True, slots=True)
class RepairPlan:
    scope: RepairScope
    status: RepairStatus
    target_key: str
    event_id: str | None
    run_key: str | None = None
    run_id: str | None = None
    actions: tuple[RepairAction, ...] = ()
    blockers: tuple[RepairBlocker, ...] = ()
    boundary_summary: RepairBoundarySummary | None = None
    touched_stage_counts: tuple[RepairStageCount, ...] = ()
    blocked_artifacts: tuple[RepairTouchedArtifact, ...] = ()
    rebuildable_artifacts: tuple[RepairTouchedArtifact, ...] = ()

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "scope": self.scope,
            "status": self.status,
            "target_key": self.target_key,
            "event_id": self.event_id,
            "run_key": self.run_key,
            "run_id": self.run_id,
            "actions": [
                action.to_json_payload(workspace_root=workspace_root)
                for action in self.actions
            ],
            "blockers": [
                blocker.to_json_payload(workspace_root=workspace_root)
                for blocker in self.blockers
            ],
            "boundary_summary": (
                None
                if self.boundary_summary is None
                else self.boundary_summary.to_json_payload()
            ),
            "touched_stage_counts": [
                item.to_json_payload() for item in self.touched_stage_counts
            ],
            "blocked_artifacts": [
                item.to_json_payload(workspace_root=workspace_root)
                for item in self.blocked_artifacts
            ],
            "rebuildable_artifacts": [
                item.to_json_payload(workspace_root=workspace_root)
                for item in self.rebuildable_artifacts
            ],
        }


@dataclass(frozen=True, slots=True)
class RepairReceipt:
    plan: RepairPlan
    applied_at: datetime
    backup_root: Path | None
    receipt_path: Path | None

    def __post_init__(self) -> None:
        if self.applied_at.tzinfo is None or self.applied_at.utcoffset() is None:
            raise OperatorRepairError("applied_at must be timezone-aware.")
        object.__setattr__(self, "applied_at", self.applied_at.astimezone(UTC))

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "applied_at": self.applied_at.isoformat(),
            "backup_root": (
                None
                if self.backup_root is None
                else _relative_path(self.backup_root, workspace_root)
            ),
            "receipt_path": (
                None
                if self.receipt_path is None
                else _relative_path(self.receipt_path, workspace_root)
            ),
            "plan": self.plan.to_json_payload(workspace_root=workspace_root),
        }


def _relative_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(
            workspace_root.resolve(strict=False)
        ).as_posix()
    except ValueError:
        return path.resolve(strict=False).as_posix()
