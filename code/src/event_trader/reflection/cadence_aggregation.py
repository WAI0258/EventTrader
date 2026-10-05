"""Thin aggregation seam for completed cadence-review target reflections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.research_memory import resolve_page_ref
from event_trader.storage import WorkspaceLayout

from .review_coverage_index import (
    ReviewCoverageIndexRecord,
    read_review_coverage_index,
)


class ReflectionCadenceAggregationError(ValueError):
    """Raised when cadence-review aggregation inputs or review paths are malformed."""


@dataclass(frozen=True, slots=True)
class AggregatedCompletedReflection:
    """Thin deterministic reference to one completed target review artifact."""

    target_key: str
    episode_id: str
    review_page_path: str
    artifact_path: Path
    opened_at: datetime
    closed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=ReflectionCadenceAggregationError,
            ),
        )
        page_ref = resolve_page_ref(self.review_page_path)
        if page_ref.page_kind != "review" or page_ref.target_key != self.target_key:
            raise ReflectionCadenceAggregationError(
                "review_page_path must resolve to a canonical target review page for "
                "target_key."
            )
        if not isinstance(self.artifact_path, Path):
            raise ReflectionCadenceAggregationError(
                "artifact_path must be a pathlib.Path instance."
            )
        object.__setattr__(
            self,
            "opened_at",
            validate_timestamp(
                self.opened_at,
                field_name="opened_at",
                error_type=ReflectionCadenceAggregationError,
            ),
        )
        object.__setattr__(
            self,
            "closed_at",
            validate_timestamp(
                self.closed_at,
                field_name="closed_at",
                error_type=ReflectionCadenceAggregationError,
            ),
        )
        if self.closed_at <= self.opened_at:
            raise ReflectionCadenceAggregationError(
                "closed_at must be later than opened_at."
            )
        if not isinstance(self.episode_id, str) or not self.episode_id.strip():
            raise ReflectionCadenceAggregationError(
                "episode_id must be a non-blank string."
            )


@dataclass(frozen=True, slots=True)
class CadenceAggregationReceipt:
    """Aggregated completed reflections for one cadence window."""

    window_start: datetime
    window_end: datetime
    completed_reflections: tuple[AggregatedCompletedReflection, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "window_start",
            validate_timestamp(
                self.window_start,
                field_name="window_start",
                error_type=ReflectionCadenceAggregationError,
            ),
        )
        object.__setattr__(
            self,
            "window_end",
            validate_timestamp(
                self.window_end,
                field_name="window_end",
                error_type=ReflectionCadenceAggregationError,
            ),
        )
        if self.window_end <= self.window_start:
            raise ReflectionCadenceAggregationError(
                "window_end must be later than window_start."
            )
        if not isinstance(self.completed_reflections, tuple):
            raise ReflectionCadenceAggregationError(
                "completed_reflections must be a tuple."
            )
        for reflection in self.completed_reflections:
            if not isinstance(reflection, AggregatedCompletedReflection):
                raise ReflectionCadenceAggregationError(
                    "completed_reflections must contain only "
                    "AggregatedCompletedReflection instances."
                )
            if not (self.window_start < reflection.closed_at <= self.window_end):
                raise ReflectionCadenceAggregationError(
                    "aggregated reflection closed_at must satisfy "
                    "window_start < closed_at <= window_end."
                )


def aggregate_completed_target_reflections(
    *,
    layout: WorkspaceLayout,
    window_start: datetime,
    window_end: datetime,
) -> CadenceAggregationReceipt:
    """Collect completed target reviews whose closed_at fall in one cadence window."""

    if not isinstance(layout, WorkspaceLayout):
        raise ReflectionCadenceAggregationError(
            "layout must be a WorkspaceLayout instance."
        )
    validated_window_start = validate_timestamp(
        window_start,
        field_name="window_start",
        error_type=ReflectionCadenceAggregationError,
    )
    validated_window_end = validate_timestamp(
        window_end,
        field_name="window_end",
        error_type=ReflectionCadenceAggregationError,
    )
    if validated_window_end <= validated_window_start:
        raise ReflectionCadenceAggregationError(
            "window_end must be later than window_start."
        )

    completed_reflections: list[AggregatedCompletedReflection] = []
    for record in read_review_coverage_index(layout):
        if not record.covered_horizons_hours:
            continue
        aggregated = _aggregation_from_coverage_record(record)
        if not (validated_window_start < aggregated.closed_at <= validated_window_end):
            continue
        completed_reflections.append(aggregated)

    return CadenceAggregationReceipt(
        window_start=validated_window_start,
        window_end=validated_window_end,
        completed_reflections=tuple(completed_reflections),
    )


def _aggregation_from_coverage_record(
    record: ReviewCoverageIndexRecord,
) -> AggregatedCompletedReflection:
    closed_at = record.closed_at or record.replay_end_at
    if closed_at is None:
        raise ReflectionCadenceAggregationError(
            "coverage records with horizons must carry closed_at or replay_end_at."
        )
    return AggregatedCompletedReflection(
        target_key=record.target_key,
        episode_id=record.episode_id,
        review_page_path=record.review_page_path,
        artifact_path=record.artifact_path,
        opened_at=record.opened_at,
        closed_at=closed_at,
    )


__all__ = [
    "AggregatedCompletedReflection",
    "CadenceAggregationReceipt",
    "ReflectionCadenceAggregationError",
    "aggregate_completed_target_reflections",
]
