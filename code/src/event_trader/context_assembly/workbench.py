"""Analysis Workbench contracts compiled into analysis ContextPackets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, cast

from event_trader.ceau.contracts import UnitFormationLane
from event_trader.contracts import EvidenceLedgerRecord, PageReadResult
from event_trader.contracts.view_state_change import ViewState
from event_trader.contracts.evidence_review import (
    EvidenceReviewDimensions,
    classify_evidence_review_dimensions,
)
from event_trader.feeds.historical_web_search_guard import (
    HistoricalWebSearchFutureLeakage,
    detect_historical_web_search_future_leakage,
)
from event_trader.integrations.markdown_context import (
    MarkdownContextError,
    find_markdown_section,
)
from event_trader.market.contracts import MarketContextSnapshot
from event_trader.operator_context import (
    OperatorContextCard,
    OperatorContextContractError,
    parse_operator_context_page,
)
from event_trader.source_policy import (
    SourcePolicyError,
    extract_event_type,
    extract_operator_source_metadata,
    extract_source_kind,
)
from event_trader.thesis_revision.canonical_bundle import (
    workbench_canonical_thesis_bundle_sections,
)

ACTIVE_EVIDENCE_EXCERPT_CHARS = 1200
MAX_EVIDENCE_LANE_CHARS = 5000
MEMORY_SECTION_EXCERPT_CHARS = 700
MAX_MEMORY_IMPACT_LANE_CHARS = 7000
OPERATOR_CONTEXT_CARD_EXCERPT_CHARS = 700
MAX_OPERATOR_CONTEXT_LANE_CHARS = 6000
MAX_MARKET_LANE_CHARS = 3000
MAX_METHOD_LANE_CHARS = 5000
WORKBENCH_COMPILER_POLICY_VERSION = "analysis_workbench_slice_d_v1"

type WorkbenchGroundingStatus = Literal[
    "compiled_excerpt_available",
    "compiled_excerpt_truncated",
    "compiled_excerpt_unavailable",
]
type EvidenceGroundingStatus = Literal["compiled", "tool_read", "missing"]
type MemoryGroundingStatus = Literal["compiled", "tool_read", "missing"]
type MarketGroundingStatus = Literal["compiled", "tool_read", "not_required", "missing"]
type MemoryCardStatus = Literal["available", "missing", "empty"]
type OperatorContextPageStatus = Literal["available", "missing", "empty"]
type WorkbenchOperatorContextCardStatus = Literal["active"]
type RecapReconciliationStatus = Literal[
    "directionally_consistent",
    "not_directionally_consistent",
    "not_enough_data",
    "stale",
    "unavailable",
    "not_required",
]
type MarketDataFreshness = Literal["fresh", "stale", "unavailable", "not_required"]

_MARKET_DEEP_TOOLS: tuple[str, ...] = (
    "read_market_overview",
    "read_price_volume_window",
    "read_technical_panel",
    "read_option_activity",
    "read_cross_asset_context",
    "read_cross_asset_window",
)
_FORBIDDEN_MARKET_PAYLOAD_TERMS = (
    "priced_in",
    "should_no_update",
    "default_cognition_policy",
    "market_confirms_source_move",
    "move_likely_already_occurred",
)
class AnalysisWorkbenchError(ValueError):
    """Raised when an AnalysisWorkbench payload is malformed."""


@dataclass(frozen=True, slots=True)
class GroundingCoverageReceipt:
    evidence_grounding: EvidenceGroundingStatus
    memory_grounding: MemoryGroundingStatus
    market_grounding: MarketGroundingStatus
    compiled_evidence_event_ids: tuple[str, ...] = ()
    tool_read_evidence_event_ids: tuple[str, ...] = ()
    compiled_memory_cards: tuple[str, ...] = ()
    compiler_memory_read_receipt_ids: tuple[str, ...] = ()
    tool_read_memory_refs: tuple[str, ...] = ()
    compiled_market_grounding_count: int = 0
    tool_read_market_refs: tuple[str, ...] = ()
    failed_reasons: tuple[str, ...] = ()

    @property
    def status(self) -> Literal["passed", "failed"]:
        return "failed" if self.failed_reasons else "passed"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "compiled_evidence_event_ids",
            tuple(self.compiled_evidence_event_ids),
        )
        object.__setattr__(
            self,
            "tool_read_evidence_event_ids",
            tuple(self.tool_read_evidence_event_ids),
        )
        object.__setattr__(self, "compiled_memory_cards", tuple(self.compiled_memory_cards))
        object.__setattr__(
            self,
            "compiler_memory_read_receipt_ids",
            tuple(self.compiler_memory_read_receipt_ids),
        )
        object.__setattr__(
            self,
            "tool_read_memory_refs",
            tuple(self.tool_read_memory_refs),
        )
        object.__setattr__(
            self,
            "tool_read_market_refs",
            tuple(self.tool_read_market_refs),
        )
        object.__setattr__(self, "failed_reasons", tuple(self.failed_reasons))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "evidence_grounding": self.evidence_grounding,
            "memory_grounding": self.memory_grounding,
            "market_grounding": self.market_grounding,
            "compiled_evidence_event_ids": list(self.compiled_evidence_event_ids),
            "tool_read_evidence_event_ids": list(self.tool_read_evidence_event_ids),
            "compiled_memory_cards": list(self.compiled_memory_cards),
            "compiler_memory_read_receipt_ids": list(
                self.compiler_memory_read_receipt_ids
            ),
            "tool_read_memory_refs": list(self.tool_read_memory_refs),
            "compiled_market_grounding_count": self.compiled_market_grounding_count,
            "tool_read_market_refs": list(self.tool_read_market_refs),
            "failed_reasons": list(self.failed_reasons),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> GroundingCoverageReceipt:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("grounding coverage receipt must be an object.")
        return cls(
            evidence_grounding=_evidence_grounding_status(
                payload.get("evidence_grounding")
            ),
            memory_grounding=_memory_grounding_status(payload.get("memory_grounding")),
            market_grounding=_market_grounding_status(payload.get("market_grounding")),
            compiled_evidence_event_ids=_string_tuple(
                payload.get("compiled_evidence_event_ids"),
                "compiled_evidence_event_ids",
            ),
            tool_read_evidence_event_ids=_string_tuple(
                payload.get("tool_read_evidence_event_ids"),
                "tool_read_evidence_event_ids",
            ),
            compiled_memory_cards=_string_tuple(
                payload.get("compiled_memory_cards"),
                "compiled_memory_cards",
            ),
            compiler_memory_read_receipt_ids=_string_tuple(
                payload.get("compiler_memory_read_receipt_ids"),
                "compiler_memory_read_receipt_ids",
            ),
            tool_read_memory_refs=_string_tuple(
                payload.get("tool_read_memory_refs"),
                "tool_read_memory_refs",
            ),
            compiled_market_grounding_count=_optional_non_negative_int(
                payload,
                "compiled_market_grounding_count",
            ),
            tool_read_market_refs=_string_tuple(
                payload.get("tool_read_market_refs"),
                "tool_read_market_refs",
            ),
            failed_reasons=_string_tuple(payload.get("failed_reasons"), "failed_reasons"),
        )


@dataclass(frozen=True, slots=True)
class WorkbenchSourceExcerpt:
    text: str
    sha256: str
    content_sha256: str
    char_count: int
    truncated: bool
    full_text_available_via: str

    def to_json_payload(self) -> dict[str, object]:
        return {
            "text": self.text,
            "sha256": self.sha256,
            "content_sha256": self.content_sha256,
            "char_count": self.char_count,
            "truncated": self.truncated,
            "full_text_available_via": self.full_text_available_via,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> WorkbenchSourceExcerpt:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("source_excerpt must be an object.")
        return cls(
            text=_require_raw_string(payload, "text"),
            sha256=_require_string(payload, "sha256"),
            content_sha256=_require_string(payload, "content_sha256"),
            char_count=_require_non_negative_int(payload, "char_count"),
            truncated=payload.get("truncated") is True,
            full_text_available_via=_require_string(payload, "full_text_available_via"),
        )


@dataclass(frozen=True, slots=True)
class OperatorSourceMetadata:
    operator_confidence: str
    operator_source_basis_value: str

    def to_json_payload(self) -> dict[str, object]:
        return {
            "operator_confidence": self.operator_confidence,
            "operator_source_basis": self.operator_source_basis_value,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> OperatorSourceMetadata:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("operator_source_metadata must be an object.")
        return cls(
            operator_confidence=_require_string(payload, "operator_confidence"),
            operator_source_basis_value=_require_string(payload, "operator_source_basis"),
        )


@dataclass(frozen=True, slots=True)
class WorkbenchEvidenceCard:
    event_id: str
    source_ref: str
    title: str
    event_type: str
    source_kind: str
    labels: tuple[str, ...]
    evidence_review_dimensions: EvidenceReviewDimensions
    operator_source_metadata: OperatorSourceMetadata | None
    source_excerpt: WorkbenchSourceExcerpt | None
    grounding_status: WorkbenchGroundingStatus
    requires_deep_read_for_full_quote_or_conflict: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "labels", tuple(self.labels))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "source_ref": self.source_ref,
            "title": self.title,
            "event_type": self.event_type,
            "source_kind": self.source_kind,
            "labels": list(self.labels),
            "evidence_review_dimensions": self.evidence_review_dimensions.to_dict(),
            "operator_source_metadata": (
                None
                if self.operator_source_metadata is None
                else self.operator_source_metadata.to_json_payload()
            ),
            "source_excerpt": (
                None
                if self.source_excerpt is None
                else self.source_excerpt.to_json_payload()
            ),
            "grounding_status": self.grounding_status,
            "requires_deep_read_for_full_quote_or_conflict": (
                self.requires_deep_read_for_full_quote_or_conflict
            ),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> WorkbenchEvidenceCard:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("evidence card must be an object.")
        labels = payload.get("labels")
        if not isinstance(labels, list):
            raise AnalysisWorkbenchError("evidence card labels must be a list.")
        excerpt_payload = payload.get("source_excerpt")
        operator_metadata_payload = payload.get("operator_source_metadata")
        return cls(
            event_id=_require_string(payload, "event_id"),
            source_ref=_require_string(payload, "source_ref"),
            title=_require_string(payload, "title"),
            event_type=_require_string(payload, "event_type"),
            source_kind=_require_string(payload, "source_kind"),
            labels=tuple(str(label) for label in labels),
            evidence_review_dimensions=_dimensions_from_payload(
                payload.get("evidence_review_dimensions")
            ),
            operator_source_metadata=(
                None
                if operator_metadata_payload is None
                else OperatorSourceMetadata.from_json_payload(operator_metadata_payload)
            ),
            source_excerpt=(
                None
                if excerpt_payload is None
                else WorkbenchSourceExcerpt.from_json_payload(excerpt_payload)
            ),
            grounding_status=_grounding_status(payload.get("grounding_status")),
            requires_deep_read_for_full_quote_or_conflict=(
                payload.get("requires_deep_read_for_full_quote_or_conflict") is True
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkbenchOmissionReceipt:
    lane: str
    budget_chars: int
    used_chars: int
    omitted_ref: str
    reason: str
    available_via: str
    details: dict[str, object] | None = None

    def to_json_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "lane": self.lane,
            "budget_chars": self.budget_chars,
            "used_chars": self.used_chars,
            "omitted_ref": self.omitted_ref,
            "reason": self.reason,
            "available_via": self.available_via,
        }
        if self.details is not None:
            payload["details"] = dict(self.details)
        return payload

    @classmethod
    def from_json_payload(cls, payload: object) -> WorkbenchOmissionReceipt:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("omission receipt must be an object.")
        return cls(
            lane=_require_string(payload, "lane"),
            budget_chars=_require_non_negative_int(payload, "budget_chars"),
            used_chars=_require_non_negative_int(payload, "used_chars"),
            omitted_ref=_require_string(payload, "omitted_ref"),
            reason=_require_string(payload, "reason"),
            available_via=_require_string(payload, "available_via"),
            details=_optional_object_dict(payload.get("details"), "details"),
        )


@dataclass(frozen=True, slots=True)
class WorkbenchCompilerReadReceipt:
    receipt_id: str
    context_packet_id: str
    stage: Literal["analysis"]
    target_key: str
    business_at: datetime
    runtime_scope: Literal["live", "replay"]
    run_id: str
    surface_type: Literal["research_memory_section"]
    page_path: str
    section_name: str
    content_sha256: str
    excerpt_sha256: str
    excerpt_char_count: int
    excerpt_truncated: bool
    available_via: str
    read_policy: str
    read_run_id: str
    compiler_policy_version: str = WORKBENCH_COMPILER_POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_at", _normalize_datetime(self.business_at))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "receipt_type": "workbench_compiler_memory_read",
            "receipt_id": self.receipt_id,
            "context_packet_id": self.context_packet_id,
            "stage": self.stage,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "runtime_scope": self.runtime_scope,
            "run_id": self.run_id,
            "surface_type": self.surface_type,
            "page_path": self.page_path,
            "section_name": self.section_name,
            "content_sha256": self.content_sha256,
            "excerpt_sha256": self.excerpt_sha256,
            "excerpt_char_count": self.excerpt_char_count,
            "excerpt_truncated": self.excerpt_truncated,
            "available_via": self.available_via,
            "read_policy": self.read_policy,
            "read_run_id": self.read_run_id,
            "compiler_policy_version": self.compiler_policy_version,
        }


@dataclass(frozen=True, slots=True)
class WorkbenchOperatorContextCard:
    card_id: str
    section_name: str
    status: WorkbenchOperatorContextCardStatus
    excerpt: str
    content_sha256: str
    excerpt_sha256: str
    char_count: int
    excerpt_truncated: bool
    full_section_available_via: str
    compiler_read_receipt_id: str

    def __post_init__(self) -> None:
        if self.status != "active":
            raise AnalysisWorkbenchError("operator context card status is invalid.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "section_name": self.section_name,
            "status": self.status,
            "excerpt": self.excerpt,
            "content_sha256": self.content_sha256,
            "excerpt_sha256": self.excerpt_sha256,
            "char_count": self.char_count,
            "excerpt_truncated": self.excerpt_truncated,
            "full_section_available_via": self.full_section_available_via,
            "compiler_read_receipt_id": self.compiler_read_receipt_id,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> WorkbenchOperatorContextCard:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("operator context card must be an object.")
        return cls(
            card_id=_require_string(payload, "card_id"),
            section_name=_require_string(payload, "section_name"),
            status=_workbench_operator_context_card_status(payload.get("status")),
            excerpt=_require_raw_string(payload, "excerpt"),
            content_sha256=_require_string(payload, "content_sha256"),
            excerpt_sha256=_require_string(payload, "excerpt_sha256"),
            char_count=_require_non_negative_int(payload, "char_count"),
            excerpt_truncated=payload.get("excerpt_truncated") is True,
            full_section_available_via=_require_string(
                payload,
                "full_section_available_via",
            ),
            compiler_read_receipt_id=_require_string(
                payload,
                "compiler_read_receipt_id",
            ),
        )


@dataclass(frozen=True, slots=True)
class OperatorContextCompilerReadReceipt:
    receipt_id: str
    context_packet_id: str
    stage: Literal["analysis"]
    target_key: str
    business_at: datetime
    runtime_scope: Literal["live", "replay"]
    run_id: str
    page_path: str
    section_name: str
    content_sha256: str
    excerpt_sha256: str
    excerpt_char_count: int
    excerpt_truncated: bool
    read_policy: str
    read_run_id: str
    compiler_policy_version: str = WORKBENCH_COMPILER_POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_at", _normalize_datetime(self.business_at))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "receipt_type": "operator_context_compiler_read",
            "receipt_id": self.receipt_id,
            "context_packet_id": self.context_packet_id,
            "stage": self.stage,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "runtime_scope": self.runtime_scope,
            "run_id": self.run_id,
            "page_path": self.page_path,
            "section_name": self.section_name,
            "content_sha256": self.content_sha256,
            "excerpt_sha256": self.excerpt_sha256,
            "excerpt_char_count": self.excerpt_char_count,
            "excerpt_truncated": self.excerpt_truncated,
            "read_policy": self.read_policy,
            "read_run_id": self.read_run_id,
            "compiler_policy_version": self.compiler_policy_version,
        }


@dataclass(frozen=True, slots=True)
class WorkbenchMemoryCard:
    card_id: str
    page_path: str
    section_name: str
    status: MemoryCardStatus
    excerpt: str
    content_hash: str
    excerpt_hash: str
    char_count: int
    truncated: bool
    full_section_available_via: str
    compiler_read_receipt_id: str

    def to_json_payload(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "page_path": self.page_path,
            "section_name": self.section_name,
            "status": self.status,
            "excerpt": self.excerpt,
            "content_hash": self.content_hash,
            "excerpt_hash": self.excerpt_hash,
            "char_count": self.char_count,
            "truncated": self.truncated,
            "full_section_available_via": self.full_section_available_via,
            "compiler_read_receipt_id": self.compiler_read_receipt_id,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> WorkbenchMemoryCard:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("memory card must be an object.")
        return cls(
            card_id=_require_string(payload, "card_id"),
            page_path=_require_string(payload, "page_path"),
            section_name=_require_string(payload, "section_name"),
            status=_memory_card_status(payload.get("status")),
            excerpt=_optional_raw_string(payload, "excerpt"),
            content_hash=_optional_string(payload, "content_hash"),
            excerpt_hash=_optional_string(payload, "excerpt_hash"),
            char_count=_require_non_negative_int(payload, "char_count"),
            truncated=payload.get("truncated") is True,
            full_section_available_via=_require_string(
                payload,
                "full_section_available_via",
            ),
            compiler_read_receipt_id=_optional_string(
                payload,
                "compiler_read_receipt_id",
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceLane:
    active_records: tuple[WorkbenchEvidenceCard, ...]
    budget_chars: int
    used_chars: int
    omission_receipts: tuple[WorkbenchOmissionReceipt, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "active_records", tuple(self.active_records))
        object.__setattr__(self, "omission_receipts", tuple(self.omission_receipts))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "active_records": [card.to_json_payload() for card in self.active_records],
            "budget_chars": self.budget_chars,
            "used_chars": self.used_chars,
            "omission_receipts": [
                receipt.to_json_payload() for receipt in self.omission_receipts
            ],
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> EvidenceLane:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("evidence_lane must be an object.")
        return cls(
            active_records=tuple(
                WorkbenchEvidenceCard.from_json_payload(item)
                for item in _require_list(payload, "active_records")
            ),
            budget_chars=_require_non_negative_int(payload, "budget_chars"),
            used_chars=_require_non_negative_int(payload, "used_chars"),
            omission_receipts=tuple(
                WorkbenchOmissionReceipt.from_json_payload(item)
                for item in _optional_list(payload, "omission_receipts")
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryImpactLane:
    cards: tuple[WorkbenchMemoryCard, ...]
    claim_card_ids: tuple[str, ...]
    budget_chars: int
    used_chars: int
    omission_receipts: tuple[WorkbenchOmissionReceipt, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "cards", tuple(self.cards))
        object.__setattr__(self, "claim_card_ids", tuple(self.claim_card_ids))
        object.__setattr__(self, "omission_receipts", tuple(self.omission_receipts))

    @property
    def available_cards(self) -> tuple[WorkbenchMemoryCard, ...]:
        return tuple(card for card in self.cards if card.status == "available")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "cards": [card.to_json_payload() for card in self.cards],
            "claim_card_ids": list(self.claim_card_ids),
            "budget_chars": self.budget_chars,
            "used_chars": self.used_chars,
            "omission_receipts": [
                receipt.to_json_payload() for receipt in self.omission_receipts
            ],
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> MemoryImpactLane:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("memory_impact_lane must be an object.")
        return cls(
            cards=tuple(
                WorkbenchMemoryCard.from_json_payload(item)
                for item in _require_list(payload, "cards")
            ),
            claim_card_ids=_string_tuple(
                _optional_list(payload, "claim_card_ids"),
                "claim_card_ids",
            ),
            budget_chars=_require_non_negative_int(payload, "budget_chars"),
            used_chars=_require_non_negative_int(payload, "used_chars"),
            omission_receipts=tuple(
                WorkbenchOmissionReceipt.from_json_payload(item)
                for item in _optional_list(payload, "omission_receipts")
            ),
        )


@dataclass(frozen=True, slots=True)
class OperatorContextLane:
    page_path: str
    page_status: OperatorContextPageStatus
    cards: tuple[WorkbenchOperatorContextCard, ...]
    active_card_ids: tuple[str, ...]
    budget_chars: int
    used_chars: int
    omission_receipts: tuple[WorkbenchOmissionReceipt, ...] = ()

    def __post_init__(self) -> None:
        if self.page_status not in {"available", "missing", "empty"}:
            raise AnalysisWorkbenchError("operator context page status is invalid.")
        object.__setattr__(self, "cards", tuple(self.cards))
        object.__setattr__(self, "active_card_ids", tuple(self.active_card_ids))
        object.__setattr__(self, "omission_receipts", tuple(self.omission_receipts))

    @property
    def active_cards(self) -> tuple[WorkbenchOperatorContextCard, ...]:
        return tuple(card for card in self.cards if card.status == "active")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "page_path": self.page_path,
            "page_status": self.page_status,
            "cards": [card.to_json_payload() for card in self.cards],
            "active_card_ids": list(self.active_card_ids),
            "budget_chars": self.budget_chars,
            "used_chars": self.used_chars,
            "omission_receipts": [
                receipt.to_json_payload() for receipt in self.omission_receipts
            ],
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> OperatorContextLane:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("operator_context_lane must be an object.")
        return cls(
            page_path=_require_string(payload, "page_path"),
            page_status=_operator_context_page_status(payload.get("page_status")),
            cards=tuple(
                WorkbenchOperatorContextCard.from_json_payload(item)
                for item in _require_list(payload, "cards")
            ),
            active_card_ids=_string_tuple(
                payload.get("active_card_ids"),
                "active_card_ids",
            ),
            budget_chars=_require_non_negative_int(payload, "budget_chars"),
            used_chars=_require_non_negative_int(payload, "used_chars"),
            omission_receipts=tuple(
                WorkbenchOmissionReceipt.from_json_payload(item)
                for item in _optional_list(payload, "omission_receipts")
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketRecapReconciliation:
    required: bool
    reason: str
    status: RecapReconciliationStatus
    data_freshness: MarketDataFreshness
    latest_visible_bar_start_at: str | None
    latest_visible_bar_end_at: str | None
    returns: dict[str, object]
    volume_context: dict[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "returns", dict(self.returns))
        object.__setattr__(self, "volume_context", dict(self.volume_context))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "required": self.required,
            "reason": self.reason,
            "status": self.status,
            "data_freshness": self.data_freshness,
            "latest_visible_bar_start_at": self.latest_visible_bar_start_at,
            "latest_visible_bar_end_at": self.latest_visible_bar_end_at,
            "returns": dict(self.returns),
            "volume_context": dict(self.volume_context),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> MarketRecapReconciliation:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("recap_reconciliation must be an object.")
        returns = payload.get("returns")
        if not isinstance(returns, dict):
            raise AnalysisWorkbenchError("recap_reconciliation returns must be an object.")
        volume_context = payload.get("volume_context")
        if not isinstance(volume_context, dict):
            raise AnalysisWorkbenchError(
                "recap_reconciliation volume_context must be an object."
            )
        return cls(
            required=payload.get("required") is True,
            reason=_require_string(payload, "reason"),
            status=_recap_reconciliation_status(payload.get("status")),
            data_freshness=_market_data_freshness(payload.get("data_freshness")),
            latest_visible_bar_start_at=_optional_text_value(
                payload.get("latest_visible_bar_start_at")
            ),
            latest_visible_bar_end_at=_optional_text_value(
                payload.get("latest_visible_bar_end_at")
            ),
            returns=dict(returns),
            volume_context=dict(volume_context),
        )


@dataclass(frozen=True, slots=True)
class MarketLane:
    available: bool
    target_key: str
    business_at: datetime
    tradable_proxy_symbol: str
    bar_granularity: str
    component_status: dict[str, str]
    recap_reconciliation: MarketRecapReconciliation
    deep_market_tools_available: tuple[str, ...]
    price_volume_facts: dict[str, object]
    budget_chars: int
    used_chars: int
    omission_receipts: tuple[WorkbenchOmissionReceipt, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_at", _normalize_datetime(self.business_at))
        object.__setattr__(
            self,
            "component_status",
            {str(key): str(value) for key, value in sorted(self.component_status.items())},
        )
        object.__setattr__(
            self,
            "deep_market_tools_available",
            tuple(self.deep_market_tools_available),
        )
        object.__setattr__(self, "price_volume_facts", dict(self.price_volume_facts))
        object.__setattr__(self, "omission_receipts", tuple(self.omission_receipts))
        _validate_market_payload_is_low_commitment(self.to_json_payload())

    def to_json_payload(self) -> dict[str, object]:
        return {
            "available": self.available,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "tradable_proxy_symbol": self.tradable_proxy_symbol,
            "bar_granularity": self.bar_granularity,
            "component_status": dict(self.component_status),
            "recap_reconciliation": self.recap_reconciliation.to_json_payload(),
            "deep_market_tools_available": list(self.deep_market_tools_available),
            "price_volume_facts": dict(self.price_volume_facts),
            "budget_chars": self.budget_chars,
            "used_chars": self.used_chars,
            "omission_receipts": [
                receipt.to_json_payload() for receipt in self.omission_receipts
            ],
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> MarketLane:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("market_lane must be an object.")
        component_status = payload.get("component_status")
        if not isinstance(component_status, dict):
            raise AnalysisWorkbenchError("market_lane component_status must be an object.")
        price_volume_facts = payload.get("price_volume_facts")
        if not isinstance(price_volume_facts, dict):
            raise AnalysisWorkbenchError(
                "market_lane price_volume_facts must be an object."
            )
        return cls(
            available=payload.get("available") is True,
            target_key=_require_string(payload, "target_key"),
            business_at=_parse_datetime(payload.get("business_at"), "business_at"),
            tradable_proxy_symbol=_optional_string(
                payload,
                "tradable_proxy_symbol",
            ),
            bar_granularity=_optional_string(payload, "bar_granularity"),
            component_status={
                str(key): str(value) for key, value in component_status.items()
            },
            recap_reconciliation=MarketRecapReconciliation.from_json_payload(
                payload.get("recap_reconciliation")
            ),
            deep_market_tools_available=_string_tuple(
                payload.get("deep_market_tools_available"),
                "deep_market_tools_available",
            ),
            price_volume_facts=dict(price_volume_facts),
            budget_chars=_require_non_negative_int(payload, "budget_chars"),
            used_chars=_require_non_negative_int(payload, "used_chars"),
            omission_receipts=tuple(
                WorkbenchOmissionReceipt.from_json_payload(item)
                for item in _optional_list(payload, "omission_receipts")
            ),
        )


@dataclass(frozen=True, slots=True)
class MethodLane:
    budget_chars: int
    used_chars: int
    omission_receipts: tuple[WorkbenchOmissionReceipt, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "omission_receipts", tuple(self.omission_receipts))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "budget_chars": self.budget_chars,
            "used_chars": self.used_chars,
            "omission_receipts": [
                receipt.to_json_payload() for receipt in self.omission_receipts
            ],
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> MethodLane:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("method_lane must be an object.")
        return cls(
            budget_chars=_require_non_negative_int(payload, "budget_chars"),
            used_chars=_require_non_negative_int(payload, "used_chars"),
            omission_receipts=tuple(
                WorkbenchOmissionReceipt.from_json_payload(item)
                for item in _optional_list(payload, "omission_receipts")
            ),
        )


@dataclass(frozen=True, slots=True)
class MarketSetupLane:
    target_key: str
    section_name: str = "Market Setup Dashboard"
    exposure_blind: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_key", _require_non_blank_target_key(self.target_key))
        if self.section_name != "Market Setup Dashboard":
            raise AnalysisWorkbenchError(
                "market_setup_lane.section_name must be Market Setup Dashboard."
            )
        if self.exposure_blind is not True:
            raise AnalysisWorkbenchError("market_setup_lane.exposure_blind must be true.")

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "section_name": self.section_name,
            "exposure_blind": self.exposure_blind,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> MarketSetupLane:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("market_setup_lane must be an object.")
        return cls(
            target_key=_require_string(payload, "target_key"),
            section_name=_require_string(payload, "section_name"),
            exposure_blind=_require_bool(payload.get("exposure_blind"), "exposure_blind"),
        )


@dataclass(frozen=True, slots=True)
class MarketPathLane:
    available: bool
    target_key: str
    business_at: datetime
    path_payload: dict[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_key", _require_non_blank_target_key(self.target_key))
        object.__setattr__(self, "business_at", _normalize_datetime(self.business_at))
        if not isinstance(self.available, bool):
            raise AnalysisWorkbenchError("market_path_lane.available must be a boolean.")
        if not isinstance(self.path_payload, dict):
            raise AnalysisWorkbenchError("market_path_lane.path_payload must be an object.")
        object.__setattr__(self, "path_payload", dict(self.path_payload))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "available": self.available,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "path_payload": dict(self.path_payload),
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> MarketPathLane:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("market_path_lane must be an object.")
        return cls(
            available=payload.get("available") is True,
            target_key=_require_string(payload, "target_key"),
            business_at=_parse_datetime(payload.get("business_at"), "business_at"),
            path_payload=_require_json_object(
                payload.get("path_payload"),
                "market_path_lane.path_payload",
            ),
        )


@dataclass(frozen=True, slots=True)
class ExposurePathLane:
    target_key: str
    current_state: ViewState
    target_weight: float
    state_effective_at: str | None
    proxy_entry_price: float | None
    latest_price: float | None
    move_since_state_change_points: float | None
    move_since_state_change_pct: float | None
    mfe_points: float | None
    mae_points: float | None
    exposure_flags: tuple[str, ...]
    data_status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_key", _require_non_blank_target_key(self.target_key))
        object.__setattr__(
            self,
            "current_state",
            _require_view_state(self.current_state, "exposure_path_lane.current_state"),
        )
        for field_name in (
            "target_weight",
            "proxy_entry_price",
            "latest_price",
            "move_since_state_change_points",
            "move_since_state_change_pct",
            "mfe_points",
            "mae_points",
        ):
            _validate_optional_float(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "state_effective_at",
            _optional_text_value(self.state_effective_at),
        )
        object.__setattr__(
            self,
            "exposure_flags",
            _validate_string_tuple(self.exposure_flags, "exposure_flags"),
        )
        object.__setattr__(
            self,
            "data_status",
            _require_non_blank_target_key(self.data_status),
        )

    def to_json_payload(self) -> dict[str, object]:
        return {
            "target_key": self.target_key,
            "current_state": self.current_state,
            "target_weight": self.target_weight,
            "state_effective_at": self.state_effective_at,
            "proxy_entry_price": self.proxy_entry_price,
            "latest_price": self.latest_price,
            "move_since_state_change_points": self.move_since_state_change_points,
            "move_since_state_change_pct": self.move_since_state_change_pct,
            "mfe_points": self.mfe_points,
            "mae_points": self.mae_points,
            "exposure_flags": list(self.exposure_flags),
            "data_status": self.data_status,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> ExposurePathLane:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("exposure_path_lane must be an object.")
        return cls(
            target_key=_require_string(payload, "target_key"),
            current_state=_require_view_state(
                payload.get("current_state"),
                "exposure_path_lane.current_state",
            ),
            target_weight=_require_float(payload.get("target_weight"), "target_weight"),
            state_effective_at=_optional_text_value(payload.get("state_effective_at")),
            proxy_entry_price=_optional_float(payload.get("proxy_entry_price")),
            latest_price=_optional_float(payload.get("latest_price")),
            move_since_state_change_points=_optional_float(
                payload.get("move_since_state_change_points")
            ),
            move_since_state_change_pct=_optional_float(
                payload.get("move_since_state_change_pct")
            ),
            mfe_points=_optional_float(payload.get("mfe_points")),
            mae_points=_optional_float(payload.get("mae_points")),
            exposure_flags=_string_tuple(
                payload.get("exposure_flags"),
                "exposure_flags",
            ),
            data_status=_require_string(payload, "data_status"),
        )


@dataclass(frozen=True, slots=True)
class AnalysisWorkbench:
    target_key: str
    business_at: datetime
    context_packet_id: str
    evidence_lane: EvidenceLane
    memory_impact_lane: MemoryImpactLane
    operator_context_lane: OperatorContextLane
    market_lane: MarketLane
    market_path_lane: MarketPathLane
    exposure_path_lane: ExposurePathLane
    method_lane: MethodLane
    market_setup_lane: MarketSetupLane
    unit_formation_lane: UnitFormationLane | None = None
    compiler_policy_version: str = WORKBENCH_COMPILER_POLICY_VERSION
    analysis_workbench_payload_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_at", _normalize_datetime(self.business_at))
        if not self.analysis_workbench_payload_hash:
            object.__setattr__(
                self,
                "analysis_workbench_payload_hash",
                hash_analysis_workbench_payload(self),
            )

    def to_json_payload(self, *, include_payload_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "context_packet_id": self.context_packet_id,
            "evidence_lane": self.evidence_lane.to_json_payload(),
            "memory_impact_lane": self.memory_impact_lane.to_json_payload(),
            "operator_context_lane": self.operator_context_lane.to_json_payload(),
            "market_lane": self.market_lane.to_json_payload(),
            "market_path_lane": self.market_path_lane.to_json_payload(),
            "exposure_path_lane": self.exposure_path_lane.to_json_payload(),
            "method_lane": self.method_lane.to_json_payload(),
            "market_setup_lane": self.market_setup_lane.to_json_payload(),
            "unit_formation_lane": (
                None
                if self.unit_formation_lane is None
                else self.unit_formation_lane.to_json_payload()
            ),
            "compiler_policy_version": self.compiler_policy_version,
        }
        if include_payload_hash:
            payload["analysis_workbench_payload_hash"] = (
                self.analysis_workbench_payload_hash
            )
        return payload

    @classmethod
    def from_json_payload(cls, payload: object) -> AnalysisWorkbench:
        if not isinstance(payload, dict):
            raise AnalysisWorkbenchError("analysis_workbench must be an object.")
        return cls(
            target_key=_require_string(payload, "target_key"),
            business_at=_parse_datetime(payload.get("business_at"), "business_at"),
            context_packet_id=_require_string(payload, "context_packet_id"),
            evidence_lane=EvidenceLane.from_json_payload(payload.get("evidence_lane")),
            memory_impact_lane=MemoryImpactLane.from_json_payload(
                payload.get("memory_impact_lane")
            ),
            operator_context_lane=OperatorContextLane.from_json_payload(
                payload.get("operator_context_lane")
            ),
            market_lane=MarketLane.from_json_payload(payload.get("market_lane")),
            market_path_lane=(
                _fallback_market_path_lane(payload)
                if payload.get("market_path_lane") is None
                else MarketPathLane.from_json_payload(payload.get("market_path_lane"))
            ),
            exposure_path_lane=(
                _fallback_exposure_path_lane(payload)
                if payload.get("exposure_path_lane") is None
                else ExposurePathLane.from_json_payload(
                    payload.get("exposure_path_lane")
                )
            ),
            method_lane=MethodLane.from_json_payload(payload.get("method_lane")),
            market_setup_lane=MarketSetupLane.from_json_payload(
                payload.get("market_setup_lane")
            ),
            unit_formation_lane=(
                None
                if payload.get("unit_formation_lane") is None
                else UnitFormationLane.from_json_payload(
                    payload.get("unit_formation_lane")
                )
            ),
            compiler_policy_version=_require_string(
                payload,
                "compiler_policy_version",
            ),
            analysis_workbench_payload_hash=_optional_string(
                payload,
                "analysis_workbench_payload_hash",
            ),
        )


def build_analysis_workbench(
    *,
    target_key: str,
    business_at: datetime,
    context_packet_id: str,
    evidence_records: tuple[EvidenceLedgerRecord, ...],
    target_pages: tuple[PageReadResult, ...] = (),
    claim_card_ids: tuple[str, ...] = (),
    market_context: MarketContextSnapshot | None = None,
    memory_read_policy: str = "",
    runtime_scope: Literal["live", "replay"] = "live",
    run_id: str = "",
    unit_formation_lane: UnitFormationLane | None = None,
) -> tuple[
    AnalysisWorkbench,
    tuple[WorkbenchCompilerReadReceipt | OperatorContextCompilerReadReceipt, ...],
]:
    business_at = _normalize_datetime(business_at)
    evidence_lane = _build_evidence_lane(evidence_records, business_at=business_at)
    memory_impact_lane, memory_receipts = _build_memory_impact_lane(
        target_key=target_key,
        business_at=business_at,
        context_packet_id=context_packet_id,
        target_pages=target_pages,
        claim_card_ids=claim_card_ids,
        memory_read_policy=memory_read_policy,
        runtime_scope=runtime_scope,
        run_id=run_id,
    )
    operator_context_lane, operator_context_receipts = _build_operator_context_lane(
        target_key=target_key,
        business_at=business_at,
        context_packet_id=context_packet_id,
        target_pages=target_pages,
        memory_read_policy=memory_read_policy,
        runtime_scope=runtime_scope,
        run_id=run_id,
    )
    market_lane = _build_market_lane(
        target_key=target_key,
        business_at=business_at,
        evidence_cards=evidence_lane.active_records,
        market_context=market_context,
    )
    market_setup_lane = _build_market_setup_lane(target_key=target_key)
    market_path_lane = _build_market_path_lane(
        target_key=target_key,
        business_at=business_at,
        market_context=market_context,
    )
    exposure_path_lane = _build_exposure_path_lane(
        target_key=target_key,
        market_path_lane=market_path_lane,
    )
    return AnalysisWorkbench(
        target_key=target_key,
        business_at=business_at,
        context_packet_id=context_packet_id,
        evidence_lane=evidence_lane,
        memory_impact_lane=memory_impact_lane,
        operator_context_lane=operator_context_lane,
        market_lane=market_lane,
        market_path_lane=market_path_lane,
        exposure_path_lane=exposure_path_lane,
        method_lane=_build_method_lane(),
        market_setup_lane=market_setup_lane,
        unit_formation_lane=unit_formation_lane,
    ), (*memory_receipts, *operator_context_receipts)


def hash_analysis_workbench_payload(workbench: AnalysisWorkbench) -> str:
    encoded = json.dumps(
        workbench.to_json_payload(include_payload_hash=False),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _build_evidence_lane(
    records: tuple[EvidenceLedgerRecord, ...],
    *,
    business_at: datetime,
) -> EvidenceLane:
    cards: list[WorkbenchEvidenceCard] = []
    omissions: list[WorkbenchOmissionReceipt] = []
    used_chars = 0
    for record in records:
        remaining = MAX_EVIDENCE_LANE_CHARS - used_chars
        if remaining <= 0:
            omissions.append(
                WorkbenchOmissionReceipt(
                    lane="evidence",
                    budget_chars=MAX_EVIDENCE_LANE_CHARS,
                    used_chars=used_chars,
                    omitted_ref=record.event_id,
                    reason="lane_budget_exceeded",
                    available_via="read_evidence",
                )
            )
            continue
        future_leakage = detect_historical_web_search_future_leakage(
            source_ref=record.source_ref,
            title=record.title,
            content=record.content,
            visible_at=business_at,
        )
        if future_leakage is not None:
            omissions.append(
                _future_observed_evidence_exclusion_receipt(
                    record=record,
                    used_chars=used_chars,
                    future_leakage=future_leakage,
                )
            )
            continue
        card = _evidence_card(
            record,
            excerpt_limit=min(ACTIVE_EVIDENCE_EXCERPT_CHARS, remaining),
        )
        card_chars = _json_char_count(card.to_json_payload())
        if used_chars + card_chars > MAX_EVIDENCE_LANE_CHARS:
            omissions.append(
                WorkbenchOmissionReceipt(
                    lane="evidence",
                    budget_chars=MAX_EVIDENCE_LANE_CHARS,
                    used_chars=used_chars,
                    omitted_ref=record.event_id,
                    reason="lane_budget_exceeded",
                    available_via="read_evidence",
                )
            )
            continue
        cards.append(card)
        used_chars += card_chars
    return EvidenceLane(
        active_records=tuple(cards),
        budget_chars=MAX_EVIDENCE_LANE_CHARS,
        used_chars=used_chars,
        omission_receipts=tuple(omissions),
    )


def _build_method_lane() -> MethodLane:
    return MethodLane(
        budget_chars=MAX_METHOD_LANE_CHARS,
        used_chars=0,
        omission_receipts=(),
    )


def _build_market_setup_lane(
    *,
    target_key: str,
) -> MarketSetupLane:
    return MarketSetupLane(
        target_key=target_key,
    )


def _build_memory_impact_lane(
    *,
    target_key: str,
    business_at: datetime,
    context_packet_id: str,
    target_pages: tuple[PageReadResult, ...],
    claim_card_ids: tuple[str, ...],
    memory_read_policy: str,
    runtime_scope: Literal["live", "replay"],
    run_id: str,
) -> tuple[MemoryImpactLane, tuple[WorkbenchCompilerReadReceipt, ...]]:
    pages_by_path = {page.page_path.replace("\\", "/"): page for page in target_pages}
    cards: list[WorkbenchMemoryCard] = []
    receipts: list[WorkbenchCompilerReadReceipt] = []
    omissions: list[WorkbenchOmissionReceipt] = []
    used_chars = 0
    for bundle_section in workbench_canonical_thesis_bundle_sections(target_key):
        page = pages_by_path.get(bundle_section.page_path)
        candidate_card, candidate_receipt = _memory_card_for_section(
            target_key=target_key,
            business_at=business_at,
            context_packet_id=context_packet_id,
            page=page,
            page_path=bundle_section.page_path,
            section_name=bundle_section.section_name,
            memory_read_policy=memory_read_policy,
            runtime_scope=runtime_scope,
            run_id=run_id,
        )
        card_chars = _json_char_count(candidate_card.to_json_payload())
        if used_chars + card_chars > MAX_MEMORY_IMPACT_LANE_CHARS:
            omissions.append(
                WorkbenchOmissionReceipt(
                    lane="memory_impact",
                    budget_chars=MAX_MEMORY_IMPACT_LANE_CHARS,
                    used_chars=used_chars,
                    omitted_ref=candidate_card.card_id,
                    reason="lane_budget_exceeded",
                    available_via="read_section",
                )
            )
            continue
        cards.append(candidate_card)
        used_chars += card_chars
        if candidate_receipt is not None:
            receipts.append(candidate_receipt)
    return (
        MemoryImpactLane(
            cards=tuple(cards),
            claim_card_ids=tuple(claim_card_ids),
            budget_chars=MAX_MEMORY_IMPACT_LANE_CHARS,
            used_chars=used_chars,
            omission_receipts=tuple(omissions),
        ),
        tuple(receipts),
    )


def _build_operator_context_lane(
    *,
    target_key: str,
    business_at: datetime,
    context_packet_id: str,
    target_pages: tuple[PageReadResult, ...],
    memory_read_policy: str,
    runtime_scope: Literal["live", "replay"],
    run_id: str,
) -> tuple[OperatorContextLane, tuple[OperatorContextCompilerReadReceipt, ...]]:
    page_path = f"targets/{target_key}/operator.md"
    pages_by_path = {page.page_path.replace("\\", "/"): page for page in target_pages}
    page = pages_by_path.get(page_path)
    if page is None:
        return _empty_operator_context_lane(
            page_path=page_path,
            status="missing",
        ), ()
    if not page.content_md.strip():
        return _empty_operator_context_lane(
            page_path=page_path,
            status="empty",
        ), ()

    try:
        source_cards = parse_operator_context_page(
            page.content_md,
            target_key=target_key,
        )
    except OperatorContextContractError as exc:
        raise AnalysisWorkbenchError(f"operator.md is malformed: {exc}") from exc

    included_cards: list[WorkbenchOperatorContextCard] = []
    receipts: list[OperatorContextCompilerReadReceipt] = []
    omissions: list[WorkbenchOmissionReceipt] = []
    used_chars = 0

    for source_card in sorted(source_cards, key=_operator_context_card_sort_key):
        omission_reason = _operator_context_card_omission_reason(
            source_card,
        )
        if omission_reason is not None:
            omissions.append(
                WorkbenchOmissionReceipt(
                    lane="operator_context",
                    budget_chars=MAX_OPERATOR_CONTEXT_LANE_CHARS,
                    used_chars=used_chars,
                    omitted_ref=source_card.card_id,
                    reason=omission_reason,
                    available_via="read_section",
                )
            )
            continue

        candidate_card, candidate_receipt = _operator_context_workbench_card(
            target_key=target_key,
            business_at=business_at,
            context_packet_id=context_packet_id,
            page_path=page_path,
            source_card=source_card,
            memory_read_policy=memory_read_policy,
            runtime_scope=runtime_scope,
            run_id=run_id,
        )
        card_chars = _json_char_count(candidate_card.to_json_payload())
        if used_chars + card_chars > MAX_OPERATOR_CONTEXT_LANE_CHARS:
            omissions.append(
                WorkbenchOmissionReceipt(
                    lane="operator_context",
                    budget_chars=MAX_OPERATOR_CONTEXT_LANE_CHARS,
                    used_chars=used_chars,
                    omitted_ref=source_card.card_id,
                    reason="lane_budget_exceeded",
                    available_via="read_section",
                )
            )
            continue
        included_cards.append(candidate_card)
        receipts.append(candidate_receipt)
        used_chars += card_chars

    return (
        OperatorContextLane(
            page_path=page_path,
            page_status="available",
            cards=tuple(included_cards),
            active_card_ids=tuple(
                card.card_id for card in included_cards if card.status == "active"
            ),
            budget_chars=MAX_OPERATOR_CONTEXT_LANE_CHARS,
            used_chars=used_chars,
            omission_receipts=tuple(omissions),
        ),
        tuple(receipts),
    )


def _build_market_lane(
    *,
    target_key: str,
    business_at: datetime,
    evidence_cards: tuple[WorkbenchEvidenceCard, ...],
    market_context: MarketContextSnapshot | None,
) -> MarketLane:
    reconciliation_required, reconciliation_reason = _market_reconciliation_requirement(
        evidence_cards
    )
    if market_context is None:
        return MarketLane(
            available=False,
            target_key=target_key,
            business_at=business_at,
            tradable_proxy_symbol="",
            bar_granularity="",
            component_status={},
            recap_reconciliation=_market_recap_reconciliation(
                market_context=None,
                reconciliation_required=reconciliation_required,
                reconciliation_reason=reconciliation_reason,
                price_volume_facts={},
            ),
            deep_market_tools_available=_MARKET_DEEP_TOOLS,
            price_volume_facts={},
            budget_chars=MAX_MARKET_LANE_CHARS,
            used_chars=0,
            omission_receipts=(),
        )
    price_volume_facts = _price_volume_facts(market_context)
    lane = MarketLane(
        available=True,
        target_key=market_context.target_key,
        business_at=business_at,
        tradable_proxy_symbol=market_context.tradable_proxy_symbol,
        bar_granularity=market_context.bar_granularity,
        component_status=dict(sorted(market_context.availability.items())),
        recap_reconciliation=_market_recap_reconciliation(
            market_context=market_context,
            reconciliation_required=reconciliation_required,
            reconciliation_reason=reconciliation_reason,
            price_volume_facts=price_volume_facts,
        ),
        deep_market_tools_available=_MARKET_DEEP_TOOLS,
        price_volume_facts=price_volume_facts,
        budget_chars=MAX_MARKET_LANE_CHARS,
        used_chars=0,
        omission_receipts=(),
    )
    used_chars = _json_char_count(lane.to_json_payload())
    if used_chars <= MAX_MARKET_LANE_CHARS:
        return MarketLane(
            available=lane.available,
            target_key=lane.target_key,
            business_at=lane.business_at,
            tradable_proxy_symbol=lane.tradable_proxy_symbol,
            bar_granularity=lane.bar_granularity,
            component_status=lane.component_status,
            recap_reconciliation=lane.recap_reconciliation,
            deep_market_tools_available=lane.deep_market_tools_available,
            price_volume_facts=lane.price_volume_facts,
            budget_chars=lane.budget_chars,
            used_chars=used_chars,
            omission_receipts=(),
        )
    compact_lane = MarketLane(
        available=lane.available,
        target_key=lane.target_key,
        business_at=lane.business_at,
        tradable_proxy_symbol=lane.tradable_proxy_symbol,
        bar_granularity=lane.bar_granularity,
        component_status=lane.component_status,
        recap_reconciliation=lane.recap_reconciliation,
        deep_market_tools_available=lane.deep_market_tools_available,
        price_volume_facts={
            key: value
            for key, value in lane.price_volume_facts.items()
            if key
            in {
                "status",
                "latest_completed_bar_end_at",
                "trailing_returns",
                "volume_bucket",
            }
        },
        budget_chars=lane.budget_chars,
        used_chars=0,
        omission_receipts=(
            WorkbenchOmissionReceipt(
                lane="market",
                budget_chars=MAX_MARKET_LANE_CHARS,
                used_chars=MAX_MARKET_LANE_CHARS,
                omitted_ref="price_volume_facts",
                reason="lane_budget_exceeded",
                available_via="market_tools",
            ),
        ),
    )
    return MarketLane(
        available=compact_lane.available,
        target_key=compact_lane.target_key,
        business_at=compact_lane.business_at,
        tradable_proxy_symbol=compact_lane.tradable_proxy_symbol,
        bar_granularity=compact_lane.bar_granularity,
        component_status=compact_lane.component_status,
        recap_reconciliation=compact_lane.recap_reconciliation,
        deep_market_tools_available=compact_lane.deep_market_tools_available,
        price_volume_facts=compact_lane.price_volume_facts,
        budget_chars=compact_lane.budget_chars,
        used_chars=_json_char_count(compact_lane.to_json_payload()),
        omission_receipts=compact_lane.omission_receipts,
    )


def _build_market_path_lane(
    *,
    target_key: str,
    business_at: datetime,
    market_context: MarketContextSnapshot | None,
) -> MarketPathLane:
    if market_context is None or market_context.market_path is None:
        return MarketPathLane(
            available=False,
            target_key=target_key,
            business_at=business_at,
            path_payload={"status": "unavailable", "reason": "market_path unavailable"},
        )
    return MarketPathLane(
        available=True,
        target_key=market_context.target_key,
        business_at=business_at,
        path_payload=_jsonable_dataclass(market_context.market_path),
    )


def _build_exposure_path_lane(
    *,
    target_key: str,
    market_path_lane: MarketPathLane,
) -> ExposurePathLane:
    latest_price = _latest_price_from_market_path_lane(market_path_lane)
    return ExposurePathLane(
        target_key=target_key,
        current_state="flat",
        target_weight=0.0,
        state_effective_at=None,
        proxy_entry_price=None,
        latest_price=latest_price,
        move_since_state_change_points=None,
        move_since_state_change_pct=None,
        mfe_points=None,
        mae_points=None,
        exposure_flags=(),
        data_status="exposure_blind_unavailable",
    )


def _latest_price_from_market_path_lane(lane: MarketPathLane) -> float | None:
    range_path = lane.path_payload.get("range_path")
    if not isinstance(range_path, dict):
        return None
    return _optional_float(range_path.get("latest_price"))


def _fallback_market_path_lane(payload: dict[str, object]) -> MarketPathLane:
    return MarketPathLane(
        available=False,
        target_key=_require_string(payload, "target_key"),
        business_at=_parse_datetime(payload.get("business_at"), "business_at"),
        path_payload={
            "status": "unavailable",
            "reason": "legacy workbench payload missing market_path_lane",
        },
    )


def _fallback_exposure_path_lane(payload: dict[str, object]) -> ExposurePathLane:
    return ExposurePathLane(
        target_key=_require_string(payload, "target_key"),
        current_state="flat",
        target_weight=0.0,
        state_effective_at=None,
        proxy_entry_price=None,
        latest_price=None,
        move_since_state_change_points=None,
        move_since_state_change_pct=None,
        mfe_points=None,
        mae_points=None,
        exposure_flags=(),
        data_status="legacy_unavailable",
    )


def _market_reconciliation_requirement(
    evidence_cards: tuple[WorkbenchEvidenceCard, ...],
) -> tuple[bool, str]:
    reasons: list[str] = []
    for card in evidence_cards:
        card_reasons: list[str] = []
        if card.event_type == "price_action":
            card_reasons.append("event_type=price_action")
        if card.evidence_review_dimensions.evidence_role == "price_recap":
            card_reasons.append("evidence_role=price_recap")
        if card.evidence_review_dimensions.recap_risk in {"medium", "high"}:
            card_reasons.append(f"recap_risk={card.evidence_review_dimensions.recap_risk}")
        if card_reasons:
            reasons.append(f"{card.event_id}: {', '.join(card_reasons)}")
    if not reasons:
        return False, "not_required"
    return True, "; ".join(reasons)


def _market_recap_reconciliation(
    *,
    market_context: MarketContextSnapshot | None,
    reconciliation_required: bool,
    reconciliation_reason: str,
    price_volume_facts: dict[str, object],
) -> MarketRecapReconciliation:
    status: RecapReconciliationStatus
    freshness = _market_data_freshness_for_context(market_context, price_volume_facts)
    if not reconciliation_required:
        status = "not_required"
        freshness = "not_required"
    elif market_context is None:
        status = "unavailable"
        freshness = "unavailable"
    elif _market_context_stale(market_context):
        status = "stale"
    else:
        price_volume_status = str(price_volume_facts.get("status") or "").strip()
        if price_volume_status in {"unavailable", "disabled"}:
            status = "unavailable"
            freshness = "unavailable"
        elif not price_volume_facts.get("trailing_returns"):
            status = "not_enough_data"
        else:
            status = "not_enough_data"
    return MarketRecapReconciliation(
        required=reconciliation_required,
        reason=reconciliation_reason,
        status=status,
        data_freshness=freshness,
        latest_visible_bar_start_at=_optional_text_value(
            price_volume_facts.get("latest_completed_bar_start_at")
        ),
        latest_visible_bar_end_at=_optional_text_value(
            price_volume_facts.get("latest_completed_bar_end_at")
        ),
        returns=_market_lane_returns(price_volume_facts),
        volume_context=_market_lane_volume_context(price_volume_facts),
    )


def _market_context_stale(market_context: MarketContextSnapshot) -> bool:
    values = (
        *tuple(str(value).lower() for value in market_context.availability.values()),
        *tuple(str(value).lower() for value in market_context.staleness.values()),
    )
    return any("stale" in value for value in values)


def _market_data_freshness_for_context(
    market_context: MarketContextSnapshot | None,
    price_volume_facts: dict[str, object],
) -> MarketDataFreshness:
    if market_context is None:
        return "unavailable"
    if _market_context_stale(market_context):
        return "stale"
    if price_volume_facts.get("available") is not True:
        return "unavailable"
    return "fresh"


def _market_lane_returns(price_volume_facts: dict[str, object]) -> dict[str, object]:
    returns = price_volume_facts.get("trailing_returns")
    if isinstance(returns, dict):
        return {
            str(key): value
            for key, value in returns.items()
            if key in {"1h", "4h", "1d"}
        }
    return {}


def _market_lane_volume_context(price_volume_facts: dict[str, object]) -> dict[str, object]:
    if not price_volume_facts:
        return {"status": "unavailable", "relative_volume_bucket": "unknown"}
    return {
        "status": "available" if price_volume_facts.get("available") is True else "unavailable",
        "relative_volume_bucket": str(price_volume_facts.get("volume_bucket") or "unknown"),
        "volume_vs_lookback": price_volume_facts.get("volume_vs_lookback"),
    }


def _price_volume_facts(market_context: MarketContextSnapshot) -> dict[str, object]:
    price_volume = market_context.price_volume
    if price_volume is None:
        return {
            "status": market_context.availability.get("price_volume", "unavailable"),
            "available": False,
        }
    return {
        "status": price_volume.status.status,
        "available": price_volume.status.status == "available",
        "latest_completed_bar_start_at": _optional_iso(
            price_volume.latest_completed_bar_start_at
        ),
        "latest_completed_bar_end_at": _optional_iso(
            price_volume.latest_completed_bar_end_at
        ),
        "latest_close": price_volume.latest_close,
        "trailing_returns": {
            "1h": price_volume.trailing_return_1h,
            "4h": price_volume.trailing_return_4h,
            "1d": price_volume.trailing_return_1d,
        },
        "volume_bucket": _volume_bucket(price_volume.volume_vs_lookback),
        "volume_vs_lookback": price_volume.volume_vs_lookback,
        "price_recap_risk": price_volume.price_recap_risk,
        "field_status": dict(sorted(price_volume.field_status.items())),
    }


def _volume_bucket(volume_vs_lookback: float | None) -> str:
    if volume_vs_lookback is None:
        return "unknown"
    if volume_vs_lookback >= 2.0:
        return "very_high"
    if volume_vs_lookback >= 1.2:
        return "high"
    if volume_vs_lookback <= 0.5:
        return "low"
    return "normal"


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else _normalize_datetime(value).isoformat()


def _memory_card_for_section(
    *,
    target_key: str,
    business_at: datetime,
    context_packet_id: str,
    page: PageReadResult | None,
    page_path: str,
    section_name: str,
    memory_read_policy: str,
    runtime_scope: Literal["live", "replay"],
    run_id: str,
) -> tuple[WorkbenchMemoryCard, WorkbenchCompilerReadReceipt | None]:
    card_id = _memory_card_id(page_path=page_path, section_name=section_name)
    if page is None:
        return (
            WorkbenchMemoryCard(
                card_id=card_id,
                page_path=page_path,
                section_name=section_name,
                status="missing",
                excerpt="",
                content_hash="",
                excerpt_hash="",
                char_count=0,
                truncated=False,
                full_section_available_via="read_section",
                compiler_read_receipt_id="",
            ),
            None,
        )
    section_payload = _find_memory_section(page.content_md, section_name=section_name)
    if section_payload is None:
        return (
            WorkbenchMemoryCard(
                card_id=card_id,
                page_path=page_path,
                section_name=section_name,
                status="missing",
                excerpt="",
                content_hash=sha256(page.content_md.encode("utf-8")).hexdigest(),
                excerpt_hash="",
                char_count=0,
                truncated=False,
                full_section_available_via="read_section",
                compiler_read_receipt_id="",
            ),
            None,
        )
    excerpt = str(section_payload["content_excerpt"])
    content_hash = str(section_payload["content_sha256"])
    excerpt_hash = sha256(excerpt.encode("utf-8")).hexdigest()
    raw_char_count = section_payload["content_char_count"]
    if not isinstance(raw_char_count, int):
        raise AnalysisWorkbenchError("memory section char count must be an integer.")
    char_count = raw_char_count
    truncated = section_payload["content_truncated"] is True
    status: MemoryCardStatus = "empty" if not excerpt.strip() else "available"
    receipt_id = _compiler_receipt_id(
        context_packet_id=context_packet_id,
        page_path=page_path,
        section_name=section_name,
        content_sha256=content_hash,
        excerpt_sha256=excerpt_hash,
    )
    card = WorkbenchMemoryCard(
        card_id=card_id,
        page_path=page_path,
        section_name=section_name,
        status=status,
        excerpt=excerpt,
        content_hash=content_hash,
        excerpt_hash=excerpt_hash,
        char_count=char_count,
        truncated=truncated,
        full_section_available_via="read_section",
        compiler_read_receipt_id=receipt_id,
    )
    receipt = WorkbenchCompilerReadReceipt(
        receipt_id=receipt_id,
        context_packet_id=context_packet_id,
        stage="analysis",
        target_key=target_key,
        business_at=business_at,
        runtime_scope=runtime_scope,
        run_id=run_id if runtime_scope == "replay" else "",
        surface_type="research_memory_section",
        page_path=page_path,
        section_name=section_name,
        content_sha256=content_hash,
        excerpt_sha256=excerpt_hash,
        excerpt_char_count=len(excerpt),
        excerpt_truncated=truncated,
        available_via="read_section",
        read_policy=memory_read_policy,
        read_run_id=run_id if runtime_scope == "replay" else "",
    )
    return card, receipt


def _empty_operator_context_lane(
    *,
    page_path: str,
    status: OperatorContextPageStatus,
) -> OperatorContextLane:
    return OperatorContextLane(
        page_path=page_path,
        page_status=status,
        cards=(),
        active_card_ids=(),
        budget_chars=MAX_OPERATOR_CONTEXT_LANE_CHARS,
        used_chars=0,
        omission_receipts=(),
    )


def _operator_context_card_omission_reason(
    card: OperatorContextCard,
) -> str | None:
    if card.is_retired:
        return "retired_operator_context_card"
    return None


def _operator_context_workbench_card(
    *,
    target_key: str,
    business_at: datetime,
    context_packet_id: str,
    page_path: str,
    source_card: OperatorContextCard,
    memory_read_policy: str,
    runtime_scope: Literal["live", "replay"],
    run_id: str,
) -> tuple[WorkbenchOperatorContextCard, OperatorContextCompilerReadReceipt]:
    excerpt = source_card.body[:OPERATOR_CONTEXT_CARD_EXCERPT_CHARS]
    content_sha = sha256(source_card.body.encode("utf-8")).hexdigest()
    excerpt_sha = sha256(excerpt.encode("utf-8")).hexdigest()
    receipt_id = _operator_context_compiler_receipt_id(
        context_packet_id=context_packet_id,
        card_id=source_card.card_id,
        page_path=page_path,
        section_name=source_card.section_name,
        content_sha256=content_sha,
        excerpt_sha256=excerpt_sha,
    )
    card = WorkbenchOperatorContextCard(
        card_id=source_card.card_id,
        section_name=source_card.section_name,
        status="active",
        excerpt=excerpt,
        content_sha256=content_sha,
        excerpt_sha256=excerpt_sha,
        char_count=len(source_card.body),
        excerpt_truncated=len(source_card.body) > len(excerpt),
        full_section_available_via="read_section",
        compiler_read_receipt_id=receipt_id,
    )
    receipt = OperatorContextCompilerReadReceipt(
        receipt_id=receipt_id,
        context_packet_id=context_packet_id,
        stage="analysis",
        target_key=target_key,
        business_at=business_at,
        runtime_scope=runtime_scope,
        run_id=run_id if runtime_scope == "replay" else "",
        page_path=page_path,
        section_name=source_card.section_name,
        content_sha256=content_sha,
        excerpt_sha256=excerpt_sha,
        excerpt_char_count=len(excerpt),
        excerpt_truncated=card.excerpt_truncated,
        read_policy=memory_read_policy,
        read_run_id=run_id if runtime_scope == "replay" else "",
    )
    return card, receipt


def _operator_context_card_sort_key(
    card: OperatorContextCard,
) -> tuple[str, str]:
    return card.section_name, card.card_id


def _evidence_card(
    record: EvidenceLedgerRecord,
    *,
    excerpt_limit: int,
) -> WorkbenchEvidenceCard:
    excerpt = _bounded_excerpt(record.content, limit=excerpt_limit)
    dimensions = classify_evidence_review_dimensions(record)
    grounding_status: WorkbenchGroundingStatus = (
        "compiled_excerpt_truncated"
        if excerpt.truncated
        else "compiled_excerpt_available"
    )
    return WorkbenchEvidenceCard(
        event_id=record.event_id,
        source_ref=record.source_ref,
        title=record.title,
        event_type=_safe_event_type(record),
        source_kind=_safe_source_kind(record),
        labels=tuple(record.labels),
        evidence_review_dimensions=dimensions,
        operator_source_metadata=_operator_source_metadata_for_record(record),
        source_excerpt=excerpt,
        grounding_status=grounding_status,
        requires_deep_read_for_full_quote_or_conflict=excerpt.truncated,
    )


def _future_observed_evidence_exclusion_receipt(
    *,
    record: EvidenceLedgerRecord,
    used_chars: int,
    future_leakage: HistoricalWebSearchFutureLeakage,
) -> WorkbenchOmissionReceipt:
    return WorkbenchOmissionReceipt(
        lane="evidence",
        budget_chars=MAX_EVIDENCE_LANE_CHARS,
        used_chars=used_chars,
        omitted_ref=record.event_id,
        reason="future_observed_evidence_excluded",
        available_via="excluded_from_analysis_prompt",
        details={
            "event_id": record.event_id,
            "source_ref": record.source_ref,
            "reason_code": future_leakage.reason_code,
            "matched_date": future_leakage.matched_date.isoformat(),
            "matched_snippet": future_leakage.matched_snippet,
        },
    )


def _operator_source_metadata_for_record(
    record: EvidenceLedgerRecord,
) -> OperatorSourceMetadata | None:
    try:
        metadata = extract_operator_source_metadata(record.labels)
    except SourcePolicyError as exc:
        raise AnalysisWorkbenchError(str(exc)) from exc
    if metadata is None:
        return None
    return OperatorSourceMetadata(
        operator_confidence=metadata["operator_confidence"],
        operator_source_basis_value=metadata["operator_source_basis"],
    )


def _bounded_excerpt(content: str, *, limit: int) -> WorkbenchSourceExcerpt:
    if limit <= 0:
        excerpt = ""
    else:
        excerpt = content[:limit]
    return WorkbenchSourceExcerpt(
        text=excerpt,
        sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
        content_sha256=sha256(content.encode("utf-8")).hexdigest(),
        char_count=len(excerpt),
        truncated=len(content) > len(excerpt),
        full_text_available_via="read_evidence",
    )


def _find_memory_section(content_md: str, *, section_name: str) -> dict[str, object] | None:
    try:
        return find_markdown_section(
            content_md,
            heading=section_name,
            heading_level=2,
            excerpt_char_limit=MEMORY_SECTION_EXCERPT_CHARS,
        )
    except MarkdownContextError:
        return None


def _find_operator_context_section(
    content_md: str,
    *,
    section_name: str,
) -> dict[str, object] | None:
    try:
        return find_markdown_section(
            content_md,
            heading=section_name,
            heading_level=2,
            excerpt_char_limit=OPERATOR_CONTEXT_CARD_EXCERPT_CHARS,
        )
    except MarkdownContextError:
        return None


def _memory_card_id(*, page_path: str, section_name: str) -> str:
    return f"{page_path}:{section_name}"


def _compiler_receipt_id(
    *,
    context_packet_id: str,
    page_path: str,
    section_name: str,
    content_sha256: str,
    excerpt_sha256: str,
) -> str:
    digest = sha256(
        json.dumps(
            {
                "context_packet_id": context_packet_id,
                "page_path": page_path,
                "section_name": section_name,
                "content_sha256": content_sha256,
                "excerpt_sha256": excerpt_sha256,
                "compiler_policy_version": WORKBENCH_COMPILER_POLICY_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"workbench-compiler-memory-read:{digest}"


def _operator_context_compiler_receipt_id(
    *,
    context_packet_id: str,
    card_id: str,
    page_path: str,
    section_name: str,
    content_sha256: str,
    excerpt_sha256: str,
) -> str:
    digest = sha256(
        json.dumps(
            {
                "context_packet_id": context_packet_id,
                "card_id": card_id,
                "page_path": page_path,
                "section_name": section_name,
                "content_sha256": content_sha256,
                "excerpt_sha256": excerpt_sha256,
                "compiler_policy_version": WORKBENCH_COMPILER_POLICY_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"operator-context-compiler-read:{digest}"


def _safe_event_type(record: EvidenceLedgerRecord) -> str:
    try:
        return extract_event_type(record.labels)
    except SourcePolicyError:
        return "other"


def _safe_source_kind(record: EvidenceLedgerRecord) -> str:
    try:
        return extract_source_kind(record.labels)
    except SourcePolicyError:
        return "unknown"


def _dimensions_from_payload(payload: object) -> EvidenceReviewDimensions:
    if not isinstance(payload, dict):
        raise AnalysisWorkbenchError("evidence_review_dimensions must be an object.")
    return EvidenceReviewDimensions(
        evidence_role=_require_string(payload, "evidence_role"),
        fact_subjectivity_balance=_require_string(
            payload,
            "fact_subjectivity_balance",
        ),
        recap_risk=_require_string(payload, "recap_risk"),
        rationale=_require_string(payload, "rationale"),
    )


def _grounding_status(value: object) -> WorkbenchGroundingStatus:
    if value in {
        "compiled_excerpt_available",
        "compiled_excerpt_truncated",
        "compiled_excerpt_unavailable",
    }:
        return value  # type: ignore[return-value]
    raise AnalysisWorkbenchError("grounding_status is invalid.")


def _evidence_grounding_status(value: object) -> EvidenceGroundingStatus:
    if value in {"compiled", "tool_read", "missing"}:
        return value  # type: ignore[return-value]
    raise AnalysisWorkbenchError("evidence_grounding is invalid.")


def _memory_grounding_status(value: object) -> MemoryGroundingStatus:
    if value in {"compiled", "tool_read", "missing"}:
        return value  # type: ignore[return-value]
    raise AnalysisWorkbenchError("memory_grounding is invalid.")


def _market_grounding_status(value: object) -> MarketGroundingStatus:
    if value in {"compiled", "tool_read", "not_required", "missing"}:
        return value  # type: ignore[return-value]
    raise AnalysisWorkbenchError("market_grounding is invalid.")


def _recap_reconciliation_status(value: object) -> RecapReconciliationStatus:
    if value in {
        "directionally_consistent",
        "not_directionally_consistent",
        "not_enough_data",
        "stale",
        "unavailable",
        "not_required",
    }:
        return value  # type: ignore[return-value]
    raise AnalysisWorkbenchError("recap_reconciliation is invalid.")


def _market_data_freshness(value: object) -> MarketDataFreshness:
    if value in {"fresh", "stale", "unavailable", "not_required"}:
        return value  # type: ignore[return-value]
    raise AnalysisWorkbenchError("data_freshness is invalid.")


def _memory_card_status(value: object) -> MemoryCardStatus:
    if value in {"available", "missing", "empty"}:
        return value  # type: ignore[return-value]
    raise AnalysisWorkbenchError("memory card status is invalid.")


def _operator_context_page_status(value: object) -> OperatorContextPageStatus:
    if value in {"available", "missing", "empty"}:
        return value  # type: ignore[return-value]
    raise AnalysisWorkbenchError("operator context page status is invalid.")


def _workbench_operator_context_card_status(
    value: object,
) -> WorkbenchOperatorContextCardStatus:
    if value == "active":
        return value  # type: ignore[return-value]
    raise AnalysisWorkbenchError("operator context card status is invalid.")


def _validate_market_payload_is_low_commitment(payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    forbidden = [term for term in _FORBIDDEN_MARKET_PAYLOAD_TERMS if term in encoded]
    if forbidden:
        raise AnalysisWorkbenchError(
            "market_lane payload contains forbidden deterministic conclusion terms: "
            f"{', '.join(forbidden)}"
        )


def _json_char_count(payload: object) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _jsonable_dataclass(value: object) -> dict[str, object]:
    payload = _jsonable(value)
    if not isinstance(payload, dict):
        raise AnalysisWorkbenchError("dataclass payload must serialize to an object.")
    return payload


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {
            field_name: _jsonable(getattr(value, field_name))
            for field_name in value.__dataclass_fields__
        }
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _jsonable(raw_value)
            for key, raw_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return value


def _normalize_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise AnalysisWorkbenchError("timestamp must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise AnalysisWorkbenchError("timestamp must be timezone-aware.")
    return value.astimezone(UTC)


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise AnalysisWorkbenchError(f"{field_name} must be an ISO datetime string.")
    try:
        return _normalize_datetime(datetime.fromisoformat(value))
    except ValueError as exc:
        raise AnalysisWorkbenchError(
            f"{field_name} must be an ISO datetime string."
        ) from exc


def _require_string(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise AnalysisWorkbenchError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise AnalysisWorkbenchError(f"{field_name} must not be blank.")
    return normalized


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise AnalysisWorkbenchError(f"{field_name} must be a boolean.")
    return value


def _require_raw_string(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise AnalysisWorkbenchError(f"{field_name} must be a string.")
    return value


def _optional_string(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AnalysisWorkbenchError(f"{field_name} must be a string.")
    return value.strip()


def _optional_text_value(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnalysisWorkbenchError("optional text value must be a string.")
    return value


def _require_non_blank_target_key(target_key: str) -> str:
    if not isinstance(target_key, str):
        raise AnalysisWorkbenchError("target_key must be a string.")
    normalized = target_key.strip()
    if not normalized:
        raise AnalysisWorkbenchError("target_key must not be blank.")
    return normalized


def _optional_raw_string(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AnalysisWorkbenchError(f"{field_name} must be a string.")
    return value


def _require_non_negative_int(payload: dict[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AnalysisWorkbenchError(f"{field_name} must be a non-negative integer.")
    return value


def _require_float(value: object, field_name: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise AnalysisWorkbenchError(f"{field_name} must be a number.")
    return float(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise AnalysisWorkbenchError("optional numeric value must be a number.")
    return float(value)


def _validate_optional_float(value: object, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise AnalysisWorkbenchError(f"{field_name} must be a number when present.")


def _optional_non_negative_int(payload: dict[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AnalysisWorkbenchError(f"{field_name} must be a non-negative integer.")
    return value


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AnalysisWorkbenchError(f"{field_name} must be a list.")
    return tuple(str(item) for item in value)


def _validate_string_tuple(value: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise AnalysisWorkbenchError(f"{field_name} must be a tuple.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise AnalysisWorkbenchError(f"{field_name} must contain non-blank strings.")
        normalized.append(item.strip())
    return tuple(normalized)


def _require_view_state(value: object, field_name: str) -> ViewState:
    if value not in {
        "flat",
        "weak_long",
        "strong_long",
        "weak_short",
        "strong_short",
    }:
        raise AnalysisWorkbenchError(
            f"{field_name} must be one of the five allowed view states."
        )
    return cast(ViewState, value)


def _require_list(payload: dict[str, object], field_name: str) -> list[object]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise AnalysisWorkbenchError(f"{field_name} must be a list.")
    return value


def _optional_list(payload: dict[str, object], field_name: str) -> list[object]:
    value = payload.get(field_name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise AnalysisWorkbenchError(f"{field_name} must be a list.")
    return value


def _require_json_object(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AnalysisWorkbenchError(f"{field_name} must be an object.")
    return dict(value)


def _optional_object_dict(value: object, field_name: str) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AnalysisWorkbenchError(f"{field_name} must be an object.")
    return dict(value)


__all__ = [
    "ACTIVE_EVIDENCE_EXCERPT_CHARS",
    "MAX_EVIDENCE_LANE_CHARS",
    "MAX_MARKET_LANE_CHARS",
    "MAX_MEMORY_IMPACT_LANE_CHARS",
    "MAX_METHOD_LANE_CHARS",
    "MAX_OPERATOR_CONTEXT_LANE_CHARS",
    "MEMORY_SECTION_EXCERPT_CHARS",
    "OPERATOR_CONTEXT_CARD_EXCERPT_CHARS",
    "WORKBENCH_COMPILER_POLICY_VERSION",
    "AnalysisWorkbench",
    "AnalysisWorkbenchError",
    "EvidenceLane",
    "GroundingCoverageReceipt",
    "ExposurePathLane",
    "MarketLane",
    "MarketSetupLane",
    "MarketPathLane",
    "MarketRecapReconciliation",
    "MemoryImpactLane",
    "MethodLane",
    "OperatorContextCompilerReadReceipt",
    "OperatorContextLane",
    "OperatorSourceMetadata",
    "WorkbenchCompilerReadReceipt",
    "WorkbenchEvidenceCard",
    "WorkbenchMemoryCard",
    "WorkbenchOmissionReceipt",
    "WorkbenchOperatorContextCard",
    "WorkbenchSourceExcerpt",
    "build_analysis_workbench",
    "hash_analysis_workbench_payload",
]
