"""Validation-owned contract for closed episode snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from event_trader.contracts import PositionSegment, ViewEpisode, ViewStateChange


class ValidationEpisodeSnapshotError(ValueError):
    """Raised when a closed episode snapshot is malformed."""


@dataclass(frozen=True, slots=True)
class ClosedEpisodeSnapshot:
    """Closed directional episode snapshot derived from canonical state-changes."""

    episode: ViewEpisode
    segments: tuple[PositionSegment, ...]
    state_changes: tuple[ViewStateChange, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.episode, ViewEpisode):
            raise ValidationEpisodeSnapshotError("episode must be a ViewEpisode instance.")
        if self.episode.closed_at is None or self.episode.closed_by_state_change_id is None:
            raise ValidationEpisodeSnapshotError(
                "episode must be closed for reflection snapshot reads."
            )

        if not isinstance(self.segments, tuple) or not self.segments:
            raise ValidationEpisodeSnapshotError(
                "segments must be a non-empty tuple of PositionSegment values."
            )
        if not isinstance(self.state_changes, tuple) or not self.state_changes:
            raise ValidationEpisodeSnapshotError(
                "state_changes must be a non-empty tuple of ViewStateChange values."
            )

        state_change_by_id: dict[str, ViewStateChange] = {}
        previous_state_change_at: datetime | None = None
        for state_change in self.state_changes:
            if not isinstance(state_change, ViewStateChange):
                raise ValidationEpisodeSnapshotError(
                    "state_changes must contain only ViewStateChange instances."
                )
            if state_change.target_key != self.episode.target_key:
                raise ValidationEpisodeSnapshotError(
                    "state_changes must match the snapshot episode target_key."
                )
            if state_change.state_change_id in state_change_by_id:
                raise ValidationEpisodeSnapshotError(
                    "state_changes must not contain duplicate state_change_id values."
                )
            if (
                previous_state_change_at is not None
                and state_change.effective_at <= previous_state_change_at
            ):
                raise ValidationEpisodeSnapshotError(
                    "state_changes must be in strict chronological effective_at order."
                )
            if (
                state_change.effective_at < self.episode.opened_at
                or state_change.effective_at > self.episode.closed_at
            ):
                raise ValidationEpisodeSnapshotError(
                    "state_changes must stay within the closed episode window."
                )
            state_change_by_id[state_change.state_change_id] = state_change
            previous_state_change_at = state_change.effective_at

        required_state_change_ids = {
            self.episode.opened_by_state_change_id,
            self.episode.closed_by_state_change_id,
        }
        previous_segment_opened_at: datetime | None = None
        previous_segment_closed_at: datetime | None = None
        for segment in self.segments:
            if not isinstance(segment, PositionSegment):
                raise ValidationEpisodeSnapshotError(
                    "segments must contain only PositionSegment instances."
                )
            if segment.episode_id != self.episode.episode_id:
                raise ValidationEpisodeSnapshotError(
                    "all segments must belong to the snapshot episode_id."
                )
            if segment.target_key != self.episode.target_key:
                raise ValidationEpisodeSnapshotError(
                    "all segments must match the snapshot episode target_key."
                )
            if segment.closed_at is None or segment.closed_by_state_change_id is None:
                raise ValidationEpisodeSnapshotError(
                    "segments must be closed for reflection snapshot reads."
                )
            if (
                segment.opened_at < self.episode.opened_at
                or segment.closed_at > self.episode.closed_at
            ):
                raise ValidationEpisodeSnapshotError(
                    "segment timestamps must stay inside the closed episode window."
                )
            if (
                previous_segment_opened_at is not None
                and segment.opened_at < previous_segment_opened_at
            ):
                raise ValidationEpisodeSnapshotError(
                    "segments must be in chronological opened_at order."
                )
            if (
                previous_segment_closed_at is not None
                and segment.opened_at < previous_segment_closed_at
            ):
                raise ValidationEpisodeSnapshotError(
                    "segments must not overlap in time."
                )

            opening_state_change = state_change_by_id.get(segment.opened_by_state_change_id)
            if (
                opening_state_change is None
                or opening_state_change.effective_at != segment.opened_at
            ):
                raise ValidationEpisodeSnapshotError(
                    "segment opened_by_state_change_id must resolve to a "
                    "state change at segment.opened_at."
                )
            closing_state_change = state_change_by_id.get(segment.closed_by_state_change_id)
            if (
                closing_state_change is None
                or closing_state_change.effective_at != segment.closed_at
            ):
                raise ValidationEpisodeSnapshotError(
                    "segment closed_by_state_change_id must resolve to a "
                    "state change at segment.closed_at."
                )
            required_state_change_ids.add(segment.opened_by_state_change_id)
            required_state_change_ids.add(segment.closed_by_state_change_id)
            previous_segment_opened_at = segment.opened_at
            previous_segment_closed_at = segment.closed_at

        opened_by_state_change = state_change_by_id.get(
            self.episode.opened_by_state_change_id
        )
        if (
            opened_by_state_change is None
            or opened_by_state_change.effective_at != self.episode.opened_at
        ):
            raise ValidationEpisodeSnapshotError(
                "episode opened_by_state_change_id must resolve to a "
                "state change at episode.opened_at."
            )
        closed_by_state_change = state_change_by_id.get(
            self.episode.closed_by_state_change_id
        )
        if (
            closed_by_state_change is None
            or closed_by_state_change.effective_at != self.episode.closed_at
        ):
            raise ValidationEpisodeSnapshotError(
                "episode closed_by_state_change_id must resolve to a "
                "state change at episode.closed_at."
            )

        missing_state_change_ids = tuple(
            state_change_id
            for state_change_id in required_state_change_ids
            if state_change_id not in state_change_by_id
        )
        if missing_state_change_ids:
            raise ValidationEpisodeSnapshotError(
                "snapshot state_changes are missing required state_change_ids: "
                f"{list(sorted(missing_state_change_ids))!r}."
            )


__all__ = ["ClosedEpisodeSnapshot", "ValidationEpisodeSnapshotError"]


