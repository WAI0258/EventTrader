"""Current-state projection read contract and rendering model.

Projection is a read-side module over current research memory plus cited evidence.
It must not mutate research memory, the evidence ledger, or runtime surfaces.
This module therefore defines only the narrow load/render contract for one
current-state report target.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from event_trader.contracts import (
    EvidenceLedgerRecord,
    PageReadResult,
    ResearchMemoryReadPort,
)
from event_trader.contracts._validators import (
    normalize_content,
    validate_event_id,
    validate_target_key,
    validate_timestamp,
)
from event_trader.decision_memory import DecisionEpisodeProjection
from event_trader.integrations.analysis_citations import extract_analysis_citations
from event_trader.portfolio import PMDecision, PortfolioState

type EvidenceReader = Callable[[list[str]], list[EvidenceLedgerRecord]]
type DecisionEpisodeReader = Callable[[str], tuple[DecisionEpisodeProjection, ...]]
type PortfolioStateReader = Callable[[str], PortfolioState | None]
type PMDecisionReader = Callable[[str], PMDecision | None]

_SOURCE_PAGE_NAMES = (
    "thesis.md",
    "timeline.md",
    "risks.md",
    "watchlist.md",
    "index.md",
    "log.md",
)
_SECOND_LEVEL_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)
_RESEARCH_MEMORY_WRITE_METHODS = (
    "update_index",
    "append_log_entry",
    "update_page_section",
    "rewrite_page",
    "create_page",
)
_RUNTIME_WRITE_METHODS = ("emit_escalation",)


class CurrentStateProjectionError(ValueError):
    """Raised when projection dependencies or current-state reads are malformed."""


@dataclass(frozen=True, slots=True)
class CurrentStateSourcePages:
    """Canonical target page pack required for one current-state projection."""

    target_key: str
    thesis: PageReadResult
    timeline: PageReadResult
    risks: PageReadResult
    watchlist: PageReadResult
    index: PageReadResult
    log: PageReadResult

    def __post_init__(self) -> None:
        validated_target_key = validate_target_key(
            self.target_key,
            error_type=CurrentStateProjectionError,
        )
        expected_page_paths = _source_page_paths(validated_target_key)
        pages = self.as_tuple()

        for expected_page_path, page in zip(expected_page_paths, pages, strict=True):
            if not isinstance(page, PageReadResult):
                raise CurrentStateProjectionError(
                    "CurrentStateSourcePages must contain only PageReadResult instances."
                )
            if page.page_path != expected_page_path:
                raise CurrentStateProjectionError(
                    "CurrentStateSourcePages must use the canonical current-state "
                    f"page set for target_key={validated_target_key!r}; expected "
                    f"{expected_page_path!r}, got {page.page_path!r}."
                )

        object.__setattr__(self, "target_key", validated_target_key)

    def as_tuple(self) -> tuple[PageReadResult, ...]:
        """Return the canonical ordered source-page tuple."""
        return (
            self.thesis,
            self.timeline,
            self.risks,
            self.watchlist,
            self.index,
            self.log,
        )

    @property
    def page_paths(self) -> tuple[str, ...]:
        """Return the canonical ordered logical page paths used by projection."""
        return tuple(page.page_path for page in self.as_tuple())


@dataclass(frozen=True, slots=True)
class CurrentStateRenderModel:
    """Read-only model handed from projection loading into rendering."""

    target_key: str
    source_pages: CurrentStateSourcePages
    cited_evidence: tuple[EvidenceLedgerRecord, ...]
    generated_at: datetime
    decision_episodes: tuple[DecisionEpisodeProjection, ...] = ()
    portfolio_state: PortfolioState | None = None
    latest_pm_decision: PMDecision | None = None

    def __post_init__(self) -> None:
        validated_target_key = validate_target_key(
            self.target_key,
            error_type=CurrentStateProjectionError,
        )
        if not isinstance(self.source_pages, CurrentStateSourcePages):
            raise CurrentStateProjectionError(
                "source_pages must be a CurrentStateSourcePages instance."
            )
        if self.source_pages.target_key != validated_target_key:
            raise CurrentStateProjectionError(
                "source_pages.target_key must match CurrentStateRenderModel.target_key."
            )

        normalized_evidence: list[EvidenceLedgerRecord] = []
        for record in self.cited_evidence:
            if not isinstance(record, EvidenceLedgerRecord):
                raise CurrentStateProjectionError(
                    "cited_evidence must contain only EvidenceLedgerRecord instances."
                )
            if record.target_key != validated_target_key:
                raise CurrentStateProjectionError(
                    "cited_evidence must stay within the active projection target_key."
                )
            normalized_evidence.append(record)

        object.__setattr__(self, "target_key", validated_target_key)
        object.__setattr__(self, "cited_evidence", tuple(normalized_evidence))
        object.__setattr__(
            self,
            "generated_at",
            validate_timestamp(
                self.generated_at,
                field_name="generated_at",
                error_type=CurrentStateProjectionError,
            ),
        )
        object.__setattr__(self, "decision_episodes", tuple(self.decision_episodes))
        if self.portfolio_state is not None:
            if not isinstance(self.portfolio_state, PortfolioState):
                raise CurrentStateProjectionError(
                    "portfolio_state must be a PortfolioState when provided."
                )
            if self.portfolio_state.target_key != validated_target_key:
                raise CurrentStateProjectionError(
                    "portfolio_state.target_key must match the projection target_key."
                )
        if self.latest_pm_decision is not None:
            if not isinstance(self.latest_pm_decision, PMDecision):
                raise CurrentStateProjectionError(
                    "latest_pm_decision must be a PMDecision when provided."
                )
            if self.latest_pm_decision.target_key != validated_target_key:
                raise CurrentStateProjectionError(
                    "latest_pm_decision.target_key must match the projection target_key."
                )

    @property
    def cited_event_ids(self) -> tuple[str, ...]:
        """Return cited event ids in stable render order."""
        return tuple(record.event_id for record in self.cited_evidence)

    def build_receipt(
        self,
        *,
        artifact_path: Path | None = None,
        failure_reason: str | None = None,
    ) -> CurrentStateRenderReceipt:
        """Build the observable projection receipt for this render attempt."""
        return CurrentStateRenderReceipt(
            target_key=self.target_key,
            source_pages=self.source_pages.page_paths,
            cited_event_ids=self.cited_event_ids,
            cited_evidence_count=len(self.cited_evidence),
            artifact_path=artifact_path,
            generated_at=self.generated_at,
            failure_reason=failure_reason,
        )


@dataclass(frozen=True, slots=True)
class CurrentStateRenderReceipt:
    """Observable receipt for one current-state render attempt."""

    target_key: str
    source_pages: tuple[str, ...]
    cited_event_ids: tuple[str, ...]
    cited_evidence_count: int
    artifact_path: Path | None
    generated_at: datetime
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=CurrentStateProjectionError,
            ),
        )
        object.__setattr__(
            self,
            "source_pages",
            _normalize_source_page_paths(self.source_pages, target_key=self.target_key),
        )
        object.__setattr__(
            self,
            "cited_event_ids",
            _normalize_event_ids(self.cited_event_ids),
        )
        if (
            not isinstance(self.cited_evidence_count, int)
            or self.cited_evidence_count < 0
        ):
            raise CurrentStateProjectionError("cited_evidence_count must be >= 0.")
        if self.cited_evidence_count != len(self.cited_event_ids):
            raise CurrentStateProjectionError(
                "cited_evidence_count must match the number of cited_event_ids."
            )
        if self.artifact_path is not None and not isinstance(self.artifact_path, Path):
            raise CurrentStateProjectionError(
                "artifact_path must be a pathlib.Path when provided."
            )
        object.__setattr__(
            self,
            "generated_at",
            validate_timestamp(
                self.generated_at,
                field_name="generated_at",
                error_type=CurrentStateProjectionError,
            ),
        )
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                normalize_content(
                    self.failure_reason,
                    field_name="failure_reason",
                    error_type=CurrentStateProjectionError,
                ),
            )


@dataclass(frozen=True, slots=True)
class CurrentStateRenderOutput:
    """Projection renderer output with explicit content and observability."""

    content_md: str
    receipt: CurrentStateRenderReceipt

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "content_md",
            normalize_content(
                self.content_md,
                field_name="content_md",
                error_type=CurrentStateProjectionError,
            ),
        )
        if not isinstance(self.receipt, CurrentStateRenderReceipt):
            raise CurrentStateProjectionError(
                "receipt must be a CurrentStateRenderReceipt instance."
            )


class CurrentStateRenderer(Protocol):
    """Renderer contract for one trader-facing current-state report."""

    def render(self, model: CurrentStateRenderModel) -> CurrentStateRenderOutput:
        """Render one current-state report from the projection model."""


class CurrentStateProjectionLoader:
    """Load the canonical current-state projection model without writing truth."""

    def __init__(
        self,
        read_evidence: EvidenceReader,
        research_memory: ResearchMemoryReadPort,
        read_decision_episodes: DecisionEpisodeReader | None = None,
        read_portfolio_state: PortfolioStateReader | None = None,
        read_latest_pm_decision: PMDecisionReader | None = None,
    ) -> None:
        if not callable(read_evidence):
            raise CurrentStateProjectionError("read_evidence must be callable.")
        _validate_research_memory_reader(research_memory)

        self._read_evidence = read_evidence
        self._research_memory = research_memory
        self._read_decision_episodes = read_decision_episodes
        self._read_portfolio_state = read_portfolio_state
        self._read_latest_pm_decision = read_latest_pm_decision

    def load_model(self, target_key: str) -> CurrentStateRenderModel:
        """Load the canonical current-state page set and cited evidence refs."""
        validated_target_key = validate_target_key(
            target_key,
            error_type=CurrentStateProjectionError,
        )
        pages = self._read_pages(validated_target_key)
        source_pages = CurrentStateSourcePages(
            target_key=validated_target_key,
            thesis=pages[0],
            timeline=pages[1],
            risks=pages[2],
            watchlist=pages[3],
            index=pages[4],
            log=pages[5],
        )
        cited_event_ids = _collect_dashboard_cited_event_ids(source_pages)
        cited_evidence = self._load_cited_evidence(
            cited_event_ids,
            target_key=validated_target_key,
        )
        return CurrentStateRenderModel(
            target_key=validated_target_key,
            source_pages=source_pages,
            cited_evidence=cited_evidence,
            generated_at=datetime.now(tz=UTC),
            decision_episodes=(
                ()
                if self._read_decision_episodes is None
                else self._read_decision_episodes(validated_target_key)
            ),
            portfolio_state=(
                None
                if self._read_portfolio_state is None
                else self._read_portfolio_state(validated_target_key)
            ),
            latest_pm_decision=(
                None
                if self._read_latest_pm_decision is None
                else self._read_latest_pm_decision(validated_target_key)
            ),
        )

    def _read_pages(self, target_key: str) -> tuple[PageReadResult, ...]:
        pages: list[PageReadResult] = []
        for page_path in _source_page_paths(target_key):
            try:
                result = self._research_memory.read_page(page_path)
            except Exception as exc:  # pragma: no cover - defensive dependency wrap
                raise CurrentStateProjectionError(
                    f"Failed to read current-state source page {page_path!r}: {exc}"
                ) from exc

            if not isinstance(result, PageReadResult):
                raise CurrentStateProjectionError(
                    "research_memory.read_page must return a PageReadResult instance "
                    f"for requested page {page_path!r}."
                )
            if result.page_path != page_path:
                raise CurrentStateProjectionError(
                    "research_memory.read_page must return the requested canonical "
                    f"page_path; requested {page_path!r}, got {result.page_path!r}."
                )
            pages.append(result)

        return tuple(pages)

    def _load_cited_evidence(
        self,
        event_ids: tuple[str, ...],
        *,
        target_key: str,
    ) -> tuple[EvidenceLedgerRecord, ...]:
        if not event_ids:
            return ()

        try:
            records = self._read_evidence(list(event_ids))
        except Exception as exc:  # pragma: no cover - defensive dependency wrap
            raise CurrentStateProjectionError(
                "Failed to read cited evidence for current-state projection "
                f"target_key={target_key!r} event_ids={list(event_ids)!r}: {exc}"
            ) from exc

        if not isinstance(records, list):
            raise CurrentStateProjectionError(
                "read_evidence must return a list of EvidenceLedgerRecord instances."
            )
        if len(records) != len(event_ids):
            raise CurrentStateProjectionError(
                "read_evidence must return one EvidenceLedgerRecord per cited event_id."
            )

        normalized: list[EvidenceLedgerRecord] = []
        for expected_event_id, record in zip(event_ids, records, strict=True):
            if not isinstance(record, EvidenceLedgerRecord):
                raise CurrentStateProjectionError(
                    "read_evidence must return only EvidenceLedgerRecord instances."
                )
            if record.event_id != expected_event_id:
                raise CurrentStateProjectionError(
                    "read_evidence must preserve cited event_id order for projection."
                )
            if record.target_key != target_key:
                raise CurrentStateProjectionError(
                    "cited evidence records must match the active projection target_key."
                )
            normalized.append(record)

        return tuple(normalized)


def _collect_dashboard_cited_event_ids(
    source_pages: CurrentStateSourcePages,
) -> tuple[str, ...]:
    """Recover cited ids only from sections rendered in the current dashboard."""
    dashboard_sections = (
        _extract_market_setup_dashboard(source_pages.thesis.content_md),
        _extract_named_section(source_pages.thesis.content_md, "Why Now"),
        _extract_named_section(source_pages.risks.content_md, "Primary Risks"),
        _extract_named_section(
            source_pages.watchlist.content_md,
            "Immediate Watch Items",
        ),
        _extract_named_section(source_pages.index.content_md, "Current Reading Path"),
        _extract_latest_log_entry(source_pages.log.content_md),
    )
    cited_event_ids: list[str] = []
    seen: set[str] = set()
    for section in dashboard_sections:
        for citation in extract_analysis_citations(section):
            event_id = citation.event_id
            if event_id in seen:
                continue
            seen.add(event_id)
            cited_event_ids.append(event_id)

    return tuple(cited_event_ids)


def _extract_market_setup_dashboard(content_md: str) -> str:
    return _extract_named_section(content_md, "Market Setup Dashboard")


def _extract_named_section(content_md: str, section_name: str) -> str:
    normalized = content_md.replace("\r\n", "\n").replace("\r", "\n")
    heading = f"## {section_name}"
    start = normalized.find(heading)
    if start < 0:
        return ""

    section_start = start + len(heading)
    remaining = normalized[section_start:]
    next_heading_match = _SECOND_LEVEL_HEADING_RE.search(remaining)
    return (
        remaining[: next_heading_match.start()]
        if next_heading_match is not None
        else remaining
    ).strip()


def _extract_latest_log_entry(content_md: str) -> str:
    normalized = content_md.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_SECOND_LEVEL_HEADING_RE.finditer(normalized))
    if not matches:
        return ""

    latest_match = matches[-1]
    next_heading_match = _SECOND_LEVEL_HEADING_RE.search(
        normalized,
        latest_match.end(),
    )
    return (
        normalized[latest_match.start() : next_heading_match.start()]
        if next_heading_match is not None
        else normalized[latest_match.start() :]
    ).strip()


def _validate_research_memory_reader(research_memory: ResearchMemoryReadPort) -> None:
    if not callable(getattr(research_memory, "read_page", None)):
        raise CurrentStateProjectionError(
            "research_memory must implement read_page(page_path)."
        )

    for method_name in (*_RESEARCH_MEMORY_WRITE_METHODS, *_RUNTIME_WRITE_METHODS):
        if callable(getattr(research_memory, method_name, None)):
            raise CurrentStateProjectionError(
                "research_memory must be read-only for projection loading; "
                f"{method_name} is not an allowed dependency."
            )


def _normalize_source_page_paths(
    source_pages: tuple[str, ...],
    *,
    target_key: str,
) -> tuple[str, ...]:
    if not isinstance(source_pages, tuple):
        raise CurrentStateProjectionError(
            "source_pages must be a tuple of canonical current-state page paths."
        )

    expected = _source_page_paths(target_key)
    normalized: list[str] = []
    for expected_page_path, page_path in zip(expected, source_pages, strict=True):
        if page_path != expected_page_path:
            raise CurrentStateProjectionError(
                "source_pages must equal the canonical current-state page set for "
                f"target_key={target_key!r}."
            )
        normalized.append(page_path)

    return tuple(normalized)


def _normalize_event_ids(event_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(event_ids, tuple):
        raise CurrentStateProjectionError(
            "cited_event_ids must be a tuple of event ids."
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for event_id in event_ids:
        validated_event_id = validate_event_id(
            event_id,
            error_type=CurrentStateProjectionError,
        )
        if validated_event_id in seen:
            raise CurrentStateProjectionError(
                "cited_event_ids must not contain duplicates."
            )
        seen.add(validated_event_id)
        normalized.append(validated_event_id)

    return tuple(normalized)


def _source_page_paths(target_key: str) -> tuple[str, ...]:
    return tuple(
        f"targets/{target_key}/{page_name}" for page_name in _SOURCE_PAGE_NAMES
    )


__all__ = [
    "CurrentStateProjectionError",
    "CurrentStateProjectionLoader",
    "CurrentStateRenderModel",
    "CurrentStateRenderOutput",
    "CurrentStateRenderReceipt",
    "CurrentStateRenderer",
    "CurrentStateSourcePages",
    "EvidenceReader",
    "PortfolioStateReader",
]
