"""Canonical append-only state-change storage for validation truth."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from event_trader.contracts._validators import validate_target_key
from event_trader.contracts.view_state_change import (
    ViewStateChange,
    ViewStateChangeContractError,
    ViewStateChangeSourceKind,
)
from event_trader.storage import WorkspaceLayout


class StateChangeStoreError(ValueError):
    """Raised when canonical state-change storage is invalid."""


def classify_view_state_change_source(
    state_change: ViewStateChange,
) -> ViewStateChangeSourceKind:
    """Classify validation state-change source without changing the persisted schema."""
    if not isinstance(state_change, ViewStateChange):
        raise StateChangeStoreError("state_change must be a ViewStateChange instance.")
    return state_change.source_kind


def state_change_shard_path(
    layout: WorkspaceLayout,
    target_key: str,
    effective_at: datetime,
) -> Path:
    """Resolve one canonical monthly state-change shard path."""
    if not isinstance(layout, WorkspaceLayout):
        raise StateChangeStoreError("layout must be a WorkspaceLayout instance.")
    validated_target_key = validate_target_key(
        target_key,
        error_type=StateChangeStoreError,
    )
    if not isinstance(effective_at, datetime):
        raise StateChangeStoreError("effective_at must be a datetime instance.")
    shard_name = effective_at.strftime("%Y-%m")
    return (
        layout.runtime_root
        / "validation"
        / "state-changes"
        / validated_target_key
        / f"{shard_name}.jsonl"
    ).resolve(strict=False)


def append_state_change(layout: WorkspaceLayout, state_change: ViewStateChange) -> Path:
    """Append one ViewStateChange to its canonical monthly state-change shard."""
    if not isinstance(state_change, ViewStateChange):
        raise StateChangeStoreError("state_change must be a ViewStateChange instance.")

    shard_path = state_change_shard_path(
        layout,
        state_change.target_key,
        state_change.effective_at,
    )
    existing_state_changes = read_state_changes(layout, state_change.target_key)
    if (
        existing_state_changes
        and state_change.effective_at <= existing_state_changes[-1].effective_at
    ):
        raise StateChangeStoreError(
            "state-change effective_at must be strictly increasing per target."
        )

    payload_line = (
        json.dumps(
            _serialize_view_state_change(state_change),
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with shard_path.open("ab") as handle:
            handle.write(payload_line)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StateChangeStoreError(
            "Failed to append state-change to canonical shard "
            f"{shard_path}: {exc}"
        ) from exc

    return shard_path


def append_state_change_once(
    layout: WorkspaceLayout,
    state_change: ViewStateChange,
) -> tuple[Path, bool]:
    """Append a state-change unless the exact state_change_id is already persisted."""
    if not isinstance(state_change, ViewStateChange):
        raise StateChangeStoreError("state_change must be a ViewStateChange instance.")

    existing_state_changes = read_state_changes(layout, state_change.target_key)
    for existing_state_change in existing_state_changes:
        if existing_state_change.state_change_id != state_change.state_change_id:
            continue
        if existing_state_change != state_change:
            raise StateChangeStoreError(
                "state-change idempotency conflict: existing payload differs for "
                f"state_change_id={state_change.state_change_id!r}."
            )
        return (
            state_change_shard_path(
                layout,
                state_change.target_key,
                state_change.effective_at,
            ),
            False,
        )

    return append_state_change(layout, state_change), True


def read_state_changes(
    layout: WorkspaceLayout,
    target_key: str,
    *,
    source_kind: ViewStateChangeSourceKind | None = None,
) -> tuple[ViewStateChange, ...]:
    """Read one target's canonical state-change stream across monthly shards.

    When ``source_kind`` is specified, non-matching historical records are left
    outside the requested projection and are not deserialized as ViewStateChange.
    """
    if not isinstance(layout, WorkspaceLayout):
        raise StateChangeStoreError("layout must be a WorkspaceLayout instance.")
    validated_target_key = validate_target_key(
        target_key,
        error_type=StateChangeStoreError,
    )
    target_root = (
        layout.runtime_root / "validation" / "state-changes" / validated_target_key
    ).resolve(strict=False)
    if not target_root.exists():
        return ()
    if not target_root.is_dir():
        raise StateChangeStoreError(
            f"Expected state-change target root directory but found a file at: {target_root}"
        )

    shard_paths = tuple(sorted(target_root.glob("*.jsonl")))
    if not shard_paths:
        return ()

    state_changes: list[ViewStateChange] = []
    previous_effective_at: datetime | None = None
    for shard_path in shard_paths:
        if not shard_path.is_file():
            raise StateChangeStoreError(
                f"Expected state-change shard file but found a directory at: {shard_path}"
            )
        try:
            lines = shard_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise StateChangeStoreError(
                f"Failed to read state-change shard {shard_path}: {exc}"
            ) from exc

        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                raise StateChangeStoreError(
                    "state-change shard contains a blank line at "
                    f"{shard_path}:{line_number}."
                )
            if source_kind is not None:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StateChangeStoreError(
                        "state-change shard contains invalid JSON at "
                        f"{shard_path}:{line_number}: {exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise StateChangeStoreError(
                        "state-change shard lines must decode to JSON objects at "
                        f"{shard_path}:{line_number}."
                    )
                if payload.get("source_kind") != source_kind:
                    continue
            state_change = _deserialize_view_state_change(
                line,
                shard_path=shard_path,
                line_number=line_number,
            )
            if state_change.target_key != validated_target_key:
                raise StateChangeStoreError(
                    "state-change shard contains mixed target_key values at "
                    f"{shard_path}:{line_number}."
                )
            if (
                previous_effective_at is not None
                and state_change.effective_at <= previous_effective_at
            ):
                raise StateChangeStoreError(
                    "state-change effective_at must be strictly increasing at "
                    f"{shard_path}:{line_number}."
                )
            state_changes.append(state_change)
            previous_effective_at = state_change.effective_at

    return tuple(state_changes)


def read_pm_execution_sidecar_state_changes(
    layout: WorkspaceLayout,
    target_key: str,
) -> tuple[ViewStateChange, ...]:
    """Read the PM-execution projection of a target's state-change stream."""

    return read_state_changes(
        layout,
        target_key,
        source_kind="pm_execution_sidecar",
    )


def _serialize_view_state_change(state_change: ViewStateChange) -> dict[str, object]:
    return {
        "state_change_id": state_change.state_change_id,
        "target_key": state_change.target_key,
        "state": state_change.state,
        "direction": state_change.direction,
        "conviction": state_change.conviction,
        "target_weight": state_change.target_weight,
        "effective_at": state_change.effective_at.isoformat(),
        "source_event_ids": list(state_change.source_event_ids),
        "rationale_md": state_change.rationale_md,
        "used_lesson_ids": list(state_change.used_lesson_ids),
        "source_kind": state_change.source_kind,
        "pm_decision_id": state_change.pm_decision_id,
        "execution_record_id": state_change.execution_record_id,
        "predecessor_instrument_basis": state_change.predecessor_instrument_basis,
        "instrument_basis": state_change.instrument_basis,
    }


def _deserialize_view_state_change(
    raw_line: str,
    *,
    shard_path: Path,
    line_number: int,
) -> ViewStateChange:
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise StateChangeStoreError(
            "state-change shard contains invalid JSON at "
            f"{shard_path}:{line_number}: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise StateChangeStoreError(
            "state-change shard lines must decode to JSON objects at "
            f"{shard_path}:{line_number}."
        )
    try:
        return ViewStateChange(
            state_change_id=payload["state_change_id"],
            target_key=payload["target_key"],
            state=payload["state"],
            direction=payload["direction"],
            conviction=payload["conviction"],
            target_weight=payload["target_weight"],
            effective_at=_parse_iso_timestamp(
                payload["effective_at"],
                field_name="effective_at",
                shard_path=shard_path,
                line_number=line_number,
            ),
            source_event_ids=_parse_source_event_ids(
                payload["source_event_ids"],
                shard_path=shard_path,
                line_number=line_number,
            ),
            rationale_md=payload["rationale_md"],
            used_lesson_ids=_parse_used_lesson_ids(
                payload.get("used_lesson_ids", []),
                shard_path=shard_path,
                line_number=line_number,
            ),
            source_kind=payload["source_kind"],
            pm_decision_id=payload.get("pm_decision_id"),
            execution_record_id=payload.get("execution_record_id"),
            predecessor_instrument_basis=payload.get("predecessor_instrument_basis"),
            instrument_basis=payload.get("instrument_basis"),
        )
    except KeyError as exc:
        raise StateChangeStoreError(
            "Missing required state-change field "
            f"{exc.args[0]!r} at {shard_path}:{line_number}."
        ) from exc
    except (ViewStateChangeContractError, TypeError, ValueError) as exc:
        raise StateChangeStoreError(
            "Invalid state-change payload encountered at "
            f"{shard_path}:{line_number}: {exc}"
        ) from exc


def _parse_iso_timestamp(
    value: object,
    *,
    field_name: str,
    shard_path: Path,
    line_number: int,
) -> datetime:
    if not isinstance(value, str):
        raise StateChangeStoreError(
            f"{field_name} must be an ISO8601 string at {shard_path}:{line_number}."
        )
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise StateChangeStoreError(
            f"{field_name} must be a valid ISO8601 string at "
            f"{shard_path}:{line_number}: {exc}"
        ) from exc


def _parse_source_event_ids(
    value: object,
    *,
    shard_path: Path,
    line_number: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StateChangeStoreError(
            "source_event_ids must be a JSON array of strings at "
            f"{shard_path}:{line_number}."
        )
    return tuple(value)


def _parse_used_lesson_ids(
    value: object,
    *,
    shard_path: Path,
    line_number: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StateChangeStoreError(
            "used_lesson_ids must be a JSON array of strings at "
            f"{shard_path}:{line_number}."
        )
    return tuple(value)


__all__ = [
    "StateChangeStoreError",
    "append_state_change",
    "append_state_change_once",
    "read_state_changes",
    "state_change_shard_path",
]


