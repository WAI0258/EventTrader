"""Role-aware learning-card selection."""

from __future__ import annotations

from datetime import UTC, datetime

from event_trader.contracts._validators import validate_scope_key, validate_target_key
from event_trader.learning_cards.contracts import (
    LearningCard,
    LearningCardConsumerRole,
)
from event_trader.learning_cards.store import FileBackedLearningCardStore


class LearningCardSelectorError(ValueError):
    """Raised when learning-card selection inputs are malformed."""


def select_learning_cards(
    *,
    store: FileBackedLearningCardStore,
    consumer_role: LearningCardConsumerRole,
    target_key: str | None,
    business_at: datetime,
    scope_keys: tuple[str, ...] | None = None,
    budget: int | None = None,
) -> tuple[LearningCard, ...]:
    """Select visible cards by explicit consumer role, scope, and replay time."""

    if not isinstance(store, FileBackedLearningCardStore):
        raise LearningCardSelectorError("store must be a FileBackedLearningCardStore.")
    if consumer_role not in {
        "analysis",
        "pm_review",
        "reflection_learning",
        "risk_policy_review",
    }:
        raise LearningCardSelectorError("consumer_role is not supported.")
    normalized_target = (
        validate_target_key(target_key, error_type=LearningCardSelectorError)
        if target_key is not None
        else None
    )
    normalized_business_at = _normalize_datetime(business_at, "business_at")
    normalized_budget = _normalize_budget(budget)
    allowed_scopes = _allowed_scope_keys(
        target_key=normalized_target,
        scope_keys=scope_keys,
    )
    selected = [
        persisted.record
        for persisted in store.read_all_cards()
        if persisted.record.consumer_role == consumer_role
        and persisted.record.scope_key in allowed_scopes
        and persisted.record.usable_from <= normalized_business_at
        and (
            persisted.record.retired_at is None
            or persisted.record.retired_at > normalized_business_at
        )
    ]
    selected.sort(
        key=lambda card: (
            card.usable_from,
            -card.confidence,
            card.card_id,
        )
    )
    if normalized_budget is not None:
        selected = selected[:normalized_budget]
    return tuple(selected)


def _allowed_scope_keys(
    *,
    target_key: str | None,
    scope_keys: tuple[str, ...] | None,
) -> set[str]:
    if scope_keys is not None:
        if not isinstance(scope_keys, tuple):
            raise LearningCardSelectorError("scope_keys must be a tuple when provided.")
        return {
            validate_scope_key(scope_key, error_type=LearningCardSelectorError)
            for scope_key in scope_keys
        }
    if target_key is None:
        return {"shared"}
    return {"shared", f"target:{target_key}"}


def _normalize_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise LearningCardSelectorError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise LearningCardSelectorError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


def _normalize_budget(value: int | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LearningCardSelectorError("budget must be a non-negative integer.")
    return value


__all__ = [
    "LearningCardSelectorError",
    "select_learning_cards",
]
