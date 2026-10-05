"""Projection helpers for PM-facing as-of EpisodeMemory views."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from event_trader.contracts._validators import validate_timestamp, validate_target_key
from event_trader.episode_memory.contracts import (
    EpisodeMemoryDelta,
    EpisodeMemoryDeltaSourceRefs,
    episode_memory_record_hash,
)
from event_trader.episode_memory.store import PersistedEpisodeMemoryDelta


@dataclass(frozen=True, slots=True)
class PMEpisodeMemoryViewDelta:
    """A single PM-facing item in a projected layer-2 view."""

    delta_id: str
    summary_md: str
    thesis_delta: str
    pm_management_delta: str
    risk_delta: str
    invalidation_delta: str
    source_refs: EpisodeMemoryDeltaSourceRefs


@dataclass(frozen=True, slots=True)
class PMEpisodeMemoryView:
    """PM-facing, as-of projection over canonical EpisodeMemoryDeltas."""

    episode_id: str
    target_key: str
    business_at: datetime
    provisional_warnings: tuple[PMEpisodeMemoryViewDelta, ...]
    validated_lessons: tuple[PMEpisodeMemoryViewDelta, ...]
    finalized_hindsight: tuple[PMEpisodeMemoryViewDelta, ...]
    built_from_delta_ids: tuple[str, ...]
    built_from_delta_hashes: tuple[str, ...]
    excluded_delta_ids: tuple[str, ...]
    excluded_delta_reasons: tuple[str, ...]
    source_visible_through_max: datetime
    usable_from_max: datetime
    drilldown_refs: tuple[EpisodeMemoryDeltaSourceRefs, ...]
    projection_hash: str

    def to_json_payload(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "provisional_warnings": [
                _view_delta_payload(item) for item in self.provisional_warnings
            ],
            "validated_lessons": [
                _view_delta_payload(item) for item in self.validated_lessons
            ],
            "finalized_hindsight": [
                _view_delta_payload(item) for item in self.finalized_hindsight
            ],
            "built_from_delta_ids": list(self.built_from_delta_ids),
            "built_from_delta_hashes": list(self.built_from_delta_hashes),
            "excluded_delta_ids": list(self.excluded_delta_ids),
            "excluded_delta_reasons": list(self.excluded_delta_reasons),
            "source_visible_through_max": self.source_visible_through_max.isoformat(),
            "usable_from_max": self.usable_from_max.isoformat(),
            "drilldown_refs": [item.to_json_payload() for item in self.drilldown_refs],
        }


def build_pm_episode_memory_view_as_of(
    *,
    target_key: str,
    episode_id: str,
    business_at: datetime,
    deltas: tuple[EpisodeMemoryDelta | PersistedEpisodeMemoryDelta, ...],
) -> PMEpisodeMemoryView:
    """Build one rebuildable PM-facing projection as of ``business_at``."""

    normalized_target = validate_target_key(target_key, error_type=EpisodeMemoryValidationError)
    normalized_episode_id = _validate_non_blank_text(episode_id, "episode_id")
    normalized_business_at = validate_timestamp(
        business_at,
        field_name="business_at",
        error_type=EpisodeMemoryValidationError,
    )

    provisional: list[PMEpisodeMemoryViewDelta] = []
    validated: list[PMEpisodeMemoryViewDelta] = []
    finalized: list[PMEpisodeMemoryViewDelta] = []
    built_ids: list[str] = []
    built_hashes: list[str] = []
    excluded_ids: list[str] = []
    excluded_reasons: list[str] = []
    source_visible_times: list[datetime] = []
    usable_from_times: list[datetime] = []
    drilldown_refs: list[EpisodeMemoryDeltaSourceRefs] = []

    for delta in _iter_deltas(deltas):
        if delta.target_key != normalized_target:
            raise EpisodeMemoryValidationError(
                "projection input contains mixed target_key records."
            )
        if delta.episode_id != normalized_episode_id:
            raise EpisodeMemoryValidationError(
                "projection input contains mixed episode_id records."
            )
        if delta.usable_from <= normalized_business_at:
            entry = _to_view_delta(delta)
            if delta.memory_status == "provisional":
                provisional.append(entry)
            elif delta.memory_status == "validated":
                validated.append(entry)
            elif delta.memory_status == "finalized":
                finalized.append(entry)
            elif delta.memory_status == "invalidated":
                provisional.append(entry)
            else:
                raise EpisodeMemoryValidationError(
                    f"unsupported memory_status in projection: {delta.memory_status!r}"
                )
            built_ids.append(delta.delta_id)
            delta_hash = episode_memory_record_hash(delta.to_json_payload())
            built_hashes.append(delta.content_hash)
            source_visible_times.append(delta.source_visible_through)
            usable_from_times.append(delta.usable_from)
            drilldown_refs.append(delta.source_refs)
        else:
            excluded_ids.append(delta.delta_id)
            excluded_reasons.append(
                "excluded: usable_from is after requested business_at."
            )

    source_visible_through_max = (
        max(source_visible_times) if source_visible_times else normalized_business_at
    )
    usable_from_max = max(usable_from_times) if usable_from_times else normalized_business_at

    view = PMEpisodeMemoryView(
        episode_id=normalized_episode_id,
        target_key=normalized_target,
        business_at=normalized_business_at,
        provisional_warnings=tuple(provisional),
        validated_lessons=tuple(validated),
        finalized_hindsight=tuple(finalized),
        built_from_delta_ids=tuple(built_ids),
        built_from_delta_hashes=tuple(built_hashes),
        excluded_delta_ids=tuple(excluded_ids),
        excluded_delta_reasons=tuple(excluded_reasons),
        source_visible_through_max=source_visible_through_max,
        usable_from_max=usable_from_max,
        drilldown_refs=tuple(drilldown_refs),
        projection_hash="",
    )
    projection_hash = episode_memory_record_hash(view.to_json_payload())
    return _replace_projection_hash(view, projection_hash)


def _iter_deltas(
    deltas: tuple[EpisodeMemoryDelta | PersistedEpisodeMemoryDelta, ...],
) -> tuple[EpisodeMemoryDelta, ...]:
    records: list[EpisodeMemoryDelta] = []
    for delta in deltas:
        if isinstance(delta, EpisodeMemoryDelta):
            records.append(delta)
            continue
        if isinstance(delta, PersistedEpisodeMemoryDelta):
            records.append(delta.record)
            continue
        raise EpisodeMemoryValidationError("deltas must be EpisodeMemoryDelta values.")
    return tuple(records)


def _to_view_delta(delta: EpisodeMemoryDelta) -> PMEpisodeMemoryViewDelta:
    return PMEpisodeMemoryViewDelta(
        delta_id=delta.delta_id,
        summary_md=delta.summary_md,
        thesis_delta=delta.thesis_delta,
        pm_management_delta=delta.pm_management_delta,
        risk_delta=delta.risk_delta,
        invalidation_delta=delta.invalidation_delta,
        source_refs=delta.source_refs,
    )


def _replace_projection_hash(
    view: PMEpisodeMemoryView,
    projection_hash: str,
) -> PMEpisodeMemoryView:
    return PMEpisodeMemoryView(
        episode_id=view.episode_id,
        target_key=view.target_key,
        business_at=view.business_at,
        provisional_warnings=view.provisional_warnings,
        validated_lessons=view.validated_lessons,
        finalized_hindsight=view.finalized_hindsight,
        built_from_delta_ids=view.built_from_delta_ids,
        built_from_delta_hashes=view.built_from_delta_hashes,
        excluded_delta_ids=view.excluded_delta_ids,
        excluded_delta_reasons=view.excluded_delta_reasons,
        source_visible_through_max=view.source_visible_through_max,
        usable_from_max=view.usable_from_max,
        drilldown_refs=view.drilldown_refs,
        projection_hash=projection_hash,
    )


def _view_delta_payload(entry: PMEpisodeMemoryViewDelta) -> dict[str, object]:
    return {
        "delta_id": entry.delta_id,
        "summary_md": entry.summary_md,
        "thesis_delta": entry.thesis_delta,
        "pm_management_delta": entry.pm_management_delta,
        "risk_delta": entry.risk_delta,
        "invalidation_delta": entry.invalidation_delta,
        "source_refs": entry.source_refs.to_json_payload(),
    }


def _validate_non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise EpisodeMemoryValidationError(f"{field_name} must be a non-blank string.")
    normalized = value.strip()
    if not normalized:
        raise EpisodeMemoryValidationError(f"{field_name} must be a non-blank string.")
    return normalized


class EpisodeMemoryValidationError(ValueError):
    """Raised when a projection cannot be built from inconsistent input."""


__all__ = [
    "PMEpisodeMemoryView",
    "PMEpisodeMemoryViewDelta",
    "EpisodeMemoryValidationError",
    "build_pm_episode_memory_view_as_of",
]
