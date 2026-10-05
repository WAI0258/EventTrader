"""Controlled promotion from canonical episode memory into learning cards."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, TypeAlias, cast

from event_trader.contracts._validators import validate_timestamp
from event_trader.episode_memory.contracts import (
    EpisodeMemoryDelta,
    EpisodeMemoryPromotionLearningConsumerRole,
    EpisodeMemoryPromotionValidationStatus,
    EpisodeMemoryPromotionWriteStatus,
    EpisodeMemoryPromotionValidationReceipt,
    EpisodeMemoryPromotionWriteReceipt,
    LearningCardPromotionIntent,
    RiskPolicyCandidatePromotionIntent,
)
from event_trader.episode_memory.store import (
    EpisodeMemoryStoreError,
    FileBackedEpisodeMemoryStore,
)
from event_trader.learning_cards.contracts import (
    LearningCard,
    LearningCardConsumerRole,
    LearningCardContractError,
)
from event_trader.learning_cards.store import (
    FileBackedLearningCardStore,
    LearningCardStoreError,
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
_ANALYSIS_LEAK_PATTERNS = (
    re.compile(r"(?i)\b(pnl|profit\s+and\s+loss)\b"),
    re.compile(r"(?i)\b(actual\s+)?exposure\b"),
    re.compile(r"(?i)\bposition\s+path\b"),
    re.compile(r"(?i)\bPMEpisodeMemoryView\b"),
    re.compile(r"(?i)\bopen[-\s]?position raw review\b"),
    re.compile(r"(?i)\braw review markdown\b"),
)


class PromotionDecisionError(ValueError):
    """Raised when a promotion intent cannot be validated or written."""


PositivePromotionIntent: TypeAlias = (
    LearningCardPromotionIntent | RiskPolicyCandidatePromotionIntent
)
PromotionDecisionStatus = Literal["accepted", "rejected", "written"]
PromotionDecisionReceipt = (
    EpisodeMemoryPromotionValidationReceipt | EpisodeMemoryPromotionWriteReceipt
)


@dataclass(frozen=True, slots=True)
class ValidatedPromotionDecision:
    """Validator-approved learning-card promotion that the writer may persist."""

    intent: LearningCardPromotionIntent
    card: LearningCard
    validation_receipt: EpisodeMemoryPromotionValidationReceipt
    source_deltas: tuple[EpisodeMemoryDelta, ...]


@dataclass(frozen=True, slots=True)
class PromotionValidationResult:
    """Result of deterministic learning-card promotion validation."""

    receipt: EpisodeMemoryPromotionValidationReceipt
    validated: ValidatedPromotionDecision | None


class PromotionDecisionValidator:
    """Validate canonical learning-card promotion intents against source deltas."""

    def __init__(
        self,
        episode_memory_store: FileBackedEpisodeMemoryStore,
        *,
        validator_name: str = "episode-memory-promotion-validator",
        append_receipts: bool = True,
    ) -> None:
        if not isinstance(episode_memory_store, FileBackedEpisodeMemoryStore):
            raise PromotionDecisionError(
                "episode_memory_store must be a FileBackedEpisodeMemoryStore."
            )
        self._episode_memory_store = episode_memory_store
        self._validator_name = validator_name
        self._append_receipts = append_receipts

    def validate_intent(
        self,
        intent: LearningCardPromotionIntent,
        *,
        recorded_at: datetime | None = None,
    ) -> PromotionValidationResult:
        recorded_at_utc = _normalize_recorded_at(recorded_at)
        try:
            if not isinstance(intent, LearningCardPromotionIntent):
                raise PromotionDecisionError(
                    "learning-card promotion requires LearningCardPromotionIntent."
                )
            source_deltas = validate_positive_promotion_source_deltas(
                intent,
                self._episode_memory_store,
            )
            _validate_learning_card_intent_content(intent)
            card = _build_learning_card(intent)
            receipt = _build_validation_receipt(
                intent=intent,
                status="accepted",
                validator_name=self._validator_name,
                recorded_at=recorded_at_utc,
                failure_reason=None,
            )
        except (EpisodeMemoryStoreError, LearningCardContractError, PromotionDecisionError) as exc:
            receipt = _build_validation_receipt(
                intent=intent,
                status="rejected",
                validator_name=self._validator_name,
                recorded_at=recorded_at_utc,
                failure_reason=str(exc),
            )
            _append_validation_receipt(
                self._episode_memory_store,
                receipt,
                append_receipts=self._append_receipts,
            )
            return PromotionValidationResult(receipt=receipt, validated=None)

        _append_validation_receipt(
            self._episode_memory_store,
            receipt,
            append_receipts=self._append_receipts,
        )
        return PromotionValidationResult(
            receipt=receipt,
            validated=ValidatedPromotionDecision(
                intent=intent,
                card=card,
                validation_receipt=receipt,
                source_deltas=source_deltas,
            ),
        )


class PromotionWriter:
    """Write only validator-approved learning-card promotions."""

    def __init__(
        self,
        store: FileBackedLearningCardStore,
        episode_memory_store: FileBackedEpisodeMemoryStore | None = None,
    ) -> None:
        if not isinstance(store, FileBackedLearningCardStore):
            raise PromotionDecisionError(
                "store must be a FileBackedLearningCardStore."
            )
        self._store = store
        self._episode_memory_store = episode_memory_store

    def write(
        self,
        validated: ValidatedPromotionDecision,
        *,
        recorded_at: datetime | None = None,
    ) -> EpisodeMemoryPromotionWriteReceipt:
        if not isinstance(validated, ValidatedPromotionDecision):
            raise PromotionDecisionError(
                "writer accepts only ValidatedPromotionDecision."
            )
        recorded_at_utc = _normalize_recorded_at(recorded_at)
        try:
            artifact_path = self._store.append_card(validated.card)
        except LearningCardStoreError as exc:
            receipt = _build_write_receipt(
                intent=validated.intent,
                validated_receipt_id=validated.validation_receipt.receipt_id,
                status="failed",
                artifact_refs=(),
                recorded_at=recorded_at_utc,
                failure_reason=str(exc),
            )
            _append_write_receipt(self._episode_memory_store, receipt)
            return receipt

        receipt = _build_write_receipt(
            intent=validated.intent,
            validated_receipt_id=validated.validation_receipt.receipt_id,
            status="written",
            artifact_refs=(
                f"learning-card://{validated.card.card_id}",
                artifact_path.as_posix(),
            ),
            recorded_at=recorded_at_utc,
            failure_reason=None,
        )
        _append_write_receipt(self._episode_memory_store, receipt)
        return receipt


def validate_positive_promotion_source_deltas(
    intent: PositivePromotionIntent,
    episode_memory_store: FileBackedEpisodeMemoryStore,
) -> tuple[EpisodeMemoryDelta, ...]:
    persisted = episode_memory_store.read_deltas(
        target_key=intent.target_key,
        episode_id=intent.episode_id,
    )
    deltas_by_id = {item.record.delta_id: item.record for item in persisted}
    source_deltas: list[EpisodeMemoryDelta] = []

    for delta_id, delta_hash in zip(intent.source_delta_ids, intent.source_delta_hashes):
        delta = deltas_by_id.get(delta_id)
        if delta is None:
            raise PromotionDecisionError(
                f"source delta {delta_id} was not found in the intent target_key/episode_id shard."
            )
        if delta.target_key != intent.target_key or delta.episode_id != intent.episode_id:
            raise PromotionDecisionError(
                f"source delta {delta_id} must match the intent target_key and episode_id."
            )
        if delta.content_hash != delta_hash:
            raise PromotionDecisionError(
                f"source delta {delta_id} content_hash does not match source_delta_hashes."
            )
        source_deltas.append(delta)

    if not source_deltas:
        raise PromotionDecisionError("positive promotion requires at least one source delta.")

    max_usable_from = max(delta.usable_from for delta in source_deltas)
    if intent.usable_from < max_usable_from:
        raise PromotionDecisionError(
            "intent.usable_from must be greater than or equal to max(source_delta.usable_from)."
        )
    max_source_visible_through = max(
        delta.source_visible_through for delta in source_deltas
    )
    if intent.source_visible_through < max_source_visible_through:
        raise PromotionDecisionError(
            "intent.source_visible_through must be greater than or equal to max(source_delta.source_visible_through)."
        )
    if not any(
        delta.memory_status == "finalized" and delta.validation_basis == "close_review"
        for delta in source_deltas
    ):
        raise PromotionDecisionError(
            "close/finalization promotion requires at least one finalized close_review source delta."
        )
    return tuple(source_deltas)


def _build_learning_card(intent: LearningCardPromotionIntent) -> LearningCard:
    target_key = None if intent.scope_key == "shared" else intent.target_key
    consumer_role = cast(LearningCardConsumerRole, intent.consumer_role)
    return LearningCard(
        card_id=_deterministic_card_id(intent),
        scope_key=intent.scope_key,
        consumer_role=consumer_role,
        title=intent.title,
        summary_md=intent.summary_md,
        body_md=intent.body_md,
        use_when=(intent.use_when,),
        avoid_when=(intent.avoid_when,),
        source_delta_ids=intent.source_delta_ids,
        source_delta_hashes=intent.source_delta_hashes,
        source_review_paths=(),
        source_episode_ids=(intent.episode_id,),
        target_key=target_key,
        confidence=intent.decision_confidence,
        usable_from=intent.usable_from,
        created_at=intent.recorded_at,
        retired_at=None,
        supersedes_card_ids=intent.supersedes_card_ids,
        tags=intent.tags,
        source_kind="episode_memory_promotion",
        promotion_decision_id=intent.decision_id,
    )


def _validate_learning_card_intent_content(intent: LearningCardPromotionIntent) -> None:
    if intent.consumer_role == "risk_policy_review":
        raise PromotionDecisionError(
            "learning-card promotion consumer_role must not be risk_policy_review."
        )
    for field_name, value in (
        ("title", intent.title),
        ("summary_md", intent.summary_md),
        ("body_md", intent.body_md),
        ("use_when", intent.use_when),
        ("avoid_when", intent.avoid_when),
    ):
        _reject_trade_action_phrasing(value, field_name=field_name)
    if intent.consumer_role == "analysis":
        combined = "\n".join(
            (intent.title, intent.summary_md, intent.body_md, intent.use_when, intent.avoid_when)
        )
        for pattern in _ANALYSIS_LEAK_PATTERNS:
            if pattern.search(combined):
                raise PromotionDecisionError(
                    "analysis-facing learning card must not leak PM exposure, PnL, position path, PMEpisodeMemoryView, or open-position raw review content."
                )


def _build_validation_receipt(
    *,
    intent: PositivePromotionIntent,
    status: EpisodeMemoryPromotionValidationStatus,
    validator_name: str,
    recorded_at: datetime,
    failure_reason: str | None,
) -> EpisodeMemoryPromotionValidationReceipt:
    return EpisodeMemoryPromotionValidationReceipt(
        receipt_id=_stable_prefixed_id(
            "promotion-validation-receipt",
            {
                "decision_id": intent.decision_id,
                "decision_content_hash": intent.content_hash,
                "status": status,
                "validator_name": validator_name,
            },
        ),
        decision_id=intent.decision_id,
        episode_id=intent.episode_id,
        target_key=intent.target_key,
        decision_content_hash=intent.content_hash,
        validation_status=status,
        validator_name=validator_name,
        source_delta_ids=intent.source_delta_ids,
        source_delta_hashes=intent.source_delta_hashes,
        recorded_at=recorded_at,
        failure_reason=failure_reason,
    )


def _build_write_receipt(
    *,
    intent: PositivePromotionIntent,
    validated_receipt_id: str,
    status: EpisodeMemoryPromotionWriteStatus,
    artifact_refs: tuple[str, ...],
    recorded_at: datetime,
    failure_reason: str | None,
) -> EpisodeMemoryPromotionWriteReceipt:
    return EpisodeMemoryPromotionWriteReceipt(
        receipt_id=_stable_prefixed_id(
            "promotion-write-receipt",
            {
                "decision_id": intent.decision_id,
                "decision_content_hash": intent.content_hash,
                "status": status,
                "validated_receipt_id": validated_receipt_id,
                "artifact_refs": list(artifact_refs),
            },
        ),
        decision_id=intent.decision_id,
        episode_id=intent.episode_id,
        target_key=intent.target_key,
        decision_content_hash=intent.content_hash,
        validated_receipt_id=validated_receipt_id,
        write_status=status,
        artifact_refs=artifact_refs,
        recorded_at=recorded_at,
        failure_reason=failure_reason,
    )


def _append_validation_receipt(
    episode_memory_store: FileBackedEpisodeMemoryStore,
    receipt: EpisodeMemoryPromotionValidationReceipt,
    *,
    append_receipts: bool,
) -> None:
    if append_receipts:
        episode_memory_store.append_promotion_validation_receipt(receipt)


def _append_write_receipt(
    episode_memory_store: FileBackedEpisodeMemoryStore | None,
    receipt: EpisodeMemoryPromotionWriteReceipt,
) -> None:
    if episode_memory_store is not None:
        episode_memory_store.append_promotion_write_receipt(receipt)


def _deterministic_card_id(intent: LearningCardPromotionIntent) -> str:
    return _stable_prefixed_id(
        "learning-card",
        {
            "decision_id": intent.decision_id,
            "scope_key": intent.scope_key,
            "consumer_role": intent.consumer_role,
            "source_delta_ids": list(intent.source_delta_ids),
            "source_delta_hashes": list(intent.source_delta_hashes),
        },
    )


def _stable_prefixed_id(prefix: str, payload: Mapping[str, object]) -> str:
    return f"{prefix}-{_stable_digest(payload)[:24]}"


def _stable_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_recorded_at(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    return validate_timestamp(
        timestamp,
        field_name="recorded_at",
        error_type=PromotionDecisionError,
    ).astimezone(UTC)


def _reject_trade_action_phrasing(text: str, *, field_name: str) -> None:
    for pattern in _COMMAND_PATTERNS:
        if pattern.search(text):
            raise PromotionDecisionError(
                f"{field_name} must not include trade action commands."
            )


__all__ = [
    "PromotionDecisionError",
    "PromotionDecisionReceipt",
    "PromotionDecisionStatus",
    "PromotionDecisionValidator",
    "PromotionValidationResult",
    "PromotionWriter",
    "ValidatedPromotionDecision",
    "validate_positive_promotion_source_deltas",
]
