"""Deterministic citation handling for analysis write tools."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from event_trader.contracts._validators import validate_event_id

_CITATION_RE = re.compile(r"`([0-9a-f]{24})`\s*\|\s*`([^`\r\n]+)`")
_EVENT_ID_REFERENCE_RE = re.compile(
    r"(?<![`0-9A-Za-z_/=:-])`?([0-9a-f]{24})`?(?![`0-9A-Za-z_/=:-])"
)


class AnalysisCitationError(ValueError):
    """Raised when analysis citations cannot be constructed."""


@dataclass(frozen=True, slots=True)
class AnalysisCitation:
    """One active evidence citation available to an analysis pass."""

    event_id: str
    source_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _validate_event_id(self.event_id))
        object.__setattr__(
            self,
            "source_ref",
            _validate_non_blank(self.source_ref, "source_ref"),
        )

    @property
    def markdown(self) -> str:
        return f"`{self.event_id}` | `{self.source_ref}`"


def serialize_analysis_citations(citations: Iterable[AnalysisCitation]) -> str:
    """Serialize active citations for the child MCP write server."""
    payload = [
        {"event_id": citation.event_id, "source_ref": citation.source_ref}
        for citation in citations
    ]
    if not payload:
        raise AnalysisCitationError("analysis citations must not be empty.")
    return json.dumps(payload, ensure_ascii=False)


def load_analysis_citations(raw_value: str | None) -> tuple[AnalysisCitation, ...]:
    """Load active citations from the parent-provided environment payload."""
    if raw_value is None or not raw_value.strip():
        raise AnalysisCitationError("analysis citation environment payload is required.")
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise AnalysisCitationError(
            "analysis citation environment payload must be valid JSON."
        ) from exc
    if not isinstance(payload, list) or not payload:
        raise AnalysisCitationError("analysis citation payload must be a non-empty list.")
    citations: list[AnalysisCitation] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise AnalysisCitationError(
                f"analysis citation payload item {index} must be an object."
            )
        citations.append(
            AnalysisCitation(
                event_id=_require_string(item.get("event_id"), f"item {index}.event_id"),
                source_ref=_require_string(item.get("source_ref"), f"item {index}.source_ref"),
            )
        )
    return tuple(citations)


def ensure_active_citation(content_md: str, citations: tuple[AnalysisCitation, ...]) -> str:
    """Return markdown content that contains at least one active citation."""
    if not isinstance(content_md, str):
        raise AnalysisCitationError("content_md must be a string.")
    if not citations:
        raise AnalysisCitationError("active citations must not be empty.")
    active_pairs = {(citation.event_id, citation.source_ref) for citation in citations}
    existing_pairs = {
        (citation.event_id, citation.source_ref)
        for citation in extract_analysis_citations(content_md)
    }
    if existing_pairs & active_pairs:
        return content_md
    suffix = f"\n\nEvidence: {citations[0].markdown}"
    if content_md.endswith("\n"):
        return f"{content_md.rstrip()}{suffix}\n"
    return f"{content_md}{suffix}"


def extract_analysis_citations(content_md: str) -> tuple[AnalysisCitation, ...]:
    """Extract canonical markdown evidence citations from content.

    Only the left side of `` `event_id` | `source_ref` `` is interpreted as an
    evidence id. The source_ref is opaque and may contain URL path fragments
    that happen to look like ids.
    """
    if not isinstance(content_md, str):
        raise AnalysisCitationError("content_md must be a string.")
    citations: list[AnalysisCitation] = []
    for event_id, source_ref in _CITATION_RE.findall(content_md):
        citations.append(
            AnalysisCitation(
                event_id=event_id.strip(),
                source_ref=source_ref.strip(),
            )
        )
    return tuple(citations)


def extract_analysis_event_ids(content_md: str) -> tuple[str, ...]:
    """Extract canonical citation event ids in stable first-seen order."""
    if not isinstance(content_md, str):
        raise AnalysisCitationError("content_md must be a string.")

    event_ids: list[str] = []
    seen: set[str] = set()

    for citation in extract_analysis_citations(content_md):
        event_id = _validate_event_id(citation.event_id)
        if event_id in seen:
            continue
        seen.add(event_id)
        event_ids.append(event_id)

    return tuple(event_ids)


def extract_noncanonical_analysis_event_ids(content_md: str) -> tuple[str, ...]:
    """Extract event id references that are not canonical citations."""
    if not isinstance(content_md, str):
        raise AnalysisCitationError("content_md must be a string.")
    masked_content = _mask_canonical_citation_spans(content_md)
    event_ids: list[str] = []
    seen: set[str] = set()
    for raw_event_id in _EVENT_ID_REFERENCE_RE.findall(masked_content):
        event_id = _validate_event_id(raw_event_id.strip())
        if event_id in seen:
            continue
        seen.add(event_id)
        event_ids.append(event_id)
    return tuple(event_ids)


def canonicalize_analysis_citations(
    content_md: str,
    source_refs_by_event_id: dict[str, str],
) -> str:
    """Rewrite known evidence references using event_id -> source_ref mapping."""
    if not isinstance(content_md, str):
        raise AnalysisCitationError("content_md must be a string.")
    if not isinstance(source_refs_by_event_id, dict):
        raise AnalysisCitationError("source_refs_by_event_id must be a dict.")
    normalized_content_md = _canonicalize_known_citation_pairs(
        content_md,
        source_refs_by_event_id,
    )
    normalized_content_md = _canonicalize_known_event_id_references(
        normalized_content_md,
        source_refs_by_event_id,
    )

    def replace(match: re.Match[str]) -> str:
        event_id = match.group(1).strip()
        source_ref = source_refs_by_event_id.get(event_id)
        if source_ref is None:
            return match.group(0)
        return f"`{event_id}` | `{source_ref}`"

    return _CITATION_RE.sub(replace, normalized_content_md)


def _canonicalize_known_citation_pairs(
    content_md: str,
    source_refs_by_event_id: dict[str, str],
) -> str:
    normalized_content_md = content_md
    for event_id, source_ref in source_refs_by_event_id.items():
        if not isinstance(event_id, str) or not isinstance(source_ref, str):
            continue
        normalized_event_id = event_id.strip()
        normalized_source_ref = source_ref.strip()
        if not normalized_event_id or not normalized_source_ref:
            continue
        pattern = re.compile(
            rf"(?<![`0-9A-Za-z_/=:-])`?{re.escape(normalized_event_id)}`?"
            rf"\s*\|\s*`?{re.escape(normalized_source_ref)}`?"
            rf"(?![`0-9A-Za-z_/=:-])"
        )
        replacement = f"`{normalized_event_id}` | `{normalized_source_ref}`"
        normalized_content_md = pattern.sub(replacement, normalized_content_md)
    return normalized_content_md


def _canonicalize_known_event_id_references(
    content_md: str,
    source_refs_by_event_id: dict[str, str],
) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _CITATION_RE.finditer(content_md):
        parts.append(
            _canonicalize_known_event_id_references_segment(
                content_md[cursor:match.start()],
                source_refs_by_event_id,
            )
        )
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(
        _canonicalize_known_event_id_references_segment(
            content_md[cursor:],
            source_refs_by_event_id,
        )
    )
    return "".join(parts)


def _canonicalize_known_event_id_references_segment(
    content_md: str,
    source_refs_by_event_id: dict[str, str],
) -> str:
    def replace(match: re.Match[str]) -> str:
        event_id = match.group(1).strip()
        source_ref = source_refs_by_event_id.get(event_id)
        if source_ref is None:
            return match.group(0)
        return f"`{event_id}` | `{source_ref}`"

    return _EVENT_ID_REFERENCE_RE.sub(replace, content_md)


def _mask_canonical_citation_spans(content_md: str) -> str:
    characters = list(content_md)
    for match in _CITATION_RE.finditer(content_md):
        start, end = match.span()
        characters[start:end] = " " * (end - start)
    return "".join(characters)


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AnalysisCitationError(f"{field_name} must be a string.")
    return _validate_non_blank(value, field_name)


def _validate_non_blank(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise AnalysisCitationError(f"{field_name} must not be blank.")
    return normalized


def _validate_event_id(value: str) -> str:
    return validate_event_id(value, error_type=AnalysisCitationError)
