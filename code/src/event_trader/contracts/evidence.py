"""Project-owned evidence identity and ledger-path contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from event_trader.source_policy import ensure_source_classification_labels
from event_trader.storage import WorkspaceLayout

from ._validators import (
    format_identity_timestamp,
    normalize_content,
    validate_event_id,
    validate_labels,
    validate_source_ref,
    validate_target_key,
    validate_timestamp,
)


class EvidenceContractError(ValueError):
    """Raised when an evidence contract input or path is invalid."""


@dataclass(frozen=True, slots=True)
class EvidenceEvent:
    """Lightweight runtime payload that points back to durable evidence truth."""

    event_id: str
    target_key: str
    source_ref: str
    ts_source: datetime
    ts_event: datetime
    ts_init: datetime

    def __post_init__(self) -> None:
        validate_event_id(self.event_id, error_type=EvidenceContractError)
        validate_target_key(self.target_key, error_type=EvidenceContractError)
        validate_source_ref(self.source_ref, error_type=EvidenceContractError)
        validate_timestamp(
            self.ts_source,
            field_name="ts_source",
            error_type=EvidenceContractError,
        )
        validate_timestamp(
            self.ts_event,
            field_name="ts_event",
            error_type=EvidenceContractError,
        )
        validate_timestamp(
            self.ts_init,
            field_name="ts_init",
            error_type=EvidenceContractError,
        )


@dataclass(frozen=True, slots=True)
class EvidenceLedgerRecord:
    """Durable append-only evidence record retained in the ledger."""

    event_id: str
    target_key: str
    source_ref: str
    title: str
    content: str
    labels: list[str]
    ts_source: datetime
    ts_event: datetime
    ts_init: datetime

    def __post_init__(self) -> None:
        validate_event_id(self.event_id, error_type=EvidenceContractError)
        validate_target_key(self.target_key, error_type=EvidenceContractError)
        validate_source_ref(self.source_ref, error_type=EvidenceContractError)
        if not isinstance(self.title, str) or not self.title.strip():
            raise EvidenceContractError("title must not be blank.")
        object.__setattr__(
            self,
            "content",
            normalize_content(
                self.content,
                field_name="content",
                error_type=EvidenceContractError,
            ),
        )
        object.__setattr__(
            self,
            "labels",
            ensure_source_classification_labels(
                source_ref=self.source_ref,
                title=self.title,
                content=self.content,
                labels=validate_labels(self.labels, error_type=EvidenceContractError),
            ),
        )
        validate_timestamp(
            self.ts_source,
            field_name="ts_source",
            error_type=EvidenceContractError,
        )
        validate_timestamp(
            self.ts_event,
            field_name="ts_event",
            error_type=EvidenceContractError,
        )
        validate_timestamp(
            self.ts_init,
            field_name="ts_init",
            error_type=EvidenceContractError,
        )



def derive_event_id(
    *,
    target_key: str,
    source_ref: str,
    ts_event: datetime,
    content: str,
) -> str:
    """Derive the stable evidence identity from admitted evidence inputs."""
    validated_target_key = validate_target_key(target_key, error_type=EvidenceContractError)
    validated_source_ref = validate_source_ref(source_ref, error_type=EvidenceContractError)
    validated_ts_event = validate_timestamp(
        ts_event,
        field_name="ts_event",
        error_type=EvidenceContractError,
    )
    normalized_content = normalize_content(
        content,
        field_name="content",
        error_type=EvidenceContractError,
    )

    content_hash = sha256(normalized_content.encode("utf-8")).hexdigest()
    identity_material = "\n".join(
        (
            validated_target_key,
            validated_source_ref,
            format_identity_timestamp(validated_ts_event),
            content_hash,
        )
    )
    return sha256(identity_material.encode("utf-8")).hexdigest()[:24]



def ledger_path_for_record(layout: WorkspaceLayout, record: EvidenceLedgerRecord) -> Path:
    """Resolve the canonical ledger path for a durable evidence record."""
    if not isinstance(layout, WorkspaceLayout):
        raise EvidenceContractError("layout must be a WorkspaceLayout instance.")

    event_day = validate_timestamp(
        record.ts_event,
        field_name="ts_event",
        error_type=EvidenceContractError,
    ).date()
    return (layout.ledger_root / record.target_key / f"{event_day.isoformat()}.jsonl").resolve(
        strict=False
    )


__all__ = [
    "EvidenceContractError",
    "EvidenceEvent",
    "EvidenceLedgerRecord",
    "derive_event_id",
    "ledger_path_for_record",
]
