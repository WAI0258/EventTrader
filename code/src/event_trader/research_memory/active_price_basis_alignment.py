"""Deterministic alignment between runtime active-price truth and persisted assessments."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime

from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.config import (
    MarketContextConfig,
    ValidationConfig,
    ValidationMarketMappingConfig,
)
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.analysis_price_semantics import AnalysisPriceSemantics
from event_trader.market.adjustments import load_adjustment_sidecar_from_archive_root
from event_trader.market.contracts import MarketContextSnapshot
from event_trader.research_memory.active_price_projection import (
    active_price_levels,
    projected_market_setup_dashboard_md,
)
from event_trader.research_memory.analysis_price_basis import (
    canonicalize_analysis_assessment_price_bases,
    canonicalize_instrument_basis,
    is_proxy_instrument_basis,
    narrowed_active_price_levels,
)
from event_trader.research_memory.analysis_price_basis_rebase import (
    rescale_analysis_assessment,
)
from event_trader.research_memory.analysis_price_semantics_assembler import (
    AnalysisPriceSemanticsAssemblyError,
    enrich_assessment_with_analysis_price_semantics,
)
from event_trader.storage import WorkspaceLayout

_RAW_POLICY = "raw"
_LEVEL_DATE_PATTERN = re.compile(r"(?P<yyyymmdd>\d{8})$")


class ActivePriceBasisAlignmentError(ValueError):
    """Raised when runtime active-price truth cannot be aligned deterministically."""


def align_analysis_assessment_to_active_price_basis(
    assessment: AnalysisAssessment | None,
    *,
    layout: WorkspaceLayout,
    validation_config: ValidationConfig | None,
    market_context_config: MarketContextConfig | None,
    market_context: MarketContextSnapshot | None,
) -> AnalysisAssessment | None:
    original_assessment = assessment
    try:
        assessment = enrich_assessment_with_analysis_price_semantics(
            assessment,
            market_context=market_context,
        )
    except AnalysisPriceSemanticsAssemblyError as exc:
        assessment = _fallback_alignment_assessment(
            assessment=assessment,
            layout=layout,
            market_context=market_context,
        )
        if assessment is None:
            raise ActivePriceBasisAlignmentError(str(exc)) from exc
    if assessment is not None and assessment.analysis_price_semantics is None:
        fallback_assessment = _fallback_alignment_assessment(
            assessment=assessment,
            layout=layout,
            market_context=market_context,
        )
        if fallback_assessment is not None:
            assessment = fallback_assessment
    if assessment is None:
        return None
    if validation_config is None:
        return _finalize_effective_active_setup(
            layout=layout,
            assessment=assessment,
        )
    from event_trader.migrations.active_price_basis_cutover import (
        read_active_price_basis_pointer,
    )

    pointer = read_active_price_basis_pointer(
        layout=layout,
        target_key=assessment.target_key,
    )
    if pointer is None:
        return _finalize_effective_active_setup(
            layout=layout,
            assessment=assessment,
        )
    if not assessment.price_level_roles and assessment.analysis_price_semantics is None:
        return _finalize_effective_active_setup(
            layout=layout,
            assessment=assessment,
        )
    if assessment.analysis_price_semantics is None:
        raise ActivePriceBasisAlignmentError(
            "active price basis pointer exists but analysis_price_semantics could not "
            "be assembled deterministically."
        )
    market_mapping = validation_config.market_mappings.get(assessment.target_key)
    if market_mapping is None:
        raise ActivePriceBasisAlignmentError(
            "active price basis pointer exists but validation.market_mappings is "
            f"missing target {assessment.target_key!r}."
        )
    if (
        original_assessment is not None
        and original_assessment.analysis_price_semantics is not None
    ):
        ratio = _ratio_from_original_semantics(
            original_assessment=original_assessment,
            aligned_assessment=assessment,
        )
        if ratio is not None and abs(ratio - 1.0) > 1e-12:
            assessment = _replace_semantics(
                rescale_analysis_assessment(
                    original_assessment,
                    ratio=ratio,
                ),
                semantics=assessment.analysis_price_semantics,
            )
    else:
        previous_assessment = _latest_persisted_assessment(
            layout=layout,
            target_key=assessment.target_key,
            level_ids=tuple(level.level_id for level in assessment.price_level_roles),
        )
        overlap_ratio = _infer_overlap_ratio(
            assessment=assessment,
            previous_assessment=previous_assessment,
        )
        if overlap_ratio is not None and abs(overlap_ratio - 1.0) > 1e-12:
            assessment = rescale_analysis_assessment(
                assessment,
                ratio=overlap_ratio,
            )
        else:
            source_policy = market_context_adjustment_policy_for_target(
                market_context_config=market_context_config,
                target_key=assessment.target_key,
                market_symbol=market_mapping.market_symbol,
            )
            level_date_ratio = infer_level_date_rebase_ratio(
                assessment=assessment,
                market_mapping=market_mapping,
                local_archive_root=validation_config.market_data.local_archive_root,
                source_policy=source_policy,
                target_policy=pointer.policy,
                target_reference_at=pointer.anchor_at,
            )
            if level_date_ratio is not None and abs(level_date_ratio - 1.0) > 1e-12:
                assessment = rescale_analysis_assessment(
                    assessment,
                    ratio=level_date_ratio,
                )
            else:
                ratio = active_price_basis_projection_ratio(
                    market_mapping=market_mapping,
                    local_archive_root=validation_config.market_data.local_archive_root,
                    source_policy=source_policy,
                    source_reference_at=_analysis_reference_at(
                        assessment=assessment,
                        market_context=market_context,
                    ),
                    target_policy=pointer.policy,
                    target_reference_at=pointer.anchor_at,
                )
                if abs(ratio - 1.0) > 1e-12:
                    assessment = rescale_analysis_assessment(
                        assessment,
                        ratio=ratio,
                    )
    return _finalize_effective_active_setup(
        layout=layout,
        assessment=assessment,
    )


def market_context_adjustment_policy_for_target(
    *,
    market_context_config: MarketContextConfig | None,
    target_key: str,
    market_symbol: str,
) -> str:
    if market_context_config is None:
        return _RAW_POLICY
    profile = market_context_config.target_profiles.get(target_key)
    if profile is None:
        return _RAW_POLICY
    for subscription in profile.market_data_subscriptions:
        if subscription.symbol == market_symbol:
            return (
                _RAW_POLICY
                if subscription.adjustment_policy is None
                else subscription.adjustment_policy
            )
    return _RAW_POLICY


def active_price_basis_projection_ratio(
    *,
    market_mapping: ValidationMarketMappingConfig,
    local_archive_root,
    source_policy: str,
    source_reference_at: datetime,
    target_policy: str,
    target_reference_at: datetime,
) -> float:
    source_multiplier = _basis_projection_multiplier(
        market_mapping=market_mapping,
        local_archive_root=local_archive_root,
        policy=source_policy,
        price_at=source_reference_at,
        reference_at=source_reference_at,
    )
    target_multiplier = _basis_projection_multiplier(
        market_mapping=market_mapping,
        local_archive_root=local_archive_root,
        policy=target_policy,
        price_at=source_reference_at,
        reference_at=target_reference_at,
    )
    return target_multiplier / source_multiplier


def _basis_projection_multiplier(
    *,
    market_mapping: ValidationMarketMappingConfig,
    local_archive_root,
    policy: str,
    price_at: datetime,
    reference_at: datetime,
) -> float:
    if policy == _RAW_POLICY:
        return 1.0
    if local_archive_root is None:
        raise ActivePriceBasisAlignmentError(
            "non-raw active price basis alignment requires "
            "validation.market_data.local_archive_root."
        )
    sidecar = load_adjustment_sidecar_from_archive_root(
        archive_root=local_archive_root,
        market_symbol=market_mapping.market_symbol,
        policy=policy,
    )
    if policy == "forward_adjusted_visible":
        return sidecar.factor_for_timestamp(price_at) / sidecar.factor_for_timestamp(reference_at)
    return sidecar.factor_for_timestamp(price_at)


def _analysis_reference_at(
    *,
    assessment: AnalysisAssessment,
    market_context: MarketContextSnapshot | None,
) -> datetime:
    if assessment.analysis_price_semantics is not None:
        return assessment.analysis_price_semantics.analysis_reference_at
    if market_context is not None:
        range_path = (
            None
            if market_context.market_path is None
            else market_context.market_path.range_path
        )
        if range_path is not None and range_path.lookback_end_at is not None:
            return range_path.lookback_end_at
        return market_context.as_of_at
    return assessment.business_at


def _latest_persisted_assessment(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    level_ids: tuple[str, ...],
) -> AnalysisAssessment | None:
    records = AnalysisAssessmentStore(layout).read_records(target_key=target_key)
    if not records:
        return None
    requested_level_ids = set(level_ids)
    if requested_level_ids:
        for persisted in reversed(records):
            candidate = persisted.record
            candidate_level_ids = {level.level_id for level in candidate.price_level_roles}
            if requested_level_ids & candidate_level_ids:
                return candidate
    return records[-1].record


def _latest_persisted_active_setup_assessment(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    before_business_at: datetime,
) -> AnalysisAssessment | None:
    for persisted in reversed(AnalysisAssessmentStore(layout).read_records(target_key=target_key)):
        candidate = persisted.record
        if candidate.business_at >= before_business_at:
            continue
        if active_price_levels(candidate):
            return candidate
    return None


def _ratio_from_original_semantics(
    *,
    original_assessment: AnalysisAssessment,
    aligned_assessment: AnalysisAssessment,
) -> float | None:
    original = original_assessment.analysis_price_semantics
    aligned = aligned_assessment.analysis_price_semantics
    if original is None or aligned is None:
        return None
    if original.analysis_reference_price <= 0.0:
        return None
    return aligned.analysis_reference_price / original.analysis_reference_price


def _infer_overlap_ratio(
    *,
    assessment: AnalysisAssessment,
    previous_assessment: AnalysisAssessment | None,
) -> float | None:
    if previous_assessment is None:
        return None
    previous_by_id = {level.level_id: level for level in previous_assessment.price_level_roles}
    ratios: list[float] = []
    for level in assessment.price_level_roles:
        if level.value is None:
            continue
        previous = previous_by_id.get(level.level_id)
        if previous is None or previous.value is None or level.value <= 0.0:
            continue
        ratios.append(previous.value / level.value)
    if not ratios:
        return None
    return ratios[0]


def infer_level_date_rebase_ratio(
    *,
    assessment: AnalysisAssessment,
    market_mapping: ValidationMarketMappingConfig,
    local_archive_root,
    source_policy: str,
    target_policy: str,
    target_reference_at: datetime,
) -> float | None:
    ratios: list[float] = []
    for level in assessment.price_level_roles:
        price_at = _price_at_from_level_id(level.level_id)
        if price_at is None:
            continue
        source_multiplier = _basis_projection_multiplier(
            market_mapping=market_mapping,
            local_archive_root=local_archive_root,
            policy=source_policy,
            price_at=price_at,
            reference_at=price_at,
        )
        target_multiplier = _basis_projection_multiplier(
            market_mapping=market_mapping,
            local_archive_root=local_archive_root,
            policy=target_policy,
            price_at=price_at,
            reference_at=target_reference_at,
        )
        ratios.append(target_multiplier / source_multiplier)
    if not ratios:
        return None
    return sum(ratios) / len(ratios)


def _price_at_from_level_id(level_id: str) -> datetime | None:
    match = _LEVEL_DATE_PATTERN.search(level_id)
    if match is None:
        return None
    token = match.group("yyyymmdd")
    try:
        parsed = datetime.strptime(token, "%Y%m%d")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _replace_semantics(
    assessment: AnalysisAssessment,
    *,
    semantics: AnalysisPriceSemantics | None,
) -> AnalysisAssessment:
    return assessment if assessment.analysis_price_semantics == semantics else replace(
        assessment,
        analysis_price_semantics=semantics,
    )


def _fallback_alignment_assessment(
    *,
    assessment: AnalysisAssessment | None,
    layout: WorkspaceLayout,
    market_context: MarketContextSnapshot | None,
) -> AnalysisAssessment | None:
    canonical_assessment = canonicalize_analysis_assessment_price_bases(assessment)
    if canonical_assessment is None:
        return None
    if market_context is None or market_context.market_path is None:
        return None
    range_path = market_context.market_path.range_path
    if range_path.latest_price is None:
        return None
    if not _allows_proxy_alignment_fallback(canonical_assessment):
        return None
    instrument_basis = _fallback_alignment_instrument_basis(
        assessment=canonical_assessment,
        layout=layout,
    )
    if instrument_basis is None:
        return None
    current_leg = market_context.market_path.swing_path.current_leg
    active_levels = narrowed_active_price_levels(
        canonical_assessment.price_level_roles,
        instrument_basis=instrument_basis,
    )
    return replace(
        canonical_assessment,
        analysis_price_semantics=AnalysisPriceSemantics(
            target_key=canonical_assessment.target_key,
            instrument_basis=instrument_basis,
            analysis_reference_price=range_path.latest_price,
            analysis_reference_at=market_context.as_of_at,
            current_leg_start_price=(
                None if current_leg is None else current_leg.from_pivot_price
            ),
            current_leg_end_price=None if current_leg is None else current_leg.latest_price,
            swing_high=_latest_confirmed_pivot_price(
                market_context=market_context,
                kind="swing_high",
            ),
            swing_low=_latest_confirmed_pivot_price(
                market_context=market_context,
                kind="swing_low",
            ),
            window_high=range_path.window_high,
            window_low=range_path.window_low,
            active_price_level_ids=tuple(level.level_id for level in active_levels),
        ),
    )


def _allows_proxy_alignment_fallback(
    assessment: AnalysisAssessment,
) -> bool:
    if not assessment.price_level_roles:
        return True
    return all(
        is_proxy_instrument_basis(level.instrument_basis)
        for level in assessment.price_level_roles
    )


def _fallback_alignment_instrument_basis(
    *,
    assessment: AnalysisAssessment,
    layout: WorkspaceLayout,
) -> str | None:
    if assessment.analysis_price_semantics is not None:
        return canonicalize_instrument_basis(
            assessment.analysis_price_semantics.instrument_basis
        )
    previous = _latest_persisted_active_setup_assessment(
        layout=layout,
        target_key=assessment.target_key,
        before_business_at=assessment.business_at,
    )
    if previous is not None and previous.analysis_price_semantics is not None:
        return canonicalize_instrument_basis(
            previous.analysis_price_semantics.instrument_basis
        )
    basis_values = {
        canonicalize_instrument_basis(level.instrument_basis)
        for level in assessment.price_level_roles
    }
    if len(basis_values) == 1:
        return next(iter(basis_values))
    return None


def _latest_confirmed_pivot_price(
    *,
    market_context: MarketContextSnapshot,
    kind: str,
) -> float | None:
    matching = tuple(
        pivot
        for pivot in market_context.market_path.swing_path.confirmed_pivots
        if pivot.kind == kind
    )
    if not matching:
        return None
    latest = max(
        matching,
        key=lambda pivot: (pivot.confirmed_at, pivot.pivot_at),
    )
    return latest.pivot_price


def _finalize_effective_active_setup(
    *,
    layout: WorkspaceLayout,
    assessment: AnalysisAssessment,
) -> AnalysisAssessment:
    effective_assessment = _carry_forward_active_setup(
        layout=layout,
        assessment=assessment,
    )
    projected_dashboard = projected_market_setup_dashboard_md(effective_assessment)
    if (
        projected_dashboard is not None
        and projected_dashboard != effective_assessment.market_setup_dashboard_md
    ):
        effective_assessment = replace(
            effective_assessment,
            market_setup_dashboard_md=projected_dashboard,
        )
    return effective_assessment


def _carry_forward_active_setup(
    *,
    layout: WorkspaceLayout,
    assessment: AnalysisAssessment,
) -> AnalysisAssessment:
    if assessment.analysis_price_semantics is None or active_price_levels(assessment):
        return assessment
    previous = _latest_persisted_active_setup_assessment(
        layout=layout,
        target_key=assessment.target_key,
        before_business_at=assessment.business_at,
    )
    if previous is None:
        return assessment
    previous_semantics = previous.analysis_price_semantics
    if previous_semantics is None:
        return assessment
    carried_level_ids = previous_semantics.active_price_level_ids
    if not carried_level_ids:
        carried_level_ids = tuple(level.level_id for level in active_price_levels(previous))
    if not carried_level_ids:
        return assessment
    carried_levels = previous.price_level_roles
    carried = replace(
        assessment,
        price_level_roles=carried_levels,
        analysis_price_semantics=replace(
            assessment.analysis_price_semantics,
            active_price_level_ids=carried_level_ids,
        ),
        pm_candidate_review_required=(
            assessment.pm_candidate_review_required
            or any(level.role_if_flat == "entry" for level in carried_levels)
        ),
    )
    return carried


__all__ = [
    "ActivePriceBasisAlignmentError",
    "active_price_basis_projection_ratio",
    "align_analysis_assessment_to_active_price_basis",
    "infer_level_date_rebase_ratio",
    "market_context_adjustment_policy_for_target",
]
