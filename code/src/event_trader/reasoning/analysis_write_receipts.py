"""Provider-neutral admission contract for analysis tool receipts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, cast

from event_trader.contracts.analysis_assessment import AnalysisAssessment
from event_trader.contracts.runtime import AnalysisResult
from event_trader.reasoning.analysis_contract_repair import (
    ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
    AnalysisContractFailure,
    AnalysisContractRepairRequired,
)
from event_trader.reasoning.analysis_read_audit import (
    ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS,
    ANALYSIS_MEMORY_READ_OPERATIONS,
)
from event_trader.research_memory.active_price_projection import (
    project_active_price_sections,
)
from event_trader.research_memory.analysis_commit import (
    AnalysisCommitWrite,
    validate_analysis_commit_contract,
)

AnalysisMaterialWriteOperation = Literal[
    "update_page_section",
    "rewrite_page",
    "create_page",
]

ANALYSIS_READ_RECEIPT_OPERATIONS = frozenset({"read_evidence"}).union(
    ANALYSIS_MEMORY_READ_OPERATIONS,
    ANALYSIS_MARKET_READ_RECEIPT_OPERATIONS,
)
ANALYSIS_MATERIAL_WRITE_OPERATIONS = frozenset(
    {
        "update_page_section",
        "rewrite_page",
        "create_page",
    }
)


class AnalysisWriteReceiptContractError(RuntimeError):
    """Raised when an analysis tool receipt is not admissible for memory writes."""


@dataclass(frozen=True, slots=True)
class AdmittedAnalysisWriteReceipt:
    operation: AnalysisMaterialWriteOperation
    written_path: str
    content_md: str


def admit_analysis_write_receipt(
    receipt: dict[str, object],
) -> AdmittedAnalysisWriteReceipt | None:
    """Return an admitted material write, or None for a recognized read receipt."""

    operation = _require_receipt_text(receipt, "operation")
    if operation in ANALYSIS_READ_RECEIPT_OPERATIONS:
        return None

    written_path = _require_receipt_text(receipt, "written_path")
    content_md = _require_receipt_text(receipt, "content_md")
    if operation == "append_log_entry":
        raise AnalysisWriteReceiptContractError(
            "Analysis attempted append_log_entry, but analysis must not write "
            "research-memory log.md."
        )
    if operation == "update_index":
        raise AnalysisWriteReceiptContractError(
            "Analysis attempted update_index, but analysis must not write index.md."
        )
    if operation not in ANALYSIS_MATERIAL_WRITE_OPERATIONS:
        raise AnalysisWriteReceiptContractError(
            f"Unknown analysis write receipt operation: {operation}"
        )
    return AdmittedAnalysisWriteReceipt(
        operation=cast(AnalysisMaterialWriteOperation, operation),
        written_path=written_path,
        content_md=content_md,
    )


def collapse_analysis_write_receipts(
    write_receipts: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    """Collapse superseded material writes while preserving surviving order."""

    collapsed: list[dict[str, object]] = []
    for receipt in write_receipts:
        operation = _require_receipt_text(receipt, "operation")
        if operation not in ANALYSIS_MATERIAL_WRITE_OPERATIONS:
            collapsed.append(receipt)
            continue
        page_path = _receipt_page_path(receipt)
        if operation == "update_page_section":
            section_name = _require_receipt_text(receipt, "section_name")
            collapsed = [
                existing
                for existing in collapsed
                if not (
                    _optional_receipt_text(existing, "operation") == "update_page_section"
                    and _receipt_page_path(existing) == page_path
                    and _optional_receipt_text(existing, "section_name") == section_name
                )
            ]
            collapsed.append(receipt)
            continue
        if operation in {"rewrite_page", "create_page"}:
            collapsed = [
                existing for existing in collapsed if _receipt_page_path(existing) != page_path
            ]
            collapsed.append(receipt)
            continue
    return tuple(collapsed)


def protected_rewrite_contract_repair_required(
    *,
    write_receipts: tuple[dict[str, object], ...],
    analysis_assessment: AnalysisAssessment | None,
    attempt_index: int,
) -> AnalysisContractRepairRequired | None:
    """Return a repair signal when a full-page rewrite could erase projected sections."""

    failure = protected_rewrite_contract_failure(
        write_receipts=write_receipts,
        analysis_assessment=analysis_assessment,
    )
    if failure is None:
        return None
    return AnalysisContractRepairRequired(
        failure=failure,
        failure_count=attempt_index + 1,
        threshold=ANALYSIS_CONTRACT_REPAIR_MAX_ATTEMPTS,
    )


def protected_rewrite_contract_failure(
    *,
    write_receipts: tuple[dict[str, object], ...],
    analysis_assessment: AnalysisAssessment | None,
) -> AnalysisContractFailure | None:
    """Return the shared protected-section failure without choosing retry ownership."""

    protected_sections_by_page = protected_rewrite_sections_by_page(analysis_assessment)
    if not protected_sections_by_page:
        return None
    for receipt in write_receipts:
        operation = _require_receipt_text(receipt, "operation")
        if operation != "rewrite_page":
            continue
        page_path = _receipt_page_path(receipt)
        protected_sections = protected_sections_by_page.get(page_path)
        if not protected_sections:
            continue
        protected_section_names = ", ".join(
            f"`{section_name}`" for section_name in protected_sections
        )
        message = (
            "Analysis attempted rewrite_page for "
            f"{page_path}, but deterministic active sections must stay "
            f"section-owned: {protected_section_names}."
        )
        return AnalysisContractFailure(
            surface="write_tool",
            error_code="protected_section_rewrite_forbidden",
            message=message,
            recoverable=True,
            suggested_action=(
                "Do not use rewrite_page for this page. Use update_page_section "
                "for the relevant canonical top-level section, then validate the "
                "final payload again."
            ),
            details={
                "tool_name": "rewrite_page",
                "error_code": "protected_section_rewrite_forbidden",
                "arguments": {
                    "page_path": page_path,
                    "protected_sections": list(protected_sections),
                },
                "page_path": page_path,
                "protected_sections": list(protected_sections),
                "allowed_top_sections": list(protected_sections),
                "suggested_action": (
                    "Use update_page_section for canonical top-level sections; "
                    "do not rewrite the full fixed target page."
                ),
            },
        )
    return None


def analysis_revision_plan_contract_failure(
    *,
    target_key: str,
    requires_watchlist_maintenance: bool,
    analysis_assessment: AnalysisAssessment | None,
    revision_intents: tuple[Mapping[str, object], ...],
) -> AnalysisContractFailure | None:
    """Validate plan semantics before any writer can create staged residue."""

    intent_receipts = tuple(dict(intent) for intent in revision_intents)
    protected_rewrite_failure = protected_rewrite_contract_failure(
        write_receipts=intent_receipts,
        analysis_assessment=analysis_assessment,
    )
    if protected_rewrite_failure is not None:
        return protected_rewrite_failure
    try:
        validate_analysis_watchlist_maintenance(
            target_key=target_key,
            required=requires_watchlist_maintenance,
            write_receipts=intent_receipts,
        )
    except AnalysisWriteReceiptContractError as exc:
        required_watchlist_path = f"targets/{target_key}/watchlist.md"
        return AnalysisContractFailure(
            surface="write_tool",
            error_code="missing_watchlist_maintenance",
            message=str(exc),
            suggested_action=(
                "Add one bounded revision intent for the required target watchlist "
                "before writing any ResearchMemory page."
            ),
            details={
                "page_path": required_watchlist_path,
                "requires_watchlist_maintenance": True,
            },
        )
    return None


def protected_rewrite_sections_by_page(
    analysis_assessment: AnalysisAssessment | None,
) -> dict[str, tuple[str, ...]]:
    """Group deterministic assessment-projected sections by normalized page path."""

    protected_rewrite_sections_by_page_lists: dict[str, list[str]] = {}
    for section in project_active_price_sections(analysis_assessment):
        protected_rewrite_sections_by_page_lists.setdefault(section.page_path, [])
        protected_rewrite_sections_by_page_lists[section.page_path].append(
            section.section_name
        )
    return {
        page_path: tuple(section_names)
        for page_path, section_names in protected_rewrite_sections_by_page_lists.items()
    }


def validate_analysis_watchlist_maintenance(
    *,
    target_key: str,
    required: bool,
    write_receipts: tuple[dict[str, object], ...],
) -> None:
    """Require a target watchlist write when a material update requests it."""

    if not required or not write_receipts:
        return
    required_watchlist_path = f"targets/{target_key}/watchlist.md"
    if any(_receipt_page_path(receipt) == required_watchlist_path for receipt in write_receipts):
        return
    raise AnalysisWriteReceiptContractError(
        f"missing_watchlist_maintenance: {required_watchlist_path}"
    )


def validate_analysis_commit_receipts(
    *,
    target_key: str,
    analysis_assessment: AnalysisAssessment | None,
    write_receipts: tuple[dict[str, object], ...],
) -> None:
    """Normalize tool receipts and enforce the final analysis commit contract."""

    validate_analysis_commit_contract(
        target_key=target_key,
        analysis_assessment=analysis_assessment,
        writes=tuple(
            AnalysisCommitWrite(
                operation=_require_receipt_text(receipt, "operation"),
                page_path=_receipt_page_path(receipt),
                content_md=_require_receipt_text(receipt, "content_md"),
                section_name=_optional_receipt_text(receipt, "section_name"),
            )
            for receipt in write_receipts
        ),
    )


def finalize_analysis_write_outcome(
    *,
    result: AnalysisResult,
    saw_material_write: bool,
    write_receipts: tuple[dict[str, object], ...],
) -> tuple[AnalysisResult, tuple[dict[str, object], ...]]:
    """Derive the analysis outcome from admitted material memory writes."""

    if not saw_material_write:
        if write_receipts:
            raise AnalysisWriteReceiptContractError(
                "analysis_write_receipts_contained_no_material_memory_update"
            )
        return (
            AnalysisResult(
                target_key=result.target_key,
                event_ids=result.event_ids,
                outcome="no_update",
                analysis_assessment=result.analysis_assessment,
                used_lesson_ids=result.used_lesson_ids,
            ),
            (),
        )
    return (
        AnalysisResult(
            target_key=result.target_key,
            event_ids=result.event_ids,
            outcome="memory_updated",
            analysis_assessment=result.analysis_assessment,
            used_lesson_ids=result.used_lesson_ids,
        ),
        write_receipts,
    )


def is_analysis_material_write_receipt(receipt: dict[str, object]) -> bool:
    """Return whether a receipt represents an analysis-owned material write."""

    return _require_receipt_text(receipt, "operation") in ANALYSIS_MATERIAL_WRITE_OPERATIONS


def serialize_analysis_write_receipt_summary(
    receipt: dict[str, object],
) -> dict[str, str]:
    """Return the deterministic audit summary for one analysis write receipt."""

    content_md = _require_receipt_text(receipt, "content_md")
    payload = {
        "operation": _require_receipt_text(receipt, "operation"),
        "content_sha256": sha256(content_md.encode("utf-8")).hexdigest(),
        "content_preview": _content_preview(content_md),
    }
    for field_name in ("page_path", "section_name", "scope_key"):
        value = _optional_receipt_text(receipt, field_name)
        if value is not None:
            payload[field_name] = value
    return payload


def _receipt_page_path(receipt: dict[str, object]) -> str:
    page_path = receipt.get("page_path")
    if isinstance(page_path, str) and page_path.strip():
        return page_path.strip().replace("\\", "/")
    return _require_receipt_text(receipt, "written_path").replace("\\", "/")


def _optional_receipt_text(
    receipt: dict[str, object],
    field_name: str,
) -> str | None:
    value = receipt.get(field_name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _require_receipt_text(receipt: dict[str, object], field_name: str) -> str:
    value = receipt.get(field_name)
    if not isinstance(value, str):
        raise AnalysisWriteReceiptContractError(
            f"Analysis write receipt field {field_name!r} must be a string."
        )
    normalized = value.strip()
    if not normalized:
        raise AnalysisWriteReceiptContractError(
            f"Analysis write receipt field {field_name!r} must not be blank."
        )
    return normalized


def _content_preview(content_md: str, *, max_length: int = 240) -> str:
    collapsed = " ".join(content_md.split())
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max_length - 3].rstrip() + "..."


__all__ = [
    "ANALYSIS_MATERIAL_WRITE_OPERATIONS",
    "ANALYSIS_READ_RECEIPT_OPERATIONS",
    "AdmittedAnalysisWriteReceipt",
    "AnalysisMaterialWriteOperation",
    "AnalysisWriteReceiptContractError",
    "analysis_revision_plan_contract_failure",
    "admit_analysis_write_receipt",
    "collapse_analysis_write_receipts",
    "finalize_analysis_write_outcome",
    "is_analysis_material_write_receipt",
    "protected_rewrite_contract_repair_required",
    "protected_rewrite_contract_failure",
    "protected_rewrite_sections_by_page",
    "serialize_analysis_write_receipt_summary",
    "validate_analysis_commit_receipts",
    "validate_analysis_watchlist_maintenance",
]
