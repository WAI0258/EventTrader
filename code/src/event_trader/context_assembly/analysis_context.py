"""Analysis context assembly for structured, role-aware learning surfaces."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from event_trader.ceau.contracts import UnitFormationLane
from event_trader.context_assembly.claim_cards import select_research_claim_cards
from event_trader.context_assembly.packets import (
    CURRENT_MEMORY_READ_POLICY,
    ContextAssemblyError,
    ContextItem,
    ContextPacket,
    ContextPacketRuntimeScope,
    _default_packet_id,
    _market_context_items,
    _normalize_datetime,
    _research_memory_read_receipt,
)
from event_trader.context_assembly.workbench import build_analysis_workbench
from event_trader.contracts import EvidenceLedgerRecord, PageReadResult
from event_trader.learning_cards import (
    FileBackedLearningCardStore,
    LearningCard,
    select_learning_cards,
)
from event_trader.market.contracts import MarketContextSnapshot
from event_trader.operator_context import OperatorContextSnapshot
from event_trader.research_memory import FileBackedClaimRegistry
from event_trader.storage import WorkspaceLayout


def build_analysis_context_packet(
    *,
    layout: WorkspaceLayout,
    target_key: str,
    business_at: datetime,
    budget: int = 8,
    event_types: tuple[str, ...] = (),
    source_kinds: tuple[str, ...] = (),
    evidence_records: tuple[EvidenceLedgerRecord, ...] = (),
    target_pages: tuple[PageReadResult, ...] = (),
    operator_context: OperatorContextSnapshot,
    market_context: MarketContextSnapshot | None = None,
    memory_read_policy: str = CURRENT_MEMORY_READ_POLICY,
    claim_card_budget: int = 12,
    runtime_scope: ContextPacketRuntimeScope = "live",
    run_id: str = "",
    unit_formation_lane: UnitFormationLane | None = None,
) -> ContextPacket:
    del budget, event_types, source_kinds
    if not isinstance(layout, WorkspaceLayout):
        raise ContextAssemblyError("layout must be a WorkspaceLayout instance.")
    business_at = _normalize_datetime(business_at)
    learning_cards, learning_card_receipt = select_analysis_learning_cards(
        store=FileBackedLearningCardStore(layout),
        target_key=target_key,
        business_at=business_at,
    )
    claim_cards, claim_receipt = select_research_claim_cards(
        registry=FileBackedClaimRegistry(layout),
        target_key=target_key,
        business_at=business_at,
        budget=claim_card_budget,
    )
    claim_card_items = tuple(
        ContextItem(
            surface_type="research_claim_card",
            source_id=card.claim_id,
            scope=card.scope,
            business_time=business_at,
            visible_time=card.visible_from,
            content_hash=card.content_hash,
            selection_reason="research_claim_sidecar",
            budget_class="research_claim_card",
            deep_read_allowed=False,
            deep_read_happened=False,
        )
        for card in claim_cards
    )
    memory_read_receipts = _research_memory_read_receipts(
        stage="analysis",
        target_key=target_key,
        business_at=business_at,
        pages=target_pages,
        read_policy=memory_read_policy,
        read_run_id=run_id if runtime_scope == "replay" else "",
    )
    operator_context_receipt = _operator_context_read_receipt(
        stage="analysis",
        operator_context=operator_context,
    )
    items = (
        *_evidence_items(evidence_records),
        *_page_items(
            pages=target_pages,
            scope=f"target:{target_key}",
            business_at=business_at,
        ),
        _operator_context_item(
            operator_context=operator_context,
            business_at=business_at,
        ),
        *_market_context_items(market_context),
        *claim_card_items,
        *_learning_card_items(learning_cards, business_at=business_at),
    )
    packet_id = _default_packet_id(
        stage="analysis",
        target_key=target_key,
        business_at=business_at,
        source_ids=tuple(record.event_id for record in evidence_records),
    )
    analysis_workbench, compiler_memory_read_receipts = build_analysis_workbench(
        target_key=target_key,
        business_at=business_at,
        context_packet_id=packet_id,
        evidence_records=evidence_records,
        target_pages=target_pages,
        operator_context=operator_context,
        claim_card_ids=tuple(card.claim_id for card in claim_cards),
        market_context=market_context,
        memory_read_policy=memory_read_policy,
        runtime_scope=runtime_scope,
        run_id=run_id,
        unit_formation_lane=unit_formation_lane,
    )
    return ContextPacket(
        packet_id=packet_id,
        stage="analysis",
        target_key=target_key,
        business_at=business_at,
        items=items,
        receipts=(
            *memory_read_receipts,
            operator_context_receipt,
            *(
                compiler_receipt.to_json_payload()
                for compiler_receipt in compiler_memory_read_receipts
            ),
            claim_receipt,
            learning_card_receipt,
        ),
        claim_cards=claim_cards,
        learning_cards=learning_cards,
        analysis_workbench=analysis_workbench,
        runtime_scope=runtime_scope,
        run_id=run_id,
    )


def select_analysis_learning_cards(
    *,
    store: FileBackedLearningCardStore,
    target_key: str,
    business_at: datetime,
    budget: int | None = None,
) -> tuple[tuple[LearningCard, ...], dict[str, object]]:
    business_at = _normalize_datetime(business_at)
    selected = select_learning_cards(
        store=store,
        consumer_role="analysis",
        target_key=target_key,
        business_at=business_at,
        budget=budget,
    )
    all_cards = tuple(persisted.record for persisted in store.read_all_cards())
    allowed_scopes = {"shared", f"target:{target_key}"}
    excluded_due_to_role = sum(
        1
        for card in all_cards
        if card.scope_key in allowed_scopes and card.consumer_role != "analysis"
    )
    excluded_due_to_scope = sum(
        1
        for card in all_cards
        if card.consumer_role == "analysis" and card.scope_key not in allowed_scopes
    )
    excluded_due_to_visibility = sum(
        1
        for card in all_cards
        if card.consumer_role == "analysis"
        and card.scope_key in allowed_scopes
        and card.usable_from > business_at
    )
    selected_ids = tuple(card.card_id for card in selected)
    selected_hashes = tuple(card.content_hash for card in selected)
    return selected, {
        "receipt_type": "learning_card_usage",
        "stage": "analysis",
        "consumer_role": "analysis",
        "target_key": target_key,
        "business_at": business_at.isoformat(),
        "selected_card_ids": list(selected_ids),
        "selected_card_hashes": list(selected_hashes),
        "usable_from_max": (
            max(card.usable_from for card in selected).isoformat()
            if selected
            else None
        ),
        "excluded_count_due_to_visibility": excluded_due_to_visibility,
        "excluded_count_due_to_role": excluded_due_to_role,
        "excluded_count_due_to_scope": excluded_due_to_scope,
        "selection_policy_version": "learning_cards_scope_role_usable_from_v1",
    }


def _evidence_items(records: tuple[EvidenceLedgerRecord, ...]) -> tuple[ContextItem, ...]:
    return tuple(
        ContextItem(
            surface_type="evidence",
            source_id=record.event_id,
            scope=f"target:{record.target_key}",
            business_time=record.ts_event,
            visible_time=record.ts_event,
            content_hash=sha256(record.content.encode("utf-8")).hexdigest(),
            selection_reason="active_analysis_event",
            budget_class="analysis_evidence",
            deep_read_allowed=True,
            deep_read_happened=False,
        )
        for record in records
    )


def _page_items(
    *,
    pages: tuple[PageReadResult, ...],
    scope: str,
    business_at: datetime,
) -> tuple[ContextItem, ...]:
    return tuple(
        ContextItem(
            surface_type="research_memory_page",
            source_id=page.page_path,
            scope=scope,
            business_time=business_at,
            visible_time=business_at,
            content_hash=sha256(page.content_md.encode("utf-8")).hexdigest(),
            selection_reason="analysis_seed_page",
            budget_class="analysis_memory_page",
            deep_read_allowed=True,
            deep_read_happened=False,
        )
        for page in pages
    )


def _learning_card_items(
    cards: tuple[LearningCard, ...],
    *,
    business_at: datetime,
) -> tuple[ContextItem, ...]:
    return tuple(
        ContextItem(
            surface_type="learning_card",
            source_id=card.card_id,
            scope=card.scope_key,
            business_time=business_at,
            visible_time=card.usable_from,
            content_hash=card.content_hash,
            selection_reason=f"consumer_role:{card.consumer_role}",
            budget_class="learning_card",
            deep_read_allowed=False,
            deep_read_happened=False,
        )
        for card in cards
    )


def _operator_context_item(
    *,
    operator_context: OperatorContextSnapshot,
    business_at: datetime,
) -> ContextItem:
    return ContextItem(
        surface_type="operator_context",
        source_id=operator_context.page_path,
        scope=f"target:{operator_context.target_key}",
        business_time=business_at,
        visible_time=business_at,
        content_hash=operator_context.content_sha256,
        selection_reason="runtime_operator_control",
        budget_class="operator_context",
        deep_read_allowed=True,
        deep_read_happened=False,
    )


def _research_memory_read_receipts(
    *,
    stage: str,
    target_key: str,
    business_at: datetime,
    pages: tuple[PageReadResult, ...],
    read_policy: str,
    read_run_id: str = "",
) -> tuple[dict[str, object], ...]:
    return tuple(
        _research_memory_read_receipt(
            stage=stage,  # type: ignore[arg-type]
            target_key=target_key,
            page_path=page.page_path,
            business_at=business_at,
            content_sha256=sha256(page.content_md.encode("utf-8")).hexdigest(),
            read_policy=read_policy,
            read_run_id=read_run_id,
            snapshot_source=page.snapshot_source,
            snapshot_revision_id=page.snapshot_revision_id,
            snapshot_committed_at=(
                None
                if page.snapshot_committed_at is None
                else page.snapshot_committed_at.isoformat()
            ),
        )
        for page in pages
    )


def _operator_context_read_receipt(
    *,
    stage: str,
    operator_context: OperatorContextSnapshot,
) -> dict[str, object]:
    return {
        "receipt_type": "operator_context_read",
        "stage": stage,
        "target_key": operator_context.target_key,
        "page_path": operator_context.page_path,
        "content_sha256": operator_context.content_sha256,
        "read_at": operator_context.read_at.isoformat(),
        "read_policy": "analysis_start_operator_context_snapshot_v1",
    }


__all__ = [
    "build_analysis_context_packet",
    "select_analysis_learning_cards",
    "select_research_claim_cards",
]
