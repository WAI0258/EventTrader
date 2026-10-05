"""Controlled risk-policy candidate substrate for promotion output."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Literal, cast

from event_trader.contracts._validators import (
    normalize_content,
    validate_scope_key,
    validate_target_key,
    validate_timestamp,
)
from event_trader.episode_memory.contracts import (
    EpisodeMemoryDelta,
    EpisodeMemoryPromotionValidationStatus,
    EpisodeMemoryPromotionWriteStatus,
    EpisodeMemoryPromotionValidationReceipt,
    EpisodeMemoryPromotionWriteReceipt,
    RiskPolicyCandidatePromotionIntent,
)
from event_trader.episode_memory.store import FileBackedEpisodeMemoryStore
from event_trader.learning_cards.promotion import (
    PromotionDecisionError,
    _normalize_recorded_at as _normalize_promotion_recorded_at,
    validate_positive_promotion_source_deltas,
)
from event_trader.storage import WorkspaceLayout

RiskPolicyReviewStatus = Literal["accepted", "rejected", "compiled"]

_RISK_POLICY_DIR = Path("risk_policy")
_CANDIDATES_FILE = "candidates.jsonl"
_COMPILED_CONFIGS_FILE = "compiled_policy_configs.jsonl"
_REVIEW_RECEIPTS_FILE = "review_receipts.jsonl"
_ALLOWED_PATCH_FIELDS = frozenset(
    {
        "max_abs_target_weight",
        "max_gross_exposure",
        "max_drawdown",
        "require_market_data",
    }
)
_LIVE_CONFIG_FIELDS = frozenset(
    {
        "live_risk_policy_config",
        "risk_policy_config",
        "risk_limits",
        "write_live_config",
    }
)
_COMMAND_PATTERN = re.compile(
    r"(?i)\b(action|please)\s*:\s*(buy|sell|exit|short|reverse|set\s+weight)\b"
    r"|\b(buy|sell|exit|short|reverse)\b"
    r"|\bset\s+weight\b"
)


class RiskPolicyCandidateError(ValueError):
    """Raised when risk-policy candidates or compiled artifacts are invalid."""


@dataclass(frozen=True, slots=True)
class RiskPolicyCandidate:
    """Validated promotion output proposed for deterministic risk-policy review."""

    candidate_id: str
    scope_key: str
    target_key: str | None
    source_promotion_id: str
    source_delta_ids: tuple[str, ...]
    source_delta_hashes: tuple[str, ...]
    consumer_role: str
    usable_from: datetime
    source_visible_through: datetime
    recorded_at: datetime
    rationale_md: str
    proposed_patch: Mapping[str, object]
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_id",
            _validate_non_blank_text(self.candidate_id, "candidate_id"),
        )
        scope_key = validate_scope_key(
            self.scope_key,
            error_type=RiskPolicyCandidateError,
        )
        object.__setattr__(self, "scope_key", scope_key)
        object.__setattr__(
            self,
            "target_key",
            _normalize_target_for_scope(scope_key, self.target_key),
        )
        object.__setattr__(
            self,
            "source_promotion_id",
            _validate_non_blank_text(self.source_promotion_id, "source_promotion_id"),
        )
        object.__setattr__(
            self,
            "source_delta_ids",
            _validate_text_tuple(
                self.source_delta_ids,
                "source_delta_ids",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "source_delta_hashes",
            _validate_hash_tuple(
                self.source_delta_hashes,
                "source_delta_hashes",
                allow_empty=False,
            ),
        )
        if len(self.source_delta_ids) != len(self.source_delta_hashes):
            raise RiskPolicyCandidateError(
                "source_delta_ids and source_delta_hashes must have the same length."
            )
        if self.consumer_role != "risk_policy_review":
            raise RiskPolicyCandidateError(
                "consumer_role must be risk_policy_review."
            )
        object.__setattr__(self, "consumer_role", self.consumer_role)
        object.__setattr__(
            self,
            "source_visible_through",
            _normalize_timestamp(
                self.source_visible_through,
                "source_visible_through",
            ),
        )
        object.__setattr__(
            self,
            "usable_from",
            _normalize_timestamp(self.usable_from, "usable_from"),
        )
        if self.usable_from < self.source_visible_through:
            raise RiskPolicyCandidateError(
                "usable_from must be at or after source_visible_through."
            )
        object.__setattr__(
            self,
            "recorded_at",
            _normalize_timestamp(self.recorded_at, "recorded_at"),
        )
        if self.recorded_at < self.source_visible_through:
            raise RiskPolicyCandidateError(
                "recorded_at must be at or after source_visible_through."
            )
        object.__setattr__(
            self,
            "rationale_md",
            normalize_content(
                _validate_non_blank_text(self.rationale_md, "rationale_md"),
                field_name="rationale_md",
                error_type=RiskPolicyCandidateError,
            ),
        )
        _reject_live_action_phrasing(self.rationale_md, field_name="rationale_md")
        object.__setattr__(
            self,
            "proposed_patch",
            _validate_policy_patch(self.proposed_patch),
        )
        object.__setattr__(self, "content_hash", risk_policy_record_hash(self._hash_payload()))

    def _hash_payload(self) -> dict[str, object]:
        payload = self.to_json_payload()
        payload.pop("content_hash", None)
        return payload

    def to_json_payload(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "scope_key": self.scope_key,
            "target_key": self.target_key,
            "source_promotion_id": self.source_promotion_id,
            "source_delta_ids": list(self.source_delta_ids),
            "source_delta_hashes": list(self.source_delta_hashes),
            "consumer_role": self.consumer_role,
            "usable_from": self.usable_from.isoformat(),
            "source_visible_through": self.source_visible_through.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "rationale_md": self.rationale_md,
            "proposed_patch": dict(self.proposed_patch),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class RiskPolicyConfigCandidate:
    """Reviewed deterministic risk-policy config candidate, not live config."""

    policy_config_id: str
    candidate_id: str
    scope_key: str
    target_key: str | None
    usable_from: datetime
    source_visible_through: datetime
    compiled_at: datetime
    proposed_patch: Mapping[str, object]
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_config_id",
            _validate_non_blank_text(self.policy_config_id, "policy_config_id"),
        )
        object.__setattr__(
            self,
            "candidate_id",
            _validate_non_blank_text(self.candidate_id, "candidate_id"),
        )
        scope_key = validate_scope_key(
            self.scope_key,
            error_type=RiskPolicyCandidateError,
        )
        object.__setattr__(self, "scope_key", scope_key)
        object.__setattr__(
            self,
            "target_key",
            _normalize_target_for_scope(scope_key, self.target_key),
        )
        object.__setattr__(
            self,
            "source_visible_through",
            _normalize_timestamp(
                self.source_visible_through,
                "source_visible_through",
            ),
        )
        object.__setattr__(
            self,
            "usable_from",
            _normalize_timestamp(self.usable_from, "usable_from"),
        )
        if self.usable_from < self.source_visible_through:
            raise RiskPolicyCandidateError(
                "usable_from must be at or after source_visible_through."
            )
        object.__setattr__(
            self,
            "compiled_at",
            _normalize_timestamp(self.compiled_at, "compiled_at"),
        )
        if self.compiled_at < self.source_visible_through:
            raise RiskPolicyCandidateError(
                "compiled_at must be at or after source_visible_through."
            )
        object.__setattr__(
            self,
            "proposed_patch",
            _validate_policy_patch(self.proposed_patch),
        )
        object.__setattr__(self, "content_hash", risk_policy_record_hash(self._hash_payload()))

    def _hash_payload(self) -> dict[str, object]:
        payload = self.to_json_payload()
        payload.pop("content_hash", None)
        return payload

    def to_json_payload(self) -> dict[str, object]:
        return {
            "policy_config_id": self.policy_config_id,
            "candidate_id": self.candidate_id,
            "scope_key": self.scope_key,
            "target_key": self.target_key,
            "usable_from": self.usable_from.isoformat(),
            "source_visible_through": self.source_visible_through.isoformat(),
            "compiled_at": self.compiled_at.isoformat(),
            "proposed_patch": dict(self.proposed_patch),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class RiskPolicyReviewReceipt:
    """Auditable deterministic review/compile receipt for one risk candidate."""

    receipt_id: str
    candidate_id: str
    status: RiskPolicyReviewStatus
    recorded_at: datetime
    candidate_hash: str | None
    policy_config_id: str | None
    policy_config_hash: str | None
    artifact_path: Path | None
    failure_reason: str | None

    def to_json_payload(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "candidate_id": self.candidate_id,
            "status": self.status,
            "recorded_at": self.recorded_at.isoformat(),
            "candidate_hash": self.candidate_hash,
            "policy_config_id": self.policy_config_id,
            "policy_config_hash": self.policy_config_hash,
            "artifact_path": (
                self.artifact_path.as_posix() if self.artifact_path is not None else None
            ),
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class RiskPolicyValidationResult:
    receipt: RiskPolicyReviewReceipt
    candidate: RiskPolicyCandidate | None
    policy_config: RiskPolicyConfigCandidate | None


@dataclass(frozen=True, slots=True)
class ValidatedRiskPolicyPromotion:
    """Validator-approved risk-policy promotion ready for candidate write."""

    intent: RiskPolicyCandidatePromotionIntent
    candidate: RiskPolicyCandidate
    validation_receipt: EpisodeMemoryPromotionValidationReceipt
    source_deltas: tuple[EpisodeMemoryDelta, ...]


@dataclass(frozen=True, slots=True)
class RiskPolicyPromotionValidationResult:
    """Result of deterministic risk-policy promotion validation."""

    receipt: EpisodeMemoryPromotionValidationReceipt
    validated: ValidatedRiskPolicyPromotion | None


class RiskPolicyPromotionValidator:
    """Validate canonical risk-policy promotion intents against source deltas."""

    def __init__(
        self,
        episode_memory_store: FileBackedEpisodeMemoryStore,
        *,
        validator_name: str = "episode-memory-promotion-validator",
        append_receipts: bool = True,
    ) -> None:
        if not isinstance(episode_memory_store, FileBackedEpisodeMemoryStore):
            raise RiskPolicyCandidateError(
                "episode_memory_store must be a FileBackedEpisodeMemoryStore."
            )
        self._episode_memory_store = episode_memory_store
        self._validator_name = validator_name
        self._append_receipts = append_receipts

    def validate_intent(
        self,
        intent: RiskPolicyCandidatePromotionIntent,
        *,
        recorded_at: datetime | None = None,
    ) -> RiskPolicyPromotionValidationResult:
        recorded_at_utc = _normalize_promotion_recorded_at(recorded_at)
        try:
            if not isinstance(intent, RiskPolicyCandidatePromotionIntent):
                raise RiskPolicyCandidateError(
                    "risk-policy promotion requires RiskPolicyCandidatePromotionIntent."
                )
            source_deltas = validate_positive_promotion_source_deltas(
                intent,
                self._episode_memory_store,
            )
            candidate = _candidate_from_intent(intent)
            receipt = _build_promotion_validation_receipt(
                intent=intent,
                status="accepted",
                validator_name=self._validator_name,
                recorded_at=recorded_at_utc,
                failure_reason=None,
            )
        except (
            PromotionDecisionError,
            RiskPolicyCandidateError,
        ) as exc:
            receipt = _build_promotion_validation_receipt(
                intent=intent,
                status="rejected",
                validator_name=self._validator_name,
                recorded_at=recorded_at_utc,
                failure_reason=str(exc),
            )
            if self._append_receipts:
                self._episode_memory_store.append_promotion_validation_receipt(receipt)
            return RiskPolicyPromotionValidationResult(receipt=receipt, validated=None)

        if self._append_receipts:
            self._episode_memory_store.append_promotion_validation_receipt(receipt)
        return RiskPolicyPromotionValidationResult(
            receipt=receipt,
            validated=ValidatedRiskPolicyPromotion(
                intent=intent,
                candidate=candidate,
                validation_receipt=receipt,
                source_deltas=source_deltas,
            ),
        )


class RiskPolicyPromotionWriter:
    """Write only validator-approved risk-policy candidates."""

    def __init__(
        self,
        store: FileBackedRiskPolicyCandidateStore,
        episode_memory_store: FileBackedEpisodeMemoryStore | None = None,
    ) -> None:
        if not isinstance(store, FileBackedRiskPolicyCandidateStore):
            raise RiskPolicyCandidateError(
                "store must be a FileBackedRiskPolicyCandidateStore."
            )
        self._store = store
        self._episode_memory_store = episode_memory_store

    def write(
        self,
        validated: ValidatedRiskPolicyPromotion,
        *,
        recorded_at: datetime | None = None,
    ) -> EpisodeMemoryPromotionWriteReceipt:
        if not isinstance(validated, ValidatedRiskPolicyPromotion):
            raise RiskPolicyCandidateError(
                "writer accepts only ValidatedRiskPolicyPromotion."
            )
        recorded_at_utc = _normalize_promotion_recorded_at(recorded_at)
        try:
            artifact_path = self._store.append_candidate(validated.candidate)
        except RiskPolicyCandidateError as exc:
            receipt = _build_promotion_write_receipt(
                intent=validated.intent,
                validated_receipt_id=validated.validation_receipt.receipt_id,
                status="failed",
                artifact_refs=(),
                recorded_at=recorded_at_utc,
                failure_reason=str(exc),
            )
            if self._episode_memory_store is not None:
                self._episode_memory_store.append_promotion_write_receipt(receipt)
            return receipt

        receipt = _build_promotion_write_receipt(
            intent=validated.intent,
            validated_receipt_id=validated.validation_receipt.receipt_id,
            status="written",
            artifact_refs=(
                f"risk-policy-candidate://{validated.candidate.candidate_id}",
                artifact_path.as_posix(),
            ),
            recorded_at=recorded_at_utc,
            failure_reason=None,
        )
        if self._episode_memory_store is not None:
            self._episode_memory_store.append_promotion_write_receipt(receipt)
        return receipt


@dataclass(frozen=True, slots=True)
class PersistedRiskPolicyCandidate:
    record: RiskPolicyCandidate
    path: Path
    line_number: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class PersistedRiskPolicyConfigCandidate:
    record: RiskPolicyConfigCandidate
    path: Path
    line_number: int
    record_hash: str


class RiskPolicyCompiler:
    """Deterministically validate and compile structured risk-policy candidates."""

    def compile_candidate(
        self,
        candidate: RiskPolicyCandidate,
        *,
        recorded_at: datetime | None = None,
    ) -> RiskPolicyValidationResult:
        recorded_at_utc = _normalize_recorded_at(recorded_at)
        try:
            if not isinstance(candidate, RiskPolicyCandidate):
                raise RiskPolicyCandidateError("candidate must be a RiskPolicyCandidate.")
            policy_config = RiskPolicyConfigCandidate(
                policy_config_id=_deterministic_policy_config_id(candidate),
                candidate_id=candidate.candidate_id,
                scope_key=candidate.scope_key,
                target_key=candidate.target_key,
                usable_from=candidate.usable_from,
                source_visible_through=candidate.source_visible_through,
                compiled_at=recorded_at_utc,
                proposed_patch=candidate.proposed_patch,
            )
        except RiskPolicyCandidateError as exc:
            return RiskPolicyValidationResult(
                receipt=_review_receipt(
                    candidate_id=(
                        candidate.candidate_id
                        if isinstance(candidate, RiskPolicyCandidate)
                        else "invalid-risk-policy-candidate"
                    ),
                    status="rejected",
                    recorded_at=recorded_at_utc,
                    candidate_hash=None,
                    policy_config=None,
                    artifact_path=None,
                    failure_reason=str(exc),
                ),
                candidate=None,
                policy_config=None,
            )
        return RiskPolicyValidationResult(
            receipt=_review_receipt(
                candidate_id=candidate.candidate_id,
                status="compiled",
                recorded_at=recorded_at_utc,
                candidate_hash=candidate.content_hash,
                policy_config=policy_config,
                artifact_path=None,
                failure_reason=None,
            ),
            candidate=candidate,
            policy_config=policy_config,
        )


class FileBackedRiskPolicyCandidateStore:
    """Append-only helper store for reviewed risk-policy candidate artifacts."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise RiskPolicyCandidateError("layout must be a WorkspaceLayout instance.")
        self._layout = layout

    def append_candidate(self, candidate: RiskPolicyCandidate) -> Path:
        path = self.candidates_path()
        candidate_hash = risk_policy_record_hash(candidate.to_json_payload())
        for persisted in self.read_candidates():
            if persisted.record.candidate_id != candidate.candidate_id:
                continue
            if persisted.record_hash == candidate_hash:
                return path
            raise RiskPolicyCandidateError("duplicate candidate_id has different payload.")
        _append_json_line(path, candidate.to_json_payload())
        return path

    def append_policy_config(self, policy_config: RiskPolicyConfigCandidate) -> Path:
        path = self.compiled_configs_path()
        config_hash = risk_policy_record_hash(policy_config.to_json_payload())
        for persisted in self.read_policy_configs():
            if persisted.record.policy_config_id != policy_config.policy_config_id:
                continue
            if persisted.record_hash == config_hash:
                return path
            raise RiskPolicyCandidateError(
                "duplicate policy_config_id has different payload."
            )
        _append_json_line(path, policy_config.to_json_payload())
        return path

    def append_review_receipt(self, receipt: RiskPolicyReviewReceipt) -> Path:
        path = self.review_receipts_path()
        _append_json_line(path, receipt.to_json_payload())
        return path

    def read_candidates(self) -> tuple[PersistedRiskPolicyCandidate, ...]:
        return _read_candidates(self.candidates_path())

    def read_policy_configs(self) -> tuple[PersistedRiskPolicyConfigCandidate, ...]:
        return _read_policy_configs(self.compiled_configs_path())

    def candidates_path(self) -> Path:
        return (self._layout.helpers_root / _RISK_POLICY_DIR / _CANDIDATES_FILE).resolve(
            strict=False
        )

    def compiled_configs_path(self) -> Path:
        return (
            self._layout.helpers_root / _RISK_POLICY_DIR / _COMPILED_CONFIGS_FILE
        ).resolve(strict=False)

    def review_receipts_path(self) -> Path:
        return (
            self._layout.helpers_root / _RISK_POLICY_DIR / _REVIEW_RECEIPTS_FILE
        ).resolve(strict=False)


def risk_policy_candidate_from_validated_promotion(
    validated: ValidatedRiskPolicyPromotion,
) -> RiskPolicyCandidate:
    """Build a structured risk-policy candidate from a validated promotion."""

    if not isinstance(validated, ValidatedRiskPolicyPromotion):
        raise RiskPolicyCandidateError(
            "validated promotion must be ValidatedRiskPolicyPromotion."
        )
    return validated.candidate


def _candidate_from_intent(
    intent: RiskPolicyCandidatePromotionIntent,
) -> RiskPolicyCandidate:
    proposed_patch = cast(Mapping[str, object], intent.proposed_patch)
    return RiskPolicyCandidate(
        candidate_id=_deterministic_risk_policy_candidate_id(
            promotion_id=intent.decision_id,
            source_delta_ids=intent.source_delta_ids,
            proposed_patch=proposed_patch,
        ),
        scope_key=intent.scope_key,
        target_key=None if intent.scope_key == "shared" else intent.target_key,
        source_promotion_id=intent.decision_id,
        source_delta_ids=intent.source_delta_ids,
        source_delta_hashes=intent.source_delta_hashes,
        consumer_role=intent.consumer_role,
        usable_from=intent.usable_from,
        source_visible_through=intent.source_visible_through,
        recorded_at=intent.recorded_at,
        rationale_md=intent.rationale_md,
        proposed_patch=proposed_patch,
    )


def _build_promotion_validation_receipt(
    *,
    intent: RiskPolicyCandidatePromotionIntent,
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


def _build_promotion_write_receipt(
    *,
    intent: RiskPolicyCandidatePromotionIntent,
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


def risk_policy_record_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def parse_risk_policy_candidate(payload: Mapping[str, object]) -> RiskPolicyCandidate:
    patch = payload.get("proposed_patch")
    if not isinstance(patch, Mapping):
        raise RiskPolicyCandidateError("proposed_patch must be a mapping.")
    return RiskPolicyCandidate(
        candidate_id=_require_text(payload, "candidate_id"),
        scope_key=_require_text(payload, "scope_key"),
        target_key=_optional_text(payload.get("target_key"), "target_key"),
        source_promotion_id=_require_text(payload, "source_promotion_id"),
        source_delta_ids=tuple(_require_string_list(payload, "source_delta_ids")),
        source_delta_hashes=tuple(_require_string_list(payload, "source_delta_hashes")),
        consumer_role=_require_text(payload, "consumer_role"),
        usable_from=_parse_timestamp(payload.get("usable_from"), "usable_from"),
        source_visible_through=_parse_timestamp(
            payload.get("source_visible_through"),
            "source_visible_through",
        ),
        recorded_at=_parse_timestamp(payload.get("recorded_at"), "recorded_at"),
        rationale_md=_require_text(payload, "rationale_md"),
        proposed_patch=patch,
    )


def parse_risk_policy_config_candidate(
    payload: Mapping[str, object],
) -> RiskPolicyConfigCandidate:
    patch = payload.get("proposed_patch")
    if not isinstance(patch, Mapping):
        raise RiskPolicyCandidateError("proposed_patch must be a mapping.")
    return RiskPolicyConfigCandidate(
        policy_config_id=_require_text(payload, "policy_config_id"),
        candidate_id=_require_text(payload, "candidate_id"),
        scope_key=_require_text(payload, "scope_key"),
        target_key=_optional_text(payload.get("target_key"), "target_key"),
        usable_from=_parse_timestamp(payload.get("usable_from"), "usable_from"),
        source_visible_through=_parse_timestamp(
            payload.get("source_visible_through"),
            "source_visible_through",
        ),
        compiled_at=_parse_timestamp(payload.get("compiled_at"), "compiled_at"),
        proposed_patch=patch,
    )


def _review_receipt(
    *,
    candidate_id: str,
    status: RiskPolicyReviewStatus,
    recorded_at: datetime,
    candidate_hash: str | None,
    policy_config: RiskPolicyConfigCandidate | None,
    artifact_path: Path | None,
    failure_reason: str | None,
) -> RiskPolicyReviewReceipt:
    policy_config_id = policy_config.policy_config_id if policy_config is not None else None
    policy_config_hash = policy_config.content_hash if policy_config is not None else None
    return RiskPolicyReviewReceipt(
        receipt_id=_deterministic_review_receipt_id(
            candidate_id=candidate_id,
            status=status,
            policy_config_id=policy_config_id,
        ),
        candidate_id=candidate_id,
        status=status,
        recorded_at=recorded_at,
        candidate_hash=candidate_hash,
        policy_config_id=policy_config_id,
        policy_config_hash=policy_config_hash,
        artifact_path=artifact_path,
        failure_reason=failure_reason,
    )


def _validate_policy_patch(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RiskPolicyCandidateError("proposed_patch must be a mapping.")
    patch = dict(value)
    if not patch:
        raise RiskPolicyCandidateError("proposed_patch must not be empty.")
    forbidden = _LIVE_CONFIG_FIELDS.intersection(patch)
    if forbidden:
        raise RiskPolicyCandidateError(
            "risk policy candidates must not write directly into live risk policy config."
        )
    unknown = set(patch).difference(_ALLOWED_PATCH_FIELDS)
    if unknown:
        raise RiskPolicyCandidateError("proposed_patch contains unsupported fields.")
    normalized: dict[str, object] = {}
    for key, item in patch.items():
        if key == "require_market_data":
            if not isinstance(item, bool):
                raise RiskPolicyCandidateError("require_market_data must be a bool.")
            normalized[key] = item
            continue
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise RiskPolicyCandidateError(f"{key} must be numeric.")
        numeric = float(item)
        if not isfinite(numeric) or numeric <= 0.0:
            raise RiskPolicyCandidateError(f"{key} must be finite and positive.")
        normalized[key] = numeric
    return normalized


def _read_candidates(path: Path) -> tuple[PersistedRiskPolicyCandidate, ...]:
    records: list[PersistedRiskPolicyCandidate] = []
    for payload, line_number in _read_payloads(path):
        candidate = parse_risk_policy_candidate(payload)
        records.append(
            PersistedRiskPolicyCandidate(
                record=candidate,
                path=path,
                line_number=line_number,
                record_hash=risk_policy_record_hash(candidate.to_json_payload()),
            )
        )
    return tuple(records)


def _read_policy_configs(path: Path) -> tuple[PersistedRiskPolicyConfigCandidate, ...]:
    records: list[PersistedRiskPolicyConfigCandidate] = []
    for payload, line_number in _read_payloads(path):
        policy_config = parse_risk_policy_config_candidate(payload)
        records.append(
            PersistedRiskPolicyConfigCandidate(
                record=policy_config,
                path=path,
                line_number=line_number,
                record_hash=risk_policy_record_hash(policy_config.to_json_payload()),
            )
        )
    return tuple(records)


def _read_payloads(path: Path) -> tuple[tuple[Mapping[str, object], int], ...]:
    if not path.exists():
        return ()
    if not path.is_file():
        raise RiskPolicyCandidateError(f"risk policy path must be a file: {path}")
    records: list[tuple[Mapping[str, object], int]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            normalized = line.strip()
            if not normalized:
                continue
            payload = json.loads(normalized)
            if not isinstance(payload, Mapping):
                raise RiskPolicyCandidateError(
                    f"risk policy line {line_number} must be an object."
                )
            records.append((payload, line_number))
    return tuple(records)


def _append_json_line(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        handle.write("\n")


def _deterministic_risk_policy_candidate_id(
    *,
    promotion_id: str,
    source_delta_ids: tuple[str, ...],
    proposed_patch: Mapping[str, object],
) -> str:
    digest = risk_policy_record_hash(
        {
            "promotion_id": promotion_id,
            "source_delta_ids": list(source_delta_ids),
            "proposed_patch": dict(proposed_patch),
        }
    )
    return f"risk-policy-candidate-{digest[:24]}"


def _stable_prefixed_id(prefix: str, payload: Mapping[str, object]) -> str:
    return f"{prefix}-{risk_policy_record_hash(payload)[:24]}"


def _deterministic_policy_config_id(candidate: RiskPolicyCandidate) -> str:
    digest = risk_policy_record_hash(
        {
            "candidate_id": candidate.candidate_id,
            "candidate_hash": candidate.content_hash,
        }
    )
    return f"risk-policy-config-candidate-{digest[:24]}"


def _deterministic_review_receipt_id(
    *,
    candidate_id: str,
    status: str,
    policy_config_id: str | None,
) -> str:
    digest = risk_policy_record_hash(
        {
            "candidate_id": candidate_id,
            "status": status,
            "policy_config_id": policy_config_id,
        }
    )
    return f"risk-policy-review-receipt-{digest[:24]}"


def _normalize_target_for_scope(scope_key: str, target_key: str | None) -> str | None:
    if scope_key == "shared":
        if target_key is not None:
            raise RiskPolicyCandidateError("shared risk policy requires target_key=None.")
        return None
    normalized_target = _optional_text(target_key, "target_key")
    if normalized_target is None:
        raise RiskPolicyCandidateError("target-scoped risk policy requires target_key.")
    normalized_target = validate_target_key(
        normalized_target,
        error_type=RiskPolicyCandidateError,
    )
    if normalized_target != scope_key.partition(":")[2]:
        raise RiskPolicyCandidateError("scope_key target segment must match target_key.")
    return normalized_target


def _normalize_timestamp(value: datetime, field_name: str) -> datetime:
    return validate_timestamp(
        value,
        field_name=field_name,
        error_type=RiskPolicyCandidateError,
    ).astimezone(UTC)


def _normalize_recorded_at(value: datetime | None) -> datetime:
    return _normalize_timestamp(value or datetime.now(UTC), "recorded_at")


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise RiskPolicyCandidateError(f"{field_name} must be an ISO datetime string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RiskPolicyCandidateError(
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
        raise RiskPolicyCandidateError(f"{field_name} must be a tuple.")
    if not values and not allow_empty:
        raise RiskPolicyCandidateError(f"{field_name} must not be empty.")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _validate_non_blank_text(item, field_name)
        if text in seen:
            raise RiskPolicyCandidateError(f"{field_name} must not contain duplicates.")
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


def _validate_hash_tuple(
    values: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    return tuple(
        _validate_sha256_hex(value, field_name)
        for value in _validate_text_tuple(
            values,
            field_name,
            allow_empty=allow_empty,
        )
    )


def _validate_sha256_hex(value: object, field_name: str) -> str:
    text = _validate_non_blank_text(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise RiskPolicyCandidateError(
            f"{field_name} must be a lower-case sha256 hex digest."
        )
    return text


def _validate_non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise RiskPolicyCandidateError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise RiskPolicyCandidateError(f"{field_name} must be non-blank.")
    return normalized


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_non_blank_text(payload.get(field_name), field_name)


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_non_blank_text(value, field_name)


def _require_string_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RiskPolicyCandidateError(f"{field_name} must be a list of strings.")
    return value


def _reject_live_action_phrasing(text: str, *, field_name: str) -> None:
    if _COMMAND_PATTERN.search(text):
        raise RiskPolicyCandidateError(
            f"{field_name} must not include live trading commands."
        )


__all__ = [
    "FileBackedRiskPolicyCandidateStore",
    "PersistedRiskPolicyCandidate",
    "PersistedRiskPolicyConfigCandidate",
    "RiskPolicyCandidate",
    "RiskPolicyCandidateError",
    "RiskPolicyCompiler",
    "RiskPolicyConfigCandidate",
    "RiskPolicyPromotionValidationResult",
    "RiskPolicyPromotionValidator",
    "RiskPolicyPromotionWriter",
    "RiskPolicyReviewReceipt",
    "RiskPolicyReviewStatus",
    "RiskPolicyValidationResult",
    "ValidatedRiskPolicyPromotion",
    "parse_risk_policy_candidate",
    "parse_risk_policy_config_candidate",
    "risk_policy_candidate_from_validated_promotion",
    "risk_policy_record_hash",
]
