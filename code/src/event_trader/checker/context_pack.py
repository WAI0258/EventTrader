"""Deterministic checker briefing pack construction."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from event_trader.contracts import (
    CheckerRequest,
    EvidenceLedgerRecord,
    PageReadResult,
    ResearchMemoryReadPort,
)
from event_trader.contracts.evidence_review import (
    EvidenceReviewDimensions,
    classify_evidence_review_dimensions,
)
from event_trader.integrations.bounded_context import bounded_text_payload
from event_trader.integrations.markdown_context import (
    MarkdownContextError,
    find_markdown_section,
)
from event_trader.market.contracts import MarketContextSnapshot
from event_trader.source_release import SourceReleaseError, normalize_web_source_ref
from event_trader.thesis_revision.canonical_bundle import (
    checker_canonical_thesis_bundle_sections,
)

CHECKER_CONTEXT_PACK_SCHEMA = "canonical_checker_pack_2026_05"
_GENERIC_HIGH_IMPACT_MARKERS = frozenset(
    {
        "earnings",
        "guidance",
        "regulatory",
        "regulation",
        "legal",
        "lawsuit",
        "m&a",
        "merger",
        "acquisition",
        "macro",
        "fomc",
        "fed",
        "rate",
        "inflation",
        "cpi",
        "jobs",
        "employment",
        "tariff",
        "sanction",
        "war",
        "geopolitical",
        "shock",
    }
)
_SOX_DIRECT_MARKERS = frozenset(
    {
        "sox",
        "soxx",
        "smh",
        "semiconductor",
        "semiconductors",
        "chip",
        "chips",
        "nvidia",
        "nvda",
        "amd",
        "broadcom",
        "avgo",
        "tsmc",
        "taiwan semiconductor",
        "tsm",
        "asml",
        "micron",
        "mu",
        "arm",
        "samsung",
        "sk hynix",
        "hynix",
        "intel",
        "intc",
        "qualcomm",
        "qcom",
        "smic",
    }
)
_SOX_MATERIAL_FACT_MARKERS = frozenset(
    {
        "earnings",
        "guidance",
        "outlook",
        "forecast",
        "revenue",
        "margin",
        "capex",
        "capital expenditure",
        "ai capex",
        "data center",
        "hyperscaler",
        "ai accelerator",
        "gpu",
        "hbm",
        "memory",
        "foundry",
        "wafer",
        "fab",
        "capacity",
        "production",
        "shipment",
        "order",
        "backlog",
        "supply",
        "shortage",
        "export control",
        "export controls",
        "license",
        "restriction",
        "ban",
        "sanction",
        "tariff",
        "china",
        "taiwan",
        "netherlands",
        "korea",
        "regulatory",
        "regulation",
        "lawsuit",
        "merger",
        "acquisition",
    }
)
_SOX_NON_INTERRUPT_ROLES = frozenset({"duplicate", "price_recap", "subjective_view"})


type EvidenceReader = Callable[[list[str]], list[EvidenceLedgerRecord]]
type EvidenceWindowReader = Callable[[str, datetime, list[str]], list[EvidenceLedgerRecord]]
type PageReader = Callable[[str], PageReadResult]
type MarketContextBuilder = Callable[[str, datetime], MarketContextSnapshot | None]
type ResearchMemoryReaderFactory = Callable[[datetime], ResearchMemoryReadPort]


class CheckerContextPackError(ValueError):
    """Raised when checker context-pack construction is malformed."""


@dataclass(frozen=True, slots=True)
class CheckerPackConfig:
    """Bounded deterministic context-pack budgets."""

    evidence_excerpt_chars: int = 4_000
    section_excerpt_chars: int = 2_000
    recent_timeline_items: int = 10
    max_pack_chars: int = 16_000
    force_escalate_on_insufficient_coverage: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_excerpt_chars",
            "section_excerpt_chars",
            "recent_timeline_items",
            "max_pack_chars",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise CheckerContextPackError(f"{field_name} must be a positive integer.")
        if not isinstance(self.force_escalate_on_insufficient_coverage, bool):
            raise CheckerContextPackError(
                "force_escalate_on_insufficient_coverage must be a boolean."
            )


@dataclass(frozen=True, slots=True)
class CheckerTextSlice:
    """One bounded text slice with hash and truncation audit metadata."""

    excerpt: str
    sha256: str
    char_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class CheckerEvidenceSlice:
    """Bounded admitted-evidence briefing for checker routing."""

    event_id: str
    target_key: str
    source_ref: str
    source_identity: str
    title: str
    labels: tuple[str, ...]
    ts_source: datetime
    ts_event: datetime
    ts_init: datetime
    review_dimensions: EvidenceReviewDimensions
    content: CheckerTextSlice


@dataclass(frozen=True, slots=True)
class CheckerSectionSlice:
    """One canonical ResearchMemory section included in the checker briefing."""

    page_path: str
    section_name: str
    loaded: bool
    content: CheckerTextSlice

    @property
    def section_id(self) -> str:
        return f"{self.page_path}:{self.section_name}"


@dataclass(frozen=True, slots=True)
class CheckerMemoryPack:
    """Fixed ResearchMemory briefing surfaces visible to the checker."""

    sections: tuple[CheckerSectionSlice, ...]
    recent_timeline_items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CheckerPageReadMetadata:
    """Stable page-level provenance carried into checker context receipts."""

    page_path: str
    content_sha256: str
    snapshot_source: str | None
    snapshot_revision_id: str | None
    snapshot_committed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CheckerCoverage:
    """Coverage metadata for conservative checker validation."""

    material_sections_loaded: tuple[str, ...]
    material_sections_missing: tuple[str, ...]
    material_sections_empty: tuple[str, ...]
    material_sections_truncated: tuple[str, ...]
    high_impact_event: bool
    sufficient_for_no_action: bool
    insufficient_reasons: tuple[str, ...]
    prior_source_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CheckerContextPack:
    """Auditable fixed input to the single-pass checker policy."""

    request: CheckerRequest
    evidence: CheckerEvidenceSlice
    memory: CheckerMemoryPack
    coverage: CheckerCoverage
    page_hashes: dict[str, str]
    page_read_metadata: dict[str, CheckerPageReadMetadata]
    market_context: MarketContextSnapshot | None
    market_context_max_prompt_chars: int
    context_pack_schema: str
    context_pack_hash: str
    max_pack_chars: int


class CheckerContextPackBuilder:
    """Build deterministic checker packs from admitted evidence and canonical memory."""

    def __init__(
        self,
        *,
        read_evidence: EvidenceReader,
        read_page: PageReader | None,
        config: CheckerPackConfig,
        build_research_memory: ResearchMemoryReaderFactory | None = None,
        read_evidence_window: EvidenceWindowReader | None = None,
        build_market_context: MarketContextBuilder | None = None,
        market_context_max_prompt_chars: int = 6_000,
    ) -> None:
        if not callable(read_evidence):
            raise CheckerContextPackError("read_evidence must be callable.")
        if read_page is not None and not callable(read_page):
            raise CheckerContextPackError("read_page must be callable when provided.")
        if build_research_memory is not None and not callable(build_research_memory):
            raise CheckerContextPackError(
                "build_research_memory must be callable when provided."
            )
        if read_page is None and build_research_memory is None:
            raise CheckerContextPackError(
                "checker context packs require read_page or build_research_memory."
            )
        if not isinstance(config, CheckerPackConfig):
            raise CheckerContextPackError("config must be a CheckerPackConfig instance.")
        self._read_evidence = read_evidence
        self._read_page = read_page
        self._config = config
        self._build_research_memory = build_research_memory
        self._read_evidence_window = read_evidence_window
        self._build_market_context = build_market_context
        if (
            not isinstance(market_context_max_prompt_chars, int)
            or isinstance(market_context_max_prompt_chars, bool)
            or market_context_max_prompt_chars <= 0
        ):
            raise CheckerContextPackError(
                "market_context_max_prompt_chars must be a positive integer."
            )
        self._market_context_max_prompt_chars = market_context_max_prompt_chars

    def build(self, request: CheckerRequest) -> CheckerContextPack:
        if not isinstance(request, CheckerRequest):
            raise CheckerContextPackError("request must be a CheckerRequest instance.")
        if len(request.event_ids) != 1:
            raise CheckerContextPackError("checker context packs require exactly one event.")

        evidence_record = self._load_one_evidence(request)
        evidence_slice = _slice_evidence(
            evidence_record,
            limit=self._config.evidence_excerpt_chars,
        )
        section_limit = _derive_section_limit(self._config, target_key=request.target_key)
        sections, page_hashes, page_read_metadata = self._load_sections(
            target_key=request.target_key,
            section_limit=section_limit,
            read_page=self._resolve_page_reader(evidence_record.ts_event),
        )
        memory = CheckerMemoryPack(
            sections=sections,
            recent_timeline_items=_recent_timeline_items(
                sections,
                max_items=self._config.recent_timeline_items,
            ),
        )
        prior_source_event_ids = self._load_prior_source_event_ids(evidence_slice)
        coverage = _build_coverage(
            evidence=evidence_slice,
            memory=memory,
            prior_source_event_ids=prior_source_event_ids,
        )
        market_context = None
        if self._build_market_context is not None:
            market_context = self._build_market_context(
                request.target_key,
                evidence_record.ts_event,
            )
        pack_without_hash = CheckerContextPack(
            request=request,
            evidence=evidence_slice,
            memory=memory,
            coverage=coverage,
            page_hashes=page_hashes,
            page_read_metadata=page_read_metadata,
            market_context=market_context,
            market_context_max_prompt_chars=self._market_context_max_prompt_chars,
            context_pack_schema=CHECKER_CONTEXT_PACK_SCHEMA,
            context_pack_hash="",
            max_pack_chars=self._config.max_pack_chars,
        )
        pack_hash = _hash_pack(pack_without_hash)
        return CheckerContextPack(
            request=request,
            evidence=evidence_slice,
            memory=memory,
            coverage=coverage,
            page_hashes=page_hashes,
            page_read_metadata=page_read_metadata,
            market_context=market_context,
            market_context_max_prompt_chars=self._market_context_max_prompt_chars,
            context_pack_schema=CHECKER_CONTEXT_PACK_SCHEMA,
            context_pack_hash=pack_hash,
            max_pack_chars=self._config.max_pack_chars,
        )

    def _load_one_evidence(self, request: CheckerRequest) -> EvidenceLedgerRecord:
        records = self._read_evidence(list(request.event_ids))
        if not isinstance(records, list) or len(records) != 1:
            raise CheckerContextPackError(
                "read_evidence must return exactly one EvidenceLedgerRecord."
            )
        record = records[0]
        if not isinstance(record, EvidenceLedgerRecord):
            raise CheckerContextPackError(
                "read_evidence must return EvidenceLedgerRecord instances."
            )
        if record.event_id != request.event_ids[0] or record.target_key != request.target_key:
            raise CheckerContextPackError(
                "read_evidence returned a record that does not match the request."
            )
        return record

    def _load_sections(
        self,
        *,
        target_key: str,
        section_limit: int,
        read_page: PageReader,
    ) -> tuple[
        tuple[CheckerSectionSlice, ...],
        dict[str, str],
        dict[str, CheckerPageReadMetadata],
    ]:
        pages: dict[str, PageReadResult] = {}
        page_hashes: dict[str, str] = {}
        page_read_metadata: dict[str, CheckerPageReadMetadata] = {}
        canonical_sections = checker_canonical_thesis_bundle_sections(target_key)
        for bundle_section in canonical_sections:
            page_path = bundle_section.page_path
            if page_path not in pages:
                page = read_page(page_path)
                _validate_page_result(page, expected_page_path=page_path)
                pages[page_path] = page
                page_hashes[page_path] = sha256(page.content_md.encode("utf-8")).hexdigest()
                page_read_metadata[page_path] = CheckerPageReadMetadata(
                    page_path=page.page_path,
                    content_sha256=page_hashes[page_path],
                    snapshot_source=page.snapshot_source,
                    snapshot_revision_id=page.snapshot_revision_id,
                    snapshot_committed_at=page.snapshot_committed_at,
                )

        sections: list[CheckerSectionSlice] = []
        for bundle_section in canonical_sections:
            page = pages[bundle_section.page_path]
            sections.append(
                _slice_markdown_section(
                    page=page,
                    section_name=bundle_section.section_name,
                    limit=section_limit,
                )
            )
        return tuple(sections), page_hashes, page_read_metadata

    def _resolve_page_reader(self, business_at: datetime) -> PageReader:
        if self._build_research_memory is not None:
            return self._build_research_memory(business_at).read_page
        if self._read_page is None:
            raise CheckerContextPackError(
                "checker context packs could not resolve a research-memory page reader."
            )
        return self._read_page

    def _load_prior_source_event_ids(
        self,
        evidence: CheckerEvidenceSlice,
    ) -> tuple[str, ...]:
        if self._read_evidence_window is None:
            return ()
        prior_records = self._read_evidence_window(
            evidence.target_key,
            evidence.ts_event,
            [evidence.event_id],
        )
        current_sort_key = (evidence.ts_event, evidence.ts_init, evidence.event_id)
        matches = [
            record.event_id
            for record in prior_records
            if _canonical_source_identity(record.source_ref) == evidence.source_identity
            and (record.ts_event, record.ts_init, record.event_id) < current_sort_key
        ]
        return tuple(matches)


def serialize_context_pack(pack: CheckerContextPack) -> dict[str, Any]:
    """Serialize a context pack into canonical JSON-compatible primitives."""
    if not isinstance(pack, CheckerContextPack):
        raise CheckerContextPackError("pack must be a CheckerContextPack instance.")
    return {
        "request": {
            "target_key": pack.request.target_key,
            "event_ids": list(pack.request.event_ids),
        },
        "evidence": {
            "event_id": pack.evidence.event_id,
            "target_key": pack.evidence.target_key,
            "source_ref": pack.evidence.source_ref,
            "source_identity": pack.evidence.source_identity,
            "title": pack.evidence.title,
            "labels": list(pack.evidence.labels),
            "ts_source": pack.evidence.ts_source.isoformat(),
            "ts_event": pack.evidence.ts_event.isoformat(),
            "ts_init": pack.evidence.ts_init.isoformat(),
            "review_dimensions": pack.evidence.review_dimensions.to_dict(),
            "content": _serialize_text_slice(pack.evidence.content),
        },
        "memory": {
            "sections": [
                {
                    "section_id": section.section_id,
                    "page_path": section.page_path,
                    "section_name": section.section_name,
                    "loaded": section.loaded,
                    "content": _serialize_text_slice(section.content),
                }
                for section in pack.memory.sections
            ],
            "recent_timeline_items": list(pack.memory.recent_timeline_items),
        },
        "coverage": {
            "material_sections_loaded": list(pack.coverage.material_sections_loaded),
            "material_sections_missing": list(pack.coverage.material_sections_missing),
            "material_sections_empty": list(pack.coverage.material_sections_empty),
            "material_sections_truncated": list(pack.coverage.material_sections_truncated),
            "high_impact_event": pack.coverage.high_impact_event,
            "sufficient_for_no_action": pack.coverage.sufficient_for_no_action,
            "insufficient_reasons": list(pack.coverage.insufficient_reasons),
            "prior_source_event_ids": list(pack.coverage.prior_source_event_ids),
        },
        "page_hashes": dict(sorted(pack.page_hashes.items())),
        "page_read_metadata": {
            page_path: {
                "content_sha256": metadata.content_sha256,
                "snapshot_source": metadata.snapshot_source,
                "snapshot_revision_id": metadata.snapshot_revision_id,
                "snapshot_committed_at": (
                    None
                    if metadata.snapshot_committed_at is None
                    else metadata.snapshot_committed_at.isoformat()
                ),
            }
            for page_path, metadata in sorted(pack.page_read_metadata.items())
        },
        "market_context": (
            pack.market_context.to_prompt_dict(
                max_chars=pack.market_context_max_prompt_chars
            )
            if pack.market_context is not None
            else None
        ),
        "context_pack_schema": pack.context_pack_schema,
        "context_pack_hash": pack.context_pack_hash,
        "max_pack_chars": pack.max_pack_chars,
    }


def _slice_evidence(record: EvidenceLedgerRecord, *, limit: int) -> CheckerEvidenceSlice:
    return CheckerEvidenceSlice(
        event_id=record.event_id,
        target_key=record.target_key,
        source_ref=record.source_ref,
        source_identity=_canonical_source_identity(record.source_ref),
        title=record.title,
        labels=tuple(record.labels),
        ts_source=record.ts_source,
        ts_event=record.ts_event,
        ts_init=record.ts_init,
        review_dimensions=classify_evidence_review_dimensions(record),
        content=_text_slice(record.content, limit=limit),
    )


def _slice_markdown_section(
    *,
    page: PageReadResult,
    section_name: str,
    limit: int,
) -> CheckerSectionSlice:
    try:
        section = find_markdown_section(
            page.content_md,
            heading=section_name,
            heading_level=2,
            excerpt_char_limit=limit,
        )
    except MarkdownContextError as exc:
        raise CheckerContextPackError(
            f"failed to read checker section {page.page_path}:{section_name}: {exc}"
        ) from exc
    if section is None:
        return CheckerSectionSlice(
            page_path=page.page_path,
            section_name=section_name,
            loaded=False,
            content=_text_slice("", limit=limit),
        )
    excerpt = section["content_excerpt"]
    if not isinstance(excerpt, str):
        raise CheckerContextPackError("markdown section excerpt must be a string.")
    return CheckerSectionSlice(
        page_path=page.page_path,
        section_name=section_name,
        loaded=True,
        content=CheckerTextSlice(
            excerpt=excerpt,
            sha256=_require_str(section["content_sha256"], "content_sha256"),
            char_count=_require_int(section["content_char_count"], "content_char_count"),
            truncated=_require_bool(section["content_truncated"], "content_truncated"),
        ),
    )


def _build_coverage(
    *,
    evidence: CheckerEvidenceSlice,
    memory: CheckerMemoryPack,
    prior_source_event_ids: tuple[str, ...],
) -> CheckerCoverage:
    loaded: list[str] = []
    missing: list[str] = []
    empty: list[str] = []
    truncated: list[str] = []
    for section in memory.sections:
        if not section.loaded:
            missing.append(section.section_id)
            continue
        loaded.append(section.section_id)
        if not section.content.excerpt.strip():
            empty.append(section.section_id)
        if section.content.truncated:
            truncated.append(section.section_id)

    insufficient_reasons: list[str] = []
    if missing:
        insufficient_reasons.append("canonical memory section missing")
    if len(loaded) == len(empty):
        insufficient_reasons.append("canonical memory sections contain no positive context")

    return CheckerCoverage(
        material_sections_loaded=tuple(loaded),
        material_sections_missing=tuple(missing),
        material_sections_empty=tuple(empty),
        material_sections_truncated=tuple(truncated),
        high_impact_event=_is_high_impact_event(evidence),
        sufficient_for_no_action=not insufficient_reasons,
        insufficient_reasons=tuple(insufficient_reasons),
        prior_source_event_ids=prior_source_event_ids,
    )


def _recent_timeline_items(
    sections: tuple[CheckerSectionSlice, ...],
    *,
    max_items: int,
) -> tuple[str, ...]:
    timeline = next(
        (
            section
            for section in sections
            if section.page_path.endswith("/timeline.md")
            and section.section_name == "Recent Developments"
        ),
        None,
    )
    if timeline is None:
        return ()
    items = [line.strip() for line in timeline.content.excerpt.splitlines() if line.strip()]
    return tuple(items[:max_items])


def _derive_section_limit(config: CheckerPackConfig, *, target_key: str) -> int:
    remaining = config.max_pack_chars - config.evidence_excerpt_chars
    per_section = max(
        1,
        remaining // len(checker_canonical_thesis_bundle_sections(target_key)),
    )
    return min(config.section_excerpt_chars, per_section)


def _canonical_source_identity(source_ref: str) -> str:
    if source_ref.startswith(("http://", "https://")):
        try:
            return normalize_web_source_ref(source_ref)
        except SourceReleaseError:
            return source_ref
    return source_ref


def _text_slice(value: str, *, limit: int) -> CheckerTextSlice:
    payload = bounded_text_payload(value, limit=limit)
    return CheckerTextSlice(
        excerpt=_require_str(payload["excerpt"], "excerpt"),
        sha256=_require_str(payload["sha256"], "sha256"),
        char_count=_require_int(payload["char_count"], "char_count"),
        truncated=_require_bool(payload["truncated"], "truncated"),
    )


def _serialize_text_slice(text: CheckerTextSlice) -> dict[str, object]:
    return {
        "excerpt": text.excerpt,
        "sha256": text.sha256,
        "char_count": text.char_count,
        "truncated": text.truncated,
    }


def _hash_pack(pack: CheckerContextPack) -> str:
    payload = serialize_context_pack(pack)
    payload["context_pack_hash"] = ""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _is_high_impact_event(evidence: CheckerEvidenceSlice) -> bool:
    haystack = " ".join(
        (
            evidence.title,
            evidence.source_ref,
            " ".join(evidence.labels),
            evidence.content.excerpt,
        )
    ).casefold()
    if evidence.target_key == "sox":
        if evidence.review_dimensions.evidence_role in _SOX_NON_INTERRUPT_ROLES:
            return False
        return (
            any(marker in haystack for marker in _SOX_DIRECT_MARKERS)
            and any(marker in haystack for marker in _SOX_MATERIAL_FACT_MARKERS)
        )
    return any(marker in haystack for marker in _GENERIC_HIGH_IMPACT_MARKERS)


def _validate_page_result(page: PageReadResult, *, expected_page_path: str) -> None:
    if not isinstance(page, PageReadResult):
        raise CheckerContextPackError("read_page must return a PageReadResult.")
    if page.page_path != expected_page_path:
        raise CheckerContextPackError(
            "read_page must return the requested canonical page_path; "
            f"requested {expected_page_path!r}, got {page.page_path!r}."
        )


def _require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise CheckerContextPackError(f"{field_name} must be a string.")
    return value


def _require_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CheckerContextPackError(f"{field_name} must be an integer.")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CheckerContextPackError(f"{field_name} must be a boolean.")
    return value


__all__ = [
    "CHECKER_CONTEXT_PACK_SCHEMA",
    "CheckerContextPack",
    "CheckerContextPackBuilder",
    "CheckerContextPackError",
    "CheckerCoverage",
    "CheckerEvidenceSlice",
    "CheckerMemoryPack",
    "CheckerPageReadMetadata",
    "CheckerPackConfig",
    "CheckerSectionSlice",
    "CheckerTextSlice",
    "serialize_context_pack",
]
