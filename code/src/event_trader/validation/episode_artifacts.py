"""Persisted inspectable episode artifacts derived from canonical state-changes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from event_trader.contracts import (
    PositionSegment,
    ReflectionTrigger,
    ViewEpisode,
    ViewStateChange,
)
from event_trader.contracts.view_state_change import ViewStateChangeContractError
from event_trader.storage import WorkspaceLayout

from .episode_builder import EpisodeBuildResult, ValidationEpisodeBuilderError, build_episodes
from .episode_snapshot import ClosedEpisodeSnapshot
from .state_change_store import read_state_changes


class EpisodeArtifactStoreError(ValueError):
    """Raised when persisted episode artifacts are missing or malformed."""


def closed_episode_artifact_path(layout: WorkspaceLayout, episode: ViewEpisode) -> Path:
    """Resolve one closed episode artifact path."""
    if not isinstance(layout, WorkspaceLayout):
        raise EpisodeArtifactStoreError("layout must be a WorkspaceLayout instance.")
    if not isinstance(episode, ViewEpisode):
        raise EpisodeArtifactStoreError("episode must be a ViewEpisode instance.")
    if episode.closed_at is None:
        raise EpisodeArtifactStoreError(
            "closed episode artifacts require episode.closed_at to be set."
        )
    return (
        layout.runtime_root
        / "validation"
        / "episodes"
        / episode.target_key
        / (
            f"{_format_review_timestamp(episode.opened_at)}"
            f"_{_format_review_timestamp(episode.closed_at)}.json"
        )
    ).resolve(strict=False)


def open_episode_artifact_path(layout: WorkspaceLayout, target_key: str) -> Path:
    """Resolve one target's open episode artifact path."""
    if not isinstance(layout, WorkspaceLayout):
        raise EpisodeArtifactStoreError("layout must be a WorkspaceLayout instance.")
    if not isinstance(target_key, str) or not target_key.strip():
        raise EpisodeArtifactStoreError("target_key must be a non-empty string.")
    return (
        layout.runtime_root / "validation" / "episodes" / target_key / "open.json"
    ).resolve(strict=False)


def refresh_episode_artifacts(layout: WorkspaceLayout, target_key: str) -> None:
    """Rebuild one target's episode artifacts from canonical state-changes."""
    if not isinstance(layout, WorkspaceLayout):
        raise EpisodeArtifactStoreError("layout must be a WorkspaceLayout instance.")
    state_changes = read_state_changes(layout, target_key)
    episode_state = build_episodes(state_changes)
    target_root = open_episode_artifact_path(layout, target_key).parent
    target_root.mkdir(parents=True, exist_ok=True)

    expected_closed_paths: set[Path] = set()
    generated_at = datetime.now(UTC)
    for episode in episode_state.completed_episodes:
        closed_at = episode.closed_at
        if closed_at is None:
            raise EpisodeArtifactStoreError(
                "completed episode artifacts require closed_at timestamps."
            )
        artifact_path = closed_episode_artifact_path(layout, episode)
        expected_closed_paths.add(artifact_path)
        payload: dict[str, object] = {
            "generated_at": generated_at.isoformat(),
            "episode": _serialize_episode(episode),
            "segments": [
                _serialize_segment(segment)
                for segment in episode_state.completed_segments
                if segment.episode_id == episode.episode_id
            ],
            "state_changes": [
                _serialize_state_change(state_change)
                for state_change in state_changes
                if episode.opened_at <= state_change.effective_at <= closed_at
            ],
            "reflection_triggers": [
                _serialize_reflection_trigger(trigger)
                for trigger in episode_state.reflection_triggers
                if trigger.episode_id == episode.episode_id
            ],
        }
        _write_json_artifact(artifact_path, payload)

    open_path = open_episode_artifact_path(layout, target_key)
    if episode_state.open_episode is None:
        if open_path.exists():
            open_path.unlink()
    else:
        if episode_state.open_segment is None:
            raise EpisodeArtifactStoreError(
                "open episode artifacts require an open_segment."
            )
        open_episode_segments = [
            segment
            for segment in episode_state.completed_segments
            if segment.episode_id == episode_state.open_episode.episode_id
        ]
        open_episode_segments.append(episode_state.open_segment)
        open_payload: dict[str, object] = {
            "generated_at": generated_at.isoformat(),
            "episode": _serialize_episode(episode_state.open_episode),
            "segments": [
                _serialize_segment(segment)
                for segment in open_episode_segments
            ],
            "state_changes": [
                _serialize_state_change(state_change)
                for state_change in state_changes
                if state_change.effective_at >= episode_state.open_episode.opened_at
            ],
            "reflection_triggers": [],
        }
        _write_json_artifact(open_path, open_payload)

    for artifact_path in target_root.glob("*.json"):
        if artifact_path.name == "open.json":
            continue
        if artifact_path.resolve(strict=False) not in expected_closed_paths:
            artifact_path.unlink()

def read_target_episode_artifacts(layout: WorkspaceLayout, target_key: str) -> EpisodeBuildResult:
    """Read one target's persisted episode artifacts as the primary runtime surface."""
    if not isinstance(layout, WorkspaceLayout):
        raise EpisodeArtifactStoreError("layout must be a WorkspaceLayout instance.")
    target_root = open_episode_artifact_path(layout, target_key).parent
    open_path = open_episode_artifact_path(layout, target_key)
    closed_paths = tuple(
        sorted(path for path in target_root.glob("*.json") if path.name != "open.json")
    )

    if not closed_paths and not open_path.exists():
        if read_state_changes(layout, target_key):
            raise EpisodeArtifactStoreError(
                "Persisted state-changes exist but episode artifacts are missing for "
                f"target_key={target_key!r}."
            )
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
    open_episode: ViewEpisode | None = None
    open_segment: PositionSegment | None = None

    for artifact_path in closed_paths:
        payload = _read_json_artifact(artifact_path)
        episode = _deserialize_episode(payload.get("episode"), artifact_path=artifact_path)
        if episode.closed_at is None:
            raise EpisodeArtifactStoreError(
                f"Closed episode artifact must carry closed_at: {artifact_path}"
            )
        completed_episodes.append(episode)
        completed_segments.extend(
            _deserialize_segments(payload.get("segments"), artifact_path=artifact_path)
        )
        reflection_triggers.extend(
            _deserialize_reflection_triggers(
                payload.get("reflection_triggers"),
                artifact_path=artifact_path,
            )
        )

    if open_path.exists():
        payload = _read_json_artifact(open_path)
        open_episode = _deserialize_episode(payload.get("episode"), artifact_path=open_path)
        if open_episode.closed_at is not None:
            raise EpisodeArtifactStoreError(
                f"open.json must not contain a closed episode: {open_path}"
            )
        open_segments = _deserialize_segments(payload.get("segments"), artifact_path=open_path)
        open_segment_candidates = [
            segment for segment in open_segments if segment.closed_at is None
        ]
        if len(open_segment_candidates) != 1:
            raise EpisodeArtifactStoreError(
                f"open.json must contain exactly one open segment: {open_path}"
            )
        open_segment = open_segment_candidates[0]
        completed_segments.extend(
            segment for segment in open_segments if segment.closed_at is not None
        )

    return EpisodeBuildResult(
        completed_episodes=tuple(sorted(completed_episodes, key=lambda value: value.opened_at)),
        open_episode=open_episode,
        completed_segments=tuple(
            sorted(completed_segments, key=lambda value: value.opened_at)
        ),
        open_segment=open_segment,
        reflection_triggers=tuple(
            sorted(reflection_triggers, key=lambda value: value.triggered_at)
        ),
        transitions=(),
    )


def read_closed_episode_artifact(
    layout: WorkspaceLayout,
    target_key: str,
    episode_id: str,
) -> ClosedEpisodeSnapshot:
    """Read one persisted closed episode artifact as a reflection snapshot."""
    if not isinstance(layout, WorkspaceLayout):
        raise EpisodeArtifactStoreError("layout must be a WorkspaceLayout instance.")
    target_root = open_episode_artifact_path(layout, target_key).parent
    if not target_root.exists():
        if read_state_changes(layout, target_key):
            raise EpisodeArtifactStoreError(
                "Persisted state-changes exist but episode artifacts are missing for "
                f"target_key={target_key!r}."
            )
        raise ValidationEpisodeBuilderError(
            "episode_id was not found in completed_episodes: "
            f"{episode_id!r} for target_key {target_key!r}."
        )

    for artifact_path in sorted(target_root.glob("*.json")):
        if artifact_path.name == "open.json":
            continue
        payload = _read_json_artifact(artifact_path)
        episode = _deserialize_episode(payload.get("episode"), artifact_path=artifact_path)
        if episode.episode_id != episode_id:
            continue
        return ClosedEpisodeSnapshot(
            episode=episode,
            segments=_deserialize_segments(payload.get("segments"), artifact_path=artifact_path),
            state_changes=_deserialize_state_changes(
                payload.get("state_changes"),
                artifact_path=artifact_path,
            ),
        )

    raise ValidationEpisodeBuilderError(
        "episode_id was not found in completed_episodes: "
        f"{episode_id!r} for target_key {target_key!r}."
    )


def _write_json_artifact(path: Path, payload: dict[str, object]) -> None:
    try:
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise EpisodeArtifactStoreError(
            f"Failed to write episode artifact {path}: {exc}"
        ) from exc


def _read_json_artifact(path: Path) -> dict[str, object]:
    if not path.exists() or not path.is_file():
        raise EpisodeArtifactStoreError(f"Missing episode artifact file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EpisodeArtifactStoreError(
            f"Failed to read episode artifact {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise EpisodeArtifactStoreError(
            f"Episode artifact payload must be a JSON object: {path}"
        )
    return payload


def _serialize_state_change(state_change: ViewStateChange) -> dict[str, object]:
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
    }


def _serialize_episode(episode: ViewEpisode) -> dict[str, object]:
    payload: dict[str, object] = {
        "episode_id": episode.episode_id,
        "target_key": episode.target_key,
        "direction": episode.direction,
        "opened_at": episode.opened_at.isoformat(),
        "opened_by_state_change_id": episode.opened_by_state_change_id,
        "opened_by_event_ids": list(episode.opened_by_event_ids),
    }
    if episode.closed_at is not None:
        payload["closed_at"] = episode.closed_at.isoformat()
        payload["closed_by_state_change_id"] = episode.closed_by_state_change_id
        payload["closed_by_event_ids"] = list(episode.closed_by_event_ids or ())
        payload["close_reason"] = episode.close_reason
    return payload


def _serialize_segment(segment: PositionSegment) -> dict[str, object]:
    payload: dict[str, object] = {
        "segment_id": segment.segment_id,
        "episode_id": segment.episode_id,
        "target_key": segment.target_key,
        "target_weight": segment.target_weight,
        "opened_at": segment.opened_at.isoformat(),
        "opened_by_state_change_id": segment.opened_by_state_change_id,
    }
    if segment.closed_at is not None:
        payload["closed_at"] = segment.closed_at.isoformat()
        payload["closed_by_state_change_id"] = segment.closed_by_state_change_id
        payload["close_reason"] = segment.close_reason
    return payload


def _serialize_reflection_trigger(trigger: ReflectionTrigger) -> dict[str, object]:
    return {
        "episode_id": trigger.episode_id,
        "target_key": trigger.target_key,
        "triggered_at": trigger.triggered_at.isoformat(),
        "close_reason": trigger.close_reason,
        "closed_by_state_change_id": trigger.closed_by_state_change_id,
        "closed_by_event_ids": list(trigger.closed_by_event_ids),
    }


def _deserialize_state_changes(
    value: object,
    *,
    artifact_path: Path,
) -> tuple[ViewStateChange, ...]:
    if not isinstance(value, list):
        raise EpisodeArtifactStoreError(
            f"Episode artifact state_changes must be a JSON array: {artifact_path}"
        )
    state_changes: list[ViewStateChange] = []
    for item in value:
        if not isinstance(item, dict):
            raise EpisodeArtifactStoreError(
                f"Episode artifact state_changes must contain JSON objects: {artifact_path}"
            )
        try:
            state_changes.append(
                ViewStateChange(
                    state_change_id=item["state_change_id"],
                    target_key=item["target_key"],
                    state=item["state"],
                    direction=item["direction"],
                    conviction=item["conviction"],
                    target_weight=item["target_weight"],
                    effective_at=datetime.fromisoformat(item["effective_at"]),
                    source_event_ids=tuple(item["source_event_ids"]),
                    rationale_md=item["rationale_md"],
                    used_lesson_ids=tuple(item.get("used_lesson_ids", ())),
                    source_kind=item["source_kind"],
                    pm_decision_id=item.get("pm_decision_id"),
                    execution_record_id=item.get("execution_record_id"),
                )
            )
        except (KeyError, TypeError, ValueError, ViewStateChangeContractError) as exc:
            raise EpisodeArtifactStoreError(
                f"Invalid ViewStateChange in episode artifact {artifact_path}: {exc}"
            ) from exc
    return tuple(state_changes)


def _deserialize_episode(value: object, *, artifact_path: Path) -> ViewEpisode:
    if not isinstance(value, dict):
        raise EpisodeArtifactStoreError(
            f"Episode artifact episode must be a JSON object: {artifact_path}"
        )
    try:
        return ViewEpisode(
            episode_id=value["episode_id"],
            target_key=value["target_key"],
            direction=value["direction"],
            opened_at=datetime.fromisoformat(value["opened_at"]),
            opened_by_state_change_id=value["opened_by_state_change_id"],
            opened_by_event_ids=tuple(value["opened_by_event_ids"]),
            closed_at=(
                None
                if value.get("closed_at") is None
                else datetime.fromisoformat(value["closed_at"])
            ),
            closed_by_state_change_id=value.get("closed_by_state_change_id"),
            closed_by_event_ids=(
                None
                if value.get("closed_by_event_ids") is None
                else tuple(value["closed_by_event_ids"])
            ),
            close_reason=value.get("close_reason"),
        )
    except (KeyError, TypeError, ValueError, ViewStateChangeContractError) as exc:
        raise EpisodeArtifactStoreError(
            f"Invalid ViewEpisode in episode artifact {artifact_path}: {exc}"
        ) from exc


def _deserialize_segments(value: object, *, artifact_path: Path) -> tuple[PositionSegment, ...]:
    if not isinstance(value, list):
        raise EpisodeArtifactStoreError(
            f"Episode artifact segments must be a JSON array: {artifact_path}"
        )
    segments: list[PositionSegment] = []
    for item in value:
        if not isinstance(item, dict):
            raise EpisodeArtifactStoreError(
                f"Episode artifact segments must contain JSON objects: {artifact_path}"
            )
        try:
            segments.append(
                PositionSegment(
                    segment_id=item["segment_id"],
                    episode_id=item["episode_id"],
                    target_key=item["target_key"],
                    target_weight=item["target_weight"],
                    opened_at=datetime.fromisoformat(item["opened_at"]),
                    opened_by_state_change_id=item["opened_by_state_change_id"],
                    closed_at=(
                        None
                        if item.get("closed_at") is None
                        else datetime.fromisoformat(item["closed_at"])
                    ),
                    closed_by_state_change_id=item.get("closed_by_state_change_id"),
                    close_reason=item.get("close_reason"),
                )
            )
        except (KeyError, TypeError, ValueError, ViewStateChangeContractError) as exc:
            raise EpisodeArtifactStoreError(
                f"Invalid PositionSegment in episode artifact {artifact_path}: {exc}"
            ) from exc
    return tuple(segments)


def _deserialize_reflection_triggers(
    value: object,
    *,
    artifact_path: Path,
) -> tuple[ReflectionTrigger, ...]:
    if not isinstance(value, list):
        raise EpisodeArtifactStoreError(
            f"Episode artifact reflection_triggers must be a JSON array: {artifact_path}"
        )
    triggers: list[ReflectionTrigger] = []
    for item in value:
        if not isinstance(item, dict):
            raise EpisodeArtifactStoreError(
                "Episode artifact reflection_triggers must contain JSON objects: "
                f"{artifact_path}"
            )
        try:
            triggers.append(
                ReflectionTrigger(
                    episode_id=item["episode_id"],
                    target_key=item["target_key"],
                    triggered_at=datetime.fromisoformat(item["triggered_at"]),
                    close_reason=item["close_reason"],
                    closed_by_state_change_id=item["closed_by_state_change_id"],
                    closed_by_event_ids=tuple(item["closed_by_event_ids"]),
                )
            )
        except (KeyError, TypeError, ValueError, ViewStateChangeContractError) as exc:
            raise EpisodeArtifactStoreError(
                f"Invalid ReflectionTrigger in episode artifact {artifact_path}: {exc}"
            ) from exc
    return tuple(triggers)


def _format_review_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%y%m%dT%H%M%S")


__all__ = [
    "EpisodeArtifactStoreError",
    "closed_episode_artifact_path",
    "open_episode_artifact_path",
    "read_closed_episode_artifact",
    "read_target_episode_artifacts",
    "refresh_episode_artifacts",
]


