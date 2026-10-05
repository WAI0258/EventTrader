"""Analysis-side context loading outside checker dispatch.

The analysis boundary consumes the canonical checker escalation handoff,
recovers admitted evidence plus the current target/shared wiki context, and
keeps the checker rationale available only as a weak attention hint.
It intentionally exposes no write surface, selector layer, or runtime effect
adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from event_trader.ceau.contracts import UnitFormationLane
from event_trader.contracts import (
    AnalysisRequest,
    DecisionVisibilityBoundary,
    DecisionVisibilityBoundaryError,
    EvidenceLedgerRecord,
    PageReadResult,
    PageRef,
    ResearchMemoryReadPort,
    WikiMatch,
    resolve_page_path,
    resolve_page_ref,
    resolve_scope,
)
from event_trader.contracts._validators import normalize_content, validate_target_key
from event_trader.market.contracts import MarketContextSnapshot
from event_trader.storage import WorkspaceLayout

type EvidenceReader = Callable[[list[str]], list[EvidenceLedgerRecord]]
type MarketContextBuilder = Callable[[str, datetime], MarketContextSnapshot | None]
type ResearchMemoryReaderFactory = Callable[[datetime], ResearchMemoryReadPort]
type TargetSeedPagePathsResolver = Callable[[str], tuple[str, ...]]

_TARGET_FIXED_PAGE_NAMES = (
    "index.md",
    "thesis.md",
    "timeline.md",
    "risks.md",
    "watchlist.md",
    "operator.md",
    "log.md",
)
_TARGET_DESK_PAGE_NAMES = (
    "index.md",
    "thesis.md",
    "risks.md",
    "watchlist.md",
    "timeline.md",
    "operator.md",
)
_HISTORICAL_TARGET_SEED_PAGE_NAMES = (
    "index.md",
    "thesis.md",
    "risks.md",
    "watchlist.md",
    "timeline.md",
    "operator.md",
)
_SHARED_FIXED_PAGE_PATHS = (
    "shared/index.md",
    "shared/log.md",
)
_RESEARCH_MEMORY_WRITE_METHODS = (
    "update_index",
    "append_log_entry",
    "update_page_section",
    "rewrite_page",
    "create_page",
)
_RUNTIME_WRITE_METHODS = ("emit_escalation",)
_TARGET_DYNAMIC_FAMILIES = ("topics", "sources", "reviews")
_WIKI_SNIPPET_RADIUS = 120


class AnalysisContextError(ValueError):
    """Raised when analysis context dependencies or reads are malformed."""


@dataclass(frozen=True, slots=True)
class AnalysisAttentionHint:
    """Weak checker attention rationale kept separate from evidence and memory."""

    why_escalated: str

    def __post_init__(self) -> None:
        if not isinstance(self.why_escalated, str) or not self.why_escalated:
            raise AnalysisContextError(
                "why_escalated must be a non-empty string attention hint."
            )


@dataclass(frozen=True, slots=True)
class AnalysisMemoryContext:
    """Minimal runtime-seeded wiki context recovered for one analysis decision."""

    target_pages: tuple[PageReadResult, ...]
    shared_pages: tuple[PageReadResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_pages",
            _normalize_page_results(
                self.target_pages,
                field_name="target_pages",
            ),
        )
        object.__setattr__(
            self,
            "shared_pages",
            _normalize_page_results(
                self.shared_pages,
                field_name="shared_pages",
            ),
        )


@dataclass(frozen=True, slots=True)
class AnalysisContextReceipt:
    """Observable receipt describing the exact runtime-seeded read pack loaded."""

    target_key: str
    evidence_count: int
    target_page_paths: tuple[str, ...]
    shared_page_paths: tuple[str, ...]
    market_context_present: bool = False
    market_context_audit: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target_key, str) or not self.target_key:
            raise AnalysisContextError("target_key must be a non-empty string.")
        if not isinstance(self.evidence_count, int) or self.evidence_count < 1:
            raise AnalysisContextError("evidence_count must be >= 1.")
        object.__setattr__(
            self,
            "target_page_paths",
            _normalize_page_paths(
                self.target_page_paths,
                field_name="target_page_paths",
            ),
        )
        object.__setattr__(
            self,
            "shared_page_paths",
            _normalize_page_paths(
                self.shared_page_paths,
                field_name="shared_page_paths",
            ),
        )
        if not isinstance(self.market_context_present, bool):
            raise AnalysisContextError("market_context_present must be a boolean.")
        if self.market_context_audit is not None and not isinstance(
            self.market_context_audit,
            dict,
        ):
            raise AnalysisContextError("market_context_audit must be a dict when present.")


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    """Explicit analysis input pack with hint, evidence, and wiki context split."""

    request: AnalysisRequest
    business_at: datetime
    attention_hint: AnalysisAttentionHint
    evidence_records: tuple[EvidenceLedgerRecord, ...]
    memory_context: AnalysisMemoryContext
    receipt: AnalysisContextReceipt
    market_context: MarketContextSnapshot | None = None
    unit_formation_lane: UnitFormationLane | None = None
    market_context_max_prompt_chars: int = 6_000
    _decision_visibility: DecisionVisibilityBoundary = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.request, AnalysisRequest):
            raise AnalysisContextError("request must be an AnalysisRequest instance.")
        try:
            decision_visibility = DecisionVisibilityBoundary.at_business_time(
                self.business_at
            )
        except DecisionVisibilityBoundaryError as exc:
            raise AnalysisContextError(str(exc)) from exc
        object.__setattr__(self, "business_at", decision_visibility.business_at)
        object.__setattr__(self, "_decision_visibility", decision_visibility)
        if not isinstance(self.attention_hint, AnalysisAttentionHint):
            raise AnalysisContextError(
                "attention_hint must be an AnalysisAttentionHint instance."
            )
        object.__setattr__(
            self,
            "evidence_records",
            _normalize_evidence_tuple(self.evidence_records),
        )
        if not isinstance(self.memory_context, AnalysisMemoryContext):
            raise AnalysisContextError(
                "memory_context must be an AnalysisMemoryContext instance."
            )
        if not isinstance(self.receipt, AnalysisContextReceipt):
            raise AnalysisContextError(
                "receipt must be an AnalysisContextReceipt instance."
            )
        if self.market_context is not None and not isinstance(
            self.market_context,
            MarketContextSnapshot,
        ):
            raise AnalysisContextError(
                "market_context must be a MarketContextSnapshot when present."
            )
        if (
            self.unit_formation_lane is not None
            and not isinstance(self.unit_formation_lane, UnitFormationLane)
        ):
            raise AnalysisContextError(
                "unit_formation_lane must be a UnitFormationLane when present."
            )
        if (
            not isinstance(self.market_context_max_prompt_chars, int)
            or isinstance(self.market_context_max_prompt_chars, bool)
            or self.market_context_max_prompt_chars <= 0
        ):
            raise AnalysisContextError(
                "market_context_max_prompt_chars must be a positive integer."
            )
        if self.evidence_records:
            evidence_business_at = max(record.ts_event for record in self.evidence_records)
            if self.decision_visibility.max_visible_event_time < evidence_business_at:
                raise AnalysisContextError(
                    "business_at must not be earlier than the latest evidence ts_event."
                )
        if (
            self.unit_formation_lane is not None
            and self.decision_visibility.business_at < self.unit_formation_lane.deadline_at
        ):
            raise AnalysisContextError(
                "business_at must not be earlier than unit_formation_lane.deadline_at."
            )

    @property
    def decision_visibility(self) -> DecisionVisibilityBoundary:
        return self._decision_visibility


class AnalysisContextLoader:
    """Small analysis dependency surface for read-only evidence and wiki access."""

    def __init__(
        self,
        read_evidence: EvidenceReader,
        research_memory: ResearchMemoryReadPort | None = None,
        build_research_memory: ResearchMemoryReaderFactory | None = None,
        build_market_context: MarketContextBuilder | None = None,
        market_context_max_prompt_chars: int = 6_000,
        target_seed_page_paths: TargetSeedPagePathsResolver | None = None,
    ) -> None:
        if not callable(read_evidence):
            raise AnalysisContextError("read_evidence must be callable.")
        if (research_memory is None) == (build_research_memory is None):
            raise AnalysisContextError(
                "provide exactly one of research_memory or build_research_memory."
            )
        if research_memory is not None:
            _validate_research_memory_reader(research_memory)
            build_research_memory = lambda _business_at: research_memory
        elif not callable(build_research_memory):
            raise AnalysisContextError("build_research_memory must be callable.")
        if target_seed_page_paths is None:
            target_seed_page_paths = current_target_seed_page_paths
        if not callable(target_seed_page_paths):
            raise AnalysisContextError("target_seed_page_paths must be callable.")

        self._read_evidence = read_evidence
        self._build_research_memory = build_research_memory
        self._build_market_context = build_market_context
        self._target_seed_page_paths = target_seed_page_paths
        if (
            not isinstance(market_context_max_prompt_chars, int)
            or isinstance(market_context_max_prompt_chars, bool)
            or market_context_max_prompt_chars <= 0
        ):
            raise AnalysisContextError(
                "market_context_max_prompt_chars must be a positive integer."
            )
        self._market_context_max_prompt_chars = market_context_max_prompt_chars

    def load_context(
        self,
        request: AnalysisRequest,
        *,
        unit_formation_lane: UnitFormationLane | None = None,
    ) -> AnalysisContext:
        """Load admitted evidence plus the minimal runtime-seeded wiki context."""
        if not isinstance(request, AnalysisRequest):
            raise AnalysisContextError("request must be an AnalysisRequest instance.")
        if (
            unit_formation_lane is not None
            and not isinstance(unit_formation_lane, UnitFormationLane)
        ):
            raise AnalysisContextError(
                "unit_formation_lane must be a UnitFormationLane when present."
            )

        evidence_records = self._load_evidence_records(request)
        business_at = _analysis_context_business_at(
            evidence_records=evidence_records,
            unit_formation_lane=unit_formation_lane,
        )
        research_memory = self._build_research_memory(business_at)
        _validate_research_memory_reader(research_memory)
        target_pages = self._read_pages(
            research_memory=research_memory,
            page_paths=self._target_seed_page_paths(request.target_key),
        )
        shared_pages: tuple[PageReadResult, ...] = ()
        memory_context = AnalysisMemoryContext(
            target_pages=target_pages,
            shared_pages=shared_pages,
        )
        market_context = None
        if self._build_market_context is not None:
            market_context = self._build_market_context(request.target_key, business_at)

        return AnalysisContext(
            request=request,
            business_at=business_at,
            attention_hint=AnalysisAttentionHint(
                why_escalated=request.why_escalated,
            ),
            evidence_records=evidence_records,
            memory_context=memory_context,
            receipt=AnalysisContextReceipt(
                target_key=request.target_key,
                evidence_count=len(evidence_records),
                target_page_paths=tuple(page.page_path for page in target_pages),
                shared_page_paths=tuple(page.page_path for page in shared_pages),
                market_context_present=market_context is not None,
                market_context_audit=(
                    market_context.compact_audit()
                    if market_context is not None
                    else None
                ),
            ),
            market_context=market_context,
            market_context_max_prompt_chars=self._market_context_max_prompt_chars,
            unit_formation_lane=unit_formation_lane,
        )

    def _load_evidence_records(
        self,
        request: AnalysisRequest,
    ) -> tuple[EvidenceLedgerRecord, ...]:
        try:
            raw_records = self._read_evidence(list(request.event_ids))
        except (
            Exception
        ) as exc:  # pragma: no cover - defensive wrap for dependency errors
            raise AnalysisContextError(
                "Failed to read admitted evidence for analysis request "
                f"target_key={request.target_key!r} event_ids={list(request.event_ids)!r}: {exc}"
            ) from exc

        return _normalize_evidence_records(raw_records, request=request)

    def _read_pages(
        self,
        *,
        research_memory: ResearchMemoryReadPort,
        page_paths: tuple[str, ...],
    ) -> tuple[PageReadResult, ...]:
        pages: list[PageReadResult] = []
        for page_path in page_paths:
            try:
                result = research_memory.read_page(page_path)
            except (
                Exception
            ) as exc:  # pragma: no cover - defensive wrap for dependency errors
                raise AnalysisContextError(
                    "Failed to read analysis context page " f"{page_path!r}: {exc}"
                ) from exc

            if not isinstance(result, PageReadResult):
                raise AnalysisContextError(
                    "research_memory.read_page must return a PageReadResult instance "
                    f"for requested page {page_path!r}."
                )
            if result.page_path != page_path:
                raise AnalysisContextError(
                    "research_memory.read_page must return the requested canonical "
                    f"page_path; requested {page_path!r}, got {result.page_path!r}."
                )
            pages.append(result)

        return tuple(pages)


class FileBackedResearchMemoryReader:
    """Read canonical research-memory pages for analysis without write seams."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise AnalysisContextError("layout must be a WorkspaceLayout instance.")
        self._layout = layout

    def read_page(self, page_path: str) -> PageReadResult:
        absolute_page_path = resolve_page_path(self._layout, page_path)
        try:
            content_md = absolute_page_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AnalysisContextError(
                f"Failed to read canonical analysis page {page_path!r}: {exc}"
            ) from exc
        return PageReadResult(page_path=page_path, content_md=content_md)

    def list_pages(self, scope: str) -> list[PageRef]:
        resolved_scope = resolve_scope(self._layout, _normalize_read_scope(scope))
        _ensure_scope_root_exists(resolved_scope.root, scope_key=resolved_scope.scope_key)
        if resolved_scope.scope_kind == "shared":
            return _list_shared_pages(self._layout)
        return _list_target_pages(self._layout, resolved_scope.target_key or "")

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


def _validate_research_memory_reader(research_memory: ResearchMemoryReadPort) -> None:
    if not callable(getattr(research_memory, "read_page", None)):
        raise AnalysisContextError(
            "research_memory must implement read_page(page_path)."
        )

    for method_name in (*_RESEARCH_MEMORY_WRITE_METHODS, *_RUNTIME_WRITE_METHODS):
        if callable(getattr(research_memory, method_name, None)):
            raise AnalysisContextError(
                "research_memory must be read-only for analysis context loading; "
                f"{method_name} is not an allowed dependency."
            )


def _normalize_read_scope(scope: str) -> str:
    if not isinstance(scope, str):
        raise AnalysisContextError("scope must be a string.")
    normalized = scope.strip()
    path_scope = normalized.replace("\\", "/")
    if path_scope == "shared" or path_scope.startswith("target:"):
        return normalized
    parts = path_scope.split("/")
    if len(parts) == 2 and parts[0] in {"target", "targets"}:
        target_key = validate_target_key(parts[1], error_type=AnalysisContextError)
        return f"target:{target_key}"
    return normalized


def _normalize_evidence_records(
    records: list[EvidenceLedgerRecord],
    *,
    request: AnalysisRequest,
) -> tuple[EvidenceLedgerRecord, ...]:
    if not isinstance(records, list):
        raise AnalysisContextError(
            "read_evidence must return a list of EvidenceLedgerRecord instances."
        )
    if len(records) != len(request.event_ids):
        raise AnalysisContextError(
            "read_evidence must return one EvidenceLedgerRecord per requested event_id."
        )

    normalized: list[EvidenceLedgerRecord] = []
    for expected_event_id, record in zip(request.event_ids, records, strict=True):
        if not isinstance(record, EvidenceLedgerRecord):
            raise AnalysisContextError(
                "read_evidence must return only EvidenceLedgerRecord instances."
            )
        if record.event_id != expected_event_id:
            raise AnalysisContextError(
                "read_evidence must preserve requested event_id order for analysis context."
            )
        if record.target_key != request.target_key:
            raise AnalysisContextError(
                "read_evidence returned an EvidenceLedgerRecord whose target_key does "
                "not match the AnalysisRequest target_key."
            )
        normalized.append(record)

    return tuple(normalized)


def _normalize_evidence_tuple(
    records: tuple[EvidenceLedgerRecord, ...],
) -> tuple[EvidenceLedgerRecord, ...]:
    normalized: list[EvidenceLedgerRecord] = []
    for record in records:
        if not isinstance(record, EvidenceLedgerRecord):
            raise AnalysisContextError(
                "evidence_records must contain only EvidenceLedgerRecord instances."
            )
        normalized.append(record)
    return tuple(normalized)


def _normalize_page_results(
    pages: tuple[PageReadResult, ...],
    *,
    field_name: str,
) -> tuple[PageReadResult, ...]:
    normalized: list[PageReadResult] = []
    for page in pages:
        if not isinstance(page, PageReadResult):
            raise AnalysisContextError(
                f"{field_name} must contain only PageReadResult instances."
            )
        normalized.append(page)
    return tuple(normalized)


def _normalize_page_paths(
    page_paths: tuple[str, ...],
    *,
    field_name: str,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for page_path in page_paths:
        if not isinstance(page_path, str) or not page_path:
            raise AnalysisContextError(
                f"{field_name} must contain only non-empty canonical page paths."
            )
        normalized.append(page_path)
    return tuple(normalized)


def current_target_seed_page_paths(target_key: str) -> tuple[str, ...]:
    return tuple(
        f"targets/{target_key}/{page_name}" for page_name in _TARGET_DESK_PAGE_NAMES
    )


def historical_target_seed_page_paths(target_key: str) -> tuple[str, ...]:
    return tuple(
        f"targets/{target_key}/{page_name}"
        for page_name in _HISTORICAL_TARGET_SEED_PAGE_NAMES
    )


def _target_fixed_page_paths(target_key: str) -> tuple[str, ...]:
    return tuple(
        f"targets/{target_key}/{page_name}" for page_name in _TARGET_FIXED_PAGE_NAMES
    )


def _list_shared_pages(layout: WorkspaceLayout) -> list[PageRef]:
    page_refs = [resolve_page_ref(page_path) for page_path in _SHARED_FIXED_PAGE_PATHS]
    _ensure_existing_markdown_pages(layout, tuple(page.page_path for page in page_refs))
    page_refs.extend(
        _dynamic_page_refs(layout.shared_topics_root, prefix="shared/topics")
    )
    page_refs.extend(
        _dynamic_page_refs(layout.shared_entities_root, prefix="shared/entities")
    )
    return page_refs


def _list_target_pages(layout: WorkspaceLayout, target_key: str) -> list[PageRef]:
    page_refs = [
        resolve_page_ref(page_path)
        for page_path in _target_fixed_page_paths(target_key)
    ]
    _ensure_existing_markdown_pages(layout, tuple(page.page_path for page in page_refs))
    target_scope = resolve_scope(layout, f"target:{target_key}")
    for family in _TARGET_DYNAMIC_FAMILIES:
        family_root = (target_scope.root / family).resolve(strict=False)
        _ensure_existing_directory(
            family_root,
            role_description=f"canonical target {family} directory",
        )
        page_refs.extend(
            _dynamic_page_refs(
                family_root,
                prefix=f"targets/{target_key}/{family}",
            )
        )
    return page_refs


def _dynamic_page_refs(root: Path, *, prefix: str) -> list[PageRef]:
    page_refs: list[PageRef] = []
    for path in sorted(root.glob("*.md")):
        page_refs.append(resolve_page_ref(f"{prefix}/{path.name}"))
    return page_refs


def _ensure_existing_markdown_pages(
    layout: WorkspaceLayout,
    page_paths: tuple[str, ...],
) -> None:
    for page_path in page_paths:
        absolute_path = resolve_page_path(layout, page_path)
        _ensure_existing_file(
            absolute_path,
            role_description=f"canonical analysis page {page_path!r}",
        )


def _ensure_scope_root_exists(root: Path, *, scope_key: str) -> None:
    _ensure_existing_directory(
        root,
        role_description=f"canonical research-memory scope root for {scope_key!r}",
    )


def _ensure_existing_directory(path: Path, *, role_description: str) -> None:
    if not path.exists():
        raise AnalysisContextError(f"Missing required {role_description}: {path}")
    if not path.is_dir():
        raise AnalysisContextError(
            f"Expected {role_description}, but found a file at: {path}"
        )


def _ensure_existing_file(path: Path, *, role_description: str) -> None:
    if not path.exists():
        raise AnalysisContextError(f"Missing required {role_description}: {path}")
    if not path.is_file():
        raise AnalysisContextError(
            f"Expected {role_description}, but found a directory at: {path}"
        )


def _normalize_non_blank_query(query: str) -> str:
    if not isinstance(query, str):
        raise AnalysisContextError("query must be a string.")
    normalized = query.strip()
    if not normalized:
        raise AnalysisContextError("query must not be blank.")
    return normalized


def _extract_wiki_snippet(
    content_md: str,
    *,
    match_start: int,
    match_length: int,
) -> str:
    if match_start < 0 or match_length < 1:
        raise AnalysisContextError("wiki snippet extraction requires a positive match.")
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
        error_type=AnalysisContextError,
    )


def _analysis_context_business_at(
    *,
    evidence_records: tuple[EvidenceLedgerRecord, ...],
    unit_formation_lane: UnitFormationLane | None,
) -> datetime:
    evidence_business_at = max(record.ts_event for record in evidence_records)
    if unit_formation_lane is None:
        return evidence_business_at
    return max(evidence_business_at, unit_formation_lane.deadline_at)


__all__ = [
    "AnalysisAttentionHint",
    "AnalysisContext",
    "AnalysisContextError",
    "AnalysisContextLoader",
    "AnalysisContextReceipt",
    "AnalysisMemoryContext",
    "EvidenceReader",
    "FileBackedResearchMemoryReader",
    "current_target_seed_page_paths",
    "historical_target_seed_page_paths",
]
