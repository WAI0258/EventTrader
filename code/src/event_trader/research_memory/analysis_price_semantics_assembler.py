"""Deterministic runtime assembly for analysis price semantics."""

from __future__ import annotations

from dataclasses import replace

from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.analysis_price_semantics import AnalysisPriceSemantics
from event_trader.market.contracts import CausalPivot, MarketContextSnapshot
from event_trader.research_memory.analysis_price_basis import (
    canonicalize_price_level_role,
    is_proxy_instrument_basis,
    is_semantically_active_price_level,
    canonicalize_analysis_assessment_price_bases,
    narrowed_active_price_levels,
)


class AnalysisPriceSemanticsAssemblyError(ValueError):
    """Raised when runtime market facts cannot deterministically assemble semantics."""


def enrich_assessment_with_analysis_price_semantics(
    assessment: AnalysisAssessment | None,
    *,
    market_context: MarketContextSnapshot | None,
) -> AnalysisAssessment | None:
    assessment = canonicalize_analysis_assessment_price_bases(assessment)
    if assessment is None:
        return None
    if market_context is None or market_context.market_path is None:
        return assessment

    range_path = market_context.market_path.range_path
    if range_path.latest_price is None:
        return assessment

    instrument_basis = _instrument_basis_for_semantics(assessment=assessment)
    if instrument_basis is None:
        return assessment
    active_levels = narrowed_active_price_levels(
        assessment.price_level_roles,
        instrument_basis=instrument_basis,
    )

    current_leg = market_context.market_path.swing_path.current_leg
    semantics = AnalysisPriceSemantics(
        target_key=assessment.target_key,
        instrument_basis=instrument_basis,
        analysis_reference_price=range_path.latest_price,
        analysis_reference_at=market_context.as_of_at,
        current_leg_start_price=(
            None if current_leg is None else current_leg.from_pivot_price
        ),
        current_leg_end_price=None if current_leg is None else current_leg.latest_price,
        swing_high=_latest_confirmed_pivot_price(
            market_context.market_path.swing_path.confirmed_pivots,
            kind="swing_high",
        ),
        swing_low=_latest_confirmed_pivot_price(
            market_context.market_path.swing_path.confirmed_pivots,
            kind="swing_low",
        ),
        window_high=range_path.window_high,
        window_low=range_path.window_low,
        active_price_level_ids=tuple(level.level_id for level in active_levels),
    )
    return _replace_analysis_price_semantics(
        assessment,
        analysis_price_semantics=semantics,
    )


def _instrument_basis_for_semantics(
    *,
    assessment: AnalysisAssessment,
) -> str | None:
    canonical_levels = tuple(
        canonicalize_price_level_role(level) for level in assessment.price_level_roles
    )
    if not canonical_levels:
        return None
    active_non_proxy_bases = sorted(
        {
            level.instrument_basis
            for level in canonical_levels
            if is_semantically_active_price_level(level)
            and not is_proxy_instrument_basis(level.instrument_basis)
        }
    )
    if len(active_non_proxy_bases) > 1:
        raise AnalysisPriceSemanticsAssemblyError(
            "analysis_price_semantics requires one unique non-proxy instrument_basis "
            "across semantically active price levels; found "
            f"{active_non_proxy_bases!r}."
        )
    if len(active_non_proxy_bases) == 1:
        return active_non_proxy_bases[0]
    all_non_proxy_bases = sorted(
        {
            level.instrument_basis
            for level in canonical_levels
            if not is_proxy_instrument_basis(level.instrument_basis)
        }
    )
    if not all_non_proxy_bases:
        return None
    if len(all_non_proxy_bases) == 1:
        return all_non_proxy_bases[0]
    raise AnalysisPriceSemanticsAssemblyError(
        "analysis_price_semantics requires one unique non-proxy instrument_basis "
        "across price_level_roles when no semantically active target basis is "
        f"available; found {all_non_proxy_bases!r}."
    )


def _latest_confirmed_pivot_price(
    pivots: tuple[CausalPivot, ...],
    *,
    kind: str,
) -> float | None:
    matching = tuple(pivot for pivot in pivots if pivot.kind == kind)
    if not matching:
        return None
    latest = max(
        matching,
        key=lambda pivot: (pivot.confirmed_at, pivot.pivot_at),
    )
    return latest.pivot_price


def _replace_analysis_price_semantics(
    assessment: AnalysisAssessment,
    *,
    analysis_price_semantics: AnalysisPriceSemantics | None,
) -> AnalysisAssessment:
    if assessment.analysis_price_semantics == analysis_price_semantics:
        return assessment
    return replace(
        assessment,
        analysis_price_semantics=analysis_price_semantics,
    )


__all__ = [
    "AnalysisPriceSemanticsAssemblyError",
    "enrich_assessment_with_analysis_price_semantics",
]
