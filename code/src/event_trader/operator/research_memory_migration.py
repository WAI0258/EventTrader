"""Research-memory citation migration helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from event_trader.evidence_ledger import FileBackedEvidenceLedger, FileBackedEvidenceLedgerError
from event_trader.integrations.analysis_citations import (
    canonicalize_analysis_citations,
    extract_analysis_event_ids,
)
from event_trader.storage import WorkspaceLayout


class ResearchMemoryMigrationError(ValueError):
    """Raised when research-memory citation migration cannot preserve provenance."""


@dataclass(frozen=True, slots=True)
class ResearchMemoryCitationMigrationReceipt:
    """One research-memory page inspected for citation normalization."""

    page_path: str
    changed: bool
    event_ids: tuple[str, ...]


def migrate_research_memory_citations(
    *,
    layout: WorkspaceLayout,
    apply: bool,
) -> tuple[ResearchMemoryCitationMigrationReceipt, ...]:
    """Normalize canonical research-memory citations using ledger truth."""
    if not isinstance(layout, WorkspaceLayout):
        raise ResearchMemoryMigrationError("layout must be a WorkspaceLayout instance.")
    if not isinstance(apply, bool):
        raise ResearchMemoryMigrationError("apply must be a boolean.")
    if not layout.research_memory_root.exists():
        return ()
    if not layout.research_memory_root.is_dir():
        raise ResearchMemoryMigrationError(
            f"research_memory root must be a directory: {layout.research_memory_root}"
        )

    ledger = FileBackedEvidenceLedger(layout)
    receipts: list[ResearchMemoryCitationMigrationReceipt] = []
    for page_file in sorted(layout.research_memory_root.rglob("*.md")):
        content_md = page_file.read_text(encoding="utf-8")
        event_ids = extract_analysis_event_ids(content_md)
        source_refs_by_event_id = _source_refs_for_event_ids(
            ledger=ledger,
            event_ids=event_ids,
            page_file=page_file,
        )
        normalized_content_md = canonicalize_analysis_citations(
            content_md,
            source_refs_by_event_id,
        )
        changed = normalized_content_md != content_md
        if changed and apply:
            page_file.write_text(normalized_content_md, encoding="utf-8", newline="\n")
        receipts.append(
            ResearchMemoryCitationMigrationReceipt(
                page_path=page_file.relative_to(layout.research_memory_root).as_posix(),
                changed=changed,
                event_ids=event_ids,
            )
        )
    return tuple(receipts)


def _source_refs_for_event_ids(
    *,
    ledger: FileBackedEvidenceLedger,
    event_ids: tuple[str, ...],
    page_file: Path,
) -> dict[str, str]:
    if not event_ids:
        return {}
    try:
        records = ledger.read_many(list(event_ids))
    except FileBackedEvidenceLedgerError as exc:
        raise ResearchMemoryMigrationError(
            "Cannot migrate research-memory citations because ledger truth is "
            f"missing for at least one event_id referenced in {page_file}."
        ) from exc
    return {record.event_id: record.source_ref for record in records}


__all__ = [
    "ResearchMemoryCitationMigrationReceipt",
    "ResearchMemoryMigrationError",
    "migrate_research_memory_citations",
]
