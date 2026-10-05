"""Contracts for append-only decision episode memory."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal, cast

from event_trader.contracts._validators import (
    validate_event_id,
    validate_target_key,
    validate_timestamp,
)

type DecisionEpisodeRecordType = Literal[
    "attention",
    "analysis",
    "pm_decision",
    "validation_mark",
    "reflection",
    "reflection_learning",
    "repair",
]

type DecisionEpisodeStatus = Literal[
    "open_attention",
    "analysis_failed",
    "analysis_no_update",
    "analysis_memory_updated",
    "validated",
    "reflected",
]

_RECORD_TYPES = {
    "attention",
    "analysis",
    "pm_decision",
    "validation_mark",
    "reflection",
    "reflection_learning",
    "repair",
}


class DecisionMemoryContractError(ValueError):
    """Raised when decision episode contracts are malformed."""


@dataclass(frozen=True, slots=True)
class DecisionEpisodeRecord:
    """One append-only event in a decision episode chain."""

    episode_id: str
    target_key: str
    record_type: DecisionEpisodeRecordType
    business_at: datetime
    recorded_at: datetime
    event_ids: tuple[str, ...]
    source_record_id: str
    source_path: str | None
    source_line: int | None
    context_packet_id: str | None
    context_packet_hash: str | None
    status: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        episode_id = self.episode_id.strip() if isinstance(self.episode_id, str) else ""
        if not episode_id.startswith("decision-episode:"):
            raise DecisionMemoryContractError(
                "episode_id must start with 'decision-episode:'."
            )
        object.__setattr__(self, "episode_id", episode_id)
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=DecisionMemoryContractError,
            ),
        )
        if self.record_type not in _RECORD_TYPES:
            raise DecisionMemoryContractError("unsupported decision episode record_type.")
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=DecisionMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "recorded_at",
            validate_timestamp(
                self.recorded_at,
                field_name="recorded_at",
                error_type=DecisionMemoryContractError,
            ),
        )
        object.__setattr__(self, "event_ids", _normalize_event_ids(self.event_ids))
        source_record_id = (
            self.source_record_id.strip()
            if isinstance(self.source_record_id, str)
            else ""
        )
        if not source_record_id:
            raise DecisionMemoryContractError("source_record_id must be non-blank.")
        object.__setattr__(self, "source_record_id", source_record_id)
        object.__setattr__(
            self,
            "source_path",
            _optional_text(self.source_path, field_name="source_path"),
        )
        if self.source_line is not None and (
            not isinstance(self.source_line, int) or self.source_line < 1
        ):
            raise DecisionMemoryContractError("source_line must be a positive integer.")
        object.__setattr__(
            self,
            "context_packet_id",
            _optional_text(self.context_packet_id, field_name="context_packet_id"),
        )
        context_packet_hash = _optional_text(
            self.context_packet_hash,
            field_name="context_packet_hash",
        )
        if context_packet_hash is not None and len(context_packet_hash) != 64:
            raise DecisionMemoryContractError(
                "context_packet_hash must be a sha256 hex digest."
            )
        object.__setattr__(self, "context_packet_hash", context_packet_hash)
        status = self.status.strip() if isinstance(self.status, str) else ""
        if not status:
            raise DecisionMemoryContractError("status must be non-blank.")
        object.__setattr__(self, "status", status)
        if not isinstance(self.payload, Mapping):
            raise DecisionMemoryContractError("payload must be a mapping.")
        object.__setattr__(self, "payload", dict(self.payload))

    @property
    def natural_key(self) -> tuple[str, str, str]:
        """Return the idempotency key for one source-backed episode record."""
        return (self.episode_id, self.record_type, self.source_record_id)

    def to_json_payload(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "record_type": self.record_type,
            "business_at": self.business_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "event_ids": list(self.event_ids),
            "source_record_id": self.source_record_id,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "context_packet_id": self.context_packet_id,
            "context_packet_hash": self.context_packet_hash,
            "status": self.status,
            "payload": dict(self.payload),
        }


def derive_decision_episode_id(
    *,
    target_key: str,
    first_event_id: str,
    checker_business_at: datetime,
) -> str:
    """Derive the stable decision episode id for one checker escalation."""
    normalized_target = validate_target_key(
        target_key,
        error_type=DecisionMemoryContractError,
    )
    normalized_event_id = validate_event_id(
        first_event_id,
        error_type=DecisionMemoryContractError,
    )
    normalized_business_at = validate_timestamp(
        checker_business_at,
        field_name="checker_business_at",
        error_type=DecisionMemoryContractError,
    )
    digest = sha256(
        (
            normalized_target
            + normalized_event_id
            + normalized_business_at.isoformat()
        ).encode("utf-8")
    ).hexdigest()
    return f"decision-episode:{normalized_target}:{digest}"


def decision_episode_record_hash(record: DecisionEpisodeRecord) -> str:
    """Hash one record's canonical JSON payload."""
    encoded = json.dumps(
        record.to_json_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def decision_episode_record_matches_retry(
    left: DecisionEpisodeRecord,
    right: DecisionEpisodeRecord,
) -> bool:
    """Return whether two source-backed records are equivalent for idempotent retry."""
    left_payload = left.to_json_payload()
    right_payload = right.to_json_payload()
    for field_name in ("recorded_at", "source_path", "source_line", "context_packet_hash"):
        left_payload.pop(field_name, None)
        right_payload.pop(field_name, None)
    return left_payload == right_payload


def parse_decision_episode_record(
    payload: Mapping[str, object],
) -> DecisionEpisodeRecord:
    """Parse one persisted decision episode record."""
    return DecisionEpisodeRecord(
        episode_id=_require_text(payload, "episode_id"),
        target_key=_require_text(payload, "target_key"),
        record_type=cast(
            DecisionEpisodeRecordType,
            _require_text(payload, "record_type"),
        ),
        business_at=_parse_datetime(payload.get("business_at"), "business_at"),
        recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
        event_ids=tuple(str(item) for item in _require_list(payload, "event_ids")),
        source_record_id=_require_text(payload, "source_record_id"),
        source_path=_optional_text(payload.get("source_path"), field_name="source_path"),
        source_line=_optional_int(payload.get("source_line"), field_name="source_line"),
        context_packet_id=_optional_text(
            payload.get("context_packet_id"),
            field_name="context_packet_id",
        ),
        context_packet_hash=_optional_text(
            payload.get("context_packet_hash"),
            field_name="context_packet_hash",
        ),
        status=_require_text(payload, "status"),
        payload=_require_mapping(payload, "payload"),
    )


def _normalize_event_ids(event_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(event_ids, tuple):
        raise DecisionMemoryContractError("event_ids must be a tuple.")
    normalized: list[str] = []
    seen: set[str] = set()
    for event_id in event_ids:
        validated = validate_event_id(
            event_id,
            error_type=DecisionMemoryContractError,
        )
        if validated in seen:
            raise DecisionMemoryContractError("event_ids must not contain duplicates.")
        seen.add(validated)
        normalized.append(validated)
    if not normalized:
        raise DecisionMemoryContractError("event_ids must not be empty.")
    return tuple(normalized)


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise DecisionMemoryContractError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _optional_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DecisionMemoryContractError(f"{field_name} must be a string when set.")
    normalized = value.strip()
    return normalized or None


def _optional_int(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise DecisionMemoryContractError(f"{field_name} must be an integer when set.")
    return value


def _require_list(payload: Mapping[str, object], field_name: str) -> list[object]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise DecisionMemoryContractError(f"{field_name} must be a list.")
    return value


def _require_mapping(
    payload: Mapping[str, object],
    field_name: str,
) -> Mapping[str, object]:
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        raise DecisionMemoryContractError(f"{field_name} must be an object.")
    return value


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DecisionMemoryContractError(f"{field_name} must be an ISO datetime.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DecisionMemoryContractError(
            f"{field_name} must be an ISO datetime."
        ) from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=DecisionMemoryContractError,
    )


__all__ = [
    "DecisionEpisodeRecord",
    "DecisionEpisodeRecordType",
    "DecisionEpisodeStatus",
    "DecisionMemoryContractError",
    "decision_episode_record_hash",
    "decision_episode_record_matches_retry",
    "derive_decision_episode_id",
    "parse_decision_episode_record",
]
