"""Narrow web-search runtime seam for canonical raw-source publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.feeds.models import LiveWebSearchInput
from event_trader.feeds.web_search import SearchAgentRunner, gather_live_web_search_materials
from event_trader.runtime.source_publisher import (
    RuntimeRawSourcePublisher,
    RuntimeRawSourcePublishReceipt,
)
from event_trader.source_archive.web_search import (
    append_historical_web_search_record,
    historical_web_search_observation_id,
    map_live_web_search_to_historical_record,
)
from event_trader.source_release import release_current_web_search_observation_ids
from event_trader.storage import WorkspaceLayout
from event_trader.web_search_schedule import (
    LiveWebSearchCadenceProfile,
    plan_live_web_search_due_windows,
)


class WebSearchBatchDriverError(ValueError):
    """Raised when the web-search batch runtime seam receives invalid state."""


@dataclass(frozen=True, slots=True)
class LiveWebSearchBatchReceipt:
    """Observable result for one successful live web-search slice."""

    target_key: str
    window_start: datetime
    window_end: datetime
    material_count: int
    raw_source_receipts: tuple[RuntimeRawSourcePublishReceipt, ...]


def release_live_web_search_materials(
    *,
    target_key: str,
    query: str,
    discovered_at: datetime,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    run_search_agent: SearchAgentRunner,
    raw_source_publisher: RuntimeRawSourcePublisher,
    ts_init: datetime,
    historical_layout: WorkspaceLayout,
) -> LiveWebSearchBatchReceipt:
    """Run canonical web-search gather/archive and publish raw bus messages."""
    validated_target_key = validate_target_key(
        target_key,
        error_type=WebSearchBatchDriverError,
    )
    validated_discovered_at = _validate_aware_datetime(
        discovered_at,
        field_name="discovered_at",
    )
    validated_ts_init = _validate_aware_datetime(ts_init, field_name="ts_init")
    if not callable(run_search_agent):
        raise WebSearchBatchDriverError("run_search_agent must be callable.")
    if not isinstance(raw_source_publisher, RuntimeRawSourcePublisher):
        raise WebSearchBatchDriverError(
            "raw_source_publisher must be a RuntimeRawSourcePublisher instance."
        )
    if not isinstance(historical_layout, WorkspaceLayout):
        raise WebSearchBatchDriverError(
            "historical_layout must be a WorkspaceLayout instance."
        )

    receipt_window_start = (
        validated_discovered_at.astimezone(UTC)
        if window_start is None
        else _validate_utc_datetime(window_start, field_name="window_start")
    )
    receipt_window_end = (
        validated_discovered_at.astimezone(UTC)
        if window_end is None
        else _validate_utc_datetime(window_end, field_name="window_end")
    )
    if receipt_window_end < receipt_window_start:
        raise WebSearchBatchDriverError(
            "web-search receipt window_end must be greater than or equal to "
            "window_start."
        )

    materials = gather_live_web_search_materials(
        target_key=validated_target_key,
        query=query,
        discovered_at=validated_discovered_at,
        run_search_agent=run_search_agent,
    )
    if window_start is not None and window_end is not None:
        materials = _filter_windowed_materials(
            materials,
            window_start=receipt_window_start,
            window_end=receipt_window_end,
        )
    archived_records = tuple(
        map_live_web_search_to_historical_record(
            target_key=validated_target_key,
            ingress_input=ingress_input,
            labels=labels,
        )
        for ingress_input, labels in materials
    )
    written_observation_ids: set[str] = set()
    for record in archived_records:
        receipt = append_historical_web_search_record(
            historical_layout,
            record=record,
        )
        if receipt.status == "written":
            written_observation_ids.add(historical_web_search_observation_id(record))
    releaseable_observation_ids = release_current_web_search_observation_ids(
        layout=historical_layout,
        target_key=validated_target_key,
        current_records=archived_records,
    )
    raw_source_receipts: list[RuntimeRawSourcePublishReceipt] = []
    for (ingress_input, _labels), record in zip(materials, archived_records, strict=True):
        observation_id = historical_web_search_observation_id(record)
        if observation_id not in releaseable_observation_ids:
            continue
        if observation_id not in written_observation_ids:
            continue
        raw_source_receipts.append(
            raw_source_publisher.publish_web_result_raw(
                target_key=validated_target_key,
                source_ref=ingress_input.source_ref,
                record_id=observation_id,
                event_time=record.visible_at,
                recorded_at=validated_ts_init,
            )
        )

    return LiveWebSearchBatchReceipt(
        target_key=validated_target_key,
        window_start=receipt_window_start,
        window_end=receipt_window_end,
        material_count=len(materials),
        raw_source_receipts=tuple(raw_source_receipts),
    )


@dataclass(slots=True)
class WebSearchBatchDriver:
    """Run at most one due web-search batch window per invocation."""

    target_key: str
    search_intent: str
    cadence_profile: LiveWebSearchCadenceProfile
    last_successful_batch_end: datetime
    next_due_at: datetime
    run_search_agent: SearchAgentRunner
    raw_source_publisher: RuntimeRawSourcePublisher
    historical_layout: WorkspaceLayout

    def __post_init__(self) -> None:
        self.target_key = validate_target_key(
            self.target_key,
            error_type=WebSearchBatchDriverError,
        )
        self.search_intent = _validate_non_blank_text(
            self.search_intent,
            field_name="search_intent",
        )
        if not isinstance(self.cadence_profile, LiveWebSearchCadenceProfile):
            raise WebSearchBatchDriverError(
                "cadence_profile must be a LiveWebSearchCadenceProfile instance."
            )
        self.last_successful_batch_end = _validate_utc_datetime(
            self.last_successful_batch_end,
            field_name="last_successful_batch_end",
        )
        self.next_due_at = _validate_utc_datetime(
            self.next_due_at,
            field_name="next_due_at",
        )
        if self.next_due_at <= self.last_successful_batch_end:
            raise WebSearchBatchDriverError(
                "next_due_at must be after last_successful_batch_end."
            )
        if not callable(self.run_search_agent):
            raise WebSearchBatchDriverError("run_search_agent must be callable.")
        if not isinstance(self.raw_source_publisher, RuntimeRawSourcePublisher):
            raise WebSearchBatchDriverError(
                "raw_source_publisher must be a RuntimeRawSourcePublisher instance."
            )
        if not isinstance(self.historical_layout, WorkspaceLayout):
            raise WebSearchBatchDriverError(
                "historical_layout must be a WorkspaceLayout instance."
            )

    def run_due(self, now: datetime) -> LiveWebSearchBatchReceipt | None:
        """Run one due batch window ending at `now`, or return None."""
        validated_now = _validate_utc_datetime(now, field_name="now")
        plan = plan_live_web_search_due_windows(
            self.cadence_profile,
            cursor_at=self.last_successful_batch_end,
            now=validated_now,
            max_windows=1,
        )
        self.next_due_at = plan.next_due_at
        if not plan.due_windows:
            return None

        due_window = plan.due_windows[0]
        window_start = due_window.window_start
        window_end = due_window.window_end
        query = _render_window_query(
            search_intent=self.search_intent,
            window_start=window_start,
            window_end=window_end,
        )
        batch_receipt = release_live_web_search_materials(
            target_key=self.target_key,
            query=query,
            discovered_at=window_end,
            window_start=window_start,
            window_end=window_end,
            run_search_agent=self.run_search_agent,
            raw_source_publisher=self.raw_source_publisher,
            ts_init=window_end,
            historical_layout=self.historical_layout,
        )
        self.last_successful_batch_end = window_end
        return batch_receipt


def _filter_windowed_materials(
    materials: tuple[tuple[LiveWebSearchInput, list[str]], ...],
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[tuple[LiveWebSearchInput, list[str]], ...]:
    filtered_materials: list[tuple[LiveWebSearchInput, list[str]]] = []
    for ingress_input, labels in materials:
        published_at = getattr(ingress_input, "published_at", None)
        if published_at is not None:
            published_at_utc = _validate_utc_datetime(
                published_at,
                field_name="published_at",
            )
            if published_at_utc < window_start or published_at_utc > window_end:
                continue
        filtered_materials.append((ingress_input, labels))
    return tuple(filtered_materials)


def _render_window_query(
    *,
    search_intent: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    return (
        f"{search_intent}\n"
        f"window_start={window_start.isoformat()}\n"
        f"window_end={window_end.isoformat()}"
    )


def _validate_non_blank_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise WebSearchBatchDriverError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise WebSearchBatchDriverError(f"{field_name} must not be blank.")
    return normalized


def _validate_aware_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise WebSearchBatchDriverError(f"{field_name} must be a datetime.")
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=WebSearchBatchDriverError,
    )


def _validate_utc_datetime(value: object, *, field_name: str) -> datetime:
    validated = _validate_aware_datetime(value, field_name=field_name)
    return validated.astimezone(UTC)


__all__ = [
    "LiveWebSearchBatchReceipt",
    "WebSearchBatchDriver",
    "WebSearchBatchDriverError",
    "release_live_web_search_materials",
]
