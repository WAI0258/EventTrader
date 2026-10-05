"""Replay-only event cursor checkpoint records."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from event_trader.contracts._validators import validate_event_id, validate_target_key
from event_trader.storage import WorkspaceLayout

ReplayCheckpointStatus = Literal["started", "completed", "failed"]


class ReplayCheckpointError(ValueError):
    """Raised when replay checkpoint state is malformed."""


@dataclass(frozen=True, slots=True)
class ReplayEventCheckpoint:
    """One append-only replay event pipeline checkpoint entry."""

    run_key: str
    target_key: str
    event_id: str
    replay_at: datetime
    source_kind: str
    source_ref: str
    stage: str
    status: ReplayCheckpointStatus
    error: str | None
    recorded_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "run_key",
            _validate_non_blank(self.run_key, "run_key"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ReplayCheckpointError),
        )
        object.__setattr__(
            self,
            "event_id",
            validate_event_id(self.event_id, error_type=ReplayCheckpointError),
        )
        object.__setattr__(
            self,
            "replay_at",
            _validate_datetime(self.replay_at, "replay_at"),
        )
        object.__setattr__(
            self,
            "source_kind",
            _validate_non_blank(self.source_kind, "source_kind"),
        )
        object.__setattr__(
            self,
            "source_ref",
            _validate_non_blank(self.source_ref, "source_ref"),
        )
        object.__setattr__(
            self,
            "stage",
            _validate_non_blank(self.stage, "stage"),
        )
        if self.status not in {"started", "completed", "failed"}:
            raise ReplayCheckpointError(
                "status must be 'started', 'completed', or 'failed'."
            )
        if self.status == "failed":
            if not isinstance(self.error, str) or not self.error.strip():
                raise ReplayCheckpointError("failed checkpoints must include error.")
        elif self.error is not None:
            raise ReplayCheckpointError(
                "started/completed checkpoints must not include error."
            )
        object.__setattr__(
            self,
            "recorded_at",
            _validate_datetime(self.recorded_at, "recorded_at"),
        )


@dataclass(frozen=True, slots=True)
class PersistedReplayEventCheckpoint:
    """One persisted replay checkpoint entry with its file location."""

    checkpoint: ReplayEventCheckpoint
    path: Path
    line_number: int


def replay_checkpoint_path(layout: WorkspaceLayout, target_key: str) -> Path:
    """Return the replay-only event checkpoint path for one target."""
    if not isinstance(layout, WorkspaceLayout):
        raise ReplayCheckpointError("layout must be a WorkspaceLayout instance.")
    validated_target_key = validate_target_key(target_key, error_type=ReplayCheckpointError)
    return (
        layout.runtime_root / "replay_run" / validated_target_key / "events.jsonl"
    ).resolve(strict=False)


def append_replay_checkpoint(
    layout: WorkspaceLayout,
    checkpoint: ReplayEventCheckpoint,
) -> Path:
    """Append one replay event checkpoint entry durably."""
    path = replay_checkpoint_path(layout, checkpoint.target_key)
    payload = _serialize_checkpoint(checkpoint)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ReplayCheckpointError(
            f"Failed to append replay checkpoint for event_id={checkpoint.event_id}: {exc}"
        ) from exc
    return path


def read_latest_replay_checkpoints(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    run_key: str,
) -> dict[str, ReplayEventCheckpoint]:
    """Read latest checkpoint entry per event_id for one replay run."""
    latest: dict[str, ReplayEventCheckpoint] = {}
    for persisted in read_persisted_replay_checkpoints(layout, target_key=target_key):
        checkpoint = persisted.checkpoint
        if checkpoint.run_key != run_key:
            continue
        latest[checkpoint.event_id] = checkpoint
    return latest


def read_persisted_replay_checkpoints(
    layout: WorkspaceLayout,
    *,
    target_key: str,
) -> tuple[PersistedReplayEventCheckpoint, ...]:
    """Read every persisted replay checkpoint entry for one target."""
    path = replay_checkpoint_path(layout, target_key)
    if not path.exists():
        return ()
    if not path.is_file():
        raise ReplayCheckpointError(f"Replay checkpoint path must be a file: {path}")
    persisted: list[PersistedReplayEventCheckpoint] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            normalized = line.strip()
            if not normalized:
                continue
            try:
                payload = json.loads(normalized)
            except json.JSONDecodeError as exc:
                raise ReplayCheckpointError(
                    f"Replay checkpoint line {line_number} is invalid JSON."
                ) from exc
            persisted.append(
                PersistedReplayEventCheckpoint(
                    checkpoint=_deserialize_checkpoint(payload, line_number=line_number),
                    path=path.resolve(strict=False),
                    line_number=line_number,
                )
            )
    return tuple(persisted)


def _serialize_checkpoint(checkpoint: ReplayEventCheckpoint) -> dict[str, object]:
    return {
        "run_key": checkpoint.run_key,
        "target_key": checkpoint.target_key,
        "event_id": checkpoint.event_id,
        "replay_at": checkpoint.replay_at.isoformat(),
        "source_kind": checkpoint.source_kind,
        "source_ref": checkpoint.source_ref,
        "stage": checkpoint.stage,
        "status": checkpoint.status,
        "error": checkpoint.error,
        "recorded_at": checkpoint.recorded_at.isoformat(),
    }


def _deserialize_checkpoint(
    payload: object,
    *,
    line_number: int,
) -> ReplayEventCheckpoint:
    if not isinstance(payload, dict):
        raise ReplayCheckpointError(
            f"Replay checkpoint line {line_number} must be a JSON object."
        )
    return ReplayEventCheckpoint(
        run_key=_require_string(payload.get("run_key"), "run_key"),
        target_key=_require_string(payload.get("target_key"), "target_key"),
        event_id=_require_string(payload.get("event_id"), "event_id"),
        replay_at=_parse_datetime(payload.get("replay_at"), "replay_at"),
        source_kind=_require_string(payload.get("source_kind"), "source_kind"),
        source_ref=_require_string(payload.get("source_ref"), "source_ref"),
        stage=_require_string(payload.get("stage"), "stage"),
        status=_parse_status(payload.get("status")),
        error=_optional_string(payload.get("error"), "error"),
        recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
    )


def _parse_status(value: object) -> ReplayCheckpointStatus:
    if value == "started":
        return "started"
    if value == "completed":
        return "completed"
    if value == "failed":
        return "failed"
    raise ReplayCheckpointError("status must be 'started', 'completed', or 'failed'.")


def _parse_datetime(value: object, field_name: str) -> datetime:
    raw_value = _require_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayCheckpointError(f"{field_name} must be an ISO8601 timestamp.") from exc
    return _validate_datetime(parsed, field_name)


def _validate_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ReplayCheckpointError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReplayCheckpointError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ReplayCheckpointError(f"{field_name} must be a string.")
    return _validate_non_blank(value, field_name)


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, field_name)


def _validate_non_blank(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ReplayCheckpointError(f"{field_name} must not be blank.")
    return normalized
