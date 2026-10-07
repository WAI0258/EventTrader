"""Deterministic canonicalization for analysis-owned price-basis labels."""

from __future__ import annotations

from dataclasses import replace

from event_trader.contracts import instrument_basis as contract_instrument_basis
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.analysis_price_semantics import AnalysisPriceSemantics
from event_trader.contracts.price_level_role import (
    PriceLevelRole,
    is_semantically_active_price_level,
)


def canonicalize_instrument_basis(instrument_basis: str) -> str:
    return contract_instrument_basis.canonicalize_instrument_basis(instrument_basis)


def is_proxy_instrument_basis(instrument_basis: str) -> bool:
    return contract_instrument_basis.is_proxy_instrument_basis(instrument_basis)


def canonicalize_price_level_role(level: PriceLevelRole) -> PriceLevelRole:
    canonical_basis = canonicalize_instrument_basis(level.instrument_basis)
    if canonical_basis == level.instrument_basis:
        return level
    return replace(level, instrument_basis=canonical_basis)


def canonicalize_analysis_price_semantics(
    semantics: AnalysisPriceSemantics | None,
) -> AnalysisPriceSemantics | None:
    if semantics is None:
        return None
    canonical_basis = canonicalize_instrument_basis(semantics.instrument_basis)
    if canonical_basis == semantics.instrument_basis:
        return semantics
    return replace(semantics, instrument_basis=canonical_basis)


def canonicalize_analysis_assessment_price_bases(
    assessment: AnalysisAssessment | None,
) -> AnalysisAssessment | None:
    if assessment is None:
        return None
    canonical_levels = tuple(
        canonicalize_price_level_role(level) for level in assessment.price_level_roles
    )
    canonical_semantics = canonicalize_analysis_price_semantics(
        assessment.analysis_price_semantics
    )
    if (
        canonical_levels == assessment.price_level_roles
        and canonical_semantics == assessment.analysis_price_semantics
    ):
        return assessment
    return replace(
        assessment,
        price_level_roles=canonical_levels,
        analysis_price_semantics=canonical_semantics,
    )


def narrowed_active_price_levels(
    levels: tuple[PriceLevelRole, ...],
    *,
    instrument_basis: str,
) -> tuple[PriceLevelRole, ...]:
    canonical_basis = canonicalize_instrument_basis(instrument_basis)
    return tuple(
        canonicalize_price_level_role(level)
        for level in levels
        if is_semantically_active_price_level(level)
        and canonicalize_instrument_basis(level.instrument_basis) == canonical_basis
    )


__all__ = [
    "canonicalize_analysis_assessment_price_bases",
    "canonicalize_analysis_price_semantics",
    "canonicalize_instrument_basis",
    "canonicalize_price_level_role",
    "is_proxy_instrument_basis",
    "narrowed_active_price_levels",
]
