"""Structured role-aware learning card contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, cast

from event_trader.contracts._validators import (
    normalize_content,
    validate_scope_key,
    validate_target_key,
    validate_timestamp,
)

LearningCardConsumerRole = Literal[
    "analysis",
    "pm_review",
    "reflection_learning",
    "risk_policy_review",
]

LEARNING_CARD_CONSUMER_ROLES: tuple[LearningCardConsumerRole, ...] = (
    "analysis",
    "pm_review",
    "reflection_learning",
    "risk_policy_review",
)

_COMMAND_PATTERNS = (
    re.compile(
        r"(?i)\b(action|please)\s*:?\s*(open|close|hold|reverse|resize|reduce|increase|decrease|buy|sell)\b"
    ),
    re.compile(
        r"(?i)\b(open|close|hold|reverse|resize|reduce|increase|decrease)\s+(a|an|the|this|that|new|existing|current|our|target)\s+([a-z_-]+\s+){0,3}(position|trade|allocation|orders?)\b"
    ),
    re.compile(
        r"(?i)\b(buy|sell)\s+((a|an|the|this|that|new|existing|current|our|target)\s+)?([a-z_-]+\s+){0,3}(position|trade|allocation|orders?)\b"
    ),
)


class LearningCardContractError(ValueError):
    """Raised when a learning-card contract is malformed."""


@dataclass(frozen=True, slots=True)
class LearningCard:
    """Canonical machine-readable learning card selected by scope and role."""

    card_id: str
    scope_key: str
    consumer_role: LearningCardConsumerRole
    title: str
    summary_md: str
    body_md: str
    use_when: tuple[str, ...]
    avoid_when: tuple[str, ...]
    source_delta_ids: tuple[str, ...]
    source_delta_hashes: tuple[str, ...]
    source_review_paths: tuple[str, ...]
    source_episode_ids: tuple[str, ...]
    target_key: str | None
    confidence: float
    usable_from: datetime
    created_at: datetime
    retired_at: datetime | None
    supersedes_card_ids: tuple[str, ...]
    tags: tuple[str, ...] = ()
    source_kind: str | None = None
    promotion_decision_id: str | None = None
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_id", _validate_non_blank_text(self.card_id, "card_id"))
        scope_key = validate_scope_key(
            self.scope_key,
            error_type=LearningCardContractError,
        )
        object.__setattr__(self, "scope_key", scope_key)
        object.__setattr__(
            self,
            "target_key",
            _normalize_target_for_scope(scope_key, self.target_key),
        )
        object.__setattr__(self, "consumer_role", _normalize_consumer_role(self.consumer_role))
        object.__setattr__(
            self,
            "title",
            normalize_content(
                _validate_non_blank_text(self.title, "title"),
                field_name="title",
                error_type=LearningCardContractError,
            ),
        )
        object.__setattr__(
            self,
            "summary_md",
            normalize_content(
                _validate_non_blank_text(self.summary_md, "summary_md"),
                field_name="summary_md",
                error_type=LearningCardContractError,
            ),
        )
        object.__setattr__(
            self,
            "body_md",
            normalize_content(
                _validate_non_blank_text(self.body_md, "body_md"),
                field_name="body_md",
                error_type=LearningCardContractError,
            ),
        )
        object.__setattr__(self, "use_when", _validate_text_tuple(self.use_when, "use_when"))
        object.__setattr__(
            self,
            "avoid_when",
            _validate_text_tuple(self.avoid_when, "avoid_when"),
        )
        object.__setattr__(
            self,
            "source_delta_ids",
            _validate_text_tuple(self.source_delta_ids, "source_delta_ids"),
        )
        object.__setattr__(
            self,
            "source_delta_hashes",
            _validate_hash_tuple(self.source_delta_hashes, "source_delta_hashes"),
        )
        if len(self.source_delta_ids) != len(self.source_delta_hashes):
            raise LearningCardContractError(
                "source_delta_ids and source_delta_hashes must have the same length."
            )
        object.__setattr__(
            self,
            "source_review_paths",
            _validate_text_tuple(self.source_review_paths, "source_review_paths"),
        )
        object.__setattr__(
            self,
            "source_episode_ids",
            _validate_text_tuple(self.source_episode_ids, "source_episode_ids"),
        )
        object.__setattr__(self, "confidence", _validate_confidence(self.confidence))
        object.__setattr__(
            self,
            "usable_from",
            validate_timestamp(
                self.usable_from,
                field_name="usable_from",
                error_type=LearningCardContractError,
            ).astimezone(UTC),
        )
        object.__setattr__(
            self,
            "created_at",
            validate_timestamp(
                self.created_at,
                field_name="created_at",
                error_type=LearningCardContractError,
            ).astimezone(UTC),
        )
        if self.retired_at is not None:
            retired_at = validate_timestamp(
                self.retired_at,
                field_name="retired_at",
                error_type=LearningCardContractError,
            ).astimezone(UTC)
            if retired_at <= self.usable_from:
                raise LearningCardContractError("retired_at must be later than usable_from.")
            object.__setattr__(self, "retired_at", retired_at)
        object.__setattr__(
            self,
            "supersedes_card_ids",
            _validate_text_tuple(self.supersedes_card_ids, "supersedes_card_ids"),
        )
        object.__setattr__(self, "tags", _validate_text_tuple(self.tags, "tags"))
        object.__setattr__(
            self,
            "source_kind",
            _optional_text(self.source_kind, "source_kind"),
        )
        object.__setattr__(
            self,
            "promotion_decision_id",
            _optional_text(self.promotion_decision_id, "promotion_decision_id"),
        )
        if not self.source_delta_ids and not self.source_review_paths:
            if self.source_kind != "operator_authored":
                raise LearningCardContractError(
                    "learning cards require at least one source delta or source review path."
                )
        if self.consumer_role in {"analysis", "pm_review"}:
            _reject_trade_action_phrasing(self.title, field_name="title")
            _reject_trade_action_phrasing(self.summary_md, field_name="summary_md")
            _reject_trade_action_phrasing(self.body_md, field_name="body_md")
        object.__setattr__(self, "content_hash", learning_card_record_hash(self._hash_payload()))

    def _hash_payload(self) -> dict[str, object]:
        payload = self.to_json_payload()
        payload.pop("content_hash", None)
        return payload

    def to_json_payload(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "scope_key": self.scope_key,
            "consumer_role": self.consumer_role,
            "title": self.title,
            "summary_md": self.summary_md,
            "body_md": self.body_md,
            "use_when": list(self.use_when),
            "avoid_when": list(self.avoid_when),
            "source_delta_ids": list(self.source_delta_ids),
            "source_delta_hashes": list(self.source_delta_hashes),
            "source_review_paths": list(self.source_review_paths),
            "source_episode_ids": list(self.source_episode_ids),
            "target_key": self.target_key,
            "confidence": self.confidence,
            "usable_from": self.usable_from.isoformat(),
            "created_at": self.created_at.isoformat(),
            "retired_at": self.retired_at.isoformat() if self.retired_at else None,
            "supersedes_card_ids": list(self.supersedes_card_ids),
            "tags": list(self.tags),
            "source_kind": self.source_kind,
            "promotion_decision_id": self.promotion_decision_id,
            "content_hash": self.content_hash,
        }


def parse_learning_card(payload: Mapping[str, object]) -> LearningCard:
    """Parse one JSON learning-card payload into a canonical contract."""

    return LearningCard(
        card_id=_require_text(payload, "card_id"),
        scope_key=_require_text(payload, "scope_key"),
        consumer_role=cast(LearningCardConsumerRole, _require_text(payload, "consumer_role")),
        title=_require_text(payload, "title"),
        summary_md=_require_text(payload, "summary_md"),
        body_md=_require_text(payload, "body_md"),
        use_when=tuple(_require_string_list(payload, "use_when")),
        avoid_when=tuple(_require_string_list(payload, "avoid_when")),
        source_delta_ids=tuple(_require_string_list(payload, "source_delta_ids")),
        source_delta_hashes=tuple(_require_string_list(payload, "source_delta_hashes")),
        source_review_paths=tuple(_require_string_list(payload, "source_review_paths")),
        source_episode_ids=tuple(_require_string_list(payload, "source_episode_ids")),
        target_key=_optional_text(payload.get("target_key"), "target_key"),
        confidence=_require_float(payload, "confidence"),
        usable_from=_parse_datetime(payload.get("usable_from"), "usable_from"),
        created_at=_parse_datetime(payload.get("created_at"), "created_at"),
        retired_at=(
            _parse_datetime(payload.get("retired_at"), "retired_at")
            if payload.get("retired_at") is not None
            else None
        ),
        supersedes_card_ids=tuple(_require_string_list(payload, "supersedes_card_ids")),
        tags=tuple(_optional_string_list(payload.get("tags"), "tags")),
        source_kind=_optional_text(payload.get("source_kind"), "source_kind"),
        promotion_decision_id=_optional_text(
            payload.get("promotion_decision_id"),
            "promotion_decision_id",
        ),
        content_hash=_optional_text(payload.get("content_hash"), "content_hash") or "",
    )


def learning_card_record_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_target_for_scope(scope_key: str, target_key: str | None) -> str | None:
    if scope_key == "shared":
        if target_key is not None:
            raise LearningCardContractError("shared learning cards require target_key=None.")
        return None
    expected = scope_key.partition(":")[2]
    normalized_target = _optional_text(target_key, "target_key")
    if normalized_target is None:
        raise LearningCardContractError("target-scoped learning cards require target_key.")
    normalized_target = validate_target_key(
        normalized_target,
        error_type=LearningCardContractError,
    )
    if normalized_target != expected:
        raise LearningCardContractError("scope_key target segment must match target_key.")
    return normalized_target


def _normalize_consumer_role(value: object) -> LearningCardConsumerRole:
    if value not in LEARNING_CARD_CONSUMER_ROLES:
        raise LearningCardContractError(
            "consumer_role must be analysis, pm_review, reflection_learning, or "
            "risk_policy_review."
        )
    return cast(LearningCardConsumerRole, value)


def _validate_text_tuple(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise LearningCardContractError(f"{field_name} must be a tuple.")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _validate_non_blank_text(value, field_name)
        if text in seen:
            raise LearningCardContractError(f"{field_name} must not contain duplicates.")
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


def _validate_hash_tuple(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    return tuple(_validate_sha256_hex(value, field_name) for value in values)


def _validate_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LearningCardContractError("confidence must be a number.")
    confidence = float(value)
    if confidence < 0 or confidence > 1:
        raise LearningCardContractError("confidence must be between 0 and 1.")
    return confidence


def _validate_non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise LearningCardContractError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise LearningCardContractError(f"{field_name} must be non-blank.")
    return normalized


def _validate_sha256_hex(value: object, field_name: str) -> str:
    text = _validate_non_blank_text(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise LearningCardContractError(
            f"{field_name} must be a lower-case sha256 hex digest."
        )
    return text


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LearningCardContractError(f"{field_name} must be a string when set.")
    normalized = value.strip()
    return normalized or None


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank_text(payload.get(field_name), field_name)


def _require_string_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise LearningCardContractError(f"{field_name} must be a list.")
    if not all(isinstance(item, str) for item in value):
        raise LearningCardContractError(f"{field_name} must be a list of strings.")
    return value


def _optional_string_list(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LearningCardContractError(f"{field_name} must be a list when set.")
    if not all(isinstance(item, str) for item in value):
        raise LearningCardContractError(f"{field_name} must be a list of strings.")
    return value


def _require_float(payload: Mapping[str, object], field_name: str) -> float:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LearningCardContractError(f"{field_name} must be a number.")
    return float(value)


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise LearningCardContractError(f"{field_name} must be an ISO datetime string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LearningCardContractError(
            f"{field_name} must be an ISO datetime string."
        ) from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=LearningCardContractError,
    ).astimezone(UTC)


def _reject_trade_action_phrasing(text: str, *, field_name: str) -> None:
    for pattern in _COMMAND_PATTERNS:
        if pattern.search(text):
            raise LearningCardContractError(
                f"{field_name} must not include trade action commands."
            )


__all__ = [
    "LEARNING_CARD_CONSUMER_ROLES",
    "LearningCard",
    "LearningCardConsumerRole",
    "LearningCardContractError",
    "learning_card_record_hash",
    "parse_learning_card",
]
