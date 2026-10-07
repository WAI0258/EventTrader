"""Provider-neutral commit boundary for analysis-owned ResearchMemory writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.reasoning.analysis_write_receipts import (
    collapse_analysis_write_receipts,
    protected_rewrite_sections_by_page,
    validate_analysis_commit_receipts,
)
from event_trader.research_memory.active_price_projection import (
    project_active_price_sections,
)
from event_trader.research_memory.write_receipts import (
    ReceiptedResearchMemoryPageWriter,
    ResearchMemoryWriteAttribution,
    ResearchMemoryWriteReceipt,
)
from event_trader.storage import WorkspaceLayout


class AnalysisMemoryCommitError(RuntimeError):
    """Raised when an admitted analysis write cannot be dispatched safely."""


@dataclass(frozen=True, slots=True)
class CommittedAnalysisMemoryWrites:
    receipt_ids: tuple[str, ...]
    receipts: tuple[ResearchMemoryWriteReceipt, ...]


def commit_analysis_memory_writes(
    *,
    layout: WorkspaceLayout,
    write_receipts: tuple[dict[str, object], ...],
    analysis_assessment: AnalysisAssessment | None,
    target_key: str,
    actor_id: str,
    business_at: datetime,
    committed_at: datetime,
    event_ids: tuple[str, ...],
    context_packet_id: str,
    context_packet_hash: str,
) -> CommittedAnalysisMemoryWrites:
    """Validate, project, and commit analysis page writes with attribution."""

    collapsed_receipts = collapse_analysis_write_receipts(write_receipts)
    validate_analysis_commit_receipts(
        target_key=target_key,
        analysis_assessment=analysis_assessment,
        write_receipts=collapsed_receipts,
    )

    projected_sections = project_active_price_sections(analysis_assessment)
    projected_section_ids = {section.section_id for section in projected_sections}
    protected_sections_by_page = protected_rewrite_sections_by_page(analysis_assessment)

    page_writes = ReceiptedResearchMemoryPageWriter(
        layout=layout,
        attribution=ResearchMemoryWriteAttribution(
            actor_type="analysis",
            actor_id=actor_id,
            business_at=business_at,
            committed_at=committed_at,
            event_ids=event_ids,
            context_packet_id=context_packet_id,
            context_packet_hash=context_packet_hash,
        ),
    )
    for receipt in collapsed_receipts:
        operation = _require_receipt_text(receipt, "operation")
        content_md = _require_receipt_text(receipt, "content_md")
        if operation == "update_index":
            raise AnalysisMemoryCommitError(
                "Analysis attempted update_index, but analysis must not write index.md."
            )
        if operation == "update_page_section":
            page_path = _require_receipt_text(receipt, "page_path")
            section_name = _require_receipt_text(receipt, "section_name")
            if f"{page_path}:{section_name}" in projected_section_ids:
                continue
            page_writes.update_page_section(page_path, section_name, content_md)
            continue
        if operation == "rewrite_page":
            page_path = _require_receipt_text(receipt, "page_path")
            protected_sections = protected_sections_by_page.get(page_path)
            if protected_sections:
                protected_section_names = ", ".join(
                    f"`{section_name}`" for section_name in protected_sections
                )
                raise AnalysisMemoryCommitError(
                    "Analysis attempted rewrite_page for "
                    f"{page_path}, but deterministic active sections must stay "
                    f"section-owned: {protected_section_names}."
                )
            page_writes.rewrite_page(page_path, content_md)
            continue
        if operation == "create_page":
            page_path = _require_receipt_text(receipt, "page_path")
            page_writes.create_page(page_path, content_md)
            continue
        raise AnalysisMemoryCommitError(f"Unknown analysis write receipt operation: {operation}")

    for section in projected_sections:
        page_writes.update_page_section(
            section.page_path,
            section.section_name,
            section.content_md,
        )
    return CommittedAnalysisMemoryWrites(
        receipt_ids=page_writes.receipt_ids,
        receipts=page_writes.receipts,
    )


def _require_receipt_text(receipt: dict[str, object], field_name: str) -> str:
    value = receipt.get(field_name)
    if not isinstance(value, str):
        raise AnalysisMemoryCommitError(
            f"Analysis write receipt field {field_name!r} must be a string."
        )
    normalized = value.strip()
    if not normalized:
        raise AnalysisMemoryCommitError(
            f"Analysis write receipt field {field_name!r} must not be blank."
        )
    return normalized


__all__ = [
    "AnalysisMemoryCommitError",
    "CommittedAnalysisMemoryWrites",
    "commit_analysis_memory_writes",
]
