"""Strict CEAU contracts for stream-routing and runtime records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal, TypeAlias, cast

from event_trader.contracts._validators import validate_target_key, validate_timestamp
from event_trader.contracts.runtime import AnalysisOutcomeValue


class CEAUContractError(ValueError):
    """Raised when a CEAU payload or value violates its contract."""


CEAURoute = Literal["no_action", "watch", "accumulate", "emit"]
CandidateRoute = Literal["watch_or_accumulate", "watch", "accumulate", "emit", "no_action"]
RecordType = Literal[
    "event_route",
    "watch_opened",
    "watch_matched",
    "watch_expired",
    "unit_opened",
    "unit_appended",
    "unit_emitted",
    "unit_closed",
    "unit_expired",
    "late_followup",
    "analysis_queued",
    "analysis_started",
    "analysis_completed",
    "analysis_failed",
    "source_completeness_observed",
    "watermark_observed",
]
CompletenessDomain = Literal[
    "market_bars",
    "news_web_search",
    "official_filings",
    "operator_admission",
]
CompletenessModel = Literal[
    "complete_through",
    "lower_bound_only",
    "poll_snapshot",
    "unknown",
]
CompletenessPurpose = Literal["absence_inference", "evidence_maturity"]
CompletenessStatus = Literal[
    "healthy",
    "stale",
    "unavailable",
    "unknown_completeness",
]
DomainWatermarkStatus = Literal["advanced", "frozen", "unavailable"]
WatermarkOverallStatus = Literal[
    "advanced",
    "partially_advanced",
    "frozen_no_source_completeness",
    "frozen_stale_source",
    "unavailable",
]
RuntimeScope = Literal["live", "replay"]
EmissionClock = Literal["event_time", "processing_time"]
EmissionReason = Literal[
    "hard_trigger",
    "processing_time_safety_close",
    "replay_final_drain",
    "context_budget",
    "other",
]
UnitFormationLaneStatus = Literal["pending", "emitted", "closed", "expired"]
AnalysisCompletedStatus: TypeAlias = AnalysisOutcomeValue

@dataclass(frozen=True, slots=True)
class CEAURecordEnvelope:
    """Strict wrapper for canonical CEAU JSONL records."""

    record: object

    def __post_init__(self) -> None:
        if not isinstance(
            self.record,
            (
                CEAUEventRouteRecord,
                CEAUUnitOpenedRecord,
                CEAUUnitAppendedRecord,
                CEAUUnitEmittedRecord,
                CEAUAnalysisQueuedRecord,
                CEAUAnalysisStartedRecord,
                CEAUAnalysisCompletedRecord,
                CEAUAnalysisFailedRecord,
            ),
        ):
            raise CEAUContractError("record must be a supported canonical CEAU record.")

    @property
    def record_type(self) -> RecordType:
        return self._canonical_record().record_type

    @property
    def record_id(self) -> str:
        return self._canonical_record().record_id

    @property
    def target_key(self) -> str:
        return self._canonical_record().target_key

    @property
    def recorded_at(self) -> datetime:
        return self._canonical_record().recorded_at

    def to_json_payload(self) -> dict[str, object]:
        return self._canonical_record().to_json_payload()

    def _canonical_record(
        self,
    ) -> (
        CEAUEventRouteRecord
        | CEAUUnitOpenedRecord
        | CEAUUnitAppendedRecord
        | CEAUUnitEmittedRecord
        | CEAUAnalysisQueuedRecord
        | CEAUAnalysisStartedRecord
        | CEAUAnalysisCompletedRecord
        | CEAUAnalysisFailedRecord
    ):
        if isinstance(
            self.record,
            (
                CEAUEventRouteRecord,
                CEAUUnitOpenedRecord,
                CEAUUnitAppendedRecord,
                CEAUUnitEmittedRecord,
                CEAUAnalysisQueuedRecord,
                CEAUAnalysisStartedRecord,
                CEAUAnalysisCompletedRecord,
                CEAUAnalysisFailedRecord,
            ),
        ):
            return self.record
        raise CEAUContractError("record must be a supported canonical CEAU record.")

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAURecordEnvelope:
        payload_map = _require_dict(payload, "CEAURecordEnvelope")
        record_type = _validate_record_type(
            _require_text(payload_map.get("record_type"), "record_type")
        )
        if record_type == "event_route":
            return cls(CEAUEventRouteRecord.from_json_payload(payload_map))
        if record_type == "unit_opened":
            return cls(CEAUUnitOpenedRecord.from_json_payload(payload_map))
        if record_type == "unit_appended":
            return cls(CEAUUnitAppendedRecord.from_json_payload(payload_map))
        if record_type == "unit_emitted":
            return cls(CEAUUnitEmittedRecord.from_json_payload(payload_map))
        if record_type == "analysis_queued":
            return cls(CEAUAnalysisQueuedRecord.from_json_payload(payload_map))
        if record_type == "analysis_started":
            return cls(CEAUAnalysisStartedRecord.from_json_payload(payload_map))
        if record_type == "analysis_completed":
            return cls(CEAUAnalysisCompletedRecord.from_json_payload(payload_map))
        if record_type == "analysis_failed":
            return cls(CEAUAnalysisFailedRecord.from_json_payload(payload_map))
        raise CEAUContractError(
            "CEAURecordEnvelope only supports event_route, unit_opened, "
            "unit_appended, unit_emitted, analysis_queued, analysis_started, "
            "analysis_completed, and analysis_failed."
        )


@dataclass(frozen=True, slots=True)
class CEAUEventRouteRecord:
    """Canonical receipt for the final route assigned to one admitted event."""

    record_type: RecordType
    record_id: str
    target_key: str
    recorded_at: datetime
    event_id: str
    routing_lane: str
    final_route: CEAURoute
    routing_reason_codes: tuple[str, ...]
    analysis_unit_id: str | None
    watch_id: str | None
    policy_version: str
    policy_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_type",
            _require_exact_record_type(self.record_type, "event_route"),
        )
        _validate_record_audit_fields(self)
        object.__setattr__(self, "event_id", _require_text(self.event_id, "event_id"))
        object.__setattr__(
            self, "routing_lane", _require_text(self.routing_lane, "routing_lane")
        )
        object.__setattr__(
            self,
            "final_route",
            _validate_final_route(_require_text(self.final_route, "final_route")),
        )
        object.__setattr__(
            self,
            "routing_reason_codes",
            _normalize_text_tuple(
                _require_list(self.routing_reason_codes, "routing_reason_codes"),
                field_name="routing_reason_codes",
                allow_empty=False,
            ),
        )
        object.__setattr__(self, "analysis_unit_id", _optional_text(self.analysis_unit_id))
        object.__setattr__(self, "watch_id", _optional_text(self.watch_id))
        _validate_route_downstream_identity(
            final_route=self.final_route,
            analysis_unit_id=self.analysis_unit_id,
            watch_id=self.watch_id,
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "target_key": self.target_key,
            "recorded_at": self.recorded_at.isoformat(),
            "event_id": self.event_id,
            "routing_lane": self.routing_lane,
            "final_route": self.final_route,
            "routing_reason_codes": list(self.routing_reason_codes),
            "analysis_unit_id": self.analysis_unit_id,
            "watch_id": self.watch_id,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUEventRouteRecord:
        payload_map = _require_dict(payload, "CEAUEventRouteRecord")
        required_fields = {
            "record_type",
            "record_id",
            "target_key",
            "recorded_at",
            "event_id",
            "routing_lane",
            "final_route",
            "routing_reason_codes",
            "policy_version",
            "policy_hash",
        }
        optional_fields = {"analysis_unit_id", "watch_id"}
        _reject_missing_or_unexpected(
            payload_map,
            required_fields,
            optional_fields,
            "CEAUEventRouteRecord",
        )
        return cls(
            record_type=_validate_record_type(
                _require_text(payload_map["record_type"], "record_type")
            ),
            record_id=_require_text(payload_map["record_id"], "record_id"),
            target_key=_require_text(payload_map["target_key"], "target_key"),
            recorded_at=_require_datetime(payload_map["recorded_at"], "recorded_at"),
            event_id=_require_text(payload_map["event_id"], "event_id"),
            routing_lane=_require_text(payload_map["routing_lane"], "routing_lane"),
            final_route=_validate_final_route(
                _require_text(payload_map["final_route"], "final_route")
            ),
            routing_reason_codes=_normalize_text_tuple(
                _require_list(payload_map["routing_reason_codes"], "routing_reason_codes"),
                field_name="routing_reason_codes",
                allow_empty=False,
            ),
            analysis_unit_id=_optional_text(payload_map.get("analysis_unit_id")),
            watch_id=_optional_text(payload_map.get("watch_id")),
            policy_version=_require_text(payload_map["policy_version"], "policy_version"),
            policy_hash=_validate_policy_hash(
                _require_text(payload_map["policy_hash"], "policy_hash")
            ),
        )


@dataclass(frozen=True, slots=True)
class CEAUUnitOpenedRecord:
    """Canonical record for opening one pending CEAU analysis unit."""

    record_type: RecordType
    record_id: str
    target_key: str
    recorded_at: datetime
    analysis_unit_id: str
    unit_key: CEAUUnitKey
    scope_key: tuple[str, str, str, str]
    opened_at: datetime
    business_at: datetime
    deadline_at: datetime
    max_delay_bars: int
    unit_char_count: int
    event_ids: tuple[str, ...]
    primary_event_ids: tuple[str, ...]
    context_event_ids: tuple[str, ...]
    event_time_completeness_claim: bool
    advanced_completeness_requirements: tuple[str, ...]
    missing_completeness_requirements: tuple[str, ...]
    policy_version: str
    policy_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_type",
            _require_exact_record_type(self.record_type, "unit_opened"),
        )
        _validate_record_audit_fields(self)
        _normalize_unit_lifecycle_fields(self)

    def to_json_payload(self) -> dict[str, object]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "target_key": self.target_key,
            "recorded_at": self.recorded_at.isoformat(),
            "analysis_unit_id": self.analysis_unit_id,
            "unit_key": self.unit_key.to_json_payload(),
            "scope_key": list(self.scope_key),
            "opened_at": self.opened_at.isoformat(),
            "business_at": self.business_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "max_delay_bars": self.max_delay_bars,
            "unit_char_count": self.unit_char_count,
            "event_ids": list(self.event_ids),
            "primary_event_ids": list(self.primary_event_ids),
            "context_event_ids": list(self.context_event_ids),
            "event_time_completeness_claim": self.event_time_completeness_claim,
            "advanced_completeness_requirements": list(
                self.advanced_completeness_requirements
            ),
            "missing_completeness_requirements": list(
                self.missing_completeness_requirements
            ),
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUUnitOpenedRecord:
        payload_map = _require_dict(payload, "CEAUUnitOpenedRecord")
        required_fields = _UNIT_LIFECYCLE_REQUIRED_FIELDS | {"record_type", "record_id"}
        _reject_missing_or_unexpected(
            payload_map,
            required_fields,
            set(),
            "CEAUUnitOpenedRecord",
        )
        return cls(
            record_type=_validate_record_type(
                _require_text(payload_map["record_type"], "record_type")
            ),
            record_id=_require_text(payload_map["record_id"], "record_id"),
            **cast(Any, _unit_lifecycle_kwargs(payload_map)),
        )


@dataclass(frozen=True, slots=True)
class CEAUUnitAppendedRecord:
    """Canonical record for appending one event to a pending CEAU analysis unit."""

    record_type: RecordType
    record_id: str
    target_key: str
    recorded_at: datetime
    analysis_unit_id: str
    unit_key: CEAUUnitKey
    scope_key: tuple[str, str, str, str]
    opened_at: datetime
    business_at: datetime
    deadline_at: datetime
    max_delay_bars: int
    unit_char_count: int
    event_ids: tuple[str, ...]
    primary_event_ids: tuple[str, ...]
    context_event_ids: tuple[str, ...]
    event_time_completeness_claim: bool
    advanced_completeness_requirements: tuple[str, ...]
    missing_completeness_requirements: tuple[str, ...]
    policy_version: str
    policy_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_type",
            _require_exact_record_type(self.record_type, "unit_appended"),
        )
        _validate_record_audit_fields(self)
        _normalize_unit_lifecycle_fields(self)

    def to_json_payload(self) -> dict[str, object]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "target_key": self.target_key,
            "recorded_at": self.recorded_at.isoformat(),
            "analysis_unit_id": self.analysis_unit_id,
            "unit_key": self.unit_key.to_json_payload(),
            "scope_key": list(self.scope_key),
            "opened_at": self.opened_at.isoformat(),
            "business_at": self.business_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "max_delay_bars": self.max_delay_bars,
            "unit_char_count": self.unit_char_count,
            "event_ids": list(self.event_ids),
            "primary_event_ids": list(self.primary_event_ids),
            "context_event_ids": list(self.context_event_ids),
            "event_time_completeness_claim": self.event_time_completeness_claim,
            "advanced_completeness_requirements": list(
                self.advanced_completeness_requirements
            ),
            "missing_completeness_requirements": list(
                self.missing_completeness_requirements
            ),
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUUnitAppendedRecord:
        payload_map = _require_dict(payload, "CEAUUnitAppendedRecord")
        required_fields = _UNIT_LIFECYCLE_REQUIRED_FIELDS | {"record_type", "record_id"}
        _reject_missing_or_unexpected(
            payload_map,
            required_fields,
            set(),
            "CEAUUnitAppendedRecord",
        )
        return cls(
            record_type=_validate_record_type(
                _require_text(payload_map["record_type"], "record_type")
            ),
            record_id=_require_text(payload_map["record_id"], "record_id"),
            **cast(Any, _unit_lifecycle_kwargs(payload_map)),
        )


@dataclass(frozen=True, slots=True)
class CEAUUnitEmittedRecord:
    """Canonical record for one emitted analysis unit."""

    record_type: RecordType
    record_id: str
    target_key: str
    recorded_at: datetime
    analysis_unit_id: str
    unit_key: CEAUUnitKey
    opened_at: datetime
    business_at: datetime
    deadline_at: datetime
    max_delay_bars: int
    event_ids: tuple[str, ...]
    primary_event_ids: tuple[str, ...]
    context_event_ids: tuple[str, ...]
    emission_reason: EmissionReason
    emission_clock: EmissionClock
    event_time_completeness_claim: bool
    advanced_completeness_requirements: tuple[str, ...]
    missing_completeness_requirements: tuple[str, ...]
    policy_version: str
    policy_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_type",
            _require_exact_record_type(self.record_type, "unit_emitted"),
        )
        _validate_record_audit_fields(self)
        object.__setattr__(
            self,
            "analysis_unit_id",
            _require_text(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(
            self,
            "unit_key",
            self.unit_key
            if isinstance(self.unit_key, CEAUUnitKey)
            else CEAUUnitKey.from_json_payload(self.unit_key),
        )
        object.__setattr__(self, "opened_at", _require_datetime(self.opened_at, "opened_at"))
        object.__setattr__(
            self,
            "business_at",
            _require_datetime(self.business_at, "business_at"),
        )
        object.__setattr__(
            self,
            "deadline_at",
            _require_datetime(self.deadline_at, "deadline_at"),
        )
        object.__setattr__(
            self,
            "max_delay_bars",
            _normalize_non_negative_int(self.max_delay_bars, "max_delay_bars"),
        )
        object.__setattr__(
            self,
            "event_ids",
            _normalize_text_tuple(
                _require_list(self.event_ids, "event_ids"),
                field_name="event_ids",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "primary_event_ids",
            _normalize_text_tuple(
                _require_list(self.primary_event_ids, "primary_event_ids"),
                field_name="primary_event_ids",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "context_event_ids",
            _normalize_text_tuple(
                _require_list(self.context_event_ids, "context_event_ids"),
                field_name="context_event_ids",
            ),
        )
        object.__setattr__(
            self,
            "emission_reason",
            _validate_emission_reason(_require_text(self.emission_reason, "emission_reason")),
        )
        object.__setattr__(
            self,
            "emission_clock",
            _validate_emission_clock(_require_text(self.emission_clock, "emission_clock")),
        )
        object.__setattr__(
            self,
            "event_time_completeness_claim",
            _require_bool(self.event_time_completeness_claim, "event_time_completeness_claim"),
        )
        object.__setattr__(
            self,
            "advanced_completeness_requirements",
            _normalize_text_tuple(
                _require_list(
                    self.advanced_completeness_requirements,
                    "advanced_completeness_requirements",
                ),
                field_name="advanced_completeness_requirements",
            ),
        )
        object.__setattr__(
            self,
            "missing_completeness_requirements",
            _normalize_text_tuple(
                _require_list(
                    self.missing_completeness_requirements,
                    "missing_completeness_requirements",
                ),
                field_name="missing_completeness_requirements",
            ),
        )
        _validate_unit_event_sets(
            event_ids=self.event_ids,
            primary_event_ids=self.primary_event_ids,
            context_event_ids=self.context_event_ids,
        )
        if self.business_at < self.opened_at:
            raise CEAUContractError("business_at must not be earlier than opened_at.")
        if self.deadline_at < self.business_at:
            raise CEAUContractError("deadline_at must not be earlier than business_at.")
        if (
            self.emission_reason == "processing_time_safety_close"
            and self.emission_clock != "processing_time"
        ):
            raise CEAUContractError(
                "processing_time_safety_close is only valid with emission_clock=processing_time."
            )
        if (
            self.emission_reason == "processing_time_safety_close"
            and self.event_time_completeness_claim
        ):
            raise CEAUContractError(
                "processing_time_safety_close requires event_time_completeness_claim=false."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "target_key": self.target_key,
            "recorded_at": self.recorded_at.isoformat(),
            "analysis_unit_id": self.analysis_unit_id,
            "unit_key": self.unit_key.to_json_payload(),
            "opened_at": self.opened_at.isoformat(),
            "business_at": self.business_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
            "max_delay_bars": self.max_delay_bars,
            "event_ids": list(self.event_ids),
            "primary_event_ids": list(self.primary_event_ids),
            "context_event_ids": list(self.context_event_ids),
            "emission_reason": self.emission_reason,
            "emission_clock": self.emission_clock,
            "event_time_completeness_claim": self.event_time_completeness_claim,
            "advanced_completeness_requirements": list(
                self.advanced_completeness_requirements
            ),
            "missing_completeness_requirements": list(
                self.missing_completeness_requirements
            ),
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUUnitEmittedRecord:
        payload_map = _require_dict(payload, "CEAUUnitEmittedRecord")
        required_fields = {
            "record_type",
            "record_id",
            "target_key",
            "recorded_at",
            "analysis_unit_id",
            "unit_key",
            "event_ids",
            "primary_event_ids",
            "context_event_ids",
            "emission_reason",
            "emission_clock",
            "event_time_completeness_claim",
            "advanced_completeness_requirements",
            "missing_completeness_requirements",
            "policy_version",
            "policy_hash",
        }
        optional_fields = {
            "opened_at",
            "business_at",
            "deadline_at",
            "max_delay_bars",
        }
        _reject_missing_or_unexpected(
            payload_map,
            required_fields,
            optional_fields,
            "CEAUUnitEmittedRecord",
        )
        return cls(
            record_type=_validate_record_type(
                _require_text(payload_map["record_type"], "record_type")
            ),
            record_id=_require_text(payload_map["record_id"], "record_id"),
            target_key=_require_text(payload_map["target_key"], "target_key"),
            recorded_at=_require_datetime(payload_map["recorded_at"], "recorded_at"),
            analysis_unit_id=_require_text(
                payload_map["analysis_unit_id"], "analysis_unit_id"
            ),
            unit_key=CEAUUnitKey.from_json_payload(payload_map["unit_key"]),
            opened_at=_require_datetime(
                payload_map.get("opened_at", payload_map["recorded_at"]),
                "opened_at",
            ),
            business_at=_require_datetime(
                payload_map.get("business_at", payload_map["recorded_at"]),
                "business_at",
            ),
            deadline_at=_require_datetime(
                payload_map.get("deadline_at", payload_map["recorded_at"]),
                "deadline_at",
            ),
            max_delay_bars=_normalize_non_negative_int(
                payload_map.get("max_delay_bars", 0),
                "max_delay_bars",
            ),
            event_ids=_normalize_text_tuple(
                _require_list(payload_map["event_ids"], "event_ids"),
                field_name="event_ids",
                allow_empty=False,
            ),
            primary_event_ids=_normalize_text_tuple(
                _require_list(payload_map["primary_event_ids"], "primary_event_ids"),
                field_name="primary_event_ids",
                allow_empty=False,
            ),
            context_event_ids=_normalize_text_tuple(
                _require_list(payload_map["context_event_ids"], "context_event_ids"),
                field_name="context_event_ids",
            ),
            emission_reason=_validate_emission_reason(
                _require_text(payload_map["emission_reason"], "emission_reason")
            ),
            emission_clock=_validate_emission_clock(
                _require_text(payload_map["emission_clock"], "emission_clock")
            ),
            event_time_completeness_claim=_require_bool(
                payload_map["event_time_completeness_claim"],
                "event_time_completeness_claim",
            ),
            advanced_completeness_requirements=_normalize_text_tuple(
                _require_list(
                    payload_map["advanced_completeness_requirements"],
                    "advanced_completeness_requirements",
                ),
                field_name="advanced_completeness_requirements",
            ),
            missing_completeness_requirements=_normalize_text_tuple(
                _require_list(
                    payload_map["missing_completeness_requirements"],
                    "missing_completeness_requirements",
                ),
                field_name="missing_completeness_requirements",
            ),
            policy_version=_require_text(payload_map["policy_version"], "policy_version"),
            policy_hash=_validate_policy_hash(
                _require_text(payload_map["policy_hash"], "policy_hash")
            ),
        )


@dataclass(frozen=True, slots=True)
class CEAUAnalysisQueuedRecord:
    """Canonical lifecycle marker indicating an emitted CEAU analysis has queued."""

    record_type: RecordType
    record_id: str
    target_key: str
    recorded_at: datetime
    analysis_unit_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_type",
            _require_exact_record_type(self.record_type, "analysis_queued"),
        )
        object.__setattr__(
            self,
            "record_id",
            _require_text(self.record_id, "record_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CEAUContractError),
        )
        object.__setattr__(
            self,
            "recorded_at",
            _require_datetime(self.recorded_at, "recorded_at"),
        )
        object.__setattr__(
            self,
            "analysis_unit_id",
            _require_text(self.analysis_unit_id, "analysis_unit_id"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "target_key": self.target_key,
            "recorded_at": self.recorded_at.isoformat(),
            "analysis_unit_id": self.analysis_unit_id,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUAnalysisQueuedRecord:
        payload_map = _require_dict(payload, "CEAUAnalysisQueuedRecord")
        required_fields = {
            "record_type",
            "record_id",
            "target_key",
            "recorded_at",
            "analysis_unit_id",
        }
        _reject_missing_or_unexpected(payload_map, required_fields, set(), "CEAUAnalysisQueuedRecord")
        return cls(
            record_type=_validate_record_type(
                _require_text(payload_map["record_type"], "record_type")
            ),
            record_id=_require_text(payload_map["record_id"], "record_id"),
            target_key=_require_text(payload_map["target_key"], "target_key"),
            recorded_at=_require_datetime(payload_map["recorded_at"], "recorded_at"),
            analysis_unit_id=_require_text(
                payload_map["analysis_unit_id"], "analysis_unit_id"
            ),
        )


@dataclass(frozen=True, slots=True)
class CEAUAnalysisStartedRecord:
    """Canonical lifecycle marker indicating an analysis unit started execution."""

    record_type: RecordType
    record_id: str
    target_key: str
    recorded_at: datetime
    analysis_unit_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_type",
            _require_exact_record_type(self.record_type, "analysis_started"),
        )
        object.__setattr__(
            self,
            "record_id",
            _require_text(self.record_id, "record_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CEAUContractError),
        )
        object.__setattr__(
            self,
            "recorded_at",
            _require_datetime(self.recorded_at, "recorded_at"),
        )
        object.__setattr__(
            self,
            "analysis_unit_id",
            _require_text(self.analysis_unit_id, "analysis_unit_id"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "target_key": self.target_key,
            "recorded_at": self.recorded_at.isoformat(),
            "analysis_unit_id": self.analysis_unit_id,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUAnalysisStartedRecord:
        payload_map = _require_dict(payload, "CEAUAnalysisStartedRecord")
        required_fields = {
            "record_type",
            "record_id",
            "target_key",
            "recorded_at",
            "analysis_unit_id",
        }
        _reject_missing_or_unexpected(payload_map, required_fields, set(), "CEAUAnalysisStartedRecord")
        return cls(
            record_type=_validate_record_type(
                _require_text(payload_map["record_type"], "record_type")
            ),
            record_id=_require_text(payload_map["record_id"], "record_id"),
            target_key=_require_text(payload_map["target_key"], "target_key"),
            recorded_at=_require_datetime(payload_map["recorded_at"], "recorded_at"),
            analysis_unit_id=_require_text(
                payload_map["analysis_unit_id"], "analysis_unit_id"
            ),
        )


@dataclass(frozen=True, slots=True)
class CEAUAnalysisFailedRecord:
    """Canonical lifecycle marker indicating an emitted unit analysis failed."""

    record_type: RecordType
    record_id: str
    target_key: str
    recorded_at: datetime
    analysis_unit_id: str
    error_type: str
    error_message: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_type",
            _require_exact_record_type(self.record_type, "analysis_failed"),
        )
        object.__setattr__(
            self,
            "record_id",
            _require_text(self.record_id, "record_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CEAUContractError),
        )
        object.__setattr__(
            self,
            "recorded_at",
            _require_datetime(self.recorded_at, "recorded_at"),
        )
        object.__setattr__(
            self,
            "analysis_unit_id",
            _require_text(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(
            self,
            "error_type",
            _require_text(self.error_type, "error_type"),
        )
        object.__setattr__(
            self,
            "error_message",
            _require_text(self.error_message, "error_message"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "target_key": self.target_key,
            "recorded_at": self.recorded_at.isoformat(),
            "analysis_unit_id": self.analysis_unit_id,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUAnalysisFailedRecord:
        payload_map = _require_dict(payload, "CEAUAnalysisFailedRecord")
        required_fields = {
            "record_type",
            "record_id",
            "target_key",
            "recorded_at",
            "analysis_unit_id",
            "error_type",
            "error_message",
        }
        _reject_missing_or_unexpected(payload_map, required_fields, set(), "CEAUAnalysisFailedRecord")
        return cls(
            record_type=_validate_record_type(
                _require_text(payload_map["record_type"], "record_type")
            ),
            record_id=_require_text(payload_map["record_id"], "record_id"),
            target_key=_require_text(payload_map["target_key"], "target_key"),
            recorded_at=_require_datetime(payload_map["recorded_at"], "recorded_at"),
            analysis_unit_id=_require_text(
                payload_map["analysis_unit_id"], "analysis_unit_id"
            ),
            error_type=_require_text(payload_map["error_type"], "error_type"),
            error_message=_require_text(payload_map["error_message"], "error_message"),
        )


@dataclass(frozen=True, slots=True)
class CEAUAnalysisCompletedRecord:
    """Canonical pointer-only record for a completed analysis outcome."""

    record_type: RecordType
    record_id: str
    target_key: str
    recorded_at: datetime
    analysis_unit_id: str
    pointer: CEAUAnalysisCompletedPointer

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_type",
            _require_exact_record_type(self.record_type, "analysis_completed"),
        )
        object.__setattr__(self, "record_id", _require_text(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CEAUContractError),
        )
        object.__setattr__(self, "recorded_at", _require_datetime(self.recorded_at, "recorded_at"))
        object.__setattr__(
            self,
            "analysis_unit_id",
            _require_text(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(
            self,
            "pointer",
            self.pointer
            if isinstance(self.pointer, CEAUAnalysisCompletedPointer)
            else CEAUAnalysisCompletedPointer.from_json_payload(self.pointer),
        )

    def to_json_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "target_key": self.target_key,
            "recorded_at": self.recorded_at.isoformat(),
            "analysis_unit_id": self.analysis_unit_id,
        }
        payload.update(self.pointer.to_json_payload())
        return payload

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUAnalysisCompletedRecord:
        payload_map = _require_dict(payload, "CEAUAnalysisCompletedRecord")
        shared_fields = {
            "record_type",
            "record_id",
            "target_key",
            "recorded_at",
            "analysis_unit_id",
        }
        pointer_fields = {
            "analysis_outcome_record_id",
            "analysis_outcome_path",
            "analysis_outcome_sha256",
            "completed_status",
            "completed_at",
        }
        _reject_missing_or_unexpected(
            payload_map,
            shared_fields | pointer_fields,
            set(),
            "CEAUAnalysisCompletedRecord",
        )
        return cls(
            record_type=_validate_record_type(
                _require_text(payload_map["record_type"], "record_type")
            ),
            record_id=_require_text(payload_map["record_id"], "record_id"),
            target_key=_require_text(payload_map["target_key"], "target_key"),
            recorded_at=_require_datetime(payload_map["recorded_at"], "recorded_at"),
            analysis_unit_id=_require_text(
                payload_map["analysis_unit_id"], "analysis_unit_id"
            ),
            pointer=CEAUAnalysisCompletedPointer.from_json_payload(
                {field: payload_map[field] for field in pointer_fields}
            ),
        )


@dataclass(frozen=True, slots=True)
class CompletenessRequirement:
    """Purpose-scoped completeness requirement contract."""

    purpose: CompletenessPurpose
    domain: CompletenessDomain
    deadline_at: datetime | None
    required_for: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "purpose",
            _validate_completeness_purpose(_require_text(self.purpose, "purpose")),
        )
        object.__setattr__(
            self,
            "domain",
            _validate_completeness_domain(_require_text(self.domain, "domain")),
        )
        object.__setattr__(
            self,
            "deadline_at",
            _optional_datetime(self.deadline_at, "deadline_at"),
        )
        object.__setattr__(
            self,
            "required_for",
            _normalize_text_tuple(
                _require_list(self.required_for, "required_for"),
                field_name="required_for",
                allow_empty=False,
            ),
        )

    def to_json_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "purpose": self.purpose,
            "domain": self.domain,
            "required_for": list(self.required_for),
        }
        if self.deadline_at is not None:
            payload["deadline_at"] = self.deadline_at.isoformat()
        return payload

    @classmethod
    def from_json_payload(cls, payload: object) -> CompletenessRequirement:
        payload_map = _require_dict(payload, "CompletenessRequirement")
        required_fields = {"purpose", "domain", "required_for"}
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "CompletenessRequirement missing required field(s): "
                + ", ".join(missing_fields)
            )

        unexpected_fields = sorted(set(payload_map) - required_fields - {"deadline_at"})
        if unexpected_fields:
            raise CEAUContractError(
                "CompletenessRequirement has unexpected field(s): "
                + ", ".join(unexpected_fields)
            )

        return cls(
            purpose=_validate_completeness_purpose(
                _require_text(payload_map["purpose"], "purpose")
            ),
            domain=_validate_completeness_domain(_require_text(payload_map["domain"], "domain")),
            deadline_at=_optional_datetime(payload_map.get("deadline_at"), "deadline_at"),
            required_for=_normalize_text_tuple(
                _require_list(payload_map["required_for"], "required_for"),
                field_name="required_for",
                allow_empty=False,
            ),
        )


@dataclass(frozen=True, slots=True)
class SourceCompletenessObservation:
    """Source-adapter declared completeness observation."""

    source_name: str
    target_key: str
    observed_at: datetime
    completeness_domain: CompletenessDomain
    completeness_model: CompletenessModel
    complete_through_at: datetime | None
    lower_bound_at: datetime | None
    checkpoint_ref: str | None
    status: CompletenessStatus
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_name", _require_text(self.source_name, "source_name"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CEAUContractError),
        )
        object.__setattr__(
            self,
            "observed_at",
            _require_datetime(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "completeness_domain",
            _validate_completeness_domain(
                _require_text(self.completeness_domain, "completeness_domain")
            ),
        )
        object.__setattr__(
            self,
            "completeness_model",
            _validate_completeness_model(
                _require_text(self.completeness_model, "completeness_model")
            ),
        )
        object.__setattr__(
            self,
            "complete_through_at",
            _optional_datetime(self.complete_through_at, "complete_through_at"),
        )
        object.__setattr__(
            self,
            "lower_bound_at",
            _optional_datetime(self.lower_bound_at, "lower_bound_at"),
        )
        object.__setattr__(
            self,
            "checkpoint_ref",
            _optional_text(self.checkpoint_ref),
        )
        object.__setattr__(
            self,
            "status",
            _validate_completeness_status(_require_text(self.status, "status")),
        )
        object.__setattr__(
            self,
            "reason_codes",
            _normalize_text_tuple(
                _require_list(self.reason_codes, "reason_codes"),
                field_name="reason_codes",
                allow_empty=False,
            ),
        )

        if (
            self.completeness_domain == "news_web_search"
            and self.completeness_model == "complete_through"
        ):
            raise CEAUContractError(
                "news_web_search complete_through is not allowed in this slice."
            )

        if self.completeness_model == "complete_through" and self.complete_through_at is None:
            raise CEAUContractError(
                "complete_through_at is required when completeness_model is complete_through."
            )

        if self.completeness_model != "complete_through" and self.complete_through_at is not None:
            raise CEAUContractError(
                "complete_through_at may only be set when completeness_model is complete_through."
            )
        if self.completeness_model == "lower_bound_only" and self.lower_bound_at is None:
            raise CEAUContractError(
                "lower_bound_at is required when completeness_model is lower_bound_only."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "source_name": self.source_name,
            "target_key": self.target_key,
            "observed_at": self.observed_at.isoformat(),
            "completeness_domain": self.completeness_domain,
            "completeness_model": self.completeness_model,
            "complete_through_at": (
                self.complete_through_at.isoformat() if self.complete_through_at else None
            ),
            "lower_bound_at": (
                self.lower_bound_at.isoformat() if self.lower_bound_at else None
            ),
            "checkpoint_ref": self.checkpoint_ref,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> SourceCompletenessObservation:
        payload_map = _require_dict(payload, "SourceCompletenessObservation")
        required_fields = {
            "source_name",
            "target_key",
            "observed_at",
            "completeness_domain",
            "completeness_model",
            "status",
            "reason_codes",
        }
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "SourceCompletenessObservation missing required field(s): "
                + ", ".join(missing_fields)
            )

        extra_fields = sorted(
            set(payload_map)
            - required_fields
            - {"complete_through_at", "lower_bound_at", "checkpoint_ref"}
        )
        if extra_fields:
            raise CEAUContractError(
                "SourceCompletenessObservation has unexpected field(s): "
                + ", ".join(extra_fields)
            )

        return cls(
            source_name=_require_text(payload_map["source_name"], "source_name"),
            target_key=_require_text(payload_map["target_key"], "target_key"),
            observed_at=_require_datetime(payload_map["observed_at"], "observed_at"),
            completeness_domain=_validate_completeness_domain(
                _require_text(payload_map["completeness_domain"], "completeness_domain")
            ),
            completeness_model=_validate_completeness_model(
                _require_text(payload_map["completeness_model"], "completeness_model")
            ),
            complete_through_at=_optional_datetime(
                payload_map.get("complete_through_at"), "complete_through_at"
            ),
            lower_bound_at=_optional_datetime(payload_map.get("lower_bound_at"), "lower_bound_at"),
            checkpoint_ref=_optional_text(payload_map.get("checkpoint_ref")),
            status=_validate_completeness_status(_require_text(payload_map["status"], "status")),
            reason_codes=_normalize_text_tuple(
                _require_list(payload_map["reason_codes"], "reason_codes"),
                field_name="reason_codes",
                allow_empty=False,
            ),
        )


@dataclass(frozen=True, slots=True)
class DomainWatermark:
    """Domain-level completeness watermark state."""

    domain: CompletenessDomain
    watermark_at: datetime | None
    status: DomainWatermarkStatus
    supporting_source_names: tuple[str, ...]
    source_models: tuple[CompletenessModel, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "domain",
            _validate_completeness_domain(_require_text(self.domain, "domain")),
        )
        object.__setattr__(
            self,
            "watermark_at",
            _optional_datetime(self.watermark_at, "watermark_at"),
        )
        object.__setattr__(
            self,
            "status",
            _validate_domain_watermark_status(_require_text(self.status, "status")),
        )
        object.__setattr__(
            self,
            "supporting_source_names",
            _normalize_text_tuple(
                _require_list(self.supporting_source_names, "supporting_source_names")
            ),
        )
        object.__setattr__(
            self,
            "source_models",
            tuple(
                _validate_completeness_model(_require_text(item, "source_model"))
                for item in _require_list(self.source_models, "source_models")
            ),
        )
        object.__setattr__(
            self,
            "reason_codes",
            _normalize_text_tuple(_require_list(self.reason_codes, "reason_codes")),
        )
        if self.status == "advanced":
            if self.watermark_at is None:
                raise CEAUContractError(
                    "advanced DomainWatermark requires watermark_at."
                )
            if not self.supporting_source_names:
                raise CEAUContractError(
                    "advanced DomainWatermark requires supporting_source_names."
                )
            if not self.source_models:
                raise CEAUContractError(
                    "advanced DomainWatermark requires source_models."
                )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "watermark_at": self.watermark_at.isoformat() if self.watermark_at else None,
            "status": self.status,
            "supporting_source_names": list(self.supporting_source_names),
            "source_models": list(self.source_models),
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> DomainWatermark:
        payload_map = _require_dict(payload, "DomainWatermark")
        required_fields = {
            "domain",
            "status",
            "supporting_source_names",
            "source_models",
            "reason_codes",
        }
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "DomainWatermark missing required field(s): " + ", ".join(missing_fields)
            )
        unexpected_fields = sorted(set(payload_map) - required_fields - {"watermark_at"})
        if unexpected_fields:
            raise CEAUContractError(
                "DomainWatermark has unexpected field(s): " + ", ".join(unexpected_fields)
            )

        return cls(
            domain=_validate_completeness_domain(_require_text(payload_map["domain"], "domain")),
            watermark_at=_optional_datetime(payload_map.get("watermark_at"), "watermark_at"),
            status=_validate_domain_watermark_status(
                _require_text(payload_map["status"], "status")
            ),
            supporting_source_names=_normalize_text_tuple(
                _require_list(payload_map["supporting_source_names"], "supporting_source_names"),
            ),
            source_models=tuple(
                _validate_completeness_model(_require_text(item, "source_model"))
                for item in _require_list(payload_map["source_models"], "source_models")
            ),
            reason_codes=_normalize_text_tuple(
                _require_list(payload_map["reason_codes"], "reason_codes"),
            ),
        )


@dataclass(frozen=True, slots=True)
class WatermarkObservation:
    """Per-target latest completeness watermark view used by routing checks."""

    target_key: str
    runtime_scope: RuntimeScope
    recorded_at: datetime
    domain_watermarks: dict[CompletenessDomain, DomainWatermark]
    source_observations: tuple[SourceCompletenessObservation, ...]
    overall_status: WatermarkOverallStatus
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CEAUContractError),
        )
        object.__setattr__(
            self,
            "runtime_scope",
            _validate_runtime_scope(_require_text(self.runtime_scope, "runtime_scope")),
        )
        object.__setattr__(
            self,
            "recorded_at",
            _require_datetime(self.recorded_at, "recorded_at"),
        )

        if not isinstance(self.domain_watermarks, dict):
            raise CEAUContractError("domain_watermarks must be a mapping.")
        normalized: dict[CompletenessDomain, DomainWatermark] = {}
        for raw_domain, raw_watermark in self.domain_watermarks.items():
            domain = _validate_completeness_domain(_require_text(raw_domain, "domain"))
            watermark = (
                raw_watermark
                if isinstance(raw_watermark, DomainWatermark)
                else DomainWatermark.from_json_payload(raw_watermark)
            )
            if watermark.domain != domain:
                raise CEAUContractError("domain_watermarks keys must match watermark.domain.")
            normalized[domain] = watermark
        object.__setattr__(self, "domain_watermarks", normalized)

        object.__setattr__(
            self,
            "source_observations",
            tuple(
                item
                if isinstance(item, SourceCompletenessObservation)
                else SourceCompletenessObservation.from_json_payload(item)
                for item in _normalize_tuple(self.source_observations, "source_observations")
            ),
        )
        object.__setattr__(
            self,
            "overall_status",
            _validate_overall_watermark_status(
                _require_text(self.overall_status, "overall_status")
            ),
        )
        object.__setattr__(
            self,
            "reason_codes",
            _normalize_text_tuple(_require_list(self.reason_codes, "reason_codes")),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "runtime_scope": self.runtime_scope,
            "recorded_at": self.recorded_at.isoformat(),
            "domain_watermarks": {
                domain: watermark.to_json_payload()
                for domain, watermark in self.domain_watermarks.items()
            },
            "source_observations": [obs.to_json_payload() for obs in self.source_observations],
            "overall_status": self.overall_status,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> WatermarkObservation:
        payload_map = _require_dict(payload, "WatermarkObservation")
        required_fields = {
            "target_key",
            "runtime_scope",
            "recorded_at",
            "domain_watermarks",
            "source_observations",
            "overall_status",
            "reason_codes",
        }
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "WatermarkObservation missing required field(s): "
                + ", ".join(missing_fields)
            )
        unexpected_fields = sorted(set(payload_map) - required_fields)
        if unexpected_fields:
            raise CEAUContractError(
                "WatermarkObservation has unexpected field(s): "
                + ", ".join(unexpected_fields)
            )

        domain_watermark_payload = _require_dict(
            payload_map["domain_watermarks"],
            "WatermarkObservation.domain_watermarks",
        )
        normalized_watermarks: dict[CompletenessDomain, DomainWatermark] = {}
        for raw_domain, raw_watermark in domain_watermark_payload.items():
            domain = _validate_completeness_domain(_require_text(raw_domain, "domain"))
            watermark = DomainWatermark.from_json_payload(raw_watermark)
            if watermark.domain != domain:
                raise CEAUContractError(
                    "domain_watermarks keys must match DomainWatermark.domain."
                )
            normalized_watermarks[domain] = watermark

        return cls(
            target_key=_require_text(payload_map["target_key"], "target_key"),
            runtime_scope=_validate_runtime_scope(
                _require_text(payload_map["runtime_scope"], "runtime_scope")
            ),
            recorded_at=_require_datetime(payload_map["recorded_at"], "recorded_at"),
            domain_watermarks=normalized_watermarks,
            source_observations=tuple(
                item
                if isinstance(item, SourceCompletenessObservation)
                else SourceCompletenessObservation.from_json_payload(item)
                for item in _normalize_tuple(
                    payload_map["source_observations"],
                    "source_observations",
                )
            ),
            overall_status=_validate_overall_watermark_status(
                _require_text(payload_map["overall_status"], "overall_status")
            ),
            reason_codes=_normalize_text_tuple(
                _require_list(payload_map["reason_codes"], "reason_codes"),
            ),
        )


@dataclass(frozen=True, slots=True)
class CEAUSourceCompletenessObservedRecord:
    """Canonical runtime log record for a single source completeness observation."""

    record_type: RecordType
    record_id: str
    target_key: str
    recorded_at: datetime
    source_observation: SourceCompletenessObservation

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_type",
            _require_exact_record_type(self.record_type, "source_completeness_observed"),
        )
        object.__setattr__(self, "record_id", _require_text(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CEAUContractError),
        )
        object.__setattr__(
            self,
            "recorded_at",
            _require_datetime(self.recorded_at, "recorded_at"),
        )
        object.__setattr__(
            self,
            "source_observation",
            self.source_observation
            if isinstance(self.source_observation, SourceCompletenessObservation)
            else SourceCompletenessObservation.from_json_payload(
                self.source_observation
            ),
        )
        if self.source_observation.target_key != self.target_key:
            raise CEAUContractError(
                "source_observation.target_key must match record target_key."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "target_key": self.target_key,
            "recorded_at": self.recorded_at.isoformat(),
            "source_observation": self.source_observation.to_json_payload(),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUSourceCompletenessObservedRecord:
        payload_map = _require_dict(
            payload, "CEAUSourceCompletenessObservedRecord"
        )
        required_fields = {
            "record_type",
            "record_id",
            "target_key",
            "recorded_at",
            "source_observation",
        }
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "CEAUSourceCompletenessObservedRecord missing required field(s): "
                + ", ".join(missing_fields)
            )
        unexpected_fields = sorted(set(payload_map) - required_fields)
        if unexpected_fields:
            raise CEAUContractError(
                "CEAUSourceCompletenessObservedRecord has unexpected field(s): "
                + ", ".join(unexpected_fields)
            )

        return cls(
            record_type=_validate_record_type(
                _require_text(payload_map["record_type"], "record_type")
            ),
            record_id=_require_text(payload_map["record_id"], "record_id"),
            target_key=_require_text(payload_map["target_key"], "target_key"),
            recorded_at=_require_datetime(payload_map["recorded_at"], "recorded_at"),
            source_observation=SourceCompletenessObservation.from_json_payload(
                payload_map["source_observation"]
            ),
        )


@dataclass(frozen=True, slots=True)
class CEAUWatermarkObservedRecord:
    """Canonical runtime log record for watermark-observation snapshots."""

    record_type: RecordType
    record_id: str
    target_key: str
    recorded_at: datetime
    watermark_observation: WatermarkObservation

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_type",
            _require_exact_record_type(self.record_type, "watermark_observed"),
        )
        object.__setattr__(self, "record_id", _require_text(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CEAUContractError),
        )
        object.__setattr__(
            self,
            "recorded_at",
            _require_datetime(self.recorded_at, "recorded_at"),
        )
        object.__setattr__(
            self,
            "watermark_observation",
            self.watermark_observation
            if isinstance(self.watermark_observation, WatermarkObservation)
            else WatermarkObservation.from_json_payload(
                self.watermark_observation
            ),
        )
        if self.watermark_observation.target_key != self.target_key:
            raise CEAUContractError(
                "watermark_observation.target_key must match record target_key."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "target_key": self.target_key,
            "recorded_at": self.recorded_at.isoformat(),
            "watermark_observation": self.watermark_observation.to_json_payload(),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUWatermarkObservedRecord:
        payload_map = _require_dict(payload, "CEAUWatermarkObservedRecord")
        required_fields = {
            "record_type",
            "record_id",
            "target_key",
            "recorded_at",
            "watermark_observation",
        }
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "CEAUWatermarkObservedRecord missing required field(s): "
                + ", ".join(missing_fields)
            )
        unexpected_fields = sorted(set(payload_map) - required_fields)
        if unexpected_fields:
            raise CEAUContractError(
                "CEAUWatermarkObservedRecord has unexpected field(s): "
                + ", ".join(unexpected_fields)
            )

        return cls(
            record_type=_validate_record_type(
                _require_text(payload_map["record_type"], "record_type")
            ),
            record_id=_require_text(payload_map["record_id"], "record_id"),
            target_key=_require_text(payload_map["target_key"], "target_key"),
            recorded_at=_require_datetime(payload_map["recorded_at"], "recorded_at"),
            watermark_observation=WatermarkObservation.from_json_payload(
                payload_map["watermark_observation"]
            ),
        )


@dataclass(frozen=True, slots=True)
class CEAUUnitKey:
    """Compact immutable unit identity used by analysis-workbench surfaces."""

    target_key: str
    routing_lane: str
    event_family: str
    memory_anchor: str
    market_anchor: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=CEAUContractError),
        )
        object.__setattr__(
            self, "routing_lane", _require_text(self.routing_lane, "routing_lane")
        )
        object.__setattr__(
            self, "event_family", _require_text(self.event_family, "event_family")
        )
        object.__setattr__(
            self, "memory_anchor", _require_text(self.memory_anchor, "memory_anchor")
        )
        object.__setattr__(
            self, "market_anchor", _require_text(self.market_anchor, "market_anchor")
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "routing_lane": self.routing_lane,
            "event_family": self.event_family,
            "memory_anchor": self.memory_anchor,
            "market_anchor": self.market_anchor,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUUnitKey:
        payload_map = _require_dict(payload, "CEAUUnitKey")
        required_fields = {
            "target_key",
            "routing_lane",
            "event_family",
            "memory_anchor",
            "market_anchor",
        }
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "CEAUUnitKey missing required field(s): " + ", ".join(missing_fields)
            )
        unexpected_fields = sorted(set(payload_map) - required_fields)
        if unexpected_fields:
            raise CEAUContractError(
                "CEAUUnitKey has unexpected field(s): " + ", ".join(unexpected_fields)
            )
        return cls(
            target_key=_require_text(payload_map["target_key"], "target_key"),
            routing_lane=_require_text(payload_map["routing_lane"], "routing_lane"),
            event_family=_require_text(payload_map["event_family"], "event_family"),
            memory_anchor=_require_text(payload_map["memory_anchor"], "memory_anchor"),
            market_anchor=_require_text(payload_map["market_anchor"], "market_anchor"),
        )


@dataclass(frozen=True, slots=True)
class UnitFormationLane:
    """Compiled lane record for emitted CEAU analysis tasks."""

    analysis_unit_id: str
    unit_key: CEAUUnitKey
    unit_type: str
    unit_status: UnitFormationLaneStatus
    opened_at: datetime
    emitted_at: datetime | None
    business_at: datetime
    event_ids: tuple[str, ...]
    primary_event_ids: tuple[str, ...]
    context_event_ids: tuple[str, ...]
    unit_event_count: int
    emission_reason: EmissionReason
    emission_clock: EmissionClock
    event_time_completeness_claim: bool
    routing_reason_codes_by_event: tuple[str, ...]
    deadline_at: datetime
    max_delay_bars: int
    completeness_requirements: tuple[CompletenessRequirement, ...]
    advanced_completeness_requirements: tuple[str, ...]
    missing_completeness_requirements: tuple[str, ...]
    watermark_observation_ref: str | None
    domain_watermark_summary: tuple[str, ...]
    policy_version: str
    policy_hash: str
    policy_status: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "analysis_unit_id",
            _require_text(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(
            self,
            "unit_key",
            self.unit_key
            if isinstance(self.unit_key, CEAUUnitKey)
            else CEAUUnitKey.from_json_payload(self.unit_key),
        )
        object.__setattr__(self, "unit_type", _require_text(self.unit_type, "unit_type"))
        object.__setattr__(
            self,
            "unit_status",
            _validate_unit_status(_require_text(self.unit_status, "unit_status")),
        )
        object.__setattr__(self, "opened_at", _require_timestamp(self.opened_at, "opened_at"))
        object.__setattr__(
            self,
            "emitted_at",
            _optional_timestamp(self.emitted_at, "emitted_at"),
        )
        object.__setattr__(self, "business_at", _require_timestamp(self.business_at, "business_at"))
        object.__setattr__(
            self,
            "event_ids",
            _normalize_text_tuple(
                _require_list(self.event_ids, "event_ids"),
                field_name="event_ids",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "primary_event_ids",
            _normalize_text_tuple(
                _require_list(self.primary_event_ids, "primary_event_ids"),
                field_name="primary_event_ids",
            ),
        )
        object.__setattr__(
            self,
            "context_event_ids",
            _normalize_text_tuple(
                _require_list(self.context_event_ids, "context_event_ids"),
                field_name="context_event_ids",
            ),
        )
        object.__setattr__(
            self,
            "unit_event_count",
            _normalize_positive_int(self.unit_event_count, "unit_event_count"),
        )
        object.__setattr__(
            self,
            "emission_reason",
            _validate_emission_reason(_require_text(self.emission_reason, "emission_reason")),
        )
        object.__setattr__(
            self,
            "emission_clock",
            _validate_emission_clock(_require_text(self.emission_clock, "emission_clock")),
        )
        object.__setattr__(
            self,
            "event_time_completeness_claim",
            _require_bool(self.event_time_completeness_claim, "event_time_completeness_claim"),
        )
        object.__setattr__(
            self,
            "routing_reason_codes_by_event",
            _normalize_text_tuple(
                _require_list(
                    self.routing_reason_codes_by_event,
                    "routing_reason_codes_by_event",
                ),
                field_name="routing_reason_codes_by_event",
            ),
        )
        object.__setattr__(
            self,
            "deadline_at",
            _require_timestamp(self.deadline_at, "deadline_at"),
        )
        object.__setattr__(
            self,
            "max_delay_bars",
            _normalize_non_negative_int(self.max_delay_bars, "max_delay_bars"),
        )
        object.__setattr__(
            self,
            "completeness_requirements",
            tuple(
                item
                if isinstance(item, CompletenessRequirement)
                else CompletenessRequirement.from_json_payload(item)
                for item in _normalize_tuple(
                    self.completeness_requirements,
                    "completeness_requirements",
                )
            ),
        )
        object.__setattr__(
            self,
            "advanced_completeness_requirements",
            _normalize_text_tuple(
                _require_list(
                    self.advanced_completeness_requirements,
                    "advanced_completeness_requirements",
                ),
                field_name="advanced_completeness_requirements",
            ),
        )
        object.__setattr__(
            self,
            "missing_completeness_requirements",
            _normalize_text_tuple(
                _require_list(
                    self.missing_completeness_requirements,
                    "missing_completeness_requirements",
                ),
                field_name="missing_completeness_requirements",
            ),
        )
        object.__setattr__(
            self,
            "watermark_observation_ref",
            _optional_text(self.watermark_observation_ref),
        )
        object.__setattr__(
            self,
            "domain_watermark_summary",
            _normalize_text_tuple(
                _require_list(self.domain_watermark_summary, "domain_watermark_summary"),
                field_name="domain_watermark_summary",
            ),
        )
        object.__setattr__(
            self,
            "policy_version",
            _require_text(self.policy_version, "policy_version"),
        )
        object.__setattr__(
            self,
            "policy_hash",
            _validate_policy_hash(_require_text(self.policy_hash, "policy_hash")),
        )
        object.__setattr__(self, "policy_status", _optional_text(self.policy_status))

        if self.unit_event_count != len(self.event_ids):
            raise CEAUContractError(
                "unit_event_count must equal the number of event_ids."
            )
        if not set(self.primary_event_ids).issubset(set(self.event_ids)):
            raise CEAUContractError(
                "primary_event_ids must be a subset of event_ids."
            )
        if not set(self.context_event_ids).issubset(set(self.event_ids)):
            raise CEAUContractError(
                "context_event_ids must be a subset of event_ids."
            )
        if self.unit_status == "emitted" and self.emitted_at is None:
            raise CEAUContractError("emitted units require emitted_at.")
        if self.unit_status != "emitted" and self.emitted_at is not None:
            raise CEAUContractError(
                "emitted_at is only valid when unit_status=emitted."
            )
        if (
            self.emission_reason == "processing_time_safety_close"
            and self.emission_clock != "processing_time"
        ):
            raise CEAUContractError(
                "processing_time_safety_close is only valid with emission_clock=processing_time."
            )
        if (
            self.emission_reason == "processing_time_safety_close"
            and self.event_time_completeness_claim
        ):
            raise CEAUContractError(
                "processing_time_safety_close requires event_time_completeness_claim=false."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "analysis_unit_id": self.analysis_unit_id,
            "unit_key": self.unit_key.to_json_payload(),
            "unit_type": self.unit_type,
            "unit_status": self.unit_status,
            "opened_at": self.opened_at.isoformat(),
            "emitted_at": self.emitted_at.isoformat() if self.emitted_at else None,
            "business_at": self.business_at.isoformat(),
            "event_ids": list(self.event_ids),
            "primary_event_ids": list(self.primary_event_ids),
            "context_event_ids": list(self.context_event_ids),
            "unit_event_count": self.unit_event_count,
            "emission_reason": self.emission_reason,
            "emission_clock": self.emission_clock,
            "event_time_completeness_claim": self.event_time_completeness_claim,
            "routing_reason_codes_by_event": list(self.routing_reason_codes_by_event),
            "deadline_at": self.deadline_at.isoformat(),
            "max_delay_bars": self.max_delay_bars,
            "completeness_requirements": [
                req.to_json_payload() for req in self.completeness_requirements
            ],
            "advanced_completeness_requirements": list(
                self.advanced_completeness_requirements
            ),
            "missing_completeness_requirements": list(
                self.missing_completeness_requirements
            ),
            "watermark_observation_ref": self.watermark_observation_ref,
            "domain_watermark_summary": list(self.domain_watermark_summary),
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
            "policy_status": self.policy_status,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> UnitFormationLane:
        payload_map = _require_dict(payload, "UnitFormationLane")
        required_fields = {
            "analysis_unit_id",
            "unit_key",
            "unit_type",
            "unit_status",
            "opened_at",
            "business_at",
            "event_ids",
            "primary_event_ids",
            "context_event_ids",
            "unit_event_count",
            "emission_reason",
            "emission_clock",
            "event_time_completeness_claim",
            "routing_reason_codes_by_event",
            "deadline_at",
            "max_delay_bars",
            "completeness_requirements",
            "advanced_completeness_requirements",
            "missing_completeness_requirements",
            "domain_watermark_summary",
            "policy_version",
            "policy_hash",
        }
        optional_fields = {
            "emitted_at",
            "watermark_observation_ref",
            "policy_status",
        }
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "UnitFormationLane missing required field(s): "
                + ", ".join(missing_fields)
            )
        unexpected_fields = sorted(set(payload_map) - required_fields - optional_fields)
        if unexpected_fields:
            raise CEAUContractError(
                "UnitFormationLane has unexpected field(s): "
                + ", ".join(unexpected_fields)
            )

        return cls(
            analysis_unit_id=_require_text(payload_map["analysis_unit_id"], "analysis_unit_id"),
            unit_key=CEAUUnitKey.from_json_payload(payload_map["unit_key"]),
            unit_type=_require_text(payload_map["unit_type"], "unit_type"),
            unit_status=_validate_unit_status(
                _require_text(payload_map["unit_status"], "unit_status")
            ),
            opened_at=_require_datetime(payload_map["opened_at"], "opened_at"),
            emitted_at=_optional_iso_datetime(payload_map.get("emitted_at"), "emitted_at"),
            business_at=_require_datetime(payload_map["business_at"], "business_at"),
            event_ids=_normalize_text_tuple(
                _require_list(payload_map["event_ids"], "event_ids"),
                field_name="event_ids",
                allow_empty=False,
            ),
            primary_event_ids=_normalize_text_tuple(
                _require_list(payload_map["primary_event_ids"], "primary_event_ids"),
                field_name="primary_event_ids",
            ),
            context_event_ids=_normalize_text_tuple(
                _require_list(payload_map["context_event_ids"], "context_event_ids"),
                field_name="context_event_ids",
            ),
            unit_event_count=_normalize_positive_int(
                payload_map["unit_event_count"], "unit_event_count"
            ),
            emission_reason=_validate_emission_reason(
                _require_text(payload_map["emission_reason"], "emission_reason")
            ),
            emission_clock=_validate_emission_clock(
                _require_text(payload_map["emission_clock"], "emission_clock")
            ),
            event_time_completeness_claim=_require_bool(
                payload_map["event_time_completeness_claim"],
                "event_time_completeness_claim",
            ),
            routing_reason_codes_by_event=_normalize_text_tuple(
                _require_list(
                    payload_map["routing_reason_codes_by_event"],
                    "routing_reason_codes_by_event",
                ),
                field_name="routing_reason_codes_by_event",
            ),
            deadline_at=_require_datetime(payload_map["deadline_at"], "deadline_at"),
            max_delay_bars=_normalize_non_negative_int(
                payload_map["max_delay_bars"], "max_delay_bars"
            ),
            completeness_requirements=tuple(
                req
                if isinstance(req, CompletenessRequirement)
                else CompletenessRequirement.from_json_payload(req)
                for req in _normalize_tuple(
                    payload_map["completeness_requirements"],
                    "completeness_requirements",
                )
            ),
            advanced_completeness_requirements=_normalize_text_tuple(
                _require_list(
                    payload_map["advanced_completeness_requirements"],
                    "advanced_completeness_requirements",
                ),
                field_name="advanced_completeness_requirements",
            ),
            missing_completeness_requirements=_normalize_text_tuple(
                _require_list(
                    payload_map["missing_completeness_requirements"],
                    "missing_completeness_requirements",
                ),
                field_name="missing_completeness_requirements",
            ),
            watermark_observation_ref=_optional_text(
                payload_map.get("watermark_observation_ref"),
            ),
            domain_watermark_summary=_normalize_text_tuple(
                _require_list(
                    payload_map["domain_watermark_summary"],
                    "domain_watermark_summary",
                ),
                field_name="domain_watermark_summary",
            ),
            policy_version=_require_text(
                payload_map["policy_version"],
                "policy_version",
            ),
            policy_hash=_validate_policy_hash(
                _require_text(payload_map["policy_hash"], "policy_hash")
            ),
            policy_status=_optional_text(payload_map.get("policy_status")),
        )

    def payload_hash(self) -> str:
        return sha256(
            json_dumps(self.to_json_payload()).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class CEAUAnalysisCompletedPointer:
    """Pointer-only contract for completed analysis outcomes."""

    analysis_outcome_record_id: str
    analysis_outcome_path: str
    analysis_outcome_sha256: str
    completed_status: AnalysisCompletedStatus
    completed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "analysis_outcome_record_id",
            _require_text(self.analysis_outcome_record_id, "analysis_outcome_record_id"),
        )
        object.__setattr__(
            self,
            "analysis_outcome_path",
            _require_text(self.analysis_outcome_path, "analysis_outcome_path"),
        )
        object.__setattr__(
            self,
            "analysis_outcome_sha256",
            _validate_sha256(
                _require_text(
                    self.analysis_outcome_sha256,
                    "analysis_outcome_sha256",
                )
            ),
        )
        object.__setattr__(
            self,
            "completed_status",
            _validate_analysis_completed_status(
                _require_text(self.completed_status, "completed_status")
            ),
        )
        object.__setattr__(
            self,
            "completed_at",
            _require_timestamp(self.completed_at, "completed_at"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "analysis_outcome_record_id": self.analysis_outcome_record_id,
            "analysis_outcome_path": self.analysis_outcome_path,
            "analysis_outcome_sha256": self.analysis_outcome_sha256,
            "completed_status": self.completed_status,
            "completed_at": self.completed_at.isoformat(),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> CEAUAnalysisCompletedPointer:
        payload_map = _require_dict(payload, "CEAUAnalysisCompletedPointer")
        required_fields = {
            "analysis_outcome_record_id",
            "analysis_outcome_path",
            "analysis_outcome_sha256",
            "completed_status",
            "completed_at",
        }
        missing_fields = sorted(required_fields - set(payload_map))
        if missing_fields:
            raise CEAUContractError(
                "CEAUAnalysisCompletedPointer missing required field(s): "
                + ", ".join(missing_fields)
            )
        unexpected_fields = sorted(set(payload_map) - required_fields)
        if unexpected_fields:
            raise CEAUContractError(
                "CEAUAnalysisCompletedPointer has unexpected field(s): "
                + ", ".join(unexpected_fields)
            )

        return cls(
            analysis_outcome_record_id=_require_text(
                payload_map["analysis_outcome_record_id"],
                "analysis_outcome_record_id",
            ),
            analysis_outcome_path=_require_text(
                payload_map["analysis_outcome_path"], "analysis_outcome_path"
            ),
            analysis_outcome_sha256=_validate_sha256(
                _require_text(
                    payload_map["analysis_outcome_sha256"],
                    "analysis_outcome_sha256",
                )
            ),
            completed_status=_validate_analysis_completed_status(
                _require_text(payload_map["completed_status"], "completed_status")
            ),
            completed_at=_require_datetime(payload_map["completed_at"], "completed_at"),
        )


def _require_dict(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CEAUContractError(f"{field_name} must be a JSON object.")
    return cast(dict[str, object], value)


def _require_list(value: object, field_name: str) -> tuple[object, ...]:
    if not isinstance(value, list | tuple):
        raise CEAUContractError(f"{field_name} must be a list.")
    return tuple(value)


def _normalize_tuple(value: object, field_name: str) -> tuple[object, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    raise CEAUContractError(f"{field_name} must be an array.")


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise CEAUContractError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise CEAUContractError(f"{field_name} must not be empty.")
    return normalized


def _optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    return _require_text(value, "text")


def _require_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise CEAUContractError(f"{field_name} must be a datetime.")
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=CEAUContractError,
    )


def _require_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise CEAUContractError(
                f"{field_name} must be an ISO8601 datetime string."
            ) from exc
    else:
        raise CEAUContractError(
            f"{field_name} must be a datetime or ISO8601 datetime string."
        )
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=CEAUContractError,
    )


def _optional_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _require_datetime(value, field_name)


def _optional_iso_datetime(value: object, field_name: str) -> datetime | None:
    return _optional_datetime(value, field_name)


def _optional_timestamp(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise CEAUContractError(f"{field_name} must be a datetime.")
    return _require_timestamp(value, field_name)


def _normalize_text_tuple(
    values: tuple[object, ...],
    *,
    field_name: str = "field",
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if not values:
        if allow_empty:
            return ()
        raise CEAUContractError(f"{field_name} must not be empty.")
    normalized: list[str] = []
    for item in values:
        normalized.append(_require_text(item, field_name))
    return tuple(normalized)


def _normalize_non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CEAUContractError(f"{field_name} must be a non-negative integer.")
    if value < 0:
        raise CEAUContractError(f"{field_name} must be greater than or equal to zero.")
    return value


def _normalize_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CEAUContractError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise CEAUContractError(f"{field_name} must be greater than zero.")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CEAUContractError(f"{field_name} must be a boolean.")
    return value


def _reject_missing_or_unexpected(
    payload_map: dict[str, object],
    required_fields: set[str],
    optional_fields: set[str],
    contract_name: str,
) -> None:
    missing_fields = sorted(required_fields - set(payload_map))
    if missing_fields:
        raise CEAUContractError(
            f"{contract_name} missing required field(s): " + ", ".join(missing_fields)
        )
    unexpected_fields = sorted(set(payload_map) - required_fields - optional_fields)
    if unexpected_fields:
        raise CEAUContractError(
            f"{contract_name} has unexpected field(s): " + ", ".join(unexpected_fields)
        )


_UNIT_LIFECYCLE_REQUIRED_FIELDS = {
    "target_key",
    "recorded_at",
    "analysis_unit_id",
    "unit_key",
    "scope_key",
    "opened_at",
    "business_at",
    "deadline_at",
    "max_delay_bars",
    "unit_char_count",
    "event_ids",
    "primary_event_ids",
    "context_event_ids",
    "event_time_completeness_claim",
    "advanced_completeness_requirements",
    "missing_completeness_requirements",
    "policy_version",
    "policy_hash",
}


def _unit_lifecycle_kwargs(payload_map: dict[str, object]) -> dict[str, object]:
    return {
        "target_key": _require_text(payload_map["target_key"], "target_key"),
        "recorded_at": _require_datetime(payload_map["recorded_at"], "recorded_at"),
        "analysis_unit_id": _require_text(
            payload_map["analysis_unit_id"], "analysis_unit_id"
        ),
        "unit_key": CEAUUnitKey.from_json_payload(payload_map["unit_key"]),
        "scope_key": _normalize_scope_key(payload_map["scope_key"]),
        "opened_at": _require_datetime(payload_map["opened_at"], "opened_at"),
        "business_at": _require_datetime(payload_map["business_at"], "business_at"),
        "deadline_at": _require_datetime(payload_map["deadline_at"], "deadline_at"),
        "max_delay_bars": _normalize_non_negative_int(
            payload_map["max_delay_bars"], "max_delay_bars"
        ),
        "unit_char_count": _normalize_non_negative_int(
            payload_map["unit_char_count"], "unit_char_count"
        ),
        "event_ids": _normalize_text_tuple(
            _require_list(payload_map["event_ids"], "event_ids"),
            field_name="event_ids",
            allow_empty=False,
        ),
        "primary_event_ids": _normalize_text_tuple(
            _require_list(payload_map["primary_event_ids"], "primary_event_ids"),
            field_name="primary_event_ids",
            allow_empty=False,
        ),
        "context_event_ids": _normalize_text_tuple(
            _require_list(payload_map["context_event_ids"], "context_event_ids"),
            field_name="context_event_ids",
        ),
        "event_time_completeness_claim": _require_bool(
            payload_map["event_time_completeness_claim"],
            "event_time_completeness_claim",
        ),
        "advanced_completeness_requirements": _normalize_text_tuple(
            _require_list(
                payload_map["advanced_completeness_requirements"],
                "advanced_completeness_requirements",
            ),
            field_name="advanced_completeness_requirements",
        ),
        "missing_completeness_requirements": _normalize_text_tuple(
            _require_list(
                payload_map["missing_completeness_requirements"],
                "missing_completeness_requirements",
            ),
            field_name="missing_completeness_requirements",
        ),
        "policy_version": _require_text(payload_map["policy_version"], "policy_version"),
        "policy_hash": _validate_policy_hash(
            _require_text(payload_map["policy_hash"], "policy_hash")
        ),
    }


def _normalize_scope_key(value: object) -> tuple[str, str, str, str]:
    items = _normalize_text_tuple(_require_list(value, "scope_key"), field_name="scope_key")
    if len(items) != 4:
        raise CEAUContractError("scope_key must contain exactly four strings.")
    return items


def _normalize_unit_lifecycle_fields(
    record: CEAUUnitOpenedRecord | CEAUUnitAppendedRecord,
) -> None:
    object.__setattr__(
        record,
        "analysis_unit_id",
        _require_text(record.analysis_unit_id, "analysis_unit_id"),
    )
    object.__setattr__(
        record,
        "unit_key",
        record.unit_key
        if isinstance(record.unit_key, CEAUUnitKey)
        else CEAUUnitKey.from_json_payload(record.unit_key),
    )
    object.__setattr__(record, "scope_key", _normalize_scope_key(record.scope_key))
    object.__setattr__(record, "opened_at", _require_datetime(record.opened_at, "opened_at"))
    object.__setattr__(
        record,
        "business_at",
        _require_datetime(record.business_at, "business_at"),
    )
    object.__setattr__(
        record,
        "deadline_at",
        _require_datetime(record.deadline_at, "deadline_at"),
    )
    object.__setattr__(
        record,
        "max_delay_bars",
        _normalize_non_negative_int(record.max_delay_bars, "max_delay_bars"),
    )
    object.__setattr__(
        record,
        "unit_char_count",
        _normalize_non_negative_int(record.unit_char_count, "unit_char_count"),
    )
    object.__setattr__(
        record,
        "event_ids",
        _normalize_text_tuple(
            _require_list(record.event_ids, "event_ids"),
            field_name="event_ids",
            allow_empty=False,
        ),
    )
    object.__setattr__(
        record,
        "primary_event_ids",
        _normalize_text_tuple(
            _require_list(record.primary_event_ids, "primary_event_ids"),
            field_name="primary_event_ids",
            allow_empty=False,
        ),
    )
    object.__setattr__(
        record,
        "context_event_ids",
        _normalize_text_tuple(
            _require_list(record.context_event_ids, "context_event_ids"),
            field_name="context_event_ids",
        ),
    )
    object.__setattr__(
        record,
        "event_time_completeness_claim",
        _require_bool(
            record.event_time_completeness_claim,
            "event_time_completeness_claim",
        ),
    )
    object.__setattr__(
        record,
        "advanced_completeness_requirements",
        _normalize_text_tuple(
            _require_list(
                record.advanced_completeness_requirements,
                "advanced_completeness_requirements",
            ),
            field_name="advanced_completeness_requirements",
        ),
    )
    object.__setattr__(
        record,
        "missing_completeness_requirements",
        _normalize_text_tuple(
            _require_list(
                record.missing_completeness_requirements,
                "missing_completeness_requirements",
            ),
            field_name="missing_completeness_requirements",
        ),
    )
    _validate_unit_event_sets(
        event_ids=record.event_ids,
        primary_event_ids=record.primary_event_ids,
        context_event_ids=record.context_event_ids,
    )


def _validate_record_audit_fields(
    record: CEAUEventRouteRecord
    | CEAUUnitOpenedRecord
    | CEAUUnitAppendedRecord
    | CEAUUnitEmittedRecord,
) -> None:
    object.__setattr__(
        record,
        "record_id",
        _require_text(record.record_id, "record_id"),
    )
    object.__setattr__(
        record,
        "target_key",
        validate_target_key(record.target_key, error_type=CEAUContractError),
    )
    object.__setattr__(
        record,
        "recorded_at",
        _require_datetime(record.recorded_at, "recorded_at"),
    )
    object.__setattr__(
        record,
        "policy_version",
        _require_text(record.policy_version, "policy_version"),
    )
    object.__setattr__(
        record,
        "policy_hash",
        _validate_policy_hash(_require_text(record.policy_hash, "policy_hash")),
    )


def _validate_record_type(value: str) -> RecordType:
    if value not in {
        "event_route",
        "watch_opened",
        "watch_matched",
        "watch_expired",
        "unit_opened",
        "unit_appended",
        "unit_emitted",
        "unit_closed",
        "unit_expired",
        "late_followup",
        "analysis_queued",
        "analysis_started",
        "analysis_completed",
        "analysis_failed",
        "source_completeness_observed",
        "watermark_observed",
    }:
        raise CEAUContractError("record_type is not a supported CEAU record type.")
    return cast(RecordType, value)


def _require_exact_record_type(value: object, expected: RecordType) -> RecordType:
    record_type = _validate_record_type(_require_text(value, "record_type"))
    if record_type != expected:
        raise CEAUContractError(f"record_type must be {expected}.")
    return record_type


def _validate_final_route(value: str) -> CEAURoute:
    if value not in {"no_action", "watch", "accumulate", "emit"}:
        raise CEAUContractError(
            "final_route must be one of: no_action, watch, accumulate, emit."
        )
    return cast(CEAURoute, value)


def _validate_route_downstream_identity(
    *,
    final_route: CEAURoute,
    analysis_unit_id: str | None,
    watch_id: str | None,
) -> None:
    if final_route in {"emit", "accumulate"}:
        if analysis_unit_id is None:
            raise CEAUContractError(
                "analysis_unit_id is required when final_route is emit or accumulate."
            )
        if watch_id is not None:
            raise CEAUContractError(
                "watch_id is only valid when final_route is watch."
            )
        return

    if final_route == "watch":
        if watch_id is None:
            raise CEAUContractError("watch_id is required when final_route is watch.")
        if analysis_unit_id is not None:
            raise CEAUContractError(
                "analysis_unit_id is only valid when final_route is emit or accumulate."
            )
        return

    if analysis_unit_id is not None or watch_id is not None:
        raise CEAUContractError(
            "no_action routes must not carry analysis_unit_id or watch_id."
        )


def _validate_unit_event_sets(
    *,
    event_ids: tuple[str, ...],
    primary_event_ids: tuple[str, ...],
    context_event_ids: tuple[str, ...],
) -> None:
    event_id_set = set(event_ids)
    if not set(primary_event_ids).issubset(event_id_set):
        raise CEAUContractError("primary_event_ids must be a subset of event_ids.")
    if not set(context_event_ids).issubset(event_id_set):
        raise CEAUContractError("context_event_ids must be a subset of event_ids.")


def _validate_completeness_purpose(value: str) -> CompletenessPurpose:
    if value not in {"absence_inference", "evidence_maturity"}:
        raise CEAUContractError(
            "completeness purpose must be one of: absence_inference, evidence_maturity."
        )
    return cast(CompletenessPurpose, value)


def _validate_completeness_domain(value: str) -> CompletenessDomain:
    if value not in {
        "market_bars",
        "news_web_search",
        "official_filings",
        "operator_admission",
    }:
        raise CEAUContractError(
            "completeness_domain must be one of: market_bars, news_web_search, "
            "official_filings, operator_admission."
        )
    return cast(CompletenessDomain, value)


def _validate_completeness_model(value: str) -> CompletenessModel:
    if value not in {
        "complete_through",
        "lower_bound_only",
        "poll_snapshot",
        "unknown",
    }:
        raise CEAUContractError(
            "completeness_model must be one of: complete_through, lower_bound_only, "
            "poll_snapshot, unknown."
        )
    return cast(CompletenessModel, value)


def _validate_completeness_status(value: str) -> CompletenessStatus:
    if value not in {
        "healthy",
        "stale",
        "unavailable",
        "unknown_completeness",
    }:
        raise CEAUContractError(
            "status must be one of: healthy, stale, unavailable, unknown_completeness."
        )
    return cast(CompletenessStatus, value)


def _validate_domain_watermark_status(value: str) -> DomainWatermarkStatus:
    if value not in {"advanced", "frozen", "unavailable"}:
        raise CEAUContractError(
            "DomainWatermark.status must be one of: advanced, frozen, unavailable."
        )
    return cast(DomainWatermarkStatus, value)


def _validate_overall_watermark_status(value: str) -> WatermarkOverallStatus:
    if value not in {
        "advanced",
        "partially_advanced",
        "frozen_no_source_completeness",
        "frozen_stale_source",
        "unavailable",
    }:
        raise CEAUContractError(
            "overall_status must be one of: advanced, partially_advanced, "
            "frozen_no_source_completeness, frozen_stale_source, unavailable."
        )
    return cast(WatermarkOverallStatus, value)


def _validate_runtime_scope(value: str) -> RuntimeScope:
    if value not in {"live", "replay"}:
        raise CEAUContractError("runtime_scope must be one of: live, replay.")
    return cast(RuntimeScope, value)


def _validate_emission_clock(value: str) -> EmissionClock:
    if value not in {"event_time", "processing_time"}:
        raise CEAUContractError(
            "emission_clock must be one of: event_time, processing_time."
        )
    return cast(EmissionClock, value)


def _validate_emission_reason(value: str) -> EmissionReason:
    if value not in {
        "hard_trigger",
        "processing_time_safety_close",
        "replay_final_drain",
        "context_budget",
        "other",
    }:
        raise CEAUContractError(
            "emission_reason must be one of: hard_trigger, "
            "processing_time_safety_close, replay_final_drain, context_budget, other."
        )
    return cast(EmissionReason, value)


def _validate_unit_status(value: str) -> UnitFormationLaneStatus:
    if value not in {"pending", "emitted", "closed", "expired"}:
        raise CEAUContractError(
            "unit_status must be one of: pending, emitted, closed, expired."
        )
    return cast(UnitFormationLaneStatus, value)


def _validate_analysis_completed_status(value: str) -> AnalysisCompletedStatus:
    if value not in {"no_update", "memory_updated"}:
        raise CEAUContractError(
            "completed_status must be one of: no_update, memory_updated."
        )
    return cast(AnalysisCompletedStatus, value)


def _validate_policy_hash(value: str) -> str:
    return _validate_sha256_digest(value, "policy_hash")


def _validate_sha256(value: str) -> str:
    return _validate_sha256_digest(value, "analysis_outcome_sha256")


def _validate_sha256_digest(value: str, field_name: str) -> str:
    if len(value) != 64:
        raise CEAUContractError(f"{field_name} must be a 64-char hex digest.")
    try:
        int(value, 16)
    except ValueError as exc:
        raise CEAUContractError(
            f"{field_name} must be a 64-char hex digest."
        ) from exc
    return value.lower()


def json_dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "AnalysisCompletedStatus",
    "CEAUAnalysisFailedRecord",
    "CEAUAnalysisQueuedRecord",
    "CEAUAnalysisStartedRecord",
    "CEAUAnalysisCompletedRecord",
    "CEAUAnalysisCompletedPointer",
    "CEAUContractError",
    "CEAUEventRouteRecord",
    "CEAURecordEnvelope",
    "CEAURoute",
    "CEAUUnitKey",
    "CEAUUnitOpenedRecord",
    "CEAUUnitAppendedRecord",
    "CEAUUnitEmittedRecord",
    "CandidateRoute",
    "CompletenessDomain",
    "CompletenessModel",
    "CompletenessPurpose",
    "CompletenessRequirement",
    "CompletenessStatus",
    "CEAUSourceCompletenessObservedRecord",
    "CEAUWatermarkObservedRecord",
    "DomainWatermark",
    "DomainWatermarkStatus",
    "EmissionClock",
    "EmissionReason",
    "RecordType",
    "RuntimeScope",
    "SourceCompletenessObservation",
    "UnitFormationLane",
    "UnitFormationLaneStatus",
    "WatermarkObservation",
    "WatermarkOverallStatus",
    "json_dumps",
]
