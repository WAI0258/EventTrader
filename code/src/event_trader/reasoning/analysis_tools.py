"""Project-owned canonical Analysis read, market, validation, and staged-write tools."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from functools import wraps
from hashlib import sha256
from inspect import signature
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

from event_trader.analysis import FileBackedResearchMemoryReader
from event_trader.config import KernelConfig
from event_trader.context_assembly import ContextPacket
from event_trader.contracts import PageReadResult
from event_trader.contracts.analysis_final_payload_validation import (
    validate_analysis_final_payload as validate_analysis_final_payload_contract,
)
from event_trader.contracts.execution_direction_policy import (
    ExecutionDirectionMode,
    validate_execution_direction_mode,
)
from event_trader.contracts.research_memory import (
    ResearchMemoryContractError,
    resolve_page_ref,
)
from event_trader.contracts.view_state_change import MarketDataSeries, MarketMapping
from event_trader.evidence_ledger import (
    FileBackedEvidenceLedger,
)
from event_trader.evidence_ledger import (
    read_evidence as read_ledger_evidence,
)
from event_trader.integrations.analysis_citations import (
    AnalysisCitation,
    AnalysisCitationError,
    canonicalize_analysis_citations,
    ensure_active_citation,
    extract_analysis_citations,
    extract_analysis_event_ids,
    extract_noncanonical_analysis_event_ids,
)
from event_trader.integrations.bounded_context import (
    DEFAULT_EVIDENCE_EXCERPT_CHAR_LIMIT,
    DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
    bounded_text_payload,
)
from event_trader.integrations.markdown_context import (
    MarkdownContextError,
    build_citation_neighborhood,
    build_markdown_page_map,
    find_markdown_section,
)
from event_trader.market.contracts import (
    OptionBarObservation,
    OptionContractSnapshot,
    OptionSelectionPolicy,
    OptionTradeObservation,
)
from event_trader.market.provider import MarketBarsProvider
from event_trader.market.shared_store import (
    SharedMarketDataProvider,
    SharedMarketDataStore,
)
from event_trader.market.terminal import MarketContextTerminal
from event_trader.operator_context import OperatorContextSnapshot
from event_trader.reasoning.analysis_write_support import (
    AnalysisWriteSupportError,
    validate_analysis_write_support_for_tool_context,
)
from event_trader.research_memory.current_view import (
    LEGACY_CURRENT_VIEW_SECTION,
    MARKET_SETUP_DASHBOARD_ERROR_CODE,
    MARKET_SETUP_DASHBOARD_SECTION,
    MarketSetupDashboardError,
    validate_market_setup_dashboard,
)
from event_trader.research_memory.discipline import fixed_target_entry_sections
from event_trader.research_memory.page_writes import (
    FileBackedResearchMemoryPageWriter,
    ResearchMemoryPageWriteContractError,
)
from event_trader.storage import WorkspaceLayout, validate_workspace_layout
from event_trader.thesis_revision.canonical_bundle import canonical_thesis_page_sections
from event_trader.thesis_revision.historical_reader import HistoricalThesisSnapshotReader

_READ_AROUND_CITATION_CHAR_LIMIT = 2_400
@dataclass(slots=True)
class AnalysisToolContext:
    """One Analysis attempt's explicit, isolated business-tool state."""

    canonical_layout: WorkspaceLayout
    stage_layout: WorkspaceLayout
    receipt_path: Path
    active_citations: tuple[AnalysisCitation, ...]
    context_citations: tuple[AnalysisCitation, ...]
    target_key: str
    business_at: datetime | None
    runtime_scope: Literal["live", "replay"]
    execution_direction_mode: ExecutionDirectionMode
    market_config: KernelConfig | None
    operator_context: OperatorContextSnapshot
    market_data_store_root: Path | None = None
    market_data_snapshot_id: str | None = None
    context_packet: ContextPacket | None = None
    included_lesson_ids: tuple[str, ...] = ()
    ledger: FileBackedEvidenceLedger = field(init=False)
    reader: FileBackedResearchMemoryReader | HistoricalThesisSnapshotReader = field(init=False)
    page_writes: FileBackedResearchMemoryPageWriter = field(init=False)
    read_result_registry: dict[str, dict[str, str]] = field(
        init=False,
        default_factory=dict,
    )

    def __post_init__(self) -> None:
        validate_workspace_layout(self.canonical_layout)
        validate_workspace_layout(self.stage_layout)
        if self.stage_layout.root == self.canonical_layout.root:
            raise RuntimeError(
                "Analysis staged writes must not target the canonical workspace root."
            )
        self.receipt_path = self.receipt_path.expanduser().resolve(strict=False)
        if not self.receipt_path.is_relative_to(self.stage_layout.root):
            raise RuntimeError("Analysis receipt_path must stay inside the staged workspace root.")
        if not self.target_key.strip():
            raise RuntimeError("Analysis tool target_key must be non-blank.")
        self.target_key = self.target_key.strip()
        if self.runtime_scope not in {"live", "replay"}:
            raise RuntimeError("Analysis tool runtime_scope must be 'live' or 'replay'.")
        self.execution_direction_mode = validate_execution_direction_mode(
            self.execution_direction_mode,
            error_type=RuntimeError,
        )
        self.market_data_store_root = (
            None
            if self.market_data_store_root is None
            else self.market_data_store_root.expanduser().resolve(strict=False)
        )
        if self.market_data_snapshot_id is not None:
            self.market_data_snapshot_id = self.market_data_snapshot_id.strip() or None
        self.ledger = FileBackedEvidenceLedger(self.canonical_layout)
        if self.operator_context.target_key != self.target_key:
            raise RuntimeError("operator_context target_key must match target_key.")
        if self.runtime_scope == "replay":
            if self.business_at is None:
                raise RuntimeError("Analysis tool business_at is required for replay reads.")
            self.reader = HistoricalThesisSnapshotReader(
                self.canonical_layout,
                business_at=self.business_at,
            )
        else:
            self.reader = FileBackedResearchMemoryReader(self.stage_layout)
        self.page_writes = FileBackedResearchMemoryPageWriter(self.stage_layout)


_ACTIVE_TOOL_CONTEXT: ContextVar[AnalysisToolContext | None] = ContextVar(
    "event_trader_analysis_tool_context",
    default=None,
)


def _active_context() -> AnalysisToolContext:
    context = _ACTIVE_TOOL_CONTEXT.get()
    if context is None:
        raise RuntimeError("Analysis tool call requires an active AnalysisToolGateway.")
    return context


def _read_analysis_page(page_path: str) -> PageReadResult:
    context = _active_context()
    page_ref = resolve_page_ref(page_path)
    if page_ref.page_kind != "operator":
        return context.reader.read_page(page_path)
    if page_ref.target_key != context.target_key:
        raise RuntimeError("operator context read must target the active analysis target.")
    operator_context = context.operator_context
    return PageReadResult(
        page_path=operator_context.page_path,
        content_md=operator_context.content_md,
    )


def _load_active_instrument_basis() -> str | None:
    packet = _active_context().context_packet
    if packet is None or packet.analysis_workbench is None:
        return None
    basis = packet.analysis_workbench.market_lane.tradable_proxy_symbol.strip()
    return basis or None


def validate_analysis_final_payload(payload: dict[str, Any]) -> str:
    """Read-only validation for the exact final boxed analysis JSON payload."""
    result = validate_analysis_final_payload_contract(
        payload,
        expected_target_key=_active_context().target_key,
        expected_event_ids=tuple(
            citation.event_id for citation in _active_context().active_citations
        ),
        included_lesson_ids=_active_context().included_lesson_ids,
        execution_direction_mode=_active_context().execution_direction_mode,
        active_instrument_basis=_load_active_instrument_basis(),
    )
    return _json_text(result.to_json_payload())


def read_evidence(event_ids: list[str]) -> str:
    """Read admitted evidence records by canonical event id list.

    The content field is a bounded working-memory excerpt; hash and truncation
    metadata identify the full ledger record without putting it in the prompt.
    """
    read_key = _read_key("read_evidence", {"event_ids": event_ids})
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="read_evidence",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    raw_records = read_ledger_evidence(event_ids, ledger=_active_context().ledger)
    for record in raw_records:
        _validate_evidence_read_allowed(record)
    records = [_serialize_evidence_record(record) for record in raw_records]
    payload = records
    payload_sha256 = _payload_hash(payload)
    receipt = _record_activity_receipt(
        operation="read_evidence",
        records=records,
        result_mode="full",
        read_key=read_key,
        payload_sha256=payload_sha256,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(payload)


def read_page(page_path: str) -> str:
    """Read one canonical wiki page by logical page_path.

    content_md is a bounded working-memory excerpt of the full wiki page.
    """
    read_key = _read_key("read_page", {"page_path": page_path})
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="read_page",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    page = _read_analysis_page(page_path)
    payload = _serialize_page_read(page.page_path, page.content_md)
    payload_sha256 = _payload_hash(payload)
    receipt = _record_activity_receipt(
        operation="read_page",
        page_path=page.page_path,
        content_md=cast(str, payload["content_md"]),
        result_mode="full",
        read_key=read_key,
        payload_sha256=payload_sha256,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(payload)


def read_section(page_path: str, section_name: str) -> str:
    """Read one named markdown section from a canonical wiki page."""
    read_key = _read_key(
        "read_section",
        {"page_path": page_path, "section_name": section_name},
    )
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="read_section",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    page = _read_analysis_page(page_path)
    payload = _serialize_section_read(
        page_path=page.page_path,
        content_md=page.content_md,
        section_name=section_name,
    )
    payload_sha256 = _payload_hash(payload)
    receipt = _record_activity_receipt(
        operation="read_section",
        page_path=page.page_path,
        section_name=cast(str, payload["section_name"]),
        content_md=cast(str, payload["content_md"]),
        result_mode="full",
        read_key=read_key,
        payload_sha256=payload_sha256,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(payload)


def read_around_citation(page_path: str, event_id: str) -> str:
    """Read bounded neighborhood text around one citation event id in a wiki page."""
    read_key = _read_key(
        "read_around_citation",
        {"page_path": page_path, "event_id": event_id},
    )
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="read_around_citation",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    page = _read_analysis_page(page_path)
    payload = _serialize_citation_neighborhood_read(
        page_path=page.page_path,
        content_md=page.content_md,
        event_id=event_id,
    )
    payload_sha256 = _payload_hash(payload)
    receipt = _record_activity_receipt(
        operation="read_around_citation",
        page_path=page.page_path,
        event_id=cast(str, payload["event_id"]),
        content_md=cast(str, payload["content_md"]),
        result_mode="full",
        read_key=read_key,
        payload_sha256=payload_sha256,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(payload)


def list_pages(scope: str) -> str:
    """List wiki pages in shared, target:<key>, target/<key>, or targets/<key> scope."""
    pages = [asdict(page_ref) for page_ref in _active_context().reader.list_pages(scope)]
    _record_activity_receipt(
        operation="list_pages",
        scope=scope,
    )
    return _json_text(pages)


def search_wiki(query: str, scope: str) -> str:
    """Search wiki content in shared, target:<key>, target/<key>, or targets/<key>."""
    read_key = _read_key("search_wiki", {"query": query, "scope": scope})
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="search_wiki",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    operator_page_path = f"targets/{_active_context().target_key}/operator.md"
    matches = [
        asdict(match)
        for match in _active_context().reader.search_wiki(query, scope)
        if match.page_path != operator_page_path
    ]
    payload = matches
    payload_sha256 = _payload_hash(payload)
    receipt = _record_activity_receipt(
        operation="search_wiki",
        query=query,
        scope=scope,
        matches=matches,
        result_mode="full",
        read_key=read_key,
        payload_sha256=payload_sha256,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(matches)


def read_market_overview(target_key: str, as_of_at: str) -> str:
    """Read compact deterministic market overview for one target and as_of."""
    parsed_as_of = _validate_market_read_scope(
        target_key=target_key,
        as_of_at=as_of_at,
    )
    arguments: dict[str, object] = {"target_key": target_key, "as_of_at": as_of_at}
    read_key = _read_key("read_market_overview", arguments)
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="read_market_overview",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    terminal = _load_market_terminal()
    payload = terminal.read_market_overview(
        target_key=target_key,
        as_of_at=parsed_as_of,
    ).to_dict()
    receipt = _record_market_activity_receipt(
        operation="read_market_overview",
        arguments=arguments,
        payload=payload,
        result_mode="full",
        read_key=read_key,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(payload)


def read_price_volume_window(
    target_key: str,
    as_of_at: str,
    lookback_hours: float,
    granularity: str | None = None,
    max_rows: int = 80,
    include_bars: bool = False,
) -> str:
    """Read completed target bars ending at or before as_of.

    Starts with a compact summary. Set include_bars=true only when bounded
    bars are needed for the current decision.
    """
    parsed_as_of = _validate_market_read_scope(
        target_key=target_key,
        as_of_at=as_of_at,
    )
    arguments = {
        "target_key": target_key,
        "as_of_at": as_of_at,
        "lookback_hours": lookback_hours,
        "granularity": granularity,
        "max_rows": max_rows,
        "include_bars": include_bars,
    }
    read_key = _read_key("read_price_volume_window", arguments)
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="read_price_volume_window",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    terminal = _load_market_terminal()
    raw_payload = terminal.read_price_volume_window(
        target_key=target_key,
        as_of_at=parsed_as_of,
        lookback=timedelta(hours=lookback_hours),
        granularity=granularity,
        max_rows=max_rows,
    ).to_dict()
    payload = _compact_price_volume_window_payload(
        raw_payload,
        include_bars=include_bars,
    )
    receipt = _record_market_activity_receipt(
        operation="read_price_volume_window",
        arguments=arguments,
        payload=payload,
        result_mode="full",
        read_key=read_key,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(payload)


def read_technical_panel(target_key: str, as_of_at: str) -> str:
    """Read deterministic technical panel for one target and as_of."""
    parsed_as_of = _validate_market_read_scope(
        target_key=target_key,
        as_of_at=as_of_at,
    )
    arguments: dict[str, object] = {"target_key": target_key, "as_of_at": as_of_at}
    read_key = _read_key("read_technical_panel", arguments)
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="read_technical_panel",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    terminal = _load_market_terminal()
    payload = terminal.read_technical_panel(
        target_key=target_key,
        as_of_at=parsed_as_of,
    ).to_dict()
    receipt = _record_market_activity_receipt(
        operation="read_technical_panel",
        arguments=arguments,
        payload=payload,
        result_mode="full",
        read_key=read_key,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(payload)


def read_option_activity(
    target_key: str,
    as_of_at: str,
    max_contracts: int = 10,
) -> str:
    """Read compact deterministic option activity for one target and as_of."""
    parsed_as_of = _validate_market_read_scope(
        target_key=target_key,
        as_of_at=as_of_at,
    )
    arguments = {
        "target_key": target_key,
        "as_of_at": as_of_at,
        "max_contracts": max_contracts,
    }
    read_key = _read_key("read_option_activity", arguments)
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="read_option_activity",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    terminal = _load_market_terminal()
    payload = terminal.read_option_activity(
        target_key=target_key,
        as_of_at=parsed_as_of,
        max_contracts=max_contracts,
    ).to_dict()
    receipt = _record_market_activity_receipt(
        operation="read_option_activity",
        arguments=arguments,
        payload=payload,
        result_mode="full",
        read_key=read_key,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(payload)


def read_cross_asset_context(
    target_key: str,
    as_of_at: str,
    include_proxy_details: bool = False,
) -> str:
    """Read configured cross-asset summary for one target and as_of.

    Starts with compact group/status facts. Set include_proxy_details=true only
    when per-proxy details are needed for the current decision.
    """
    parsed_as_of = _validate_market_read_scope(
        target_key=target_key,
        as_of_at=as_of_at,
    )
    arguments = {
        "target_key": target_key,
        "as_of_at": as_of_at,
        "include_proxy_details": include_proxy_details,
    }
    read_key = _read_key("read_cross_asset_context", arguments)
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="read_cross_asset_context",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    terminal = _load_market_terminal()
    raw_payload = terminal.read_cross_asset_context(
        target_key=target_key,
        as_of_at=parsed_as_of,
    ).to_dict()
    payload = _compact_cross_asset_context_payload(
        raw_payload,
        include_proxy_details=include_proxy_details,
    )
    receipt = _record_market_activity_receipt(
        operation="read_cross_asset_context",
        arguments=arguments,
        payload=payload,
        result_mode="full",
        read_key=read_key,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(payload)


def read_cross_asset_window(
    target_key: str,
    as_of_at: str,
    lookback_hours: float,
    max_rows_per_symbol: int = 80,
) -> str:
    """Read bounded completed proxy bars from configured cross-asset groups."""
    parsed_as_of = _validate_market_read_scope(
        target_key=target_key,
        as_of_at=as_of_at,
    )
    arguments = {
        "target_key": target_key,
        "as_of_at": as_of_at,
        "lookback_hours": lookback_hours,
        "max_rows_per_symbol": max_rows_per_symbol,
    }
    read_key = _read_key("read_cross_asset_window", arguments)
    duplicate_payload = _record_duplicate_read_if_seen(
        operation="read_cross_asset_window",
        read_key=read_key,
    )
    if duplicate_payload is not None:
        return _json_text(duplicate_payload)
    terminal = _load_market_terminal()
    payload = terminal.read_cross_asset_window(
        target_key=target_key,
        as_of_at=parsed_as_of,
        lookback=timedelta(hours=lookback_hours),
        max_rows_per_symbol=max_rows_per_symbol,
    ).to_dict()
    receipt = _record_market_activity_receipt(
        operation="read_cross_asset_window",
        arguments=arguments,
        payload=payload,
        result_mode="full",
        read_key=read_key,
    )
    _remember_read_result(read_key, receipt=receipt)
    return _json_text(payload)


def update_page_section(
    page_path: str,
    section_name: str,
    new_content_md: str,
    absence_based_support: dict[str, object] | None = None,
    maturity_based_support: dict[str, object] | None = None,
) -> str:
    """Rewrite only the body of one named section inside an existing canonical wiki page.

    new_content_md must be body-only markdown: do not include the page title, the
    named section heading, or any other required top-level section heading. Fixed
    target pages keep exact top-level section sets, but existing unique nested
    headings such as watch items may be rewritten directly. watchlist.md has
    Immediate Watch Items, Questions To Resolve, Triggers To Escalate;
    timeline.md has Recent Developments, Thesis Shifts, Open Threads; thesis.md
    has Market Setup Dashboard, Why Now, Key Evidence, Invalidation; risks.md
    has Primary Risks, Contradictory Evidence, Failure Conditions.
    """
    try:
        _validate_staged_write_support(
            absence_based_support=absence_based_support,
            maturity_based_support=maturity_based_support,
        )
        cited_content_md = _ensure_active_citation(new_content_md)
        _validate_market_setup_dashboard_section_write(
            page_path=page_path,
            section_name=section_name,
            content_md=cited_content_md,
        )
        written_path = _active_context().page_writes.update_page_section(
            page_path,
            section_name,
            cited_content_md,
        )
    except (AnalysisCitationError, ResearchMemoryContractError, RuntimeError) as exc:
        return _json_text(
            _write_tool_error_payload(
                tool="update_page_section",
                exc=exc,
                page_path=page_path,
                section_name=section_name,
            )
        )
    _record_write_receipt(
        operation="update_page_section",
        written_path=written_path,
        content_md=cited_content_md,
        page_path=page_path,
        section_name=section_name,
        absence_based_support=absence_based_support,
        maturity_based_support=maturity_based_support,
    )
    _clear_read_result_registry()
    return _json_text({"written_path": str(written_path)})


def rewrite_page(
    page_path: str,
    new_content_md: str,
    absence_based_support: dict[str, object] | None = None,
    maturity_based_support: dict[str, object] | None = None,
) -> str:
    """Rewrite one existing canonical wiki page.

    For fixed target pages, new_content_md must include the full canonical page
    skeleton exactly once and in order: watchlist.md, timeline.md, thesis.md, and
    risks.md must preserve their required top-level sections.
    """
    try:
        _validate_staged_write_support(
            absence_based_support=absence_based_support,
            maturity_based_support=maturity_based_support,
        )
        cited_content_md = _ensure_active_citation(new_content_md)
        _validate_market_setup_dashboard_page_rewrite(
            page_path=page_path,
            content_md=cited_content_md,
        )
        written_path = _active_context().page_writes.rewrite_page(
            page_path,
            cited_content_md,
        )
    except (AnalysisCitationError, ResearchMemoryContractError, RuntimeError) as exc:
        return _json_text(
            _write_tool_error_payload(
                tool="rewrite_page",
                exc=exc,
                page_path=page_path,
                section_name=None,
            )
        )
    _record_write_receipt(
        operation="rewrite_page",
        written_path=written_path,
        content_md=cited_content_md,
        page_path=page_path,
        absence_based_support=absence_based_support,
        maturity_based_support=maturity_based_support,
    )
    _clear_read_result_registry()
    return _json_text({"written_path": str(written_path)})


def create_page(
    page_path: str,
    initial_content_md: str,
    absence_based_support: dict[str, object] | None = None,
    maturity_based_support: dict[str, object] | None = None,
) -> str:
    """Create one new canonical wiki page."""
    try:
        _validate_staged_write_support(
            absence_based_support=absence_based_support,
            maturity_based_support=maturity_based_support,
        )
        cited_content_md = _ensure_active_citation(initial_content_md)
        written_path = _active_context().page_writes.create_page(
            page_path,
            cited_content_md,
        )
    except (AnalysisCitationError, ResearchMemoryContractError, RuntimeError) as exc:
        return _json_text(
            _write_tool_error_payload(
                tool="create_page",
                exc=exc,
                page_path=page_path,
                section_name=None,
            )
        )
    _record_write_receipt(
        operation="create_page",
        written_path=written_path,
        content_md=cited_content_md,
        page_path=page_path,
        absence_based_support=absence_based_support,
        maturity_based_support=maturity_based_support,
    )
    _clear_read_result_registry()
    return _json_text({"written_path": str(written_path)})


def _serialize_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _serialize_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_mapping(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _serialize_evidence_record(record: Any) -> dict[str, Any]:
    payload = _serialize_mapping(asdict(record))
    content = payload.get("content")
    if not isinstance(content, str):
        raise RuntimeError("ledger evidence record content must be a string.")
    bounded = bounded_text_payload(
        content,
        limit=DEFAULT_EVIDENCE_EXCERPT_CHAR_LIMIT,
    )
    payload["content"] = bounded["excerpt"]
    payload["content_sha256"] = bounded["sha256"]
    payload["content_char_count"] = bounded["char_count"]
    payload["content_truncated"] = bounded["truncated"]
    return payload


def _serialize_page_read(page_path: str, content_md: str) -> dict[str, object]:
    bounded = bounded_text_payload(
        content_md,
        limit=DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
    )
    return {
        "page_path": page_path,
        "content_md": bounded["excerpt"],
        "content_sha256": bounded["sha256"],
        "content_char_count": bounded["char_count"],
        "content_truncated": bounded["truncated"],
        "page_map": _serialize_page_map(content_md),
    }


def _serialize_section_read(
    *,
    page_path: str,
    content_md: str,
    section_name: str,
) -> dict[str, object]:
    try:
        section = find_markdown_section(
            content_md,
            heading=section_name.strip(),
            excerpt_char_limit=DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
        )
    except MarkdownContextError as exc:
        raise RuntimeError(f"Failed to read markdown section: {exc}") from exc
    if section is None:
        raise RuntimeError(f"Section {section_name.strip()!r} was not found in the requested page.")
    return {
        "page_path": page_path,
        "section_name": section["heading"],
        "heading_level": section["level"],
        "start_offset": section["start_offset"],
        "end_offset": section["end_offset"],
        "content_md": section["content_excerpt"],
        "content_sha256": section["content_sha256"],
        "content_char_count": section["content_char_count"],
        "content_truncated": section["content_truncated"],
    }


def _serialize_citation_neighborhood_read(
    *,
    page_path: str,
    content_md: str,
    event_id: str,
) -> dict[str, object]:
    try:
        neighborhood = build_citation_neighborhood(
            content_md,
            event_id=event_id,
            neighborhood_char_limit=_READ_AROUND_CITATION_CHAR_LIMIT,
            excerpt_char_limit=DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
        )
    except MarkdownContextError as exc:
        raise RuntimeError(f"Failed to read citation neighborhood: {exc}") from exc
    if neighborhood is None:
        raise RuntimeError(f"event_id {event_id!r} was not found in the requested page.")
    return {
        "page_path": page_path,
        "event_id": event_id,
        "anchor_text": neighborhood["anchor_text"],
        "anchor_start_offset": neighborhood["anchor_start_offset"],
        "anchor_end_offset": neighborhood["anchor_end_offset"],
        "neighborhood_start_offset": neighborhood["neighborhood_start_offset"],
        "neighborhood_end_offset": neighborhood["neighborhood_end_offset"],
        "content_md": neighborhood["content_excerpt"],
        "content_sha256": neighborhood["content_sha256"],
        "content_char_count": neighborhood["content_char_count"],
        "content_truncated": neighborhood["content_truncated"],
    }


def _validate_market_setup_dashboard_section_write(
    *,
    page_path: str,
    section_name: str,
    content_md: str,
) -> None:
    try:
        page_ref = resolve_page_ref(page_path)
    except ResearchMemoryContractError:
        return
    if page_ref.page_kind != "thesis":
        return
    normalized_section_name = section_name.strip()
    if normalized_section_name == LEGACY_CURRENT_VIEW_SECTION:
        raise MarketSetupDashboardError(
            "market_setup_dashboard_legacy_section_forbidden: active Analysis writes "
            "must target Market Setup Dashboard, not legacy Current View."
        )
    if normalized_section_name != MARKET_SETUP_DASHBOARD_SECTION:
        return
    validate_market_setup_dashboard(content_md)


def _validate_market_setup_dashboard_page_rewrite(
    *,
    page_path: str,
    content_md: str,
) -> None:
    try:
        page_ref = resolve_page_ref(page_path)
    except ResearchMemoryContractError:
        return
    if page_ref.page_kind != "thesis":
        return
    if _find_level_two_section(content_md, LEGACY_CURRENT_VIEW_SECTION) is not None:
        raise MarketSetupDashboardError(
            "market_setup_dashboard_legacy_section_forbidden: thesis.md rewrite "
            "must use ## Market Setup Dashboard, not ## Current View."
        )
    try:
        section = find_markdown_section(
            content_md,
            heading=MARKET_SETUP_DASHBOARD_SECTION,
            heading_level=2,
            excerpt_char_limit=DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
        )
    except MarkdownContextError as exc:
        raise MarketSetupDashboardError(f"market_setup_dashboard_invalid_markdown: {exc}") from exc
    if section is None:
        raise MarketSetupDashboardError(
            "market_setup_dashboard_missing: thesis.md rewrite must include "
            "## Market Setup Dashboard."
        )
    section_content = section.get("content_excerpt")
    if not isinstance(section_content, str):
        raise MarketSetupDashboardError(
            "market_setup_dashboard_invalid_markdown: Market Setup Dashboard "
            "section content must be text."
        )
    validate_market_setup_dashboard(section_content)


def _find_level_two_section(content_md: str, section_name: str) -> object | None:
    try:
        return find_markdown_section(
            content_md,
            heading=section_name,
            heading_level=2,
            excerpt_char_limit=DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
        )
    except MarkdownContextError:
        return None


def _serialize_page_map(content_md: str) -> list[dict[str, object]]:
    try:
        sections = build_markdown_page_map(
            content_md,
            excerpt_char_limit=DEFAULT_PAGE_EXCERPT_CHAR_LIMIT,
        )
    except MarkdownContextError as exc:
        raise RuntimeError(f"Failed to build markdown page map: {exc}") from exc
    return [
        {
            "heading": section["heading"],
            "level": section["level"],
            "start_offset": section["start_offset"],
            "end_offset": section["end_offset"],
            "content_start_offset": section["content_start_offset"],
            "content_end_offset": section["content_end_offset"],
            "content_sha256": section["content_sha256"],
            "content_char_count": section["content_char_count"],
            "content_truncated": section["content_truncated"],
        }
        for section in sections
    ]


def _read_key(operation: str, arguments: dict[str, object]) -> str:
    context = _active_context()
    identity = {
        "operation": operation,
        "arguments": _serialize_mapping(arguments),
        "target_key": context.target_key,
        "business_at": ("" if context.business_at is None else context.business_at.isoformat()),
        "context_packet_hash": _active_context_packet_hash(),
    }
    return _payload_hash(identity)


def _active_context_packet_hash() -> str | None:
    packet = _active_context().context_packet
    return None if packet is None else packet.packet_hash


def _payload_hash(payload: object) -> str:
    encoded = json.dumps(
        _serialize_mapping(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _record_duplicate_read_if_seen(
    *,
    operation: str,
    read_key: str,
) -> dict[str, object] | None:
    first = _active_context().read_result_registry.get(read_key)
    if first is None:
        return None
    payload: dict[str, object] = {
        "status": "already_read",
        "operation": operation,
        "read_key": read_key,
        "payload_sha256": first["payload_sha256"],
        "first_receipt_id": first["receipt_id"],
        "message": "Same arguments already returned in this analysis attempt.",
    }
    _record_activity_receipt(
        operation=operation,
        result_mode="duplicate",
        read_key=read_key,
        payload_sha256=first["payload_sha256"],
        duplicate_of_receipt_id=first["receipt_id"],
    )
    return payload


def _remember_read_result(read_key: str, *, receipt: dict[str, Any]) -> None:
    receipt_id = receipt.get("receipt_id")
    payload_sha = receipt.get("payload_sha256")
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        raise RuntimeError("read receipt_id must be a non-empty string.")
    if not isinstance(payload_sha, str) or not payload_sha.strip():
        raise RuntimeError("read payload_sha256 must be a non-empty string.")
    _active_context().read_result_registry[read_key] = {
        "receipt_id": receipt_id,
        "payload_sha256": payload_sha,
    }


def _clear_read_result_registry() -> None:
    _active_context().read_result_registry.clear()


def _compact_price_volume_window_payload(
    payload: dict[str, object],
    *,
    include_bars: bool,
) -> dict[str, object]:
    compact = dict(payload)
    bars = compact.get("bars")
    if not isinstance(bars, list):
        compact["bars_included"] = False
        compact["omitted_row_count"] = 0
        compact.pop("bars", None)
        return compact
    compact["bars_included"] = include_bars
    compact["omitted_row_count"] = 0 if include_bars else len(bars)
    compact["latest_bar"] = bars[-1] if bars else None
    compact["window_summary"] = _price_volume_window_summary(bars)
    if not include_bars:
        compact.pop("bars", None)
    return compact


def _price_volume_window_summary(bars: list[object]) -> dict[str, object]:
    bar_dicts = [bar for bar in bars if isinstance(bar, dict)]
    if not bar_dicts:
        return {
            "bar_count": 0,
            "high": None,
            "low": None,
            "open": None,
            "close": None,
            "return": None,
            "volume_total": None,
            "volume_average": None,
        }
    highs = [_float_field(bar, "high") for bar in bar_dicts]
    lows = [_float_field(bar, "low") for bar in bar_dicts]
    volumes = [_float_field(bar, "volume") for bar in bar_dicts]
    first_open = _float_field(bar_dicts[0], "open")
    last_close = _float_field(bar_dicts[-1], "close")
    valid_volumes = [value for value in volumes if value is not None]
    volume_total = sum(valid_volumes) if valid_volumes else None
    window_return = None
    if first_open is not None and first_open != 0.0 and last_close is not None:
        window_return = (last_close / first_open) - 1.0
    return {
        "bar_count": len(bar_dicts),
        "start_at": bar_dicts[0].get("start_at"),
        "end_at": bar_dicts[-1].get("end_at"),
        "high": _max_optional(highs),
        "low": _min_optional(lows),
        "open": first_open,
        "close": last_close,
        "return": window_return,
        "volume_total": volume_total,
        "volume_average": (volume_total / len(valid_volumes) if volume_total is not None else None),
    }


def _compact_cross_asset_context_payload(
    payload: dict[str, object],
    *,
    include_proxy_details: bool,
) -> dict[str, object]:
    compact = dict(payload)
    proxy_summaries = compact.get("proxy_summaries")
    proxies = proxy_summaries if isinstance(proxy_summaries, list) else []
    compact["proxy_details_included"] = include_proxy_details
    compact["proxy_summary_count"] = len(proxies)
    compact["available_proxy_count"] = sum(
        1 for proxy in proxies if isinstance(proxy, dict) and _proxy_status(proxy) == "available"
    )
    compact["proxy_statuses"] = [
        _compact_proxy_status(proxy) for proxy in proxies if isinstance(proxy, dict)
    ]
    if not include_proxy_details:
        compact.pop("proxy_summaries", None)
    return compact


def _compact_proxy_status(proxy: dict[str, object]) -> dict[str, object]:
    compact: dict[str, object] = {
        "group": proxy.get("group"),
        "symbol": proxy.get("symbol"),
        "status": _proxy_status(proxy),
    }
    for field_name in (
        "latest_completed_bar_end_at",
        "latest_close",
        "row_count",
        "truncated",
    ):
        if field_name in proxy:
            compact[field_name] = proxy[field_name]
    return compact


def _proxy_status(proxy: dict[str, object]) -> str:
    status = proxy.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    availability = proxy.get("availability")
    if isinstance(availability, str) and availability.strip():
        return availability.strip()
    if proxy.get("market_context_hash"):
        return "available"
    return "unavailable"


def _float_field(payload: dict[str, object], field_name: str) -> float | None:
    value = payload.get(field_name)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _max_optional(values: list[float | None]) -> float | None:
    real_values = [value for value in values if value is not None]
    return max(real_values) if real_values else None


def _min_optional(values: list[float | None]) -> float | None:
    real_values = [value for value in values if value is not None]
    return min(real_values) if real_values else None


def _json_text(payload: Any) -> str:
    return json.dumps(
        _serialize_mapping(payload),
        ensure_ascii=False,
        indent=2,
    )


def _validate_staged_write_support(
    *,
    absence_based_support: dict[str, object] | None,
    maturity_based_support: dict[str, object] | None,
) -> None:
    """Run the sole support contract before a writer mutates staged memory."""

    context = _active_context()
    validate_analysis_write_support_for_tool_context(
        support_receipt={
            "absence_based_support": absence_based_support,
            "maturity_based_support": maturity_based_support,
        },
        target_key=context.target_key,
        context_packet=context.context_packet,
        prior_receipts=_prior_activity_receipts(context.receipt_path),
    )


def _prior_activity_receipts(receipt_path: Path) -> tuple[dict[str, object], ...]:
    """Return only already-recorded receipts; the candidate write has no side effect yet."""

    if not receipt_path.exists():
        return ()
    receipts: list[dict[str, object]] = []
    with receipt_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError("analysis activity receipt log contains invalid JSON.") from exc
            if isinstance(value, dict):
                receipts.append(value)
    return tuple(receipts)


def _write_tool_error_payload(
    *,
    tool: str,
    exc: BaseException,
    page_path: str | None,
    section_name: str | None,
) -> dict[str, object]:
    error_code, recoverable = _classify_write_tool_error(exc)
    payload: dict[str, object] = {
        "error": str(exc),
        "error_code": error_code,
        "recoverable": recoverable,
        "tool": tool,
        "page_path": page_path,
        "section_name": section_name,
        "allowed_top_sections": _allowed_top_sections(page_path),
        "available_headings": _available_headings(page_path),
        "suggested_action": _write_tool_suggested_action(error_code),
    }
    if error_code == "unknown_event_citation":
        payload["unknown_event_ids"] = _unknown_event_citation_ids(str(exc))
    return payload


def _classify_write_tool_error(exc: BaseException) -> tuple[str, bool]:
    if isinstance(exc, MarketSetupDashboardError):
        return MARKET_SETUP_DASHBOARD_ERROR_CODE, True
    if isinstance(exc, AnalysisWriteSupportError):
        return exc.error_code, True
    if isinstance(exc, ResearchMemoryPageWriteContractError):
        return exc.error_code, exc.recoverable
    message = str(exc)
    if "not loaded as analysis context" in message:
        return "unknown_event_citation", True
    if "noncanonical event_id references" in message:
        return "noncanonical_event_reference", True
    if "Missing required canonical markdown page" in message:
        return "path_error", False
    if (
        "must not target canonical index.md" in message
        or "must not target canonical log.md" in message
        or "is not allowed for canonical page kind" in message
        or "is not creatable by this writer" in message
    ):
        return "write_target_forbidden", False
    if "citation" in message.lower():
        return "citation_error", True
    if "section_name" in message or "Section " in message:
        return "section_contract_error", True
    if isinstance(exc, RuntimeError):
        return "write_tool_runtime_error", False
    return "write_contract_error", True


def _unknown_event_citation_ids(message: str) -> list[str]:
    if ":" not in message:
        return []
    return [
        event_id.strip() for event_id in message.rsplit(":", 1)[1].split(",") if event_id.strip()
    ]


def _write_tool_suggested_action(error_code: str) -> str:
    if error_code == MARKET_SETUP_DASHBOARD_ERROR_CODE:
        return (
            "Retry the thesis.md Market Setup Dashboard write as exposure-blind "
            "market setup memory. Include Market Reference, Setup Frame, Watch "
            "Triggers, and Evidence Gaps, and do not include actual exposure, "
            "target weight, entry, PnL, MFE, or MAE fields."
        )
    if error_code in {
        "missing_section",
        "ambiguous_section",
        "section_contract_error",
        "page_title_write_forbidden",
    }:
        return (
            "Read the page_map, then use the exact existing unique nested heading "
            "or rewrite the parent canonical top-level section."
        )
    if error_code == "fixed_page_skeleton_violation":
        return (
            "Retry with body-only markdown that does not introduce, remove, rename, "
            "or reorder fixed top-level headings."
        )
    if error_code == "citation_error":
        return "Retry with readable active evidence citations in the required markdown form."
    if error_code == "unknown_event_citation":
        return "Do not cite unloaded event_ids; first read the context or remove the citation."
    if error_code == "noncanonical_event_reference":
        return (
            "Rewrite every event reference as a canonical citation in the form "
            "`event_id` | `source_ref`, using active or loaded evidence only."
        )
    if error_code == "support_payload_contract_error":
        return (
            "Retry with absence_based_support/maturity_based_support as structured "
            "objects: used boolean, reason_codes list, domains list, support_refs "
            "list. Omit the support object entirely when not using that reasoning."
        )
    return "Retry only if the error is a mechanical contract issue visible from the payload."


def _allowed_top_sections(page_path: str | None) -> list[str]:
    if page_path is None:
        return []
    try:
        page_ref = resolve_page_ref(page_path)
        if page_ref.target_key is None:
            return []
        page_name = Path(page_ref.page_path).name
        canonical_sections = canonical_thesis_page_sections(page_name)
        if canonical_sections:
            return list(canonical_sections)
        return list(fixed_target_entry_sections(page_name))
    except ResearchMemoryContractError:
        return []


def _available_headings(page_path: str | None) -> list[dict[str, object]]:
    if page_path is None:
        return []
    try:
        page = _read_analysis_page(page_path)
        return [
            {
                "heading": section["heading"],
                "level": section["level"],
            }
            for section in build_markdown_page_map(page.content_md)
        ]
    except Exception:
        return []


def _ensure_active_citation(content_md: str) -> str:
    normalized_content_md = _canonicalize_write_citations(content_md)
    return ensure_active_citation(
        normalized_content_md,
        _active_context().active_citations,
    )


def _canonicalize_write_citations(content_md: str) -> str:
    active_citations = _active_context().active_citations
    source_refs_by_event_id = {
        citation.event_id: citation.source_ref for citation in active_citations
    }
    loaded_source_refs_by_event_id = _loaded_context_citation_registry()
    source_refs_by_event_id.update(loaded_source_refs_by_event_id)
    normalized_content_md = canonicalize_analysis_citations(
        content_md,
        source_refs_by_event_id,
    )
    noncanonical_event_ids = extract_noncanonical_analysis_event_ids(normalized_content_md)
    if noncanonical_event_ids:
        raise RuntimeError(
            "Analysis write content contained noncanonical event_id references: "
            f"{', '.join(sorted(noncanonical_event_ids))}. "
            "Use canonical markdown citations only: `event_id` | `source_ref`."
        )
    cited_event_ids = extract_analysis_event_ids(normalized_content_md)
    missing_event_ids = [
        event_id for event_id in cited_event_ids if event_id not in source_refs_by_event_id
    ]
    if missing_event_ids:
        raise RuntimeError(
            "Analysis write content cited event_ids that were not loaded as "
            "analysis context: "
            f"{', '.join(sorted(missing_event_ids))}"
        )
    return normalized_content_md


def _loaded_context_citation_registry() -> dict[str, str]:
    source_refs_by_event_id = _load_initial_context_citations()
    receipt_path = _analysis_receipt_path()
    if not receipt_path.exists():
        return source_refs_by_event_id
    with receipt_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            normalized = line.strip()
            if not normalized:
                continue
            payload = json.loads(normalized)
            if not isinstance(payload, dict):
                raise RuntimeError("Analysis receipt entries must be JSON objects.")
            if payload.get("result_mode") == "duplicate":
                continue
            operation = payload.get("operation")
            if operation in {"read_page", "read_section", "read_around_citation"}:
                content_md = payload.get("content_md")
                if not isinstance(content_md, str):
                    raise RuntimeError(f"{operation} receipt content_md must be a string.")
                for citation in extract_analysis_citations(content_md):
                    source_refs_by_event_id[citation.event_id] = citation.source_ref
                continue
            if operation == "search_wiki":
                for citation in _extract_search_wiki_citations(payload):
                    source_refs_by_event_id[citation.event_id] = citation.source_ref
                continue
            if operation == "read_evidence":
                records = payload.get("records")
                if not isinstance(records, list):
                    raise RuntimeError(f"{operation} receipt records must be a list.")
                for index, record in enumerate(records):
                    if not isinstance(record, dict):
                        raise RuntimeError(f"{operation} receipt record {index} must be an object.")
                    event_id = record.get("event_id")
                    source_ref = record.get("source_ref")
                    if not isinstance(event_id, str) or not event_id.strip():
                        raise RuntimeError(
                            f"{operation} receipt record {index}.event_id must be a string."
                        )
                    if not isinstance(source_ref, str) or not source_ref.strip():
                        raise RuntimeError(
                            f"{operation} receipt record {index}.source_ref must be a string."
                        )
                    source_refs_by_event_id[event_id.strip()] = source_ref.strip()
    return source_refs_by_event_id


def _extract_search_wiki_citations(
    receipt: dict[str, object],
) -> tuple[AnalysisCitation, ...]:
    matches = receipt.get("matches")
    if not isinstance(matches, list):
        raise RuntimeError("search_wiki receipt matches must be a list.")
    citations: list[AnalysisCitation] = []
    for index, match in enumerate(matches):
        if not isinstance(match, dict):
            raise RuntimeError(f"search_wiki receipt match {index} must be an object.")
        snippet = match.get("snippet")
        if not isinstance(snippet, str):
            raise RuntimeError(f"search_wiki receipt match {index}.snippet must be a string.")
        citations.extend(extract_analysis_citations(snippet))
    return tuple(citations)


def _validate_evidence_read_allowed(record: Any) -> None:
    expected_target_key = _active_context().target_key
    if expected_target_key:
        record_target_key = getattr(record, "target_key", None)
        if record_target_key != expected_target_key:
            raise RuntimeError(
                "read_evidence rejected an event outside the active target: "
                f"event_id={getattr(record, 'event_id', '<unknown>')} "
                f"target_key={record_target_key!r} active_target={expected_target_key!r}"
            )

    business_at = _active_context().business_at
    if business_at is None:
        return
    record_ts_event = getattr(record, "ts_event", None)
    if not isinstance(record_ts_event, datetime):
        raise RuntimeError("ledger evidence record ts_event must be a datetime.")
    if record_ts_event > business_at:
        raise RuntimeError(
            "read_evidence rejected future evidence for the active analysis pass: "
            f"event_id={getattr(record, 'event_id', '<unknown>')} "
            f"ts_event={record_ts_event.isoformat()} "
            f"business_at={business_at.isoformat()}"
        )


def _load_initial_context_citations() -> dict[str, str]:
    return {
        citation.event_id: citation.source_ref for citation in _active_context().context_citations
    }


def _require_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _load_market_terminal() -> MarketContextTerminal:
    context = _active_context()
    config = context.market_config
    if config is None:
        raise RuntimeError("Analysis market_config is required for market tools.")
    provider: MarketBarsProvider | None = None
    store_root = context.market_data_store_root
    if store_root is not None:
        provider_name = (
            config.validation.market_data.provider
            if config.validation is not None
            else "unknown"
        )
        provider = SharedMarketDataProvider(
            store=SharedMarketDataStore(store_root),
            provider_name=provider_name,
            snapshot_id=(
                context.market_data_snapshot_id if config.mode == "replay" else None
            ),
        )
    elif config.mode == "replay":
        provider = _UnavailableReplayMarketDataProvider(
            "replay shared market-data store unavailable"
        )
    return MarketContextTerminal(config=config, market_data_provider=provider)


def _validate_market_read_scope(*, target_key: str, as_of_at: str) -> datetime:
    context = _active_context()
    normalized_target = _require_non_blank(target_key, "target_key")
    if normalized_target != context.target_key:
        raise RuntimeError(
            "Analysis market read rejected a target outside the active analysis pass: "
            f"requested_target={normalized_target!r} "
            f"active_target={context.target_key!r}"
        )
    parsed_as_of = _parse_market_as_of(as_of_at)
    business_at = context.business_at
    if business_at is not None and parsed_as_of > business_at:
        raise RuntimeError(
            "Analysis market read rejected future data for the active analysis pass: "
            f"as_of_at={parsed_as_of.isoformat()} "
            f"business_at={business_at.isoformat()}"
        )
    return parsed_as_of


def _parse_market_as_of(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("as_of_at must be a non-blank ISO-8601 datetime string.")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise RuntimeError("as_of_at must be an ISO-8601 datetime string.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("as_of_at must include an explicit UTC offset.")
    return parsed


class _UnavailableReplayMarketDataProvider:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _raise_unavailable(self) -> NoReturn:
        raise RuntimeError(self.reason)

    def read_series(
        self,
        mapping: MarketMapping,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> MarketDataSeries:
        _ = (mapping, start_at, end_at)
        self._raise_unavailable()

    def read_option_chain(
        self,
        *,
        underlying_symbol: str,
        as_of_at: datetime,
        underlying_price: float,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        _ = (underlying_symbol, as_of_at, underlying_price, policy)
        self._raise_unavailable()

    def read_option_chain_metadata(
        self,
        *,
        underlying_symbol: str,
        as_of_at: datetime,
        underlying_price: float,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        _ = (underlying_symbol, as_of_at, underlying_price, policy)
        self._raise_unavailable()

    def read_latest_option_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        _ = (contracts, as_of_at, policy)
        self._raise_unavailable()

    def read_latest_option_quotes(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        _ = (contracts, as_of_at, policy)
        self._raise_unavailable()

    def read_option_latest_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        _ = (contracts, as_of_at, policy)
        self._raise_unavailable()

    def read_option_latest_quotes(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        as_of_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionContractSnapshot, ...]:
        _ = (contracts, as_of_at, policy)
        self._raise_unavailable()

    def read_option_trades(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        start_at: datetime,
        end_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionTradeObservation, ...]:
        _ = (contracts, start_at, end_at, policy)
        self._raise_unavailable()

    def read_option_bars(
        self,
        *,
        contracts: tuple[OptionContractSnapshot, ...],
        start_at: datetime,
        end_at: datetime,
        policy: OptionSelectionPolicy,
    ) -> tuple[OptionBarObservation, ...]:
        _ = (contracts, start_at, end_at, policy)
        self._raise_unavailable()


def _analysis_receipt_path() -> Path:
    return _active_context().receipt_path


def _record_write_receipt(
    *,
    operation: str,
    written_path: Path,
    content_md: str,
    page_path: str | None = None,
    scope_key: str | None = None,
    section_name: str | None = None,
    absence_based_support: dict[str, object] | None = None,
    maturity_based_support: dict[str, object] | None = None,
) -> None:
    receipt_path = _analysis_receipt_path()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "operation": operation,
        "written_path": str(written_path.resolve(strict=False)),
        "content_md": content_md,
        "page_path": page_path,
        "scope_key": scope_key,
        "section_name": section_name,
        "recorded_at": datetime.now().isoformat(),
    }
    if absence_based_support is not None:
        payload["absence_based_support"] = absence_based_support
    if maturity_based_support is not None:
        payload["maturity_based_support"] = maturity_based_support
    with receipt_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.write("\n")


def _record_activity_receipt(
    *,
    operation: str,
    records: list[Any] | None = None,
    page_path: str | None = None,
    section_name: str | None = None,
    event_id: str | None = None,
    content_md: str | None = None,
    query: str | None = None,
    scope: str | None = None,
    matches: list[Any] | None = None,
    content_sha256: str | None = None,
    content_char_count: int | None = None,
    content_truncated: bool | None = None,
    result_mode: str = "full",
    read_key: str | None = None,
    payload_sha256: str | None = None,
    duplicate_of_receipt_id: str | None = None,
) -> dict[str, Any]:
    receipt_path = _analysis_receipt_path()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_id = _activity_receipt_id(
        operation=operation,
        result_mode=result_mode,
        read_key=read_key,
        payload_sha256=payload_sha256,
        duplicate_of_receipt_id=duplicate_of_receipt_id,
    )
    payload: dict[str, Any] = {
        "receipt_id": receipt_id,
        "operation": operation,
        "recorded_at": datetime.now().isoformat(),
        "result_mode": result_mode,
    }
    if read_key is not None:
        payload["read_key"] = read_key
    if payload_sha256 is not None:
        payload["payload_sha256"] = payload_sha256
    if duplicate_of_receipt_id is not None:
        payload["duplicate_of_receipt_id"] = duplicate_of_receipt_id
    if records is not None:
        payload["records"] = records
    if page_path is not None:
        payload["page_path"] = page_path
    if section_name is not None:
        payload["section_name"] = section_name
    if event_id is not None:
        payload["event_id"] = event_id
    if content_md is not None:
        payload["content_md"] = content_md
    if query is not None:
        payload["query"] = query
    if scope is not None:
        payload["scope"] = scope
    if matches is not None:
        payload["matches"] = matches
    if content_sha256 is not None:
        payload["content_sha256"] = content_sha256
    if content_char_count is not None:
        payload["content_char_count"] = content_char_count
    if content_truncated is not None:
        payload["content_truncated"] = content_truncated
    with receipt_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.write("\n")
    return payload


def _record_market_activity_receipt(
    *,
    operation: str,
    arguments: dict[str, object],
    payload: dict[str, object],
    result_mode: str = "full",
    read_key: str | None = None,
) -> dict[str, Any]:
    receipt_path = _analysis_receipt_path()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    payload_sha256 = _market_payload_hash(payload)
    receipt_id = _activity_receipt_id(
        operation=operation,
        result_mode=result_mode,
        read_key=read_key,
        payload_sha256=payload_sha256,
        duplicate_of_receipt_id=None,
    )
    receipt: dict[str, Any] = {
        "receipt_id": receipt_id,
        "operation": operation,
        "recorded_at": datetime.now().isoformat(),
        "result_mode": result_mode,
        "read_key": read_key,
        "arguments": arguments,
        "status": _market_payload_status(payload),
        "row_count": _market_payload_row_count(payload),
        "contract_count": _market_payload_contract_count(payload),
        "payload_sha256": payload_sha256,
        "truncated": _market_payload_truncated(payload),
    }
    with receipt_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False))
        handle.write("\n")
    return receipt


def _activity_receipt_id(
    *,
    operation: str,
    result_mode: str,
    read_key: str | None,
    payload_sha256: str | None,
    duplicate_of_receipt_id: str | None,
) -> str:
    payload = {
        "operation": operation,
        "result_mode": result_mode,
        "read_key": read_key,
        "payload_sha256": payload_sha256,
        "duplicate_of_receipt_id": duplicate_of_receipt_id,
        "ordinal": _analysis_receipt_ordinal(),
    }
    return f"analysis-tool:{sha256(_json_compact(payload).encode('utf-8')).hexdigest()}"


def _analysis_receipt_ordinal() -> int:
    receipt_path = _analysis_receipt_path()
    if not receipt_path.exists():
        return 0
    try:
        lines = receipt_path.read_text(encoding="utf-8").splitlines()
        return sum(1 for line in lines if line.strip())
    except OSError:
        return 0


def _json_compact(payload: object) -> str:
    return json.dumps(
        _serialize_mapping(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _market_payload_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _market_payload_status(payload: dict[str, object]) -> str:
    status = payload.get("status")
    if isinstance(status, dict):
        value = status.get("status")
        if isinstance(value, str):
            return value
    if payload.get("market_context_hash"):
        return "available"
    availability = payload.get("availability")
    if isinstance(availability, dict):
        values = [value for value in availability.values() if isinstance(value, str)]
        if values and all(value == "available" for value in values):
            return "available"
        if values and any(value == "available" for value in values):
            return "partial"
    return "unavailable"


def _market_payload_row_count(payload: dict[str, object]) -> int | None:
    row_count = payload.get("row_count")
    if isinstance(row_count, int):
        return row_count
    proxy_windows = payload.get("proxy_windows")
    if isinstance(proxy_windows, list):
        total = 0
        for item in proxy_windows:
            if isinstance(item, dict):
                item_row_count = item.get("row_count")
                if isinstance(item_row_count, int):
                    total += item_row_count
        return total
    proxy_summaries = payload.get("proxy_summaries")
    if isinstance(proxy_summaries, list):
        return len(proxy_summaries)
    return None


def _market_payload_contract_count(payload: dict[str, object]) -> int | None:
    contract_count = payload.get("selected_contract_count")
    return contract_count if isinstance(contract_count, int) else None


def _market_payload_truncated(payload: dict[str, object]) -> bool:
    truncated = payload.get("truncated")
    if isinstance(truncated, bool):
        return truncated
    proxy_windows = payload.get("proxy_windows")
    if isinstance(proxy_windows, list):
        return any(bool(item.get("truncated")) for item in proxy_windows if isinstance(item, dict))
    return False


_ANALYSIS_TOOL_OPERATIONS: dict[str, Callable[..., str]] = {
    operation.__name__: operation
    for operation in (
        validate_analysis_final_payload,
        read_evidence,
        read_page,
        read_section,
        read_around_citation,
        list_pages,
        search_wiki,
        update_page_section,
        rewrite_page,
        create_page,
        read_market_overview,
        read_price_volume_window,
        read_technical_panel,
        read_option_activity,
        read_cross_asset_context,
        read_cross_asset_window,
    )
}
ANALYSIS_TOOL_NAMES: tuple[str, ...] = tuple(_ANALYSIS_TOOL_OPERATIONS)


class AnalysisToolGateway:
    """Dispatch canonical Analysis tools inside one isolated attempt context."""

    def __init__(self, *, context: AnalysisToolContext) -> None:
        if not isinstance(context, AnalysisToolContext):
            raise RuntimeError("context must be an AnalysisToolContext instance.")
        self._context = context

    @property
    def context(self) -> AnalysisToolContext:
        return self._context

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call one canonical tool with exact argument binding and isolated state."""
        operation = _ANALYSIS_TOOL_OPERATIONS.get(tool_name)
        if operation is None:
            raise RuntimeError(f"Unknown analysis tool {tool_name!r}.")
        if not isinstance(arguments, dict):
            raise RuntimeError("Analysis tool arguments must be an object.")
        try:
            signature(operation).bind(**arguments)
        except TypeError as exc:
            raise RuntimeError(f"Invalid arguments for analysis tool {tool_name!r}: {exc}") from exc
        if tool_name == "read_around_citation":
            _validate_read_around_citation_target(
                context=self._context,
                page_path=arguments.get("page_path"),
                event_id=arguments.get("event_id"),
            )
        token = _ACTIVE_TOOL_CONTEXT.set(self._context)
        try:
            return operation(**arguments)
        finally:
            _ACTIVE_TOOL_CONTEXT.reset(token)
    def bind_tool(self, tool_name: str) -> Callable[..., str]:
        """Bind one canonical function signature to this gateway for transports."""
        operation = _ANALYSIS_TOOL_OPERATIONS.get(tool_name)
        if operation is None:
            raise RuntimeError(f"Unknown analysis tool {tool_name!r}.")

        @wraps(operation)
        def bound_operation(*args: Any, **kwargs: Any) -> str:
            bound = signature(operation).bind(*args, **kwargs)
            return self.call_tool(tool_name, dict(bound.arguments))

        return bound_operation


def _validate_read_around_citation_target(
    *,
    context: AnalysisToolContext,
    page_path: object,
    event_id: object,
) -> None:
    if not isinstance(page_path, str) or not page_path.strip():
        raise RuntimeError("read_around_citation page_path must be non-blank.")
    if not isinstance(event_id, str) or not event_id.strip():
        raise RuntimeError("read_around_citation event_id must be non-blank.")
    page = context.reader.read_page(page_path)
    content_md = getattr(page, "content_md", None)
    if not isinstance(content_md, str):
        raise RuntimeError("read_around_citation target page returned non-text content.")
    citations = (*context.active_citations, *context.context_citations)
    for citation in citations:
        if citation.event_id != event_id:
            continue
        if build_citation_neighborhood(
            content_md,
            event_id=citation.event_id,
            source_ref=citation.source_ref,
        ) is not None:
            return
    raise RuntimeError(
        "read_around_citation page_path/event_id pair is outside the active "
        "citation topology."
    )


__all__ = [
    "ANALYSIS_TOOL_NAMES",
    "AnalysisToolContext",
    "AnalysisToolGateway",
]
