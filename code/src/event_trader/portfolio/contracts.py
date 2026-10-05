"""Contracts for deterministic PM decisions and portfolio state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from typing import cast

from event_trader.contracts._validators import (
    normalize_content,
    validate_event_id,
    validate_target_key,
    validate_timestamp,
)
from event_trader.contracts.view_state_change import (
    ViewState,
    canonical_view_state_target_weight,
)


class PortfolioContractError(ValueError):
    """Raised when portfolio contracts are malformed."""


@dataclass(frozen=True, slots=True)
class PMDecision:
    """Execution-facing PM decision derived deterministically from PM judgment."""

    decision_id: str
    decision_episode_id: str
    pm_review_request_id: str | None
    target_key: str
    business_at: datetime
    decision_available_at: datetime
    source_event_ids: tuple[str, ...]
    actual_state_before_decision: ViewState
    actual_target_weight_before_decision: float
    execution_required: bool
    requested_state: ViewState
    requested_target_weight: float
    rationale_md: str
    fallback_state_if_clamped: ViewState | None = None
    fallback_rationale_md: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _validate_id(self.decision_id, "decision_id"))
        object.__setattr__(
            self,
            "decision_episode_id",
            _validate_id(self.decision_episode_id, "decision_episode_id"),
        )
        object.__setattr__(
            self,
            "pm_review_request_id",
            _validate_optional_id(self.pm_review_request_id, "pm_review_request_id"),
        )
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PortfolioContractError),
        )
        object.__setattr__(
            self,
            "business_at",
            validate_timestamp(
                self.business_at,
                field_name="business_at",
                error_type=PortfolioContractError,
            ),
        )
        object.__setattr__(
            self,
            "decision_available_at",
            validate_timestamp(
                self.decision_available_at,
                field_name="decision_available_at",
                error_type=PortfolioContractError,
            ),
        )
        if self.decision_available_at < self.business_at:
            raise PortfolioContractError(
                "decision_available_at must be at or after business_at."
            )
        object.__setattr__(self, "source_event_ids", _validate_event_ids(self.source_event_ids))
        object.__setattr__(
            self,
            "actual_state_before_decision",
            _validate_view_state(self.actual_state_before_decision),
        )
        object.__setattr__(
            self,
            "actual_target_weight_before_decision",
            _validate_finite_float(
                self.actual_target_weight_before_decision,
                field_name="actual_target_weight_before_decision",
            ),
        )
        _validate_state_target_weight(
            state=self.actual_state_before_decision,
            target_weight=self.actual_target_weight_before_decision,
            state_field_name="actual_state_before_decision",
            weight_field_name="actual_target_weight_before_decision",
        )
        if not isinstance(self.execution_required, bool):
            raise PortfolioContractError("execution_required must be a boolean.")
        object.__setattr__(self, "requested_state", _validate_view_state(self.requested_state))
        object.__setattr__(
            self,
            "requested_target_weight",
            _validate_finite_float(
                self.requested_target_weight,
                field_name="requested_target_weight",
            ),
        )
        _validate_state_target_weight(
            state=self.requested_state,
            target_weight=self.requested_target_weight,
            state_field_name="requested_state",
            weight_field_name="requested_target_weight",
        )
        if self.execution_required != (
            self.requested_target_weight != self.actual_target_weight_before_decision
        ):
            raise PortfolioContractError(
                "execution_required must match the decision-time target-weight delta."
            )
        object.__setattr__(
            self,
            "rationale_md",
            normalize_content(
                self.rationale_md,
                field_name="rationale_md",
                error_type=PortfolioContractError,
            ),
        )
        fallback_state = _validate_optional_view_state(self.fallback_state_if_clamped)
        fallback_rationale = _validate_optional_content(
            self.fallback_rationale_md,
            field_name="fallback_rationale_md",
        )
        if (fallback_state is None) != (fallback_rationale is None):
            raise PortfolioContractError(
                "fallback_state_if_clamped and fallback_rationale_md must be set together."
            )
        object.__setattr__(self, "fallback_state_if_clamped", fallback_state)
        object.__setattr__(self, "fallback_rationale_md", fallback_rationale)

    def to_json_payload(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "decision_episode_id": self.decision_episode_id,
            "pm_review_request_id": self.pm_review_request_id,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "decision_available_at": self.decision_available_at.isoformat(),
            "source_event_ids": list(self.source_event_ids),
            "actual_state_before_decision": self.actual_state_before_decision,
            "actual_target_weight_before_decision": self.actual_target_weight_before_decision,
            "execution_required": self.execution_required,
            "requested_state": self.requested_state,
            "requested_target_weight": self.requested_target_weight,
            "rationale_md": self.rationale_md,
            "fallback_state_if_clamped": self.fallback_state_if_clamped,
            "fallback_rationale_md": self.fallback_rationale_md,
        }


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Current deterministic portfolio exposure state for one target."""

    target_key: str
    target_weight: float
    state: ViewState
    updated_at: datetime
    source_pm_decision_id: str
    source_execution_record_id: str
    decision_episode_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(self.target_key, error_type=PortfolioContractError),
        )
        object.__setattr__(
            self,
            "target_weight",
            _validate_finite_float(self.target_weight, field_name="target_weight"),
        )
        object.__setattr__(self, "state", _validate_view_state(self.state))
        _validate_state_target_weight(
            state=self.state,
            target_weight=self.target_weight,
            state_field_name="state",
            weight_field_name="target_weight",
        )
        object.__setattr__(
            self,
            "updated_at",
            validate_timestamp(
                self.updated_at,
                field_name="updated_at",
                error_type=PortfolioContractError,
            ),
        )
        object.__setattr__(
            self,
            "source_pm_decision_id",
            _validate_id(self.source_pm_decision_id, "source_pm_decision_id"),
        )
        object.__setattr__(
            self,
            "source_execution_record_id",
            _validate_id(self.source_execution_record_id, "source_execution_record_id"),
        )
        object.__setattr__(
            self,
            "decision_episode_id",
            _validate_id(self.decision_episode_id, "decision_episode_id"),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "target_weight": self.target_weight,
            "state": self.state,
            "updated_at": self.updated_at.isoformat(),
            "source_pm_decision_id": self.source_pm_decision_id,
            "source_execution_record_id": self.source_execution_record_id,
            "decision_episode_id": self.decision_episode_id,
        }


def parse_pm_decision(payload: Mapping[str, object]) -> PMDecision:
    return PMDecision(
        decision_id=_require_text(payload, "decision_id"),
        decision_episode_id=_require_text(payload, "decision_episode_id"),
        pm_review_request_id=_optional_text(payload.get("pm_review_request_id")),
        target_key=_require_text(payload, "target_key"),
        business_at=_parse_timestamp(payload.get("business_at"), "business_at"),
        decision_available_at=_parse_timestamp(
            payload.get("decision_available_at", payload.get("business_at")),
            "decision_available_at",
        ),
        source_event_ids=tuple(_require_string_list(payload, "source_event_ids")),
        actual_state_before_decision=cast(
            ViewState, _require_text(payload, "actual_state_before_decision")
        ),
        actual_target_weight_before_decision=_require_float(
            payload,
            "actual_target_weight_before_decision",
        ),
        execution_required=_require_bool(payload, "execution_required"),
        requested_state=cast(ViewState, _require_text(payload, "requested_state")),
        requested_target_weight=_require_float(payload, "requested_target_weight"),
        rationale_md=_require_text(payload, "rationale_md"),
        fallback_state_if_clamped=_optional_view_state(payload.get("fallback_state_if_clamped")),
        fallback_rationale_md=_optional_text(payload.get("fallback_rationale_md")),
    )


def parse_portfolio_state(payload: Mapping[str, object]) -> PortfolioState:
    return PortfolioState(
        target_key=_require_text(payload, "target_key"),
        target_weight=_require_float(payload, "target_weight"),
        state=cast(ViewState, _require_text(payload, "state")),
        updated_at=_parse_timestamp(payload.get("updated_at"), "updated_at"),
        source_pm_decision_id=_require_text(payload, "source_pm_decision_id"),
        source_execution_record_id=_require_text(payload, "source_execution_record_id"),
        decision_episode_id=_require_text(payload, "decision_episode_id"),
    )


def portfolio_contract_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _validate_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortfolioContractError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _validate_optional_id(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_id(value, field_name)


def _validate_event_ids(value: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise PortfolioContractError("source_event_ids must be a tuple.")
    normalized: list[str] = []
    seen: set[str] = set()
    for event_id in value:
        validated = validate_event_id(event_id, error_type=PortfolioContractError)
        if validated in seen:
            raise PortfolioContractError("source_event_ids must not contain duplicates.")
        seen.add(validated)
        normalized.append(validated)
    if not normalized:
        raise PortfolioContractError("source_event_ids must not be empty.")
    return tuple(normalized)


def _validate_view_state(value: object) -> ViewState:
    if value not in {"flat", "weak_long", "strong_long", "weak_short", "strong_short"}:
        raise PortfolioContractError("state must be one of the allowed view states.")
    return cast(ViewState, value)


def _validate_optional_view_state(value: object) -> ViewState | None:
    if value is None:
        return None
    return _validate_view_state(value)


def _optional_view_state(value: object) -> ViewState | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PortfolioContractError("optional view-state fields must be strings when set.")
    return _validate_view_state(value)


def _validate_state_target_weight(
    *,
    state: ViewState,
    target_weight: float,
    state_field_name: str,
    weight_field_name: str,
) -> None:
    expected_weight = canonical_view_state_target_weight(state)
    if target_weight != expected_weight:
        raise PortfolioContractError(
            f"{weight_field_name} must match canonical weight for {state_field_name}."
        )


def _validate_finite_float(value: object, *, field_name: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise PortfolioContractError(f"{field_name} must be numeric.")
    normalized = float(value)
    if not isfinite(normalized):
        raise PortfolioContractError(f"{field_name} must be finite.")
    return normalized


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    return _validate_id(payload.get(field_name), field_name)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PortfolioContractError("optional text fields must be strings when set.")
    return value


def _validate_optional_content(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PortfolioContractError(f"{field_name} must be a string when set.")
    return normalize_content(
        value,
        field_name=field_name,
        error_type=PortfolioContractError,
    )


def _require_float(payload: Mapping[str, object], field_name: str) -> float:
    return _validate_finite_float(payload.get(field_name), field_name=field_name)


def _require_bool(payload: Mapping[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise PortfolioContractError(f"{field_name} must be a boolean.")
    return value


def _require_string_list(payload: Mapping[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PortfolioContractError(f"{field_name} must be a list of strings.")
    return value


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise PortfolioContractError(f"{field_name} must be an ISO timestamp string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PortfolioContractError(f"{field_name} must be an ISO timestamp string.") from exc
    return validate_timestamp(
        parsed,
        field_name=field_name,
        error_type=PortfolioContractError,
    )


__all__ = [
    "PMDecision",
    "PortfolioContractError",
    "PortfolioState",
    "parse_pm_decision",
    "parse_portfolio_state",
    "portfolio_contract_hash",
]
