"""Deterministic reflection trigger policy and obligation contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, cast

from event_trader.contracts._validators import (
    normalize_content,
    validate_target_key,
    validate_timestamp,
)

ReflectionTriggerKind = Literal[
    "episode_finalization",
    "manual_operator",
    "pm_review_completion",
]
ReflectionObligationStatus = Literal["pending", "resolved", "cancelled"]
ReflectionObligationResolutionKind = Literal[
    "episode_memory_delta",
    "episode_memory_no_update",
    "cancelled",
]

REFLECTION_TRIGGER_KINDS: tuple[ReflectionTriggerKind, ...] = (
    "episode_finalization",
    "manual_operator",
    "pm_review_completion",
)
REFLECTION_OBLIGATION_STATUSES: tuple[ReflectionObligationStatus, ...] = (
    "pending",
    "resolved",
    "cancelled",
)
REFLECTION_OBLIGATION_RESOLUTION_KINDS: tuple[
    ReflectionObligationResolutionKind,
    ...,
] = (
    "episode_memory_delta",
    "episode_memory_no_update",
    "cancelled",
)


class ReflectionTriggerPolicyError(ValueError):
    """Raised when reflection trigger inputs or obligations are malformed."""


@dataclass(frozen=True, slots=True)
class ReflectionObligation:
    """Canonical auditable obligation to run sparse hindsight reflection."""

    obligation_id: str
    target_key: str
    episode_id: str | None
    trigger_kind: ReflectionTriggerKind
    trigger_source_ids: tuple[str, ...]
    source_observed_through: datetime
    due_at: datetime
    created_at: datetime
    status: ReflectionObligationStatus
    reason: str
    details: str
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "obligation_id", _validate_non_blank_text(self.obligation_id, "obligation_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ReflectionTriggerPolicyError),
        )
        object.__setattr__(
            self,
            "episode_id",
            _optional_non_blank_text(self.episode_id, "episode_id"),
        )
        object.__setattr__(self, "trigger_kind", _normalize_trigger_kind(self.trigger_kind))
        object.__setattr__(
            self,
            "trigger_source_ids",
            _validate_text_tuple(self.trigger_source_ids, "trigger_source_ids", allow_empty=False),
        )
        object.__setattr__(
            self,
            "source_observed_through",
            _normalize_timestamp(self.source_observed_through, "source_observed_through"),
        )
        object.__setattr__(self, "due_at", _normalize_timestamp(self.due_at, "due_at"))
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(self.created_at, "created_at"),
        )
        if self.due_at < self.source_observed_through:
            raise ReflectionTriggerPolicyError(
                "due_at must be at or after source_observed_through."
            )
        if self.created_at < self.source_observed_through:
            raise ReflectionTriggerPolicyError(
                "created_at must be at or after source_observed_through."
            )
        object.__setattr__(self, "status", _normalize_status(self.status))
        object.__setattr__(
            self,
            "reason",
            normalize_content(
                _validate_non_blank_text(self.reason, "reason"),
                field_name="reason",
                error_type=ReflectionTriggerPolicyError,
            ),
        )
        object.__setattr__(
            self,
            "details",
            normalize_content(
                _validate_non_blank_text(self.details, "details"),
                field_name="details",
                error_type=ReflectionTriggerPolicyError,
            ),
        )
        object.__setattr__(
            self,
            "content_hash",
            reflection_obligation_record_hash(self._hash_payload()),
        )

    def _hash_payload(self) -> dict[str, object]:
        payload = self.to_json_payload()
        payload.pop("content_hash", None)
        return payload

    def to_json_payload(self) -> dict[str, object]:
        return {
            "obligation_id": self.obligation_id,
            "target_key": self.target_key,
            "episode_id": self.episode_id,
            "trigger_kind": self.trigger_kind,
            "trigger_source_ids": list(self.trigger_source_ids),
            "source_observed_through": self.source_observed_through.isoformat(),
            "due_at": self.due_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "reason": self.reason,
            "details": self.details,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class ReflectionObligationResolution:
    """Append-only record resolving a reflection obligation."""

    resolution_id: str
    obligation_id: str
    target_key: str
    resolved_at: datetime
    resolution_kind: ReflectionObligationResolutionKind
    source_record_ids: tuple[str, ...]
    source_record_hashes: tuple[str, ...]
    reason: str
    content_hash: str = ""

    def __post_init__(self) -> None:
        for field_name in ("resolution_id", "obligation_id"):
            object.__setattr__(self, field_name, _validate_non_blank_text(getattr(self, field_name), field_name))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ReflectionTriggerPolicyError),
        )
        object.__setattr__(
            self,
            "resolved_at",
            _normalize_timestamp(self.resolved_at, "resolved_at"),
        )
        object.__setattr__(
            self,
            "resolution_kind",
            _normalize_resolution_kind(self.resolution_kind),
        )
        object.__setattr__(
            self,
            "source_record_ids",
            _validate_text_tuple(
                self.source_record_ids,
                "source_record_ids",
                allow_empty=self.resolution_kind == "cancelled",
            ),
        )
        object.__setattr__(
            self,
            "source_record_hashes",
            _validate_hash_tuple(
                self.source_record_hashes,
                "source_record_hashes",
                allow_empty=self.resolution_kind == "cancelled",
            ),
        )
        if len(self.source_record_ids) != len(self.source_record_hashes):
            raise ReflectionTriggerPolicyError(
                "source_record_ids and source_record_hashes must have the same length."
            )
        object.__setattr__(
            self,
            "reason",
            normalize_content(
                _validate_non_blank_text(self.reason, "reason"),
                field_name="reason",
                error_type=ReflectionTriggerPolicyError,
            ),
        )
        object.__setattr__(
            self,
            "content_hash",
            reflection_obligation_record_hash(self._hash_payload()),
        )

    def _hash_payload(self) -> dict[str, object]:
        payload = self.to_json_payload()
        payload.pop("content_hash", None)
        return payload

    def to_json_payload(self) -> dict[str, object]:
        return {
            "resolution_id": self.resolution_id,
            "obligation_id": self.obligation_id,
            "target_key": self.target_key,
            "resolved_at": self.resolved_at.isoformat(),
            "resolution_kind": self.resolution_kind,
            "source_record_ids": list(self.source_record_ids),
            "source_record_hashes": list(self.source_record_hashes),
            "reason": self.reason,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class EpisodeFinalizationObservation:
    """Deterministic close/finalization observation used as trigger input."""

    finalization_id: str
    target_key: str
    episode_id: str
    observed_at: datetime
    reason: str

    def __post_init__(self) -> None:
        for field_name in ("finalization_id", "episode_id"):
            object.__setattr__(self, field_name, _validate_non_blank_text(getattr(self, field_name), field_name))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ReflectionTriggerPolicyError),
        )
        object.__setattr__(
            self,
            "observed_at",
            _normalize_timestamp(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "reason",
            normalize_content(
                _validate_non_blank_text(self.reason, "reason"),
                field_name="reason",
                error_type=ReflectionTriggerPolicyError,
            ),
        )


@dataclass(frozen=True, slots=True)
class ManualReflectionTrigger:
    """Explicit operator request to create a reflection obligation."""

    trigger_id: str
    target_key: str
    episode_id: str | None
    requested_at: datetime
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger_id", _validate_non_blank_text(self.trigger_id, "trigger_id"))
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ReflectionTriggerPolicyError),
        )
        object.__setattr__(
            self,
            "episode_id",
            _optional_non_blank_text(self.episode_id, "episode_id"),
        )
        object.__setattr__(
            self,
            "requested_at",
            _normalize_timestamp(self.requested_at, "requested_at"),
        )
        object.__setattr__(
            self,
            "reason",
            normalize_content(
                _validate_non_blank_text(self.reason, "reason"),
                field_name="reason",
                error_type=ReflectionTriggerPolicyError,
            ),
        )


@dataclass(frozen=True, slots=True)
class PMReviewCompletionObservation:
    """Deterministic PMReview completion observation used as trigger input."""

    completion_id: str
    target_key: str
    episode_id: str
    observed_at: datetime
    pm_review_request_id: str
    pm_decision_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "completion_id",
            "episode_id",
            "pm_review_request_id",
            "pm_decision_ref",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_non_blank_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=ReflectionTriggerPolicyError),
        )
        object.__setattr__(
            self,
            "observed_at",
            _normalize_timestamp(self.observed_at, "observed_at"),
        )


class ReflectionTriggerPolicy:
    """Evaluate supported reflection triggers without LLM judgment."""

    def evaluate(
        self,
        *,
        target_key: str,
        checked_at: datetime,
        heartbeat_id: str | None = None,
        episode_finalizations: tuple[EpisodeFinalizationObservation, ...] = (),
        manual_triggers: tuple[ManualReflectionTrigger, ...] = (),
        pm_review_completions: tuple[PMReviewCompletionObservation, ...] = (),
    ) -> tuple[ReflectionObligation, ...]:
        normalized_target = validate_target_key(target_key, error_type=ReflectionTriggerPolicyError)
        _normalize_timestamp(checked_at, "checked_at")
        if heartbeat_id is not None:
            _validate_non_blank_text(heartbeat_id, "heartbeat_id")

        obligations: list[ReflectionObligation] = []
        for finalization in episode_finalizations:
            _require_target_match(finalization.target_key, normalized_target)
            obligations.append(
                _build_obligation(
                    target_key=normalized_target,
                    episode_id=finalization.episode_id,
                    trigger_kind="episode_finalization",
                    trigger_source_ids=(finalization.finalization_id,),
                    source_observed_through=finalization.observed_at,
                    created_at=finalization.observed_at,
                    reason="Episode finalized.",
                    details=finalization.reason,
                )
            )

        for trigger in manual_triggers:
            _require_target_match(trigger.target_key, normalized_target)
            obligations.append(
                _build_obligation(
                    target_key=normalized_target,
                    episode_id=trigger.episode_id,
                    trigger_kind="manual_operator",
                    trigger_source_ids=(trigger.trigger_id,),
                    source_observed_through=trigger.requested_at,
                    created_at=trigger.requested_at,
                    reason="Operator requested reflection.",
                    details=trigger.reason,
                )
            )

        for completion in pm_review_completions:
            _require_target_match(completion.target_key, normalized_target)
            obligations.append(
                _build_obligation(
                    target_key=normalized_target,
                    episode_id=completion.episode_id,
                    trigger_kind="pm_review_completion",
                    trigger_source_ids=(completion.completion_id,),
                    source_observed_through=completion.observed_at,
                    created_at=completion.observed_at,
                    reason="PMReview completed.",
                    details=(
                        f"pm_review_request_id={completion.pm_review_request_id} "
                        f"pm_decision_ref={completion.pm_decision_ref}"
                    ),
                )
            )

        return tuple(
            sorted(
                obligations,
                key=lambda obligation: (
                    obligation.due_at,
                    obligation.trigger_kind,
                    obligation.obligation_id,
                ),
            )
        )


def parse_reflection_obligation(payload: Mapping[str, object]) -> ReflectionObligation:
    """Parse one persisted reflection obligation payload."""

    return ReflectionObligation(
        obligation_id=_require_text(payload, "obligation_id"),
        target_key=_require_text(payload, "target_key"),
        episode_id=_optional_non_blank_text(payload.get("episode_id"), "episode_id"),
        trigger_kind=cast(ReflectionTriggerKind, _require_text(payload, "trigger_kind")),
        trigger_source_ids=tuple(_require_string_list(payload, "trigger_source_ids")),
        source_observed_through=_parse_datetime(
            payload.get("source_observed_through"),
            "source_observed_through",
        ),
        due_at=_parse_datetime(payload.get("due_at"), "due_at"),
        created_at=_parse_datetime(payload.get("created_at"), "created_at"),
        status=cast(ReflectionObligationStatus, _require_text(payload, "status")),
        reason=_require_text(payload, "reason"),
        details=_require_text(payload, "details"),
        content_hash=_optional_non_blank_text(payload.get("content_hash"), "content_hash") or "",
    )


def parse_reflection_obligation_resolution(
    payload: Mapping[str, object],
) -> ReflectionObligationResolution:
    """Parse one persisted reflection obligation resolution payload."""

    return ReflectionObligationResolution(
        resolution_id=_require_text(payload, "resolution_id"),
        obligation_id=_require_text(payload, "obligation_id"),
        target_key=_require_text(payload, "target_key"),
        resolved_at=_parse_datetime(payload.get("resolved_at"), "resolved_at"),
        resolution_kind=cast(
            ReflectionObligationResolutionKind,
            _require_text(payload, "resolution_kind"),
        ),
        source_record_ids=tuple(_require_string_list(payload, "source_record_ids")),
        source_record_hashes=tuple(_require_string_list(payload, "source_record_hashes")),
        reason=_require_text(payload, "reason"),
        content_hash=_optional_non_blank_text(payload.get("content_hash"), "content_hash") or "",
    )


def reflection_obligation_record_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _build_obligation(
    *,
    target_key: str,
    episode_id: str | None,
    trigger_kind: ReflectionTriggerKind,
    trigger_source_ids: tuple[str, ...],
    source_observed_through: datetime,
    created_at: datetime,
    reason: str,
    details: str,
) -> ReflectionObligation:
    obligation_id = _deterministic_obligation_id(
        target_key=target_key,
        episode_id=episode_id,
        trigger_kind=trigger_kind,
        trigger_source_ids=trigger_source_ids,
    )
    return ReflectionObligation(
        obligation_id=obligation_id,
        target_key=target_key,
        episode_id=episode_id,
        trigger_kind=trigger_kind,
        trigger_source_ids=trigger_source_ids,
        source_observed_through=source_observed_through,
        due_at=source_observed_through,
        created_at=created_at,
        status="pending",
        reason=reason,
        details=details,
    )


def _deterministic_obligation_id(
    *,
    target_key: str,
    episode_id: str | None,
    trigger_kind: ReflectionTriggerKind,
    trigger_source_ids: tuple[str, ...],
) -> str:
    identity_payload = {
        "target_key": target_key,
        "episode_id": episode_id,
        "trigger_kind": trigger_kind,
        "trigger_source_ids": list(trigger_source_ids),
    }
    digest = reflection_obligation_record_hash(identity_payload)[:32]
    return f"reflection_obligation:{digest}"


def _require_target_match(source_target_key: str, expected_target_key: str) -> None:
    if source_target_key != expected_target_key:
        raise ReflectionTriggerPolicyError(
            "trigger input target_key must match evaluated target_key."
        )


def _normalize_trigger_kind(value: object) -> ReflectionTriggerKind:
    if value not in REFLECTION_TRIGGER_KINDS:
        raise ReflectionTriggerPolicyError(
            "trigger_kind must be one of the supported reflection trigger kinds."
        )
    return cast(ReflectionTriggerKind, value)


def _normalize_status(value: object) -> ReflectionObligationStatus:
    if value not in REFLECTION_OBLIGATION_STATUSES:
        raise ReflectionTriggerPolicyError("status must be pending, resolved, or cancelled.")
    return cast(ReflectionObligationStatus, value)


def _normalize_resolution_kind(value: object) -> ReflectionObligationResolutionKind:
    if value not in REFLECTION_OBLIGATION_RESOLUTION_KINDS:
        raise ReflectionTriggerPolicyError(
            "resolution_kind must be episode_memory_delta, episode_memory_no_update, or cancelled."
        )
    return cast(ReflectionObligationResolutionKind, value)


def _normalize_timestamp(value: datetime, field_name: str) -> datetime:
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=ReflectionTriggerPolicyError,
    ).astimezone(UTC)


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ReflectionTriggerPolicyError(f"{field_name} must be an ISO datetime string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReflectionTriggerPolicyError(
            f"{field_name} must be an ISO datetime string."
        ) from exc
    return _normalize_timestamp(parsed, field_name)


def _validate_text_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ReflectionTriggerPolicyError(f"{field_name} must be a tuple.")
    if not allow_empty and not values:
        raise ReflectionTriggerPolicyError(f"{field_name} must not be empty.")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _validate_non_blank_text(value, field_name)
        if text in seen:
            raise ReflectionTriggerPolicyError(f"{field_name} must not contain duplicates.")
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


def _validate_hash_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ReflectionTriggerPolicyError(f"{field_name} must be a tuple.")
    if not allow_empty and not values:
        raise ReflectionTriggerPolicyError(f"{field_name} must not be empty.")
    return tuple(_validate_sha256_hex(value, field_name) for value in values)


def _validate_sha256_hex(value: object, field_name: str) -> str:
    text = _validate_non_blank_text(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ReflectionTriggerPolicyError(
            f"{field_name} must be a lower-case sha256 hex digest."
        )
    return text


def _require_string_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise ReflectionTriggerPolicyError(f"{field_name} must be a list.")
    if not all(isinstance(item, str) for item in value):
        raise ReflectionTriggerPolicyError(f"{field_name} must be a list of strings.")
    return value


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank_text(payload.get(field_name), field_name)


def _validate_non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ReflectionTriggerPolicyError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ReflectionTriggerPolicyError(f"{field_name} must be non-blank.")
    return normalized


def _optional_non_blank_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_non_blank_text(value, field_name)


__all__ = [
    "EpisodeFinalizationObservation",
    "ManualReflectionTrigger",
    "PMReviewCompletionObservation",
    "REFLECTION_OBLIGATION_STATUSES",
    "REFLECTION_OBLIGATION_RESOLUTION_KINDS",
    "REFLECTION_TRIGGER_KINDS",
    "ReflectionObligation",
    "ReflectionObligationResolution",
    "ReflectionObligationResolutionKind",
    "ReflectionObligationStatus",
    "ReflectionTriggerKind",
    "ReflectionTriggerPolicy",
    "ReflectionTriggerPolicyError",
    "parse_reflection_obligation",
    "parse_reflection_obligation_resolution",
    "reflection_obligation_record_hash",
]
