"""Replay runtime repair boundary discovery and application."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from event_trader.contracts._validators import validate_event_id, validate_target_key
from event_trader.operator.repair.artifacts import (
    backup_and_delete_directory,
    read_jsonl_records,
    rewrite_jsonl_removing_lines,
)
from event_trader.operator.repair.contracts import (
    OperatorRepairError,
    RepairAction,
    RepairBlocker,
    RepairPlan,
    RepairReceipt,
)
from event_trader.operator.repair.lifecycle_planner import (
    LifecycleRepairBoundary,
    build_lifecycle_repair_plan,
)
from event_trader.storage import WorkspaceLayout


def replay_run_key(
    *,
    target_key: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    return (
        f"{target_key}:"
        f"{window_start.astimezone(UTC).isoformat()}:"
        f"{window_end.astimezone(UTC).isoformat()}"
    )


def default_replay_run_id(
    *,
    target_key: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    return (
        f"{target_key}-"
        f"{window_start.astimezone(UTC).date().isoformat()}-"
        f"{window_end.astimezone(UTC).date().isoformat()}"
    )


def plan_replay_failed_event_repair(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    run_key: str,
    run_id: str,
    event_id: str | None = None,
) -> RepairPlan:
    if not isinstance(layout, WorkspaceLayout):
        raise OperatorRepairError("layout must be a WorkspaceLayout instance.")
    normalized_target_key = validate_target_key(
        target_key,
        error_type=OperatorRepairError,
    )
    selected_event_id = (
        None
        if event_id is None
        else validate_event_id(event_id, error_type=OperatorRepairError)
    )
    checkpoint_path = _checkpoint_path(layout, normalized_target_key)
    checkpoint_records = read_jsonl_records(checkpoint_path)
    selected_event_id = _select_repairable_event_id(
        records=checkpoint_records,
        target_key=normalized_target_key,
        run_key=run_key,
        event_id=selected_event_id,
    )
    if selected_event_id is None:
        raise OperatorRepairError(
            "No unfinished replay checkpoint was found to repair."
        )

    checkpoint_blockers = _checkpoint_blockers(
        records=checkpoint_records,
        target_key=normalized_target_key,
        run_key=run_key,
        event_id=selected_event_id,
    )
    checkpoint_action = _checkpoint_action(
        records=checkpoint_records,
        target_key=normalized_target_key,
        run_key=run_key,
        event_id=selected_event_id,
    )
    planner_result = build_lifecycle_repair_plan(
        layout=layout,
        boundary=LifecycleRepairBoundary(
            scope="replay",
            target_key=normalized_target_key,
            boundary_kind="replay_event",
            event_id=selected_event_id,
            run_key=run_key,
            run_id=run_id,
            repair_replay_at=_repair_replay_at(
                records=checkpoint_records,
                target_key=normalized_target_key,
                run_key=run_key,
                event_id=selected_event_id,
            ),
            preserved_event_ids=_preserved_event_ids(
                records=checkpoint_records,
                target_key=normalized_target_key,
                run_key=run_key,
                selected_event_id=selected_event_id,
            ),
            affected_event_ids=(selected_event_id,),
            initial_actions=(() if checkpoint_action is None else (checkpoint_action,)),
            replay_context_roots=(
                (
                    layout.runtime_root
                    / "context_packets"
                    / "replay"
                    / run_id
                ).resolve(strict=False),
            ),
        ),
    )
    planner_plan = planner_result.plan
    blockers = checkpoint_blockers + planner_plan.blockers
    return RepairPlan(
        scope=planner_plan.scope,
        status="blocked" if blockers else planner_plan.status,
        target_key=planner_plan.target_key,
        event_id=planner_plan.event_id,
        run_key=planner_plan.run_key,
        run_id=planner_plan.run_id,
        actions=planner_plan.actions,
        blockers=blockers,
        boundary_summary=planner_plan.boundary_summary,
        touched_stage_counts=planner_plan.touched_stage_counts,
        blocked_artifacts=planner_plan.blocked_artifacts,
        rebuildable_artifacts=planner_plan.rebuildable_artifacts,
    )


def apply_replay_repair_plan(
    *,
    layout: WorkspaceLayout,
    plan: RepairPlan,
    applied_at: datetime | None = None,
) -> RepairReceipt:
    if plan.scope != "replay":
        raise OperatorRepairError("apply_replay_repair_plan requires a replay repair plan.")
    if plan.event_id is None or plan.run_key is None:
        raise OperatorRepairError("Replay repair plans require event_id and run_key.")
    if plan.blockers:
        reasons = "; ".join(blocker.reason for blocker in plan.blockers)
        raise OperatorRepairError(
            "Replay repair refused to mutate because blockers were found: " + reasons
        )
    now = (applied_at or datetime.now(tz=UTC)).astimezone(UTC)
    backup_root = _backup_root(
        layout=layout,
        target_key=plan.target_key,
        event_id=plan.event_id,
        created_at=now,
    )
    backup_root.mkdir(parents=True, exist_ok=False)

    for action in plan.actions:
        if action.kind == "remove_jsonl_records":
            rewrite_jsonl_removing_lines(
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
            continue
        raise OperatorRepairError(f"Unknown repair action kind: {action.kind}")

    applied_plan = RepairPlan(
        scope=plan.scope,
        status="applied",
        target_key=plan.target_key,
        event_id=plan.event_id,
        run_key=plan.run_key,
        run_id=plan.run_id,
        actions=plan.actions,
        blockers=(),
        boundary_summary=plan.boundary_summary,
        touched_stage_counts=plan.touched_stage_counts,
        blocked_artifacts=plan.blocked_artifacts,
        rebuildable_artifacts=plan.rebuildable_artifacts,
    )
    receipt_path = _write_replay_repair_receipt(
        layout=layout,
        plan=applied_plan,
        backup_root=backup_root,
        applied_at=now,
    )
    return RepairReceipt(
        plan=applied_plan,
        applied_at=now,
        backup_root=backup_root,
        receipt_path=receipt_path,
    )


def _checkpoint_path(layout: WorkspaceLayout, target_key: str) -> Path:
    return (
        layout.runtime_root / "replay_run" / target_key / "events.jsonl"
    ).resolve(strict=False)


def _select_repairable_event_id(
    *,
    records: tuple,
    target_key: str,
    run_key: str,
    event_id: str | None,
) -> str | None:
    if event_id is not None:
        return event_id
    selected: str | None = None
    for record in records:
        payload = record.payload
        if (
            payload.get("target_key") == target_key
            and payload.get("run_key") == run_key
            and payload.get("status") in {"started", "failed"}
        ):
            raw_event_id = payload.get("event_id")
            if isinstance(raw_event_id, str):
                selected = raw_event_id
    return selected


def _checkpoint_blockers(
    *,
    records: tuple,
    target_key: str,
    run_key: str,
    event_id: str,
) -> tuple[RepairBlocker, ...]:
    blockers: list[RepairBlocker] = []
    found_repairable = False
    for record in records:
        payload = record.payload
        if (
            payload.get("target_key") != target_key
            or payload.get("run_key") != run_key
            or payload.get("event_id") != event_id
        ):
            continue
        if payload.get("status") == "completed":
            blockers.append(
                RepairBlocker(
                    reason=f"event_id={event_id} already has a completed checkpoint",
                    path=record.path,
                )
            )
        if payload.get("status") in {"started", "failed"}:
            found_repairable = True
    if not found_repairable:
        blockers.append(
            RepairBlocker(
                reason=f"event_id={event_id} has no unfinished checkpoint for this run",
                path=records[0].path if records else None,
            )
        )
    return tuple(blockers)


def _checkpoint_action(
    *,
    records: tuple,
    target_key: str,
    run_key: str,
    event_id: str,
) -> RepairAction | None:
    line_numbers = tuple(
        record.line_number
        for record in records
        if _is_repairable_checkpoint_record(
            payload=record.payload,
            target_key=target_key,
            run_key=run_key,
            event_id=event_id,
        )
    )
    if not line_numbers:
        return None
    return RepairAction(
        kind="remove_jsonl_records",
        path=records[0].path,
        record_count=len(line_numbers),
        line_numbers=line_numbers,
    )


def _is_repairable_checkpoint_record(
    *,
    payload: dict[str, object],
    target_key: str,
    run_key: str,
    event_id: str,
) -> bool:
    return (
        payload.get("target_key") == target_key
        and payload.get("run_key") == run_key
        and payload.get("event_id") == event_id
        and payload.get("status") in {"started", "failed"}
    )


def _preserved_event_ids(
    *,
    records: tuple,
    target_key: str,
    run_key: str,
    selected_event_id: str,
) -> tuple[str, ...]:
    preserved = {
        payload_event_id
        for record in records
        for payload_event_id in (_event_id(record.payload),)
        if payload_event_id is not None
        and payload_event_id != selected_event_id
        and record.payload.get("target_key") == target_key
        and record.payload.get("run_key") == run_key
        and record.payload.get("status") == "completed"
    }
    return tuple(sorted(preserved))


def _repair_replay_at(
    *,
    records: tuple,
    target_key: str,
    run_key: str,
    event_id: str,
) -> datetime | None:
    for record in records:
        payload = record.payload
        if (
            payload.get("target_key") != target_key
            or payload.get("run_key") != run_key
            or payload.get("event_id") != event_id
        ):
            continue
        raw_value = payload.get("replay_at")
        if not isinstance(raw_value, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        return parsed.astimezone(UTC)
    return None


def _event_id(payload: dict[str, object]) -> str | None:
    raw_value = payload.get("event_id")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    return raw_value.strip()


def _backup_root(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    event_id: str,
    created_at: datetime,
) -> Path:
    stamp = created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        layout.runtime_root
        / "repair"
        / "backups"
        / "replay"
        / target_key
        / f"{stamp}-{event_id}"
    ).resolve(strict=False)


def _write_replay_repair_receipt(
    *,
    layout: WorkspaceLayout,
    plan: RepairPlan,
    backup_root: Path,
    applied_at: datetime,
) -> Path:
    if plan.event_id is None:
        raise OperatorRepairError("Replay repair receipt requires event_id.")
    stamp = applied_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = (
        layout.runtime_root
        / "repair"
        / "replay"
        / plan.target_key
        / f"{stamp}-{plan.event_id}.json"
    ).resolve(strict=False)
    receipt = RepairReceipt(
        plan=plan,
        applied_at=applied_at,
        backup_root=backup_root,
        receipt_path=path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            receipt.to_json_payload(workspace_root=layout.root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
