"""Provider-neutral citation contract for analysis-owned memory writes."""

from __future__ import annotations

from event_trader.analysis import AnalysisContext
from event_trader.integrations.analysis_citations import (
    canonicalize_analysis_citations,
    extract_analysis_citations,
    extract_noncanonical_analysis_event_ids,
)
from event_trader.integrations.bounded_context import (
    DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
    bounded_text_payload,
)


class AnalysisWriteCitationError(RuntimeError):
    """Raised when analysis write citations violate the business contract."""


def collect_analysis_context_citations(
    *,
    context: AnalysisContext,
    receipts: tuple[dict[str, object], ...],
    active_citations: dict[str, str],
) -> dict[str, str]:
    """Collect citations actually visible through memory and read receipts."""

    citations: dict[str, str] = {}
    for page in (
        *context.memory_context.target_pages,
        *context.memory_context.shared_pages,
    ):
        visible_content = bounded_text_payload(
            page.content_md,
            limit=DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
        )["excerpt"]
        if not isinstance(visible_content, str):
            raise AnalysisWriteCitationError("bounded analysis page excerpt must be a string.")
        add_analysis_context_citations(
            citations,
            extract_analysis_citation_pairs(visible_content),
            active_citations=active_citations,
        )
    for receipt in receipts:
        if receipt.get("result_mode") == "duplicate":
            continue
        operation = _require_receipt_text(receipt, "operation")
        if operation in {"read_page", "read_section", "read_around_citation"}:
            add_analysis_context_citations(
                citations,
                extract_analysis_citation_pairs(_require_receipt_string(receipt, "content_md")),
                active_citations=active_citations,
            )
            continue
        if operation == "search_wiki":
            add_analysis_context_citations(
                citations,
                _extract_search_wiki_citations(receipt),
                active_citations=active_citations,
            )
            continue
        if operation == "read_evidence":
            add_analysis_context_citations(
                citations,
                _extract_read_evidence_citations(receipt),
                active_citations=active_citations,
            )
            continue
    return citations


def validate_analysis_write_citations(
    *,
    content_md: str,
    active_citations: dict[str, str],
    context_citations: dict[str, str],
) -> None:
    """Require canonical, admitted citations with at least one active event."""

    noncanonical_event_ids = set(extract_noncanonical_analysis_event_ids(content_md))
    if noncanonical_event_ids:
        raise AnalysisWriteCitationError(
            "Analysis write content contained noncanonical event_id references: "
            f"{', '.join(sorted(noncanonical_event_ids))}. "
            "Use canonical markdown citations only: `event_id` | `source_ref`."
        )
    citations = extract_analysis_citation_pairs(content_md)
    if not citations:
        raise AnalysisWriteCitationError(
            "Analysis write content must include readable citations in the form "
            "`event_id` | `source_ref`."
        )
    unknown_event_ids = {
        event_id
        for event_id, _source_ref in citations
        if event_id not in active_citations and event_id not in context_citations
    }
    if unknown_event_ids:
        raise AnalysisWriteCitationError(
            "Analysis write content cited event_ids that were neither active nor "
            f"loaded as analysis context: {', '.join(sorted(unknown_event_ids))}"
        )
    if not any(event_id in active_citations for event_id, _source_ref in citations):
        raise AnalysisWriteCitationError(
            "Analysis write content must cite at least one active request event_id / "
            "source_ref pairs."
        )


def canonicalize_analysis_write_citations(
    *,
    content_md: str,
    active_citations: dict[str, str],
    context_citations: dict[str, str],
) -> str:
    return canonicalize_analysis_citations(
        content_md,
        {
            **context_citations,
            **active_citations,
        },
    )


def extract_analysis_citation_pairs(content_md: str) -> set[tuple[str, str]]:
    return {
        (citation.event_id, citation.source_ref)
        for citation in extract_analysis_citations(content_md)
    }


def add_analysis_context_citations(
    citations: dict[str, str],
    pairs: set[tuple[str, str]],
    *,
    active_citations: dict[str, str],
) -> None:
    for event_id, source_ref in pairs:
        if event_id in active_citations:
            continue
        existing_source_ref = citations.get(event_id)
        if existing_source_ref is None:
            citations[event_id] = source_ref
            continue
        if existing_source_ref != source_ref:
            raise AnalysisWriteCitationError(
                "Analysis context contains ambiguous source_ref values for "
                f"event_id {event_id}: {existing_source_ref!r} and {source_ref!r}."
            )


def _extract_search_wiki_citations(
    receipt: dict[str, object],
) -> set[tuple[str, str]]:
    matches = receipt.get("matches")
    if not isinstance(matches, list):
        raise AnalysisWriteCitationError(
            "Analysis search_wiki receipt field 'matches' must be a list."
        )
    citations: set[tuple[str, str]] = set()
    for index, match in enumerate(matches):
        if not isinstance(match, dict):
            raise AnalysisWriteCitationError(
                f"Analysis search_wiki receipt match {index} must be an object."
            )
        snippet = match.get("snippet")
        if not isinstance(snippet, str):
            raise AnalysisWriteCitationError(
                f"Analysis search_wiki receipt match {index}.snippet must be a string."
            )
        citations.update(extract_analysis_citation_pairs(snippet))
    return citations


def _extract_read_evidence_citations(
    receipt: dict[str, object],
) -> set[tuple[str, str]]:
    records = receipt.get("records")
    if not isinstance(records, list):
        raise AnalysisWriteCitationError(
            "Analysis read_evidence receipt field 'records' must be a list."
        )
    citations: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise AnalysisWriteCitationError(
                f"Analysis read_evidence receipt record {index} must be an object."
            )
        event_id = record.get("event_id")
        source_ref = record.get("source_ref")
        if not isinstance(event_id, str) or not event_id.strip():
            raise AnalysisWriteCitationError(
                f"Analysis read_evidence receipt record {index}.event_id must be a "
                "non-blank string."
            )
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise AnalysisWriteCitationError(
                f"Analysis read_evidence receipt record {index}.source_ref must be a "
                "non-blank string."
            )
        citations.add((event_id.strip(), source_ref.strip()))
    return citations


def _require_receipt_text(receipt: dict[str, object], field_name: str) -> str:
    value = receipt.get(field_name)
    if not isinstance(value, str):
        raise AnalysisWriteCitationError(
            f"Analysis write receipt field {field_name!r} must be a string."
        )
    normalized = value.strip()
    if not normalized:
        raise AnalysisWriteCitationError(
            f"Analysis write receipt field {field_name!r} must not be blank."
        )
    return normalized


def _require_receipt_string(receipt: dict[str, object], field_name: str) -> str:
    value = receipt.get(field_name)
    if not isinstance(value, str):
        raise AnalysisWriteCitationError(f"Analysis receipt field {field_name!r} must be a string.")
    return value


__all__ = [
    "AnalysisWriteCitationError",
    "add_analysis_context_citations",
    "canonicalize_analysis_write_citations",
    "collect_analysis_context_citations",
    "extract_analysis_citation_pairs",
    "validate_analysis_write_citations",
]
