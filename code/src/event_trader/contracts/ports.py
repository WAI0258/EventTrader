"""Thin project-semantic adapter protocols for runtime and memory
boundaries."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .evidence import EvidenceLedgerRecord
from .research_memory import PageReadResult, PageRef, WikiMatch

if TYPE_CHECKING:
    from event_trader.contracts.view_state_change import MarketDataSeries, MarketMapping
    from event_trader.reflection.contracts import (
        OutcomeContextPacket,
        ReviewAnchorIdentity,
        ReviewCoverage,
        TradeContextPacket,
    )


class EvidenceLedgerPort(Protocol):
    """Business-shaped ledger seam for admitted evidence truth."""

    def append(self, record: EvidenceLedgerRecord) -> str:
        """Append one durable evidence record and return its immutable event id."""

    def read(self, event_id: str) -> EvidenceLedgerRecord:
        """Read one admitted evidence record by immutable event id."""

    def read_many(self, event_ids: list[str]) -> list[EvidenceLedgerRecord]:
        """Read multiple admitted evidence records for checker or analysis."""


class ResearchMemoryReadPort(Protocol):
    """Business-shaped read seam for library-readable research-memory access."""

    def read_page(self, page_path: str) -> PageReadResult:
        """Read one logical wiki page by canonical library path."""

    def list_pages(self, scope: str) -> list[PageRef]:
        """List wiki pages visible inside one documented scope."""

    def search_wiki(self, query: str, scope: str) -> list[WikiMatch]:
        """Search wiki content inside one documented scope."""


class ResearchMemoryPort(Protocol):
    """Business-shaped write seam for index and log updates."""

    def update_index(self, scope_key: str, new_content_md: str) -> Path:
        """Update the canonical index markdown page for a shared or target scope."""

    def append_log_entry(self, scope_key: str, entry_md: str) -> Path:
        """Append a markdown log entry for a shared or target scope."""


class ResearchMemoryPageWritePort(Protocol):
    """Analysis-facing direct page-write seam for canonical wiki pages."""

    def update_page_section(
        self,
        page_path: str,
        section_name: str,
        new_content_md: str,
    ) -> Path:
        """Rewrite one named section inside a canonical existing wiki page."""

    def rewrite_page(self, page_path: str, new_content_md: str) -> Path:
        """Rewrite one canonical wiki page when section edits no longer fit."""

    def create_page(self, page_path: str, initial_content_md: str) -> Path:
        """Create one new canonical wiki page for a durable research object."""


class EscalationPort(Protocol):
    """Checker-to-analysis runtime handoff seam."""

    def emit_escalation(
        self,
        target_key: str,
        event_ids: list[str],
        why_escalated: str,
        requires_watchlist_maintenance: bool,
    ) -> None:
        """Emit a minimal escalation handoff for downstream analysis."""


class OutcomeContextPort(Protocol):
    """Read-only subordinate seam for structured reflection outcome context.

    This port exists so reflection can load bounded, fixture-friendly outcome
    context without creating a new canonical truth surface, durable store, or
    live fetch path inside the reflection loader itself.
    """

    def read_outcome_context(
        self,
        anchor: ReviewAnchorIdentity,
        coverage: ReviewCoverage,
    ) -> OutcomeContextPacket:
        """Read one required outcome context packet for the selected review anchor."""


class TradeContextPort(Protocol):
    """Read-only subordinate seam for optional reflection trade outcome context."""

    def read_trade_context(
        self,
        anchor: ReviewAnchorIdentity,
        coverage: ReviewCoverage,
    ) -> TradeContextPacket | None:
        """Read one optional trade context packet for the selected review anchor."""


class MarketDataPort(Protocol):
    """Read-only deterministic seam for validation market bars."""

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        """Read bars needed to value the UTC window, including end_at coverage when required."""


__all__ = [
    "EscalationPort",
    "EvidenceLedgerPort",
    "MarketDataPort",
    "OutcomeContextPort",
    "ResearchMemoryPageWritePort",
    "ResearchMemoryPort",
    "ResearchMemoryReadPort",
    "TradeContextPort",
]

