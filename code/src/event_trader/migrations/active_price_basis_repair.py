"""Deterministic repair for post-cutover active-price assessments and pages."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from event_trader.analysis_assessment_store import (
    AnalysisAssessmentStore,
    AnalysisAssessmentStoreError,
    PersistedAnalysisAssessment,
)
from event_trader.config import (
    MarketContextConfig,
    ValidationConfig,
    ValidationMarketMappingConfig,
)
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.analysis_price_semantics import AnalysisPriceSemantics
from event_trader.contracts.price_level_role import PriceLevelRole
from event_trader.contracts.view_state_change import MarketMapping
from event_trader.market.adjustments import (
    apply_adjustment_policy_to_series,
    load_adjustment_sidecar_from_archive_root,
)
from event_trader.market.local_archive_provider import LocalArchiveMarketDataProvider
from event_trader.migrations.active_price_basis_cutover import (
    ActivePriceBasisCutoverError,
    read_active_price_basis_pointer,
)
from event_trader.research_memory.active_price_projection import (
    ActivePriceProjectionSection,
    active_price_levels,
    project_active_price_sections,
    projected_market_setup_dashboard_md,
)
from event_trader.research_memory.active_price_basis_alignment import (
    infer_level_date_rebase_ratio,
)
from event_trader.research_memory.analysis_price_basis import canonicalize_instrument_basis
from event_trader.research_memory.analysis_price_basis_rebase import (
    rescale_analysis_assessment,
)
from event_trader.research_memory.write_receipts import (
    ReceiptedResearchMemoryPageWriter,
    ResearchMemoryWriteAttribution,
)
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.builder import ThesisRevisionBuildError, ThesisRevisionBuilder
from event_trader.thesis_revision.store import ThesisRevisionStore, ThesisRevisionStoreError

_RAW_POLICY = "raw"
_REPAIR_ACTOR_TYPE = "operator"


class ActivePriceBasisRepairError(ValueError):
    """Raised when persisted active-price truth cannot be repaired deterministically."""


@dataclass(frozen=True, slots=True)
class ActivePriceBasisRepairReceipt:
    target_key: str
    anchor_at: datetime
    assessment_count: int
    repaired_assessment_ids: tuple[str, ...]
    rewritten_paths: tuple[str, ...]
    thesis_revision_id: str | None

    def to_json_payload(self, *, workspace_root: Path) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "anchor_at": self.anchor_at.isoformat(),
            "assessment_count": self.assessment_count,
            "repaired_assessment_ids": list(self.repaired_assessment_ids),
            "rewritten_paths": list(self.rewritten_paths),
            "thesis_revision_id": self.thesis_revision_id,
            "workspace_root": workspace_root.as_posix(),
        }


def repair_active_price_basis_chain(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    validation_config: ValidationConfig | None,
    market_context_config: MarketContextConfig | None,
) -> ActivePriceBasisRepairReceipt:
    if validation_config is None:
        raise ActivePriceBasisRepairError(
            "active price basis repair requires validation config."
        )
    pointer = read_active_price_basis_pointer(layout=layout, target_key=target_key)
    if pointer is None:
        raise ActivePriceBasisRepairError(
            f"active price basis pointer is missing for target {target_key!r}."
        )
    market_mapping = validation_config.market_mappings.get(target_key)
    if market_mapping is None:
        raise ActivePriceBasisRepairError(
            f"validation.market_mappings is missing target {target_key!r}."
        )
    store = AnalysisAssessmentStore(layout)
    try:
        persisted = store.read_records(target_key=target_key)
    except AnalysisAssessmentStoreError as exc:
        raise ActivePriceBasisRepairError(str(exc)) from exc
    candidate_records = tuple(
        record
        for record in persisted
        if record.record.business_at >= pointer.anchor_at
    )
    if not candidate_records:
        return ActivePriceBasisRepairReceipt(
            target_key=target_key,
            anchor_at=pointer.anchor_at,
            assessment_count=0,
            repaired_assessment_ids=(),
            rewritten_paths=(),
            thesis_revision_id=None,
        )

    previous_repaired: AnalysisAssessment | None = None
    latest_repaired_with_levels: AnalysisAssessment | None = None
    repaired_by_id: dict[str, AnalysisAssessment] = {}
    changed_ids: list[str] = []
    for record in candidate_records:
        repaired = _repair_assessment(
            assessment=record.record,
            previous_repaired=previous_repaired,
            latest_repaired_with_levels=latest_repaired_with_levels,
            market_mapping=market_mapping,
            validation_config=validation_config,
            target_policy=pointer.policy,
        )
        previous_repaired = repaired
        if repaired.price_level_roles:
            latest_repaired_with_levels = repaired
        repaired_by_id[record.record.assessment_id] = repaired
        if repaired != record.record:
            changed_ids.append(record.record.assessment_id)

    rewritten_paths = tuple(
        path.as_posix()
        for path in _rewrite_assessment_files(
            persisted_records=tuple(record for record in persisted if record.record.assessment_id in repaired_by_id),
            repaired_by_id=repaired_by_id,
        )
    )
    latest_repaired = previous_repaired
    thesis_revision_id: str | None = None
    if latest_repaired is not None:
        projected_sections = project_active_price_sections(latest_repaired)
        if projected_sections:
            receipts = _write_projected_sections(
                layout=layout,
                business_at=latest_repaired.business_at,
                assessment_id=latest_repaired.assessment_id,
                projected_sections=projected_sections,
            )
            thesis_revision_id = _append_repair_revision(
                layout=layout,
                assessment=latest_repaired,
                receipts=receipts,
            )
            rewritten_paths = tuple(
                dict.fromkeys(
                    (*rewritten_paths, *(section.page_path for section in projected_sections))
                )
            )
    return ActivePriceBasisRepairReceipt(
        target_key=target_key,
        anchor_at=pointer.anchor_at,
        assessment_count=len(candidate_records),
        repaired_assessment_ids=tuple(changed_ids),
        rewritten_paths=rewritten_paths,
        thesis_revision_id=thesis_revision_id,
    )


def _repair_assessment(
    *,
    assessment: AnalysisAssessment,
    previous_repaired: AnalysisAssessment | None,
    latest_repaired_with_levels: AnalysisAssessment | None,
    market_mapping: ValidationMarketMappingConfig,
    validation_config: ValidationConfig,
    target_policy: str,
) -> AnalysisAssessment:
    repaired = assessment
    target_semantics = _bootstrap_target_semantics(
        assessment=repaired,
        previous_repaired=previous_repaired,
        market_mapping=market_mapping,
        validation_config=validation_config,
        target_policy=target_policy,
    )
    if repaired.analysis_price_semantics is not None:
        rescaled = False
        if repaired.analysis_price_semantics.analysis_reference_price > 0.0:
            ratio = (
                target_semantics.analysis_reference_price
                / repaired.analysis_price_semantics.analysis_reference_price
            )
            if abs(ratio - 1.0) > 1e-12:
                repaired = rescale_analysis_assessment(
                    repaired,
                    ratio=ratio,
                )
                rescaled = True
        if not rescaled:
            overlap_ratio = _infer_overlap_ratio(
                assessment=repaired,
                previous_repaired=latest_repaired_with_levels,
            )
            if overlap_ratio is not None and abs(overlap_ratio - 1.0) > 1e-12:
                repaired = rescale_analysis_assessment(
                    repaired,
                    ratio=overlap_ratio,
                )
                rescaled = True
        if not rescaled:
            level_date_ratio = infer_level_date_rebase_ratio(
                assessment=repaired,
                market_mapping=market_mapping,
                local_archive_root=validation_config.market_data.local_archive_root,
                source_policy=_RAW_POLICY,
                target_policy=target_policy,
                target_reference_at=target_semantics.analysis_reference_at,
            )
            if level_date_ratio is not None and abs(level_date_ratio - 1.0) > 1e-12:
                repaired = rescale_analysis_assessment(
                    repaired,
                    ratio=level_date_ratio,
                )
        repaired = replace(repaired, analysis_price_semantics=target_semantics)
    else:
        overlap_ratio = _infer_overlap_ratio(
            assessment=repaired,
            previous_repaired=latest_repaired_with_levels,
        )
        if overlap_ratio is not None and abs(overlap_ratio - 1.0) > 1e-12:
            repaired = rescale_analysis_assessment(
                repaired,
                ratio=overlap_ratio,
            )
        else:
            level_date_ratio = infer_level_date_rebase_ratio(
                assessment=repaired,
                market_mapping=market_mapping,
                local_archive_root=validation_config.market_data.local_archive_root,
                source_policy=_RAW_POLICY,
                target_policy=target_policy,
                target_reference_at=target_semantics.analysis_reference_at,
            )
            if level_date_ratio is not None and abs(level_date_ratio - 1.0) > 1e-12:
                repaired = rescale_analysis_assessment(
                    repaired,
                    ratio=level_date_ratio,
                )
        repaired = replace(
            repaired,
            analysis_price_semantics=target_semantics,
        )
    repaired = _carry_forward_active_setup_from_latest(
        assessment=repaired,
        latest_repaired_with_levels=latest_repaired_with_levels,
    )
    projected_dashboard = _projected_market_setup_dashboard(repaired)
    if projected_dashboard is not None and projected_dashboard != repaired.market_setup_dashboard_md:
        repaired = replace(repaired, market_setup_dashboard_md=projected_dashboard)
    return repaired


def _bootstrap_target_semantics(
    *,
    assessment: AnalysisAssessment,
    previous_repaired: AnalysisAssessment | None,
    market_mapping: ValidationMarketMappingConfig,
    validation_config: ValidationConfig,
    target_policy: str,
) -> AnalysisPriceSemantics:
    reference_at = assessment.business_at
    reference_price = load_market_reference_price_for_policy(
        target_key=assessment.target_key,
        market_mapping=market_mapping,
        validation_config=validation_config,
        price_policy=target_policy,
        as_of_at=reference_at,
    )
    active_levels = _active_levels(assessment.price_level_roles)
    if active_levels:
        basis_values = {
            canonicalize_instrument_basis(level.instrument_basis) for level in active_levels
        }
        instrument_basis = sorted(basis_values)[0]
        active_level_ids = tuple(level.level_id for level in active_levels)
    elif previous_repaired is not None and previous_repaired.analysis_price_semantics is not None:
        instrument_basis = previous_repaired.analysis_price_semantics.instrument_basis
        active_level_ids = ()
    else:
        instrument_basis = market_mapping.market_symbol
        active_level_ids = ()
    return AnalysisPriceSemantics(
        target_key=assessment.target_key,
        instrument_basis=instrument_basis,
        analysis_reference_price=reference_price,
        analysis_reference_at=reference_at,
        current_leg_start_price=None,
        current_leg_end_price=None,
        swing_high=None,
        swing_low=None,
        window_high=None,
        window_low=None,
        active_price_level_ids=active_level_ids,
    )


def _infer_overlap_ratio(
    *,
    assessment: AnalysisAssessment,
    previous_repaired: AnalysisAssessment | None,
) -> float | None:
    if previous_repaired is None:
        return None
    previous_by_id = {level.level_id: level for level in previous_repaired.price_level_roles}
    ratios: list[float] = []
    for level in assessment.price_level_roles:
        if level.value is None or level.value <= 0.0:
            continue
        previous = previous_by_id.get(level.level_id)
        if previous is None or previous.value is None:
            continue
        ratios.append(previous.value / level.value)
    if not ratios:
        return None
    return ratios[0]


def load_market_reference_price_for_policy(
    *,
    target_key: str,
    market_mapping: ValidationMarketMappingConfig,
    validation_config: ValidationConfig,
    price_policy: str,
    as_of_at: datetime,
) -> float:
    archive_root = validation_config.market_data.local_archive_root
    if archive_root is None:
        raise ActivePriceBasisRepairError(
            "active price basis repair requires validation.market_data.local_archive_root."
        )
    provider = LocalArchiveMarketDataProvider(archive_root)
    mapping = MarketMapping(
        target_key=target_key,
        market_symbol=market_mapping.market_symbol,
        market_session=market_mapping.market_session,
        exchange=market_mapping.exchange,
        bar_granularity=market_mapping.bar_granularity,
        exchange_session_scope=market_mapping.exchange_session_scope,
    )
    series = provider.read_series(
        mapping,
        start_at=datetime(1970, 1, 1, tzinfo=UTC),
        end_at=as_of_at + timedelta(seconds=1),
    )
    bars = series.bars
    if price_policy != _RAW_POLICY:
        sidecar = load_adjustment_sidecar_from_archive_root(
            archive_root=archive_root,
            market_symbol=market_mapping.market_symbol,
            policy=price_policy,
        )
        bars = apply_adjustment_policy_to_series(
            series=series,
            sidecar=sidecar,
            reference_at=as_of_at,
        ).series.bars
    visible = tuple(bar for bar in bars if bar.end_at <= as_of_at)
    if not visible:
        raise ActivePriceBasisRepairError(
            "no market bars are available at or before "
            f"{as_of_at.isoformat()} for target {target_key!r}."
        )
    return visible[-1].close_price


def _rewrite_assessment_files(
    *,
    persisted_records: tuple[PersistedAnalysisAssessment, ...],
    repaired_by_id: dict[str, AnalysisAssessment],
) -> tuple[Path, ...]:
    records_by_path: dict[Path, list[PersistedAnalysisAssessment]] = {}
    for record in persisted_records:
        records_by_path.setdefault(record.path, []).append(record)
    rewritten_paths: list[Path] = []
    for path, path_records in records_by_path.items():
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        changed = False
        for record in path_records:
            repaired = repaired_by_id[record.record.assessment_id]
            new_line = json.dumps(repaired.to_json_payload(), ensure_ascii=False)
            index = record.line_number - 1
            if index >= len(lines):
                raise ActivePriceBasisRepairError(
                    f"analysis assessment shard line is out of range: {path}:{record.line_number}"
                )
            if lines[index] != new_line:
                lines[index] = new_line
                changed = True
        if not changed:
            continue
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        rewritten_paths.append(path)
    return tuple(rewritten_paths)


def _write_projected_sections(
    *,
    layout: WorkspaceLayout,
    business_at: datetime,
    assessment_id: str,
    projected_sections: tuple[ActivePriceProjectionSection, ...],
):
    writer = ReceiptedResearchMemoryPageWriter(
        layout=layout,
        attribution=ResearchMemoryWriteAttribution(
            actor_type=_REPAIR_ACTOR_TYPE,
            actor_id=f"active-price-basis-repair:{assessment_id}",
            business_at=business_at,
            committed_at=business_at,
        ),
    )
    for section in projected_sections:
        writer.update_page_section(
            section.page_path,
            section.section_name,
            section.content_md,
        )
    return writer.receipts


def _append_repair_revision(
    *,
    layout: WorkspaceLayout,
    assessment: AnalysisAssessment,
    receipts,
) -> str | None:
    if not receipts:
        return None
    try:
        revision = ThesisRevisionBuilder(
            layout=layout,
            store=ThesisRevisionStore(layout),
        ).build(
            target_key=assessment.target_key,
            business_at=assessment.business_at,
            committed_at=assessment.business_at,
            source="active_price_basis_cutover",
            analysis_assessment=assessment,
            context_packet_id=None,
            context_packet_hash=None,
            source_event_ids=assessment.source_event_ids,
            research_memory_write_receipt_ids=tuple(
                receipt.receipt_id for receipt in receipts
            ),
            committed_write_receipts=receipts,
        )
        ThesisRevisionStore(layout).append(revision)
        return revision.revision_id
    except (ThesisRevisionBuildError, ThesisRevisionStoreError, ActivePriceBasisCutoverError):
        return None


def _projected_market_setup_dashboard(
    assessment: AnalysisAssessment,
) -> str | None:
    return projected_market_setup_dashboard_md(assessment)


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


def _carry_forward_active_setup_from_latest(
    *,
    assessment: AnalysisAssessment,
    latest_repaired_with_levels: AnalysisAssessment | None,
) -> AnalysisAssessment:
    if assessment.analysis_price_semantics is None or active_price_levels(assessment):
        return assessment
    if latest_repaired_with_levels is None or latest_repaired_with_levels.analysis_price_semantics is None:
        return assessment
    carried_level_ids = latest_repaired_with_levels.analysis_price_semantics.active_price_level_ids
    if not carried_level_ids:
        carried_level_ids = tuple(
            level.level_id for level in active_price_levels(latest_repaired_with_levels)
        )
    if not carried_level_ids:
        return assessment
    carried_levels = latest_repaired_with_levels.price_level_roles
    carried = replace(
        assessment,
        price_level_roles=carried_levels,
        analysis_price_semantics=replace(
            assessment.analysis_price_semantics,
            active_price_level_ids=carried_level_ids,
        ),
    )
    return carried


__all__ = [
    "ActivePriceBasisRepairError",
    "ActivePriceBasisRepairReceipt",
    "load_market_reference_price_for_policy",
    "repair_active_price_basis_chain",
]
