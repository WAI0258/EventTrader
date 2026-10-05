"""Deterministic markdown context helpers for bounded agent working-memory views."""

from __future__ import annotations

import re
from dataclasses import dataclass

from event_trader.contracts._validators import validate_event_id
from event_trader.integrations.bounded_context import (
    DEFAULT_EVIDENCE_EXCERPT_CHAR_LIMIT,
    DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
    bounded_text_payload,
)

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+([^\r\n]+?)[ \t]*$", re.MULTILINE)
DEFAULT_SECTION_EXCERPT_CHAR_LIMIT = DEFAULT_PAGE_EXCERPT_CHAR_LIMIT
DEFAULT_CITATION_NEIGHBORHOOD_CHAR_LIMIT = 600
DEFAULT_CITATION_EXCERPT_CHAR_LIMIT = DEFAULT_EVIDENCE_EXCERPT_CHAR_LIMIT


class MarkdownContextError(ValueError):
    """Raised when markdown context helpers receive invalid inputs."""


@dataclass(frozen=True, slots=True)
class _HeadingSpan:
    text: str
    level: int
    start_offset: int
    content_start_offset: int


def build_markdown_page_map(
    content_md: str,
    *,
    excerpt_char_limit: int = DEFAULT_SECTION_EXCERPT_CHAR_LIMIT,
) -> tuple[dict[str, object], ...]:
    """Build deterministic heading-bounded section maps for one markdown page."""
    normalized_content = _require_text(content_md, field_name="content_md")
    limit = _require_positive_int(excerpt_char_limit, field_name="excerpt_char_limit")

    headings = _parse_headings(normalized_content)
    if not headings:
        return ()

    page_map: list[dict[str, object]] = []
    for index, heading in enumerate(headings):
        section_end_offset = _section_end_offset(headings, index, len(normalized_content))
        section_content = normalized_content[
            heading.content_start_offset : section_end_offset
        ]
        bounded = bounded_text_payload(section_content, limit=limit)
        page_map.append(
            {
                "heading": heading.text,
                "level": heading.level,
                "start_offset": heading.start_offset,
                "end_offset": section_end_offset,
                "content_start_offset": heading.content_start_offset,
                "content_end_offset": section_end_offset,
                "content_excerpt": bounded["excerpt"],
                "content_sha256": bounded["sha256"],
                "content_char_count": bounded["char_count"],
                "content_truncated": bounded["truncated"],
            }
        )
    return tuple(page_map)


def find_markdown_section(
    content_md: str,
    *,
    heading: str,
    heading_level: int | None = None,
    excerpt_char_limit: int = DEFAULT_SECTION_EXCERPT_CHAR_LIMIT,
) -> dict[str, object] | None:
    """Return one heading-bounded section map by heading text, or None if absent."""
    normalized_heading = _require_heading_text(heading)
    if heading_level is not None and (
        not isinstance(heading_level, int) or heading_level <= 0 or heading_level > 6
    ):
        raise MarkdownContextError("heading_level must be an integer in range 1..6.")

    sections = build_markdown_page_map(
        content_md,
        excerpt_char_limit=excerpt_char_limit,
    )
    matches = [
        section
        for section in sections
        if section["heading"] == normalized_heading
        and (heading_level is None or section["level"] == heading_level)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise MarkdownContextError(
            f"heading {normalized_heading!r} is ambiguous in markdown page."
        )
    return matches[0]


def build_citation_neighborhood(
    content_md: str,
    *,
    event_id: str | None = None,
    source_ref: str | None = None,
    neighborhood_char_limit: int = DEFAULT_CITATION_NEIGHBORHOOD_CHAR_LIMIT,
    excerpt_char_limit: int = DEFAULT_CITATION_EXCERPT_CHAR_LIMIT,
) -> dict[str, object] | None:
    """Return bounded neighborhood text around the first citation anchor match."""
    normalized_content = _require_text(content_md, field_name="content_md")
    neighborhood_limit = _require_positive_int(
        neighborhood_char_limit,
        field_name="neighborhood_char_limit",
    )
    excerpt_limit = _require_positive_int(
        excerpt_char_limit,
        field_name="excerpt_char_limit",
    )

    normalized_event_id: str | None = None
    normalized_source_ref: str | None = None
    if event_id is not None:
        if not isinstance(event_id, str):
            raise MarkdownContextError("event_id must be a string when provided.")
        normalized_event_id = validate_event_id(event_id, error_type=MarkdownContextError)
    if source_ref is not None:
        normalized_source_ref = _require_non_blank(source_ref, field_name="source_ref")
    if normalized_event_id is None and normalized_source_ref is None:
        raise MarkdownContextError("event_id or source_ref is required.")

    anchors = _citation_anchors(
        normalized_event_id=normalized_event_id,
        normalized_source_ref=normalized_source_ref,
    )
    anchor = _first_anchor_match(normalized_content, anchors)
    if anchor is None:
        return None

    anchor_text, anchor_start_offset = anchor
    anchor_end_offset = anchor_start_offset + len(anchor_text)

    half_window = neighborhood_limit // 2
    neighborhood_start_offset = max(0, anchor_start_offset - half_window)
    neighborhood_end_offset = min(
        len(normalized_content),
        anchor_end_offset + half_window,
    )
    neighborhood = normalized_content[neighborhood_start_offset:neighborhood_end_offset]
    bounded = bounded_text_payload(neighborhood, limit=excerpt_limit)

    return {
        "anchor_text": anchor_text,
        "anchor_start_offset": anchor_start_offset,
        "anchor_end_offset": anchor_end_offset,
        "neighborhood_start_offset": neighborhood_start_offset,
        "neighborhood_end_offset": neighborhood_end_offset,
        "content_excerpt": bounded["excerpt"],
        "content_sha256": bounded["sha256"],
        "content_char_count": bounded["char_count"],
        "content_truncated": bounded["truncated"],
    }


def _parse_headings(content_md: str) -> tuple[_HeadingSpan, ...]:
    headings: list[_HeadingSpan] = []
    for match in _HEADING_RE.finditer(content_md):
        heading_text = match.group(2).strip()
        if not heading_text:
            continue
        line_end_offset = match.end()
        if content_md.startswith("\r\n", line_end_offset):
            content_start_offset = line_end_offset + 2
        elif content_md.startswith("\n", line_end_offset) or content_md.startswith(
            "\r", line_end_offset
        ):
            content_start_offset = line_end_offset + 1
        else:
            content_start_offset = line_end_offset
        headings.append(
            _HeadingSpan(
                text=heading_text,
                level=len(match.group(1)),
                start_offset=match.start(),
                content_start_offset=content_start_offset,
            )
        )
    return tuple(headings)


def _section_end_offset(
    headings: tuple[_HeadingSpan, ...],
    heading_index: int,
    content_length: int,
) -> int:
    heading = headings[heading_index]
    for candidate in headings[heading_index + 1 :]:
        if candidate.level <= heading.level:
            return candidate.start_offset
    return content_length


def _citation_anchors(
    *,
    normalized_event_id: str | None,
    normalized_source_ref: str | None,
) -> tuple[str, ...]:
    anchors: list[str] = []
    if normalized_event_id is not None and normalized_source_ref is not None:
        anchors.append(f"`{normalized_event_id}` | `{normalized_source_ref}`")
        anchors.append(f"{normalized_event_id} | {normalized_source_ref}")
    if normalized_event_id is not None:
        anchors.append(f"`{normalized_event_id}`")
        anchors.append(normalized_event_id)
    if normalized_source_ref is not None:
        anchors.append(f"`{normalized_source_ref}`")
        anchors.append(normalized_source_ref)
    deduped = tuple(dict.fromkeys(anchors))
    return deduped


def _first_anchor_match(content_md: str, anchors: tuple[str, ...]) -> tuple[str, int] | None:
    best_anchor: str | None = None
    best_offset: int | None = None
    for anchor in anchors:
        offset = content_md.find(anchor)
        if offset < 0:
            continue
        if best_offset is None or offset < best_offset:
            best_anchor = anchor
            best_offset = offset
    if best_anchor is None or best_offset is None:
        return None
    return best_anchor, best_offset


def _require_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MarkdownContextError(f"{field_name} must be a string.")
    return value


def _require_non_blank(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MarkdownContextError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MarkdownContextError(f"{field_name} must not be blank.")
    return normalized


def _require_heading_text(value: object) -> str:
    if not isinstance(value, str):
        raise MarkdownContextError("heading must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MarkdownContextError("heading must not be blank.")
    if normalized != value:
        raise MarkdownContextError(
            "heading must not include leading or trailing whitespace."
        )
    if "\n" in normalized or "\r" in normalized:
        raise MarkdownContextError("heading must stay on one line.")
    return normalized


def _require_positive_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise MarkdownContextError(f"{field_name} must be a positive integer.")
    return value


__all__ = [
    "DEFAULT_CITATION_EXCERPT_CHAR_LIMIT",
    "DEFAULT_CITATION_NEIGHBORHOOD_CHAR_LIMIT",
    "DEFAULT_SECTION_EXCERPT_CHAR_LIMIT",
    "MarkdownContextError",
    "build_citation_neighborhood",
    "build_markdown_page_map",
    "find_markdown_section",
]
