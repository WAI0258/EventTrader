"""Live-scoped CEAU processing-time and source-completeness coordination."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

from event_trader.ceau.contracts import (
    CEAUSourceCompletenessObservedRecord,
    CEAUUnitEmittedRecord,
    CEAUWatermarkObservedRecord,
    CompletenessDomain,
    DomainWatermark,
    SourceCompletenessObservation,
    WatermarkObservation,
    WatermarkOverallStatus,
)
from event_trader.ceau.coordinator import (
    CEAUCoordinatorError,
    CEAUEventRouteResult,
    CEAUUnitFormationCore,
)
from event_trader.ceau.policy import StreamRoutingConfig
from event_trader.ceau.routing import (
    StreamRoutingDecision,
    StreamRoutingEvent,
    SupportSectionResolver,
)
from event_trader.ceau.store import FileBackedCEAUStore, FileBackedCEAUStoreError


class LiveCEAUError(ValueError):
    """Raised when live CEAU coordination cannot proceed."""


class LiveCEAUCoordinator:
    """Live coordinator for processing-time safety close and source completeness."""

    def __init__(
        self,
        *,
        store: FileBackedCEAUStore,
        stream_routing_config: StreamRoutingConfig,
    ) -> None:
        if not isinstance(stream_routing_config, StreamRoutingConfig):
            raise LiveCEAUError(
                "stream_routing_config must be a StreamRoutingConfig."
            )
        if not stream_routing_config.processing_time_safety_emit_enabled:
            raise LiveCEAUError(
                "live CEAU requires processing_time_safety_emit_enabled=true."
            )
        if stream_routing_config.processing_time_safety_emit_after_seconds is None:
            raise LiveCEAUError(
                "live CEAU requires processing_time_safety_emit_after_seconds."
            )
        self._store = store
        self._config = stream_routing_config
        self._max_pending_age = timedelta(
            seconds=stream_routing_config.processing_time_safety_emit_after_seconds
        )
        try:
            self._core = CEAUUnitFormationCore(
                store=store,
                stream_routing_config=stream_routing_config,
            )
        except CEAUCoordinatorError as exc:
            raise LiveCEAUError(str(exc)) from exc
        self._latest_observation = _latest_live_observation_from_store(
            store=store,
            target_key=stream_routing_config.target_key,
        )

    @property
    def pending_units(self):
        return self._core.pending_units

    def latest_observation(self) -> WatermarkObservation | None:
        return self._latest_observation

    def observe_source_completeness(
        self,
        observation: SourceCompletenessObservation,
    ) -> WatermarkObservation:
        latest = append_live_source_completeness(
            store=self._store,
            observation=observation,
        )
        self._latest_observation = latest
        return latest

    def close_due_by_processing_time(
        self,
        *,
        current_time: datetime,
    ) -> tuple[CEAUUnitEmittedRecord, ...]:
        self._refresh_latest_observation()
        try:
            return self._core.close_due_by_processing_time(
                current_time=current_time,
                max_pending_age=self._max_pending_age,
                watermark_observation=self._latest_observation,
            )
        except CEAUCoordinatorError as exc:
            raise LiveCEAUError(str(exc)) from exc

    def route_live_event(
        self,
        route_input: StreamRoutingEvent,
        *,
        support_resolver: SupportSectionResolver | None = None,
    ) -> CEAUEventRouteResult:
        self._refresh_latest_observation()
        try:
            return self._core.route_event(
                route_input,
                watermark_observation=self._latest_observation,
                support_resolver=support_resolver,
            )
        except CEAUCoordinatorError as exc:
            raise LiveCEAUError(str(exc)) from exc

    def route_live_event_decision(
        self,
        route_input: StreamRoutingEvent,
        *,
        support_resolver: SupportSectionResolver | None = None,
    ) -> StreamRoutingDecision:
        return self.route_live_event(
            route_input,
            support_resolver=support_resolver,
        ).decision

    def _refresh_latest_observation(self) -> None:
        restored = _latest_live_observation_from_store(
            store=self._store,
            target_key=self._config.target_key,
        )
        if restored is not None:
            self._latest_observation = restored

def append_live_source_completeness(
    *,
    store: FileBackedCEAUStore,
    observation: SourceCompletenessObservation,
) -> WatermarkObservation:
    """Append live source completeness and its derived watermark observation."""
    if not isinstance(store, FileBackedCEAUStore):
        raise LiveCEAUError("store must be a FileBackedCEAUStore.")
    if not isinstance(observation, SourceCompletenessObservation):
        raise LiveCEAUError("observation must be a SourceCompletenessObservation.")
    source_record = CEAUSourceCompletenessObservedRecord(
        record_type="source_completeness_observed",
        record_id=_derive_source_completeness_record_id(observation),
        target_key=observation.target_key,
        recorded_at=observation.observed_at,
        source_observation=observation,
    )
    try:
        store.append_record(source_record)
        projection = store.rebuild_projection(target_key=observation.target_key)
    except FileBackedCEAUStoreError as exc:
        raise LiveCEAUError(str(exc)) from exc

    source_observations = {
        (item.source_name, item.completeness_domain): item
        for item in projection.latest_source_observations
    }
    source_observations[
        (observation.source_name, observation.completeness_domain)
    ] = observation

    domain_watermarks = _domain_watermarks_from_sources(
        tuple(source_observations.values())
    )
    latest = WatermarkObservation(
        target_key=observation.target_key,
        runtime_scope="live",
        recorded_at=observation.observed_at,
        domain_watermarks=domain_watermarks,
        source_observations=tuple(
            source_observations[key] for key in sorted(source_observations)
        ),
        overall_status=_overall_status(tuple(domain_watermarks.values())),
        reason_codes=("live_source_completeness_observed",),
    )
    watermark_record = CEAUWatermarkObservedRecord(
        record_type="watermark_observed",
        record_id=_derive_watermark_record_id(latest),
        target_key=observation.target_key,
        recorded_at=observation.observed_at,
        watermark_observation=latest,
    )
    try:
        store.append_record(watermark_record)
    except FileBackedCEAUStoreError as exc:
        raise LiveCEAUError(str(exc)) from exc
    return latest


def _latest_live_observation_from_store(
    *,
    store: FileBackedCEAUStore,
    target_key: str,
) -> WatermarkObservation | None:
    try:
        projection = store.rebuild_projection(target_key=target_key)
    except FileBackedCEAUStoreError as exc:
        raise LiveCEAUError(str(exc)) from exc
    if not projection.latest_domain_watermarks:
        return None
    domain_watermarks = {
        item.domain: item for item in projection.latest_domain_watermarks
    }
    return WatermarkObservation(
        target_key=target_key,
        runtime_scope="live",
        recorded_at=datetime.now(UTC),
        domain_watermarks=domain_watermarks,
        source_observations=projection.latest_source_observations,
        overall_status=_overall_status(tuple(domain_watermarks.values())),
        reason_codes=("live_watermark_restored_from_store",),
    )


def _domain_watermarks_from_sources(
    observations: tuple[SourceCompletenessObservation, ...],
) -> dict[CompletenessDomain, DomainWatermark]:
    by_domain: dict[CompletenessDomain, list[SourceCompletenessObservation]] = {}
    for observation in observations:
        by_domain.setdefault(observation.completeness_domain, []).append(observation)
    return {
        domain: _domain_watermark_from_sources(domain, tuple(items))
        for domain, items in by_domain.items()
    }


def _domain_watermark_from_sources(
    domain: CompletenessDomain,
    observations: tuple[SourceCompletenessObservation, ...],
) -> DomainWatermark:
    advanced_sources = tuple(
        item
        for item in observations
        if item.status == "healthy"
        and item.completeness_model == "complete_through"
        and item.complete_through_at is not None
    )
    if advanced_sources:
        watermark_candidates = tuple(
            item.complete_through_at
            for item in advanced_sources
            if item.complete_through_at is not None
        )
        watermark_at = max(watermark_candidates)
        return DomainWatermark(
            domain=domain,
            watermark_at=watermark_at,
            status="advanced",
            supporting_source_names=tuple(
                sorted(item.source_name for item in advanced_sources)
            ),
            source_models=tuple(
                sorted({item.completeness_model for item in advanced_sources})
            ),
            reason_codes=_merge_reason_codes(advanced_sources),
        )
    if observations and all(item.status == "unavailable" for item in observations):
        return DomainWatermark(
            domain=domain,
            watermark_at=None,
            status="unavailable",
            supporting_source_names=(),
            source_models=(),
            reason_codes=_merge_reason_codes(observations),
        )
    return DomainWatermark(
        domain=domain,
        watermark_at=None,
        status="frozen",
        supporting_source_names=(),
        source_models=(),
        reason_codes=_merge_reason_codes(observations),
    )


def _merge_reason_codes(
    observations: tuple[SourceCompletenessObservation, ...],
) -> tuple[str, ...]:
    values: list[str] = []
    for observation in observations:
        for reason_code in observation.reason_codes:
            if reason_code not in values:
                values.append(reason_code)
    return tuple(values) or ("source_completeness_observed",)


def _overall_status(watermarks: tuple[DomainWatermark, ...]) -> WatermarkOverallStatus:
    if not watermarks:
        return "frozen_no_source_completeness"
    statuses = {item.status for item in watermarks}
    if statuses == {"advanced"}:
        return "advanced"
    if "advanced" in statuses:
        return "partially_advanced"
    if statuses == {"unavailable"}:
        return "unavailable"
    return "frozen_no_source_completeness"


def _derive_source_completeness_record_id(
    observation: SourceCompletenessObservation,
) -> str:
    material = "|".join(
        (
            "event-trader::ceau::live-source-completeness",
            observation.source_name,
            observation.target_key,
            observation.observed_at.isoformat(),
            observation.completeness_domain,
            observation.completeness_model,
            observation.complete_through_at.isoformat()
            if observation.complete_through_at
            else "",
            observation.lower_bound_at.isoformat() if observation.lower_bound_at else "",
            observation.status,
            ";".join(observation.reason_codes),
        )
    )
    return f"source-completeness-{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _derive_watermark_record_id(observation: WatermarkObservation) -> str:
    watermark_parts = []
    for domain, watermark in sorted(observation.domain_watermarks.items()):
        watermark_parts.append(
            "|".join(
                (
                    domain,
                    watermark.status,
                    watermark.watermark_at.isoformat()
                    if watermark.watermark_at
                    else "",
                    ";".join(watermark.reason_codes),
                )
            )
        )
    material = "|".join(
        (
            "event-trader::ceau::live-watermark",
            observation.target_key,
            observation.recorded_at.isoformat(),
            ";".join(watermark_parts),
        )
    )
    return f"watermark-{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _normalize_live_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise LiveCEAUError(f"{field_name} must be a datetime.")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "LiveCEAUCoordinator",
    "LiveCEAUError",
    "append_live_source_completeness",
]
