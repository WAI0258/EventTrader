"""Provider-neutral grounding contract for analysis-owned memory writes."""

from __future__ import annotations

from event_trader.analysis import AnalysisContext
from event_trader.ceau.contracts import UnitFormationLane
from event_trader.context_assembly import ContextPacket
from event_trader.reasoning.analysis_read_audit import (
    ANALYSIS_MEMORY_READ_OPERATIONS,
    read_evidence_receipt_event_ids,
    target_memory_read_ref,
)

_ABSENCE_SUPPORT_REASON_CODES = frozenset(
    {
        "no_news_followup",
        "no_official_followup",
        "no_market_confirmation",
        "no_operator_followup",
    }
)
_MATURITY_SUPPORT_REASON_CODES = frozenset(
    {
        "market_bar_reaction_mature",
        "official_filing_window_complete",
        "operator_admission_visible",
    }
)
_SUPPORT_REASON_PURPOSE: dict[str, str] = {
    "no_news_followup": "absence_inference",
    "no_official_followup": "absence_inference",
    "no_market_confirmation": "absence_inference",
    "no_operator_followup": "absence_inference",
    "market_bar_reaction_mature": "evidence_maturity",
    "official_filing_window_complete": "evidence_maturity",
    "operator_admission_visible": "evidence_maturity",
}
_COMPLETENESS_DOMAINS = frozenset(
    {
        "market_bars",
        "news_web_search",
        "official_filings",
        "operator_admission",
    }
)


class AnalysisWriteSupportError(RuntimeError):
    """Raised when an analysis write claim lacks admitted support."""

    error_code = "write_support_not_grounded_or_advanced"


def analysis_unit_formation_lane(
    *,
    context: AnalysisContext,
    context_packet: ContextPacket | None,
) -> UnitFormationLane | None:
    if context.unit_formation_lane is not None:
        return context.unit_formation_lane
    if context_packet is not None and context_packet.analysis_workbench is not None:
        return context_packet.analysis_workbench.unit_formation_lane
    return None


def collect_analysis_support_refs(
    *,
    write_receipts: tuple[dict[str, object], ...],
    context: AnalysisContext,
    context_packet: ContextPacket | None,
) -> set[str]:
    """Collect support references made visible by reads and compiled workbench lanes."""

    return _collect_analysis_support_refs(
        write_receipts=write_receipts,
        target_key=context.request.target_key,
        context_packet=context_packet,
    )


def _collect_analysis_support_refs(
    *,
    write_receipts: tuple[dict[str, object], ...],
    target_key: str,
    context_packet: ContextPacket | None,
) -> set[str]:

    grounded_refs: set[str] = set()
    for receipt in write_receipts:
        operation = receipt.get("operation")
        if not isinstance(operation, str):
            continue
        if operation == "read_evidence":
            grounded_refs.update(read_evidence_receipt_event_ids(receipt))
            continue
        if operation == "search_wiki":
            matches = receipt.get("matches")
            if isinstance(matches, list):
                for match in matches:
                    if not isinstance(match, dict):
                        continue
                    for key in ("page_path", "scope"):
                        value = match.get(key)
                        if isinstance(value, str):
                            normalized = value.strip()
                            if normalized:
                                grounded_refs.add(normalized)
            scope = receipt.get("scope")
            if isinstance(scope, str):
                normalized_scope = scope.strip()
                if normalized_scope:
                    grounded_refs.add(normalized_scope)
            continue
        if operation in ANALYSIS_MEMORY_READ_OPERATIONS:
            memory_ref = target_memory_read_ref(
                receipt,
                target_key=target_key,
            )
            if memory_ref is not None:
                grounded_refs.add(memory_ref)
            page_path = receipt.get("page_path")
            if isinstance(page_path, str):
                grounded_refs.add(page_path.replace("\\", "/").strip())
            section_name = receipt.get("section_name")
            if isinstance(section_name, str):
                grounded_refs.add(section_name.strip())
            event_id = receipt.get("event_id")
            if isinstance(event_id, str):
                grounded_refs.add(event_id.strip())
    if context_packet is None or context_packet.analysis_workbench is None:
        return grounded_refs

    workbench = context_packet.analysis_workbench
    grounded_refs.update(card.event_id for card in workbench.evidence_lane.active_records)
    for memory_card in workbench.memory_impact_lane.cards:
        grounded_refs.add(memory_card.card_id)
        grounded_refs.add(memory_card.page_path)
        grounded_refs.add(f"{memory_card.page_path}:{memory_card.section_name}")
        if memory_card.compiler_read_receipt_id:
            grounded_refs.add(memory_card.compiler_read_receipt_id)
    for packet_receipt in context_packet.receipts:
        if isinstance(packet_receipt, dict):
            receipt_id = packet_receipt.get("receipt_id")
        else:
            receipt_id = getattr(packet_receipt, "receipt_id", None)
        if isinstance(receipt_id, str):
            normalized = receipt_id.strip()
            if normalized:
                grounded_refs.add(normalized)
    return grounded_refs


def validate_analysis_write_support(
    *,
    support_receipt: dict[str, object],
    unit_formation_lane: UnitFormationLane | None,
    grounded_support_refs: set[str],
) -> None:
    """Require absence and maturity claims to be grounded or lane-advanced."""

    supported_by_lane: set[tuple[str, str]] = set()
    supported_by_ref: set[tuple[str, str]] = set()
    claims: list[tuple[str, str, str]] = []
    for payload_name, support_reason_codes in (
        ("absence_based_support", _ABSENCE_SUPPORT_REASON_CODES),
        ("maturity_based_support", _MATURITY_SUPPORT_REASON_CODES),
    ):
        payload = support_receipt.get(payload_name)
        if payload is None:
            continue
        used, reasons, domains, support_refs = _parse_analysis_support_payload(
            payload,
            payload_name=payload_name,
            support_reason_codes=support_reason_codes,
        )
        if not used:
            continue
        ref_supported = _support_refs_grounded(
            support_refs=support_refs,
            grounded_support_refs=grounded_support_refs,
        )
        for reason_code in reasons:
            purpose = _SUPPORT_REASON_PURPOSE.get(reason_code)
            if purpose is None:
                raise AnalysisWriteSupportError(
                    f"{payload_name}.reason_codes contains unsupported values: {reason_code}"
                )
            for domain in domains:
                claim = (reason_code, domain)
                if unit_formation_lane is not None and _requirement_is_advanced(
                    unit_formation_lane=unit_formation_lane,
                    purpose=purpose,
                    reason_code=reason_code,
                    domain=domain,
                ):
                    supported_by_lane.add(claim)
                if ref_supported:
                    supported_by_ref.add(claim)
                claims.append((payload_name, reason_code, domain))
    missing_claims = [
        f"{claim_payload_name}:{reason_code}:{domain}"
        for claim_payload_name, reason_code, domain in claims
        if (reason_code, domain) not in supported_by_lane
        and (reason_code, domain) not in supported_by_ref
    ]
    if missing_claims:
        raise AnalysisWriteSupportError(
            "analysis write support for absence/maturity claims is not grounded "
            "or advanced in lane: " + ", ".join(missing_claims)
        )


def validate_analysis_write_support_for_tool_context(
    *,
    support_receipt: dict[str, object],
    target_key: str,
    context_packet: ContextPacket | None,
    prior_receipts: tuple[dict[str, object], ...],
) -> None:
    """Validate a staged write before any page or receipt side effect occurs."""

    lane = (
        context_packet.analysis_workbench.unit_formation_lane
        if context_packet is not None and context_packet.analysis_workbench is not None
        else None
    )
    grounded_support_refs = _collect_analysis_support_refs(
        write_receipts=prior_receipts,
        target_key=target_key,
        context_packet=context_packet,
    )
    validate_analysis_write_support(
        support_receipt=support_receipt,
        unit_formation_lane=lane,
        grounded_support_refs=grounded_support_refs,
    )


def _normalize_support_string_tuple(
    value: object,
    *,
    field_name: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if value is None:
        if allow_empty:
            return ()
        raise AnalysisWriteSupportError(f"{field_name} must be a list or tuple.")
    if not isinstance(value, (list, tuple)):
        raise AnalysisWriteSupportError(f"{field_name} must be a list or tuple.")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise AnalysisWriteSupportError(f"{field_name}[{index}] must be a string.")
        normalized_item = item.strip()
        if not normalized_item:
            raise AnalysisWriteSupportError(f"{field_name}[{index}] must not be blank.")
        normalized.append(normalized_item)
    return tuple(normalized)


def _parse_analysis_support_payload(
    payload: object,
    *,
    payload_name: str,
    support_reason_codes: frozenset[str],
) -> tuple[bool, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if not isinstance(payload, dict):
        raise AnalysisWriteSupportError(f"{payload_name} must be an object.")

    used = payload.get("used")
    if not isinstance(used, bool):
        raise AnalysisWriteSupportError(f"{payload_name}.used must be a boolean.")

    reason_codes = _normalize_support_string_tuple(
        payload.get("reason_codes"),
        field_name=f"{payload_name}.reason_codes",
    )
    if not reason_codes:
        raise AnalysisWriteSupportError(f"{payload_name}.reason_codes must not be empty.")
    unsupported_reason_codes = [
        reason_code for reason_code in reason_codes if reason_code not in support_reason_codes
    ]
    if unsupported_reason_codes:
        raise AnalysisWriteSupportError(
            f"{payload_name}.reason_codes contains unsupported values: "
            f"{', '.join(unsupported_reason_codes)}."
        )

    domains = _normalize_support_string_tuple(
        payload.get("domains"),
        field_name=f"{payload_name}.domains",
    )
    if not domains:
        raise AnalysisWriteSupportError(f"{payload_name}.domains must not be empty.")
    unsupported_domains = [domain for domain in domains if domain not in _COMPLETENESS_DOMAINS]
    if unsupported_domains:
        raise AnalysisWriteSupportError(
            f"{payload_name}.domains contains unsupported values: {', '.join(unsupported_domains)}."
        )

    support_refs = _normalize_support_string_tuple(
        payload.get("support_refs"),
        field_name=f"{payload_name}.support_refs",
        allow_empty=True,
    )
    return used, reason_codes, domains, support_refs


def _support_refs_grounded(
    *,
    support_refs: tuple[str, ...],
    grounded_support_refs: set[str],
) -> bool:
    if not support_refs:
        return False
    return any(ref in grounded_support_refs for ref in support_refs)


def _requirement_is_advanced(
    *,
    unit_formation_lane: UnitFormationLane,
    purpose: str,
    reason_code: str,
    domain: str,
) -> bool:
    if reason_code not in unit_formation_lane.advanced_completeness_requirements:
        return False
    return any(
        requirement.purpose == purpose
        and requirement.domain == domain
        and reason_code in requirement.required_for
        for requirement in unit_formation_lane.completeness_requirements
    )


__all__ = [
    "AnalysisWriteSupportError",
    "analysis_unit_formation_lane",
    "collect_analysis_support_refs",
    "validate_analysis_write_support",
    "validate_analysis_write_support_for_tool_context",
]
