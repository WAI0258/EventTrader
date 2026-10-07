"""Mode-neutral CEAU unit-formation coordinator core."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Final

from event_trader.ceau.contracts import (
    CEAUUnitAppendedRecord,
    CEAUUnitEmittedRecord,
    CEAUUnitKey,
    CEAUUnitOpenedRecord,
    CompletenessRequirement,
    EmissionClock,
    EmissionReason,
    WatermarkObservation,
)
from event_trader.ceau.policy import StreamRoutingConfig, StreamRoutingPolicy
from event_trader.ceau.routing import (
    StreamRoutingDecision,
    StreamRoutingEvent,
    SupportSectionResolver,
    resolve_routing_lane,
    route_event,
)
from event_trader.ceau.store import FileBackedCEAUStore, FileBackedCEAUStoreError

_GRANULARITY_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<count>[1-9][0-9]*)(?P<unit>m|min|h|d)$", re.IGNORECASE
)


class CEAUCoordinatorError(ValueError):
    """Raised when CEAU unit formation cannot proceed."""


@dataclass(frozen=True, slots=True)
class PendingCEAUUnit:
    """In-memory pending lane that must eventually emit a canonical receipt."""

    analysis_unit_id: str
    target_key: str
    scope_key: tuple[str, str, str, str]
    unit_key: CEAUUnitKey
    opened_at: datetime
    business_at: datetime
    deadline_at: datetime
    max_delay_bars: int
    unit_char_count: int
    event_ids: tuple[str, ...]
    primary_event_ids: tuple[str, ...]
    context_event_ids: tuple[str, ...]
    advanced_completeness_requirements: tuple[str, ...]
    missing_completeness_requirements: tuple[str, ...]
    event_time_completeness_claim: bool
    policy_version: str
    policy_hash: str


@dataclass(frozen=True, slots=True)
class CEAUEventRouteResult:
    """Route result plus units emitted before the current event route."""

    preclosed_emitted_records: tuple[CEAUUnitEmittedRecord, ...]
    decision: StreamRoutingDecision


class CEAUUnitFormationCore:
    """Shared pending-unit state machine for replay and live CEAU coordinators."""

    def __init__(
        self,
        *,
        store: FileBackedCEAUStore,
        stream_routing_config: StreamRoutingConfig,
    ) -> None:
        if not isinstance(store, FileBackedCEAUStore):
            raise CEAUCoordinatorError("store must be a FileBackedCEAUStore.")
        if not isinstance(stream_routing_config, StreamRoutingConfig):
            raise CEAUCoordinatorError(
                "stream_routing_config must be a StreamRoutingConfig."
            )
        self._store = store
        self._config = stream_routing_config
        self._pending_by_unit_id: dict[str, PendingCEAUUnit] = {}
        self._pending_by_scope_key: dict[tuple[str, str, str, str], str] = {}
        # An emitted unit is immutable business history.  Replaying its source
        # events must still produce the existing routing decision, but must not
        # recreate the pending unit and try to emit a second payload.
        self._emitted_unit_ids: set[str] = set()
        self._emitted_unit_id_by_event_id: dict[str, str] = {}
        self._restore_pending_units_from_store()

    @property
    def pending_units(self) -> tuple[PendingCEAUUnit, ...]:
        return tuple(
            sorted(
                self._pending_by_unit_id.values(),
                key=lambda item: (
                    item.deadline_at,
                    item.opened_at,
                    item.analysis_unit_id,
                ),
            )
        )

    def route_event(
        self,
        route_input: StreamRoutingEvent,
        *,
        watermark_observation: WatermarkObservation | None = None,
        support_resolver: SupportSectionResolver | None = None,
    ) -> CEAUEventRouteResult:
        if route_input.target_key != self._config.target_key:
            raise CEAUCoordinatorError(
                "route_input.target_key must match stream_routing_config.target_key."
            )
        lane = resolve_routing_lane(
            event=route_input,
            stream_routing_config=self._config,
        )
        unit_policy = self._unit_policy_for_lane(lane)
        scope_key = _scope_key(
            route_input=route_input,
            routing_lane=lane,
            unit_scope=unit_policy.unit_scope,
            default_market_anchor=f"{self._config.target_key}:{self._config.bar_granularity}",
        )
        budget_emitted_records: tuple[CEAUUnitEmittedRecord, ...] = ()
        already_emitted_unit_id = self._emitted_unit_id_by_event_id.get(
            route_input.event_id
        )
        existing_unit_id = self._pending_by_scope_key.get(scope_key)
        existing_unit = (
            None
            if existing_unit_id is None
            else self._pending_by_unit_id.get(existing_unit_id)
        )
        if (
            already_emitted_unit_id is None
            and existing_unit is not None
            and existing_unit.unit_char_count + route_input.content_char_count
            > unit_policy.max_unit_chars
        ):
            budget_emitted_records = (
                self.emit_pending_unit(
                    unit=existing_unit,
                    recorded_at=route_input.business_at,
                    emission_reason="context_budget",
                    emission_clock="event_time",
                    watermark_observation=watermark_observation,
                ),
            )
            existing_unit = None

        analysis_unit_id_override = already_emitted_unit_id
        if analysis_unit_id_override is None and existing_unit is not None:
            analysis_unit_id_override = existing_unit.analysis_unit_id
        if analysis_unit_id_override is None:
            analysis_unit_id_override = _derive_accumulate_analysis_unit_id(
                target_key=route_input.target_key,
                scope_key=scope_key,
                routing_lane=lane,
                opening_event_id=route_input.event_id,
                opening_business_at=route_input.business_at,
                policy_hash=self._config.policy_hash,
            )

        decision = route_event(
            route_input,
            stream_routing_config=self._config,
            store=self._store,
            watermark_observation=watermark_observation,
            support_resolver=support_resolver,
            analysis_unit_id_override=analysis_unit_id_override,
        )
        if decision.unit_emitted_record is not None:
            self._remember_emitted_unit(decision.unit_emitted_record)
        if decision.final_route == "accumulate":
            self._track_pending_unit(
                decision=decision,
                route_input=route_input,
                scope_key=scope_key,
            )
        return CEAUEventRouteResult(
            preclosed_emitted_records=budget_emitted_records,
            decision=decision,
        )

    def close_due_by_event_time(
        self,
        *,
        current_event_time: datetime,
        watermark_observation: WatermarkObservation | None = None,
    ) -> tuple[CEAUUnitEmittedRecord, ...]:
        current_time = _normalize_datetime(
            current_event_time,
            field_name="current_event_time",
        )
        return self._emit_units(
            units=tuple(unit for unit in self.pending_units if unit.deadline_at <= current_time),
            recorded_at=current_time,
            emission_reason="other",
            emission_clock="event_time",
            watermark_observation=watermark_observation,
        )

    def close_due_by_processing_time(
        self,
        *,
        current_time: datetime,
        max_pending_age: timedelta,
        watermark_observation: WatermarkObservation | None = None,
    ) -> tuple[CEAUUnitEmittedRecord, ...]:
        now = _normalize_datetime(current_time, field_name="current_time")
        if not isinstance(max_pending_age, timedelta) or max_pending_age.total_seconds() <= 0:
            raise CEAUCoordinatorError("max_pending_age must be a positive timedelta.")
        return self._emit_units(
            units=tuple(
                unit
                for unit in self.pending_units
                if unit.opened_at + max_pending_age <= now
            ),
            recorded_at=now,
            emission_reason="processing_time_safety_close",
            emission_clock="processing_time",
            watermark_observation=watermark_observation,
        )

    def drain(
        self,
        *,
        recorded_at: datetime,
        emission_reason: EmissionReason,
        emission_clock: EmissionClock,
        watermark_observation: WatermarkObservation | None = None,
    ) -> tuple[CEAUUnitEmittedRecord, ...]:
        return self._emit_units(
            units=self.pending_units,
            recorded_at=_normalize_datetime(recorded_at, field_name="recorded_at"),
            emission_reason=emission_reason,
            emission_clock=emission_clock,
            watermark_observation=watermark_observation,
        )

    def emit_pending_unit(
        self,
        *,
        unit: PendingCEAUUnit,
        recorded_at: datetime,
        emission_reason: EmissionReason,
        emission_clock: EmissionClock,
        watermark_observation: WatermarkObservation | None = None,
    ) -> CEAUUnitEmittedRecord:
        recorded_at_utc = _normalize_datetime(recorded_at, field_name="recorded_at")
        completeness = self._emission_completeness(
            unit,
            watermark_observation=watermark_observation,
        )
        event_time_completeness_claim = completeness[0]
        advanced = completeness[1]
        missing = completeness[2]
        if emission_reason == "processing_time_safety_close":
            event_time_completeness_claim = False
        emitted_record = CEAUUnitEmittedRecord(
            record_type="unit_emitted",
            record_id=_derive_unit_emitted_record_id(
                analysis_unit_id=unit.analysis_unit_id,
                emission_reason=emission_reason,
                recorded_at=recorded_at_utc,
            ),
            target_key=unit.target_key,
            recorded_at=recorded_at_utc,
            analysis_unit_id=unit.analysis_unit_id,
            unit_key=unit.unit_key,
            opened_at=unit.opened_at,
            business_at=unit.business_at,
            deadline_at=unit.deadline_at,
            max_delay_bars=unit.max_delay_bars,
            event_ids=unit.event_ids,
            primary_event_ids=unit.primary_event_ids,
            context_event_ids=unit.context_event_ids,
            emission_reason=emission_reason,
            emission_clock=emission_clock,
            event_time_completeness_claim=event_time_completeness_claim,
            advanced_completeness_requirements=advanced,
            missing_completeness_requirements=missing,
            policy_version=unit.policy_version,
            policy_hash=unit.policy_hash,
        )
        try:
            self._store.append_record(emitted_record)
        except FileBackedCEAUStoreError as exc:
            raise CEAUCoordinatorError(str(exc)) from exc
        self._remember_emitted_unit(emitted_record)
        self._pending_by_unit_id.pop(unit.analysis_unit_id, None)
        if self._pending_by_scope_key.get(unit.scope_key) == unit.analysis_unit_id:
            self._pending_by_scope_key.pop(unit.scope_key, None)
        return emitted_record

    def _emit_units(
        self,
        *,
        units: tuple[PendingCEAUUnit, ...],
        recorded_at: datetime,
        emission_reason: EmissionReason,
        emission_clock: EmissionClock,
        watermark_observation: WatermarkObservation | None,
    ) -> tuple[CEAUUnitEmittedRecord, ...]:
        emitted_records: list[CEAUUnitEmittedRecord] = []
        for unit in units:
            if unit.analysis_unit_id not in self._pending_by_unit_id:
                continue
            emitted_records.append(
                self.emit_pending_unit(
                    unit=unit,
                    recorded_at=recorded_at,
                    emission_reason=emission_reason,
                    emission_clock=emission_clock,
                    watermark_observation=watermark_observation,
                )
            )
        return tuple(emitted_records)

    def _track_pending_unit(
        self,
        *,
        decision: StreamRoutingDecision,
        route_input: StreamRoutingEvent,
        scope_key: tuple[str, str, str, str],
    ) -> None:
        analysis_unit_id = decision.analysis_unit_id
        if analysis_unit_id is None:
            raise CEAUCoordinatorError("accumulate route must include analysis_unit_id.")
        if analysis_unit_id in self._emitted_unit_ids:
            return
        existing_unit_id = self._pending_by_scope_key.get(scope_key)
        existing = (
            None
            if existing_unit_id is None
            else self._pending_by_unit_id.get(existing_unit_id)
        )
        if existing is not None:
            if route_input.event_id in existing.event_ids:
                return
            updated = PendingCEAUUnit(
                analysis_unit_id=existing.analysis_unit_id,
                target_key=existing.target_key,
                scope_key=existing.scope_key,
                unit_key=existing.unit_key,
                opened_at=existing.opened_at,
                business_at=route_input.business_at.astimezone(UTC),
                deadline_at=existing.deadline_at,
                max_delay_bars=existing.max_delay_bars,
                unit_char_count=existing.unit_char_count + route_input.content_char_count,
                event_ids=(*existing.event_ids, route_input.event_id),
                primary_event_ids=(*existing.primary_event_ids, route_input.event_id),
                context_event_ids=_stable_union(
                    existing.context_event_ids,
                    tuple(route_input.explicit_support_refs),
                ),
                advanced_completeness_requirements=existing.advanced_completeness_requirements,
                missing_completeness_requirements=existing.missing_completeness_requirements,
                event_time_completeness_claim=existing.event_time_completeness_claim,
                policy_version=existing.policy_version,
                policy_hash=existing.policy_hash,
            )
            self._pending_by_unit_id[existing.analysis_unit_id] = updated
            self._append_unit_appended_record(
                unit=updated,
                recorded_at=route_input.recorded_at,
            )
            return

        unit_policy = self._unit_policy_for_lane(decision.routing_lane)
        business_at = route_input.business_at.astimezone(UTC)
        deadline_at = _compute_deadline_at(
            business_at=business_at,
            max_delay_bars=unit_policy.max_delay_bars,
            bar_granularity=self._config.bar_granularity,
        )
        unit_key = CEAUUnitKey(
            target_key=route_input.target_key,
            routing_lane=(
                unit_policy.unit_scope
                if unit_policy.unit_scope != "lane"
                else decision.routing_lane
            ),
            event_family=route_input.event_family or "event",
            memory_anchor=route_input.memory_anchor or f"target:{route_input.target_key}",
            market_anchor=(
                route_input.market_anchor
                or f"{self._config.target_key}:{self._config.bar_granularity}"
            ),
        )
        pending_unit = PendingCEAUUnit(
            analysis_unit_id=analysis_unit_id,
            target_key=route_input.target_key,
            scope_key=scope_key,
            unit_key=unit_key,
            opened_at=_normalize_datetime(
                route_input.recorded_at,
                field_name="route_input.recorded_at",
            ),
            business_at=business_at,
            deadline_at=deadline_at,
            max_delay_bars=unit_policy.max_delay_bars,
            unit_char_count=route_input.content_char_count,
            event_ids=(route_input.event_id,),
            primary_event_ids=(route_input.event_id,),
            context_event_ids=tuple(route_input.explicit_support_refs),
            advanced_completeness_requirements=(),
            missing_completeness_requirements=(),
            event_time_completeness_claim=False,
            policy_version=self._config.policy_version,
            policy_hash=self._config.policy_hash,
        )
        self._pending_by_unit_id[analysis_unit_id] = pending_unit
        self._pending_by_scope_key[scope_key] = analysis_unit_id
        self._append_unit_opened_record(unit=pending_unit)

    def _append_unit_opened_record(self, *, unit: PendingCEAUUnit) -> None:
        record = CEAUUnitOpenedRecord(
            record_type="unit_opened",
            record_id=_derive_unit_opened_record_id(unit.analysis_unit_id),
            target_key=unit.target_key,
            recorded_at=unit.opened_at,
            analysis_unit_id=unit.analysis_unit_id,
            unit_key=unit.unit_key,
            scope_key=unit.scope_key,
            opened_at=unit.opened_at,
            business_at=unit.business_at,
            deadline_at=unit.deadline_at,
            max_delay_bars=unit.max_delay_bars,
            unit_char_count=unit.unit_char_count,
            event_ids=unit.event_ids,
            primary_event_ids=unit.primary_event_ids,
            context_event_ids=unit.context_event_ids,
            event_time_completeness_claim=unit.event_time_completeness_claim,
            advanced_completeness_requirements=unit.advanced_completeness_requirements,
            missing_completeness_requirements=unit.missing_completeness_requirements,
            policy_version=unit.policy_version,
            policy_hash=unit.policy_hash,
        )
        try:
            self._store.append_record(record)
        except FileBackedCEAUStoreError as exc:
            raise CEAUCoordinatorError(str(exc)) from exc

    def _append_unit_appended_record(
        self,
        *,
        unit: PendingCEAUUnit,
        recorded_at: datetime,
    ) -> None:
        record = CEAUUnitAppendedRecord(
            record_type="unit_appended",
            record_id=_derive_unit_appended_record_id(
                analysis_unit_id=unit.analysis_unit_id,
                event_id=unit.event_ids[-1],
            ),
            target_key=unit.target_key,
            recorded_at=recorded_at,
            analysis_unit_id=unit.analysis_unit_id,
            unit_key=unit.unit_key,
            scope_key=unit.scope_key,
            opened_at=unit.opened_at,
            business_at=unit.business_at,
            deadline_at=unit.deadline_at,
            max_delay_bars=unit.max_delay_bars,
            unit_char_count=unit.unit_char_count,
            event_ids=unit.event_ids,
            primary_event_ids=unit.primary_event_ids,
            context_event_ids=unit.context_event_ids,
            event_time_completeness_claim=unit.event_time_completeness_claim,
            advanced_completeness_requirements=unit.advanced_completeness_requirements,
            missing_completeness_requirements=unit.missing_completeness_requirements,
            policy_version=unit.policy_version,
            policy_hash=unit.policy_hash,
        )
        try:
            self._store.append_record(record)
        except FileBackedCEAUStoreError as exc:
            raise CEAUCoordinatorError(str(exc)) from exc

    def _restore_pending_units_from_store(self) -> None:
        try:
            persisted_records = self._store.read_records(target_key=self._config.target_key)
        except FileBackedCEAUStoreError as exc:
            raise CEAUCoordinatorError(str(exc)) from exc

        for persisted in persisted_records:
            record = persisted.record
            if isinstance(record, (CEAUUnitOpenedRecord, CEAUUnitAppendedRecord)):
                if record.analysis_unit_id in self._emitted_unit_ids:
                    continue
                unit = pending_unit_from_lifecycle_record(record)
                self._pending_by_unit_id[unit.analysis_unit_id] = unit
                self._pending_by_scope_key[unit.scope_key] = unit.analysis_unit_id
                continue
            if isinstance(record, CEAUUnitEmittedRecord):
                self._remember_emitted_unit(record)
                existing = self._pending_by_unit_id.pop(record.analysis_unit_id, None)
                if (
                    existing is not None
                    and self._pending_by_scope_key.get(existing.scope_key)
                    == record.analysis_unit_id
                ):
                    self._pending_by_scope_key.pop(existing.scope_key, None)

    def _remember_emitted_unit(self, record: CEAUUnitEmittedRecord) -> None:
        self._emitted_unit_ids.add(record.analysis_unit_id)
        for event_id in record.event_ids:
            existing = self._emitted_unit_id_by_event_id.setdefault(
                event_id,
                record.analysis_unit_id,
            )
            if existing != record.analysis_unit_id:
                raise CEAUCoordinatorError(
                    "persisted emitted CEAU units assign one event to different units: "
                    f"event_id={event_id} first_unit={existing} "
                    f"second_unit={record.analysis_unit_id}"
                )

    def _unit_policy_for_lane(self, routing_lane: str) -> StreamRoutingPolicy:
        return self._config.lane_policies.get(
            routing_lane,
            self._config.default_unit_policy,
        )

    def _emission_completeness(
        self,
        unit: PendingCEAUUnit,
        *,
        watermark_observation: WatermarkObservation | None,
    ) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
        policy = self._unit_policy_for_lane(unit.unit_key.routing_lane)
        if not policy.completeness_requirements:
            return False, (), ()
        advanced: list[str] = []
        missing: list[str] = []
        for requirement in policy.completeness_requirements:
            names = tuple(requirement.required_for)
            if _requirement_satisfied_for_unit(
                requirement=requirement,
                unit=unit,
                watermark_observation=watermark_observation,
            ):
                advanced.extend(names)
            else:
                missing.extend(names)
        return not missing, tuple(advanced), tuple(missing)


def pending_unit_from_lifecycle_record(
    record: CEAUUnitOpenedRecord | CEAUUnitAppendedRecord,
) -> PendingCEAUUnit:
    return PendingCEAUUnit(
        analysis_unit_id=record.analysis_unit_id,
        target_key=record.target_key,
        scope_key=record.scope_key,
        unit_key=record.unit_key,
        opened_at=record.opened_at,
        business_at=record.business_at,
        deadline_at=record.deadline_at,
        max_delay_bars=record.max_delay_bars,
        unit_char_count=record.unit_char_count,
        event_ids=record.event_ids,
        primary_event_ids=record.primary_event_ids,
        context_event_ids=record.context_event_ids,
        advanced_completeness_requirements=record.advanced_completeness_requirements,
        missing_completeness_requirements=record.missing_completeness_requirements,
        event_time_completeness_claim=record.event_time_completeness_claim,
        policy_version=record.policy_version,
        policy_hash=record.policy_hash,
    )


def _derive_accumulate_analysis_unit_id(
    *,
    target_key: str,
    scope_key: tuple[str, str, str, str],
    routing_lane: str,
    opening_event_id: str,
    opening_business_at: datetime,
    policy_hash: str,
) -> str:
    material = "|".join(
        (
            "event-trader::ceau::unit-open",
            target_key,
            routing_lane,
            scope_key[0],
            scope_key[1],
            scope_key[2],
            scope_key[3],
            opening_event_id,
            opening_business_at.astimezone(UTC).isoformat(),
            policy_hash,
        )
    )
    return f"unit-{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _derive_unit_opened_record_id(analysis_unit_id: str) -> str:
    return f"unit-opened-{sha256(analysis_unit_id.encode('utf-8')).hexdigest()[:24]}"


def _derive_unit_appended_record_id(
    *,
    analysis_unit_id: str,
    event_id: str,
) -> str:
    material = f"{analysis_unit_id}:{event_id}"
    return f"unit-appended-{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _derive_unit_emitted_record_id(
    *,
    analysis_unit_id: str,
    emission_reason: str,
    recorded_at: datetime,
) -> str:
    material = "|".join(
        (
            "event-trader::ceau::unit-close",
            analysis_unit_id,
            emission_reason,
            recorded_at.isoformat(),
        )
    )
    return f"unit-emitted-{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _scope_key(
    *,
    route_input: StreamRoutingEvent,
    routing_lane: str,
    unit_scope: str,
    default_market_anchor: str,
) -> tuple[str, str, str, str]:
    memory_anchor = route_input.memory_anchor or f"target:{route_input.target_key}"
    market_anchor = route_input.market_anchor or default_market_anchor
    if unit_scope == "context_window":
        return (route_input.target_key, "context_window", memory_anchor, market_anchor)
    event_family = route_input.event_family or "event"
    business_at = _normalize_datetime(route_input.business_at, field_name="business_at")
    hour_bucket = business_at.replace(minute=0, second=0, microsecond=0)
    lane_scope = f"{routing_lane}:{event_family}:{hour_bucket.isoformat()}"
    return (route_input.target_key, lane_scope, memory_anchor, market_anchor)


def _compute_deadline_at(
    *,
    business_at: datetime,
    max_delay_bars: int,
    bar_granularity: str,
) -> datetime:
    if max_delay_bars <= 0:
        return business_at
    return business_at + (parse_bar_granularity(bar_granularity) * max_delay_bars)


def parse_bar_granularity(raw_value: str) -> timedelta:
    if not isinstance(raw_value, str):
        raise CEAUCoordinatorError("bar_granularity must be a string.")
    normalized = raw_value.strip()
    match = _GRANULARITY_RE.fullmatch(normalized)
    if match is None:
        raise CEAUCoordinatorError(
            "bar_granularity must match '<N>m', '<N>min', '<N>h', or '<N>d'."
        )
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    if unit in {"m", "min"}:
        return timedelta(minutes=count)
    if unit == "h":
        return timedelta(hours=count)
    if unit == "d":
        return timedelta(days=count)
    raise CEAUCoordinatorError(
        "bar_granularity must match '<N>m', '<N>min', '<N>h', or '<N>d'."
    )


def _stable_union(existing: tuple[str, ...], incoming: tuple[str, ...]) -> tuple[str, ...]:
    values = list(existing)
    for item in incoming:
        if item not in values:
            values.append(item)
    return tuple(values)


def _requirement_satisfied_for_unit(
    *,
    requirement: CompletenessRequirement,
    unit: PendingCEAUUnit,
    watermark_observation: WatermarkObservation | None,
) -> bool:
    if watermark_observation is None:
        return False
    watermark = watermark_observation.domain_watermarks.get(requirement.domain)
    if watermark is None or watermark.status != "advanced":
        return False
    if watermark.watermark_at is None:
        return False
    deadline = requirement.deadline_at or unit.deadline_at
    return watermark.watermark_at >= deadline


def _normalize_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise CEAUCoordinatorError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise CEAUCoordinatorError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


__all__ = [
    "CEAUCoordinatorError",
    "CEAUEventRouteResult",
    "CEAUUnitFormationCore",
    "PendingCEAUUnit",
    "parse_bar_granularity",
    "pending_unit_from_lifecycle_record",
]
