"""Builder for canonical thesis revision snapshots."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from event_trader.analysis import FileBackedResearchMemoryReader
from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.integrations.markdown_context import MarkdownContextError, find_markdown_section
from event_trader.research_memory.write_receipts import ResearchMemoryWriteReceipt
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.canonical_bundle import canonical_thesis_bundle_sections
from event_trader.thesis_revision.contracts import (
    ThesisRevision,
    ThesisRevisionSectionDiff,
    ThesisRevisionSectionSnapshot,
)
from event_trader.thesis_revision.store import ThesisRevisionStore


class ThesisRevisionBuildError(ValueError):
    """Raised when a thesis revision cannot be built from canonical pages."""


@dataclass(frozen=True, slots=True)
class ThesisRevisionBuilder:
    layout: WorkspaceLayout
    store: ThesisRevisionStore | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.layout, WorkspaceLayout):
            raise ThesisRevisionBuildError("layout must be a WorkspaceLayout instance.")
        if self.store is not None and not isinstance(self.store, ThesisRevisionStore):
            raise ThesisRevisionBuildError("store must be a ThesisRevisionStore instance.")

    def build(
        self,
        *,
        target_key: str,
        business_at: datetime,
        committed_at: datetime,
        source: str,
        analysis_assessment: AnalysisAssessment | None,
        context_packet_id: str | None,
        context_packet_hash: str | None,
        source_event_ids: tuple[str, ...],
        research_memory_write_receipt_ids: tuple[str, ...],
        committed_write_receipts: tuple[ResearchMemoryWriteReceipt, ...],
    ) -> ThesisRevision:
        store = self.store or ThesisRevisionStore(self.layout)
        previous = store.read_latest(target_key=target_key)
        sections = self._load_sections(target_key)
        canonical_bundle_sha256 = _canonical_bundle_sha256(sections)
        diffs_from_previous = (
            ()
            if previous is None
            else _diffs_from_previous(previous.record, sections)
        )
        revision_id = _revision_id(
            source=source,
            target_key=target_key,
            business_at=business_at,
            analysis_assessment_id=(
                None if analysis_assessment is None else analysis_assessment.assessment_id
            ),
            context_packet_id=context_packet_id,
            source_event_ids=(
                analysis_assessment.source_event_ids
                if analysis_assessment is not None
                else source_event_ids
            ),
            research_memory_write_receipt_ids=research_memory_write_receipt_ids,
            previous_revision_id=(
                None if previous is None else previous.record.revision_id
            ),
            canonical_bundle_sha256=canonical_bundle_sha256,
        )
        return ThesisRevision(
            revision_id=revision_id,
            target_key=target_key,
            business_at=business_at,
            committed_at=committed_at,
            source=source,
            analysis_assessment_id=(
                None if analysis_assessment is None else analysis_assessment.assessment_id
            ),
            context_packet_id=context_packet_id,
            context_packet_hash=context_packet_hash,
            source_event_ids=(
                analysis_assessment.source_event_ids
                if analysis_assessment is not None
                else source_event_ids
            ),
            research_memory_write_receipt_ids=research_memory_write_receipt_ids,
            changed_claim_ids=_aggregate_changed_claim_ids(committed_write_receipts),
            sections=sections,
            diffs_from_previous=diffs_from_previous,
            key_claim_ids=(
                ()
                if analysis_assessment is None
                else analysis_assessment.key_claim_ids
            ),
            contested_prior_claim_ids=(
                ()
                if analysis_assessment is None
                else analysis_assessment.contested_prior_claim_ids
            ),
            price_level_role_ids=(
                ()
                if analysis_assessment is None
                else tuple(level.level_id for level in analysis_assessment.price_level_roles)
            ),
            previous_revision_id=(
                None if previous is None else previous.record.revision_id
            ),
            canonical_bundle_sha256=canonical_bundle_sha256,
        )

    def _load_sections(
        self,
        target_key: str,
    ) -> tuple[ThesisRevisionSectionSnapshot, ...]:
        reader = FileBackedResearchMemoryReader(self.layout)
        unique_page_paths = tuple(
            dict.fromkeys(
                section.page_path
                for section in canonical_thesis_bundle_sections(target_key)
            )
        )
        pages_by_path = {
            page_path: reader.read_page(page_path) for page_path in unique_page_paths
        }
        snapshots: list[ThesisRevisionSectionSnapshot] = []
        for bundle_section in canonical_thesis_bundle_sections(target_key):
            page = pages_by_path.get(bundle_section.page_path)
            if page is None:
                raise ThesisRevisionBuildError(
                    f"Missing required canonical thesis page: {bundle_section.page_path}"
                )
            section_content = _extract_required_section(
                page.content_md,
                page_path=bundle_section.page_path,
                section_name=bundle_section.section_name,
            )
            snapshots.append(
                ThesisRevisionSectionSnapshot(
                    page_path=bundle_section.page_path,
                    section_name=bundle_section.section_name,
                    content_md=section_content,
                    content_sha256=_content_sha256(section_content),
                )
            )
        return tuple(snapshots)


def _extract_required_section(
    content_md: str,
    *,
    page_path: str,
    section_name: str,
) -> str:
    try:
        section = find_markdown_section(
            content_md,
            heading=section_name,
            heading_level=2,
            excerpt_char_limit=max(1, len(content_md)),
        )
    except MarkdownContextError as exc:
        raise ThesisRevisionBuildError(
            f"Failed to extract canonical thesis section {page_path}:{section_name}: {exc}"
        ) from exc
    if section is None:
        raise ThesisRevisionBuildError(
            f"Missing required canonical thesis section {page_path}:{section_name}"
        )
    excerpt = section.get("content_excerpt")
    truncated = section.get("content_truncated")
    if not isinstance(excerpt, str):
        raise ThesisRevisionBuildError(
            f"Canonical thesis section excerpt must be a string: {page_path}:{section_name}"
        )
    if truncated is True:
        raise ThesisRevisionBuildError(
            f"Canonical thesis section extraction truncated unexpectedly: {page_path}:{section_name}"
        )
    return excerpt


def _diffs_from_previous(
    previous: ThesisRevision,
    sections: tuple[ThesisRevisionSectionSnapshot, ...],
) -> tuple[ThesisRevisionSectionDiff, ...]:
    previous_by_id = {section.section_id: section for section in previous.sections}
    diffs: list[ThesisRevisionSectionDiff] = []
    for section in sections:
        previous_section = previous_by_id.get(section.section_id)
        if previous_section is None:
            raise ThesisRevisionBuildError(
                "previous thesis revision is missing a canonical bundle section: "
                f"{section.section_id}"
            )
        diffs.append(
            ThesisRevisionSectionDiff(
                page_path=section.page_path,
                section_name=section.section_name,
                previous_content_sha256=previous_section.content_sha256,
                content_sha256=section.content_sha256,
                unified_diff_md=_unified_diff(
                    before=previous_section.content_md,
                    after=section.content_md,
                    section_id=section.section_id,
                ),
            )
        )
    return tuple(diffs)


def _aggregate_changed_claim_ids(
    receipts: tuple[ResearchMemoryWriteReceipt, ...],
) -> tuple[str, ...]:
    changed_claim_ids: list[str] = []
    for receipt in receipts:
        changed_claim_ids.extend(receipt.changed_claim_ids)
    return tuple(dict.fromkeys(changed_claim_ids))


def _canonical_bundle_sha256(
    sections: tuple[ThesisRevisionSectionSnapshot, ...],
) -> str:
    payload = [
        {
            "page_path": section.page_path,
            "section_name": section.section_name,
            "content_sha256": section.content_sha256,
            "content_md": section.content_md,
        }
        for section in sections
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _revision_id(
    *,
    source: str,
    target_key: str,
    business_at: datetime,
    analysis_assessment_id: str | None,
    context_packet_id: str | None,
    source_event_ids: tuple[str, ...],
    research_memory_write_receipt_ids: tuple[str, ...],
    previous_revision_id: str | None,
    canonical_bundle_sha256: str,
) -> str:
    identity = {
        "source": source,
        "target_key": target_key,
        "business_at": business_at.isoformat(),
        "analysis_assessment_id": analysis_assessment_id,
        "context_packet_id": context_packet_id,
        "source_event_ids": list(source_event_ids),
        "research_memory_write_receipt_ids": list(research_memory_write_receipt_ids),
        "previous_revision_id": previous_revision_id,
        "canonical_bundle_sha256": canonical_bundle_sha256,
    }
    digest = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"thesis-revision:{digest}"


def _unified_diff(*, before: str, after: str, section_id: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{section_id}:previous",
            tofile=f"{section_id}:current",
            lineterm="",
        )
    )


def _content_sha256(content_md: str) -> str:
    return sha256(content_md.encode("utf-8")).hexdigest()


__all__ = [
    "ThesisRevisionBuildError",
    "ThesisRevisionBuilder",
]
