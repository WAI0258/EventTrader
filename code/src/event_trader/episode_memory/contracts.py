"""Typed contracts for episode-memory canonical and candidate records.

Contracts in this module are conservative and deterministic.
Canonical records are replay-safe and append-only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal, cast

from event_trader.contracts._validators import (
    normalize_content,
    validate_event_id,
    validate_target_key,
    validate_timestamp,
)

EpisodeMemoryStatus = Literal[
    "provisional",
    "validated",
    "invalidated",
    "finalized",
]
EpisodeMemoryValidationBasis = Literal[
    "current_evidence",
    "later_evidence",
    "market_return",
    "execution_feedback",
    "portfolio_feedback",
    "close_review",
]
EpisodeMemoryValidationReceiptStatus = Literal["accepted", "rejected"]
EpisodeMemoryWriteStatus = Literal["accepted", "rejected", "stored"]
EpisodeMemoryPromotionIntentType = Literal[
    "no_promote",
    "propose_learning_card",
    "propose_risk_policy_candidate",
]
EpisodeMemoryPromotionValidationStatus = Literal["accepted", "rejected"]
EpisodeMemoryPromotionWriteStatus = Literal["written", "failed"]
EpisodeMemoryPromotionLearningConsumerRole = Literal[
    "analysis",
    "pm_review",
    "reflection_learning",
]
EpisodeMemoryPromotionRiskConsumerRole = Literal["risk_policy_review"]
EpisodeMemoryPromotionDecisionValidationViewStatus = Literal[
    "not_required",
    "pending",
    "accepted",
    "rejected",
]
EpisodeMemoryPromotionDecisionWriteViewStatus = Literal[
    "not_attempted",
    "pending",
    "written",
    "failed",
]

_VALID_STATUSES = frozenset(
    {
        "provisional",
        "validated",
        "invalidated",
        "finalized",
    }
)
_VALID_BASIS = frozenset(
    {
        "current_evidence",
        "later_evidence",
        "market_return",
        "execution_feedback",
        "portfolio_feedback",
        "close_review",
    }
)
_VALIDATION_STATUSES = frozenset({"accepted", "rejected"})
_WRITE_STATUSES = frozenset({"accepted", "rejected", "stored"})
_VALID_PROMOTION_INTENTS = frozenset(
    {
        "no_promote",
        "propose_learning_card",
        "propose_risk_policy_candidate",
    }
)
_VALID_PROMOTION_VALIDATION_STATUSES = frozenset({"accepted", "rejected"})
_VALID_PROMOTION_WRITE_STATUSES = frozenset({"written", "failed"})
EPISODE_MEMORY_LEARNING_CARD_CONSUMER_ROLES: tuple[
    EpisodeMemoryPromotionLearningConsumerRole, ...
] = ("analysis", "pm_review", "reflection_learning")
_VALID_LEARNING_CARD_CONSUMER_ROLES = frozenset(
    EPISODE_MEMORY_LEARNING_CARD_CONSUMER_ROLES
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


class EpisodeMemoryContractError(ValueError):
    """Raised when an episode-memory contract is malformed."""


@dataclass(frozen=True, slots=True)
class EpisodeMemoryDeltaSourceRefs:
    """Source references that support one episode-memory delta or no-update record."""

    evidence_event_ids: tuple[str, ...]
    market_bar_refs: tuple[str, ...]
    review_paths: tuple[str, ...]
    pm_review_request_ids: tuple[str, ...]
    pm_decision_ids: tuple[str, ...]
    execution_record_ids: tuple[str, ...]
    portfolio_record_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence_event_ids",
            _validate_event_ids(self.evidence_event_ids, "evidence_event_ids"),
        )
        object.__setattr__(
            self,
            "market_bar_refs",
            _validate_id_tuple(
                self.market_bar_refs,
                "market_bar_refs",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "review_paths",
            _validate_id_tuple(
                self.review_paths,
                "review_paths",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "pm_review_request_ids",
            _validate_id_tuple(
                self.pm_review_request_ids,
                "pm_review_request_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "pm_decision_ids",
            _validate_id_tuple(
                self.pm_decision_ids,
                "pm_decision_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "execution_record_ids",
            _validate_id_tuple(
                self.execution_record_ids,
                "execution_record_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "portfolio_record_ids",
            _validate_id_tuple(
                self.portfolio_record_ids,
                "portfolio_record_ids",
                allow_empty=True,
            ),
        )

    @property
    def has_any_reference(self) -> bool:
        return any(
            (
                self.evidence_event_ids,
                self.market_bar_refs,
                self.review_paths,
                self.pm_review_request_ids,
                self.pm_decision_ids,
                self.execution_record_ids,
                self.portfolio_record_ids,
            )
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "evidence_event_ids": list(self.evidence_event_ids),
            "market_bar_refs": list(self.market_bar_refs),
            "review_paths": list(self.review_paths),
            "pm_review_request_ids": list(self.pm_review_request_ids),
            "pm_decision_ids": list(self.pm_decision_ids),
            "execution_record_ids": list(self.execution_record_ids),
            "portfolio_record_ids": list(self.portfolio_record_ids),
        }


@dataclass(frozen=True, slots=True)
class EpisodeMemoryDeltaCandidate:
    """Candidate delta emitted by reflection before canonical commit."""

    episode_id: str
    target_key: str
    delta_kind: str
    memory_status: EpisodeMemoryStatus
    validation_basis: EpisodeMemoryValidationBasis
    source_visible_through: datetime
    usable_from: datetime
    source_refs: EpisodeMemoryDeltaSourceRefs
    summary_md: str
    thesis_delta: str
    pm_management_delta: str
    risk_delta: str
    invalidation_delta: str
    error_attributions: tuple[str, ...]
    confidence: float
    supersedes_delta_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "episode_id",
            _validate_non_blank_text(self.episode_id, "episode_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=EpisodeMemoryContractError),
        )
        object.__setattr__(
            self,
            "delta_kind",
            _validate_non_blank_text(self.delta_kind, "delta_kind"),
        )
        object.__setattr__(
            self,
            "memory_status",
            _validate_status(self.memory_status, "memory_status"),
        )
        object.__setattr__(
            self,
            "validation_basis",
            _validate_validation_basis(self.validation_basis, "validation_basis"),
        )
        object.__setattr__(
            self,
            "source_visible_through",
            validate_timestamp(
                self.source_visible_through,
                field_name="source_visible_through",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "usable_from",
            validate_timestamp(
                self.usable_from,
                field_name="usable_from",
                error_type=EpisodeMemoryContractError,
            ),
        )
        if self.usable_from < self.source_visible_through:
            raise EpisodeMemoryContractError(
                "usable_from must be greater than or equal to source_visible_through."
            )
        if not isinstance(self.source_refs, EpisodeMemoryDeltaSourceRefs):
            raise EpisodeMemoryContractError(
                "source_refs must be EpisodeMemoryDeltaSourceRefs."
            )
        object.__setattr__(
            self,
            "summary_md",
            normalize_content(
                self.summary_md,
                field_name="summary_md",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "thesis_delta",
            normalize_content(
                self.thesis_delta,
                field_name="thesis_delta",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "pm_management_delta",
            normalize_content(
                self.pm_management_delta,
                field_name="pm_management_delta",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "risk_delta",
            normalize_content(
                self.risk_delta,
                field_name="risk_delta",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "invalidation_delta",
            normalize_content(
                self.invalidation_delta,
                field_name="invalidation_delta",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "error_attributions",
            _validate_text_tuple(self.error_attributions, "error_attributions"),
        )
        object.__setattr__(
            self,
            "supersedes_delta_ids",
            _validate_id_tuple(
                self.supersedes_delta_ids,
                "supersedes_delta_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(self, "confidence", _validate_confidence(self.confidence))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "delta_kind": self.delta_kind,
            "memory_status": self.memory_status,
            "validation_basis": self.validation_basis,
            "source_visible_through": self.source_visible_through.isoformat(),
            "usable_from": self.usable_from.isoformat(),
            "source_refs": self.source_refs.to_json_payload(),
            "summary_md": self.summary_md,
            "thesis_delta": self.thesis_delta,
            "pm_management_delta": self.pm_management_delta,
            "risk_delta": self.risk_delta,
            "invalidation_delta": self.invalidation_delta,
            "error_attributions": list(self.error_attributions),
            "confidence": self.confidence,
            "supersedes_delta_ids": list(self.supersedes_delta_ids),
        }


@dataclass(frozen=True, slots=True)
class EpisodeMemoryDelta(EpisodeMemoryDeltaCandidate):
    """Committed delta record persisted under research_memory."""

    delta_id: str
    recorded_at: datetime
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.__class__ is EpisodeMemoryDelta:
            object.__setattr__(
                self,
                "delta_id",
                _validate_non_blank_text(self.delta_id, "delta_id"),
            )
            object.__setattr__(
                self,
                "recorded_at",
                validate_timestamp(
                    self.recorded_at,
                    field_name="recorded_at",
                    error_type=EpisodeMemoryContractError,
                ),
            )
            EpisodeMemoryDeltaCandidate.__post_init__(self)
            if not self.source_refs.has_any_reference:
                raise EpisodeMemoryContractError(
                    "canonical EpisodeMemoryDelta requires source_refs with at least one source reference."
                )
            object.__setattr__(
                self,
                "content_hash",
                _canonical_record_hash(self._hash_payload()),
            )
            self._validate_no_trade_action_phrasing()

    def _validate_no_trade_action_phrasing(self) -> None:
        _reject_trade_action_phrasing(self.summary_md, field_name="summary_md")
        _reject_trade_action_phrasing(
            self.thesis_delta,
            field_name="thesis_delta",
        )
        _reject_trade_action_phrasing(
            self.pm_management_delta,
            field_name="pm_management_delta",
        )
        _reject_trade_action_phrasing(self.risk_delta, field_name="risk_delta")
        _reject_trade_action_phrasing(
            self.invalidation_delta,
            field_name="invalidation_delta",
        )
        for idx, attribution in enumerate(self.error_attributions):
            _reject_trade_action_phrasing(
                attribution,
                field_name=f"error_attributions[{idx}]",
            )

    def _hash_payload(self) -> dict[str, object]:
        payload = self.to_json_payload()
        payload.pop("content_hash", None)
        return payload

    def to_json_payload(self) -> dict[str, object]:
        payload = EpisodeMemoryDeltaCandidate.to_json_payload(self)
        payload.update(
            {
                "delta_id": self.delta_id,
                "recorded_at": self.recorded_at.isoformat(),
                "content_hash": self.content_hash,
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class EpisodeMemoryNoUpdateCandidate:
    """Candidate no-update receipt emitted by reflection before canonical commit."""

    candidate_id: str
    episode_id: str
    target_key: str
    review_path: str
    business_at: datetime
    source_visible_through: datetime
    usable_from: datetime
    reason: str
    reason_code: str
    source_refs: EpisodeMemoryDeltaSourceRefs
    reviewed_delta_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_id",
            _validate_non_blank_text(self.candidate_id, "candidate_id"),
        )
        object.__setattr__(
            self,
            "episode_id",
            _validate_non_blank_text(self.episode_id, "episode_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=EpisodeMemoryContractError),
        )
        object.__setattr__(
            self,
            "review_path",
            _validate_non_blank_text(self.review_path, "review_path"),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "source_visible_through",
            validate_timestamp(
                self.source_visible_through,
                field_name="source_visible_through",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "usable_from",
            validate_timestamp(
                self.usable_from,
                field_name="usable_from",
                error_type=EpisodeMemoryContractError,
            ),
        )
        if self.usable_from < self.source_visible_through:
            raise EpisodeMemoryContractError(
                "usable_from must be greater than or equal to source_visible_through."
            )
        if not isinstance(self.source_refs, EpisodeMemoryDeltaSourceRefs):
            raise EpisodeMemoryContractError(
                "source_refs must be EpisodeMemoryDeltaSourceRefs."
            )
        object.__setattr__(
            self,
            "reason",
            normalize_content(self.reason, field_name="reason", error_type=EpisodeMemoryContractError),
        )
        object.__setattr__(
            self,
            "reason_code",
            _validate_non_blank_text(self.reason_code, "reason_code"),
        )
        object.__setattr__(
            self,
            "reviewed_delta_ids",
            _validate_id_tuple(self.reviewed_delta_ids, "reviewed_delta_ids", allow_empty=True),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "review_path": self.review_path,
            "business_at": self.business_at.isoformat(),
            "source_visible_through": self.source_visible_through.isoformat(),
            "usable_from": self.usable_from.isoformat(),
            "reason": self.reason,
            "reason_code": self.reason_code,
            "source_refs": self.source_refs.to_json_payload(),
            "reviewed_delta_ids": list(self.reviewed_delta_ids),
        }


@dataclass(frozen=True, slots=True)
class EpisodeMemoryNoUpdate:
    """Committed no-update receipt in research_memory."""

    receipt_id: str
    episode_id: str
    target_key: str
    review_path: str
    business_at: datetime
    recorded_at: datetime
    source_visible_through: datetime
    usable_from: datetime
    reason: str
    reason_code: str
    source_refs: EpisodeMemoryDeltaSourceRefs
    reviewed_delta_ids: tuple[str, ...]
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "receipt_id",
            _validate_non_blank_text(self.receipt_id, "receipt_id"),
        )
        object.__setattr__(
            self,
            "episode_id",
            _validate_non_blank_text(self.episode_id, "episode_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=EpisodeMemoryContractError),
        )
        object.__setattr__(
            self,
            "review_path",
            _validate_non_blank_text(self.review_path, "review_path"),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "recorded_at",
            validate_timestamp(
                self.recorded_at,
                field_name="recorded_at",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "source_visible_through",
            validate_timestamp(
                self.source_visible_through,
                field_name="source_visible_through",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "usable_from",
            validate_timestamp(
                self.usable_from,
                field_name="usable_from",
                error_type=EpisodeMemoryContractError,
            ),
        )
        if self.usable_from < self.source_visible_through:
            raise EpisodeMemoryContractError(
                "usable_from must be greater than or equal to source_visible_through."
            )
        object.__setattr__(
            self,
            "reason",
            normalize_content(
                self.reason,
                field_name="reason",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "reason_code",
            _validate_non_blank_text(self.reason_code, "reason_code"),
        )
        if not isinstance(self.source_refs, EpisodeMemoryDeltaSourceRefs):
            raise EpisodeMemoryContractError(
                "source_refs must be EpisodeMemoryDeltaSourceRefs."
            )
        if not self.source_refs.has_any_reference:
            raise EpisodeMemoryContractError(
                "no-update receipt must include at least one source ref."
            )
        object.__setattr__(
            self,
            "reviewed_delta_ids",
            _validate_id_tuple(
                self.reviewed_delta_ids,
                "reviewed_delta_ids",
                allow_empty=True,
            ),
        )
        object.__setattr__(self, "content_hash", _canonical_record_hash(self._hash_payload()))

    def _hash_payload(self) -> dict[str, object]:
        payload = self.to_json_payload()
        payload.pop("content_hash", None)
        return payload

    def to_json_payload(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "review_path": self.review_path,
            "business_at": self.business_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "source_visible_through": self.source_visible_through.isoformat(),
            "usable_from": self.usable_from.isoformat(),
            "content_hash": self.content_hash,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "source_refs": self.source_refs.to_json_payload(),
            "reviewed_delta_ids": list(self.reviewed_delta_ids),
        }


@dataclass(frozen=True, slots=True)
class EpisodeMemoryValidationReceipt:
    """Validation outcome for one candidate delta."""

    receipt_id: str
    delta_id: str
    episode_id: str
    target_key: str
    status: EpisodeMemoryValidationReceiptStatus
    recorded_at: datetime
    source_visible_through: datetime
    usable_from: datetime
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "receipt_id",
            _validate_non_blank_text(self.receipt_id, "receipt_id"),
        )
        object.__setattr__(
            self, "delta_id", _validate_non_blank_text(self.delta_id, "delta_id")
        )
        object.__setattr__(
            self,
            "episode_id",
            _validate_non_blank_text(self.episode_id, "episode_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=EpisodeMemoryContractError),
        )
        object.__setattr__(
            self,
            "status",
            _validate_validation_receipt_status(self.status, "status"),
        )
        object.__setattr__(
            self,
            "recorded_at",
            validate_timestamp(
                self.recorded_at,
                field_name="recorded_at",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "source_visible_through",
            validate_timestamp(
                self.source_visible_through,
                field_name="source_visible_through",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "usable_from",
            validate_timestamp(
                self.usable_from,
                field_name="usable_from",
                error_type=EpisodeMemoryContractError,
            ),
        )
        if self.usable_from < self.source_visible_through:
            raise EpisodeMemoryContractError(
                "usable_from must be greater than or equal to source_visible_through."
            )
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                _optional_text(self.failure_reason, "failure_reason"),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "delta_id": self.delta_id,
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "status": self.status,
            "recorded_at": self.recorded_at.isoformat(),
            "source_visible_through": self.source_visible_through.isoformat(),
            "usable_from": self.usable_from.isoformat(),
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class EpisodeMemoryWriteReceipt:
    """Write receipt for one delta append attempt."""

    receipt_id: str
    delta_id: str
    episode_id: str
    target_key: str
    artifact_path: str
    content_hash: str
    source_visible_through: datetime
    usable_from: datetime
    validator_receipt_id: str
    status: EpisodeMemoryWriteStatus
    recorded_at: datetime
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "receipt_id",
            _validate_non_blank_text(self.receipt_id, "receipt_id"),
        )
        object.__setattr__(
            self, "delta_id", _validate_non_blank_text(self.delta_id, "delta_id")
        )
        object.__setattr__(
            self,
            "episode_id",
            _validate_non_blank_text(self.episode_id, "episode_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=EpisodeMemoryContractError),
        )
        object.__setattr__(
            self,
            "artifact_path",
            _validate_non_blank_text(self.artifact_path, "artifact_path"),
        )
        object.__setattr__(
            self,
            "content_hash",
            _validate_sha256_hex(self.content_hash, "content_hash"),
        )
        object.__setattr__(
            self,
            "source_visible_through",
            validate_timestamp(
                self.source_visible_through,
                field_name="source_visible_through",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "usable_from",
            validate_timestamp(
                self.usable_from,
                field_name="usable_from",
                error_type=EpisodeMemoryContractError,
            ),
        )
        if self.usable_from < self.source_visible_through:
            raise EpisodeMemoryContractError(
                "usable_from must be greater than or equal to source_visible_through."
            )
        object.__setattr__(
            self,
            "validator_receipt_id",
            _validate_non_blank_text(self.validator_receipt_id, "validator_receipt_id"),
        )
        object.__setattr__(
            self,
            "status",
            _validate_write_status(self.status, "status"),
        )
        object.__setattr__(
            self,
            "recorded_at",
            validate_timestamp(
                self.recorded_at,
                field_name="recorded_at",
                error_type=EpisodeMemoryContractError,
            ),
        )
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                _optional_text(self.failure_reason, "failure_reason"),
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "delta_id": self.delta_id,
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "artifact_path": self.artifact_path,
            "content_hash": self.content_hash,
            "source_visible_through": self.source_visible_through.isoformat(),
            "usable_from": self.usable_from.isoformat(),
            "validator_receipt_id": self.validator_receipt_id,
            "status": self.status,
            "recorded_at": self.recorded_at.isoformat(),
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class _EpisodeMemoryPromotionIntentBase:
    decision_id: str
    promotion_batch_id: str
    episode_id: str
    target_key: str
    business_at: datetime
    recorded_at: datetime
    source_visible_through: datetime
    usable_from: datetime
    source_delta_ids: tuple[str, ...]
    source_delta_hashes: tuple[str, ...]
    promotion_intent: EpisodeMemoryPromotionIntentType
    decision_confidence: float
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_id",
            _validate_non_blank_text(self.decision_id, "decision_id"),
        )
        object.__setattr__(
            self,
            "promotion_batch_id",
            _validate_non_blank_text(self.promotion_batch_id, "promotion_batch_id"),
        )
        object.__setattr__(
            self,
            "episode_id",
            _validate_non_blank_text(self.episode_id, "episode_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=EpisodeMemoryContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "recorded_at",
            validate_timestamp(
                self.recorded_at,
                field_name="recorded_at",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "source_visible_through",
            validate_timestamp(
                self.source_visible_through,
                field_name="source_visible_through",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "usable_from",
            validate_timestamp(
                self.usable_from,
                field_name="usable_from",
                error_type=EpisodeMemoryContractError,
            ),
        )
        if self.usable_from < self.source_visible_through:
            raise EpisodeMemoryContractError(
                "usable_from must be greater than or equal to source_visible_through."
            )
        object.__setattr__(
            self,
            "source_delta_ids",
            _validate_id_tuple(self.source_delta_ids, "source_delta_ids"),
        )
        object.__setattr__(
            self,
            "source_delta_hashes",
            _validate_sha256_hash_tuple(
                self.source_delta_hashes,
                "source_delta_hashes",
                allow_empty=False,
            ),
        )
        if len(self.source_delta_ids) != len(self.source_delta_hashes):
            raise EpisodeMemoryContractError(
                "len(source_delta_ids) must equal len(source_delta_hashes)."
            )
        object.__setattr__(
            self,
            "promotion_intent",
            _validate_promotion_intent_type(self.promotion_intent, "promotion_intent"),
        )
        object.__setattr__(
            self,
            "decision_confidence",
            _validate_confidence(self.decision_confidence),
        )

    def _hash_payload(self) -> dict[str, object]:
        payload = self.to_json_payload()
        payload.pop("content_hash", None)
        return payload

    def _set_content_hash(self) -> None:
        object.__setattr__(
            self,
            "content_hash",
            _canonical_record_hash(self._hash_payload()),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "promotion_batch_id": self.promotion_batch_id,
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "source_visible_through": self.source_visible_through.isoformat(),
            "usable_from": self.usable_from.isoformat(),
            "source_delta_ids": list(self.source_delta_ids),
            "source_delta_hashes": list(self.source_delta_hashes),
            "promotion_intent": self.promotion_intent,
            "decision_confidence": self.decision_confidence,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class NoPromoteIntent(_EpisodeMemoryPromotionIntentBase):
    no_promote_reason: str = ""
    no_promote_reason_code: str = ""

    def __post_init__(self) -> None:
        _EpisodeMemoryPromotionIntentBase.__post_init__(self)
        if self.promotion_intent != "no_promote":
            raise EpisodeMemoryContractError(
                "NoPromoteIntent promotion_intent must be 'no_promote'."
            )
        object.__setattr__(
            self,
            "no_promote_reason",
            normalize_content(
                self.no_promote_reason,
                field_name="no_promote_reason",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "no_promote_reason_code",
            _validate_non_blank_text(
                self.no_promote_reason_code,
                "no_promote_reason_code",
            ),
        )
        self._set_content_hash()

    def to_json_payload(self) -> dict[str, object]:
        payload = _EpisodeMemoryPromotionIntentBase.to_json_payload(self)
        payload.update(
            {
                "no_promote_reason": self.no_promote_reason,
                "no_promote_reason_code": self.no_promote_reason_code,
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class LearningCardPromotionIntent(_EpisodeMemoryPromotionIntentBase):
    scope_key: str = ""
    consumer_role: EpisodeMemoryPromotionLearningConsumerRole | str = ""
    title: str = ""
    summary_md: str = ""
    body_md: str = ""
    use_when: str = ""
    avoid_when: str = ""
    tags: tuple[str, ...] = ()
    supersedes_card_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _EpisodeMemoryPromotionIntentBase.__post_init__(self)
        if self.promotion_intent != "propose_learning_card":
            raise EpisodeMemoryContractError(
                "LearningCardPromotionIntent promotion_intent must be 'propose_learning_card'."
            )
        object.__setattr__(
            self,
            "scope_key",
            _validate_promotion_scope_key(
                self.scope_key,
                target_key=self.target_key,
            ),
        )
        object.__setattr__(
            self,
            "consumer_role",
            validate_learning_card_consumer_role(
                self.consumer_role,
                "consumer_role",
            ),
        )
        object.__setattr__(
            self,
            "title",
            _validate_non_blank_text(self.title, "title"),
        )
        object.__setattr__(
            self,
            "summary_md",
            normalize_content(
                self.summary_md,
                field_name="summary_md",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "body_md",
            normalize_content(
                self.body_md,
                field_name="body_md",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "use_when",
            normalize_content(
                self.use_when,
                field_name="use_when",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "avoid_when",
            normalize_content(
                self.avoid_when,
                field_name="avoid_when",
                error_type=EpisodeMemoryContractError,
            ),
        )
        object.__setattr__(
            self,
            "tags",
            _validate_text_tuple(self.tags, "tags", allow_empty=True),
        )
        object.__setattr__(
            self,
            "supersedes_card_ids",
            _validate_id_tuple(
                self.supersedes_card_ids,
                "supersedes_card_ids",
                allow_empty=True,
            ),
        )
        self._set_content_hash()

    def to_json_payload(self) -> dict[str, object]:
        payload = _EpisodeMemoryPromotionIntentBase.to_json_payload(self)
        payload.update(
            {
                "scope_key": self.scope_key,
                "consumer_role": self.consumer_role,
                "title": self.title,
                "summary_md": self.summary_md,
                "body_md": self.body_md,
                "use_when": self.use_when,
                "avoid_when": self.avoid_when,
                "tags": list(self.tags),
                "supersedes_card_ids": list(self.supersedes_card_ids),
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class RiskPolicyCandidatePromotionIntent(_EpisodeMemoryPromotionIntentBase):
    scope_key: str = ""
    consumer_role: EpisodeMemoryPromotionRiskConsumerRole | str = ""
    proposed_patch: Mapping[str, object] | None = None
    rationale_md: str = ""

    def __post_init__(self) -> None:
        _EpisodeMemoryPromotionIntentBase.__post_init__(self)
        if self.promotion_intent != "propose_risk_policy_candidate":
            raise EpisodeMemoryContractError(
                "RiskPolicyCandidatePromotionIntent promotion_intent must be 'propose_risk_policy_candidate'."
            )
        object.__setattr__(
            self,
            "scope_key",
            _validate_promotion_scope_key(
                self.scope_key,
                target_key=self.target_key,
            ),
        )
        object.__setattr__(
            self,
            "consumer_role",
            _validate_risk_policy_consumer_role(
                self.consumer_role,
                "consumer_role",
            ),
        )
        object.__setattr__(
            self,
            "proposed_patch",
            _validate_patch_mapping(self.proposed_patch, "proposed_patch"),
        )
        object.__setattr__(
            self,
            "rationale_md",
            normalize_content(
                self.rationale_md,
                field_name="rationale_md",
                error_type=EpisodeMemoryContractError,
            ),
        )
        self._set_content_hash()

    def to_json_payload(self) -> dict[str, object]:
        payload = _EpisodeMemoryPromotionIntentBase.to_json_payload(self)
        payload.update(
            {
                "scope_key": self.scope_key,
                "consumer_role": self.consumer_role,
                "proposed_patch": dict(self.proposed_patch or {}),
                "rationale_md": self.rationale_md,
            }
        )
        return payload


EpisodeMemoryPromotionDecisionIntent = (
    NoPromoteIntent
    | LearningCardPromotionIntent
    | RiskPolicyCandidatePromotionIntent
)


@dataclass(frozen=True, slots=True)
class EpisodeMemoryPromotionValidationReceipt:
    receipt_id: str
    decision_id: str
    episode_id: str
    target_key: str
    decision_content_hash: str
    validation_status: EpisodeMemoryPromotionValidationStatus
    validator_name: str
    source_delta_ids: tuple[str, ...]
    source_delta_hashes: tuple[str, ...]
    recorded_at: datetime
    failure_reason: str | None
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "receipt_id",
            _validate_non_blank_text(self.receipt_id, "receipt_id"),
        )
        object.__setattr__(
            self,
            "decision_id",
            _validate_non_blank_text(self.decision_id, "decision_id"),
        )
        object.__setattr__(
            self,
            "episode_id",
            _validate_non_blank_text(self.episode_id, "episode_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=EpisodeMemoryContractError),
        )
        object.__setattr__(
            self,
            "decision_content_hash",
            _validate_sha256_hex(self.decision_content_hash, "decision_content_hash"),
        )
        object.__setattr__(
            self,
            "validation_status",
            _validate_promotion_validation_status(
                self.validation_status,
                "validation_status",
            ),
        )
        object.__setattr__(
            self,
            "validator_name",
            _validate_non_blank_text(self.validator_name, "validator_name"),
        )
        object.__setattr__(
            self,
            "source_delta_ids",
            _validate_id_tuple(self.source_delta_ids, "source_delta_ids"),
        )
        object.__setattr__(
            self,
            "source_delta_hashes",
            _validate_sha256_hash_tuple(
                self.source_delta_hashes,
                "source_delta_hashes",
                allow_empty=False,
            ),
        )
        if len(self.source_delta_ids) != len(self.source_delta_hashes):
            raise EpisodeMemoryContractError(
                "len(source_delta_ids) must equal len(source_delta_hashes)."
            )
        object.__setattr__(
            self,
            "recorded_at",
            validate_timestamp(
                self.recorded_at,
                field_name="recorded_at",
                error_type=EpisodeMemoryContractError,
            ),
        )
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                _optional_text(self.failure_reason, "failure_reason"),
            )
        object.__setattr__(
            self,
            "content_hash",
            _canonical_record_hash(self._hash_payload()),
        )

    def _hash_payload(self) -> dict[str, object]:
        payload = self.to_json_payload()
        payload.pop("content_hash", None)
        return payload

    def to_json_payload(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "decision_id": self.decision_id,
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "decision_content_hash": self.decision_content_hash,
            "validation_status": self.validation_status,
            "validator_name": self.validator_name,
            "source_delta_ids": list(self.source_delta_ids),
            "source_delta_hashes": list(self.source_delta_hashes),
            "recorded_at": self.recorded_at.isoformat(),
            "failure_reason": self.failure_reason,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class EpisodeMemoryPromotionWriteReceipt:
    receipt_id: str
    decision_id: str
    episode_id: str
    target_key: str
    decision_content_hash: str
    validated_receipt_id: str
    write_status: EpisodeMemoryPromotionWriteStatus
    artifact_refs: tuple[str, ...]
    recorded_at: datetime
    failure_reason: str | None
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "receipt_id",
            _validate_non_blank_text(self.receipt_id, "receipt_id"),
        )
        object.__setattr__(
            self,
            "decision_id",
            _validate_non_blank_text(self.decision_id, "decision_id"),
        )
        object.__setattr__(
            self,
            "episode_id",
            _validate_non_blank_text(self.episode_id, "episode_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=EpisodeMemoryContractError),
        )
        object.__setattr__(
            self,
            "decision_content_hash",
            _validate_sha256_hex(self.decision_content_hash, "decision_content_hash"),
        )
        object.__setattr__(
            self,
            "validated_receipt_id",
            _validate_non_blank_text(self.validated_receipt_id, "validated_receipt_id"),
        )
        object.__setattr__(
            self,
            "write_status",
            _validate_promotion_write_status(self.write_status, "write_status"),
        )
        object.__setattr__(
            self,
            "artifact_refs",
            _validate_id_tuple(self.artifact_refs, "artifact_refs", allow_empty=True),
        )
        object.__setattr__(
            self,
            "recorded_at",
            validate_timestamp(
                self.recorded_at,
                field_name="recorded_at",
                error_type=EpisodeMemoryContractError,
            ),
        )
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                _optional_text(self.failure_reason, "failure_reason"),
            )
        object.__setattr__(
            self,
            "content_hash",
            _canonical_record_hash(self._hash_payload()),
        )

    def _hash_payload(self) -> dict[str, object]:
        payload = self.to_json_payload()
        payload.pop("content_hash", None)
        return payload

    def to_json_payload(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "decision_id": self.decision_id,
            "episode_id": self.episode_id,
            "target_key": self.target_key,
            "decision_content_hash": self.decision_content_hash,
            "validated_receipt_id": self.validated_receipt_id,
            "write_status": self.write_status,
            "artifact_refs": list(self.artifact_refs),
            "recorded_at": self.recorded_at.isoformat(),
            "failure_reason": self.failure_reason,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class EpisodeMemoryPromotionDecisionView:
    decision_id: str
    promotion_batch_id: str
    episode_id: str
    target_key: str
    promotion_intent: EpisodeMemoryPromotionIntentType
    validation_status: EpisodeMemoryPromotionDecisionValidationViewStatus
    write_status: EpisodeMemoryPromotionDecisionWriteViewStatus
    artifact_refs: tuple[str, ...]
    failure_reason: str | None
    latest_validation_receipt_id: str | None
    latest_write_receipt_id: str | None


def validate_promotion_intent_batch(
    intents: tuple[EpisodeMemoryPromotionDecisionIntent, ...],
) -> None:
    if not isinstance(intents, tuple) or not intents:
        raise EpisodeMemoryContractError(
            "promotion intent batch must contain at least one intent."
        )
    batch_ids = {intent.promotion_batch_id for intent in intents}
    if len(batch_ids) != 1:
        raise EpisodeMemoryContractError(
            "promotion intent batch must use exactly one promotion_batch_id."
        )
    no_promote_count = sum(
        1 for intent in intents if intent.promotion_intent == "no_promote"
    )
    positive_count = len(intents) - no_promote_count
    if no_promote_count == 1 and positive_count == 0:
        return
    if no_promote_count == 0 and positive_count >= 1:
        return
    raise EpisodeMemoryContractError(
        "promotion intent batch must contain exactly one NoPromoteIntent or one "
        "or more positive proposal intents, but not both."
    )


def build_promotion_decision_view(
    *,
    intents: tuple[EpisodeMemoryPromotionDecisionIntent, ...],
    validation_receipts: tuple[EpisodeMemoryPromotionValidationReceipt, ...] = (),
    write_receipts: tuple[EpisodeMemoryPromotionWriteReceipt, ...] = (),
) -> tuple[EpisodeMemoryPromotionDecisionView, ...]:
    if intents:
        _validate_promotion_batches(intents)
    latest_validation_by_decision: dict[
        str, EpisodeMemoryPromotionValidationReceipt
    ] = {}
    for validation_record in validation_receipts:
        latest_validation_by_decision[validation_record.decision_id] = (
            validation_record
        )
    latest_write_by_decision: dict[str, EpisodeMemoryPromotionWriteReceipt] = {}
    for write_record in write_receipts:
        latest_write_by_decision[write_record.decision_id] = write_record

    views: list[EpisodeMemoryPromotionDecisionView] = []
    for intent in intents:
        validation_receipt: EpisodeMemoryPromotionValidationReceipt | None = (
            latest_validation_by_decision.get(intent.decision_id)
        )
        write_receipt: EpisodeMemoryPromotionWriteReceipt | None = (
            latest_write_by_decision.get(intent.decision_id)
        )
        validation_status, write_status = _derive_promotion_view_statuses(
            intent=intent,
            validation_receipt=validation_receipt,
            write_receipt=write_receipt,
        )
        failure_reason = None
        artifact_refs: tuple[str, ...] = ()
        if write_receipt is not None:
            artifact_refs = write_receipt.artifact_refs
            failure_reason = write_receipt.failure_reason
        elif validation_receipt is not None:
            failure_reason = validation_receipt.failure_reason
        views.append(
            EpisodeMemoryPromotionDecisionView(
                decision_id=intent.decision_id,
                promotion_batch_id=intent.promotion_batch_id,
                episode_id=intent.episode_id,
                target_key=intent.target_key,
                promotion_intent=intent.promotion_intent,
                validation_status=validation_status,
                write_status=write_status,
                artifact_refs=artifact_refs,
                failure_reason=failure_reason,
                latest_validation_receipt_id=(
                    validation_receipt.receipt_id
                    if validation_receipt is not None
                    else None
                ),
                latest_write_receipt_id=(
                    write_receipt.receipt_id if write_receipt is not None else None
                ),
            )
        )
    return tuple(views)


def parse_episode_memory_delta_source_refs(
    payload: Mapping[str, object],
) -> EpisodeMemoryDeltaSourceRefs:
    return EpisodeMemoryDeltaSourceRefs(
        evidence_event_ids=tuple(_require_string_list(payload, "evidence_event_ids")),
        market_bar_refs=tuple(_require_string_list(payload, "market_bar_refs")),
        review_paths=tuple(_require_string_list(payload, "review_paths")),
        pm_review_request_ids=tuple(_require_string_list(payload, "pm_review_request_ids")),
        pm_decision_ids=tuple(_require_string_list(payload, "pm_decision_ids")),
        execution_record_ids=tuple(_require_string_list(payload, "execution_record_ids")),
        portfolio_record_ids=tuple(_require_string_list(payload, "portfolio_record_ids")),
    )


def parse_episode_memory_delta_candidate(
    payload: Mapping[str, object]
) -> EpisodeMemoryDeltaCandidate:
    return EpisodeMemoryDeltaCandidate(
        episode_id=_require_non_blank_text(payload, "episode_id"),
        target_key=_require_non_blank_text(payload, "target_key"),
        delta_kind=_require_non_blank_text(payload, "delta_kind"),
        memory_status=cast(
            EpisodeMemoryStatus,
            _require_non_blank_text(payload, "memory_status"),
        ),
        validation_basis=cast(
            EpisodeMemoryValidationBasis,
            _require_non_blank_text(payload, "validation_basis"),
        ),
        source_visible_through=_parse_datetime(
            payload.get("source_visible_through"), "source_visible_through"
        ),
        usable_from=_parse_datetime(payload.get("usable_from"), "usable_from"),
        source_refs=parse_episode_memory_delta_source_refs(
            _require_mapping(payload, "source_refs")
        ),
        summary_md=_require_non_blank_text(payload, "summary_md"),
        thesis_delta=_require_non_blank_text(payload, "thesis_delta"),
        pm_management_delta=_require_non_blank_text(payload, "pm_management_delta"),
        risk_delta=_require_non_blank_text(payload, "risk_delta"),
        invalidation_delta=_require_non_blank_text(payload, "invalidation_delta"),
        error_attributions=tuple(_require_string_list(payload, "error_attributions")),
        confidence=_require_float(payload, "confidence"),
        supersedes_delta_ids=tuple(_require_string_list(payload, "supersedes_delta_ids")),
    )


def parse_episode_memory_delta(payload: Mapping[str, object]) -> EpisodeMemoryDelta:
    return EpisodeMemoryDelta(
        delta_id=_require_non_blank_text(payload, "delta_id"),
        episode_id=_require_non_blank_text(payload, "episode_id"),
        target_key=_require_non_blank_text(payload, "target_key"),
        delta_kind=_require_non_blank_text(payload, "delta_kind"),
        memory_status=cast(
            EpisodeMemoryStatus,
            _require_non_blank_text(payload, "memory_status"),
        ),
        validation_basis=cast(
            EpisodeMemoryValidationBasis,
            _require_non_blank_text(payload, "validation_basis"),
        ),
        recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
        source_visible_through=_parse_datetime(
            payload.get("source_visible_through"), "source_visible_through"
        ),
        usable_from=_parse_datetime(payload.get("usable_from"), "usable_from"),
        source_refs=parse_episode_memory_delta_source_refs(
            _require_mapping(payload, "source_refs")
        ),
        summary_md=_require_non_blank_text(payload, "summary_md"),
        thesis_delta=_require_non_blank_text(payload, "thesis_delta"),
        pm_management_delta=_require_non_blank_text(payload, "pm_management_delta"),
        risk_delta=_require_non_blank_text(payload, "risk_delta"),
        invalidation_delta=_require_non_blank_text(payload, "invalidation_delta"),
        error_attributions=tuple(_require_string_list(payload, "error_attributions")),
        confidence=_require_float(payload, "confidence"),
        supersedes_delta_ids=tuple(_require_string_list(payload, "supersedes_delta_ids")),
        content_hash=_require_non_blank_text(payload, "content_hash"),
    )


def parse_episode_memory_no_update_candidate(
    payload: Mapping[str, object]
) -> EpisodeMemoryNoUpdateCandidate:
    return EpisodeMemoryNoUpdateCandidate(
        candidate_id=_require_non_blank_text(payload, "candidate_id"),
        episode_id=_require_non_blank_text(payload, "episode_id"),
        target_key=_require_non_blank_text(payload, "target_key"),
        review_path=_require_non_blank_text(payload, "review_path"),
        business_at=_parse_datetime(payload.get("business_at"), "business_at"),
        source_visible_through=_parse_datetime(
            payload.get("source_visible_through"), "source_visible_through"
        ),
        usable_from=_parse_datetime(payload.get("usable_from"), "usable_from"),
        reason=_require_non_blank_text(payload, "reason"),
        reason_code=_require_non_blank_text(payload, "reason_code"),
        source_refs=parse_episode_memory_delta_source_refs(
            _require_mapping(payload, "source_refs")
        ),
        reviewed_delta_ids=tuple(_require_string_list(payload, "reviewed_delta_ids")),
    )


def parse_episode_memory_no_update(payload: Mapping[str, object]) -> EpisodeMemoryNoUpdate:
    return EpisodeMemoryNoUpdate(
        receipt_id=_require_non_blank_text(payload, "receipt_id"),
        episode_id=_require_non_blank_text(payload, "episode_id"),
        target_key=_require_non_blank_text(payload, "target_key"),
        review_path=_require_non_blank_text(payload, "review_path"),
        business_at=_parse_datetime(payload.get("business_at"), "business_at"),
        recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
        source_visible_through=_parse_datetime(
            payload.get("source_visible_through"), "source_visible_through"
        ),
        usable_from=_parse_datetime(payload.get("usable_from"), "usable_from"),
        content_hash=_require_non_blank_text(payload, "content_hash"),
        reason=_require_non_blank_text(payload, "reason"),
        reason_code=_require_non_blank_text(payload, "reason_code"),
        source_refs=parse_episode_memory_delta_source_refs(
            _require_mapping(payload, "source_refs")
        ),
        reviewed_delta_ids=tuple(_require_string_list(payload, "reviewed_delta_ids")),
    )


def parse_episode_memory_validation_receipt(
    payload: Mapping[str, object]
) -> EpisodeMemoryValidationReceipt:
    return EpisodeMemoryValidationReceipt(
        receipt_id=_require_non_blank_text(payload, "receipt_id"),
        delta_id=_require_non_blank_text(payload, "delta_id"),
        episode_id=_require_non_blank_text(payload, "episode_id"),
        target_key=_require_non_blank_text(payload, "target_key"),
        status=cast(
            EpisodeMemoryValidationReceiptStatus,
            _require_non_blank_text(payload, "status"),
        ),
        recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
        source_visible_through=_parse_datetime(
            payload.get("source_visible_through"), "source_visible_through"
        ),
        usable_from=_parse_datetime(payload.get("usable_from"), "usable_from"),
        failure_reason=_optional_text(payload.get("failure_reason"), "failure_reason"),
    )


def parse_episode_memory_write_receipt(
    payload: Mapping[str, object]
) -> EpisodeMemoryWriteReceipt:
    return EpisodeMemoryWriteReceipt(
        receipt_id=_require_non_blank_text(payload, "receipt_id"),
        delta_id=_require_non_blank_text(payload, "delta_id"),
        episode_id=_require_non_blank_text(payload, "episode_id"),
        target_key=_require_non_blank_text(payload, "target_key"),
        artifact_path=_require_non_blank_text(payload, "artifact_path"),
        content_hash=_require_non_blank_text(payload, "content_hash"),
        source_visible_through=_parse_datetime(
            payload.get("source_visible_through"), "source_visible_through"
        ),
        usable_from=_parse_datetime(payload.get("usable_from"), "usable_from"),
        validator_receipt_id=_require_non_blank_text(payload, "validator_receipt_id"),
        status=cast(EpisodeMemoryWriteStatus, _require_non_blank_text(payload, "status")),
        recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
        failure_reason=_optional_text(payload.get("failure_reason"), "failure_reason"),
    )


def parse_episode_memory_promotion_intent(
    payload: Mapping[str, object],
) -> EpisodeMemoryPromotionDecisionIntent:
    promotion_intent = _require_non_blank_text(payload, "promotion_intent")
    if promotion_intent == "no_promote":
        _reject_forbidden_no_promote_fields(payload)
        return NoPromoteIntent(
            decision_id=_require_non_blank_text(payload, "decision_id"),
            promotion_batch_id=_require_non_blank_text(payload, "promotion_batch_id"),
            episode_id=_require_non_blank_text(payload, "episode_id"),
            target_key=_require_non_blank_text(payload, "target_key"),
            business_at=_parse_datetime(payload.get("business_at"), "business_at"),
            recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
            source_visible_through=_parse_datetime(
                payload.get("source_visible_through"),
                "source_visible_through",
            ),
            usable_from=_parse_datetime(payload.get("usable_from"), "usable_from"),
            source_delta_ids=tuple(_require_string_list(payload, "source_delta_ids")),
            source_delta_hashes=tuple(
                _require_string_list(payload, "source_delta_hashes")
            ),
            promotion_intent=cast(EpisodeMemoryPromotionIntentType, promotion_intent),
            decision_confidence=_require_float(payload, "decision_confidence"),
            content_hash=_require_non_blank_text(payload, "content_hash"),
            no_promote_reason=_require_non_blank_text(payload, "no_promote_reason"),
            no_promote_reason_code=_require_non_blank_text(
                payload,
                "no_promote_reason_code",
            ),
        )
    if promotion_intent == "propose_learning_card":
        _reject_multiple_card_fields(payload)
        return LearningCardPromotionIntent(
            decision_id=_require_non_blank_text(payload, "decision_id"),
            promotion_batch_id=_require_non_blank_text(payload, "promotion_batch_id"),
            episode_id=_require_non_blank_text(payload, "episode_id"),
            target_key=_require_non_blank_text(payload, "target_key"),
            business_at=_parse_datetime(payload.get("business_at"), "business_at"),
            recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
            source_visible_through=_parse_datetime(
                payload.get("source_visible_through"),
                "source_visible_through",
            ),
            usable_from=_parse_datetime(payload.get("usable_from"), "usable_from"),
            source_delta_ids=tuple(_require_string_list(payload, "source_delta_ids")),
            source_delta_hashes=tuple(
                _require_string_list(payload, "source_delta_hashes")
            ),
            promotion_intent=cast(EpisodeMemoryPromotionIntentType, promotion_intent),
            decision_confidence=_require_float(payload, "decision_confidence"),
            content_hash=_require_non_blank_text(payload, "content_hash"),
            scope_key=_require_non_blank_text(payload, "scope_key"),
            consumer_role=_require_non_blank_text(payload, "consumer_role"),
            title=_require_non_blank_text(payload, "title"),
            summary_md=_require_non_blank_text(payload, "summary_md"),
            body_md=_require_non_blank_text(payload, "body_md"),
            use_when=_require_non_blank_text(payload, "use_when"),
            avoid_when=_require_non_blank_text(payload, "avoid_when"),
            tags=tuple(_require_string_list(payload, "tags")),
            supersedes_card_ids=tuple(
                _require_string_list(payload, "supersedes_card_ids")
            ),
        )
    if promotion_intent == "propose_risk_policy_candidate":
        return RiskPolicyCandidatePromotionIntent(
            decision_id=_require_non_blank_text(payload, "decision_id"),
            promotion_batch_id=_require_non_blank_text(payload, "promotion_batch_id"),
            episode_id=_require_non_blank_text(payload, "episode_id"),
            target_key=_require_non_blank_text(payload, "target_key"),
            business_at=_parse_datetime(payload.get("business_at"), "business_at"),
            recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
            source_visible_through=_parse_datetime(
                payload.get("source_visible_through"),
                "source_visible_through",
            ),
            usable_from=_parse_datetime(payload.get("usable_from"), "usable_from"),
            source_delta_ids=tuple(_require_string_list(payload, "source_delta_ids")),
            source_delta_hashes=tuple(
                _require_string_list(payload, "source_delta_hashes")
            ),
            promotion_intent=cast(EpisodeMemoryPromotionIntentType, promotion_intent),
            decision_confidence=_require_float(payload, "decision_confidence"),
            content_hash=_require_non_blank_text(payload, "content_hash"),
            scope_key=_require_non_blank_text(payload, "scope_key"),
            consumer_role=_require_non_blank_text(payload, "consumer_role"),
            proposed_patch=_require_mapping(payload, "proposed_patch"),
            rationale_md=_require_non_blank_text(payload, "rationale_md"),
        )
    raise EpisodeMemoryContractError(
        "promotion_intent is not a supported episode memory promotion intent."
    )


def parse_episode_memory_promotion_validation_receipt(
    payload: Mapping[str, object],
) -> EpisodeMemoryPromotionValidationReceipt:
    return EpisodeMemoryPromotionValidationReceipt(
        receipt_id=_require_non_blank_text(payload, "receipt_id"),
        decision_id=_require_non_blank_text(payload, "decision_id"),
        episode_id=_require_non_blank_text(payload, "episode_id"),
        target_key=_require_non_blank_text(payload, "target_key"),
        decision_content_hash=_require_non_blank_text(payload, "decision_content_hash"),
        validation_status=cast(
            EpisodeMemoryPromotionValidationStatus,
            _require_non_blank_text(payload, "validation_status"),
        ),
        validator_name=_require_non_blank_text(payload, "validator_name"),
        source_delta_ids=tuple(_require_string_list(payload, "source_delta_ids")),
        source_delta_hashes=tuple(
            _require_string_list(payload, "source_delta_hashes")
        ),
        recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
        failure_reason=_optional_text(payload.get("failure_reason"), "failure_reason"),
        content_hash=_require_non_blank_text(payload, "content_hash"),
    )


def parse_episode_memory_promotion_write_receipt(
    payload: Mapping[str, object],
) -> EpisodeMemoryPromotionWriteReceipt:
    return EpisodeMemoryPromotionWriteReceipt(
        receipt_id=_require_non_blank_text(payload, "receipt_id"),
        decision_id=_require_non_blank_text(payload, "decision_id"),
        episode_id=_require_non_blank_text(payload, "episode_id"),
        target_key=_require_non_blank_text(payload, "target_key"),
        decision_content_hash=_require_non_blank_text(payload, "decision_content_hash"),
        validated_receipt_id=_require_non_blank_text(payload, "validated_receipt_id"),
        write_status=cast(
            EpisodeMemoryPromotionWriteStatus,
            _require_non_blank_text(payload, "write_status"),
        ),
        artifact_refs=tuple(_require_string_list(payload, "artifact_refs")),
        recorded_at=_parse_datetime(payload.get("recorded_at"), "recorded_at"),
        failure_reason=_optional_text(payload.get("failure_reason"), "failure_reason"),
        content_hash=_require_non_blank_text(payload, "content_hash"),
    )


def episode_memory_record_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_record_hash(payload: Mapping[str, object]) -> str:
    return episode_memory_record_hash(payload)


def _reject_trade_action_phrasing(text: str, *, field_name: str) -> None:
    for pattern in _COMMAND_PATTERNS:
        if pattern.search(text):
            raise EpisodeMemoryContractError(
                f"{field_name} must not include trade action commands."
            )


def _validate_status(value: object, field_name: str) -> EpisodeMemoryStatus:
    if value not in _VALID_STATUSES:
        raise EpisodeMemoryContractError(
            f"{field_name} is not a supported episode memory status."
        )
    return cast(EpisodeMemoryStatus, value)


def _validate_validation_basis(
    value: object,
    field_name: str,
) -> EpisodeMemoryValidationBasis:
    if value not in _VALID_BASIS:
        raise EpisodeMemoryContractError(
            f"{field_name} is not a supported validation basis."
        )
    return cast(EpisodeMemoryValidationBasis, value)


def _validate_validation_receipt_status(
    value: object,
    field_name: str,
) -> EpisodeMemoryValidationReceiptStatus:
    if value not in _VALIDATION_STATUSES:
        raise EpisodeMemoryContractError(
            f"{field_name} is not a supported validation receipt status."
        )
    return cast(EpisodeMemoryValidationReceiptStatus, value)


def _validate_write_status(value: object, field_name: str) -> EpisodeMemoryWriteStatus:
    if value not in _WRITE_STATUSES:
        raise EpisodeMemoryContractError(
            f"{field_name} is not a supported episode memory write status."
        )
    return cast(EpisodeMemoryWriteStatus, value)


def _validate_promotion_intent_type(
    value: object,
    field_name: str,
) -> EpisodeMemoryPromotionIntentType:
    if value not in _VALID_PROMOTION_INTENTS:
        raise EpisodeMemoryContractError(
            f"{field_name} is not a supported episode memory promotion intent."
        )
    return cast(EpisodeMemoryPromotionIntentType, value)


def _validate_promotion_validation_status(
    value: object,
    field_name: str,
) -> EpisodeMemoryPromotionValidationStatus:
    if value not in _VALID_PROMOTION_VALIDATION_STATUSES:
        raise EpisodeMemoryContractError(
            f"{field_name} is not a supported promotion validation status."
        )
    return cast(EpisodeMemoryPromotionValidationStatus, value)


def _validate_promotion_write_status(
    value: object,
    field_name: str,
) -> EpisodeMemoryPromotionWriteStatus:
    if value not in _VALID_PROMOTION_WRITE_STATUSES:
        raise EpisodeMemoryContractError(
            f"{field_name} is not a supported promotion write status."
        )
    return cast(EpisodeMemoryPromotionWriteStatus, value)


def _validate_event_ids(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise EpisodeMemoryContractError(f"{field_name} must be a tuple.")
    normalized: list[str] = []
    for value in values:
        normalized.append(validate_event_id(value, error_type=EpisodeMemoryContractError))
    return tuple(normalized)


def _validate_id_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise EpisodeMemoryContractError(f"{field_name} must be a tuple.")
    if not values and not allow_empty:
        raise EpisodeMemoryContractError(f"{field_name} must not be empty.")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _validate_non_blank_text(value, field_name)
        if text in seen:
            raise EpisodeMemoryContractError(f"{field_name} must not contain duplicates.")
        normalized.append(text)
        seen.add(text)
    return tuple(normalized)


def _validate_text_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise EpisodeMemoryContractError(f"{field_name} must be a tuple.")
    if not values and not allow_empty:
        raise EpisodeMemoryContractError(f"{field_name} must not be empty.")
    normalized: list[str] = []
    for value in values:
        normalized.append(
            normalize_content(
                _validate_non_blank_text(value, field_name),
                field_name=field_name,
                error_type=EpisodeMemoryContractError,
            )
        )
    return tuple(normalized)


def _validate_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EpisodeMemoryContractError("confidence must be a number.")
    normalized = float(value)
    if normalized < 0 or normalized > 1:
        raise EpisodeMemoryContractError("confidence must be between 0 and 1.")
    return normalized


def _validate_promotion_scope_key(value: object, *, target_key: str) -> str:
    text = _validate_non_blank_text(value, "scope_key")
    if text == "shared":
        return text
    expected_target_scope = f"target:{target_key}"
    if text == expected_target_scope:
        return text
    raise EpisodeMemoryContractError(
        "scope_key must be 'shared' or exactly 'target:<target_key>'."
    )


def validate_learning_card_consumer_role(
    value: object,
    field_name: str = "consumer_role",
) -> EpisodeMemoryPromotionLearningConsumerRole:
    if value not in _VALID_LEARNING_CARD_CONSUMER_ROLES:
        raise EpisodeMemoryContractError(
            f"{field_name} must be analysis, pm_review, or reflection_learning."
        )
    return cast(EpisodeMemoryPromotionLearningConsumerRole, value)


def _validate_risk_policy_consumer_role(
    value: object,
    field_name: str,
) -> EpisodeMemoryPromotionRiskConsumerRole:
    if value != "risk_policy_review":
        raise EpisodeMemoryContractError(
            f"{field_name} must be exactly risk_policy_review."
        )
    return "risk_policy_review"


def _validate_patch_mapping(
    value: object,
    field_name: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EpisodeMemoryContractError(f"{field_name} must be an object.")
    if not value:
        raise EpisodeMemoryContractError(f"{field_name} must not be empty.")
    return dict(value)


def _validate_non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise EpisodeMemoryContractError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise EpisodeMemoryContractError(f"{field_name} must be non-blank.")
    return normalized


def _validate_sha256_hex(value: object, field_name: str) -> str:
    text = _validate_non_blank_text(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise EpisodeMemoryContractError(
            f"{field_name} must be a lower-case sha256 hex digest."
        )
    return text


def _validate_sha256_hash_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise EpisodeMemoryContractError(f"{field_name} must be a tuple.")
    if not values and not allow_empty:
        raise EpisodeMemoryContractError(f"{field_name} must not be empty.")
    normalized: list[str] = []
    for value in values:
        normalized.append(_validate_sha256_hex(value, field_name))
    return tuple(normalized)


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EpisodeMemoryContractError(
            f"{field_name} must be a string when set."
        )
    text = value.strip()
    if not text:
        raise EpisodeMemoryContractError(
            f"{field_name} must be non-blank when set."
        )
    return text


def _require_non_blank_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank_text(payload.get(field_name), field_name)


def _require_string_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise EpisodeMemoryContractError(f"{field_name} must be a list.")
    if not all(isinstance(item, str) for item in value):
        raise EpisodeMemoryContractError(f"{field_name} must be a list of strings.")
    return value


def _require_float(payload: Mapping[str, object], field_name: str) -> float:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EpisodeMemoryContractError(f"{field_name} must be a number.")
    normalized = float(value)
    return normalized


def _require_mapping(payload: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        raise EpisodeMemoryContractError(f"{field_name} must be an object.")
    return value


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise EpisodeMemoryContractError(
            f"{field_name} must be an ISO datetime string."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EpisodeMemoryContractError(
            f"{field_name} must be an ISO datetime string."
        ) from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=EpisodeMemoryContractError,
    )


def _reject_forbidden_no_promote_fields(payload: Mapping[str, object]) -> None:
    forbidden_fields = (
        "scope_key",
        "consumer_role",
        "title",
        "summary_md",
        "body_md",
        "use_when",
        "avoid_when",
        "tags",
        "supersedes_card_ids",
        "proposed_patch",
        "rationale_md",
        "artifact_payload",
        "artifact_payloads",
        "cards",
        "learning_cards",
    )
    for field_name in forbidden_fields:
        if field_name in payload:
            raise EpisodeMemoryContractError(
                f"NoPromoteIntent must not include {field_name}."
            )


def _reject_multiple_card_fields(payload: Mapping[str, object]) -> None:
    for field_name in ("cards", "learning_cards", "learning_card_candidates"):
        if field_name in payload:
            raise EpisodeMemoryContractError(
                "LearningCardPromotionIntent proposes exactly one learning card."
            )


def _validate_promotion_batches(
    intents: tuple[EpisodeMemoryPromotionDecisionIntent, ...],
) -> None:
    by_batch: dict[str, list[EpisodeMemoryPromotionDecisionIntent]] = {}
    for intent in intents:
        by_batch.setdefault(intent.promotion_batch_id, []).append(intent)
    for batch_intents in by_batch.values():
        validate_promotion_intent_batch(tuple(batch_intents))


def _derive_promotion_view_statuses(
    *,
    intent: EpisodeMemoryPromotionDecisionIntent,
    validation_receipt: EpisodeMemoryPromotionValidationReceipt | None,
    write_receipt: EpisodeMemoryPromotionWriteReceipt | None,
) -> tuple[
    EpisodeMemoryPromotionDecisionValidationViewStatus,
    EpisodeMemoryPromotionDecisionWriteViewStatus,
]:
    if intent.promotion_intent == "no_promote":
        return ("not_required", "not_attempted")
    if validation_receipt is None:
        return ("pending", "not_attempted")
    if validation_receipt.validation_status == "rejected":
        return ("rejected", "not_attempted")
    if write_receipt is None:
        return ("accepted", "pending")
    if write_receipt.write_status == "written":
        return ("accepted", "written")
    return ("accepted", "failed")


__all__ = [
    "EpisodeMemoryContractError",
    "EpisodeMemoryDelta",
    "EpisodeMemoryDeltaCandidate",
    "EpisodeMemoryDeltaSourceRefs",
    "EpisodeMemoryPromotionDecisionIntent",
    "EPISODE_MEMORY_LEARNING_CARD_CONSUMER_ROLES",
    "NoPromoteIntent",
    "LearningCardPromotionIntent",
    "RiskPolicyCandidatePromotionIntent",
    "EpisodeMemoryPromotionValidationReceipt",
    "EpisodeMemoryPromotionWriteReceipt",
    "EpisodeMemoryPromotionDecisionView",
    "EpisodeMemoryPromotionIntentType",
    "EpisodeMemoryPromotionValidationStatus",
    "EpisodeMemoryPromotionWriteStatus",
    "EpisodeMemoryNoUpdate",
    "EpisodeMemoryNoUpdateCandidate",
    "EpisodeMemoryValidationReceipt",
    "EpisodeMemoryValidationBasis",
    "EpisodeMemoryValidationReceiptStatus",
    "EpisodeMemoryWriteReceipt",
    "EpisodeMemoryWriteStatus",
    "EpisodeMemoryStatus",
    "episode_memory_record_hash",
    "parse_episode_memory_delta",
    "parse_episode_memory_delta_candidate",
    "parse_episode_memory_delta_source_refs",
    "parse_episode_memory_no_update",
    "parse_episode_memory_no_update_candidate",
    "parse_episode_memory_promotion_intent",
    "parse_episode_memory_promotion_validation_receipt",
    "parse_episode_memory_promotion_write_receipt",
    "parse_episode_memory_validation_receipt",
    "parse_episode_memory_write_receipt",
    "validate_promotion_intent_batch",
    "validate_learning_card_consumer_role",
    "build_promotion_decision_view",
]
