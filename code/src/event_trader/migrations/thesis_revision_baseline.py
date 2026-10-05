"""One-time baseline migration for thesis revision audit history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from event_trader.contracts._validators import validate_target_key
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.builder import ThesisRevisionBuilder
from event_trader.thesis_revision.store import ThesisRevisionStore

DEFAULT_THESIS_REVISION_BASELINE_MIGRATION_ID = "thesis_revision_baseline_v1"


class ThesisRevisionBaselineMigrationError(ValueError):
    """Raised when the thesis revision baseline migration cannot run."""


@dataclass(frozen=True, slots=True)
class ThesisRevisionBaselineMigrationResult:
    migration_id: str
    revision_ids_by_target: dict[str, str]
    revision_paths_by_target: dict[str, Path]


def run_thesis_revision_baseline_migration(
    *,
    layout: WorkspaceLayout,
    target_keys: tuple[str, ...],
    migrated_at: datetime,
    migration_id: str = DEFAULT_THESIS_REVISION_BASELINE_MIGRATION_ID,
) -> ThesisRevisionBaselineMigrationResult:
    if not isinstance(layout, WorkspaceLayout):
        raise ThesisRevisionBaselineMigrationError(
            "layout must be a WorkspaceLayout instance."
        )
    if not isinstance(migrated_at, datetime) or migrated_at.tzinfo is None:
        raise ThesisRevisionBaselineMigrationError(
            "migrated_at must be a timezone-aware datetime."
        )
    if not isinstance(migration_id, str) or not migration_id.strip():
        raise ThesisRevisionBaselineMigrationError("migration_id must be non-blank.")
    normalized_targets = _normalize_target_keys(target_keys)
    store = ThesisRevisionStore(layout)
    builder = ThesisRevisionBuilder(layout=layout, store=store)
    revision_ids_by_target: dict[str, str] = {}
    revision_paths_by_target: dict[str, Path] = {}
    for target_key in normalized_targets:
        existing = store.read_records(target_key=target_key)
        if existing:
            if all(item.record.source == "migration_baseline" for item in existing):
                latest = store.read_latest(target_key=target_key)
                if latest is None:
                    raise ThesisRevisionBaselineMigrationError(
                        "existing thesis revision baseline records could not be read."
                    )
                revision_ids_by_target[target_key] = latest.record.revision_id
                revision_paths_by_target[target_key] = latest.path
                continue
            raise ThesisRevisionBaselineMigrationError(
                "thesis revision baseline migration refuses to fabricate history for "
                f"{target_key}; revisions already exist."
            )
        revision = builder.build(
            target_key=target_key,
            business_at=migrated_at,
            committed_at=migrated_at,
            source="migration_baseline",
            analysis_assessment=None,
            context_packet_id=None,
            context_packet_hash=None,
            source_event_ids=(),
            research_memory_write_receipt_ids=(),
            committed_write_receipts=(),
        )
        path = store.append(revision)
        revision_ids_by_target[target_key] = revision.revision_id
        revision_paths_by_target[target_key] = path
    return ThesisRevisionBaselineMigrationResult(
        migration_id=migration_id.strip(),
        revision_ids_by_target=revision_ids_by_target,
        revision_paths_by_target=revision_paths_by_target,
    )


def _normalize_target_keys(target_keys: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(target_keys, tuple) or not target_keys:
        raise ThesisRevisionBaselineMigrationError("target_keys must be a non-empty tuple.")
    seen: set[str] = set()
    normalized: list[str] = []
    for target_key in target_keys:
        text = validate_target_key(
            target_key,
            error_type=ThesisRevisionBaselineMigrationError,
        )
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)


__all__ = [
    "DEFAULT_THESIS_REVISION_BASELINE_MIGRATION_ID",
    "ThesisRevisionBaselineMigrationError",
    "ThesisRevisionBaselineMigrationResult",
    "run_thesis_revision_baseline_migration",
]
