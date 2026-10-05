"""Deterministic stream-routing engine for CEAU admission payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Final, cast

from event_trader.ceau.contracts import (
    CandidateRoute,
    CEAUEventRouteRecord,
    CEAURoute,
    CEAUUnitEmittedRecord,
    CEAUUnitKey,
    CompletenessDomain,
    CompletenessPurpose,
    WatermarkObservation,
)
from event_trader.ceau.policy import StreamRoutingConfig, StreamRoutingPolicy

_SALT: Final[str] = "event-trader::ceu::stream-routing"
_ABSENCE_BASED_REASON_PREFIXES: Final[tuple[str, ...]] = (
    "no_followup",
    "no_news_followup",
    "no_market_confirmation",
)
_PURPOSE_SCOPE_FOR_ABSENCE_REASONS: Final[dict[str, CompletenessPurpose]] = {
    "no_followup": "absence_inference",
    "no_news_followup": "absence_inference",
    "no_market_confirmation": "evidence_maturity",
}
_HARDCODED_INTERRUPT_LANES: Final[frozenset[str]] = frozenset(
    {"operator_interrupt", "risk_interrupt"}
)
_WATCH_OR_ACCUMULATE_LANES: Final[frozenset[str]] = frozenset(
    {"context_window"}
)


class StreamRoutingError(ValueError):
    """Raised when stream-routing inputs or policy evaluation cannot continue."""


@dataclass(frozen=True, slots=True)
class SupportSection:
    """Minimal support section index unit."""

    section_id: str
    section_title: str


@dataclass(frozen=True, slots=True)
class ResolvedSupportSection:
    """Resolved support section identity."""

    section_id: str
    section_title: str
    request_alias: str


class SupportSectionResolver:
    """Deterministic support section lookup without LLM dependence."""

    def __init__(self, *, sections: tuple[SupportSection, ...]) -> None:
        self._sections = _require_sections(sections)

    def resolve(self, alias: str) -> ResolvedSupportSection:
        candidates = _normalize_str_list((alias,))
        alias_value = candidates[0]
        if alias_value in {section.section_id for section in self._sections}:
            section = _lookup_exact(self._sections, alias_value)
            return ResolvedSupportSection(
                section_id=section.section_id,
                section_title=section.section_title,
                request_alias=alias_value,
            )

        basename_alias = Path(alias_value).name
        basename_matches = [
            section
            for section in self._sections
            if Path(section.section_id).name == basename_alias
        ]
        if len(basename_matches) == 1:
            section = basename_matches[0]
            return ResolvedSupportSection(
                section_id=section.section_id,
                section_title=section.section_title,
                request_alias=alias_value,
            )

        if len(basename_matches) > 1:
            raise StreamRoutingError(
                f"ambiguous support section alias: {alias_value!r}"
            )

        title_alias_matches = [
            section
            for section in self._sections
            if section.section_title == alias_value
        ]
        if len(title_alias_matches) == 1:
            section = title_alias_matches[0]
            return ResolvedSupportSection(
                section_id=section.section_id,
                section_title=section.section_title,
                request_alias=alias_value,
            )
        if len(title_alias_matches) > 1:
            raise StreamRoutingError(
                f"ambiguous support section alias: {alias_value!r}"
            )
        raise StreamRoutingError(f"unsupported support section alias: {alias_value!r}")

    def resolve_all(
        self,
        aliases: tuple[str, ...] | list[str] | None,
    ) -> tuple[ResolvedSupportSection, ...]:
        normalized = _normalize_str_list(aliases)
        return tuple(self.resolve(alias) for alias in normalized)


@dataclass(frozen=True, slots=True)
class StreamRoutingEvent:
    """Normalized event-like payload entering stream-routing."""

    event_id: str
    target_key: str
    business_at: datetime
    recorded_at: datetime
    event_type: str
    source_kind: str
    routing_lane: str | None = None
    event_family: str | None = None
    evidence_role: str | None = None
    reason_codes: tuple[str, ...] = ()
    support_sections: tuple[str, ...] = ()
    explicit_support_refs: tuple[str, ...] = ()
    raw_route_hint: str | None = None
    market_anchor: str | None = None
    memory_anchor: str | None = None
    content_char_count: int = 0


@dataclass(frozen=True, slots=True)
class StreamRoutingDecision:
    """Material output of one stream route attempt."""

    input: StreamRoutingEvent
    routing_lane: str
    final_route: CEAURoute
    route_reason_codes: tuple[str, ...]
    analysis_unit_id: str | None
    watch_id: str | None
    event_route_record: CEAUEventRouteRecord
    unit_emitted_record: CEAUUnitEmittedRecord | None
    resolved_support_sections: tuple[ResolvedSupportSection, ...]


def route_event(
    route_input: StreamRoutingEvent,
    *,
    stream_routing_config: StreamRoutingConfig,
    store: object,
    watermark_observation: WatermarkObservation | None = None,
    support_resolver: SupportSectionResolver | None = None,
    analysis_unit_id_override: str | None = None,
) -> StreamRoutingDecision:
    """Evaluate one event and append a CEAU event_route receipt."""
    if not isinstance(stream_routing_config, StreamRoutingConfig):
        raise StreamRoutingError("stream_routing_config must be a StreamRoutingConfig.")
    if not stream_routing_config.enabled:
        raise StreamRoutingError("stream_routing is disabled in config.")

    from event_trader.ceau.store import FileBackedCEAUStore, FileBackedCEAUStoreError

    if not isinstance(store, FileBackedCEAUStore):
        raise StreamRoutingError("store must be a FileBackedCEAUStore.")
    if route_input is None:
        raise StreamRoutingError("route_input is required.")

    normalized_input = _normalize_route_input(route_input)
    lane = resolve_routing_lane(
        event=normalized_input,
        stream_routing_config=stream_routing_config,
    )
    policy = _get_lane_policy(stream_routing_config, lane)
    lane_route: CEAURoute | None
    if lane in _HARDCODED_INTERRUPT_LANES or (
        normalized_input.event_type in stream_routing_config.hard_emit_event_types
    ):
        lane_route = "emit"
    elif stream_routing_config.hard_emit_source_kinds:
        lane_route = (
            "emit"
            if normalized_input.source_kind in stream_routing_config.hard_emit_source_kinds
            else None
        )
    else:
        lane_route = None

    raw_candidate = _normalize_candidate_hint(normalized_input.raw_route_hint)
    if raw_candidate is None:
        raw_candidate = policy.candidate_default_route
    if (
        raw_candidate not in policy.allowed_candidate_routes
        and raw_candidate != policy.candidate_default_route
    ):
        raise StreamRoutingError(
            f"raw route hint {raw_candidate!r} is not allowed for lane {lane!r}."
        )

    candidate_route = _resolve_candidate_route(
        policy=policy,
        candidate=raw_candidate,
        lane=lane,
    )

    if lane_route is not None:
        final_route = lane_route
    else:
        final_route = candidate_route

    resolved_support_sections: tuple[ResolvedSupportSection, ...] = ()
    if normalized_input.support_sections:
        if support_resolver is None:
            raise StreamRoutingError("support sections require a support_resolver.")
        resolved_support_sections = support_resolver.resolve_all(
            list(normalized_input.support_sections)
        )

    route_reason_codes = _stable_unique_tuple(normalized_input.reason_codes)
    if not route_reason_codes:
        route_reason_codes = (f"routing_lane_{lane}",)

    if final_route == "no_action":
        if _is_absence_or_maturity_reasoned(route_reason_codes):
            if not _supports_absence_no_action(
                route_input=normalized_input,
                policy=policy,
                watermark_observation=watermark_observation,
            ):
                final_route = "watch"
                route_reason_codes = (
                    *route_reason_codes,
                    "downgraded_no_action_to_watch_without_absence_support",
                )

    analysis_unit_id: str | None = None
    watch_id: str | None = None
    unit_emitted_record = None
    emit_single_event = final_route == "emit"
    if final_route in {"emit", "accumulate"}:
        if final_route == "accumulate" and analysis_unit_id_override is not None:
            analysis_unit_id = _require_non_empty_text(
                analysis_unit_id_override,
                "analysis_unit_id_override",
            )
        else:
            memory_anchor = (
                normalized_input.memory_anchor
                or f"target:{normalized_input.target_key}"
            )
            use_scoped_accumulate_unit = (
                final_route == "accumulate" and policy.unit_scope != "lane"
            )
            analysis_unit_id = _derive_analysis_unit_id(
                target_key=normalized_input.target_key,
                routing_lane=policy.unit_scope if use_scoped_accumulate_unit else lane,
                event_family=(
                    policy.unit_scope
                    if use_scoped_accumulate_unit
                    else normalized_input.event_family or "event"
                ),
                event_time=normalized_input.business_at,
                event_id=normalized_input.event_id if emit_single_event else None,
                market_anchor=(
                    normalized_input.market_anchor
                    or _derive_market_anchor_from_config(stream_routing_config)
                ),
                memory_anchor=memory_anchor,
                include_event_time=not use_scoped_accumulate_unit,
            )
    if final_route == "emit":
        if analysis_unit_id is None:
            raise StreamRoutingError("emit route must include analysis_unit_id.")
        unit_emitted_record = CEAUUnitEmittedRecord(
            record_type="unit_emitted",
            record_id=_derive_unit_emitted_record_id(
                analysis_unit_id=analysis_unit_id,
                event_id=normalized_input.event_id,
            ),
            target_key=normalized_input.target_key,
            recorded_at=normalized_input.recorded_at,
            analysis_unit_id=analysis_unit_id,
            unit_key=CEAUUnitKey(
                target_key=normalized_input.target_key,
                routing_lane=lane,
                event_family=normalized_input.event_family or "event",
                memory_anchor=(
                    normalized_input.memory_anchor
                    or f"target:{normalized_input.target_key}"
                ),
                market_anchor=(
                    normalized_input.market_anchor
                    or _derive_market_anchor_from_config(stream_routing_config)
                ),
            ),
            opened_at=normalized_input.recorded_at,
            business_at=normalized_input.business_at,
            deadline_at=normalized_input.business_at,
            max_delay_bars=0,
            event_ids=(normalized_input.event_id,),
            primary_event_ids=(normalized_input.event_id,),
            context_event_ids=(tuple(normalized_input.explicit_support_refs)),
            emission_reason="hard_trigger",
            emission_clock="event_time",
            event_time_completeness_claim=_route_is_event_time_complete(
                final_route=final_route,
                policy=policy,
                watermark_observation=watermark_observation,
            ),
            advanced_completeness_requirements=_route_requirement_names(
                policy=policy,
                watermark_observation=watermark_observation,
                satisfied_only=True,
            ),
            missing_completeness_requirements=_route_requirement_names(
                policy=policy,
                watermark_observation=watermark_observation,
                satisfied_only=False,
            ),
            policy_version=stream_routing_config.policy_version,
            policy_hash=stream_routing_config.policy_hash,
        )

    if final_route == "watch":
        watch_id = _derive_watch_id(
            route_input=normalized_input,
            lane=lane,
        )

    event_route_record = CEAUEventRouteRecord(
        record_type="event_route",
        record_id=_derive_route_record_id(
            route_input=normalized_input,
            routing_lane=lane,
            final_route=final_route,
            reasons=route_reason_codes,
            policy_version=stream_routing_config.policy_version,
            policy_hash=stream_routing_config.policy_hash,
        ),
        target_key=normalized_input.target_key,
        recorded_at=normalized_input.recorded_at,
        event_id=normalized_input.event_id,
        routing_lane=lane,
        final_route=final_route,
        routing_reason_codes=route_reason_codes,
        analysis_unit_id=analysis_unit_id,
        watch_id=watch_id,
        policy_version=stream_routing_config.policy_version,
        policy_hash=stream_routing_config.policy_hash,
    )

    try:
        store.append_record(event_route_record)
    except FileBackedCEAUStoreError as exc:
        raise StreamRoutingError(str(exc)) from exc

    if unit_emitted_record is not None:
        try:
            store.append_record(unit_emitted_record)
        except FileBackedCEAUStoreError as exc:
            raise StreamRoutingError(str(exc)) from exc

    return StreamRoutingDecision(
        input=normalized_input,
        routing_lane=lane,
        final_route=final_route,
        route_reason_codes=route_reason_codes,
        analysis_unit_id=analysis_unit_id,
        watch_id=watch_id,
        event_route_record=event_route_record,
        unit_emitted_record=unit_emitted_record,
        resolved_support_sections=resolved_support_sections,
    )


def _resolve_routing_lane(
    *,
    event: StreamRoutingEvent,
    default_lane: str,
    hard_emit_event_types: tuple[str, ...],
) -> str:
    if event.evidence_role == "operator_interrupt":
        return "operator_interrupt"
    if event.evidence_role == "risk_interrupt":
        return "risk_interrupt"
    if event.evidence_role == "news_interrupt":
        return "news_interrupt"
    if event.evidence_role in {"duplicate", "already_covered", "episode_append"}:
        return "episode_append"
    if event.routing_lane:
        return _require_non_empty_text(event.routing_lane, "routing_lane")
    if event.event_type in hard_emit_event_types:
        return "news_interrupt"
    return default_lane


def resolve_routing_lane(
    *,
    event: StreamRoutingEvent,
    stream_routing_config: StreamRoutingConfig,
) -> str:
    """Resolve the routing lane with the same deterministic policy used by route_event."""
    return _resolve_routing_lane(
        event=event,
        default_lane="context_window",
        hard_emit_event_types=stream_routing_config.hard_emit_event_types,
    )


def _get_lane_policy(
    config: StreamRoutingConfig,
    lane: str,
) -> StreamRoutingPolicy:
    return config.lane_policies.get(lane, config.default_unit_policy)


def _normalize_candidate_hint(candidate: str | None) -> CandidateRoute | None:
    if candidate is None:
        return None
    normalized = candidate.strip()
    if not normalized:
        return None
    if normalized not in {
        "watch_or_accumulate",
        "watch",
        "accumulate",
        "emit",
        "no_action",
    }:
        raise StreamRoutingError(f"unsupported route hint: {candidate!r}")
    return cast(CandidateRoute, normalized)


def _resolve_candidate_route(
    *,
    policy: StreamRoutingPolicy,
    candidate: CandidateRoute,
    lane: str,
) -> CEAURoute:
    if candidate == "watch_or_accumulate":
        return "watch" if lane in _WATCH_OR_ACCUMULATE_LANES else "accumulate"
    if candidate == "emit":
        return "emit"
    if candidate == "watch":
        return "watch"
    if candidate == "accumulate":
        return "accumulate"
    if candidate == "no_action":
        return "no_action"
    raise StreamRoutingError(f"unsupported candidate route: {candidate!r}")


def _is_absence_or_maturity_reasoned(reason_codes: tuple[str, ...]) -> bool:
    return any(
        reason in _ABSENCE_BASED_REASON_PREFIXES
        for reason in reason_codes
    )


def _supports_absence_no_action(
    *,
    route_input: StreamRoutingEvent,
    policy: StreamRoutingPolicy,
    watermark_observation: WatermarkObservation | None,
) -> bool:
    if route_input.explicit_support_refs:
        return True
    for reason in route_input.reason_codes:
        if reason not in _ABSENCE_BASED_REASON_PREFIXES:
            continue
        purpose = _PURPOSE_SCOPE_FOR_ABSENCE_REASONS.get(reason)
        if purpose is None:
            continue
        if _has_domain_advanced_support(
            policy=policy,
            purpose=purpose,
            reason_code=reason,
            watermark_observation=watermark_observation,
        ):
            return True
    return False


def _has_domain_advanced_support(
    *,
    policy: StreamRoutingPolicy,
    purpose: CompletenessPurpose,
    reason_code: str,
    watermark_observation: WatermarkObservation | None,
) -> bool:
    requirements = tuple(
        req
        for req in policy.completeness_requirements
        if req.purpose == purpose and reason_code in req.required_for
    )
    if not requirements:
        return False
    if watermark_observation is None:
        return False
    for requirement in requirements:
        watermark = watermark_observation.domain_watermarks.get(
            _coerce_completeness_domain(requirement.domain),
        )
        if watermark is None:
            continue
        if watermark.status != "advanced":
            continue
        if watermark.watermark_at is None:
            continue
        if (
            requirement.deadline_at is not None
            and watermark.watermark_at < requirement.deadline_at
        ):
            continue
        return True
    return False


def _requirement_is_satisfied(
    *,
    requirement,
    watermark_observation: WatermarkObservation | None,
) -> bool:
    if watermark_observation is None:
        return False
    watermark = watermark_observation.domain_watermarks.get(
        _coerce_completeness_domain(requirement.domain),
    )
    if watermark is None or watermark.status != "advanced":
        return False
    if (
        requirement.deadline_at is not None
        and watermark.watermark_at < requirement.deadline_at
    ):
        return False
    return True


def _route_requirement_names(
    *,
    policy: StreamRoutingPolicy,
    watermark_observation: WatermarkObservation | None,
    satisfied_only: bool,
) -> tuple[str, ...]:
    names: list[str] = []
    for requirement in policy.completeness_requirements:
        if _requirement_is_satisfied(
            requirement=requirement,
            watermark_observation=watermark_observation,
        ) == satisfied_only:
            names.extend(requirement.required_for)
    return tuple(names)


def _derive_analysis_unit_id(
    *,
    target_key: str,
    routing_lane: str,
    event_family: str,
    event_time: datetime,
    event_id: str | None,
    market_anchor: str,
    memory_anchor: str,
    include_event_time: bool = True,
) -> str:
    material_parts = [
        _SALT,
        target_key,
        routing_lane,
        event_family,
        memory_anchor,
        market_anchor,
    ]
    if include_event_time:
        material_parts.append(
            event_time.astimezone(UTC).replace(
                minute=0,
                second=0,
                microsecond=0,
            ).isoformat()
        )
    if event_id is not None:
        material_parts.append(event_id)
    material = "|".join(material_parts)
    return f"unit-{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _derive_unit_emitted_record_id(
    *,
    analysis_unit_id: str,
    event_id: str,
) -> str:
    return f"unit-emitted-{sha256(f'{analysis_unit_id}:{event_id}'.encode()).hexdigest()[:24]}"


def _derive_watch_id(
    *,
    route_input: StreamRoutingEvent,
    lane: str,
):
    material = "|".join(
        (
            _SALT,
            route_input.target_key,
            route_input.event_id,
            lane,
            route_input.business_at.astimezone(UTC).date().isoformat(),
        )
    )
    return f"watch-{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _normalize_route_input(payload: StreamRoutingEvent) -> StreamRoutingEvent:
    event = payload
    if not event.event_id:
        raise StreamRoutingError("event_id is required.")
    if not event.target_key:
        raise StreamRoutingError("target_key is required.")
    if not event.event_type:
        raise StreamRoutingError("event_type is required.")
    if not event.source_kind:
        raise StreamRoutingError("source_kind is required.")
    if _is_invalid_datetime(event.business_at):
        raise StreamRoutingError("business_at must be a timezone-aware datetime.")
    if _is_invalid_datetime(event.recorded_at):
        raise StreamRoutingError("recorded_at must be a timezone-aware datetime.")
    return StreamRoutingEvent(
        event_id=_require_non_empty_text(event.event_id, "event_id"),
        target_key=_require_non_empty_text(event.target_key, "target_key"),
        business_at=_normalize_datetime(event.business_at, "business_at"),
        recorded_at=_normalize_datetime(event.recorded_at, "recorded_at"),
        event_type=_require_non_empty_text(event.event_type, "event_type"),
        source_kind=_require_non_empty_text(event.source_kind, "source_kind"),
        routing_lane=(event.routing_lane.strip() if event.routing_lane else None),
        event_family=(event.event_family.strip() if event.event_family else None),
        evidence_role=(event.evidence_role.strip() if event.evidence_role else None),
        reason_codes=_normalize_str_list(event.reason_codes),
        support_sections=_normalize_str_list(event.support_sections),
        explicit_support_refs=_normalize_str_list(event.explicit_support_refs),
        raw_route_hint=(event.raw_route_hint.strip() if event.raw_route_hint else None),
        market_anchor=(event.market_anchor.strip() if event.market_anchor else None),
        memory_anchor=(event.memory_anchor.strip() if event.memory_anchor else None),
        content_char_count=_normalize_non_negative_int(
            event.content_char_count,
            "content_char_count",
        ),
    )


def _normalize_str_list(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise StreamRoutingError("route metadata must be strings.")
        text = item.strip()
        if not text:
            continue
        if text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _require_non_empty_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise StreamRoutingError(f"{field_name} must not be blank.")
    return normalized


def _normalize_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StreamRoutingError(f"{field_name} must be a non-negative integer.")
    if value < 0:
        raise StreamRoutingError(f"{field_name} must be greater than or equal to zero.")
    return int(value)


def _normalize_datetime(value: datetime, field_name: str) -> datetime:
    if _is_invalid_datetime(value):
        raise StreamRoutingError(f"{field_name} must be a timezone-aware datetime.")
    return value.astimezone(UTC)


def _is_invalid_datetime(value: object) -> bool:
    if not isinstance(value, datetime):
        return True
    if value.tzinfo is None:
        return True
    return value.utcoffset() is None


def _stable_unique_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in values:
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _coerce_completeness_domain(value: str) -> CompletenessDomain:
    valid = _normalize_completeness_domain(value)
    if valid not in {"market_bars", "news_web_search", "official_filings", "operator_admission"}:
        raise StreamRoutingError(f"unsupported completeness domain: {value!r}")
    return cast(CompletenessDomain, valid)


def _normalize_completeness_domain(value: str) -> str:
    return value.strip()


def _require_sections(
    sections: tuple[SupportSection, ...] | list[SupportSection],
) -> tuple[SupportSection, ...]:
    if sections is None:
        return ()
    resolved: list[SupportSection] = []
    for section in sections:
        if not isinstance(section, SupportSection):
            raise StreamRoutingError("sections must be SupportSection instances.")
        if not section.section_id or not section.section_id.strip():
            raise StreamRoutingError("section_id must not be blank.")
        if not section.section_title or not section.section_title.strip():
            raise StreamRoutingError("section_title must not be blank.")
        resolved.append(
            SupportSection(
                section_id=_require_non_empty_text(section.section_id, "section_id"),
                section_title=_require_non_empty_text(section.section_title, "section_title"),
            )
        )
    return tuple(resolved)


def _lookup_exact(
    sections: tuple[SupportSection, ...],
    section_id: str,
) -> SupportSection:
    for section in sections:
        if section.section_id == section_id:
            return section
    raise StreamRoutingError(f"unsupported support section alias: {section_id!r}")


def _route_is_event_time_complete(
    *,
    final_route: CEAURoute,
    policy: StreamRoutingPolicy,
    watermark_observation: WatermarkObservation | None,
) -> bool:
    if final_route == "no_action":
        return False
    if not policy.completeness_requirements:
        return False
    return _all_requirements_advanced(
        policy=policy,
        watermark_observation=watermark_observation,
    )


def _all_requirements_advanced(
    *,
    policy: StreamRoutingPolicy,
    watermark_observation: WatermarkObservation | None,
) -> bool:
    if not policy.completeness_requirements:
        return False
    if watermark_observation is None:
        return False
    for requirement in policy.completeness_requirements:
        if not _requirement_is_satisfied(
            requirement=requirement,
            watermark_observation=watermark_observation,
        ):
            return False
    return True


def _derive_market_anchor_from_config(config: StreamRoutingConfig) -> str:
    if config.bar_granularity:
        return f"{config.target_key}:{config.bar_granularity}"
    return f"{config.target_key}:1h"


def _derive_route_record_id(
    *,
    route_input: StreamRoutingEvent,
    routing_lane: str,
    final_route: CEAURoute,
    reasons: tuple[str, ...],
    policy_version: str,
    policy_hash: str,
) -> str:
    material = "|".join(
        (
            _SALT,
            route_input.event_id,
            route_input.target_key,
            routing_lane,
            final_route,
            route_input.business_at.astimezone(UTC).isoformat(),
            route_input.recorded_at.astimezone(UTC).isoformat(),
            ";".join(reasons),
            policy_version,
            policy_hash,
        )
    )
    return f"route-{sha256(material.encode('utf-8')).hexdigest()[:24]}"


__all__ = [
    "resolve_routing_lane",
    "StreamRoutingDecision",
    "StreamRoutingError",
    "StreamRoutingEvent",
    "SupportSection",
    "SupportSectionResolver",
    "ResolvedSupportSection",
    "route_event",
]
