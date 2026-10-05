"""Historical thesis snapshot reader for deterministic analysis-time reads."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from event_trader.contracts._validators import normalize_content, validate_target_key
from event_trader.contracts.research_memory import (
    PageReadResult,
    PageRef,
    ResearchMemoryContractError,
    WikiMatch,
    resolve_page_path,
    resolve_page_ref,
    target_page_template,
)
from event_trader.storage import WorkspaceLayout
from event_trader.thesis_revision.canonical_bundle import canonical_thesis_page_sections
from event_trader.thesis_revision.contracts import ThesisRevision
from event_trader.thesis_revision.store import (
    PersistedThesisRevision,
    ThesisRevisionStore,
)

_SUPPORTED_CANONICAL_PAGE_NAMES = (
    "index.md",
    "thesis.md",
    "risks.md",
    "watchlist.md",
    "timeline.md",
    "operator.md",
)
_THESIS_REVISION_PAGE_NAMES = (
    "thesis.md",
    "risks.md",
    "watchlist.md",
    "timeline.md",
)
_WIKI_SNIPPET_RADIUS = 120


class HistoricalThesisSnapshotReaderError(ValueError):
    """Raised when historical thesis snapshot reads are unsupported or invalid."""


class HistoricalThesisSnapshotReader:
    """Read only the canonical thesis bundle from persisted historical revisions."""

    def __init__(
        self,
        layout: WorkspaceLayout,
        business_at: datetime,
        store: ThesisRevisionStore | None = None,
    ) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise HistoricalThesisSnapshotReaderError(
                "layout must be a WorkspaceLayout instance."
            )
        if store is not None and not isinstance(store, ThesisRevisionStore):
            raise HistoricalThesisSnapshotReaderError(
                "store must be a ThesisRevisionStore instance."
            )
        self._layout = layout
        self._business_at = _normalize_business_at(business_at)
        self._store = store or ThesisRevisionStore(layout)
        self._selected_revisions: dict[str, PersistedThesisRevision | None] = {}

    def read_page(self, page_path: str) -> PageReadResult:
        page_ref = resolve_page_ref(page_path)
        page_name = _supported_page_name(page_ref)
        if page_name == "operator.md":
            return self._read_operator_page(page_ref)
        if page_name == "index.md":
            return PageReadResult(
                page_path=page_ref.page_path,
                content_md=target_page_template(page_name),
                snapshot_source="bootstrap_template",
            )
        persisted = self._resolve_revision(page_ref.target_key or "")
        content_md = (
            target_page_template(page_name)
            if persisted is None
            else render_historical_thesis_page(
                page_path=page_ref.page_path,
                revision=persisted.record,
            )
        )
        if persisted is None:
            return PageReadResult(
                page_path=page_ref.page_path,
                content_md=content_md,
                snapshot_source="bootstrap_template",
            )
        return PageReadResult(
            page_path=page_ref.page_path,
            content_md=content_md,
            snapshot_source="thesis_revision",
            snapshot_revision_id=persisted.record.revision_id,
            snapshot_committed_at=persisted.record.committed_at,
        )

    def list_pages(self, scope: str) -> list[PageRef]:
        target_key = _resolve_supported_scope(scope)
        return [
            resolve_page_ref(f"targets/{target_key}/{page_name}")
            for page_name in _SUPPORTED_CANONICAL_PAGE_NAMES
        ]

    def search_wiki(self, query: str, scope: str) -> list[WikiMatch]:
        normalized_query = _normalize_non_blank_query(query)
        matches: list[WikiMatch] = []
        for page_ref in self.list_pages(scope):
            page = self.read_page(page_ref.page_path)
            match_start = page.content_md.casefold().find(normalized_query.casefold())
            if match_start < 0:
                continue
            matches.append(
                WikiMatch(
                    page_path=page_ref.page_path,
                    target_key=page_ref.target_key,
                    snippet=_extract_wiki_snippet(
                        page.content_md,
                        match_start=match_start,
                        match_length=len(normalized_query),
                    ),
                )
            )
        return matches

    def _resolve_revision(self, target_key: str) -> PersistedThesisRevision | None:
        cached = self._selected_revisions.get(target_key)
        if target_key in self._selected_revisions:
            return cached

        persisted = self._store.read_latest_at_or_before(
            target_key=target_key,
            business_at=self._business_at,
        )
        self._selected_revisions[target_key] = persisted
        return persisted

    def _read_operator_page(self, page_ref: PageRef) -> PageReadResult:
        try:
            content_md = resolve_page_path(self._layout, page_ref.page_path).read_text(
                encoding="utf-8",
            )
        except FileNotFoundError:
            return PageReadResult(
                page_path=page_ref.page_path,
                content_md=target_page_template("operator.md"),
                snapshot_source="bootstrap_template",
            )
        except OSError as exc:
            raise HistoricalThesisSnapshotReaderError(
                f"failed to read historical operator page {page_ref.page_path!r}: {exc}"
            ) from exc
        if content_md == target_page_template("operator.md"):
            return PageReadResult(
                page_path=page_ref.page_path,
                content_md=content_md,
                snapshot_source="bootstrap_template",
            )
        return PageReadResult(page_path=page_ref.page_path, content_md=content_md)


def render_historical_thesis_page(*, page_path: str, revision: ThesisRevision) -> str:
    """Rebuild one canonical thesis page from stored section snapshots only."""
    if not isinstance(revision, ThesisRevision):
        raise HistoricalThesisSnapshotReaderError(
            "revision must be a ThesisRevision instance."
        )

    page_ref = resolve_page_ref(page_path)
    page_name = _supported_page_name(page_ref)
    if page_name not in _THESIS_REVISION_PAGE_NAMES:
        supported = ", ".join(_THESIS_REVISION_PAGE_NAMES)
        raise HistoricalThesisSnapshotReaderError(
            "historical thesis revision rendering supports only thesis pages "
            f"{supported}; got {page_ref.page_path!r}."
        )
    if page_ref.target_key != revision.target_key:
        raise HistoricalThesisSnapshotReaderError(
            "revision target_key must match the requested historical page target."
        )

    expected_sections = canonical_thesis_page_sections(page_name)
    content_by_section = {
        section.section_name: section.content_md
        for section in revision.sections
        if section.page_path == page_ref.page_path
    }
    missing_sections = [
        section_name
        for section_name in expected_sections
        if section_name not in content_by_section
    ]
    if missing_sections:
        raise HistoricalThesisSnapshotReaderError(
            "historical thesis revision is missing canonical section snapshots for "
            f"{page_ref.page_path}: {', '.join(missing_sections)}."
        )

    lines = [f"# {_page_title(page_name)}", ""]
    for section_name in expected_sections:
        lines.append(f"## {section_name}")
        lines.append("")
        body = _normalize_section_body(content_by_section[section_name])
        if body:
            lines.extend(body.split("\n"))
            lines.append("")
    return "\n".join(lines).rstrip()


def _supported_page_name(page_ref: PageRef) -> str:
    page_name = Path(page_ref.page_path).name
    if page_ref.target_key is None or page_name not in _SUPPORTED_CANONICAL_PAGE_NAMES:
        supported = ", ".join(_SUPPORTED_CANONICAL_PAGE_NAMES)
        raise HistoricalThesisSnapshotReaderError(
            "historical thesis snapshot reader supports only canonical target pages "
            f"{supported}; got {page_ref.page_path!r}."
        )
    return page_name


def _resolve_supported_scope(scope: str) -> str:
    normalized_scope = _normalize_scope(scope)
    if not normalized_scope.startswith("target:"):
        raise HistoricalThesisSnapshotReaderError(
            "historical thesis snapshot reader supports only "
            "'target:<target_key>' scopes."
        )
    return normalized_scope.partition(":")[2]


def _normalize_scope(scope: str) -> str:
    if not isinstance(scope, str):
        raise HistoricalThesisSnapshotReaderError("scope must be a string.")
    normalized = scope.strip()
    if not normalized:
        raise HistoricalThesisSnapshotReaderError("scope must not be blank.")

    logical_scope = normalized.replace("\\", "/")
    if logical_scope == "shared" or logical_scope.startswith("target:"):
        if logical_scope.startswith("target:"):
            validate_target_key(
                logical_scope.partition(":")[2],
                error_type=HistoricalThesisSnapshotReaderError,
            )
        return logical_scope

    parts = logical_scope.split("/")
    if len(parts) == 2 and parts[0] in {"target", "targets"}:
        target_key = validate_target_key(
            parts[1],
            error_type=HistoricalThesisSnapshotReaderError,
        )
        return f"target:{target_key}"

    raise HistoricalThesisSnapshotReaderError(
        "scope must be 'target:<target_key>' for historical thesis snapshot reads."
    )


def _normalize_business_at(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise HistoricalThesisSnapshotReaderError("business_at must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise HistoricalThesisSnapshotReaderError(
            "business_at must be timezone-aware."
        )
    return value.astimezone(UTC)


def _page_title(page_name: str) -> str:
    try:
        title_line = target_page_template(page_name).splitlines()[0]
    except (IndexError, ResearchMemoryContractError) as exc:
        raise HistoricalThesisSnapshotReaderError(
            f"failed to resolve canonical page template for {page_name!r}: {exc}"
        ) from exc
    if not title_line.startswith("# "):
        raise HistoricalThesisSnapshotReaderError(
            f"canonical page template for {page_name!r} must start with '# '."
        )
    return title_line[2:].strip()


def _normalize_section_body(content_md: str) -> str:
    if not isinstance(content_md, str):
        raise HistoricalThesisSnapshotReaderError(
            "historical thesis section content must be a string."
        )
    return content_md.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def _normalize_non_blank_query(query: str) -> str:
    if not isinstance(query, str):
        raise HistoricalThesisSnapshotReaderError("query must be a string.")
    normalized = query.strip()
    if not normalized:
        raise HistoricalThesisSnapshotReaderError("query must not be blank.")
    return normalized


def _extract_wiki_snippet(
    content_md: str,
    *,
    match_start: int,
    match_length: int,
) -> str:
    if match_start < 0 or match_length < 1:
        raise HistoricalThesisSnapshotReaderError(
            "wiki snippet extraction requires a positive match."
        )
    start = max(0, match_start - _WIKI_SNIPPET_RADIUS)
    end = min(len(content_md), match_start + match_length + _WIKI_SNIPPET_RADIUS)
    snippet = content_md[start:end].strip()
    if start > 0:
        snippet = f"...{snippet}"
    if end < len(content_md):
        snippet = f"{snippet}..."
    return normalize_content(
        snippet,
        field_name="snippet",
        error_type=HistoricalThesisSnapshotReaderError,
    )


__all__ = [
    "HistoricalThesisSnapshotReader",
    "HistoricalThesisSnapshotReaderError",
    "render_historical_thesis_page",
]
