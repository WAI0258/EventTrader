"""Repair failed live-catchup runtime footprints via the shared lifecycle planner."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.live_catchup_identity import (
    live_catchup_chain_run_key,
    live_catchup_run_id_prefix,
    matches_live_catchup_chain_run_key,
)
from event_trader.operator.repair.artifacts import (
    backup_and_delete_directory,
    read_jsonl_records,
    rewrite_jsonl_removing_lines,
)
from event_trader.operator.repair.contracts import (
    RepairAction,
    RepairBlocker,
    RepairPlan,
)
from event_trader.operator.repair.lifecycle_planner import (
    LifecycleRepairBoundary,
    build_lifecycle_repair_plan,
)
from event_trader.operator.repair.runtime_analysis import plan_analysis_recovery
from event_trader.operator.repair.runtime_pm import plan_pm_execution_recovery
from event_trader.replay.checkpoint import (
    PersistedReplayEventCheckpoint,
    ReplayEventCheckpoint,
    read_persisted_replay_checkpoints,
)
from event_trader.storage import WorkspaceLayout


class LiveCatchupRuntimeRepairError(ValueError):
    """Raised when live catch-up runtime repair cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class LiveCatchupRuntimeRepairReport:
    target_key: str
    start_at: datetime
    run_key: str
    run_id_prefix: str
    apply: bool
    status: Literal["dry_run", "applied", "blocked", "noop"]
    repair_replay_at: datetime | None
    preserved_event_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    analysis_unit_ids: tuple[str, ...]
    decision_episode_ids: tuple[str, ...]
    context_packet_ids: tuple[str, ...]
    actions: tuple[RepairAction, ...]
    blockers: tuple[RepairBlocker, ...]
    removed_count: int
    deleted_directories: tuple[Path, ...]
    deleted_files: tuple[Path, ...]
    backup_root: Path | None
    receipt_path: Path | None
    plan: RepairPlan | None

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "start_at": self.start_at.isoformat(),
            "run_key": self.run_key,
            "run_id_prefix": self.run_id_prefix,
            "apply": self.apply,
            "status": self.status,
            "repair_replay_at": (
                None if self.repair_replay_at is None else self.repair_replay_at.isoformat()
            ),
            "preserved_event_ids": list(self.preserved_event_ids),
            "event_ids": list(self.event_ids),
            "analysis_unit_ids": list(self.analysis_unit_ids),
            "decision_episode_ids": list(self.decision_episode_ids),
            "context_packet_ids": list(self.context_packet_ids),
            "actions": [
                action.to_json_payload(workspace_root=workspace_root)
                for action in self.actions
            ],
            "blockers": [
                blocker.to_json_payload(workspace_root=workspace_root)
                for blocker in self.blockers
            ],
            "removed_count": self.removed_count,
            "deleted_directories": [
                _relative_path(path, workspace_root) for path in self.deleted_directories
            ],
            "deleted_files": [
                _relative_path(path, workspace_root) for path in self.deleted_files
            ],
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
            "plan": (
                None
                if self.plan is None
                else self.plan.to_json_payload(workspace_root=workspace_root)
            ),
        }


@dataclass(frozen=True, slots=True)
class _CheckpointEventState:
    event_id: str
    replay_at: datetime
    latest: ReplayEventCheckpoint
    completed: ReplayEventCheckpoint | None
    persisted_entries: tuple[PersistedReplayEventCheckpoint, ...]


@dataclass(frozen=True, slots=True)
class _CheckpointRepairPlan:
    repair_replay_at: datetime | None
    affected_event_ids: frozenset[str]
    preserved_event_ids: frozenset[str]
    checkpoint_action: RepairAction | None
    matching_entries: tuple[PersistedReplayEventCheckpoint, ...] = ()
    states: tuple[_CheckpointEventState, ...] = ()


def repair_live_catchup_runtime(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    apply: bool,
    created_at: datetime | None = None,
) -> LiveCatchupRuntimeRepairReport:
    if not isinstance(layout, WorkspaceLayout):
        raise LiveCatchupRuntimeRepairError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = validate_target_key(
        target_key,
        error_type=LiveCatchupRuntimeRepairError,
    )
    normalized_start_at = validate_timestamp(
        start_at,
        field_name="start_at",
        error_type=LiveCatchupRuntimeRepairError,
    )
    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    stable_run_key = live_catchup_chain_run_key(target_key=normalized_target_key)
    run_id_prefix = live_catchup_run_id_prefix(target_key=normalized_target_key)
    checkpoint_plan = _plan_checkpoint_boundary(
        layout=layout,
        target_key=normalized_target_key,
        run_key=stable_run_key,
    )
    if checkpoint_plan.repair_replay_at is None:
        report = _noop_report(
            target_key=normalized_target_key,
            start_at=normalized_start_at,
            run_key=stable_run_key,
            run_id_prefix=run_id_prefix,
            apply=apply,
            receipt_path=(
                None
                if not apply
                else _receipt_path(layout=layout, target_key=normalized_target_key, created_at=now)
            ),
        )
        if report.receipt_path is not None:
            _write_receipt(layout=layout, report=report)
        return report
    return _repair_from_boundary(
        layout=layout,
        target_key=normalized_target_key,
        start_at=normalized_start_at,
        run_key=stable_run_key,
        run_id_prefix=run_id_prefix,
        apply=apply,
        created_at=now,
        checkpoint_plan=checkpoint_plan,
        replay_context_roots=_context_packet_roots(
            layout=layout,
            run_id_prefixes=(run_id_prefix,),
        ),
    )


def repair_live_catchup_runtime_from_replay_boundary(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    repair_replay_at: datetime,
    apply: bool,
    created_at: datetime | None = None,
) -> LiveCatchupRuntimeRepairReport:
    """Repair derived live-catchup runtime state from an explicit replay boundary."""
    if not isinstance(layout, WorkspaceLayout):
        raise LiveCatchupRuntimeRepairError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = validate_target_key(
        target_key,
        error_type=LiveCatchupRuntimeRepairError,
    )
    normalized_repair_replay_at = validate_timestamp(
        repair_replay_at,
        field_name="repair_replay_at",
        error_type=LiveCatchupRuntimeRepairError,
    ).astimezone(UTC)
    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    checkpoint_plan = _plan_checkpoint_boundary_from_replay_at(
        layout=layout,
        target_key=normalized_target_key,
        repair_replay_at=normalized_repair_replay_at,
    )
    run_key = (
        f"live_catchup_boundary_repair:{normalized_target_key}:"
        f"{_timestamp_token(normalized_repair_replay_at)}"
    )
    run_id_prefix = live_catchup_run_id_prefix(target_key=normalized_target_key)
    if checkpoint_plan.repair_replay_at is None:
        report = _noop_report(
            target_key=normalized_target_key,
            start_at=normalized_repair_replay_at,
            run_key=run_key,
            run_id_prefix=run_id_prefix,
            apply=apply,
            receipt_path=(
                None
                if not apply
                else _receipt_path(layout=layout, target_key=normalized_target_key, created_at=now)
            ),
        )
        if report.receipt_path is not None:
            _write_receipt(layout=layout, report=report)
        return report
    return _repair_from_boundary(
        layout=layout,
        target_key=normalized_target_key,
        start_at=normalized_repair_replay_at,
        run_key=run_key,
        run_id_prefix=run_id_prefix,
        apply=apply,
        created_at=now,
        checkpoint_plan=checkpoint_plan,
        replay_context_roots=_context_packet_roots(
            layout=layout,
            run_id_prefixes=(run_id_prefix,),
        ),
    )


def _repair_from_boundary(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    start_at: datetime,
    run_key: str,
    run_id_prefix: str,
    apply: bool,
    created_at: datetime,
    checkpoint_plan: _CheckpointRepairPlan,
    replay_context_roots: tuple[Path, ...],
) -> LiveCatchupRuntimeRepairReport:
    checkpoint_plan = _refine_checkpoint_plan(
        layout=layout,
        target_key=target_key,
        checkpoint_plan=checkpoint_plan,
    )
    planner_result = build_lifecycle_repair_plan(
        layout=layout,
        boundary=LifecycleRepairBoundary(
            scope="live",
            target_key=target_key,
            boundary_kind="replay_time",
            run_key=run_key,
            run_id=run_id_prefix,
            repair_replay_at=checkpoint_plan.repair_replay_at,
            preserved_event_ids=tuple(sorted(checkpoint_plan.preserved_event_ids)),
            affected_event_ids=tuple(sorted(checkpoint_plan.affected_event_ids)),
            initial_actions=(
                ()
                if checkpoint_plan.checkpoint_action is None
                else (checkpoint_plan.checkpoint_action,)
            ),
            replay_context_roots=replay_context_roots,
        ),
    )
    plan = planner_result.plan

    status: Literal["dry_run", "applied", "blocked", "noop"]
    if plan.blockers:
        status = "blocked"
    elif plan.actions or plan.rebuildable_artifacts:
        status = "dry_run"
    else:
        status = "noop"

    backup_root = None
    removed_count = 0
    deleted_directories: tuple[Path, ...] = ()
    if apply and not plan.blockers and plan.actions:
        backup_root = _backup_root(
            layout=layout,
            target_key=target_key,
            created_at=created_at,
        )
        backup_root.mkdir(parents=True, exist_ok=False)
        removed_count, deleted_directories = _apply_actions(
            actions=plan.actions,
            backup_root=backup_root,
            layout=layout,
        )
        status = "applied"

    receipt_path = None
    if apply:
        receipt_path = _receipt_path(
            layout=layout,
            target_key=target_key,
            created_at=created_at,
        )

    report = LiveCatchupRuntimeRepairReport(
        target_key=target_key,
        start_at=start_at,
        run_key=run_key,
        run_id_prefix=run_id_prefix,
        apply=apply,
        status=status,
        repair_replay_at=checkpoint_plan.repair_replay_at,
        preserved_event_ids=tuple(sorted(checkpoint_plan.preserved_event_ids)),
        event_ids=planner_result.event_ids,
        analysis_unit_ids=planner_result.analysis_unit_ids,
        decision_episode_ids=planner_result.decision_episode_ids,
        context_packet_ids=planner_result.context_packet_ids,
        actions=plan.actions,
        blockers=plan.blockers,
        removed_count=removed_count,
        deleted_directories=deleted_directories,
        deleted_files=(),
        backup_root=backup_root,
        receipt_path=receipt_path,
        plan=plan,
    )
    if receipt_path is not None:
        _write_receipt(layout=layout, report=report)
    return report


def _noop_report(
    *,
    target_key: str,
    start_at: datetime,
    run_key: str,
    run_id_prefix: str,
    apply: bool,
    receipt_path: Path | None,
) -> LiveCatchupRuntimeRepairReport:
    return LiveCatchupRuntimeRepairReport(
        target_key=target_key,
        start_at=start_at,
        run_key=run_key,
        run_id_prefix=run_id_prefix,
        apply=apply,
        status="noop",
        repair_replay_at=None,
        preserved_event_ids=(),
        event_ids=(),
        analysis_unit_ids=(),
        decision_episode_ids=(),
        context_packet_ids=(),
        actions=(),
        blockers=(),
        removed_count=0,
        deleted_directories=(),
        deleted_files=(),
        backup_root=None,
        receipt_path=receipt_path,
        plan=None,
    )


def _plan_checkpoint_boundary(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_key: str,
) -> _CheckpointRepairPlan:
    matching = [
        item
        for item in read_persisted_replay_checkpoints(layout, target_key=target_key)
        if matches_live_catchup_chain_run_key(
            candidate_run_key=item.checkpoint.run_key,
            chain_run_key=run_key,
        )
        and item.checkpoint.stage == "live_catchup_replay"
    ]
    if not matching:
        return _CheckpointRepairPlan(
            repair_replay_at=None,
            affected_event_ids=frozenset(),
            preserved_event_ids=frozenset(),
            checkpoint_action=None,
            matching_entries=(),
            states=(),
        )

    states = _checkpoint_states(matching)
    unresolved = [
        state
        for state in states
        if state.completed is None and state.latest.status in {"started", "failed"}
    ]
    if not unresolved:
        return _CheckpointRepairPlan(
            repair_replay_at=None,
            affected_event_ids=frozenset(),
            preserved_event_ids=frozenset(
                state.event_id for state in states if state.completed is not None
            ),
            checkpoint_action=None,
            matching_entries=tuple(matching),
            states=states,
        )

    boundary = min(state.replay_at for state in unresolved)
    affected_event_ids = {
        state.event_id for state in states if state.replay_at >= boundary
    }
    return _checkpoint_repair_plan(
        layout=layout,
        target_key=target_key,
        repair_replay_at=boundary,
        affected_event_ids=frozenset(affected_event_ids),
        matching_entries=tuple(matching),
        states=states,
    )


def _plan_checkpoint_boundary_from_replay_at(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    repair_replay_at: datetime,
) -> _CheckpointRepairPlan:
    normalized_boundary = validate_timestamp(
        repair_replay_at,
        field_name="repair_replay_at",
        error_type=LiveCatchupRuntimeRepairError,
    ).astimezone(UTC)
    matching = [
        item
        for item in read_persisted_replay_checkpoints(layout, target_key=target_key)
        if item.checkpoint.stage == "live_catchup_replay"
    ]
    if not matching:
        return _CheckpointRepairPlan(
            repair_replay_at=None,
            affected_event_ids=frozenset(),
            preserved_event_ids=frozenset(),
            checkpoint_action=None,
            matching_entries=(),
            states=(),
        )
    states = _checkpoint_states(matching)
    affected_event_ids = {
        state.event_id for state in states if state.replay_at >= normalized_boundary
    }
    if not affected_event_ids:
        return _CheckpointRepairPlan(
            repair_replay_at=None,
            affected_event_ids=frozenset(),
            preserved_event_ids=frozenset(
                state.event_id for state in states if state.completed is not None
            ),
            checkpoint_action=None,
            matching_entries=tuple(matching),
            states=states,
        )
    return _checkpoint_repair_plan(
        layout=layout,
        target_key=target_key,
        repair_replay_at=normalized_boundary,
        affected_event_ids=frozenset(affected_event_ids),
        matching_entries=tuple(matching),
        states=states,
    )


def _checkpoint_repair_plan(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    repair_replay_at: datetime | None,
    affected_event_ids: frozenset[str],
    matching_entries: tuple[PersistedReplayEventCheckpoint, ...],
    states: tuple[_CheckpointEventState, ...],
) -> _CheckpointRepairPlan:
    preserved_event_ids = frozenset(
        state.event_id
        for state in states
        if state.completed is not None and state.event_id not in affected_event_ids
    )
    checkpoint_action = _checkpoint_action_for_event_ids(
        layout=layout,
        target_key=target_key,
        matching_entries=matching_entries,
        affected_event_ids=affected_event_ids,
    )
    return _CheckpointRepairPlan(
        repair_replay_at=repair_replay_at,
        affected_event_ids=affected_event_ids,
        preserved_event_ids=preserved_event_ids,
        checkpoint_action=checkpoint_action,
        matching_entries=matching_entries,
        states=states,
    )


def _refine_checkpoint_plan(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    checkpoint_plan: _CheckpointRepairPlan,
) -> _CheckpointRepairPlan:
    if (
        checkpoint_plan.repair_replay_at is None
        or not checkpoint_plan.affected_event_ids
        or not checkpoint_plan.states
    ):
        return checkpoint_plan

    # Keep generic repair focused on unresolved residue plus incomplete
    # lifecycle chains; later completed events that both recovery planners already
    # classify as no_action should be preserved intact.
    candidate_event_ids = frozenset(
        state.event_id
        for state in checkpoint_plan.states
        if state.event_id in checkpoint_plan.affected_event_ids
        and state.latest.status == "completed"
        and state.replay_at > checkpoint_plan.repair_replay_at
    )
    if not candidate_event_ids:
        return checkpoint_plan

    analysis_complete_clusters = _analysis_no_action_clusters(
        layout=layout,
        target_key=target_key,
        candidate_event_ids=candidate_event_ids,
    )
    pm_complete_clusters = _pm_no_action_clusters(
        layout=layout,
        target_key=target_key,
        candidate_event_ids=candidate_event_ids,
    )
    excluded_event_ids = _provably_complete_cluster_event_ids(
        analysis_clusters=analysis_complete_clusters,
        pm_clusters=pm_complete_clusters,
    )
    if not excluded_event_ids:
        return checkpoint_plan

    affected_event_ids = checkpoint_plan.affected_event_ids - excluded_event_ids
    return _checkpoint_repair_plan(
        layout=layout,
        target_key=target_key,
        repair_replay_at=checkpoint_plan.repair_replay_at,
        affected_event_ids=affected_event_ids,
        matching_entries=checkpoint_plan.matching_entries,
        states=checkpoint_plan.states,
    )


def _analysis_no_action_clusters(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    candidate_event_ids: frozenset[str],
) -> tuple[frozenset[str], ...]:
    if not candidate_event_ids:
        return ()
    clusters: list[frozenset[str]] = []
    report = plan_analysis_recovery(layout=layout, target_key=target_key)
    for decision in report.decisions:
        if decision.planned_action != "no_action":
            continue
        cluster = _analysis_cluster_event_ids(
            decision=decision,
            candidate_event_ids=candidate_event_ids,
        )
        if cluster:
            clusters.append(cluster)
    return _unique_clusters(clusters)


def _analysis_cluster_event_ids(
    *,
    decision: object,
    candidate_event_ids: frozenset[str],
) -> frozenset[str]:
    cluster_event_ids: set[str] = set()
    task_id = getattr(decision, "task_id", None)
    if isinstance(task_id, str) and task_id.strip():
        analysis_outcome_path = getattr(decision, "analysis_outcome_path", None)
        if isinstance(analysis_outcome_path, Path):
            cluster_event_ids.update(
                _task_event_ids_from_jsonl(
                    path=analysis_outcome_path,
                    task_id=task_id,
                )
            )
        journal_path = getattr(decision, "journal_path", None)
        if isinstance(journal_path, Path):
            cluster_event_ids.update(
                _task_event_ids_from_jsonl(
                    path=journal_path,
                    task_id=task_id,
                )
            )
    event_id = getattr(decision, "event_id", None)
    if isinstance(event_id, str) and event_id in candidate_event_ids:
        cluster_event_ids.add(event_id)
    return frozenset(event_id for event_id in cluster_event_ids if event_id in candidate_event_ids)


def _task_event_ids_from_jsonl(
    *,
    path: Path,
    task_id: str,
) -> frozenset[str]:
    if not path.exists():
        return frozenset()
    event_ids: set[str] = set()
    for record in read_jsonl_records(path):
        payload = record.payload
        if _task_id_from_payload(payload) != task_id:
            continue
        raw_event_ids = payload.get("event_ids")
        if not isinstance(raw_event_ids, list) or not all(
            isinstance(event_id, str) and event_id.strip()
            for event_id in raw_event_ids
        ):
            raise LiveCatchupRuntimeRepairError(
                f"event_ids must be a non-empty string list at {path}:{record.line_number}"
            )
        event_ids.update(event_id.strip() for event_id in raw_event_ids)
    return frozenset(event_ids)


def _task_id_from_payload(payload: dict[str, object]) -> str | None:
    raw_task_id = payload.get("task_id")
    if isinstance(raw_task_id, str) and raw_task_id.strip():
        return raw_task_id.strip()
    raw_record_id = payload.get("record_id")
    if isinstance(raw_record_id, str) and raw_record_id.startswith("analysis-outcome:"):
        resolved = raw_record_id.removeprefix("analysis-outcome:").strip()
        return resolved or None
    return None


def _pm_no_action_clusters(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    candidate_event_ids: frozenset[str],
) -> tuple[frozenset[str], ...]:
    if not candidate_event_ids:
        return ()
    clusters: list[frozenset[str]] = []
    report = plan_pm_execution_recovery(layout=layout, target_key=target_key)
    for decision in report.decisions:
        if decision.planned_action != "no_action":
            continue
        cluster = frozenset(
            event_id
            for event_id in decision.source_event_ids
            if event_id in candidate_event_ids
        )
        if cluster:
            clusters.append(cluster)
    return _unique_clusters(clusters)


def _unique_clusters(clusters: list[frozenset[str]]) -> tuple[frozenset[str], ...]:
    unique: dict[tuple[str, ...], frozenset[str]] = {}
    for cluster in clusters:
        key = tuple(sorted(cluster))
        if key:
            unique[key] = cluster
    return tuple(unique[key] for key in sorted(unique))


def _provably_complete_cluster_event_ids(
    *,
    analysis_clusters: tuple[frozenset[str], ...],
    pm_clusters: tuple[frozenset[str], ...],
) -> frozenset[str]:
    if not analysis_clusters or not pm_clusters:
        return frozenset()
    analysis_covered = set().union(*analysis_clusters)
    pm_covered = set().union(*pm_clusters)
    all_clusters = (*analysis_clusters, *pm_clusters)
    excluded_event_ids: set[str] = set()
    for component in _connected_event_components(
        event_ids=analysis_covered | pm_covered,
        clusters=all_clusters,
    ):
        if component <= analysis_covered and component <= pm_covered:
            excluded_event_ids.update(component)
    return frozenset(excluded_event_ids)


def _connected_event_components(
    *,
    event_ids: set[str],
    clusters: tuple[frozenset[str], ...],
) -> tuple[frozenset[str], ...]:
    adjacency = {event_id: set() for event_id in event_ids}
    for cluster in clusters:
        members = tuple(cluster)
        if not members:
            continue
        anchor = members[0]
        adjacency.setdefault(anchor, set())
        for event_id in members[1:]:
            adjacency.setdefault(event_id, set())
            adjacency[anchor].add(event_id)
            adjacency[event_id].add(anchor)
    components: list[frozenset[str]] = []
    seen: set[str] = set()
    for event_id in sorted(adjacency):
        if event_id in seen:
            continue
        stack = [event_id]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.add(current)
            stack.extend(sorted(adjacency[current] - seen))
        components.append(frozenset(component))
    return tuple(components)


def _checkpoint_action_for_event_ids(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    matching_entries: tuple[PersistedReplayEventCheckpoint, ...],
    affected_event_ids: frozenset[str],
) -> RepairAction | None:
    if not affected_event_ids:
        return None
    line_numbers = sorted(
        {
            entry.line_number
            for entry in matching_entries
            if entry.checkpoint.event_id in affected_event_ids
        }
    )
    if not line_numbers:
        return None
    return RepairAction(
        kind="remove_jsonl_records",
        path=_checkpoint_path(layout, target_key),
        record_count=len(line_numbers),
        line_numbers=tuple(line_numbers),
    )


def _checkpoint_states(
    matching: list[PersistedReplayEventCheckpoint],
) -> tuple[_CheckpointEventState, ...]:
    grouped: dict[str, list[PersistedReplayEventCheckpoint]] = {}
    for item in matching:
        grouped.setdefault(item.checkpoint.event_id, []).append(item)
    states: list[_CheckpointEventState] = []
    for event_id, entries in grouped.items():
        latest_entry = entries[-1]
        completed_entries = [
            item for item in entries if item.checkpoint.status == "completed"
        ]
        completed_checkpoint = (
            None if not completed_entries else completed_entries[-1].checkpoint
        )
        states.append(
            _CheckpointEventState(
                event_id=event_id,
                replay_at=latest_entry.checkpoint.replay_at,
                latest=latest_entry.checkpoint,
                completed=completed_checkpoint,
                persisted_entries=tuple(entries),
            )
        )
    return tuple(sorted(states, key=lambda item: (item.replay_at, item.event_id)))


def _context_packet_roots(
    *,
    layout: WorkspaceLayout,
    run_id_prefixes: tuple[str, ...] | None,
) -> tuple[Path, ...]:
    replay_root = layout.runtime_root / "context_packets" / "replay"
    if not replay_root.exists():
        return ()
    if not run_id_prefixes:
        return tuple(
            path.resolve(strict=False)
            for path in sorted(item for item in replay_root.iterdir() if item.is_dir())
        )
    roots: list[Path] = []
    for run_id_prefix in run_id_prefixes:
        roots.extend(
            path.resolve(strict=False)
            for path in sorted(replay_root.glob(f"{run_id_prefix}*"))
            if path.is_dir()
        )
    return tuple(dict.fromkeys(roots))


def _apply_actions(
    *,
    actions: tuple[RepairAction, ...],
    backup_root: Path,
    layout: WorkspaceLayout,
) -> tuple[int, tuple[Path, ...]]:
    removed_count = 0
    deleted_directories: list[Path] = []
    for action in actions:
        if action.kind == "remove_jsonl_records":
            removed_count += rewrite_jsonl_removing_lines(
                path=action.path,
                records=read_jsonl_records(action.path),
                line_numbers=set(action.line_numbers),
                backup_root=backup_root,
                layout=layout,
            )
            continue
        if action.kind == "delete_directory":
            if action.path.exists():
                backup_and_delete_directory(
                    directory=action.path,
                    backup_root=backup_root,
                    layout=layout,
                )
                deleted_directories.append(action.path)
            continue
        raise LiveCatchupRuntimeRepairError(f"Unknown action kind: {action.kind}")
    return removed_count, tuple(deleted_directories)


def _checkpoint_path(layout: WorkspaceLayout, target_key: str) -> Path:
    return (
        layout.runtime_root / "replay_run" / target_key / "events.jsonl"
    ).resolve(strict=False)


def _backup_root(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    created_at: datetime,
) -> Path:
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        layout.runtime_root
        / "repair"
        / "backups"
        / "live_catchup_runtime"
        / target_key
        / stamp
    ).resolve(strict=False)


def _receipt_path(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    created_at: datetime,
) -> Path:
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        layout.runtime_root
        / "live_catchup"
        / target_key
        / f"{stamp}-runtime-repair.json"
    ).resolve(strict=False)


def _write_receipt(
    *,
    layout: WorkspaceLayout,
    report: LiveCatchupRuntimeRepairReport,
) -> None:
    if report.receipt_path is None:
        raise LiveCatchupRuntimeRepairError("repair receipt path is required.")
    report.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    report.receipt_path.write_text(
        json.dumps(
            report.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _timestamp_token(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _relative_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(
            workspace_root.resolve(strict=False)
        ).as_posix()
    except ValueError:
        return path.resolve(strict=False).as_posix()


__all__ = [
    "LiveCatchupRuntimeRepairError",
    "LiveCatchupRuntimeRepairReport",
    "repair_live_catchup_runtime",
    "repair_live_catchup_runtime_from_replay_boundary",
]
