"""Shared PM-review reason vocabulary.

This module is intentionally neutral: Analysis can request PM review for market
reasons, and PMReview can carry operational reasons, without either package
owning the enum.
"""

from __future__ import annotations

from typing import Literal, cast

PMReviewReason = Literal[
    "none",
    "material_setup",
    "flat_candidate_setup",
    "current_exposure_pressure",
    "watch_trigger_touched",
    "invalidation_touched",
    "material_level_used",
    "level_role_conflict",
    "risk_reward_compression",
    "risk_monitor_trigger",
    "operator_trigger",
]

PM_REVIEW_REASON_VALUES: frozenset[str] = frozenset(
    {
        "none",
        "material_setup",
        "flat_candidate_setup",
        "current_exposure_pressure",
        "watch_trigger_touched",
        "invalidation_touched",
        "material_level_used",
        "level_role_conflict",
        "risk_reward_compression",
        "risk_monitor_trigger",
        "operator_trigger",
    }
)

ANALYSIS_PM_ESCALATION_REASONS = cast(
    tuple[PMReviewReason, ...],
    (
        "flat_candidate_setup",
        "material_setup",
        "current_exposure_pressure",
        "invalidation_touched",
        "risk_reward_compression",
    ),
)
ANALYSIS_PM_ANNOTATION_ONLY_REASONS = cast(
    tuple[PMReviewReason, ...],
    (
        "material_level_used",
        "level_role_conflict",
        "watch_trigger_touched",
    ),
)


def validate_pm_review_reasons(
    values: tuple[str, ...],
    *,
    field_name: str,
    allow_none: bool,
    allow_empty: bool,
    error_type: type[Exception],
) -> tuple[PMReviewReason, ...]:
    normalized = tuple(values)
    if not normalized and not allow_empty:
        raise error_type(f"{field_name} must not be empty.")
    seen: set[str] = set()
    for value in normalized:
        if not isinstance(value, str):
            raise error_type(f"{field_name} must contain only strings.")
        if value not in PM_REVIEW_REASON_VALUES:
            raise error_type(f"{field_name} contains unsupported PM review reason.")
        if value == "none" and not allow_none:
            raise error_type(f"{field_name} must not contain 'none'.")
        if value in seen:
            raise error_type(f"{field_name} must not contain duplicate reasons.")
        seen.add(value)
    if "none" in seen and len(seen) != 1:
        raise error_type(f"{field_name} cannot combine 'none' with review reasons.")
    return cast(tuple[PMReviewReason, ...], normalized)


def analysis_pm_escalation_reasons() -> tuple[PMReviewReason, ...]:
    return ANALYSIS_PM_ESCALATION_REASONS


def analysis_pm_annotation_only_reasons() -> tuple[PMReviewReason, ...]:
    return ANALYSIS_PM_ANNOTATION_ONLY_REASONS


__all__ = [
    "ANALYSIS_PM_ANNOTATION_ONLY_REASONS",
    "ANALYSIS_PM_ESCALATION_REASONS",
    "PMReviewReason",
    "PM_REVIEW_REASON_VALUES",
    "analysis_pm_annotation_only_reasons",
    "analysis_pm_escalation_reasons",
    "validate_pm_review_reasons",
]
