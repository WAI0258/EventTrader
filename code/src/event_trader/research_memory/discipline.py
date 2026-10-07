"""Pure structural markdown-discipline helpers for research-memory writes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from event_trader.contracts import research_memory as research_memory_contracts
from event_trader.contracts.research_memory import ResearchMemoryContractError

ScopeKind = Literal["shared", "target"]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_LIST_MARKER_RE = re.compile(r"^(?:[-*+]\s+|\d+\.\s+)")
_INDEX_LIST_ENTRY_MAX_WORDS = 24
_FIXED_TARGET_ENTRY_PAGES = frozenset(
    {"thesis.md", "timeline.md", "risks.md", "watchlist.md"}
)
_INDEX_SECTIONS = (
    "Current Reading Path",
    "Core Pages",
    "Active Topics",
    "Key References",
)
_SHARED_INDEX_BOOTSTRAP_CONTENT = "# shared index\n"
_SHARED_LOG_BOOTSTRAP_CONTENT = "# shared log\n"
_SHARED_LOG_TITLES = frozenset({"shared log", "Shared Log"})


@dataclass(frozen=True, slots=True)
class MarkdownHeading:
    """One markdown heading with normalized text and source line number."""

    level: int
    text: str
    line_number: int


def normalize_markdown(content_md: str, *, field_name: str) -> str:
    """Normalize markdown newlines and ensure the payload is non-blank."""
    if not isinstance(content_md, str):
        raise ResearchMemoryContractError(f"{field_name} must be a string.")

    normalized = content_md.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise ResearchMemoryContractError(f"{field_name} must not be blank.")
    if not normalized.endswith("\n"):
        normalized = f"{normalized}\n"
    return normalized


def extract_markdown_headings(
    content_md: str,
    *,
    field_name: str,
) -> tuple[MarkdownHeading, ...]:
    """Extract normalized markdown headings from the given payload."""
    normalized = normalize_markdown(content_md, field_name=field_name)
    headings: list[MarkdownHeading] = []
    for line_number, line in enumerate(normalized.splitlines(), start=1):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        headings.append(
            MarkdownHeading(
                level=len(match.group(1)),
                text=match.group(2).strip(),
                line_number=line_number,
            )
        )
    return tuple(headings)


def validate_target_entry_page(
    page_name: str,
    content_md: str,
) -> str:
    """Validate one fixed target entry page against the contract-derived heading set."""
    normalized = normalize_markdown(content_md, field_name="content_md")
    expected_headings = _required_headings_for_page(page_name)
    actual_headings = tuple(
        heading.text
        for heading in extract_markdown_headings(normalized, field_name="content_md")
        if heading.level <= 2
    )
    _validate_required_heading_sequence(
        page_name=page_name,
        expected_headings=expected_headings,
        actual_headings=actual_headings,
    )
    return normalized


def fixed_target_entry_sections(page_name: str) -> tuple[str, ...]:
    """Return the canonical editable top-level sections for one fixed target page."""
    return _required_headings_for_page(page_name)[1:]


def validate_index_page(
    content_md: str,
    *,
    scope_kind: ScopeKind,
) -> str:
    """Validate target/shared index.md content as a navigational markdown map."""
    normalized = normalize_markdown(content_md, field_name="content_md")
    if scope_kind == "shared" and normalized == _SHARED_INDEX_BOOTSTRAP_CONTENT:
        return normalized

    actual_headings = tuple(
        heading.text
        for heading in extract_markdown_headings(normalized, field_name="content_md")
        if heading.level <= 2
    )
    first_heading = actual_headings[0] if actual_headings else None
    if first_heading not in {"Index", "Shared Index"}:
        raise ResearchMemoryContractError(
            "index.md must start with '# Index' or '# Shared Index', or preserve the "
            "shared bootstrap content '# shared index'."
        )

    expected_headings = (first_heading, *_INDEX_SECTIONS)
    _validate_required_heading_sequence(
        page_name="index.md",
        expected_headings=expected_headings,
        actual_headings=actual_headings,
    )
    _validate_index_bodies_are_navigational(normalized)
    return normalized


def validate_log_append_entry(entry_md: str) -> str:
    """Validate one appendable log entry without allowing page-title rewrites."""
    normalized = normalize_markdown(entry_md, field_name="entry_md")
    headings = extract_markdown_headings(normalized, field_name="entry_md")

    if not headings:
        raise ResearchMemoryContractError(
            "entry_md must include one '##' heading for the appended log entry."
        )
    if any(heading.level == 1 for heading in headings):
        raise ResearchMemoryContractError(
            "entry_md must not introduce a new '# ' page title; append only one '##' "
            "entry under the existing log page."
        )

    second_level_headings = [heading for heading in headings if heading.level == 2]
    if len(second_level_headings) != 1:
        raise ResearchMemoryContractError(
            "entry_md must contain exactly one top-level '##' heading for the appended "
            "log entry."
        )
    if headings[0].level != 2:
        raise ResearchMemoryContractError(
            "entry_md must start with a '##' heading before any lower-level subheadings."
        )

    body_lines = [line for line in normalized.splitlines()[1:] if line.strip()]
    if not body_lines:
        raise ResearchMemoryContractError(
            "entry_md must include non-heading body content after the '##' log heading."
        )
    return normalized


def append_log_entry(
    existing_content_md: str,
    entry_md: str,
    *,
    scope_kind: ScopeKind,
) -> str:
    """Validate a log page plus one append entry and return the combined markdown."""
    base = validate_log_page(existing_content_md, scope_kind=scope_kind)
    entry = validate_log_append_entry(entry_md)
    return f"{base.rstrip()}\n\n{entry}"


def validate_log_page(
    content_md: str,
    *,
    scope_kind: ScopeKind,
) -> str:
    """Validate an existing log.md page while preserving shared bootstrap content."""
    normalized = normalize_markdown(content_md, field_name="content_md")
    if scope_kind == "shared" and normalized == _SHARED_LOG_BOOTSTRAP_CONTENT:
        return normalized

    headings = extract_markdown_headings(normalized, field_name="content_md")
    if not headings:
        raise ResearchMemoryContractError(
            "log.md must start with a title heading or preserve the shared bootstrap "
            "content '# shared log'."
        )

    title_heading = headings[0]
    if title_heading.level != 1:
        raise ResearchMemoryContractError(
            "log.md must start with a single '# ' title heading before appended entries."
        )

    if scope_kind == "target":
        if title_heading.text != "Log":
            raise ResearchMemoryContractError("target log.md must start with '# Log'.")
    elif title_heading.text not in _SHARED_LOG_TITLES:
        raise ResearchMemoryContractError(
            "shared log.md must start with '# shared log' or '# Shared Log'."
        )

    second_level_headings = [heading for heading in headings[1:] if heading.level == 2]
    if any(heading.level == 1 for heading in headings[1:]):
        raise ResearchMemoryContractError(
            "log.md must remain one page with one title; appended entries must not add "
            "another '# ' heading."
        )
    if not second_level_headings:
        return normalized

    _validate_log_entries_follow_title(normalized, second_level_headings)
    return normalized


def shared_index_bootstrap_content() -> str:
    """Return the current shared/index.md bootstrap content."""
    return _SHARED_INDEX_BOOTSTRAP_CONTENT


def shared_log_bootstrap_content() -> str:
    """Return the current shared/log.md bootstrap content."""
    return _SHARED_LOG_BOOTSTRAP_CONTENT


def _required_headings_for_page(page_name: str) -> tuple[str, ...]:
    if page_name not in _FIXED_TARGET_ENTRY_PAGES:
        allowed_pages = ", ".join(sorted(_FIXED_TARGET_ENTRY_PAGES))
        raise ResearchMemoryContractError(
            f"page_name must be one of the fixed target entry pages: {allowed_pages}."
        )

    template = research_memory_contracts.target_page_template(page_name)
    return tuple(
        heading.text
        for heading in extract_markdown_headings(template, field_name=page_name)
        if heading.level <= 2
    )


def _validate_required_heading_sequence(
    *,
    page_name: str,
    expected_headings: tuple[str, ...],
    actual_headings: tuple[str, ...],
) -> None:
    if not actual_headings:
        raise ResearchMemoryContractError(
            f"{page_name} must include a title heading and the required top-level sections."
        )

    expected_title = expected_headings[0]
    if actual_headings[0] != expected_title:
        raise ResearchMemoryContractError(
            f"{page_name} must start with '# {expected_title}'."
        )

    actual_sections = actual_headings[1:]
    expected_sections = expected_headings[1:]

    for required_section in expected_sections:
        count = actual_sections.count(required_section)
        if count > 1:
            raise ResearchMemoryContractError(
                f"{page_name} must not repeat required section '{required_section}'."
            )

    missing_sections = [
        section for section in expected_sections if section not in actual_sections
    ]
    if missing_sections:
        missing = ", ".join(missing_sections)
        raise ResearchMemoryContractError(
            f"{page_name} is missing required top-level section(s): {missing}."
        )

    unexpected_sections = [
        section for section in actual_sections if section not in expected_sections
    ]
    if unexpected_sections:
        unexpected = ", ".join(unexpected_sections)
        raise ResearchMemoryContractError(
            f"{page_name} must not add unexpected top-level section(s): {unexpected}. "
            f"Allowed top-level headings are: "
            f"{_format_allowed_top_level_headings(expected_headings)}."
        )

    if actual_sections != expected_sections:
        expected_order = " -> ".join(expected_sections)
        raise ResearchMemoryContractError(
            f"{page_name} must preserve required top-level section order: {expected_order}."
        )


def _format_allowed_top_level_headings(expected_headings: tuple[str, ...]) -> str:
    return "; ".join(
        f"{'#' if index == 0 else '##'} {heading}"
        for index, heading in enumerate(expected_headings)
    )


def _validate_index_bodies_are_navigational(content_md: str) -> None:
    current_section: str | None = None
    inside_list_item = False
    current_list_item_word_count = 0

    for line in content_md.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _HEADING_RE.match(line)
        if match is not None:
            level = len(match.group(1))
            if level == 1:
                current_section = None
                inside_list_item = False
                current_list_item_word_count = 0
                continue
            if level == 2:
                current_section = match.group(2).strip()
                inside_list_item = False
                current_list_item_word_count = 0
                continue
            if current_section is None:
                raise ResearchMemoryContractError(
                    "index.md subheadings must stay under one of the documented navigational "
                    "sections."
                )
            continue
        if current_section is None:
            raise ResearchMemoryContractError(
                "index.md must not include prose outside the documented navigational sections."
            )
        if _LIST_MARKER_RE.match(line):
            inside_list_item = True
            current_list_item_word_count = _count_index_entry_words(line)
            if current_list_item_word_count == 0:
                raise ResearchMemoryContractError(
                    "index.md list entries must include list item content."
                )
            _validate_index_entry_brevity(
                current_list_item_word_count,
                current_section,
            )
            continue
        if line.startswith(("  ", "\t")) and inside_list_item:
            current_list_item_word_count += _count_index_entry_words(line)
            _validate_index_entry_brevity(
                current_list_item_word_count,
                current_section,
            )
            continue
        if inside_list_item:
            inside_list_item = False
        raise ResearchMemoryContractError(
            "index.md must stay navigational: non-heading content must be list-shaped "
            "entries, not thesis-style prose paragraphs."
        )


def _count_index_entry_words(line: str) -> int:
    normalized = line.strip()
    if _LIST_MARKER_RE.match(normalized):
        normalized = _LIST_MARKER_RE.sub("", normalized, count=1).strip()
    if not normalized:
        return 0
    return len([word for word in normalized.split() if word.strip()])


def _validate_index_entry_brevity(word_count: int, section_name: str | None) -> None:
    if word_count <= _INDEX_LIST_ENTRY_MAX_WORDS:
        return
    section = section_name or "unknown"
    raise ResearchMemoryContractError(
        f"index.md list entry in section `{section}` is too verbose "
        f"(max {_INDEX_LIST_ENTRY_MAX_WORDS} words)."
    )


def _validate_log_entries_follow_title(
    content_md: str,
    second_level_headings: list[MarkdownHeading],
) -> None:
    lines = content_md.splitlines()
    entry_starts = {heading.line_number for heading in second_level_headings}
    current_entry_start: int | None = None
    entry_has_body: dict[int, bool] = {
        heading.line_number: False for heading in second_level_headings
    }

    for line_number, line in enumerate(lines[1:], start=2):
        stripped = line.strip()
        if not stripped:
            continue
        match = _HEADING_RE.match(line)
        if match is not None and len(match.group(1)) == 2:
            current_entry_start = (
                line_number if line_number in entry_starts else current_entry_start
            )
            continue
        if current_entry_start is None:
            raise ResearchMemoryContractError(
                "log.md content must appear under '##' entry headings after the page title."
            )
        entry_has_body[current_entry_start] = True

    empty_entries = [
        heading.text
        for heading in second_level_headings
        if not entry_has_body.get(heading.line_number, False)
    ]
    if empty_entries:
        raise ResearchMemoryContractError(
            "log.md entries must include body content after each '##' heading: "
            + ", ".join(empty_entries)
        )


__all__ = [
    "MarkdownHeading",
    "append_log_entry",
    "extract_markdown_headings",
    "fixed_target_entry_sections",
    "normalize_markdown",
    "shared_index_bootstrap_content",
    "shared_log_bootstrap_content",
    "validate_index_page",
    "validate_log_append_entry",
    "validate_log_page",
    "validate_target_entry_page",
]
