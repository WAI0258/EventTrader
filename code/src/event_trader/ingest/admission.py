"""Deterministic admission-boundary validation, identity, and metadata helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from event_trader.contracts._validators import (
    validate_labels,
    validate_target_key,
    validate_timestamp,
)
from event_trader.contracts.evidence import (
    EvidenceEvent,
    EvidenceLedgerRecord,
    derive_event_id,
)
from event_trader.contracts.runtime import admission_lane
from event_trader.feeds.models import (
    HistoricalMacroApiInput,
    HistoricalManualDatasetInput,
    HistoricalMarketNewsInput,
    HistoricalNewsStreamInput,
    HistoricalWebSearchInput,
    LiveMacroApiInput,
    LiveManualSourceInput,
    LiveNewsStreamInput,
    LiveWebSearchInput,
)
from event_trader.replay.timestamps import replay_ts_event, replay_ts_source
from event_trader.source_policy import (
    ensure_source_classification_labels,
    validate_source_classification_labels,
)


class AdmissionError(ValueError):
    """Raised when gathered material cannot cross the admission boundary."""


type AdmissionIngressInput = (
    LiveNewsStreamInput
    | LiveWebSearchInput
    | LiveMacroApiInput
    | LiveManualSourceInput
    | HistoricalNewsStreamInput
    | HistoricalMarketNewsInput
    | HistoricalMacroApiInput
    | HistoricalManualDatasetInput
    | HistoricalWebSearchInput
)

type AdmissionMacroInput = LiveMacroApiInput | HistoricalMacroApiInput

_INGRESS_INPUT_TYPES = (
    LiveNewsStreamInput,
    LiveWebSearchInput,
    LiveMacroApiInput,
    LiveManualSourceInput,
    HistoricalNewsStreamInput,
    HistoricalMarketNewsInput,
    HistoricalMacroApiInput,
    HistoricalManualDatasetInput,
    HistoricalWebSearchInput,
)
_LEDGER_BODY_MAX_CHARS = 8000
_LEDGER_TRUNCATION_MARKER = (
    "[truncated_for_ledger: full source text retained in source_archive or "
    "source_ref-backed source store]"
)


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """Validated gathered material plus explicit admission metadata."""

    target_key: str
    ingress_input: AdmissionIngressInput
    labels: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=AdmissionError),
        )
        object.__setattr__(
            self,
            "ingress_input",
            _validate_ingress_input(self.ingress_input),
        )
        object.__setattr__(
            self,
            "labels",
            validate_labels(self.labels, error_type=AdmissionError),
        )


@dataclass(frozen=True, slots=True)
class AdmissionMetadata:
    """Explicit timestamps and business-lane metadata derived from a request."""

    target_key: str
    source_ref: str
    ts_source: datetime
    ts_event: datetime
    admission_lane: str


@dataclass(frozen=True, slots=True)
class AdmissionOutputs:
    """Deterministically shaped outputs reserved for append and publish."""

    ledger_record: EvidenceLedgerRecord
    runtime_event: EvidenceEvent


def validate_admission_request(
    *,
    target_key: str,
    ingress_input: object,
    labels: list[str] | None = None,
) -> AdmissionRequest:
    """Validate gathered material before identity or output shaping occurs."""
    return AdmissionRequest(
        target_key=target_key,
        ingress_input=_validate_ingress_input(ingress_input),
        labels=[] if labels is None else labels,
    )


def derive_admission_event_id(
    request: AdmissionRequest,
) -> str:
    """Derive stable evidence identity from a validated admission request."""
    validated_request = _validate_request(request)
    ingress_input = validated_request.ingress_input
    return derive_event_id(
        target_key=validated_request.target_key,
        source_ref=ingress_input.source_ref,
        ts_event=_admission_ts_event(ingress_input),
        content=_identity_content(ingress_input),
    )


def derive_admission_metadata(request: AdmissionRequest) -> AdmissionMetadata:
    """Expose explicit timestamp and routing metadata.

    The result stays narrower than full output shaping.
    """
    validated_request = _validate_request(request)
    ingress_input = validated_request.ingress_input
    return AdmissionMetadata(
        target_key=validated_request.target_key,
        source_ref=ingress_input.source_ref,
        ts_source=_admission_ts_source(ingress_input),
        ts_event=_admission_ts_event(ingress_input),
        admission_lane=admission_lane(validated_request.target_key),
    )


def shape_admission_outputs(
    request: AdmissionRequest,
    *,
    ts_init: datetime,
) -> AdmissionOutputs:
    """Shape the deterministic ledger and runtime outputs for one admission."""
    validated_request = _validate_request(request)
    validated_ts_init = validate_timestamp(
        ts_init,
        field_name="ts_init",
        error_type=AdmissionError,
    )
    metadata = derive_admission_metadata(validated_request)
    event_id = derive_admission_event_id(validated_request)
    ingress_input = validated_request.ingress_input

    return AdmissionOutputs(
        ledger_record=EvidenceLedgerRecord(
            event_id=event_id,
            target_key=metadata.target_key,
            source_ref=metadata.source_ref,
            title=_ledger_title(ingress_input),
            content=_ledger_content(ingress_input),
            labels=_ledger_labels(
                ingress_input=ingress_input,
                labels=validated_request.labels,
            ),
            ts_source=metadata.ts_source,
            ts_event=metadata.ts_event,
            ts_init=validated_ts_init,
        ),
        runtime_event=EvidenceEvent(
            event_id=event_id,
            target_key=metadata.target_key,
            source_ref=metadata.source_ref,
            ts_source=metadata.ts_source,
            ts_event=metadata.ts_event,
            ts_init=validated_ts_init,
        ),
    )


def _validate_request(request: AdmissionRequest) -> AdmissionRequest:
    if not isinstance(request, AdmissionRequest):
        raise AdmissionError("request must be an AdmissionRequest instance.")
    return request


def _validate_ingress_input(value: object) -> AdmissionIngressInput:
    if not isinstance(value, _INGRESS_INPUT_TYPES):
        raise AdmissionError(
            "ingress_input must be one of the canonical feed boundary models; "
            "pass a validated event_trader.feeds input instead of raw payload "
            "data."
        )
    return value


def _admission_ts_source(value: AdmissionIngressInput) -> datetime:
    if isinstance(value, LiveNewsStreamInput):
        return value.published_at
    if isinstance(value, LiveWebSearchInput):
        published_at = value.published_at
        if published_at is None:
            return value.discovered_at
        return published_at
    if isinstance(value, LiveMacroApiInput):
        return value.released_at
    if isinstance(value, LiveManualSourceInput):
        return value.provided_at
    return replay_ts_source(value)


def _admission_ts_event(value: AdmissionIngressInput) -> datetime:
    if isinstance(value, LiveNewsStreamInput):
        return value.captured_at
    if isinstance(value, LiveWebSearchInput):
        return value.discovered_at
    if isinstance(value, LiveMacroApiInput):
        return value.released_at
    if isinstance(value, LiveManualSourceInput):
        return value.provided_at
    return replay_ts_event(value)


def _ledger_title(value: AdmissionIngressInput) -> str:
    if isinstance(
        value,
        (LiveNewsStreamInput, HistoricalNewsStreamInput, HistoricalMarketNewsInput),
    ):
        return value.headline
    if isinstance(value, (LiveWebSearchInput, HistoricalWebSearchInput)):
        return value.title
    if isinstance(
        value,
        (LiveManualSourceInput, HistoricalManualDatasetInput),
    ):
        return value.title
    return _render_macro_title(value)


def _ledger_content(value: AdmissionIngressInput) -> str:
    if isinstance(
        value,
        (LiveNewsStreamInput, HistoricalNewsStreamInput, HistoricalMarketNewsInput),
    ):
        return _bounded_ledger_body(value.body)
    return _identity_content(value)


def _identity_content(value: AdmissionIngressInput) -> str:
    if isinstance(
        value,
        (LiveNewsStreamInput, HistoricalNewsStreamInput, HistoricalMarketNewsInput),
    ):
        return value.body
    if isinstance(value, (LiveWebSearchInput, HistoricalWebSearchInput)):
        return value.content
    if isinstance(
        value,
        (LiveManualSourceInput, HistoricalManualDatasetInput),
    ):
        return value.content
    return _render_macro_content(value)


def _ledger_labels(
    *,
    ingress_input: AdmissionIngressInput,
    labels: list[str],
) -> list[str]:
    if isinstance(ingress_input, (LiveWebSearchInput, HistoricalWebSearchInput)):
        return validate_source_classification_labels(labels=labels)
    return ensure_source_classification_labels(
        source_ref=ingress_input.source_ref,
        title=_ledger_title(ingress_input),
        content=_identity_content(ingress_input),
        labels=list(labels),
    )


def _bounded_ledger_body(content: str) -> str:
    if len(content) <= _LEDGER_BODY_MAX_CHARS:
        return content
    return content[:_LEDGER_BODY_MAX_CHARS] + "\n" + _LEDGER_TRUNCATION_MARKER


def _render_macro_title(value: AdmissionMacroInput) -> str:
    rendered_value = value.value_text if value.unit is None else f"{value.value_text} {value.unit}"
    return f"{value.release_key} {value.period_label}: {rendered_value}"


def _render_macro_content(
    value: AdmissionMacroInput,
) -> str:
    lines = [
        f"release_key: {value.release_key}",
        f"period_label: {value.period_label}",
        f"value_text: {value.value_text}",
    ]
    if value.unit is not None:
        lines.append(f"unit: {value.unit}")
    return "\n".join(lines)


__all__ = [
    "AdmissionError",
    "AdmissionIngressInput",
    "AdmissionMetadata",
    "AdmissionOutputs",
    "AdmissionRequest",
    "derive_admission_event_id",
    "derive_admission_metadata",
    "shape_admission_outputs",
    "validate_admission_request",
]
