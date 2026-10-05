"""Deterministic active-price-basis rebasing for analysis-owned active truth."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.analysis_price_semantics import AnalysisPriceSemantics
from event_trader.contracts.price_level_role import PriceLevelRole
from event_trader.research_memory.analysis_price_basis import (
    canonicalize_analysis_assessment_price_bases,
    canonicalize_analysis_price_semantics,
    canonicalize_instrument_basis,
    canonicalize_price_level_role,
)


class AnalysisPriceBasisRebaseError(ValueError):
    """Raised when active analysis truth cannot be rebased deterministically."""


def bootstrap_analysis_price_semantics_for_cutover(
    assessment: AnalysisAssessment,
    *,
    analysis_reference_price: float,
    analysis_reference_at: datetime,
) -> AnalysisPriceSemantics:
    canonical_assessment = canonicalize_analysis_assessment_price_bases(assessment)
    assert canonical_assessment is not None
    active_levels = _active_levels(canonical_assessment.price_level_roles)
    if not active_levels:
        raise AnalysisPriceBasisRebaseError(
            "analysis_price_semantics bootstrap requires at least one active price level."
        )
    bootstrap_levels = _dominant_basis_levels(active_levels)
    if not bootstrap_levels:
        raise AnalysisPriceBasisRebaseError(
            "analysis_price_semantics bootstrap requires at least one active target-price "
            "level after dominant instrument_basis selection."
        )
    instrument_basis = bootstrap_levels[0].instrument_basis
    return AnalysisPriceSemantics(
        target_key=assessment.target_key,
        instrument_basis=instrument_basis,
        analysis_reference_price=analysis_reference_price,
        analysis_reference_at=analysis_reference_at,
        current_leg_start_price=None,
        current_leg_end_price=None,
        swing_high=None,
        swing_low=None,
        window_high=None,
        window_low=None,
        active_price_level_ids=tuple(level.level_id for level in bootstrap_levels),
    )


def rebase_analysis_price_semantics(
    semantics: AnalysisPriceSemantics,
    *,
    ratio: float,
) -> AnalysisPriceSemantics:
    _validate_ratio(ratio)
    return canonicalize_analysis_price_semantics(
        replace(
            semantics,
            analysis_reference_price=semantics.analysis_reference_price * ratio,
            current_leg_start_price=_scaled_optional(
                semantics.current_leg_start_price, ratio
            ),
            current_leg_end_price=_scaled_optional(semantics.current_leg_end_price, ratio),
            swing_high=_scaled_optional(semantics.swing_high, ratio),
            swing_low=_scaled_optional(semantics.swing_low, ratio),
            window_high=_scaled_optional(semantics.window_high, ratio),
            window_low=_scaled_optional(semantics.window_low, ratio),
        )
    )


def rebase_price_level_role(
    level: PriceLevelRole,
    *,
    ratio: float,
) -> PriceLevelRole:
    _validate_ratio(ratio)
    return canonicalize_price_level_role(
        replace(
            level,
            value=_scaled_optional(level.value, ratio),
            lower=_scaled_optional(level.lower, ratio),
            upper=_scaled_optional(level.upper, ratio),
        )
    )


def rebase_analysis_assessment(
    assessment: AnalysisAssessment,
    *,
    ratio: float,
    successor_assessment_id: str,
    successor_business_at: datetime,
    successor_market_setup_dashboard_md: str | None = None,
    successor_analysis_price_semantics: AnalysisPriceSemantics | None = None,
    successor_memory_write_receipt_ids: tuple[str, ...] | None = None,
) -> AnalysisAssessment:
    _validate_ratio(ratio)
    rebased_levels = tuple(
        rebase_price_level_role(level, ratio=ratio) for level in assessment.price_level_roles
    )
    rebased_semantics = successor_analysis_price_semantics
    if rebased_semantics is None and assessment.analysis_price_semantics is not None:
        rebased_semantics = rebase_analysis_price_semantics(
            assessment.analysis_price_semantics,
            ratio=ratio,
        )
    return canonicalize_analysis_assessment_price_bases(
        replace(
            assessment,
            assessment_id=successor_assessment_id,
            business_at=successor_business_at,
            price_level_roles=rebased_levels,
            analysis_price_semantics=rebased_semantics,
            market_setup_dashboard_md=(
                assessment.market_setup_dashboard_md
                if successor_market_setup_dashboard_md is None
                else successor_market_setup_dashboard_md
            ),
            memory_write_receipt_ids=(
                assessment.memory_write_receipt_ids
                if successor_memory_write_receipt_ids is None
                else successor_memory_write_receipt_ids
            ),
        )
    )


def rescale_analysis_assessment(
    assessment: AnalysisAssessment,
    *,
    ratio: float,
    analysis_price_semantics: AnalysisPriceSemantics | None = None,
) -> AnalysisAssessment:
    _validate_ratio(ratio)
    rebased_levels = tuple(
        rebase_price_level_role(level, ratio=ratio) for level in assessment.price_level_roles
    )
    rebased_semantics = analysis_price_semantics
    if rebased_semantics is None and assessment.analysis_price_semantics is not None:
        rebased_semantics = rebase_analysis_price_semantics(
            assessment.analysis_price_semantics,
            ratio=ratio,
        )
    return canonicalize_analysis_assessment_price_bases(
        replace(
            assessment,
            price_level_roles=rebased_levels,
            analysis_price_semantics=rebased_semantics,
        )
    )


def _active_levels(
    levels: tuple[PriceLevelRole, ...],
) -> tuple[PriceLevelRole, ...]:
    return tuple(
        level
        for level in levels
        if not (
            level.role_if_flat == "not_relevant"
            and level.role_if_already_long == "not_relevant"
            and level.role_if_already_short == "not_relevant"
        )
    )


def _dominant_basis_levels(
    active_levels: tuple[PriceLevelRole, ...],
) -> tuple[PriceLevelRole, ...]:
    counts_by_basis: dict[str, int] = {}
    for level in active_levels:
        canonical_basis = canonicalize_instrument_basis(level.instrument_basis)
        counts_by_basis[canonical_basis] = (
            counts_by_basis.get(canonical_basis, 0) + 1
        )
    dominant_count = max(counts_by_basis.values())
    dominant_bases = tuple(
        basis
        for basis, count in counts_by_basis.items()
        if count == dominant_count
    )
    if len(dominant_bases) != 1:
        raise AnalysisPriceBasisRebaseError(
            "analysis_price_semantics bootstrap requires one unique dominant "
            "instrument_basis across active price levels; found counts "
            f"{dict(sorted(counts_by_basis.items()))!r}."
        )
    dominant_basis = dominant_bases[0]
    return tuple(
        canonicalize_price_level_role(level)
        for level in active_levels
        if canonicalize_instrument_basis(level.instrument_basis) == dominant_basis
    )


def _scaled_optional(value: float | None, ratio: float) -> float | None:
    if value is None:
        return None
    return value * ratio


def _validate_ratio(ratio: float) -> None:
    if ratio <= 0.0:
        raise AnalysisPriceBasisRebaseError("rebase ratio must be positive.")


__all__ = [
    "AnalysisPriceBasisRebaseError",
    "bootstrap_analysis_price_semantics_for_cutover",
    "rebase_analysis_assessment",
    "rebase_analysis_price_semantics",
    "rebase_price_level_role",
    "rescale_analysis_assessment",
]
