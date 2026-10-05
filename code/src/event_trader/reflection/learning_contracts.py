"""Typed learning lifecycle contracts for reflection writeback."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from event_trader.contracts._validators import normalize_content, validate_target_key
from event_trader.contracts.research_memory import resolve_page_ref

type ReflectionLearningOutcome = Literal[
    "no_learning_write",
    "emit_episode_memory_candidate",
]
type ReflectionEpisodeMemoryCandidateKind = Literal["delta", "no_update"]
type ReflectionErrorAttribution = Literal[
    "attention_error",
    "interpretation_error",
    "timing_error",
    "sizing_error",
    "risk_management_error",
    "execution_error",
    "exit_error",
    "market_regime_error",
    "unavoidable_noise",
]
type ReflectionLearningActionReceiptStatus = Literal["written", "skipped", "failed"]

REFLECTION_LEARNING_OUTCOMES: tuple[ReflectionLearningOutcome, ...] = (
    "no_learning_write",
    "emit_episode_memory_candidate",
)
REFLECTION_ERROR_ATTRIBUTIONS: tuple[ReflectionErrorAttribution, ...] = (
    "attention_error",
    "interpretation_error",
    "timing_error",
    "sizing_error",
    "risk_management_error",
    "execution_error",
    "exit_error",
    "market_regime_error",
    "unavoidable_noise",
)
class ReflectionLearningContractError(ValueError):
    """Raised when reflection learning lifecycle data is malformed."""


@dataclass(frozen=True, slots=True)
class ReflectionLearningAction:
    """One typed learning action authorized by a reflection evaluation."""

    action_id: str
    outcome: ReflectionLearningOutcome
    rationale: str
    source_episode_id: str
    source_review_path: str
    effective_from: datetime
    error_attributions: tuple[ReflectionErrorAttribution, ...]
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _normalize_id(self.action_id, "action_id"))
        object.__setattr__(self, "outcome", _normalize_outcome(self.outcome))
        object.__setattr__(
            self,
            "rationale",
            normalize_content(
                self.rationale,
                field_name="rationale",
                error_type=ReflectionLearningContractError,
            ),
        )
        object.__setattr__(
            self,
            "source_episode_id",
            _normalize_id(self.source_episode_id, "source_episode_id"),
        )
        page_ref = resolve_page_ref(self.source_review_path)
        if page_ref.page_kind != "review":
            raise ReflectionLearningContractError(
                "source_review_path must resolve to a canonical review page."
            )
        object.__setattr__(self, "source_review_path", page_ref.page_path)
        object.__setattr__(
            self,
            "effective_from",
            _normalize_datetime(self.effective_from, "effective_from"),
        )
        object.__setattr__(
            self,
            "error_attributions",
            _normalize_error_attributions(self.error_attributions),
        )
        if not isinstance(self.payload, Mapping):
            raise ReflectionLearningContractError("payload must be a mapping.")
        payload = dict(self.payload)
        object.__setattr__(self, "payload", payload)
        _validate_action_payload(self.outcome, payload)

    @property
    def writes_artifact(self) -> bool:
        return self.outcome == "emit_episode_memory_candidate"


@dataclass(frozen=True, slots=True)
class ReflectionLearningDecision:
    """Typed learning decision emitted by reflection evaluation."""

    primary_outcome: ReflectionLearningOutcome
    actions: tuple[ReflectionLearningAction, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "primary_outcome", _normalize_outcome(self.primary_outcome))
        if not isinstance(self.actions, tuple) or not self.actions:
            raise ReflectionLearningContractError(
                "actions must be a non-empty tuple of ReflectionLearningAction values."
            )
        seen: set[str] = set()
        for action in self.actions:
            if not isinstance(action, ReflectionLearningAction):
                raise ReflectionLearningContractError(
                    "actions must contain only ReflectionLearningAction values."
                )
            if action.action_id in seen:
                raise ReflectionLearningContractError("action_id values must be unique.")
            seen.add(action.action_id)
        if self.primary_outcome not in {action.outcome for action in self.actions}:
            raise ReflectionLearningContractError(
                "primary_outcome must match one of the action outcomes."
            )
        if self.primary_outcome == "no_learning_write" and any(
            action.writes_artifact for action in self.actions
        ):
            raise ReflectionLearningContractError(
                "no_learning_write decisions must not include write actions."
            )
    @property
    def write_action_count(self) -> int:
        return sum(int(action.writes_artifact) for action in self.actions)

    def validate_for_review_decision(self, review_decision: str) -> None:
        if review_decision == "skip_review":
            if self.primary_outcome != "no_learning_write" or self.write_action_count:
                raise ReflectionLearningContractError(
                    "skip_review can only pair with no_learning_write and no write actions."
                )
            return
        if self.primary_outcome == "no_learning_write":
            raise ReflectionLearningContractError(
                "write_review must use emit_episode_memory_candidate; use "
                "candidate_kind='no_update' when no delta is warranted."
            )


@dataclass(frozen=True, slots=True)
class ReflectionLearningActionReceipt:
    """Receipt for one lifecycle-applied learning action."""

    action_id: str
    outcome: ReflectionLearningOutcome
    status: ReflectionLearningActionReceiptStatus
    episode_id: str
    artifact_path: Path | None = None
    asset_id: str | None = None
    claim_id: str | None = None
    source_review_path: str | None = None
    promotion_receipt_id: str | None = None
    promotion_decision_id: str | None = None
    promotion_card_id: str | None = None
    promotion_artifact_path: Path | None = None
    promotion_status: str | None = None
    skip_reason: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _normalize_id(self.action_id, "action_id"))
        object.__setattr__(self, "outcome", _normalize_outcome(self.outcome))
        if self.status not in {"written", "skipped", "failed"}:
            raise ReflectionLearningContractError(
                "status must be written, skipped, or failed."
            )
        object.__setattr__(self, "episode_id", _normalize_id(self.episode_id, "episode_id"))
        if self.artifact_path is not None and not isinstance(self.artifact_path, Path):
            raise ReflectionLearningContractError(
                "artifact_path must be a pathlib.Path when provided."
            )
        object.__setattr__(
            self,
            "asset_id",
            _optional_text(self.asset_id, field_name="asset_id"),
        )
        object.__setattr__(
            self,
            "claim_id",
            _optional_text(self.claim_id, field_name="claim_id"),
        )
        if self.source_review_path is not None:
            object.__setattr__(
                self,
                "source_review_path",
                resolve_page_ref(self.source_review_path).page_path,
            )
        object.__setattr__(
            self,
            "promotion_receipt_id",
            _optional_text(self.promotion_receipt_id, field_name="promotion_receipt_id"),
        )
        object.__setattr__(
            self,
            "promotion_decision_id",
            _optional_text(
                self.promotion_decision_id,
                field_name="promotion_decision_id",
            ),
        )
        object.__setattr__(
            self,
            "promotion_card_id",
            _optional_text(self.promotion_card_id, field_name="promotion_card_id"),
        )
        if self.promotion_artifact_path is not None and not isinstance(
            self.promotion_artifact_path,
            Path,
        ):
            raise ReflectionLearningContractError(
                "promotion_artifact_path must be a pathlib.Path when provided."
            )
        if self.promotion_status is not None:
            if self.promotion_status not in {"accepted", "rejected", "written"}:
                raise ReflectionLearningContractError(
                    "promotion_status must be accepted, rejected, or written when provided."
                )
        object.__setattr__(
            self,
            "skip_reason",
            _optional_text(self.skip_reason, field_name="skip_reason"),
        )
        object.__setattr__(
            self,
            "failure_reason",
            _optional_text(self.failure_reason, field_name="failure_reason"),
        )
        if self.status == "failed" and self.failure_reason is None:
            raise ReflectionLearningContractError(
                "failed action receipts require failure_reason."
            )
        if self.status == "skipped" and self.skip_reason is None:
            raise ReflectionLearningContractError(
                "skipped action receipts require skip_reason."
            )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "outcome": self.outcome,
            "status": self.status,
            "episode_id": self.episode_id,
            "artifact_path": (
                self.artifact_path.as_posix() if self.artifact_path is not None else None
            ),
            "asset_id": self.asset_id,
            "claim_id": self.claim_id,
            "source_review_path": self.source_review_path,
            "promotion_receipt_id": self.promotion_receipt_id,
            "promotion_decision_id": self.promotion_decision_id,
            "promotion_card_id": self.promotion_card_id,
            "promotion_artifact_path": (
                self.promotion_artifact_path.as_posix()
                if self.promotion_artifact_path is not None
                else None
            ),
            "promotion_status": self.promotion_status,
            "skip_reason": self.skip_reason,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class ReflectionLearningReceipt:
    """Receipt for a full reflection learning decision application."""

    source_episode_id: str
    source_review_path: str
    primary_outcome: ReflectionLearningOutcome
    action_receipts: tuple[ReflectionLearningActionReceipt, ...]
    decision_episode_record_path: Path | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_episode_id",
            _normalize_id(self.source_episode_id, "source_episode_id"),
        )
        object.__setattr__(
            self,
            "source_review_path",
            resolve_page_ref(self.source_review_path).page_path,
        )
        object.__setattr__(self, "primary_outcome", _normalize_outcome(self.primary_outcome))
        if not isinstance(self.action_receipts, tuple) or not self.action_receipts:
            raise ReflectionLearningContractError(
                "action_receipts must be a non-empty tuple."
            )
        for receipt in self.action_receipts:
            if not isinstance(receipt, ReflectionLearningActionReceipt):
                raise ReflectionLearningContractError(
                    "action_receipts must contain only ReflectionLearningActionReceipt "
                    "values."
                )
        if self.decision_episode_record_path is not None and not isinstance(
            self.decision_episode_record_path, Path
        ):
            raise ReflectionLearningContractError(
                "decision_episode_record_path must be a pathlib.Path when provided."
            )

    @property
    def action_count(self) -> int:
        return len(self.action_receipts)

    @property
    def written_count(self) -> int:
        return sum(int(receipt.status == "written") for receipt in self.action_receipts)

    @property
    def failed_count(self) -> int:
        return sum(int(receipt.status == "failed") for receipt in self.action_receipts)

    def to_json_payload(self) -> dict[str, object]:
        return {
            "source_episode_id": self.source_episode_id,
            "source_review_path": self.source_review_path,
            "primary_outcome": self.primary_outcome,
            "action_receipts": [
                receipt.to_json_payload() for receipt in self.action_receipts
            ],
            "decision_episode_record_path": (
                self.decision_episode_record_path.as_posix()
                if self.decision_episode_record_path is not None
                else None
            ),
        }


def parse_learning_decision_payload(
    value: object,
    *,
    review_decision: str,
) -> ReflectionLearningDecision:
    """Parse MiroThinker JSON into a strict learning decision."""

    if not isinstance(value, Mapping):
        raise ReflectionLearningContractError("learning_decision must be a JSON object.")
    primary_outcome = _require_text(value, "primary_outcome")
    raw_actions = value.get("actions")
    if not isinstance(raw_actions, list):
        raise ReflectionLearningContractError("learning_decision.actions must be an array.")
    actions = tuple(
        _parse_action_payload(item, index=index)
        for index, item in enumerate(raw_actions)
    )
    decision = ReflectionLearningDecision(
        primary_outcome=_normalize_outcome(primary_outcome),
        actions=actions,
    )
    decision.validate_for_review_decision(review_decision)
    return decision


def _parse_action_payload(value: object, *, index: int) -> ReflectionLearningAction:
    if not isinstance(value, Mapping):
        raise ReflectionLearningContractError(
            f"learning_decision.actions[{index}] must be a JSON object."
        )
    attributions = value.get("error_attributions")
    if not isinstance(attributions, list):
        raise ReflectionLearningContractError(
            f"learning_decision.actions[{index}].error_attributions must be an array."
        )
    return ReflectionLearningAction(
        action_id=_require_text(value, "action_id"),
        outcome=_normalize_outcome(_require_text(value, "outcome")),
        rationale=_require_text(value, "rationale"),
        source_episode_id=_require_text(value, "source_episode_id"),
        source_review_path=_require_text(value, "source_review_path"),
        effective_from=_parse_datetime(value.get("effective_from"), "effective_from"),
        error_attributions=tuple(
            _normalize_error_attribution(str(item)) for item in attributions
        ),
        payload=_require_mapping(value, "payload"),
    )


def _validate_action_payload(
    outcome: ReflectionLearningOutcome,
    payload: Mapping[str, object],
) -> None:
    if outcome == "no_learning_write":
        return
    if outcome == "emit_episode_memory_candidate":
        candidate_kind = _validate_candidate_kind(
            _require_text(payload, "candidate_kind")
        )
        if candidate_kind == "delta":
            _require_text(payload, "delta_kind")
            memory_status = _require_text(payload, "memory_status")
            validation_basis = _require_text(payload, "validation_basis")
            _require_datetime_field(payload, "source_visible_through")
            _require_datetime_field(payload, "usable_from")
            _require_mapping(payload, "source_refs")
            _require_text(payload, "summary_md")
            _require_text(payload, "thesis_delta")
            _require_text(payload, "pm_management_delta")
            _require_text(payload, "risk_delta")
            _require_text(payload, "invalidation_delta")
            _require_list(payload, "error_attributions")
            _require_number(payload, "confidence")
            _require_list(payload, "supersedes_delta_ids")
            if payload.get("promotion") is not None and (
                memory_status != "finalized" or validation_basis != "close_review"
            ):
                raise ReflectionLearningContractError(
                    "promotion is allowed only for finalized close_review delta candidates."
                )
            _validate_optional_promotion_payload(payload.get("promotion"))
            return
        if payload.get("promotion") is not None:
            raise ReflectionLearningContractError(
                "promotion can only be requested for delta episode-memory candidates."
            )
        reason_md = payload.get("reason_md")
        if reason_md is None:
            _require_text(payload, "reason")
        else:
            _optional_text(reason_md, field_name="reason_md")
            if payload.get("reason") is not None:
                _optional_text(payload.get("reason"), field_name="reason")
        _require_text(payload, "reason_code")
        _require_datetime_field(payload, "source_visible_through")
        _require_datetime_field(payload, "usable_from")
        _require_mapping(payload, "source_refs")
        _require_list(payload, "reviewed_delta_ids")
        _optional_text(payload.get("receipt_id"), field_name="receipt_id")
        return
    raise ReflectionLearningContractError(f"unknown learning outcome: {outcome!r}")


def _validate_optional_promotion_payload(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ReflectionLearningContractError("promotion must be a JSON object when set.")
    _require_text(value, "consumer_role")
    _require_text(value, "scope_key")
    _require_text(value, "title")
    _require_text(value, "summary_md")
    _require_text(value, "body_md")
    _require_list(value, "use_when")
    _require_list(value, "avoid_when")
    _optional_string_list(value.get("tags"), "tags")
    _optional_string_list(value.get("supersedes_card_ids"), "supersedes_card_ids")
    _optional_text(value.get("promotion_id"), field_name="promotion_id")
    _optional_text(value.get("promotion_decision_id"), field_name="promotion_decision_id")
    _optional_text(value.get("card_id"), field_name="card_id")
    if value.get("confidence") is not None:
        _require_number(value, "confidence")
    if value.get("usable_from") is not None:
        _require_datetime_field(value, "usable_from")
    if value.get("created_at") is not None:
        _require_datetime_field(value, "created_at")


def _normalize_outcome(value: str) -> ReflectionLearningOutcome:
    if value not in REFLECTION_LEARNING_OUTCOMES:
        allowed = ", ".join(REFLECTION_LEARNING_OUTCOMES)
        raise ReflectionLearningContractError(
            f"learning outcome must be one of: {allowed}."
        )
    return cast(ReflectionLearningOutcome, value)


def _normalize_error_attributions(
    values: tuple[ReflectionErrorAttribution, ...],
) -> tuple[ReflectionErrorAttribution, ...]:
    if not isinstance(values, tuple):
        raise ReflectionLearningContractError(
            "error_attributions must be a tuple of attribution labels."
        )
    normalized: list[ReflectionErrorAttribution] = []
    seen: set[ReflectionErrorAttribution] = set()
    for value in values:
        label = _normalize_error_attribution(value)
        if label in seen:
            raise ReflectionLearningContractError(
                "error_attributions must not contain duplicates."
            )
        seen.add(label)
        normalized.append(label)
    return tuple(normalized)


def _normalize_error_attribution(value: str) -> ReflectionErrorAttribution:
    if value not in REFLECTION_ERROR_ATTRIBUTIONS:
        allowed = ", ".join(REFLECTION_ERROR_ATTRIBUTIONS)
        raise ReflectionLearningContractError(
            f"error_attributions must contain only: {allowed}."
        )
    return cast(ReflectionErrorAttribution, value)


def _normalize_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ReflectionLearningContractError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReflectionLearningContractError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ReflectionLearningContractError(f"{field_name} must be an ISO datetime.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReflectionLearningContractError(
            f"{field_name} must be an ISO datetime."
        ) from exc
    return _normalize_datetime(parsed, field_name)


def _validate_candidate_kind(value: str) -> ReflectionEpisodeMemoryCandidateKind:
    if value not in {"delta", "no_update"}:
        raise ReflectionLearningContractError(
            "candidate_kind must be 'delta' or 'no_update'."
        )
    return cast(ReflectionEpisodeMemoryCandidateKind, value)


def _require_datetime_field(payload: Mapping[str, object], field_name: str) -> datetime:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise ReflectionLearningContractError(
            f"{field_name} must be an ISO datetime."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReflectionLearningContractError(
            f"{field_name} must be an ISO datetime."
        ) from exc
    return _normalize_datetime(parsed, field_name)


def _require_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise ReflectionLearningContractError(f"{field_name} must be an array.")
    if not all(isinstance(item, str) for item in value):
        raise ReflectionLearningContractError(f"{field_name} must be an array of strings.")
    return value


def _optional_string_list(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReflectionLearningContractError(f"{field_name} must be an array when set.")
    if not all(isinstance(item, str) for item in value):
        raise ReflectionLearningContractError(f"{field_name} must be an array of strings.")
    return value


def _require_number(payload: Mapping[str, object], field_name: str) -> float:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReflectionLearningContractError(f"{field_name} must be a number.")
    return float(value)


def _normalize_id(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ReflectionLearningContractError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ReflectionLearningContractError(f"{field_name} must not be blank.")
    return normalized


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ReflectionLearningContractError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _optional_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReflectionLearningContractError(f"{field_name} must be a string when set.")
    normalized = value.strip()
    return normalized or None


def _require_mapping(
    payload: Mapping[str, object],
    field_name: str,
) -> Mapping[str, object]:
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        raise ReflectionLearningContractError(f"{field_name} must be a JSON object.")
    return dict(value)


def target_key_from_scope(scope_key: str) -> str:
    if not scope_key.startswith("target:"):
        raise ReflectionLearningContractError("scope_key must be target-scoped.")
    return validate_target_key(
        scope_key.partition(":")[2],
        error_type=ReflectionLearningContractError,
    )


__all__ = [
    "REFLECTION_ERROR_ATTRIBUTIONS",
    "REFLECTION_LEARNING_OUTCOMES",
    "ReflectionErrorAttribution",
    "ReflectionLearningAction",
    "ReflectionLearningActionReceipt",
    "ReflectionLearningContractError",
    "ReflectionLearningDecision",
    "ReflectionLearningOutcome",
    "ReflectionEpisodeMemoryCandidateKind",
    "ReflectionLearningReceipt",
    "parse_learning_decision_payload",
    "target_key_from_scope",
]
