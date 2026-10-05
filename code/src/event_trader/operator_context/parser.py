"""Parser for operator-owned context text blocks."""

from __future__ import annotations

import re
from typing import Literal

from event_trader.contracts._validators import normalize_content
from event_trader.contracts.research_memory import ResearchMemoryContractError
from event_trader.operator_context.contracts import (
    OperatorContextCard,
    OperatorContextContractError,
    OperatorContextWriteMode,
)
from event_trader.research_memory.discipline import (
    validate_target_entry_page as _validate_operator_template,
)

_CONTEXT_CARD_RE = re.compile(
    r"<!-- operator-context:start (?P<attrs>[^\n]+) -->\n"
    r"(?P<body>.*?)\n<!-- operator-context:end -->",
    re.DOTALL,
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)
_SECTION_NAME = "Context"
_ALLOWED_CARD_ATTRS = {"id"}
_OPTIONAL_CARD_ATTRS = {"is_retired"}

OperatorContextParseMode = Literal["context_card", "full"]


class OperatorContextParseError(OperatorContextContractError):
    """Raised when `operator.md` has malformed operator-context markup."""


def parse_operator_context_page(
    content_md: str,
    *,
    target_key: str | None = None,
    write_mode: OperatorContextWriteMode | None = None,
) -> tuple[OperatorContextCard, ...]:
    """
    Parse one complete operator page and validate its single context section.

    `target_key` and `write_mode` are accepted for compatibility with callers that
    validate operator pages during broader ResearchMemory workflows.
    """
    _ = target_key, write_mode
    normalized = _normalize_markdown(content_md, field_name="content_md")
    try:
        _validate_operator_template("operator.md", normalized)
    except ResearchMemoryContractError as exc:
        raise OperatorContextParseError(str(exc)) from exc
    sections = _parse_fixed_sections(normalized)
    _validate_fixed_sections(sections)
    return parse_operator_context_cards(content_md=normalized, sections=sections)


def parse_operator_context_cards(
    *,
    content_md: str,
    sections: tuple[tuple[str, int, int], ...] | None = None,
) -> tuple[OperatorContextCard, ...]:
    """Parse and validate all operator-context blocks in one file."""
    normalized = _normalize_markdown(content_md, field_name="content_md")
    section_spans = sections or _parse_fixed_sections(normalized)
    _validate_fixed_sections(section_spans)

    cards: list[OperatorContextCard] = []
    seen_card_ids: set[str] = set()
    for match in _CONTEXT_CARD_RE.finditer(normalized):
        attrs = _parse_card_attrs(match.group("attrs"), match.span(0))
        section_name = _section_for_offset(section_spans, match.start())
        if section_name != _SECTION_NAME:
            raise OperatorContextParseError(
                "operator-context block is not inside the Context section."
            )
        card_id = attrs["id"]
        if card_id in seen_card_ids:
            raise OperatorContextParseError(f"duplicate operator-context id {card_id!r}.")
        seen_card_ids.add(card_id)
        is_retired = attrs.get("is_retired", "false")
        cards.append(
            OperatorContextCard(
                card_id=card_id,
                section_name=section_name,
                body=_normalize_text(
                    match.group("body"),
                    field_name="body",
                    allow_blank=False,
                ),
                is_retired=(
                    _normalize_text(
                        is_retired,
                        field_name="is_retired",
                        allow_blank=False,
                    ).lower()
                    in {"true", "1", "yes"}
                ),
            )
        )
    return tuple(cards)


def render_operator_context_card_block(card: OperatorContextCard) -> str:
    """Render one deterministic operator-context block for markdown."""
    if not isinstance(card, OperatorContextCard):
        raise OperatorContextParseError("card must be an OperatorContextCard.")
    retired_suffix = " is_retired=true" if card.is_retired else ""
    return (
        f"<!-- operator-context:start id={card.card_id}{retired_suffix} -->\n"
        f"{card.body.rstrip()}\n"
        "<!-- operator-context:end -->"
    )


def _parse_fixed_sections(content_md: str) -> tuple[tuple[str, int, int], ...]:
    headings = list(_HEADING_RE.finditer(content_md))
    fixed_sections: list[tuple[str, int, int]] = []
    for index, match in enumerate(headings):
        if len(match.group(1)) != 2:
            continue
        section_name = match.group(2).strip()
        if section_name != _SECTION_NAME:
            continue
        section_start = match.end()
        section_end = len(content_md)
        for later in headings[index + 1 :]:
            if len(later.group(1)) == 2:
                section_end = later.start()
                break
        fixed_sections.append((section_name, section_start, section_end))
    return tuple(fixed_sections)


def _section_for_offset(
    section_spans: tuple[tuple[str, int, int], ...],
    offset: int,
) -> str | None:
    for section_name, start, end in section_spans:
        if start <= offset < end:
            return section_name
    return None


def _validate_fixed_sections(sections: tuple[tuple[str, int, int], ...]) -> None:
    if [section_name for section_name, _, _ in sections] != [_SECTION_NAME]:
        raise OperatorContextParseError(
            "operator.md fixed section order is invalid."
        )


def _parse_card_attrs(raw: str, span: tuple[int, int]) -> dict[str, str]:
    parts = raw.split()
    if not parts:
        raise OperatorContextParseError(
            f"operator-context attribute block is empty at {span}."
        )
    fields: dict[str, str] = {}
    for chunk in parts:
        key, equals, value = chunk.partition("=")
        if not equals:
            raise OperatorContextParseError(
                f"Malformed operator-context attribute at {span}: {chunk!r}."
            )
        if key in fields:
            raise OperatorContextParseError(
                f"Duplicate attribute {key!r} at {span}."
            )
        fields[key] = _strip_quoted_text(value)
    missing = _ALLOWED_CARD_ATTRS - set(fields)
    if missing:
        missing_attr = ", ".join(sorted(missing))
        raise OperatorContextParseError(
            f"operator-context block at {span} is missing required attributes: {missing_attr}."
        )
    unknown = set(fields) - (_ALLOWED_CARD_ATTRS | _OPTIONAL_CARD_ATTRS)
    if unknown:
        unknown_attr = ", ".join(sorted(unknown))
        raise OperatorContextParseError(
            f"operator-context block at {span} has unsupported attributes: {unknown_attr}."
        )
    is_retired = fields.get("is_retired")
    if is_retired is not None and (
        _normalize_text(is_retired, "is_retired", allow_blank=False).lower()
        not in {"true", "false", "1", "0", "yes", "no"}
    ):
        raise OperatorContextParseError(
            f"operator-context is_retired must be boolean-like at {span}."
        )
    return fields


def _normalize_text(value: object, field_name: str, allow_blank: bool) -> str:
    if not isinstance(value, str):
        raise OperatorContextParseError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not allow_blank and not normalized:
        raise OperatorContextParseError(f"{field_name} must not be blank.")
    return normalized


def _strip_quoted_text(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _normalize_markdown(content_md: str, field_name: str) -> str:
    return normalize_content(
        content_md,
        field_name=field_name,
        error_type=OperatorContextParseError,
    )


__all__ = [
    "OperatorContextParseMode",
    "OperatorContextParseError",
    "parse_operator_context_cards",
    "parse_operator_context_page",
    "render_operator_context_card_block",
]
