"""Thin business writer for canonical index and log research-memory writes."""

from __future__ import annotations

from pathlib import Path

from event_trader.contracts.ports import ResearchMemoryPort
from event_trader.contracts.research_memory import (
    ResearchMemoryContractError,
    initialize_target_research_layout,
    resolve_scope,
)
from event_trader.research_memory.discipline import (
    append_log_entry as append_validated_log_entry,
)
from event_trader.research_memory.discipline import (
    validate_index_page,
    validate_log_append_entry,
)
from event_trader.storage import WorkspaceLayout


class ResearchMemoryWrapperError(ValueError):
    """Raised when an index/log writer dependency is malformed."""


class FileBackedIndexLogWriter(ResearchMemoryPort):
    """Thin file-backed writer for the business meaning of index.md and log.md."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise ResearchMemoryWrapperError("layout must be a WorkspaceLayout instance.")
        self._layout = layout

    def update_index(self, scope_key: str, new_content_md: str) -> Path:
        """Rewrite the canonical index.md page for one documented scope."""
        scope = resolve_scope(self._layout, scope_key)
        normalized = validate_index_page(
            new_content_md,
            scope_kind=scope.scope_kind,
        )
        target_path = self._index_path_for_scope(scope)
        _ensure_existing_markdown_file(
            target_path,
            role_description="canonical index markdown file",
        )
        target_path.write_text(normalized, encoding="utf-8")
        return target_path.resolve(strict=False)

    def append_log_entry(self, scope_key: str, entry_md: str) -> Path:
        """Append one meaningful research-evolution entry to canonical log.md."""
        scope = resolve_scope(self._layout, scope_key)
        normalized_entry = validate_log_append_entry(entry_md)
        target_path = self._log_path_for_scope(scope)
        _ensure_existing_markdown_file(
            target_path,
            role_description="canonical log markdown file",
        )
        existing = target_path.read_text(encoding="utf-8")
        updated = append_validated_log_entry(
            existing,
            normalized_entry,
            scope_kind=scope.scope_kind,
        )
        target_path.write_text(updated, encoding="utf-8")
        return target_path.resolve(strict=False)

    def _index_path_for_scope(self, scope) -> Path:
        if scope.scope_kind == "shared":
            return scope.index_file

        initialize_target_research_layout(self._layout, scope.target_key or "")
        return scope.index_file

    def _log_path_for_scope(self, scope) -> Path:
        if scope.scope_kind == "shared":
            return scope.log_file

        initialize_target_research_layout(self._layout, scope.target_key or "")
        return scope.log_file


def _ensure_existing_markdown_file(path: Path, *, role_description: str) -> None:
    if not path.exists():
        raise ResearchMemoryContractError(
            f"Missing required {role_description}: {path.resolve(strict=False)}"
        )
    if not path.is_file():
        raise ResearchMemoryContractError(
            f"Expected {role_description}, but found a directory at: {path.resolve(strict=False)}"
        )


__all__ = ["FileBackedIndexLogWriter", "ResearchMemoryWrapperError"]
