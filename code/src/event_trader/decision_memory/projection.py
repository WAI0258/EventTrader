"""Read-side projection for decision episode chains."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from event_trader.decision_memory.contracts import (
    DecisionEpisodeRecord,
    DecisionEpisodeStatus,
)
from event_trader.decision_memory.store import PersistedDecisionEpisodeRecord


class DecisionEpisodeProjectionError(ValueError):
    """Raised when decision episode projection inputs are malformed."""


@dataclass(frozen=True, slots=True)
class DecisionEpisodeProjection:
    episode_id: str
    target_key: str
    opened_at: datetime
    last_updated_at: datetime
    status: DecisionEpisodeStatus
    event_ids: tuple[str, ...]
    attention_record: DecisionEpisodeRecord | None
    analysis_record: DecisionEpisodeRecord | None
    pm_decision_record: DecisionEpisodeRecord | None
    validation_marks: tuple[DecisionEpisodeRecord, ...]
    reflection_records: tuple[DecisionEpisodeRecord, ...]
    repair_records: tuple[DecisionEpisodeRecord, ...]


def project_decision_episodes(
    persisted_records: tuple[PersistedDecisionEpisodeRecord, ...],
) -> tuple[DecisionEpisodeProjection, ...]:
    """Build deterministic read-side episode projections from append-only records."""
    grouped: dict[str, list[DecisionEpisodeRecord]] = {}
    for persisted in persisted_records:
        grouped.setdefault(persisted.record.episode_id, []).append(persisted.record)

    projections = tuple(
        _project_episode(episode_id, tuple(records))
        for episode_id, records in grouped.items()
    )
    return tuple(
        sorted(
            projections,
            key=lambda projection: (
                projection.opened_at,
                projection.target_key,
                projection.episode_id,
            ),
        )
    )


def find_episode_ids_for_state_change(
    persisted_records: tuple[PersistedDecisionEpisodeRecord, ...],
    *,
    state_change_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Return episode ids with validation marks linked to the given state changes."""
    wanted = set(state_change_ids)
    episode_ids: set[str] = set()
    for persisted in persisted_records:
        record = persisted.record
        if record.record_type != "validation_mark":
            continue
        state_change_id = record.payload.get("state_change_id")
        if isinstance(state_change_id, str) and state_change_id in wanted:
            episode_ids.add(record.episode_id)
    return tuple(sorted(episode_ids))


def _project_episode(
    episode_id: str,
    records: tuple[DecisionEpisodeRecord, ...],
) -> DecisionEpisodeProjection:
    if not records:
        raise DecisionEpisodeProjectionError("records must not be empty.")
    ordered = tuple(
        sorted(
            records,
            key=lambda record: (
                record.business_at,
                record.recorded_at,
                record.record_type,
                record.source_record_id,
            ),
        )
    )
    first = ordered[0]
    attention_records = tuple(
        record for record in ordered if record.record_type == "attention"
    )
    analysis_records = tuple(
        record for record in ordered if record.record_type == "analysis"
    )
    pm_records = tuple(
        record
        for record in ordered
        if record.record_type == "pm_decision"
    )
    validation_marks = tuple(
        record for record in ordered if record.record_type == "validation_mark"
    )
    reflection_records = tuple(
        record for record in ordered if record.record_type == "reflection"
    )
    repair_records = tuple(record for record in ordered if record.record_type == "repair")
    latest_analysis = analysis_records[-1] if analysis_records else None
    return DecisionEpisodeProjection(
        episode_id=episode_id,
        target_key=first.target_key,
        opened_at=attention_records[0].business_at if attention_records else first.business_at,
        last_updated_at=max(record.recorded_at for record in ordered),
        status=_projection_status(
            analysis_record=latest_analysis,
            validation_marks=validation_marks,
            reflection_records=reflection_records,
        ),
        event_ids=first.event_ids,
        attention_record=attention_records[-1] if attention_records else None,
        analysis_record=latest_analysis,
        pm_decision_record=pm_records[-1] if pm_records else None,
        validation_marks=validation_marks,
        reflection_records=reflection_records,
        repair_records=repair_records,
    )


def _projection_status(
    *,
    analysis_record: DecisionEpisodeRecord | None,
    validation_marks: tuple[DecisionEpisodeRecord, ...],
    reflection_records: tuple[DecisionEpisodeRecord, ...],
) -> DecisionEpisodeStatus:
    if reflection_records:
        return "reflected"
    if validation_marks:
        return "validated"
    if analysis_record is None:
        return "open_attention"
    if analysis_record.status == "failed":
        return "analysis_failed"
    if analysis_record.status == "memory_updated":
        return "analysis_memory_updated"
    return "analysis_no_update"


__all__ = [
    "DecisionEpisodeProjection",
    "DecisionEpisodeProjectionError",
    "find_episode_ids_for_state_change",
    "project_decision_episodes",
]
