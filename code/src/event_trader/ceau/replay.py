"""Replay-scoped CEAU watermark and timer coordination."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Final

from event_trader.ceau.contracts import (
    CEAUUnitEmittedRecord,
    CEAUUnitKey,
    CEAUWatermarkObservedRecord,
    CompletenessDomain,
    CompletenessRequirement,
    DomainWatermark,
    UnitFormationLane,
    UnitFormationLaneStatus,
    WatermarkObservation,
)
from event_trader.ceau.coordinator import (
    CEAUCoordinatorError,
    CEAUEventRouteResult,
    CEAUUnitFormationCore,
    PendingCEAUUnit,
)
from event_trader.ceau.policy import StreamRoutingConfig
from event_trader.ceau.routing import (
    StreamRoutingDecision,
    StreamRoutingEvent,
    SupportSectionResolver,
)
from event_trader.ceau.store import FileBackedCEAUStore, FileBackedCEAUStoreError
from event_trader.contracts import AnalysisRequest

_REPLAY_MARKET_DOMAIN: Final[CompletenessDomain] = "market_bars"


class ReplayCEAUError(ValueError):
    """Raised when replay CEAU timer/watermark coordination cannot proceed."""


PendingReplayUnit = PendingCEAUUnit
ReplayCEAUEventRouteResult = CEAUEventRouteResult


def build_analysis_request_from_unit_emitted_record(
    *,
    emitted_record: CEAUUnitEmittedRecord,
    why_escalated: str | None = None,
    requires_watchlist_maintenance: bool = False,
    decision_episode_id: str = "",
) -> AnalysisRequest:
    """Build the canonical analysis request for an emitted CEAU unit."""
    if not isinstance(emitted_record, CEAUUnitEmittedRecord):
        raise ReplayCEAUError("emitted_record must be a CEAUUnitEmittedRecord.")
    hint = why_escalated or (
        "CEAU emitted analysis unit "
        f"{emitted_record.analysis_unit_id} via {emitted_record.emission_reason}."
    )
    return AnalysisRequest(
        target_key=emitted_record.target_key,
        event_ids=list(emitted_record.event_ids),
        why_escalated=hint,
        requires_watchlist_maintenance=requires_watchlist_maintenance,
        decision_episode_id=decision_episode_id,
    )


def build_unit_formation_lane_from_unit_emitted_record(
    *,
    emitted_record: CEAUUnitEmittedRecord,
    unit_key: CEAUUnitKey | None = None,
    unit_type: str = "analysis",
    unit_status: UnitFormationLaneStatus = "emitted",
    opened_at: datetime | None = None,
    business_at: datetime | None = None,
    deadline_at: datetime | None = None,
    max_delay_bars: int | None = None,
    emitted_at: datetime | None = None,
    routing_reason_codes_by_event: tuple[str, ...] = (),
    completeness_requirements: tuple[CompletenessRequirement, ...] = (),
    advanced_completeness_requirements: tuple[str, ...] | None = None,
    missing_completeness_requirements: tuple[str, ...] | None = None,
    watermark_observation_ref: str | None = None,
    domain_watermark_summary: tuple[str, ...] = (),
    policy_status: str | None = None,
    unit_event_count: int | None = None,
) -> UnitFormationLane:
    """Build a compiled UnitFormationLane from an emitted unit record."""
    if not isinstance(emitted_record, CEAUUnitEmittedRecord):
        raise ReplayCEAUError("emitted_record must be a CEAUUnitEmittedRecord.")
    if unit_event_count is None:
        unit_event_count = len(emitted_record.event_ids)
    resolved_opened_at = emitted_record.opened_at if opened_at is None else opened_at
    resolved_business_at = emitted_record.business_at if business_at is None else business_at
    resolved_deadline_at = emitted_record.deadline_at if deadline_at is None else deadline_at
    resolved_max_delay_bars = (
        emitted_record.max_delay_bars if max_delay_bars is None else max_delay_bars
    )
    if resolved_max_delay_bars < 0:
        raise ReplayCEAUError("max_delay_bars must be non-negative.")

    return UnitFormationLane(
        analysis_unit_id=emitted_record.analysis_unit_id,
        unit_key=emitted_record.unit_key if unit_key is None else unit_key,
        unit_type=unit_type,
        unit_status=unit_status,
        opened_at=resolved_opened_at,
        emitted_at=emitted_record.recorded_at if emitted_at is None else emitted_at,
        business_at=resolved_business_at,
        event_ids=emitted_record.event_ids,
        primary_event_ids=emitted_record.primary_event_ids,
        context_event_ids=emitted_record.context_event_ids,
        unit_event_count=unit_event_count,
        emission_reason=emitted_record.emission_reason,
        emission_clock=emitted_record.emission_clock,
        event_time_completeness_claim=emitted_record.event_time_completeness_claim,
        routing_reason_codes_by_event=routing_reason_codes_by_event,
        deadline_at=resolved_deadline_at,
        max_delay_bars=resolved_max_delay_bars,
        completeness_requirements=completeness_requirements,
        advanced_completeness_requirements=(
            emitted_record.advanced_completeness_requirements
            if advanced_completeness_requirements is None
            else advanced_completeness_requirements
        ),
        missing_completeness_requirements=(
            emitted_record.missing_completeness_requirements
            if missing_completeness_requirements is None
            else missing_completeness_requirements
        ),
        watermark_observation_ref=watermark_observation_ref,
        domain_watermark_summary=domain_watermark_summary,
        policy_version=emitted_record.policy_version,
        policy_hash=emitted_record.policy_hash,
        policy_status=policy_status,
    )


class ReplayWatermark:
    """Replay watermark tracker that persists canonical watermark_observed records."""

    def __init__(self, *, store: FileBackedCEAUStore) -> None:
        if not isinstance(store, FileBackedCEAUStore):
            raise ReplayCEAUError("store must be a FileBackedCEAUStore.")
        self._store = store
        self._latest_watermark_at: dict[str, datetime] = {}
        self._latest_observation_by_target: dict[str, WatermarkObservation] = {}

    def observe_replay_event(
        self,
        *,
        target_key: str,
        event_ts: datetime,
        business_at: datetime,
    ) -> CEAUWatermarkObservedRecord:
        recorded_at = _normalize_datetime(event_ts, field_name="event_ts")
        candidate = _normalize_datetime(business_at, field_name="business_at")
        prior = self._latest_watermark_at.get(target_key)
        advanced = prior is None or candidate > prior
        watermark_at = candidate if advanced or prior is None else prior
        if advanced:
            domain_watermark = DomainWatermark(
                domain=_REPLAY_MARKET_DOMAIN,
                watermark_at=watermark_at,
                status="advanced",
                supporting_source_names=("replay_event_stream",),
                source_models=("complete_through",),
                reason_codes=("replay_watermark_advanced",),
            )
            reason_codes = ("replay_watermark_advanced",)
            self._latest_watermark_at[target_key] = watermark_at
        else:
            domain_watermark = DomainWatermark(
                domain=_REPLAY_MARKET_DOMAIN,
                watermark_at=watermark_at,
                status="advanced",
                supporting_source_names=("replay_event_stream",),
                source_models=("complete_through",),
                reason_codes=("replay_watermark_unchanged",),
            )
            reason_codes = ("replay_watermark_unchanged",)
        observation = WatermarkObservation(
            target_key=target_key,
            runtime_scope="replay",
            recorded_at=recorded_at,
            domain_watermarks={_REPLAY_MARKET_DOMAIN: domain_watermark},
            source_observations=(),
            overall_status="advanced",
            reason_codes=reason_codes,
        )
        record = CEAUWatermarkObservedRecord(
            record_type="watermark_observed",
            record_id=_derive_watermark_record_id(
                target_key=target_key,
                recorded_at=recorded_at,
                watermark_at=watermark_at,
                status=domain_watermark.status,
                reason_codes=reason_codes,
            ),
            target_key=target_key,
            recorded_at=recorded_at,
            watermark_observation=observation,
        )
        try:
            self._store.append_record(record)
        except FileBackedCEAUStoreError as exc:
            raise ReplayCEAUError(str(exc)) from exc
        self._latest_observation_by_target[target_key] = observation
        return record

    def latest_observation(self, *, target_key: str) -> WatermarkObservation | None:
        return self._latest_observation_by_target.get(target_key)


class ReplayCEAUCoordinator:
    """Replay coordinator for event-time watermark and final-drain semantics."""

    def __init__(
        self,
        *,
        store: FileBackedCEAUStore,
        stream_routing_config: StreamRoutingConfig,
    ) -> None:
        self._config = stream_routing_config
        self._watermark = ReplayWatermark(store=store)
        try:
            self._core = CEAUUnitFormationCore(
                store=store,
                stream_routing_config=stream_routing_config,
            )
        except CEAUCoordinatorError as exc:
            raise ReplayCEAUError(str(exc)) from exc

    @property
    def pending_units(self) -> tuple[PendingReplayUnit, ...]:
        return self._core.pending_units

    def observe_replay_event(
        self,
        *,
        target_key: str,
        event_ts: datetime,
        business_at: datetime,
    ) -> CEAUWatermarkObservedRecord:
        return self._watermark.observe_replay_event(
            target_key=target_key,
            event_ts=event_ts,
            business_at=business_at,
        )

    def observe_replay_time(
        self,
        *,
        replay_at: datetime,
    ) -> CEAUWatermarkObservedRecord | None:
        normalized_replay_at = _normalize_datetime(replay_at, field_name="replay_at")
        latest = self._watermark.latest_observation(target_key=self._config.target_key)
        latest_market = None
        if latest is not None:
            latest_market = latest.domain_watermarks.get(_REPLAY_MARKET_DOMAIN)
        if latest_market is not None and latest_market.watermark_at >= normalized_replay_at:
            return None
        return self._watermark.observe_replay_event(
            target_key=self._config.target_key,
            event_ts=normalized_replay_at,
            business_at=normalized_replay_at,
        )

    def route_replay_event(
        self,
        route_input: StreamRoutingEvent,
        *,
        support_resolver: SupportSectionResolver | None = None,
    ) -> StreamRoutingDecision:
        return self.route_replay_event_with_preclose(
            route_input,
            support_resolver=support_resolver,
        ).decision

    def route_replay_event_with_preclose(
        self,
        route_input: StreamRoutingEvent,
        *,
        support_resolver: SupportSectionResolver | None = None,
    ) -> ReplayCEAUEventRouteResult:
        self.observe_replay_event(
            target_key=route_input.target_key,
            event_ts=route_input.recorded_at,
            business_at=route_input.business_at,
        )
        observation = self._watermark.latest_observation(
            target_key=route_input.target_key
        )
        preclosed_records = self.pre_close_before_event(
            current_event_time=route_input.business_at,
        )
        route_result = self._core.route_event(
            route_input,
            watermark_observation=observation,
            support_resolver=support_resolver,
        )
        return ReplayCEAUEventRouteResult(
            preclosed_emitted_records=(
                *preclosed_records,
                *route_result.preclosed_emitted_records,
            ),
            decision=route_result.decision,
        )

    def pre_close_before_event(
        self,
        *,
        current_event_time: datetime,
    ) -> tuple[CEAUUnitEmittedRecord, ...]:
        try:
            return self._core.close_due_by_event_time(
                current_event_time=current_event_time,
                watermark_observation=self._watermark.latest_observation(
                    target_key=self._config.target_key
                ),
            )
        except CEAUCoordinatorError as exc:
            raise ReplayCEAUError(str(exc)) from exc

    def final_drain(
        self,
        *,
        replay_end_time: datetime,
    ) -> tuple[CEAUUnitEmittedRecord, ...]:
        try:
            return self._core.drain(
                recorded_at=replay_end_time,
                emission_reason="replay_final_drain",
                emission_clock="event_time",
                watermark_observation=self._watermark.latest_observation(
                    target_key=self._config.target_key
                ),
            )
        except CEAUCoordinatorError as exc:
            raise ReplayCEAUError(str(exc)) from exc


def _derive_watermark_record_id(
    *,
    target_key: str,
    recorded_at: datetime,
    watermark_at: datetime,
    status: str,
    reason_codes: tuple[str, ...],
) -> str:
    material = "|".join(
        (
            "event-trader::ceau::replay-watermark",
            target_key,
            recorded_at.isoformat(),
            watermark_at.isoformat(),
            status,
            ";".join(reason_codes),
        )
    )
    return f"watermark-{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _normalize_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ReplayCEAUError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReplayCEAUError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = [
    "PendingReplayUnit",
    "ReplayCEAUEventRouteResult",
    "ReplayCEAUCoordinator",
    "ReplayCEAUError",
    "ReplayWatermark",
    "build_analysis_request_from_unit_emitted_record",
    "build_unit_formation_lane_from_unit_emitted_record",
]
