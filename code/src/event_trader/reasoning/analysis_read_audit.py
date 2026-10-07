"""Provider-neutral grounding audit for analysis reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from event_trader.analysis import AnalysisContext
from event_trader.context_assembly import ContextPacket, GroundingCoverageReceipt
from event_trader.contracts import EvidenceLedgerRecord
from event_trader.contracts.evidence_review import classify_evidence_review_dimensions
from event_trader.reasoning.analysis_contract_repair import (
    ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
    AnalysisContractFailure,
    AnalysisContractRepairRequired,
)

ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS = frozenset(
    {
        "read_market_overview",
        "read_price_volume_window",
        "read_technical_panel",
        "read_option_activity",
        "read_cross_asset_context",
        "read_cross_asset_window",
    }
)
ANALYSIS_MEMORY_READ_OPERATIONS = frozenset(
    {
        "read_page",
        "read_section",
        "read_around_citation",
        "list_pages",
        "search_wiki",
    }
)


@dataclass(frozen=True, slots=True)
class AnalysisReadAuditRequirements:
    """Frozen deterministic grounding obligations supplied to one Analysis attempt."""

    target_key: str
    active_event_ids: tuple[str, ...]
    compiled_event_ids: tuple[str, ...]
    compiled_memory_cards: tuple[str, ...]
    compiler_memory_receipt_ids: tuple[str, ...]
    market_grounding_required: bool
    compiled_market_grounding_count: int


def build_analysis_read_audit_requirements(
    *,
    context: AnalysisContext,
    context_packet: ContextPacket | None,
) -> AnalysisReadAuditRequirements:
    active_event_ids = set(context.request.event_ids)
    compiled_memory_cards, compiler_memory_receipt_ids = _compiled_memory_grounding(
        context_packet=context_packet,
    )
    market_grounding_required = _market_grounding_required(context)
    return AnalysisReadAuditRequirements(
        target_key=context.request.target_key,
        active_event_ids=tuple(context.request.event_ids),
        compiled_event_ids=tuple(
            sorted(
                _compiled_workbench_evidence_event_ids(
                    context_packet=context_packet,
                    active_event_ids=active_event_ids,
                )
            )
        ),
        compiled_memory_cards=compiled_memory_cards,
        compiler_memory_receipt_ids=compiler_memory_receipt_ids,
        market_grounding_required=market_grounding_required,
        compiled_market_grounding_count=_compiled_market_grounding_count(
            context_packet=context_packet,
            market_grounding_required=market_grounding_required,
        ),
    )


def enforce_analysis_read_audit(
    *,
    receipts: tuple[dict[str, object], ...],
    context: AnalysisContext,
    context_packet: ContextPacket | None,
) -> None:
    """Require evidence, memory, and conditionally market grounding."""

    _receipt, error_message = analysis_read_audit(
        receipts=receipts,
        context=context,
        context_packet=context_packet,
    )
    if error_message is not None:
        raise AnalysisContractRepairRequired(
            failure=AnalysisContractFailure(
                surface="read_audit",
                error_code="analysis_read_audit_contract_violation",
                message=error_message,
                suggested_action=(
                    "Satisfy grounding coverage before final boxed JSON. Use "
                    "read_evidence for active event_ids not covered by compiled "
                    "Workbench excerpts, and rely on compiler-receipted Memory "
                    "Impact Lane cards or inspect target research memory with "
                    "explicit read tools."
                ),
            ),
            failure_count=1,
            threshold=ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
        )


def analysis_read_audit(
    *,
    receipts: tuple[dict[str, object], ...],
    context: AnalysisContext,
    context_packet: ContextPacket | None = None,
) -> tuple[GroundingCoverageReceipt, str | None]:
    """Return deterministic grounding coverage and its first contract failure."""

    return analysis_read_audit_for_requirements(
        receipts=receipts,
        requirements=build_analysis_read_audit_requirements(
            context=context,
            context_packet=context_packet,
        ),
    )


def analysis_read_audit_for_requirements(
    *,
    receipts: tuple[dict[str, object], ...],
    requirements: AnalysisReadAuditRequirements,
) -> tuple[GroundingCoverageReceipt, str | None]:
    """Evaluate the same Contract from a frozen, serializable requirement projection."""

    active_event_ids = set(requirements.active_event_ids)
    compiled_event_ids = set(requirements.compiled_event_ids)
    compiled_memory_cards = requirements.compiled_memory_cards
    compiler_memory_receipt_ids = requirements.compiler_memory_receipt_ids
    market_grounding_required = requirements.market_grounding_required
    compiled_market_count = requirements.compiled_market_grounding_count
    read_event_ids: set[str] = set()
    memory_refs: set[str] = set()
    market_refs: set[str] = set()
    for receipt in receipts:
        operation = receipt.get("operation")
        if not isinstance(operation, str):
            continue
        if operation == "read_evidence":
            read_event_ids.update(read_evidence_receipt_event_ids(receipt))
            continue
        if operation in ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS:
            if receipt.get("result_mode") != "duplicate":
                market_refs.add(operation)
            continue
        if operation not in ANALYSIS_MEMORY_READ_OPERATIONS:
            continue
        memory_ref = target_memory_read_ref(
                receipt,
                target_key=requirements.target_key,
        )
        if memory_ref is not None:
            memory_refs.add(memory_ref)
    grounded_event_ids = compiled_event_ids | read_event_ids
    missing_event_ids = sorted(active_event_ids - grounded_event_ids)
    failed_reasons: list[str] = []
    if missing_event_ids:
        failed_reasons.append(f"missing_evidence_event_ids={missing_event_ids}")
    if not compiled_memory_cards and not memory_refs:
        failed_reasons.append(f"missing_target_memory_grounding={requirements.target_key!r}")
    if market_grounding_required and not compiled_market_count and not market_refs:
        failed_reasons.append(f"missing_market_grounding={requirements.target_key!r}")
    if missing_event_ids:
        evidence_grounding: Literal["compiled", "tool_read", "missing"] = "missing"
    elif compiled_event_ids:
        evidence_grounding = "compiled"
    else:
        evidence_grounding = "tool_read"
    coverage_receipt = GroundingCoverageReceipt(
        evidence_grounding=evidence_grounding,
        memory_grounding=(
            "compiled" if compiled_memory_cards else ("tool_read" if memory_refs else "missing")
        ),
        market_grounding=_market_grounding_status_for_receipt(
            required=market_grounding_required,
            compiled_market_count=compiled_market_count,
            market_refs=market_refs,
        ),
        compiled_evidence_event_ids=tuple(
            event_id for event_id in requirements.active_event_ids if event_id in compiled_event_ids
        ),
        tool_read_evidence_event_ids=tuple(
            event_id for event_id in requirements.active_event_ids if event_id in read_event_ids
        ),
        compiled_memory_cards=compiled_memory_cards,
        compiler_memory_read_receipt_ids=compiler_memory_receipt_ids,
        tool_read_memory_refs=tuple(sorted(memory_refs)),
        compiled_market_grounding_count=compiled_market_count,
        tool_read_market_refs=tuple(sorted(market_refs)),
        failed_reasons=tuple(failed_reasons),
    )
    if missing_event_ids:
        return coverage_receipt, (
            "Analysis grounding coverage failed: active evidence must be grounded by "
            "compiled Workbench evidence excerpts or read_evidence(event_ids). "
            f"missing_event_ids={missing_event_ids}."
        )
    if not compiled_memory_cards and not memory_refs:
        return coverage_receipt, (
            "Analysis grounding coverage failed: target memory must be grounded by "
            "Memory Impact Lane cards with compiler read receipts or explicit target "
            "ResearchMemory reads. "
            f"target_key={requirements.target_key!r}."
        )
    if market_grounding_required and not compiled_market_count and not market_refs:
        return coverage_receipt, (
            "Analysis grounding coverage failed: recap-sensitive active evidence "
            "requires compiled Market Lane grounding or explicit market tool reads. "
            f"target_key={requirements.target_key!r}."
        )
    return coverage_receipt, None


def read_evidence_receipt_event_ids(receipt: dict[str, object]) -> set[str]:
    records = receipt.get("records")
    if not isinstance(records, list):
        return set()
    event_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        event_id = record.get("event_id")
        if isinstance(event_id, str) and event_id.strip():
            event_ids.add(event_id.strip())
    return event_ids


def target_memory_read_ref(
    receipt: dict[str, object],
    *,
    target_key: str,
) -> str | None:
    target_prefix = f"targets/{target_key}/"
    accepted_scopes = {
        f"target:{target_key}",
        f"target/{target_key}",
        f"targets/{target_key}",
    }
    page_path = receipt.get("page_path")
    if isinstance(page_path, str):
        normalized_page_path = page_path.replace("\\", "/").strip()
        if normalized_page_path.startswith(target_prefix):
            section_name = receipt.get("section_name")
            if isinstance(section_name, str) and section_name.strip():
                return f"{normalized_page_path}:{section_name.strip()}"
            return normalized_page_path
    scope = receipt.get("scope")
    if isinstance(scope, str) and scope.strip() in accepted_scopes:
        operation = receipt.get("operation")
        return f"{operation}:{scope.strip()}" if isinstance(operation, str) else scope.strip()
    return None


def _compiled_workbench_evidence_event_ids(
    *,
    context_packet: ContextPacket | None,
    active_event_ids: set[str],
) -> set[str]:
    if context_packet is None or context_packet.analysis_workbench is None:
        return set()
    return {
        card.event_id
        for card in context_packet.analysis_workbench.evidence_lane.active_records
        if card.event_id in active_event_ids
        and card.source_excerpt is not None
        and card.grounding_status in {"compiled_excerpt_available", "compiled_excerpt_truncated"}
    }


def _compiled_memory_grounding(
    *,
    context_packet: ContextPacket | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if context_packet is None or context_packet.analysis_workbench is None:
        return (), ()
    available_cards = context_packet.analysis_workbench.memory_impact_lane.available_cards
    if not available_cards:
        return (), ()
    receipts_by_id: dict[str, dict[str, object]] = {}
    for receipt in context_packet.receipts:
        if not isinstance(receipt, dict):
            continue
        if receipt.get("receipt_type") != "workbench_compiler_memory_read":
            continue
        receipt_id = receipt.get("receipt_id")
        if isinstance(receipt_id, str) and receipt_id.strip():
            receipts_by_id[receipt_id.strip()] = receipt
    compiled_card_ids: list[str] = []
    receipt_ids: list[str] = []
    for card in available_cards:
        matched_receipt = receipts_by_id.get(card.compiler_read_receipt_id)
        if matched_receipt is None:
            return (), ()
        if not _compiler_receipt_matches_card(matched_receipt, card=card):
            return (), ()
        compiled_card_ids.append(card.card_id)
        receipt_ids.append(card.compiler_read_receipt_id)
    return tuple(compiled_card_ids), tuple(receipt_ids)


def _market_grounding_required(context: AnalysisContext) -> bool:
    return any(
        _market_grounding_required_for_record(record)
        for record in context.evidence_records
        if record.event_id in set(context.request.event_ids)
    )


def _market_grounding_required_for_record(record: EvidenceLedgerRecord) -> bool:
    dimensions = classify_evidence_review_dimensions(record)
    return (
        dimensions.recap_risk in {"medium", "high"}
        or dimensions.evidence_role == "price_recap"
        or "event_type:price_action" in record.labels
    )


def _compiled_market_grounding_count(
    *,
    context_packet: ContextPacket | None,
    market_grounding_required: bool,
) -> int:
    if not market_grounding_required:
        return 0
    if context_packet is None or context_packet.analysis_workbench is None:
        return 0
    return 1


def _market_grounding_status_for_receipt(
    *,
    required: bool,
    compiled_market_count: int,
    market_refs: set[str],
) -> Literal["compiled", "tool_read", "not_required", "missing"]:
    if not required:
        return "not_required"
    if compiled_market_count:
        return "compiled"
    if market_refs:
        return "tool_read"
    return "missing"


def _compiler_receipt_matches_card(
    receipt: dict[str, object],
    *,
    card: Any,
) -> bool:
    return (
        receipt.get("page_path") == card.page_path
        and receipt.get("section_name") == card.section_name
        and receipt.get("content_sha256") == card.content_hash
        and receipt.get("excerpt_sha256") == card.excerpt_hash
        and receipt.get("surface_type") == "research_memory_section"
    )


__all__ = [
    "ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS",
    "ANALYSIS_MEMORY_READ_OPERATIONS",
    "AnalysisReadAuditRequirements",
    "analysis_read_audit",
    "analysis_read_audit_for_requirements",
    "build_analysis_read_audit_requirements",
    "enforce_analysis_read_audit",
    "read_evidence_receipt_event_ids",
    "target_memory_read_ref",
]
