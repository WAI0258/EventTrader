"""Build validation view episodes and exposure segments from state-changes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from event_trader.contracts.view_state_change import (
    PositionSegment,
    ReflectionTrigger,
    SegmentCloseReason,
    ViewCloseReason,
    ViewEpisode,
    ViewStateChange,
)

from .state_machine import TransitionResult, ValidationStateMachineError, resolve_transition


class ValidationEpisodeBuilderError(ValueError):
    """Raised when state-changes cannot produce valid episodes or segments."""


@dataclass(frozen=True, slots=True)
class EpisodeBuildResult:
    """Built validation artifacts for one target state-change history."""

    completed_episodes: tuple[ViewEpisode, ...]
    open_episode: ViewEpisode | None
    completed_segments: tuple[PositionSegment, ...]
    open_segment: PositionSegment | None
    reflection_triggers: tuple[ReflectionTrigger, ...]
    transitions: tuple[TransitionResult, ...]


def build_episodes(state_changes: Sequence[ViewStateChange]) -> EpisodeBuildResult:
    """Build deterministic episodes/segments/triggers from one target's state-changes."""
    if not state_changes:
        return EpisodeBuildResult(
            completed_episodes=(),
            open_episode=None,
            completed_segments=(),
            open_segment=None,
            reflection_triggers=(),
            transitions=(),
        )

    completed_episodes: list[ViewEpisode] = []
    completed_segments: list[PositionSegment] = []
    reflection_triggers: list[ReflectionTrigger] = []
    transitions: list[TransitionResult] = []
    open_episode: ViewEpisode | None = None
    open_segment: PositionSegment | None = None
    episode_counter = 0
    segment_counter = 0
    previous_state_change: ViewStateChange | None = None
    previous_effective_at: datetime | None = None
    target_key = state_changes[0].target_key

    for state_change in state_changes:
        if state_change.target_key != target_key:
            raise ValidationEpisodeBuilderError(
                "All state_changes in one stream must share the same target_key."
            )
        if (
            previous_effective_at is not None
            and state_change.effective_at <= previous_effective_at
        ):
            raise ValidationEpisodeBuilderError(
                "state_change effective_at must be strictly increasing per target."
            )

        try:
            transition = resolve_transition(
                previous_state_change=previous_state_change,
                next_state_change=state_change,
            )
        except ValidationStateMachineError as exc:
            raise ValidationEpisodeBuilderError(str(exc)) from exc
        transitions.append(transition)

        closed_episode_for_trigger: ViewEpisode | None = None
        for action in transition.actions:
            if action == "record_flat":
                if state_change.state != "flat":
                    raise ValidationEpisodeBuilderError("record_flat requires flat state.")
                if open_episode is not None or open_segment is not None:
                    raise ValidationEpisodeBuilderError(
                        "record_flat cannot run while an episode or segment is open."
                    )
                continue

            if action == "open_view":
                if state_change.direction == "flat":
                    raise ValidationEpisodeBuilderError(
                        "open_view requires a directional state_change."
                    )
                if open_episode is not None:
                    raise ValidationEpisodeBuilderError(
                        "open_view cannot run with an open episode."
                    )
                episode_counter += 1
                open_episode = ViewEpisode(
                    episode_id=f"{target_key}:episode:{episode_counter}",
                    target_key=target_key,
                    direction=state_change.direction,
                    opened_at=state_change.effective_at,
                    opened_by_state_change_id=state_change.state_change_id,
                    opened_by_event_ids=state_change.source_event_ids,
                )
                continue

            if action == "open_segment":
                if open_episode is None:
                    raise ValidationEpisodeBuilderError("open_segment requires an open episode.")
                if open_segment is not None:
                    raise ValidationEpisodeBuilderError(
                        "open_segment cannot run with open segment."
                    )
                segment_counter += 1
                open_segment = PositionSegment(
                    segment_id=f"{target_key}:segment:{segment_counter}",
                    episode_id=open_episode.episode_id,
                    target_key=target_key,
                    target_weight=state_change.target_weight,
                    opened_at=state_change.effective_at,
                    opened_by_state_change_id=state_change.state_change_id,
                )
                continue

            if action == "noop":
                continue

            if action == "resize_segment":
                if open_episode is None or open_segment is None:
                    raise ValidationEpisodeBuilderError(
                        "resize_segment requires open episode/segment."
                    )
                completed_segments.append(
                    _close_segment(
                        segment=open_segment,
                        closed_at=state_change.effective_at,
                        closed_by_state_change_id=state_change.state_change_id,
                        close_reason="weight_changed",
                    )
                )
                segment_counter += 1
                open_segment = PositionSegment(
                    segment_id=f"{target_key}:segment:{segment_counter}",
                    episode_id=open_episode.episode_id,
                    target_key=target_key,
                    target_weight=state_change.target_weight,
                    opened_at=state_change.effective_at,
                    opened_by_state_change_id=state_change.state_change_id,
                )
                continue

            if action == "close_view":
                if open_episode is None or open_segment is None:
                    raise ValidationEpisodeBuilderError("close_view requires open episode/segment.")
                completed_segments.append(
                    _close_segment(
                        segment=open_segment,
                        closed_at=state_change.effective_at,
                        closed_by_state_change_id=state_change.state_change_id,
                        close_reason="view_closed",
                    )
                )
                closed_episode = _close_episode(
                    episode=open_episode,
                    closed_at=state_change.effective_at,
                    closed_by_state_change_id=state_change.state_change_id,
                    closed_by_event_ids=state_change.source_event_ids,
                    close_reason="state_changed_to_flat",
                )
                completed_episodes.append(closed_episode)
                closed_episode_for_trigger = closed_episode
                open_episode = None
                open_segment = None
                continue

            if action == "reverse_view":
                if open_episode is None or open_segment is None:
                    raise ValidationEpisodeBuilderError(
                        "reverse_view requires open episode/segment."
                    )
                if state_change.direction == "flat":
                    raise ValidationEpisodeBuilderError(
                        "reverse_view requires directional state_change."
                    )
                completed_segments.append(
                    _close_segment(
                        segment=open_segment,
                        closed_at=state_change.effective_at,
                        closed_by_state_change_id=state_change.state_change_id,
                        close_reason="view_closed",
                    )
                )
                closed_episode = _close_episode(
                    episode=open_episode,
                    closed_at=state_change.effective_at,
                    closed_by_state_change_id=state_change.state_change_id,
                    closed_by_event_ids=state_change.source_event_ids,
                    close_reason="state_changed_to_opposite_direction",
                )
                completed_episodes.append(closed_episode)
                closed_episode_for_trigger = closed_episode

                episode_counter += 1
                open_episode = ViewEpisode(
                    episode_id=f"{target_key}:episode:{episode_counter}",
                    target_key=target_key,
                    direction=state_change.direction,
                    opened_at=state_change.effective_at,
                    opened_by_state_change_id=state_change.state_change_id,
                    opened_by_event_ids=state_change.source_event_ids,
                )
                segment_counter += 1
                open_segment = PositionSegment(
                    segment_id=f"{target_key}:segment:{segment_counter}",
                    episode_id=open_episode.episode_id,
                    target_key=target_key,
                    target_weight=state_change.target_weight,
                    opened_at=state_change.effective_at,
                    opened_by_state_change_id=state_change.state_change_id,
                )
                continue

            if action == "trigger_reflection":
                if closed_episode_for_trigger is None:
                    raise ValidationEpisodeBuilderError(
                        "trigger_reflection requires a closed episode in the same transition."
                    )
                reflection_triggers.append(
                    ReflectionTrigger(
                        episode_id=closed_episode_for_trigger.episode_id,
                        target_key=target_key,
                        triggered_at=state_change.effective_at,
                        close_reason=_require_reflection_close_reason(
                            closed_episode_for_trigger.close_reason
                        ),
                        closed_by_state_change_id=state_change.state_change_id,
                        closed_by_event_ids=state_change.source_event_ids,
                    )
                )
                continue

            raise ValidationEpisodeBuilderError(f"Unsupported action '{action}'.")

        _validate_open_state(
            state_change=state_change,
            open_episode=open_episode,
            open_segment=open_segment,
        )
        previous_state_change = state_change
        previous_effective_at = state_change.effective_at

    return EpisodeBuildResult(
        completed_episodes=tuple(completed_episodes),
        open_episode=open_episode,
        completed_segments=tuple(completed_segments),
        open_segment=open_segment,
        reflection_triggers=tuple(reflection_triggers),
        transitions=tuple(transitions),
    )


def _close_episode(
    *,
    episode: ViewEpisode,
    closed_at: datetime,
    closed_by_state_change_id: str,
    closed_by_event_ids: tuple[str, ...],
    close_reason: ViewCloseReason,
) -> ViewEpisode:
    return ViewEpisode(
        episode_id=episode.episode_id,
        target_key=episode.target_key,
        direction=episode.direction,
        opened_at=episode.opened_at,
        opened_by_state_change_id=episode.opened_by_state_change_id,
        opened_by_event_ids=episode.opened_by_event_ids,
        closed_at=closed_at,
        closed_by_state_change_id=closed_by_state_change_id,
        closed_by_event_ids=closed_by_event_ids,
        close_reason=close_reason,
    )


def _close_segment(
    *,
    segment: PositionSegment,
    closed_at: datetime,
    closed_by_state_change_id: str,
    close_reason: SegmentCloseReason,
) -> PositionSegment:
    return PositionSegment(
        segment_id=segment.segment_id,
        episode_id=segment.episode_id,
        target_key=segment.target_key,
        target_weight=segment.target_weight,
        opened_at=segment.opened_at,
        opened_by_state_change_id=segment.opened_by_state_change_id,
        closed_at=closed_at,
        closed_by_state_change_id=closed_by_state_change_id,
        close_reason=close_reason,
    )


def _validate_open_state(
    *,
    state_change: ViewStateChange,
    open_episode: ViewEpisode | None,
    open_segment: PositionSegment | None,
) -> None:
    if state_change.state == "flat":
        if open_episode is not None or open_segment is not None:
            raise ValidationEpisodeBuilderError(
                "Flat state must not leave an open episode/segment."
            )
        return

    if open_episode is None or open_segment is None:
        raise ValidationEpisodeBuilderError("Directional state must keep an open episode/segment.")
    if open_episode.direction != state_change.direction:
        raise ValidationEpisodeBuilderError(
            "Open episode direction does not match current state_change."
        )
    if open_segment.target_weight != state_change.target_weight:
        raise ValidationEpisodeBuilderError(
            "Open segment weight does not match current state_change."
        )


def _require_reflection_close_reason(
    value: ViewCloseReason | None,
) -> Literal["state_changed_to_flat", "state_changed_to_opposite_direction"]:
    if value == "state_changed_to_flat":
        return "state_changed_to_flat"
    if value == "state_changed_to_opposite_direction":
        return "state_changed_to_opposite_direction"
    raise ValidationEpisodeBuilderError(
        "trigger_reflection requires close_reason to be flat or opposite-direction close."
    )


__all__ = [
    "EpisodeBuildResult",
    "ValidationEpisodeBuilderError",
    "build_episodes",
]


