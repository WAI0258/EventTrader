"""Shared execution-direction policy helpers for analysis and PM boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from event_trader.contracts.view_state_change import ViewState

ExecutionDirectionMode = Literal["long_only", "long_short"]

_EXECUTION_DIRECTION_MODES = ("long_only", "long_short")
_LONG_ONLY_VIEW_STATES: tuple[ViewState, ...] = (
    "flat",
    "weak_long",
    "strong_long",
)
_LONG_SHORT_VIEW_STATES: tuple[ViewState, ...] = (
    "flat",
    "weak_long",
    "strong_long",
    "weak_short",
    "strong_short",
)
_LONG_ONLY_ROLE_IF_ALREADY_SHORT = ("not_relevant",)


@dataclass(frozen=True, slots=True)
class DirectionPolicyViolation:
    field_path: str
    message: str


def validate_execution_direction_mode(
    value: object,
    *,
    field_name: str = "execution_direction_mode",
    error_type: type[Exception] = ValueError,
) -> ExecutionDirectionMode:
    if value not in _EXECUTION_DIRECTION_MODES:
        raise error_type(f"{field_name} must be long_only or long_short.")
    return cast(ExecutionDirectionMode, value)


def supported_analysis_view_states() -> tuple[ViewState, ...]:
    return _LONG_SHORT_VIEW_STATES


def allowed_view_states_for_execution_direction_mode(
    execution_direction_mode: ExecutionDirectionMode,
) -> tuple[ViewState, ...]:
    normalized = validate_execution_direction_mode(execution_direction_mode)
    if normalized == "long_only":
        return _LONG_ONLY_VIEW_STATES
    return _LONG_SHORT_VIEW_STATES


def allowed_role_if_already_short_for_execution_direction_mode(
    execution_direction_mode: ExecutionDirectionMode,
) -> tuple[str, ...] | None:
    normalized = validate_execution_direction_mode(execution_direction_mode)
    if normalized == "long_only":
        return _LONG_ONLY_ROLE_IF_ALREADY_SHORT
    return None


def analysis_assessment_direction_policy_violations(
    *,
    as_if_flat_state: str,
    role_if_already_short_values: Sequence[str],
    execution_direction_mode: ExecutionDirectionMode,
    field_prefix: str,
) -> tuple[DirectionPolicyViolation, ...]:
    normalized = validate_execution_direction_mode(execution_direction_mode)
    allowed_states = allowed_view_states_for_execution_direction_mode(normalized)
    violations: list[DirectionPolicyViolation] = []
    if as_if_flat_state not in allowed_states:
        violations.append(
            DirectionPolicyViolation(
                field_path=f"{field_prefix}.as_if_flat_state",
                message=(
                    f"{field_prefix}.as_if_flat_state must be one of "
                    f"{', '.join(allowed_states)} for execution_direction_mode "
                    f"{normalized!r}."
                ),
            )
        )
    allowed_short_roles = allowed_role_if_already_short_for_execution_direction_mode(
        normalized
    )
    if allowed_short_roles is None:
        return tuple(violations)
    for index, role in enumerate(role_if_already_short_values):
        if role in allowed_short_roles:
            continue
        violations.append(
            DirectionPolicyViolation(
                field_path=(
                    f"{field_prefix}.price_level_roles[{index}].role_if_already_short"
                ),
                message=(
                    f"{field_prefix}.price_level_roles[{index}].role_if_already_short "
                    "must be not_relevant for execution_direction_mode "
                    f"{normalized!r}."
                ),
            )
        )
    return tuple(violations)


def coerce_view_state_for_execution_direction_mode(
    view_state: ViewState,
    execution_direction_mode: ExecutionDirectionMode,
) -> ViewState:
    normalized = validate_execution_direction_mode(execution_direction_mode)
    if normalized == "long_only" and view_state in {"weak_short", "strong_short"}:
        return "flat"
    return view_state


def coerce_role_if_already_short_for_execution_direction_mode(
    role_if_already_short: str,
    execution_direction_mode: ExecutionDirectionMode,
) -> str:
    normalized = validate_execution_direction_mode(execution_direction_mode)
    if normalized == "long_only":
        return "not_relevant"
    return role_if_already_short


def coerce_short_implication_md_for_execution_direction_mode(
    value: str | None,
    execution_direction_mode: ExecutionDirectionMode,
) -> str | None:
    normalized = validate_execution_direction_mode(execution_direction_mode)
    if normalized == "long_only":
        return None
    return value


__all__ = [
    "DirectionPolicyViolation",
    "ExecutionDirectionMode",
    "allowed_role_if_already_short_for_execution_direction_mode",
    "allowed_view_states_for_execution_direction_mode",
    "analysis_assessment_direction_policy_violations",
    "coerce_role_if_already_short_for_execution_direction_mode",
    "coerce_short_implication_md_for_execution_direction_mode",
    "coerce_view_state_for_execution_direction_mode",
    "supported_analysis_view_states",
    "validate_execution_direction_mode",
]
