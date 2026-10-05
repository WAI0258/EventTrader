"""Durable live source batch checkpoints for observability and bookkeeping only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.storage import WorkspaceLayout

LiveSourceChannel = Literal["web_search", "market_news"]
_LIVE_SOURCE_CHANNELS = frozenset({"web_search", "market_news"})
_CHECKPOINT_FIELDS = frozenset(
    {
        "target_key",
        "channel",
        "last_successful_batch_end",
        "successful_batch_count",
        "recorded_at",
    }
)


class LiveSourceCheckpointError(ValueError):
    """Raised when live source checkpoint state is malformed."""


@dataclass(frozen=True, slots=True)
class LiveSourceCheckpoint:
    """Latest successful live source batch record for one target/channel.

    These records are durable bookkeeping. `live_runtime` must not treat them as
    startup cursors for historical replay.
    """

    target_key: str
    channel: LiveSourceChannel
    last_successful_batch_end: datetime
    successful_batch_count: int
    recorded_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=LiveSourceCheckpointError),
        )
        if self.channel not in _LIVE_SOURCE_CHANNELS:
            allowed = ", ".join(sorted(_LIVE_SOURCE_CHANNELS))
            raise LiveSourceCheckpointError(f"channel must be one of: {allowed}.")
        object.__setattr__(
            self,
            "last_successful_batch_end",
            validate_timestamp(
                self.last_successful_batch_end,
                field_name="last_successful_batch_end",
                error_type=LiveSourceCheckpointError,
            ).astimezone(UTC),
        )
        object.__setattr__(
            self,
            "recorded_at",
            validate_timestamp(
                self.recorded_at,
                field_name="recorded_at",
                error_type=LiveSourceCheckpointError,
            ).astimezone(UTC),
        )
        if isinstance(self.successful_batch_count, bool) or not isinstance(
            self.successful_batch_count,
            int,
        ):
            raise LiveSourceCheckpointError(
                "successful_batch_count must be a positive integer."
            )
        if self.successful_batch_count <= 0:
            raise LiveSourceCheckpointError(
                "successful_batch_count must be greater than zero."
            )


def live_source_checkpoint_path(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    channel: LiveSourceChannel,
) -> Path:
    """Return the durable checkpoint path for one live source channel."""
    if not isinstance(layout, WorkspaceLayout):
        raise LiveSourceCheckpointError("layout must be a WorkspaceLayout instance.")
    validated_target_key = validate_target_key(
        target_key,
        error_type=LiveSourceCheckpointError,
    )
    if channel not in _LIVE_SOURCE_CHANNELS:
        allowed = ", ".join(sorted(_LIVE_SOURCE_CHANNELS))
        raise LiveSourceCheckpointError(f"channel must be one of: {allowed}.")
    return (
        layout.runtime_root
        / "live_source"
        / channel
        / f"{validated_target_key}.json"
    ).resolve(strict=False)


def read_live_source_checkpoint(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    channel: LiveSourceChannel,
) -> LiveSourceCheckpoint | None:
    """Read the latest recorded live source batch checkpoint, if it exists."""
    validated_target_key = validate_target_key(
        target_key,
        error_type=LiveSourceCheckpointError,
    )
    if channel not in _LIVE_SOURCE_CHANNELS:
        allowed = ", ".join(sorted(_LIVE_SOURCE_CHANNELS))
        raise LiveSourceCheckpointError(f"channel must be one of: {allowed}.")
    path = live_source_checkpoint_path(
        layout,
        target_key=validated_target_key,
        channel=channel,
    )
    if not path.exists():
        return None
    if not path.is_file():
        raise LiveSourceCheckpointError(f"Live source checkpoint path must be a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LiveSourceCheckpointError(
            f"Live source checkpoint contains invalid JSON: {path}"
        ) from exc
    checkpoint = _deserialize_checkpoint(payload)
    if checkpoint.target_key != validated_target_key:
        raise LiveSourceCheckpointError(
            "Live source checkpoint target_key does not match its path: "
            f"path_target_key={validated_target_key!r} "
            f"payload_target_key={checkpoint.target_key!r}."
        )
    if checkpoint.channel != channel:
        raise LiveSourceCheckpointError(
            "Live source checkpoint channel does not match its path."
        )
    return checkpoint


def write_live_source_checkpoint(
    layout: WorkspaceLayout,
    checkpoint: LiveSourceCheckpoint,
) -> Path:
    """Write the latest recorded live source checkpoint after a successful batch."""
    if not isinstance(layout, WorkspaceLayout):
        raise LiveSourceCheckpointError("layout must be a WorkspaceLayout instance.")
    if not isinstance(checkpoint, LiveSourceCheckpoint):
        raise LiveSourceCheckpointError(
            "checkpoint must be a LiveSourceCheckpoint instance."
        )
    path = live_source_checkpoint_path(
        layout,
        target_key=checkpoint.target_key,
        channel=checkpoint.channel,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_serialize_checkpoint(checkpoint), ensure_ascii=False, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def advance_live_source_checkpoint(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    channel: LiveSourceChannel,
    last_successful_batch_end: datetime,
    recorded_at: datetime | None = None,
) -> Path:
    """Record one successful live source batch for bookkeeping after completion."""
    if not isinstance(layout, WorkspaceLayout):
        raise LiveSourceCheckpointError("layout must be a WorkspaceLayout instance.")
    validated_target_key = validate_target_key(
        target_key,
        error_type=LiveSourceCheckpointError,
    )
    if channel not in _LIVE_SOURCE_CHANNELS:
        allowed = ", ".join(sorted(_LIVE_SOURCE_CHANNELS))
        raise LiveSourceCheckpointError(f"channel must be one of: {allowed}.")
    validated_batch_end = validate_timestamp(
        last_successful_batch_end,
        field_name="last_successful_batch_end",
        error_type=LiveSourceCheckpointError,
    ).astimezone(UTC)
    existing = read_live_source_checkpoint(
        layout,
        target_key=validated_target_key,
        channel=channel,
    )
    if (
        existing is not None
        and validated_batch_end <= existing.last_successful_batch_end
    ):
        raise LiveSourceCheckpointError(
            "last_successful_batch_end must strictly advance the recorded live "
            "source batch end."
        )
    checkpoint = LiveSourceCheckpoint(
        target_key=validated_target_key,
        channel=channel,
        last_successful_batch_end=validated_batch_end,
        successful_batch_count=(
            1 if existing is None else existing.successful_batch_count + 1
        ),
        recorded_at=(
            live_source_recorded_at()
            if recorded_at is None
            else validate_timestamp(
                recorded_at,
                field_name="recorded_at",
                error_type=LiveSourceCheckpointError,
            ).astimezone(UTC)
        ),
    )
    path = write_live_source_checkpoint(layout, checkpoint)
    expected = live_source_checkpoint_path(
        layout,
        target_key=validated_target_key,
        channel=channel,
    )
    if path != expected:
        raise LiveSourceCheckpointError(
            "live source checkpoint writer returned an unexpected path."
        )
    return path


def _serialize_checkpoint(checkpoint: LiveSourceCheckpoint) -> dict[str, object]:
    return {
        "target_key": checkpoint.target_key,
        "channel": checkpoint.channel,
        "last_successful_batch_end": checkpoint.last_successful_batch_end.isoformat(),
        "successful_batch_count": checkpoint.successful_batch_count,
        "recorded_at": checkpoint.recorded_at.isoformat(),
    }


def _deserialize_checkpoint(payload: object) -> LiveSourceCheckpoint:
    if not isinstance(payload, dict):
        raise LiveSourceCheckpointError("Live source checkpoint must be a JSON object.")
    missing_fields = sorted(_CHECKPOINT_FIELDS - set(payload))
    unexpected_fields = sorted(set(payload) - _CHECKPOINT_FIELDS)
    if missing_fields or unexpected_fields:
        problems: list[str] = []
        if missing_fields:
            problems.append(f"missing field(s): {', '.join(missing_fields)}")
        if unexpected_fields:
            problems.append(f"unexpected field(s): {', '.join(unexpected_fields)}")
        raise LiveSourceCheckpointError(
            "Live source checkpoint has the wrong payload shape; "
            f"{'; '.join(problems)}."
        )
    return LiveSourceCheckpoint(
        target_key=_required_string(payload, "target_key"),
        channel=_required_channel(payload, "channel"),
        last_successful_batch_end=_required_datetime(
            payload,
            "last_successful_batch_end",
        ),
        successful_batch_count=_required_int(payload, "successful_batch_count"),
        recorded_at=_required_datetime(payload, "recorded_at"),
    )


def _required_string(payload: dict[object, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise LiveSourceCheckpointError(f"{field_name} must be a non-empty string.")
    return value


def _required_channel(
    payload: dict[object, object],
    field_name: str,
) -> LiveSourceChannel:
    value = _required_string(payload, field_name)
    if value not in _LIVE_SOURCE_CHANNELS:
        allowed = ", ".join(sorted(_LIVE_SOURCE_CHANNELS))
        raise LiveSourceCheckpointError(f"channel must be one of: {allowed}.")
    return cast(LiveSourceChannel, value)


def _required_datetime(payload: dict[object, object], field_name: str) -> datetime:
    value = _required_string(payload, field_name)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LiveSourceCheckpointError(f"{field_name} must be an ISO8601 timestamp.") from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=LiveSourceCheckpointError,
    ).astimezone(UTC)


def _required_int(payload: dict[object, object], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveSourceCheckpointError(f"{field_name} must be an integer.")
    return value


def live_source_recorded_at() -> datetime:
    """Return the current UTC timestamp for checkpoint writes."""
    return datetime.now(UTC)


__all__ = [
    "LiveSourceCheckpoint",
    "LiveSourceCheckpointError",
    "advance_live_source_checkpoint",
    "live_source_checkpoint_path",
    "live_source_recorded_at",
    "read_live_source_checkpoint",
    "write_live_source_checkpoint",
]
