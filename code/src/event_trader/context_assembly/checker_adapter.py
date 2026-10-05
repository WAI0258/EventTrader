"""Checker-stage adapter into shared context packet primitives."""

from __future__ import annotations

from event_trader.checker.context_pack import CheckerContextPack
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


def build_checker_context_packet(
    pack: CheckerContextPack,
    *,
    memory_read_policy: str = CURRENT_MEMORY_READ_POLICY,
    runtime_scope: ContextPacketRuntimeScope = "live",
    run_id: str = "",
) -> ContextPacket:
    if not isinstance(pack, CheckerContextPack):
        raise ContextAssemblyError("pack must be a CheckerContextPack instance.")
    business_at = _normalize_datetime(pack.evidence.ts_event)
    target_key = pack.request.target_key
    if pack.page_read_metadata:
        memory_read_receipts = tuple(
            _research_memory_read_receipt(
                stage="checker",
                target_key=target_key,
                page_path=page_path,
                business_at=business_at,
                content_sha256=metadata.content_sha256,
                read_policy=memory_read_policy,
                read_run_id=run_id if runtime_scope == "replay" else "",
                snapshot_source=metadata.snapshot_source,
                snapshot_revision_id=metadata.snapshot_revision_id,
                snapshot_committed_at=(
                    None
                    if metadata.snapshot_committed_at is None
                    else metadata.snapshot_committed_at.isoformat()
                ),
            )
            for page_path, metadata in sorted(pack.page_read_metadata.items())
        )
    else:
        memory_read_receipts = tuple(
            _research_memory_read_receipt(
                stage="checker",
                target_key=target_key,
                page_path=page_path,
                business_at=business_at,
                content_sha256=content_sha256,
                read_policy=memory_read_policy,
                read_run_id=run_id if runtime_scope == "replay" else "",
            )
            for page_path, content_sha256 in sorted(pack.page_hashes.items())
        )
    items = (
        ContextItem(
            surface_type="evidence",
            source_id=pack.evidence.event_id,
            scope=f"target:{target_key}",
            business_time=pack.evidence.ts_event,
            visible_time=pack.evidence.ts_event,
            content_hash=pack.evidence.content.sha256,
            selection_reason="active_checker_event",
            budget_class="checker_evidence",
            deep_read_allowed=False,
            deep_read_happened=False,
        ),
        *(
            ContextItem(
                surface_type="research_memory_section",
                source_id=section.section_id,
                scope=f"target:{target_key}",
                business_time=business_at,
                visible_time=business_at,
                content_hash=section.content.sha256,
                selection_reason="canonical_checker_section",
                budget_class="checker_memory_section",
                deep_read_allowed=False,
                deep_read_happened=False,
            )
            for section in pack.memory.sections
        ),
        *_market_context_items(pack.market_context),
    )
    return ContextPacket(
        packet_id=_default_packet_id(
            stage="checker",
            target_key=target_key,
            business_at=business_at,
            source_ids=(pack.evidence.event_id,),
        ),
        stage="checker",
        target_key=target_key,
        business_at=business_at,
        items=items,
        receipts=memory_read_receipts,
        claim_cards=(),
        runtime_scope=runtime_scope,
        run_id=run_id,
    )


__all__ = ["build_checker_context_packet"]
