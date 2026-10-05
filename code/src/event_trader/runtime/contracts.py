"""Typed message contracts for the Nautilus-aligned runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from event_trader.contracts._validators import (
    normalize_content,
    validate_event_id,
    validate_source_ref,
    validate_target_key,
    validate_timestamp,
)

from .topics import (
    analysis_failed_topic,
    analysis_outcome_topic,
    analysis_requested_topic,
    ceau_event_routed_topic,
    ceau_source_completeness_observed_topic,
    ceau_unit_appended_topic,
    ceau_unit_closed_topic,
    ceau_unit_emitted_topic,
    ceau_unit_opened_topic,
    ceau_watermark_observed_topic,
    checker_decision_topic,
    checker_failed_topic,
    checker_requested_topic,
    component_heartbeat_topic,
    evidence_admitted_topic,
    evidence_quarantined_topic,
    news_raw_topic,
    pm_review_completed_topic,
    pm_review_failed_topic,
    pm_review_requested_topic,
    pm_review_skipped_topic,
    runtime_dead_letter_topic,
    web_result_raw_topic,
)

_SCHEMA_VERSION = "runtime.v1"


class RuntimeMessageContractError(ValueError):
    """Raised when a runtime message violates its contract."""


@dataclass(frozen=True, slots=True)
class RuntimeMessage:
    """Common metadata carried by all runtime messages."""

    message_id: str
    schema_version: str
    topic: str
    event_time: datetime
    recorded_at: datetime
    correlation_id: str
    causation_id: str | None
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", _validate_identifier(self.message_id, "message_id"))
        object.__setattr__(
            self,
            "schema_version",
            _validate_schema_version(self.schema_version),
        )
        object.__setattr__(self, "topic", _validate_optional_topic(self.topic))
        object.__setattr__(
            self,
            "event_time",
            validate_timestamp(
                self.event_time,
                field_name="event_time",
                error_type=RuntimeMessageContractError,
            ),
        )
        object.__setattr__(
            self,
            "recorded_at",
            validate_timestamp(
                self.recorded_at,
                field_name="recorded_at",
                error_type=RuntimeMessageContractError,
            ),
        )
        object.__setattr__(
            self,
            "correlation_id",
            _validate_identifier(self.correlation_id, "correlation_id"),
        )
        object.__setattr__(
            self,
            "causation_id",
            _validate_optional_identifier(self.causation_id, "causation_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _validate_identifier(self.idempotency_key, "idempotency_key"),
        )

    def to_json_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        for field_name, value in asdict(self).items():
            if isinstance(value, datetime):
                payload[field_name] = value.isoformat()
            else:
                payload[field_name] = value
        return payload


@dataclass(frozen=True, slots=True)
class TargetedRuntimeMessage(RuntimeMessage):
    """Runtime message scoped to one target key."""

    target_key: str

    def __post_init__(self) -> None:
        RuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=RuntimeMessageContractError),
        )


@dataclass(frozen=True, slots=True)
class NewsRaw(TargetedRuntimeMessage):
    source_ref: str
    record_id: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=RuntimeMessageContractError),
        )
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, news_raw_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class WebResultRaw(TargetedRuntimeMessage):
    source_ref: str
    record_id: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=RuntimeMessageContractError),
        )
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, web_result_raw_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class EvidenceAdmitted(TargetedRuntimeMessage):
    event_id: str
    source_ref: str
    evidence_record_ref: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "event_id",
            validate_event_id(self.event_id, error_type=RuntimeMessageContractError),
        )
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=RuntimeMessageContractError),
        )
        object.__setattr__(
            self,
            "evidence_record_ref",
            _validate_identifier(self.evidence_record_ref, "evidence_record_ref"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, evidence_admitted_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class EvidenceQuarantined(TargetedRuntimeMessage):
    source_ref: str
    quarantine_record_ref: str
    reason_code: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "source_ref",
            validate_source_ref(self.source_ref, error_type=RuntimeMessageContractError),
        )
        object.__setattr__(
            self,
            "quarantine_record_ref",
            _validate_identifier(self.quarantine_record_ref, "quarantine_record_ref"),
        )
        object.__setattr__(
            self,
            "reason_code",
            _validate_reason_code(self.reason_code, "reason_code"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, evidence_quarantined_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class CheckerRequested(TargetedRuntimeMessage):
    event_id: str
    evidence_record_ref: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "event_id",
            validate_event_id(self.event_id, error_type=RuntimeMessageContractError),
        )
        object.__setattr__(
            self,
            "evidence_record_ref",
            _validate_identifier(self.evidence_record_ref, "evidence_record_ref"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, checker_requested_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class CheckerDecision(TargetedRuntimeMessage):
    event_id: str
    evidence_record_ref: str
    decision_record_ref: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "event_id",
            validate_event_id(self.event_id, error_type=RuntimeMessageContractError),
        )
        object.__setattr__(
            self,
            "evidence_record_ref",
            _validate_identifier(self.evidence_record_ref, "evidence_record_ref"),
        )
        object.__setattr__(
            self,
            "decision_record_ref",
            _validate_identifier(self.decision_record_ref, "decision_record_ref"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, checker_decision_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class CheckerFailed(TargetedRuntimeMessage):
    event_id: str
    evidence_record_ref: str
    failure_record_ref: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "event_id",
            validate_event_id(self.event_id, error_type=RuntimeMessageContractError),
        )
        object.__setattr__(
            self,
            "evidence_record_ref",
            _validate_identifier(self.evidence_record_ref, "evidence_record_ref"),
        )
        object.__setattr__(
            self,
            "failure_record_ref",
            _validate_identifier(self.failure_record_ref, "failure_record_ref"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, checker_failed_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class CEAUEventRouted(TargetedRuntimeMessage):
    event_id: str
    evidence_record_ref: str
    record_id: str
    analysis_unit_id: str | None = None

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "event_id",
            validate_event_id(self.event_id, error_type=RuntimeMessageContractError),
        )
        object.__setattr__(
            self,
            "evidence_record_ref",
            _validate_identifier(self.evidence_record_ref, "evidence_record_ref"),
        )
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "analysis_unit_id",
            _validate_optional_identifier(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, ceau_event_routed_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class CEAUUnitOpened(TargetedRuntimeMessage):
    analysis_unit_id: str
    record_id: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "analysis_unit_id",
            _validate_identifier(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, ceau_unit_opened_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class CEAUUnitAppended(TargetedRuntimeMessage):
    analysis_unit_id: str
    record_id: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "analysis_unit_id",
            _validate_identifier(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, ceau_unit_appended_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class CEAUUnitEmitted(TargetedRuntimeMessage):
    analysis_unit_id: str
    record_id: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "analysis_unit_id",
            _validate_identifier(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, ceau_unit_emitted_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class CEAUUnitClosed(TargetedRuntimeMessage):
    analysis_unit_id: str
    record_id: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "analysis_unit_id",
            _validate_identifier(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, ceau_unit_closed_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class CEAUWatermarkObserved(TargetedRuntimeMessage):
    record_id: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, ceau_watermark_observed_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class CEAUSourceCompletenessObserved(TargetedRuntimeMessage):
    record_id: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "topic",
            _derived_topic(
                self.topic,
                ceau_source_completeness_observed_topic(self.target_key),
            ),
        )


@dataclass(frozen=True, slots=True)
class AnalysisRequested(TargetedRuntimeMessage):
    analysis_unit_id: str
    record_id: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "analysis_unit_id",
            _validate_identifier(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, analysis_requested_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class AnalysisOutcome(TargetedRuntimeMessage):
    analysis_unit_id: str
    record_id: str
    outcome_record_ref: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "analysis_unit_id",
            _validate_identifier(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "outcome_record_ref",
            _validate_identifier(self.outcome_record_ref, "outcome_record_ref"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, analysis_outcome_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class AnalysisFailed(TargetedRuntimeMessage):
    analysis_unit_id: str
    record_id: str
    failure_record_ref: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "analysis_unit_id",
            _validate_identifier(self.analysis_unit_id, "analysis_unit_id"),
        )
        object.__setattr__(self, "record_id", _validate_identifier(self.record_id, "record_id"))
        object.__setattr__(
            self,
            "failure_record_ref",
            _validate_identifier(self.failure_record_ref, "failure_record_ref"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, analysis_failed_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class PMReviewRequested(TargetedRuntimeMessage):
    pm_review_request_id: str
    pm_review_request_ref: str
    source_analysis_unit_id: str | None = None
    source_record_id: str | None = None
    source_outcome_record_ref: str | None = None

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        for field_name in ("pm_review_request_id", "pm_review_request_ref"):
            object.__setattr__(
                self,
                field_name,
                _validate_identifier(getattr(self, field_name), field_name),
            )
        for field_name in (
            "source_analysis_unit_id",
            "source_record_id",
            "source_outcome_record_ref",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_optional_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, pm_review_requested_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class PMReviewSkipped(TargetedRuntimeMessage):
    skip_reason: str
    pm_review_request_id: str | None = None
    pm_review_request_ref: str | None = None
    source_analysis_unit_id: str | None = None
    source_record_id: str | None = None
    source_outcome_record_ref: str | None = None

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        object.__setattr__(
            self,
            "skip_reason",
            _validate_reason_code(self.skip_reason, "skip_reason"),
        )
        object.__setattr__(
            self,
            "pm_review_request_id",
            _validate_optional_identifier(
                self.pm_review_request_id,
                "pm_review_request_id",
            ),
        )
        object.__setattr__(
            self,
            "pm_review_request_ref",
            _validate_optional_identifier(
                self.pm_review_request_ref,
                "pm_review_request_ref",
            ),
        )
        for field_name in (
            "source_analysis_unit_id",
            "source_record_id",
            "source_outcome_record_ref",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_optional_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, pm_review_skipped_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class PMReviewCompleted(TargetedRuntimeMessage):
    pm_review_request_id: str
    source_episode_id: str | None
    pm_decision_ref: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        for field_name in ("pm_review_request_id", "pm_decision_ref"):
            object.__setattr__(
                self,
                field_name,
                _validate_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "source_episode_id",
            _validate_optional_identifier(self.source_episode_id, "source_episode_id"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, pm_review_completed_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class PMReviewFailed(TargetedRuntimeMessage):
    pm_review_request_id: str
    failure_record_ref: str

    def __post_init__(self) -> None:
        TargetedRuntimeMessage.__post_init__(self)
        for field_name in ("pm_review_request_id", "failure_record_ref"):
            object.__setattr__(
                self,
                field_name,
                _validate_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, pm_review_failed_topic(self.target_key)),
        )


@dataclass(frozen=True, slots=True)
class RuntimeDeadLetter(RuntimeMessage):
    component: str
    failed_topic: str
    dead_letter_ref: str

    def __post_init__(self) -> None:
        RuntimeMessage.__post_init__(self)
        object.__setattr__(self, "component", _validate_identifier(self.component, "component"))
        object.__setattr__(self, "failed_topic", _validate_topic_text(self.failed_topic))
        object.__setattr__(
            self,
            "dead_letter_ref",
            _validate_identifier(self.dead_letter_ref, "dead_letter_ref"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, runtime_dead_letter_topic(self.component)),
        )


@dataclass(frozen=True, slots=True)
class ComponentHeartbeat(RuntimeMessage):
    component: str
    heartbeat_ref: str

    def __post_init__(self) -> None:
        RuntimeMessage.__post_init__(self)
        object.__setattr__(self, "component", _validate_identifier(self.component, "component"))
        object.__setattr__(
            self,
            "heartbeat_ref",
            _validate_identifier(self.heartbeat_ref, "heartbeat_ref"),
        )
        object.__setattr__(
            self,
            "topic",
            _derived_topic(self.topic, component_heartbeat_topic(self.component)),
        )


type RawRuntimeMessage = NewsRaw | WebResultRaw
type EvidenceRuntimeMessage = EvidenceAdmitted | EvidenceQuarantined
type CheckerRuntimeMessage = CheckerRequested | CheckerDecision | CheckerFailed
type CEAURuntimeMessage = (
    CEAUEventRouted
    | CEAUUnitOpened
    | CEAUUnitAppended
    | CEAUUnitEmitted
    | CEAUUnitClosed
    | CEAUWatermarkObserved
    | CEAUSourceCompletenessObserved
)
type AnalysisRuntimeMessage = AnalysisRequested | AnalysisOutcome | AnalysisFailed
type PMReviewRuntimeMessage = (
    PMReviewRequested | PMReviewSkipped | PMReviewCompleted | PMReviewFailed
)
type RuntimeLifecycleMessage = RuntimeDeadLetter | ComponentHeartbeat


def base_message_kwargs(
    *,
    message_id: str,
    event_time: datetime,
    recorded_at: datetime,
    correlation_id: str,
    causation_id: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Build the common metadata kwargs for one runtime message."""
    return {
        "message_id": message_id,
        "schema_version": _SCHEMA_VERSION,
        "topic": "",
        "event_time": event_time,
        "recorded_at": recorded_at,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "idempotency_key": idempotency_key,
    }


def _validate_schema_version(value: str) -> str:
    if value != _SCHEMA_VERSION:
        raise RuntimeMessageContractError(
            f"schema_version must be {_SCHEMA_VERSION!r}."
        )
    return value


def _validate_optional_topic(value: str) -> str:
    if not isinstance(value, str):
        raise RuntimeMessageContractError("topic must be a string.")
    normalized = value.strip()
    if normalized and normalized != value:
        raise RuntimeMessageContractError(
            "topic must not include leading or trailing whitespace."
        )
    return normalized


def _validate_topic_text(value: str) -> str:
    return _validate_reason_code(value, "failed_topic")


def _validate_identifier(value: str, field_name: str) -> str:
    return normalize_content(
        value,
        field_name=field_name,
        error_type=RuntimeMessageContractError,
    ).strip()


def _validate_optional_identifier(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_identifier(value, field_name)


def _validate_reason_code(value: str, field_name: str) -> str:
    normalized = _validate_identifier(value, field_name)
    if any(ch.isspace() for ch in normalized):
        raise RuntimeMessageContractError(f"{field_name} must not contain whitespace.")
    return normalized


def _derived_topic(actual: str, expected: str) -> str:
    if actual and actual != expected:
        raise RuntimeMessageContractError(
            f"topic must match the canonical derived topic {expected!r}."
        )
    return expected


__all__ = [
    "AnalysisFailed",
    "AnalysisOutcome",
    "AnalysisRequested",
    "AnalysisRuntimeMessage",
    "CEAUEventRouted",
    "CEAURuntimeMessage",
    "CEAUSourceCompletenessObserved",
    "CEAUUnitAppended",
    "CEAUUnitClosed",
    "CEAUUnitEmitted",
    "CEAUUnitOpened",
    "CEAUWatermarkObserved",
    "CheckerDecision",
    "CheckerFailed",
    "CheckerRequested",
    "CheckerRuntimeMessage",
    "ComponentHeartbeat",
    "EvidenceAdmitted",
    "EvidenceQuarantined",
    "EvidenceRuntimeMessage",
    "NewsRaw",
    "PMReviewCompleted",
    "PMReviewFailed",
    "PMReviewRequested",
    "PMReviewRuntimeMessage",
    "PMReviewSkipped",
    "RawRuntimeMessage",
    "RuntimeDeadLetter",
    "RuntimeLifecycleMessage",
    "RuntimeMessage",
    "RuntimeMessageContractError",
    "TargetedRuntimeMessage",
    "WebResultRaw",
    "base_message_kwargs",
]
