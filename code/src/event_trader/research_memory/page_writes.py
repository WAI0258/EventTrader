"""Direct file-backed research-memory page writes for analysis-owned updates."""

from __future__ import annotations

import re
from pathlib import Path

from event_trader.contracts._validators import normalize_content
from event_trader.contracts.ports import ResearchMemoryPageWritePort
from event_trader.contracts.research_memory import (
    PageRef,
    ResearchMemoryContractError,
    initialize_target_research_layout,
    resolve_page_path,
    resolve_page_ref,
)
from event_trader.research_memory.discipline import (
    fixed_target_entry_sections,
    validate_target_entry_page,
)
from event_trader.storage import WorkspaceLayout

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_ALLOWED_DIRECT_PAGE_KINDS = frozenset(
    {
        "thesis",
        "timeline",
        "risks",
        "watchlist",
        "topic",
        "entity",
        "source",
        "review",
    }
)
_ALLOWED_CREATE_PAGE_KINDS = frozenset({"topic", "entity", "source", "review"})
_FIXED_TARGET_ENTRY_PAGE_KINDS = frozenset({"thesis", "timeline", "risks", "watchlist"})


class ResearchMemoryPageWriteError(ValueError):
    """Raised when a page-write helper dependency is malformed."""


class ResearchMemoryPageWriteContractError(ResearchMemoryContractError):
    """Raised when a direct page write violates a recoverable write contract."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.recoverable = recoverable


class FileBackedResearchMemoryPageWriter(ResearchMemoryPageWritePort):
    """Apply direct markdown page writes only on approved canonical wiki pages."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise ResearchMemoryPageWriteError(
                "layout must be a WorkspaceLayout instance."
            )
        self._layout = layout

    def update_page_section(
        self,
        page_path: str,
        section_name: str,
        new_content_md: str,
    ) -> Path:
        """Rewrite one named markdown section inside an existing canonical page."""
        page_ref, target_path = self._existing_page_path_for_write(
            page_path,
            operation="update_page_section",
        )
        _ensure_direct_page_write_kind(
            page_ref.page_kind,
            operation="update_page_section",
        )
        normalized_section_name = _normalize_section_name(section_name)
        normalized_content = _normalize_markdown_page(
            new_content_md,
            field_name="new_content_md",
        )
        _ensure_existing_markdown_file(
            target_path,
            role_description="canonical markdown page",
        )
        existing = target_path.read_text(encoding="utf-8")
        if _is_fixed_target_entry_page(page_ref):
            heading_level = _fixed_target_entry_section_heading_level(
                page_ref,
                existing,
                normalized_section_name,
            )
            updated = _rewrite_named_section(
                existing,
                section_name=normalized_section_name,
                new_content_md=normalized_content,
                heading_level=heading_level,
            )
        else:
            updated = _rewrite_named_section(
                existing,
                section_name=normalized_section_name,
                new_content_md=normalized_content,
            )
        validated = _validate_page_write_content(page_ref, updated)
        target_path.write_text(validated, encoding="utf-8")
        return target_path.resolve(strict=False)

    def rewrite_page(self, page_path: str, new_content_md: str) -> Path:
        """Rewrite one existing canonical page without introducing a wiki engine."""
        page_ref, target_path = self._existing_page_path_for_write(
            page_path,
            operation="rewrite_page",
        )
        _ensure_direct_page_write_kind(page_ref.page_kind, operation="rewrite_page")
        normalized_content = _normalize_markdown_page(
            new_content_md,
            field_name="new_content_md",
        )
        _ensure_existing_markdown_file(
            target_path,
            role_description="canonical markdown page",
        )
        validated = _validate_page_write_content(page_ref, normalized_content)
        target_path.write_text(validated, encoding="utf-8")
        return target_path.resolve(strict=False)

    def create_page(self, page_path: str, initial_content_md: str) -> Path:
        """Create one new canonical dynamic page inside the research-memory library."""
        page_ref, target_path = self._canonical_page_path(page_path)
        _ensure_dynamic_page_create_kind(page_ref.page_kind)
        normalized_content = _normalize_markdown_page(
            initial_content_md,
            field_name="initial_content_md",
        )
        if target_path.exists():
            raise ResearchMemoryContractError(
                "create_page requires a new canonical markdown page, but found an "
                f"existing path at: {target_path.resolve(strict=False)}"
            )
        _ensure_parent_directory(
            target_path.parent,
            role_description="canonical page family directory",
        )
        target_path.write_text(normalized_content, encoding="utf-8")
        return target_path.resolve(strict=False)

    def _existing_page_path_for_write(
        self,
        page_path: str,
        *,
        operation: str,
    ) -> tuple[PageRef, Path]:
        page_ref, target_path = self._canonical_page_path(page_path)
        _ensure_existing_markdown_file(
            target_path,
            role_description=f"canonical markdown page for {operation}",
        )
        return page_ref, target_path

    def _canonical_page_path(self, page_path: str) -> tuple[PageRef, Path]:
        page_ref = resolve_page_ref(page_path)
        if page_ref.target_key is not None:
            initialize_target_research_layout(self._layout, page_ref.target_key)
        target_path = resolve_page_path(self._layout, page_ref.page_path)
        return page_ref, target_path


def _normalize_markdown_page(content: str, *, field_name: str) -> str:
    normalized = normalize_content(
        content,
        field_name=field_name,
        error_type=ResearchMemoryContractError,
    )
    if not normalized.endswith("\n"):
        return f"{normalized}\n"
    return normalized


def _normalize_section_name(section_name: str) -> str:
    if not isinstance(section_name, str):
        raise ResearchMemoryContractError("section_name must be a string.")
    normalized = section_name.strip()
    if not normalized:
        raise ResearchMemoryContractError("section_name must not be blank.")
    if normalized != section_name:
        raise ResearchMemoryContractError(
            "section_name must not include leading or trailing whitespace."
        )
    if any(ch in normalized for ch in ("\n", "\r")):
        raise ResearchMemoryContractError("section_name must stay on one line.")
    return normalized


def _ensure_existing_markdown_file(path: Path, *, role_description: str) -> None:
    if not path.exists():
        raise ResearchMemoryContractError(
            f"Missing required {role_description}: {path.resolve(strict=False)}"
        )
    if not path.is_file():
        raise ResearchMemoryContractError(
            f"Expected {role_description}, but found a directory at: {path.resolve(strict=False)}"
        )


def _ensure_parent_directory(path: Path, *, role_description: str) -> None:
    if not path.exists():
        raise ResearchMemoryContractError(
            f"Missing required {role_description}: {path.resolve(strict=False)}"
        )
    if not path.is_dir():
        raise ResearchMemoryContractError(
            f"Expected {role_description}, but found a file at: {path.resolve(strict=False)}"
        )


def _ensure_direct_page_write_kind(page_kind: str, *, operation: str) -> None:
    if page_kind in _ALLOWED_DIRECT_PAGE_KINDS:
        return
    if page_kind == "index":
        raise ResearchMemoryContractError(
            f"{operation} must not target canonical index.md directly; use "
            "update_index(scope_key, new_content_md) instead."
        )
    if page_kind == "log":
        raise ResearchMemoryContractError(
            f"{operation} must not target canonical log.md directly; use "
            "append_log_entry(scope_key, entry_md) instead."
        )
    raise ResearchMemoryContractError(
        f"{operation} is not allowed for canonical page kind '{page_kind}'."
    )


def _ensure_dynamic_page_create_kind(page_kind: str) -> None:
    if page_kind in _ALLOWED_CREATE_PAGE_KINDS:
        return
    if page_kind in {"index", "log"}:
        _ensure_direct_page_write_kind(page_kind, operation="create_page")
    raise ResearchMemoryContractError(
        "create_page may create only canonical dynamic pages under shared/topics, "
        "shared/entities, targets/<target_key>/topics, targets/<target_key>/sources, "
        "or targets/<target_key>/reviews; "
        f"canonical page kind '{page_kind}' is not creatable by this writer."
    )


def _validate_page_write_content(page_ref: PageRef, content_md: str) -> str:
    if not _is_fixed_target_entry_page(page_ref):
        return content_md
    try:
        return validate_target_entry_page(_page_name(page_ref), content_md)
    except ResearchMemoryContractError as exc:
        raise ResearchMemoryPageWriteContractError(
            str(exc),
            error_code="fixed_page_skeleton_violation",
            recoverable=True,
        ) from exc


def _is_fixed_target_entry_page(page_ref: PageRef) -> bool:
    return (
        page_ref.target_key is not None
        and page_ref.page_kind in _FIXED_TARGET_ENTRY_PAGE_KINDS
    )


def _fixed_target_entry_section_heading_level(
    page_ref: PageRef,
    content_md: str,
    section_name: str,
) -> int:
    allowed_sections = fixed_target_entry_sections(_page_name(page_ref))
    headings = _markdown_headings(content_md)
    if section_name in allowed_sections:
        top_level_matches = [
            heading
            for heading in headings
            if heading[1] == 2 and heading[2] == section_name
        ]
        if not top_level_matches:
            raise ResearchMemoryPageWriteContractError(
                f"Missing required section {section_name!r} in canonical markdown page.",
                error_code="missing_section",
                recoverable=True,
            )
        if len(top_level_matches) > 1:
            raise ResearchMemoryPageWriteContractError(
                f"section_name {section_name!r} is ambiguous inside the canonical markdown page.",
                error_code="ambiguous_section",
                recoverable=True,
            )
        return 2

    matches = [heading for heading in headings if heading[2] == section_name]
    if not matches:
        raise ResearchMemoryPageWriteContractError(
            f"Missing required section {section_name!r} in canonical markdown page.",
            error_code="missing_section",
            recoverable=True,
        )
    if len(matches) > 1:
        raise ResearchMemoryPageWriteContractError(
            f"section_name {section_name!r} is ambiguous inside the canonical markdown page.",
            error_code="ambiguous_section",
            recoverable=True,
        )

    _line_index, heading_level, _heading_text = matches[0]
    if heading_level == 1:
        raise ResearchMemoryPageWriteContractError(
            "update_page_section must not rewrite the fixed target page title.",
            error_code="page_title_write_forbidden",
            recoverable=True,
        )
    if heading_level == 2 and section_name not in allowed_sections:
        allowed = ", ".join(allowed_sections)
        raise ResearchMemoryPageWriteContractError(
            "update_page_section may rewrite only canonical top-level sections or "
            f"existing nested sections on {_page_name(page_ref)}: {allowed}.",
            error_code="invalid_top_level_section",
            recoverable=True,
        )
    return heading_level


def _markdown_headings(content_md: str) -> list[tuple[int, int, str]]:
    lines = content_md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        headings.append((index, len(match.group(1)), match.group(2).strip()))
    return headings


def _page_name(page_ref: PageRef) -> str:
    return Path(page_ref.page_path).name


def _rewrite_named_section(
    content_md: str,
    *,
    section_name: str,
    new_content_md: str,
    heading_level: int | None = None,
) -> str:
    lines = content_md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings = _markdown_headings(content_md)

    matches = [
        heading
        for heading in headings
        if heading[2] == section_name
        and (heading_level is None or heading[1] == heading_level)
    ]
    if not matches:
        raise ResearchMemoryContractError(
            f"Missing required section {section_name!r} in canonical markdown page."
        )
    if len(matches) > 1:
        raise ResearchMemoryContractError(
            f"section_name {section_name!r} is ambiguous inside the canonical markdown page."
        )

    start_index, current_heading_level, _ = matches[0]
    end_index = len(lines)
    for candidate_index, candidate_level, _ in headings:
        if candidate_index <= start_index:
            continue
        if candidate_level <= current_heading_level:
            end_index = candidate_index
            break

    replacement_lines = new_content_md.rstrip("\n").split("\n")
    before = lines[: start_index + 1]
    after = lines[end_index:]
    updated_lines = [*before, "", *replacement_lines]
    if after and after[0] != "":
        updated_lines.append("")
    updated_lines.extend(after)
    updated = "\n".join(updated_lines)
    if not updated.endswith("\n"):
        updated = f"{updated}\n"
    return updated


__all__ = [
    "FileBackedResearchMemoryPageWriter",
    "ResearchMemoryPageWriteContractError",
    "ResearchMemoryPageWriteError",
]
