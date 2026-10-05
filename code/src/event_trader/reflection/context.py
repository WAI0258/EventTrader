"""Reflection context loading grounded in anchor pages, ledger truth, and narrow ports.

This module loads one selected reflection anchor, recovers its cited original
ledger evidence, collects later ledger evidence inside a bounded review window,
and optionally reads subordinate outcome/trade context through injected
read-only ports. It intentionally stops short of review writing so the
read-side anti-cheating boundary stays explicit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from event_trader.contracts import (
    EvidenceLedgerRecord,
    PageReadResult,
    ResearchMemoryReadPort,
)
from event_trader.contracts._validators import (
    normalize_content,
    validate_event_id,
    validate_page_key,
    validate_target_key,
    validate_timestamp,
)
from event_trader.contracts.ports import OutcomeContextPort, TradeContextPort
from event_trader.contracts.research_memory import resolve_reflection_anchor_page_ref
from event_trader.reflection.anchors import (
    ReflectionLogEntry,
    parse_reflection_log_entries,
)

from .contracts import (
    OutcomeContextPacket,
    ReflectionContextPacket,
    ReviewAnchorIdentity,
    ReviewCoverage,
    TradeContextPacket,
)

type EvidenceReader = Callable[[list[str]], list[EvidenceLedgerRecord]]
type LaterEvidenceReader = Callable[
    [datetime, datetime, list[str] | None, list[str]],
    list[EvidenceLedgerRecord],
]

_RESEARCH_MEMORY_WRITE_METHODS = (
    "update_index",
    "append_log_entry",
    "update_page_section",
    "rewrite_page",
    "create_page",
)
_RUNTIME_WRITE_METHODS = ("emit_escalation",)


class ReflectionContextError(ValueError):
    """Raised when reflection context reads or dependencies are malformed."""

    def __init__(
        self,
        message: str,
        *,
        receipt: ReflectionLedgerContextReceipt | None = None,
    ) -> None:
        super().__init__(message)
        self.receipt = receipt


@dataclass(frozen=True, slots=True)
class ReflectionLedgerContextReceipt:
    """Observable receipt for one ledger-backed reflection context load."""

    anchor_id: str
    cited_event_ids: tuple[str, ...]
    later_evidence_window_start: datetime
    later_evidence_window_end: datetime
    later_evidence_target_keys: tuple[str, ...]
    original_evidence_count: int
    later_evidence_event_ids: tuple[str, ...]
    outcome_port_used: bool = False
    trade_port_used: bool = False
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        failure_reason = self.failure_reason
        if failure_reason is not None:
            failure_reason = normalize_content(
                failure_reason,
                field_name="failure_reason",
                error_type=ReflectionContextError,
            )
            object.__setattr__(self, "failure_reason", failure_reason)

        is_failure_receipt = failure_reason is not None
        object.__setattr__(
            self,
            "anchor_id",
            _normalize_anchor_id(self.anchor_id),
        )
        object.__setattr__(
            self,
            "cited_event_ids",
            _normalize_event_ids(
                self.cited_event_ids,
                field_name="cited_event_ids",
                allow_empty=is_failure_receipt,
            ),
        )
        window_start = validate_timestamp(
            self.later_evidence_window_start,
            field_name="later_evidence_window_start",
            error_type=ReflectionContextError,
        )
        window_end = validate_timestamp(
            self.later_evidence_window_end,
            field_name="later_evidence_window_end",
            error_type=ReflectionContextError,
        )
        if window_end <= window_start:
            raise ReflectionContextError(
                "later_evidence_window_end must be later than later_evidence_window_start."
            )
        object.__setattr__(self, "later_evidence_window_start", window_start)
        object.__setattr__(self, "later_evidence_window_end", window_end)
        object.__setattr__(
            self,
            "later_evidence_target_keys",
            _normalize_target_keys(
                self.later_evidence_target_keys,
                allow_empty=is_failure_receipt,
            ),
        )
        if not isinstance(self.original_evidence_count, int):
            raise ReflectionContextError("original_evidence_count must be an int.")
        minimum_original_evidence_count = 0 if is_failure_receipt else 1
        if self.original_evidence_count < minimum_original_evidence_count:
            comparator = ">= 0" if is_failure_receipt else ">= 1"
            raise ReflectionContextError(
                f"original_evidence_count must be {comparator}."
            )
        object.__setattr__(
            self,
            "later_evidence_event_ids",
            _normalize_event_ids(
                self.later_evidence_event_ids,
                field_name="later_evidence_event_ids",
                allow_empty=True,
            ),
        )
        if not isinstance(self.outcome_port_used, bool):
            raise ReflectionContextError("outcome_port_used must be a bool.")
        if not isinstance(self.trade_port_used, bool):
            raise ReflectionContextError("trade_port_used must be a bool.")


@dataclass(frozen=True, slots=True)
class ReflectionLedgerContext:
    """One anchor plus ledger-backed and subordinate review context inputs."""

    anchor: ReviewAnchorIdentity
    anchor_entry: ReflectionLogEntry
    coverage: ReviewCoverage
    original_evidence: tuple[EvidenceLedgerRecord, ...]
    later_evidence: tuple[EvidenceLedgerRecord, ...]
    receipt: ReflectionLedgerContextReceipt
    outcome_context: OutcomeContextPacket | None = None
    trade_context: TradeContextPacket | None = None
    review_packet: ReflectionContextPacket | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, ReviewAnchorIdentity):
            raise ReflectionContextError(
                "anchor must be a ReviewAnchorIdentity instance."
            )
        if not isinstance(self.anchor_entry, ReflectionLogEntry):
            raise ReflectionContextError(
                "anchor_entry must be a ReflectionLogEntry instance."
            )
        if not isinstance(self.coverage, ReviewCoverage):
            raise ReflectionContextError("coverage must be a ReviewCoverage instance.")
        object.__setattr__(
            self,
            "original_evidence",
            _normalize_record_tuple(
                self.original_evidence,
                field_name="original_evidence",
            ),
        )
        object.__setattr__(
            self,
            "later_evidence",
            _normalize_record_tuple(
                self.later_evidence,
                field_name="later_evidence",
                allow_empty=True,
            ),
        )
        if not isinstance(self.receipt, ReflectionLedgerContextReceipt):
            raise ReflectionContextError(
                "receipt must be a ReflectionLedgerContextReceipt instance."
            )
        if self.anchor_entry.anchor_id != self.receipt.anchor_id:
            raise ReflectionContextError(
                "receipt.anchor_id must match anchor_entry.anchor_id."
            )
        if self.anchor_entry.anchor_id != _anchor_id_for_identity(self.anchor):
            raise ReflectionContextError("anchor_entry.anchor_id must match anchor.")
        if self.receipt.cited_event_ids != self.anchor_entry.cited_event_ids:
            raise ReflectionContextError(
                "receipt.cited_event_ids must match anchor_entry.cited_event_ids."
            )
        if self.receipt.original_evidence_count != len(self.original_evidence):
            raise ReflectionContextError(
                "receipt.original_evidence_count must match original_evidence length."
            )
        if self.receipt.later_evidence_event_ids != tuple(
            record.event_id for record in self.later_evidence
        ):
            raise ReflectionContextError(
                "receipt.later_evidence_event_ids must match later_evidence order."
            )
        if self.outcome_context is not None:
            if not isinstance(self.outcome_context, OutcomeContextPacket):
                raise ReflectionContextError(
                    "outcome_context must be an OutcomeContextPacket instance when provided."
                )
            if self.outcome_context.anchor != self.anchor:
                raise ReflectionContextError(
                    "outcome_context.anchor must match anchor."
                )
            if self.outcome_context.coverage != self.coverage:
                raise ReflectionContextError(
                    "outcome_context.coverage must match coverage."
                )
        elif self.receipt.outcome_port_used:
            raise ReflectionContextError(
                "receipt.outcome_port_used=True requires outcome_context to be present."
            )
        if self.trade_context is not None:
            if not isinstance(self.trade_context, TradeContextPacket):
                raise ReflectionContextError(
                    "trade_context must be a TradeContextPacket instance when provided."
                )
            if self.trade_context.anchor != self.anchor:
                raise ReflectionContextError("trade_context.anchor must match anchor.")
            if self.trade_context.coverage != self.coverage:
                raise ReflectionContextError(
                    "trade_context.coverage must match coverage."
                )
            if not self.receipt.trade_port_used:
                raise ReflectionContextError(
                    "trade_context requires receipt.trade_port_used=True."
                )

        review_packet = self.review_packet
        if review_packet is None and self.outcome_context is not None:
            review_packet = ReflectionContextPacket(
                anchor=self.anchor,
                coverage=self.coverage,
                outcome_context=self.outcome_context,
                trade_context=self.trade_context,
            )
        if review_packet is not None:
            if not isinstance(review_packet, ReflectionContextPacket):
                raise ReflectionContextError(
                    "review_packet must be a ReflectionContextPacket instance when provided."
                )
            if review_packet.anchor != self.anchor:
                raise ReflectionContextError("review_packet.anchor must match anchor.")
            if review_packet.coverage != self.coverage:
                raise ReflectionContextError(
                    "review_packet.coverage must match coverage."
                )
            if self.outcome_context is None:
                raise ReflectionContextError(
                    "review_packet requires outcome_context to be present."
                )
            if review_packet.outcome_context != self.outcome_context:
                raise ReflectionContextError(
                    "review_packet.outcome_context must match outcome_context."
                )
            if review_packet.trade_context != self.trade_context:
                raise ReflectionContextError(
                    "review_packet.trade_context must match trade_context."
                )
        object.__setattr__(self, "review_packet", review_packet)

    @property
    def anchor_id(self) -> str:
        """Return the canonical identity for the loaded review anchor."""
        return self.anchor_entry.anchor_id


class ReflectionLedgerContextLoader:
    """Load one reflection anchor plus ledger-backed original/later evidence."""

    def __init__(
        self,
        *,
        read_evidence: EvidenceReader,
        read_later_evidence: LaterEvidenceReader,
        research_memory: ResearchMemoryReadPort,
        outcome_context: OutcomeContextPort | None = None,
        trade_context: TradeContextPort | None = None,
    ) -> None:
        if not callable(read_evidence):
            raise ReflectionContextError("read_evidence must be callable.")
        if not callable(read_later_evidence):
            raise ReflectionContextError("read_later_evidence must be callable.")
        _validate_research_memory_reader(research_memory)
        _validate_outcome_context_port(outcome_context)
        _validate_trade_context_port(trade_context)

        self._read_evidence = read_evidence
        self._read_later_evidence = read_later_evidence
        self._research_memory = research_memory
        self._outcome_context = outcome_context
        self._trade_context = trade_context

    def load_context(
        self,
        *,
        anchor: ReviewAnchorIdentity,
        coverage: ReviewCoverage,
    ) -> ReflectionLedgerContext:
        """Load one reflection anchor context without bypassing ledger truth."""
        if not isinstance(anchor, ReviewAnchorIdentity):
            raise ReflectionContextError(
                "anchor must be a ReviewAnchorIdentity instance."
            )
        if not isinstance(coverage, ReviewCoverage):
            raise ReflectionContextError("coverage must be a ReviewCoverage instance.")

        anchor_entry = self._read_anchor_entry(anchor)
        if not anchor_entry.cited_event_ids:
            detail = (
                "reflection anchor must cite at least one canonical evidence pair "
                "using the documented `event_id` | `source_ref` format before "
                "ledger-backed review context can load it."
            )
            raise _context_failure(
                anchor=anchor,
                coverage=coverage,
                cited_event_ids=(),
                detail=detail,
                outcome_port_used=self._outcome_context is not None,
                trade_port_used=self._trade_context is not None,
            )

        original_evidence = self._load_original_evidence(anchor, coverage, anchor_entry)
        later_target_keys = _derive_later_target_keys(anchor, original_evidence)
        later_window_end = _later_evidence_window_end(anchor, coverage)
        later_evidence = self._load_later_evidence(
            anchor=anchor,
            coverage=coverage,
            cited_event_ids=anchor_entry.cited_event_ids,
            target_keys=later_target_keys,
            window_end=later_window_end,
        )
        outcome_packet = self._load_outcome_context(
            anchor=anchor,
            coverage=coverage,
            cited_event_ids=anchor_entry.cited_event_ids,
            target_keys=later_target_keys,
            window_end=later_window_end,
        )
        trade_packet = self._load_trade_context(
            anchor=anchor,
            coverage=coverage,
            cited_event_ids=anchor_entry.cited_event_ids,
            target_keys=later_target_keys,
            window_end=later_window_end,
        )

        receipt = ReflectionLedgerContextReceipt(
            anchor_id=anchor_entry.anchor_id,
            cited_event_ids=anchor_entry.cited_event_ids,
            later_evidence_window_start=anchor.logged_at,
            later_evidence_window_end=later_window_end,
            later_evidence_target_keys=later_target_keys,
            original_evidence_count=len(original_evidence),
            later_evidence_event_ids=tuple(
                record.event_id for record in later_evidence
            ),
            outcome_port_used=self._outcome_context is not None,
            trade_port_used=self._trade_context is not None,
        )
        return ReflectionLedgerContext(
            anchor=anchor,
            anchor_entry=anchor_entry,
            coverage=coverage,
            original_evidence=original_evidence,
            later_evidence=later_evidence,
            receipt=receipt,
            outcome_context=outcome_packet,
            trade_context=trade_packet,
        )

    def _read_anchor_entry(self, anchor: ReviewAnchorIdentity) -> ReflectionLogEntry:
        page = self._read_anchor_page(anchor.log_page_path)
        try:
            entries = parse_reflection_log_entries(
                log_page_path=page.page_path,
                content_md=page.content_md,
            )
        except Exception as exc:
            raise ReflectionContextError(
                "Failed to parse reflection anchor page "
                f"{anchor.log_page_path!r} for anchor_id={_anchor_id_for_identity(anchor)!r}: {exc}"
            ) from exc

        matching_entries = [
            entry for entry in entries if entry.entry_key == anchor.entry_key
        ]
        if not matching_entries:
            raise ReflectionContextError(
                "Failed to resolve selected reflection anchor "
                f"anchor_id={_anchor_id_for_identity(anchor)!r} from log page "
                f"{anchor.log_page_path!r}."
            )
        if len(matching_entries) > 1:
            raise ReflectionContextError(
                "Reflection anchor page resolved multiple entries for "
                f"anchor_id={_anchor_id_for_identity(anchor)!r}; the anchor surface is ambiguous."
            )

        anchor_entry = matching_entries[0]
        if anchor_entry.logged_at != anchor.logged_at:
            raise ReflectionContextError(
                "Reflection anchor identity drifted while loading context for "
                f"anchor_id={_anchor_id_for_identity(anchor)!r}; expected logged_at "
                f"{anchor.logged_at.isoformat()}, got {anchor_entry.logged_at!r}."
            )
        return anchor_entry

    def _read_anchor_page(self, page_path: str) -> PageReadResult:
        try:
            page = self._research_memory.read_page(page_path)
        except Exception as exc:  # pragma: no cover - defensive dependency wrap
            raise ReflectionContextError(
                f"Failed to read reflection anchor page {page_path!r}: {exc}"
            ) from exc

        if not isinstance(page, PageReadResult):
            raise ReflectionContextError(
                "research_memory.read_page must return a PageReadResult instance "
                f"for requested page {page_path!r}."
            )
        if page.page_path != page_path:
            raise ReflectionContextError(
                "research_memory.read_page must return the requested canonical "
                f"page_path; requested {page_path!r}, got {page.page_path!r}."
            )
        return page

    def _load_original_evidence(
        self,
        anchor: ReviewAnchorIdentity,
        coverage: ReviewCoverage,
        anchor_entry: ReflectionLogEntry,
    ) -> tuple[EvidenceLedgerRecord, ...]:
        cited_event_ids = anchor_entry.cited_event_ids
        try:
            records = self._read_evidence(list(cited_event_ids))
        except Exception as exc:  # pragma: no cover - defensive dependency wrap
            raise ReflectionContextError(
                _format_failure(
                    anchor=anchor,
                    coverage=coverage,
                    cited_event_ids=cited_event_ids,
                    detail=f"failed to read cited original evidence from the ledger: {exc}",
                )
            ) from exc

        if not isinstance(records, list):
            raise ReflectionContextError(
                "read_evidence must return a list of EvidenceLedgerRecord instances."
            )
        if len(records) != len(cited_event_ids):
            raise ReflectionContextError(
                _format_failure(
                    anchor=anchor,
                    coverage=coverage,
                    cited_event_ids=cited_event_ids,
                    detail=(
                        "read_evidence must return one EvidenceLedgerRecord per cited "
                        "event_id."
                    ),
                )
            )

        normalized: list[EvidenceLedgerRecord] = []
        cited_source_refs = set(anchor_entry.cited_source_refs)
        for expected_event_id, record in zip(cited_event_ids, records, strict=True):
            if not isinstance(record, EvidenceLedgerRecord):
                raise ReflectionContextError(
                    "read_evidence must return only EvidenceLedgerRecord instances."
                )
            if record.event_id != expected_event_id:
                raise ReflectionContextError(
                    _format_failure(
                        anchor=anchor,
                        coverage=coverage,
                        cited_event_ids=cited_event_ids,
                        detail=(
                            "read_evidence must preserve cited event_id order for "
                            "reflection context loading."
                        ),
                    )
                )
            if anchor.target_key is not None and record.target_key != anchor.target_key:
                raise ReflectionContextError(
                    _format_failure(
                        anchor=anchor,
                        coverage=coverage,
                        cited_event_ids=cited_event_ids,
                        detail=(
                            "target-scoped reflection anchors must load cited evidence only "
                            "from the same target ledger partition."
                        ),
                    )
                )
            if cited_source_refs and record.source_ref not in cited_source_refs:
                raise ReflectionContextError(
                    _format_failure(
                        anchor=anchor,
                        coverage=coverage,
                        cited_event_ids=cited_event_ids,
                        detail=(
                            "ledger-backed cited evidence did not match the source_ref values "
                            "named on the anchor."
                        ),
                    )
                )
            normalized.append(record)

        return tuple(normalized)

    def _load_later_evidence(
        self,
        *,
        anchor: ReviewAnchorIdentity,
        coverage: ReviewCoverage,
        cited_event_ids: tuple[str, ...],
        target_keys: tuple[str, ...],
        window_end: datetime,
    ) -> tuple[EvidenceLedgerRecord, ...]:
        try:
            records = self._read_later_evidence(
                anchor.logged_at,
                window_end,
                list(target_keys) if target_keys else None,
                list(cited_event_ids),
            )
        except Exception as exc:  # pragma: no cover - defensive dependency wrap
            detail = f"failed to read later evidence from the ledger window: {exc}"
            raise _context_failure(
                anchor=anchor,
                coverage=coverage,
                cited_event_ids=cited_event_ids,
                target_keys=target_keys,
                detail=detail,
                original_evidence_count=len(cited_event_ids),
                outcome_port_used=self._outcome_context is not None,
                trade_port_used=self._trade_context is not None,
            ) from exc

        if not isinstance(records, list):
            raise ReflectionContextError(
                "read_later_evidence must return a list of EvidenceLedgerRecord instances."
            )

        normalized: list[EvidenceLedgerRecord] = []
        seen: set[str] = set()
        previous_sort_key: tuple[datetime, datetime, str] | None = None
        for record in records:
            if not isinstance(record, EvidenceLedgerRecord):
                raise ReflectionContextError(
                    "read_later_evidence must return only EvidenceLedgerRecord instances."
                )
            if record.event_id in cited_event_ids:
                detail = (
                    "later evidence window must exclude the anchor's cited original "
                    f"event_ids; got {record.event_id!r}."
                )
                raise _context_failure(
                    anchor=anchor,
                    coverage=coverage,
                    cited_event_ids=cited_event_ids,
                    target_keys=target_keys,
                    detail=detail,
                    original_evidence_count=len(cited_event_ids),
                    outcome_port_used=self._outcome_context is not None,
                    trade_port_used=self._trade_context is not None,
                )
            if record.event_id in seen:
                detail = (
                    "later evidence window must not contain duplicate event_ids; got "
                    f"{record.event_id!r}."
                )
                raise _context_failure(
                    anchor=anchor,
                    coverage=coverage,
                    cited_event_ids=cited_event_ids,
                    target_keys=target_keys,
                    detail=detail,
                    original_evidence_count=len(cited_event_ids),
                    outcome_port_used=self._outcome_context is not None,
                    trade_port_used=self._trade_context is not None,
                )
            if target_keys and record.target_key not in target_keys:
                detail = (
                    "later evidence window returned a record outside the bounded "
                    f"target set: {record.target_key!r}."
                )
                raise _context_failure(
                    anchor=anchor,
                    coverage=coverage,
                    cited_event_ids=cited_event_ids,
                    target_keys=target_keys,
                    detail=detail,
                    original_evidence_count=len(cited_event_ids),
                    outcome_port_used=self._outcome_context is not None,
                    trade_port_used=self._trade_context is not None,
                )
            if record.ts_event <= anchor.logged_at or record.ts_event > window_end:
                detail = (
                    "later evidence window returned out-of-window ledger evidence "
                    f"event_id={record.event_id!r} ts_event={record.ts_event.isoformat()}."
                )
                raise _context_failure(
                    anchor=anchor,
                    coverage=coverage,
                    cited_event_ids=cited_event_ids,
                    target_keys=target_keys,
                    detail=detail,
                    original_evidence_count=len(cited_event_ids),
                    outcome_port_used=self._outcome_context is not None,
                    trade_port_used=self._trade_context is not None,
                )
            sort_key = (record.ts_event, record.ts_init, record.event_id)
            if previous_sort_key is not None and sort_key < previous_sort_key:
                detail = (
                    "read_later_evidence must preserve chronological ledger order "
                    "inside the review window."
                )
                raise _context_failure(
                    anchor=anchor,
                    coverage=coverage,
                    cited_event_ids=cited_event_ids,
                    target_keys=target_keys,
                    detail=detail,
                    original_evidence_count=len(cited_event_ids),
                    outcome_port_used=self._outcome_context is not None,
                    trade_port_used=self._trade_context is not None,
                )
            previous_sort_key = sort_key
            seen.add(record.event_id)
            normalized.append(record)

        return tuple(normalized)

    def _load_outcome_context(
        self,
        *,
        anchor: ReviewAnchorIdentity,
        coverage: ReviewCoverage,
        cited_event_ids: tuple[str, ...],
        target_keys: tuple[str, ...],
        window_end: datetime,
    ) -> OutcomeContextPacket | None:
        if self._outcome_context is None:
            return None

        try:
            packet = self._outcome_context.read_outcome_context(anchor, coverage)
        except Exception as exc:  # pragma: no cover - defensive dependency wrap
            detail = (
                "outcome context port failed while reading the selected "
                f"anchor: {exc}"
            )
            raise _context_failure(
                anchor=anchor,
                coverage=coverage,
                cited_event_ids=cited_event_ids,
                target_keys=target_keys,
                detail=detail,
                original_evidence_count=len(cited_event_ids),
                outcome_port_used=True,
                trade_port_used=self._trade_context is not None,
            ) from exc

        if packet is None:
            detail = (
                "outcome context port returned missing data for the selected " "anchor."
            )
            raise _context_failure(
                anchor=anchor,
                coverage=coverage,
                cited_event_ids=cited_event_ids,
                target_keys=target_keys,
                detail=detail,
                original_evidence_count=len(cited_event_ids),
                outcome_port_used=True,
                trade_port_used=self._trade_context is not None,
            )
        if not isinstance(packet, OutcomeContextPacket):
            detail = (
                "outcome context port returned malformed data; expected an "
                "OutcomeContextPacket instance."
            )
            raise _context_failure(
                anchor=anchor,
                coverage=coverage,
                cited_event_ids=cited_event_ids,
                target_keys=target_keys,
                detail=detail,
                original_evidence_count=len(cited_event_ids),
                outcome_port_used=True,
                trade_port_used=self._trade_context is not None,
            )

        _validate_port_packet(
            packet=packet,
            packet_name="outcome context",
            anchor=anchor,
            coverage=coverage,
            cited_event_ids=cited_event_ids,
            target_keys=target_keys,
            window_end=window_end,
            outcome_port_used=True,
            trade_port_used=self._trade_context is not None,
        )
        return packet

    def _load_trade_context(
        self,
        *,
        anchor: ReviewAnchorIdentity,
        coverage: ReviewCoverage,
        cited_event_ids: tuple[str, ...],
        target_keys: tuple[str, ...],
        window_end: datetime,
    ) -> TradeContextPacket | None:
        if self._trade_context is None:
            return None

        try:
            packet = self._trade_context.read_trade_context(anchor, coverage)
        except Exception as exc:  # pragma: no cover - defensive dependency wrap
            detail = (
                "trade context port failed while reading the selected " f"anchor: {exc}"
            )
            raise _context_failure(
                anchor=anchor,
                coverage=coverage,
                cited_event_ids=cited_event_ids,
                target_keys=target_keys,
                detail=detail,
                original_evidence_count=len(cited_event_ids),
                outcome_port_used=self._outcome_context is not None,
                trade_port_used=True,
            ) from exc

        if packet is None:
            return None
        if not isinstance(packet, TradeContextPacket):
            detail = (
                "trade context port returned malformed data; expected a "
                "TradeContextPacket instance or None."
            )
            raise _context_failure(
                anchor=anchor,
                coverage=coverage,
                cited_event_ids=cited_event_ids,
                target_keys=target_keys,
                detail=detail,
                original_evidence_count=len(cited_event_ids),
                outcome_port_used=self._outcome_context is not None,
                trade_port_used=True,
            )

        _validate_port_packet(
            packet=packet,
            packet_name="trade context",
            anchor=anchor,
            coverage=coverage,
            cited_event_ids=cited_event_ids,
            target_keys=target_keys,
            window_end=window_end,
            outcome_port_used=self._outcome_context is not None,
            trade_port_used=True,
        )
        return packet


def _validate_research_memory_reader(research_memory: ResearchMemoryReadPort) -> None:
    for method_name in ("read_page", "list_pages", "search_wiki"):
        if not callable(getattr(research_memory, method_name, None)):
            raise ReflectionContextError(
                "research_memory must implement the ResearchMemoryReadPort surface."
            )

    for method_name in (*_RESEARCH_MEMORY_WRITE_METHODS, *_RUNTIME_WRITE_METHODS):
        if callable(getattr(research_memory, method_name, None)):
            raise ReflectionContextError(
                "research_memory must be read-only for reflection context loading; "
                f"{method_name} is not an allowed dependency."
            )


def _validate_outcome_context_port(port: OutcomeContextPort | None) -> None:
    if port is None:
        return
    if not callable(getattr(port, "read_outcome_context", None)):
        raise ReflectionContextError(
            "outcome_context must implement the OutcomeContextPort surface."
        )


def _validate_trade_context_port(port: TradeContextPort | None) -> None:
    if port is None:
        return
    if not callable(getattr(port, "read_trade_context", None)):
        raise ReflectionContextError(
            "trade_context must implement the TradeContextPort surface."
        )


def _validate_port_packet(
    *,
    packet: OutcomeContextPacket | TradeContextPacket,
    packet_name: str,
    anchor: ReviewAnchorIdentity,
    coverage: ReviewCoverage,
    cited_event_ids: tuple[str, ...],
    target_keys: tuple[str, ...],
    window_end: datetime,
    outcome_port_used: bool,
    trade_port_used: bool,
) -> None:
    if packet.anchor != anchor:
        detail = (
            f"{packet_name} port returned malformed data; packet.anchor did "
            "not match the selected anchor."
        )
        raise _context_failure(
            anchor=anchor,
            coverage=coverage,
            cited_event_ids=cited_event_ids,
            target_keys=target_keys,
            detail=detail,
            original_evidence_count=len(cited_event_ids),
            outcome_port_used=outcome_port_used,
            trade_port_used=trade_port_used,
        )
    if packet.coverage != coverage:
        detail = (
            f"{packet_name} port returned malformed data; packet.coverage did "
            "not match the requested review coverage."
        )
        raise _context_failure(
            anchor=anchor,
            coverage=coverage,
            cited_event_ids=cited_event_ids,
            target_keys=target_keys,
            detail=detail,
            original_evidence_count=len(cited_event_ids),
            outcome_port_used=outcome_port_used,
            trade_port_used=trade_port_used,
        )
    if packet.observed_at <= anchor.logged_at or packet.observed_at > window_end:
        detail = (
            f"{packet_name} port returned out-of-window data; "
            f"observed_at={packet.observed_at.isoformat()} must be later than "
            f"anchor.logged_at={anchor.logged_at.isoformat()} and no later than "
            f"window_end={window_end.isoformat()}."
        )
        raise _context_failure(
            anchor=anchor,
            coverage=coverage,
            cited_event_ids=cited_event_ids,
            target_keys=target_keys,
            detail=detail,
            original_evidence_count=len(cited_event_ids),
            outcome_port_used=outcome_port_used,
            trade_port_used=trade_port_used,
        )


def _derive_later_target_keys(
    anchor: ReviewAnchorIdentity,
    original_evidence: tuple[EvidenceLedgerRecord, ...],
) -> tuple[str, ...]:
    if anchor.target_key is not None:
        return (anchor.target_key,)

    target_keys: list[str] = []
    seen: set[str] = set()
    for record in original_evidence:
        if record.target_key in seen:
            continue
        seen.add(record.target_key)
        target_keys.append(record.target_key)
    return tuple(target_keys)


def _later_evidence_window_end(
    anchor: ReviewAnchorIdentity,
    coverage: ReviewCoverage,
) -> datetime:
    return anchor.logged_at + timedelta(hours=max(coverage.horizons_hours))


def _normalize_record_tuple(
    records: tuple[EvidenceLedgerRecord, ...],
    *,
    field_name: str,
    allow_empty: bool = False,
) -> tuple[EvidenceLedgerRecord, ...]:
    if not isinstance(records, tuple):
        raise ReflectionContextError(
            f"{field_name} must be a tuple of EvidenceLedgerRecord values."
        )
    if not allow_empty and not records:
        raise ReflectionContextError(f"{field_name} must not be empty.")

    normalized: list[EvidenceLedgerRecord] = []
    for record in records:
        if not isinstance(record, EvidenceLedgerRecord):
            raise ReflectionContextError(
                f"{field_name} must contain only EvidenceLedgerRecord instances."
            )
        normalized.append(record)
    return tuple(normalized)


def _normalize_anchor_id(value: str) -> str:
    if not isinstance(value, str):
        raise ReflectionContextError("anchor_id must be a string.")
    anchor_id = value.strip()
    if not anchor_id:
        raise ReflectionContextError("anchor_id must not be blank.")
    if anchor_id != value:
        raise ReflectionContextError(
            "anchor_id must not include leading or trailing whitespace."
        )
    log_page_path, separator, entry_key = anchor_id.partition("#")
    if not separator or not entry_key:
        raise ReflectionContextError(
            "anchor_id must use the canonical '<log_page_path>#<entry_key>' format."
        )
    try:
        resolve_reflection_anchor_page_ref(log_page_path)
    except Exception as exc:
        raise ReflectionContextError(str(exc)) from exc
    validate_page_key(
        entry_key,
        field_name="anchor_id entry_key",
        error_type=ReflectionContextError,
    )
    return anchor_id


def _normalize_event_ids(
    event_ids: tuple[str, ...],
    *,
    field_name: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(event_ids, tuple):
        raise ReflectionContextError(f"{field_name} must be a tuple of event ids.")
    if not allow_empty and not event_ids:
        raise ReflectionContextError(f"{field_name} must not be empty.")

    normalized: list[str] = []
    seen: set[str] = set()
    for event_id in event_ids:
        validated_event_id = validate_event_id(
            event_id,
            error_type=ReflectionContextError,
        )
        if validated_event_id in seen:
            raise ReflectionContextError(
                f"{field_name} must not contain duplicate event ids."
            )
        seen.add(validated_event_id)
        normalized.append(validated_event_id)
    return tuple(normalized)


def _normalize_target_keys(
    target_keys: tuple[str, ...],
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(target_keys, tuple):
        raise ReflectionContextError(
            "later_evidence_target_keys must be a tuple of target keys."
        )
    if not target_keys and not allow_empty:
        raise ReflectionContextError("later_evidence_target_keys must not be empty.")

    normalized: list[str] = []
    seen: set[str] = set()
    for target_key in target_keys:
        validated_target_key = validate_target_key(
            target_key,
            error_type=ReflectionContextError,
        )
        if validated_target_key in seen:
            raise ReflectionContextError(
                "later_evidence_target_keys must not contain duplicates."
            )
        seen.add(validated_target_key)
        normalized.append(validated_target_key)
    return tuple(normalized)


def _anchor_id_for_identity(anchor: ReviewAnchorIdentity) -> str:
    return f"{anchor.log_page_path}#{anchor.entry_key}"


def _context_failure(
    *,
    anchor: ReviewAnchorIdentity,
    coverage: ReviewCoverage,
    cited_event_ids: tuple[str, ...],
    detail: str,
    target_keys: tuple[str, ...] | None = None,
    original_evidence_count: int = 0,
    later_evidence_event_ids: tuple[str, ...] = (),
    outcome_port_used: bool = False,
    trade_port_used: bool = False,
) -> ReflectionContextError:
    resolved_target_keys = target_keys
    if resolved_target_keys is None:
        resolved_target_keys = (anchor.target_key,) if anchor.target_key else ()
    return ReflectionContextError(
        _format_failure(
            anchor=anchor,
            coverage=coverage,
            cited_event_ids=cited_event_ids,
            detail=detail,
            target_keys=resolved_target_keys,
        ),
        receipt=ReflectionLedgerContextReceipt(
            anchor_id=_anchor_id_for_identity(anchor),
            cited_event_ids=cited_event_ids,
            later_evidence_window_start=anchor.logged_at,
            later_evidence_window_end=_later_evidence_window_end(anchor, coverage),
            later_evidence_target_keys=resolved_target_keys,
            original_evidence_count=original_evidence_count,
            later_evidence_event_ids=later_evidence_event_ids,
            outcome_port_used=outcome_port_used,
            trade_port_used=trade_port_used,
            failure_reason=detail,
        ),
    )


def _format_failure(
    *,
    anchor: ReviewAnchorIdentity,
    coverage: ReviewCoverage,
    cited_event_ids: tuple[str, ...],
    detail: str,
    target_keys: tuple[str, ...] | None = None,
) -> str:
    later_window_end = _later_evidence_window_end(anchor, coverage)
    target_keys = target_keys or ((anchor.target_key,) if anchor.target_key else ())
    return (
        "Reflection context load failed for "
        f"anchor_id={_anchor_id_for_identity(anchor)!r} "
        f"cited_event_ids={list(cited_event_ids)!r} "
        f"later_evidence_window={{start={anchor.logged_at.isoformat()}, "
        f"end={later_window_end.isoformat()}, target_keys={list(target_keys)!r}}}: "
        f"{detail}"
    )


__all__ = [
    "EvidenceReader",
    "LaterEvidenceReader",
    "ReflectionContextError",
    "ReflectionLedgerContext",
    "ReflectionLedgerContextLoader",
    "ReflectionLedgerContextReceipt",
]
