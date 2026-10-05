"""Shared analysis direction-policy normalization helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    analysis_assessment_direction_policy_violations,
    coerce_role_if_already_short_for_execution_direction_mode,
    coerce_short_implication_md_for_execution_direction_mode,
    coerce_view_state_for_execution_direction_mode,
    validate_execution_direction_mode,
)
from event_trader.contracts.price_level_role import MaterialLevelRole, PriceLevelRole


class AnalysisDirectionPolicyNormalizationError(ValueError):
    """Raised when an analysis assessment cannot satisfy the direction policy."""


def normalize_analysis_assessment_for_execution_direction_mode(
    assessment: AnalysisAssessment,
    *,
    execution_direction_mode: ExecutionDirectionMode,
    error_type: type[Exception] = AnalysisDirectionPolicyNormalizationError,
) -> AnalysisAssessment:
    normalized_mode = validate_execution_direction_mode(
        execution_direction_mode,
        error_type=error_type,
    )
    repaired_levels: list[PriceLevelRole] = []
    levels_changed = False
    for level in assessment.price_level_roles:
        repaired_short_role = cast(
            MaterialLevelRole,
            coerce_role_if_already_short_for_execution_direction_mode(
                level.role_if_already_short,
                normalized_mode,
            ),
        )
        if repaired_short_role != level.role_if_already_short:
            repaired_levels.append(
                replace(level, role_if_already_short=repaired_short_role)
            )
            levels_changed = True
            continue
        repaired_levels.append(level)
    repaired_state = coerce_view_state_for_execution_direction_mode(
        assessment.as_if_flat_state,
        normalized_mode,
    )
    repaired_short_implication = coerce_short_implication_md_for_execution_direction_mode(
        assessment.if_already_short_implication_md,
        normalized_mode,
    )
    repaired = assessment
    if (
        levels_changed
        or repaired_state != assessment.as_if_flat_state
        or repaired_short_implication != assessment.if_already_short_implication_md
    ):
        repaired = replace(
            assessment,
            as_if_flat_state=repaired_state,
            if_already_short_implication_md=repaired_short_implication,
            price_level_roles=tuple(repaired_levels),
        )
    violations = analysis_assessment_direction_policy_violations(
        as_if_flat_state=repaired.as_if_flat_state,
        role_if_already_short_values=tuple(
            level.role_if_already_short for level in repaired.price_level_roles
        ),
        execution_direction_mode=normalized_mode,
        field_prefix="analysis_assessment",
    )
    if violations:
        raise error_type(violations[0].message)
    return repaired


__all__ = [
    "AnalysisDirectionPolicyNormalizationError",
    "normalize_analysis_assessment_for_execution_direction_mode",
]
