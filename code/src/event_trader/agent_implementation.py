"""Closed implementation identifiers for each production agent role."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, Literal

type AnalysisAgentImplementation = Literal["mirothinker", "event_trader"]
type CheckerAgentImplementation = Literal["single_pass"]
type PMReviewAgentImplementation = Literal["mirothinker", "event_trader"]
type ReflectionAgentImplementation = Literal["mirothinker", "event_trader"]
type SearchAgentImplementation = Literal["mirothinker"]

MIROTHINKER_IMPLEMENTATION: Final = "mirothinker"
EVENT_TRADER_IMPLEMENTATION: Final = "event_trader"
SINGLE_PASS_IMPLEMENTATION: Final = "single_pass"


def _normalize_implementation[ImplementationT: str](
    value: object,
    *,
    field_name: str,
    allowed: tuple[ImplementationT, ...],
    error_type: Callable[[str], Exception],
) -> ImplementationT:
    if not isinstance(value, str):
        raise error_type(f"{field_name} must be a string.")
    normalized = value.strip().lower()
    if not normalized:
        raise error_type(f"{field_name} must not be blank.")
    if normalized not in allowed:
        choices = ", ".join(allowed)
        raise error_type(f"{field_name} must select an available implementation: {choices}.")
    return normalized


def normalize_analysis_implementation(
    value: object,
    *,
    field_name: str,
    error_type: Callable[[str], Exception] = ValueError,
) -> AnalysisAgentImplementation:
    return _normalize_implementation(
        value,
        field_name=field_name,
        allowed=(MIROTHINKER_IMPLEMENTATION, EVENT_TRADER_IMPLEMENTATION),
        error_type=error_type,
    )


def normalize_checker_implementation(
    value: object,
    *,
    field_name: str,
    error_type: Callable[[str], Exception] = ValueError,
) -> CheckerAgentImplementation:
    return _normalize_implementation(
        value,
        field_name=field_name,
        allowed=(SINGLE_PASS_IMPLEMENTATION,),
        error_type=error_type,
    )


def normalize_pm_review_implementation(
    value: object,
    *,
    field_name: str,
    error_type: Callable[[str], Exception] = ValueError,
) -> PMReviewAgentImplementation:
    return _normalize_implementation(
        value,
        field_name=field_name,
        allowed=(MIROTHINKER_IMPLEMENTATION, EVENT_TRADER_IMPLEMENTATION),
        error_type=error_type,
    )


def normalize_reflection_implementation(
    value: object,
    *,
    field_name: str,
    error_type: Callable[[str], Exception] = ValueError,
) -> ReflectionAgentImplementation:
    return _normalize_implementation(
        value,
        field_name=field_name,
        allowed=(MIROTHINKER_IMPLEMENTATION, EVENT_TRADER_IMPLEMENTATION),
        error_type=error_type,
    )


def normalize_search_implementation(
    value: object,
    *,
    field_name: str,
    error_type: Callable[[str], Exception] = ValueError,
) -> SearchAgentImplementation:
    return _normalize_implementation(
        value,
        field_name=field_name,
        allowed=(MIROTHINKER_IMPLEMENTATION,),
        error_type=error_type,
    )


__all__ = [
    "AnalysisAgentImplementation",
    "CheckerAgentImplementation",
    "EVENT_TRADER_IMPLEMENTATION",
    "MIROTHINKER_IMPLEMENTATION",
    "PMReviewAgentImplementation",
    "ReflectionAgentImplementation",
    "SINGLE_PASS_IMPLEMENTATION",
    "SearchAgentImplementation",
    "normalize_analysis_implementation",
    "normalize_checker_implementation",
    "normalize_pm_review_implementation",
    "normalize_reflection_implementation",
    "normalize_search_implementation",
]
