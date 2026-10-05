"""Stream-routing lane policy contracts for CEAU."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Final, Literal, cast

from event_trader.ceau.contracts import (
    CandidateRoute,
    CEAUContractError,
    CEAURoute,
    CompletenessRequirement,
)
from event_trader.contracts._validators import validate_target_key

_DEFAULT_MAX_DELAY_BARS: Final[int] = 0
_DEFAULT_MAX_UNIT_CHARS: Final[int] = 12_000
_DEFAULT_ALLOWED_CANDIDATE_ROUTES: Final[tuple[CandidateRoute, ...]] = ()

StreamRoutingPolicyStatus = Literal["candidate", "accepted", "retired"]
StreamRoutingUnitScope = Literal["lane", "context_window"]


@dataclass(frozen=True, slots=True)
class StreamRoutingPolicy:
    """Strict per-lane policy contract used by stream routing."""

    route_default: CEAURoute
    max_delay_bars: int = _DEFAULT_MAX_DELAY_BARS
    max_unit_chars: int = _DEFAULT_MAX_UNIT_CHARS
    unit_scope: StreamRoutingUnitScope = "lane"
    emit_on_watch_match: bool = False
    completeness_requirements: tuple[CompletenessRequirement, ...] = ()
    candidate_default_route: CandidateRoute = "no_action"
    allowed_candidate_routes: tuple[CandidateRoute, ...] = _DEFAULT_ALLOWED_CANDIDATE_ROUTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_default", _validate_final_route(self.route_default))
        object.__setattr__(
            self,
            "max_delay_bars",
            _normalize_non_negative_int(self.max_delay_bars, "max_delay_bars"),
        )
        object.__setattr__(
            self,
            "max_unit_chars",
            _normalize_positive_int(self.max_unit_chars, "max_unit_chars"),
        )
        object.__setattr__(self, "unit_scope", _validate_unit_scope(self.unit_scope))
        object.__setattr__(
            self,
            "emit_on_watch_match",
            _normalize_bool(self.emit_on_watch_match, "emit_on_watch_match"),
        )
        object.__setattr__(
            self,
            "completeness_requirements",
            _normalize_requirements(self.completeness_requirements),
        )
        object.__setattr__(
            self,
            "candidate_default_route",
            _validate_candidate_route(
                self.candidate_default_route
                if not isinstance(self.candidate_default_route, tuple)
                else "no_action",
                fallback=self.route_default,
            ),
        )
        candidate_routes = _normalize_candidate_routes(
            self.allowed_candidate_routes,
            route_default=self.route_default,
        )
        if self.candidate_default_route not in candidate_routes:
            candidate_routes = (self.candidate_default_route, *candidate_routes)
        object.__setattr__(self, "allowed_candidate_routes", _dedupe(candidate_routes))

    def to_json_payload(self, *, include_hash: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "route_default": self.route_default,
            "max_delay_bars": self.max_delay_bars,
            "max_unit_chars": self.max_unit_chars,
            "unit_scope": self.unit_scope,
            "emit_on_watch_match": self.emit_on_watch_match,
            "completeness_requirements": [
                req.to_json_payload() for req in self.completeness_requirements
            ],
            "candidate_default_route": self.candidate_default_route,
            "allowed_candidate_routes": list(self.allowed_candidate_routes),
        }
        if include_hash:
            payload["policy_hash"] = stream_routing_policy_hash(payload)
        return payload

    @classmethod
    def from_json_payload(cls, payload: object) -> StreamRoutingPolicy:
        payload_map = _require_dict(payload, "StreamRoutingPolicy")
        required_fields = {"route_default"}
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "StreamRoutingPolicy missing required field(s): "
                + ", ".join(missing_fields)
            )
        extra_fields = sorted(
            set(payload_map)
            - required_fields
            - {
                "max_delay_bars",
                "max_unit_chars",
                "unit_scope",
                "emit_on_watch_match",
                "completeness_requirements",
                "candidate_default_route",
                "allowed_candidate_routes",
            }
        )
        if extra_fields:
            raise CEAUContractError(
                "StreamRoutingPolicy has unexpected field(s): " + ", ".join(extra_fields)
            )

        return cls(
            route_default=_validate_final_route(
                _require_text(payload_map["route_default"], "route_default")
            ),
            max_delay_bars=_normalize_non_negative_int(
                payload_map.get("max_delay_bars", _DEFAULT_MAX_DELAY_BARS),
                "max_delay_bars",
            ),
            max_unit_chars=_normalize_positive_int(
                payload_map.get("max_unit_chars", _DEFAULT_MAX_UNIT_CHARS),
                "max_unit_chars",
            ),
            unit_scope=_validate_unit_scope(
                _require_text(payload_map.get("unit_scope", "lane"), "unit_scope")
            ),
            emit_on_watch_match=_normalize_bool(
                payload_map.get("emit_on_watch_match", False),
                "emit_on_watch_match",
            ),
            completeness_requirements=tuple(
                req
                if isinstance(req, CompletenessRequirement)
                else CompletenessRequirement.from_json_payload(req)
                for req in _normalize_tuple(
                    payload_map.get("completeness_requirements", []),
                    "completeness_requirements",
                )
            ),
            candidate_default_route=_validate_candidate_route(
                _normalize_text(payload_map.get("candidate_default_route", None)),
                fallback=_require_text(payload_map["route_default"], "route_default"),
            ),
            allowed_candidate_routes=tuple(
                _validate_candidate_route_entry(item)
                for item in _normalize_tuple(
                    payload_map.get("allowed_candidate_routes", ()),
                    "allowed_candidate_routes",
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class StreamRoutingConfig:
    """Strict stream-routing contract embedded in KernelConfig."""

    enabled: bool
    policy_version: str
    policy_status: StreamRoutingPolicyStatus
    target_key: str
    bar_granularity: str
    processing_time_safety_emit_enabled: bool
    processing_time_safety_emit_after_seconds: int | None
    default_unit_policy: StreamRoutingPolicy
    lane_policies: dict[str, StreamRoutingPolicy]
    max_pending_units_per_target: int = 128
    hard_emit_event_types: tuple[str, ...] = ()
    hard_emit_source_kinds: tuple[str, ...] = ()
    policy_hash: str = field(init=False, default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _normalize_bool(self.enabled, "enabled"))
        object.__setattr__(
            self,
            "policy_version",
            _require_text(self.policy_version, "policy_version"),
        )
        object.__setattr__(self, "policy_status", _validate_policy_status(self.policy_status))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CEAUContractError),
        )
        object.__setattr__(
            self,
            "bar_granularity",
            _require_text(self.bar_granularity, "bar_granularity"),
        )
        object.__setattr__(
            self,
            "processing_time_safety_emit_enabled",
            _normalize_bool(
                self.processing_time_safety_emit_enabled,
                "processing_time_safety_emit_enabled",
            ),
        )
        object.__setattr__(
            self,
            "processing_time_safety_emit_after_seconds",
            _normalize_optional_positive_int(
                self.processing_time_safety_emit_after_seconds,
                "processing_time_safety_emit_after_seconds",
            ),
        )
        object.__setattr__(
            self,
            "default_unit_policy",
            self.default_unit_policy
            if isinstance(self.default_unit_policy, StreamRoutingPolicy)
            else StreamRoutingPolicy.from_json_payload(self.default_unit_policy),
        )
        object.__setattr__(
            self,
            "lane_policies",
            _normalize_lane_policies(self.lane_policies),
        )
        object.__setattr__(
            self,
            "max_pending_units_per_target",
            _normalize_positive_int(
                self.max_pending_units_per_target,
                "max_pending_units_per_target",
            ),
        )
        object.__setattr__(
            self,
            "hard_emit_event_types",
            tuple(
                _require_text(item, "hard_emit_event_types")
                for item in _normalize_tuple(
                    self.hard_emit_event_types,
                    "hard_emit_event_types",
                )
            ),
        )
        object.__setattr__(
            self,
            "hard_emit_source_kinds",
            tuple(
                _require_text(item, "hard_emit_source_kinds")
                for item in _normalize_tuple(
                    self.hard_emit_source_kinds,
                    "hard_emit_source_kinds",
                )
            ),
        )
        object.__setattr__(
            self,
            "policy_hash",
            stream_routing_policy_hash(self),
        )

    @classmethod
    def from_json_payload(cls, payload: object) -> StreamRoutingConfig:
        payload_map = _require_dict(payload, "StreamRoutingConfig")
        required_fields = {
            "enabled",
            "policy_version",
            "policy_status",
            "target_key",
            "bar_granularity",
            "processing_time_safety_emit_enabled",
            "default_unit_policy",
            "lane_policies",
        }
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "StreamRoutingConfig missing required field(s): " + ", ".join(missing_fields)
            )
        extra_fields = sorted(
            set(payload_map)
            - required_fields
            - {
                "processing_time_safety_emit_after_seconds",
                "max_pending_units_per_target",
                "hard_emit_event_types",
                "hard_emit_source_kinds",
            }
        )
        if extra_fields:
            raise CEAUContractError(
                "StreamRoutingConfig has unexpected field(s): " + ", ".join(extra_fields)
            )

        policy_payload = payload_map["default_unit_policy"]
        lane_payload = _require_dict(payload_map["lane_policies"], "lane_policies")
        return cls(
            enabled=_normalize_bool(payload_map["enabled"], "enabled"),
            policy_version=_require_text(payload_map["policy_version"], "policy_version"),
            policy_status=_validate_policy_status(
                _require_text(payload_map["policy_status"], "policy_status")
            ),
            target_key=_require_text(payload_map["target_key"], "target_key"),
            bar_granularity=_require_text(payload_map["bar_granularity"], "bar_granularity"),
            processing_time_safety_emit_enabled=_normalize_bool(
                payload_map["processing_time_safety_emit_enabled"],
                "processing_time_safety_emit_enabled",
            ),
            processing_time_safety_emit_after_seconds=_normalize_optional_positive_int(
                payload_map.get("processing_time_safety_emit_after_seconds"),
                "processing_time_safety_emit_after_seconds",
            ),
            default_unit_policy=(
                policy_payload
                if isinstance(policy_payload, StreamRoutingPolicy)
                else StreamRoutingPolicy.from_json_payload(policy_payload)
            ),
            lane_policies={
                lane_name: (
                    policy_payload
                    if isinstance(policy_payload, StreamRoutingPolicy)
                    else StreamRoutingPolicy.from_json_payload(policy_payload)
                )
                for lane_name, policy_payload in _normalize_lane_payloads(lane_payload)
            },
            max_pending_units_per_target=_normalize_positive_int(
                payload_map.get("max_pending_units_per_target", 128),
                "max_pending_units_per_target",
            ),
            hard_emit_event_types=tuple(
                _require_text(item, "hard_emit_event_types")
                for item in _normalize_tuple(
                    payload_map.get("hard_emit_event_types", ()),
                    "hard_emit_event_types",
                )
            ),
            hard_emit_source_kinds=tuple(
                _require_text(item, "hard_emit_source_kinds")
                for item in _normalize_tuple(
                    payload_map.get("hard_emit_source_kinds", ()),
                    "hard_emit_source_kinds",
                )
            ),
        )

    def to_json_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "enabled": self.enabled,
            "policy_version": self.policy_version,
            "policy_status": self.policy_status,
            "target_key": self.target_key,
            "bar_granularity": self.bar_granularity,
            "processing_time_safety_emit_enabled": self.processing_time_safety_emit_enabled,
            "processing_time_safety_emit_after_seconds": (
                self.processing_time_safety_emit_after_seconds
            ),
            "max_pending_units_per_target": self.max_pending_units_per_target,
            "default_unit_policy": self.default_unit_policy.to_json_payload(),
            "lane_policies": {
                name: policy.to_json_payload() for name, policy in self.lane_policies.items()
            },
            "hard_emit_event_types": list(self.hard_emit_event_types),
            "hard_emit_source_kinds": list(self.hard_emit_source_kinds),
        }
        if include_hash:
            payload["policy_hash"] = stream_routing_policy_hash(payload)
        return payload


def stream_routing_policy_hash(
    payload_or_config: StreamRoutingConfig | Mapping[str, object],
) -> str:
    if isinstance(payload_or_config, StreamRoutingConfig):
        payload = payload_or_config.to_json_payload(include_hash=False)
    else:
        payload = dict(payload_or_config)
        payload.pop("policy_hash", None)
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _require_dict(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CEAUContractError(f"{field_name} must be a TOML table.")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise CEAUContractError(f"{field_name} must be a string.")
    text = value.strip()
    if not text:
        raise CEAUContractError(f"{field_name} must not be empty.")
    return text


def _normalize_text(value: object | None) -> str | None:
    if value is None:
        return None
    return _require_text(value, "candidate_default_route")


def _normalize_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CEAUContractError(f"{field_name} must be a boolean.")
    return value


def _normalize_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CEAUContractError(f"{field_name} must be a non-negative integer.")
    if value < 0:
        raise CEAUContractError(f"{field_name} must be greater than or equal to zero.")
    return int(value)


def _normalize_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CEAUContractError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise CEAUContractError(f"{field_name} must be greater than zero.")
    return int(value)


def _normalize_optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _normalize_positive_int(value, field_name)


def _validate_final_route(value: str) -> CEAURoute:
    if value not in {"no_action", "watch", "accumulate", "emit"}:
        raise CEAUContractError(
            "route_default must be one of: no_action, watch, accumulate, emit."
        )
    return cast(CEAURoute, value)


def _normalize_tuple(value: object, field_name: str) -> tuple[object, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    raise CEAUContractError(f"{field_name} must be an array.")


def _normalize_candidate_routes(
    value: tuple[CandidateRoute, ...] | list[CandidateRoute] | tuple[object, ...] | list[object],
    *,
    route_default: CEAURoute,
) -> tuple[CandidateRoute, ...]:
    entries = _normalize_tuple(value, "allowed_candidate_routes")
    if not entries:
        return (_validate_candidate_route(route_default),)
    normalized: list[CandidateRoute] = []
    for entry in entries:
        normalized.append(
            _validate_candidate_route(_require_text(entry, "allowed_candidate_routes"))
        )
    if not normalized:
        return (_validate_candidate_route(route_default),)
    normalized_tuple = tuple(normalized)
    if route_default not in normalized_tuple:
        normalized_tuple = (route_default,) + normalized_tuple
    return normalized_tuple


def _validate_candidate_route(value: str | None, fallback: str | None = None) -> CandidateRoute:
    if value is None:
        if fallback is None:
            raise CEAUContractError("candidate_default_route must be provided.")
        value = fallback
    if value not in {
        "watch_or_accumulate",
        "watch",
        "accumulate",
        "emit",
        "no_action",
    }:
        raise CEAUContractError(
            "candidate_default_route must be one of: "
            "watch_or_accumulate, watch, accumulate, emit, no_action."
        )
    return cast(CandidateRoute, value)


def _validate_candidate_route_entry(value: object) -> CandidateRoute:
    return _validate_candidate_route(_require_text(value, "allowed_candidate_routes"))


def _normalize_requirements(
    raw: object,
) -> tuple[CompletenessRequirement, ...]:
    if not raw:
        return ()
    if isinstance(raw, tuple):
        entries = raw
    elif isinstance(raw, list):
        entries = tuple(raw)
    else:
        raise CEAUContractError("completeness_requirements must be a list.")

    normalized: list[CompletenessRequirement] = []
    for entry in entries:
        if isinstance(entry, CompletenessRequirement):
            normalized.append(entry)
            continue
        if not isinstance(entry, dict):
            raise CEAUContractError(
                "completeness_requirements entries must be CompletenessRequirement payloads."
            )
        normalized.append(CompletenessRequirement.from_json_payload(entry))
    return tuple(normalized)


def _normalize_lane_payloads(payload: dict[str, object]) -> list[tuple[str, object]]:
    return [
        (name, value)
        for name, value in payload.items()
    ]


def _normalize_lane_policies(
    value: Mapping[str, object],
) -> dict[str, StreamRoutingPolicy]:
    if not isinstance(value, dict):
        raise CEAUContractError("lane_policies must be a TOML table.")
    normalized: dict[str, StreamRoutingPolicy] = {}
    for lane_name, raw_policy in value.items():
        policy = (
            raw_policy
            if isinstance(raw_policy, StreamRoutingPolicy)
            else StreamRoutingPolicy.from_json_payload(raw_policy)
        )
        normalized[_require_text(lane_name, "lane name")] = policy
    return normalized


def _validate_policy_status(value: str) -> StreamRoutingPolicyStatus:
    if value not in {"candidate", "accepted", "retired"}:
        raise CEAUContractError(
            "policy_status must be one of: candidate, accepted, retired."
        )
    return cast(StreamRoutingPolicyStatus, value)


def _validate_unit_scope(value: str) -> StreamRoutingUnitScope:
    if value not in {"lane", "context_window"}:
        raise CEAUContractError("unit_scope must be one of: lane, context_window.")
    return cast(StreamRoutingUnitScope, value)


def _dedupe(values: tuple[object, ...]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(cast(str, value))
    return tuple(seen)


UnitPolicy = StreamRoutingPolicy
StreamRoutingPolicyConfig = StreamRoutingPolicy


__all__ = [
    "StreamRoutingConfig",
    "StreamRoutingPolicy",
    "StreamRoutingPolicyConfig",
    "StreamRoutingPolicyStatus",
    "StreamRoutingUnitScope",
    "UnitPolicy",
    "stream_routing_policy_hash",
]
