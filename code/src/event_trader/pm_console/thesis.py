"""Map thesis revision audit records into PM Console DTOs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import PurePosixPath

from event_trader.analysis_assessment_store import AnalysisAssessmentStore
from event_trader.contracts._validators import validate_target_key
from event_trader.contracts.analysis_assessment import parse_analysis_assessment
from event_trader.contracts.price_level_role import PriceLevelRole
from event_trader.pm_console.schemas import (
    PMConsoleMarkdownSectionDTO,
    PMConsoleThesisPriceLevelDTO,
    PMConsoleThesisRevisionDetailDTO,
    PMConsoleThesisRevisionDetailResponseDTO,
    PMConsoleThesisRevisionsResponseDTO,
    PMConsoleThesisRevisionSummaryDTO,
    PMConsoleThesisSectionDiffDTO,
)
from event_trader.position_monitoring import list_position_monitoring_targets
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.contracts import ThesisRevisionSectionSnapshot
from event_trader.thesis_revision.store import (
    PersistedThesisRevision,
    ThesisRevisionStore,
    sort_persisted_thesis_revisions,
)


def list_thesis_revision_targets(layout: WorkspaceLayout) -> tuple[str, ...]:
    """List targets with thesis revision shards in the workspace."""

    root = layout.runtime_root / "thesis_revisions"
    if not root.exists() or not root.is_dir():
        return ()
    targets: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            targets.append(
                validate_target_key(
                    child.name,
                    error_type=ValueError,
                )
            )
        except ValueError:
            continue
    return tuple(targets)


def map_pm_console_target_views(layout: WorkspaceLayout) -> dict[str, tuple[str, ...]]:
    """Map each PM Console target to the implemented read-only views."""

    position_targets = set(list_position_monitoring_targets(layout))
    thesis_targets = set(list_thesis_revision_targets(layout))
    target_views: dict[str, tuple[str, ...]] = {}
    for target_key in sorted(position_targets | thesis_targets):
        implemented_views: list[str] = ["workbench"]
        if target_key in thesis_targets:
            implemented_views.append("thesis")
        if target_key in position_targets:
            implemented_views.append("position")
        target_views[target_key] = tuple(implemented_views)
    return target_views


def list_pm_console_targets(layout: WorkspaceLayout) -> tuple[str, ...]:
    """List PM Console targets across implemented read-only slices."""

    return tuple(map_pm_console_target_views(layout))


def build_thesis_revisions_response(
    layout: WorkspaceLayout,
    *,
    target_key: str,
) -> PMConsoleThesisRevisionsResponseDTO:
    """Build the thesis revision timeline response for one target."""

    persisted_revisions = sort_persisted_thesis_revisions(
        ThesisRevisionStore(layout).read_records(target_key=target_key)
    )
    summaries = tuple(_summary(persisted) for persisted in persisted_revisions)
    return PMConsoleThesisRevisionsResponseDTO(
        generated_at=datetime.now(UTC).isoformat(),
        target_key=target_key,
        revisions=summaries,
        latest_revision=(None if not summaries else summaries[-1]),
    )


def build_thesis_revision_detail_response(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    revision_id: str,
) -> PMConsoleThesisRevisionDetailResponseDTO | None:
    """Build the thesis revision detail response for one revision."""

    persisted = load_persisted_thesis_revision(
        layout,
        target_key=target_key,
        revision_id=revision_id,
    )
    if persisted is None:
        return None
    return PMConsoleThesisRevisionDetailResponseDTO(
        generated_at=datetime.now(UTC).isoformat(),
        target_key=target_key,
        revision=_detail(layout, persisted),
    )


def load_persisted_thesis_revision(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    revision_id: str,
) -> PersistedThesisRevision | None:
    """Load one persisted thesis revision by id."""

    for persisted in sort_persisted_thesis_revisions(
        ThesisRevisionStore(layout).read_records(target_key=target_key)
    ):
        if persisted.record.revision_id == revision_id:
            return persisted
    return None


def load_thesis_revision_section(
    layout: WorkspaceLayout,
    *,
    target_key: str,
    revision_id: str,
    section_id: str,
) -> ThesisRevisionSectionSnapshot | None:
    """Load one canonical thesis section from a persisted revision."""

    persisted = load_persisted_thesis_revision(
        layout,
        target_key=target_key,
        revision_id=revision_id,
    )
    if persisted is None:
        return None
    for section in persisted.record.sections:
        if section.section_id == section_id:
            return section
    return None


def _summary(persisted: PersistedThesisRevision) -> PMConsoleThesisRevisionSummaryDTO:
    revision = persisted.record
    return PMConsoleThesisRevisionSummaryDTO(
        revision_id=revision.revision_id,
        target_key=revision.target_key,
        business_at=revision.business_at.isoformat(),
        committed_at=revision.committed_at.isoformat(),
        source=revision.source,
        source_event_count=len(revision.source_event_ids),
        changed_claim_count=len(revision.changed_claim_ids),
        changed_section_count=sum(
            1 for diff in revision.diffs_from_previous if diff.unified_diff_md
        ),
        previous_revision_id=revision.previous_revision_id,
        is_baseline=revision.source == "migration_baseline",
    )


def _detail(
    layout: WorkspaceLayout,
    persisted: PersistedThesisRevision,
) -> PMConsoleThesisRevisionDetailDTO:
    revision = persisted.record
    changed_section_ids = {
        f"{diff.page_path}:{diff.section_name}"
        for diff in revision.diffs_from_previous
        if diff.unified_diff_md
    }
    price_levels = _load_price_levels(
        layout=layout,
        target_key=revision.target_key,
        analysis_assessment_id=revision.analysis_assessment_id,
    )
    return PMConsoleThesisRevisionDetailDTO(
        revision_id=revision.revision_id,
        target_key=revision.target_key,
        business_at=revision.business_at.isoformat(),
        committed_at=revision.committed_at.isoformat(),
        source=revision.source,
        analysis_assessment_id=revision.analysis_assessment_id,
        context_packet_id=revision.context_packet_id,
        context_packet_hash=revision.context_packet_hash,
        source_event_ids=revision.source_event_ids,
        research_memory_write_receipt_ids=revision.research_memory_write_receipt_ids,
        changed_claim_ids=revision.changed_claim_ids,
        key_claim_ids=revision.key_claim_ids,
        contested_prior_claim_ids=revision.contested_prior_claim_ids,
        price_level_role_ids=revision.price_level_role_ids,
        price_levels=price_levels,
        previous_revision_id=revision.previous_revision_id,
        canonical_bundle_sha256=revision.canonical_bundle_sha256,
        is_baseline=revision.source == "migration_baseline",
        sections=tuple(
            PMConsoleMarkdownSectionDTO(
                page_path=section.page_path,
                page_name=_page_name(section.page_path),
                section_name=section.section_name,
                content_md=section.content_md,
                content_sha256=section.content_sha256,
                changed_in_revision=section.section_id in changed_section_ids,
            )
            for section in revision.sections
        ),
        diffs_from_previous=tuple(
            PMConsoleThesisSectionDiffDTO(
                page_path=diff.page_path,
                page_name=_page_name(diff.page_path),
                section_name=diff.section_name,
                unified_diff_md=diff.unified_diff_md,
                previous_content_sha256=diff.previous_content_sha256,
                content_sha256=diff.content_sha256,
                changed=bool(diff.unified_diff_md),
            )
            for diff in revision.diffs_from_previous
        ),
    )


def _load_price_levels(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    analysis_assessment_id: str | None,
) -> tuple[PMConsoleThesisPriceLevelDTO, ...]:
    if analysis_assessment_id is None:
        return ()
    for persisted in AnalysisAssessmentStore(layout).read_records(target_key=target_key):
        assessment = persisted.record
        if assessment.assessment_id != analysis_assessment_id:
            continue
        return tuple(_price_level(level) for level in assessment.price_level_roles)
    for assessment in _iter_embedded_analysis_assessments(
        layout=layout,
        target_key=target_key,
    ):
        if assessment.assessment_id != analysis_assessment_id:
            continue
        return tuple(_price_level(level) for level in assessment.price_level_roles)
    return ()


def _iter_embedded_analysis_assessments(
    *,
    layout: WorkspaceLayout,
    target_key: str,
):
    outcome_root = layout.runtime_root / "analysis_outcomes" / target_key
    if not outcome_root.exists() or not outcome_root.is_dir():
        return
    for path in sorted(outcome_root.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            embedded = payload.get("analysis_assessment")
            if not isinstance(embedded, dict):
                continue
            try:
                assessment = parse_analysis_assessment(embedded)
            except ValueError:
                continue
            if assessment.target_key == target_key:
                yield assessment


def _price_level(level: PriceLevelRole) -> PMConsoleThesisPriceLevelDTO:
    return PMConsoleThesisPriceLevelDTO(
        level_id=level.level_id,
        display_role=_display_role(level.role_if_flat),
        role_if_flat=level.role_if_flat,
        value=level.value,
        lower=level.lower,
        upper=level.upper,
        instrument_basis=level.instrument_basis,
    )


def _display_role(role_if_flat: str) -> str:
    if role_if_flat in {"entry", "add", "hold_boundary"}:
        return "support"
    if role_if_flat in {
        "de_risk_or_take_profit",
        "exit",
        "reverse_or_cover",
        "invalidates",
    }:
        return "resistance"
    if role_if_flat == "watch_only":
        return "watch"
    return "level"


def _page_name(page_path: str) -> str:
    return PurePosixPath(page_path).name


__all__ = [
    "build_thesis_revision_detail_response",
    "build_thesis_revisions_response",
    "load_persisted_thesis_revision",
    "load_thesis_revision_section",
    "map_pm_console_target_views",
    "list_pm_console_targets",
    "list_thesis_revision_targets",
]
