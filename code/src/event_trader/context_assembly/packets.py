"""Shared context packet contracts, serialization, and visibility audits."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, cast

from event_trader.context_assembly.claim_cards import (
    ResearchClaimCard,
    ResearchClaimUsageReceipt,
)
from event_trader.context_assembly.workbench import AnalysisWorkbench
from event_trader.contracts.research_memory import target_page_template
from event_trader.contracts.temporal_visibility import (
    DecisionVisibilityBoundary,
    DecisionVisibilityBoundaryError,
)
from event_trader.learning_cards import LearningCard, parse_learning_card
from event_trader.market.contracts import MarketContextSnapshot

CURRENT_MEMORY_READ_POLICY = "current_page_read_v1"
HISTORICAL_THESIS_SNAPSHOT_MEMORY_READ_POLICY = (
    "historical_thesis_snapshot_read_v1"
)
_OPERATOR_BOOTSTRAP_TEMPLATE_HASH = sha256(
    target_page_template("operator.md").encode("utf-8")
).hexdigest()

type ContextPacketStage = Literal["checker", "analysis"]
type ContextPacketRuntimeScope = Literal["live", "replay"]


class ContextAssemblyError(ValueError):
    """Raised for malformed context packets or selector inputs."""


type ContextReceipt = ResearchClaimUsageReceipt | dict[str, object]


@dataclass(frozen=True, slots=True)
class ContextItem:
    surface_type: str
    source_id: str
    scope: str
    business_time: datetime
    visible_time: datetime
    content_hash: str
    selection_reason: str
    budget_class: str
    deep_read_allowed: bool
    deep_read_happened: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "business_time", _normalize_datetime(self.business_time))
        object.__setattr__(self, "visible_time", _normalize_datetime(self.visible_time))

    def to_json_payload(self) -> dict[str, object]:
        return {
            "surface_type": self.surface_type,
            "source_id": self.source_id,
            "scope": self.scope,
            "business_time": self.business_time.isoformat(),
            "visible_time": self.visible_time.isoformat(),
            "content_hash": self.content_hash,
            "selection_reason": self.selection_reason,
            "budget_class": self.budget_class,
            "deep_read_allowed": self.deep_read_allowed,
            "deep_read_happened": self.deep_read_happened,
        }


@dataclass(frozen=True, slots=True)
class ContextPacket:
    packet_id: str
    stage: str
    target_key: str
    business_at: datetime
    items: tuple[ContextItem, ...]
    receipts: tuple[ContextReceipt, ...]
    claim_cards: tuple[ResearchClaimCard, ...] = ()
    learning_cards: tuple[LearningCard, ...] = ()
    analysis_workbench: AnalysisWorkbench | None = None
    runtime_scope: ContextPacketRuntimeScope = "live"
    run_id: str = ""
    packet_hash: str = ""
    _decision_visibility: DecisionVisibilityBoundary = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.stage not in {"checker", "analysis"}:
            raise ContextAssemblyError("stage must be checker or analysis.")
        if self.runtime_scope not in {"live", "replay"}:
            raise ContextAssemblyError("runtime_scope must be live or replay.")
        run_id = self.run_id.strip() if isinstance(self.run_id, str) else ""
        if self.runtime_scope == "live":
            if run_id:
                raise ContextAssemblyError("live context packets must not carry run_id.")
        else:
            if not run_id:
                raise ContextAssemblyError("replay context packets require run_id.")
        try:
            decision_visibility = DecisionVisibilityBoundary.at_business_time(
                _normalize_datetime(self.business_at)
            )
        except DecisionVisibilityBoundaryError as exc:
            raise ContextAssemblyError(str(exc)) from exc
        object.__setattr__(self, "business_at", decision_visibility.business_at)
        object.__setattr__(self, "_decision_visibility", decision_visibility)
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "receipts", tuple(self.receipts))
        object.__setattr__(self, "claim_cards", tuple(self.claim_cards))
        object.__setattr__(self, "learning_cards", tuple(self.learning_cards))
        object.__setattr__(self, "run_id", run_id)
        packet_id = self.packet_id.strip() if isinstance(self.packet_id, str) else ""
        if not packet_id:
            packet_id = _default_packet_id(
                stage=self.stage,
                target_key=self.target_key,
                business_at=self.business_at,
            )
        object.__setattr__(self, "packet_id", packet_id)
        if self.analysis_workbench is not None:
            if self.stage != "analysis":
                raise ContextAssemblyError(
                    "analysis_workbench is only valid for analysis packets."
                )
            if self.analysis_workbench.target_key != self.target_key:
                raise ContextAssemblyError(
                    "analysis_workbench target_key must match packet target_key."
                )
            if self.analysis_workbench.business_at != self.business_at:
                raise ContextAssemblyError(
                    "analysis_workbench business_at must match packet business_at."
                )
            if self.analysis_workbench.context_packet_id != packet_id:
                raise ContextAssemblyError(
                    "analysis_workbench context_packet_id must match packet_id."
                )
        if not self.packet_hash:
            object.__setattr__(self, "packet_hash", hash_context_packet(self))

    @property
    def selected_claim_ids(self) -> tuple[str, ...]:
        return tuple(card.claim_id for card in self.claim_cards)

    @property
    def selected_learning_card_ids(self) -> tuple[str, ...]:
        return tuple(card.card_id for card in self.learning_cards)

    @property
    def decision_visibility(self) -> DecisionVisibilityBoundary:
        return self._decision_visibility

    def to_json_payload(self, *, include_hash: bool = True) -> dict[str, object]:
        return {
            "packet_id": self.packet_id,
            "stage": self.stage,
            "target_key": self.target_key,
            "business_at": self.business_at.isoformat(),
            "runtime_scope": self.runtime_scope,
            "run_id": self.run_id,
            "items": [item.to_json_payload() for item in self.items],
            "receipts": [_receipt_payload(receipt) for receipt in self.receipts],
            "claim_cards": [card.to_json_payload() for card in self.claim_cards],
            "learning_cards": [
                card.to_json_payload() for card in self.learning_cards
            ],
            "analysis_workbench": (
                None
                if self.analysis_workbench is None
                else self.analysis_workbench.to_json_payload()
            ),
            **({"packet_hash": self.packet_hash} if include_hash else {}),
        }


@dataclass(frozen=True, slots=True)
class VisibilityAudit:
    status: Literal["passed", "failed"]
    violation_count: int
    violations: tuple[dict[str, object], ...]

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "violation_count": self.violation_count,
            "violations": list(self.violations),
        }


@dataclass(frozen=True, slots=True)
class ReplayVisibilityAuditReport:
    packet_count: int
    violation_count: int
    violated_item_ids: tuple[str, ...]
    failed_stages: tuple[str, ...]

    @property
    def status(self) -> Literal["passed", "failed"]:
        return "failed" if self.violation_count else "passed"

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "packet_count": self.packet_count,
            "violation_count": self.violation_count,
            "violated_item_ids": list(self.violated_item_ids),
            "failed_stages": list(self.failed_stages),
        }


def serialize_context_packet(packet: ContextPacket) -> str:
    return json.dumps(packet.to_json_payload(), ensure_ascii=False)


def hash_context_packet(packet: ContextPacket) -> str:
    payload = packet.to_json_payload(include_hash=False)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def validate_context_packet_visibility(
    packet: ContextPacket,
    *,
    required_memory_read_policy: str | None = None,
    required_runtime_scope: ContextPacketRuntimeScope | None = None,
    required_run_id: str | None = None,
) -> VisibilityAudit:
    violations: list[dict[str, object]] = []
    if required_runtime_scope is not None and packet.runtime_scope != required_runtime_scope:
        violations.append(
            {
                "violation_type": "runtime_scope_mismatch",
                "packet_id": packet.packet_id,
                "stage": packet.stage,
                "source_id": packet.packet_id,
                "runtime_scope": packet.runtime_scope,
                "required_runtime_scope": required_runtime_scope,
            }
        )
    if required_run_id is not None and packet.run_id != required_run_id:
        violations.append(
            {
                "violation_type": "run_id_mismatch",
                "packet_id": packet.packet_id,
                "stage": packet.stage,
                "source_id": packet.packet_id,
                "run_id": packet.run_id,
                "required_run_id": required_run_id,
            }
        )
    memory_reads = _research_memory_read_receipts_by_path(packet)
    for item in packet.items:
        if item.visible_time > packet.decision_visibility.business_at:
            violations.append(
                {
                    "violation_type": "future_visible_context",
                    "packet_id": packet.packet_id,
                    "stage": packet.stage,
                    "source_id": item.source_id,
                    "surface_type": item.surface_type,
                    "visible_time": item.visible_time.isoformat(),
                    "business_at": packet.decision_visibility.business_at.isoformat(),
                }
            )
        read_path = _research_memory_read_path_for_item(item)
        if read_path is None:
            continue
        read_receipts = memory_reads.get(read_path, ())
        if not read_receipts:
            violations.append(
                {
                    "violation_type": "missing_research_memory_read",
                    "packet_id": packet.packet_id,
                    "stage": packet.stage,
                    "source_id": item.source_id,
                    "surface_type": item.surface_type,
                    "page_path": read_path,
                }
            )
            continue
        if required_memory_read_policy is not None:
            invalid_policies = tuple(
                receipt.get("read_policy")
                for receipt in read_receipts
                if receipt.get("read_policy") != required_memory_read_policy
            )
            if invalid_policies:
                violations.append(
                    {
                        "violation_type": "invalid_research_memory_read_policy",
                        "packet_id": packet.packet_id,
                        "stage": packet.stage,
                        "source_id": item.source_id,
                        "surface_type": item.surface_type,
                        "page_path": read_path,
                        "required_read_policy": required_memory_read_policy,
                        "read_policies": list(invalid_policies),
                    }
                )
                continue
        provenance_violations = _research_memory_read_provenance_violations(
            read_receipts=read_receipts,
            required_memory_read_policy=required_memory_read_policy,
            packet=packet,
            item=item,
            page_path=read_path,
        )
        if provenance_violations:
            violations.extend(provenance_violations)
            continue
        if required_run_id is not None:
            invalid_run_ids = tuple(
                receipt.get("read_run_id")
                for receipt in read_receipts
                if receipt.get("read_run_id") != required_run_id
            )
            if invalid_run_ids:
                violations.append(
                    {
                        "violation_type": "research_memory_read_run_id_mismatch",
                        "packet_id": packet.packet_id,
                        "stage": packet.stage,
                        "source_id": item.source_id,
                        "surface_type": item.surface_type,
                        "page_path": read_path,
                        "required_read_run_id": required_run_id,
                        "read_run_ids": list(invalid_run_ids),
                    }
                )
                continue
        if (
            item.surface_type == "research_memory_page"
            and item.content_hash
            not in {
                str(receipt.get("content_sha256"))
                for receipt in read_receipts
                if isinstance(receipt.get("content_sha256"), str)
            }
        ):
            violations.append(
                {
                    "violation_type": "research_memory_read_hash_mismatch",
                    "packet_id": packet.packet_id,
                    "stage": packet.stage,
                    "source_id": item.source_id,
                    "surface_type": item.surface_type,
                    "page_path": read_path,
                }
            )
    return VisibilityAudit(
        status="failed" if violations else "passed",
        violation_count=len(violations),
        violations=tuple(violations),
    )


def build_replay_visibility_audit_report(
    packets: tuple[ContextPacket, ...],
    *,
    required_memory_read_policy: str | None = None,
    required_runtime_scope: ContextPacketRuntimeScope | None = None,
    required_run_id: str | None = None,
) -> ReplayVisibilityAuditReport:
    violations: list[dict[str, object]] = []
    failed_stages: set[str] = set()
    for packet in packets:
        audit = validate_context_packet_visibility(
            packet,
            required_memory_read_policy=required_memory_read_policy,
            required_runtime_scope=required_runtime_scope,
            required_run_id=required_run_id,
        )
        if audit.status == "failed":
            failed_stages.add(packet.stage)
            violations.extend(audit.violations)
    violated_item_ids = tuple(
        sorted(
            str(violation["source_id"])
            for violation in violations
            if "source_id" in violation
        )
    )
    return ReplayVisibilityAuditReport(
        packet_count=len(packets),
        violation_count=len(violations),
        violated_item_ids=violated_item_ids,
        failed_stages=tuple(sorted(failed_stages)),
    )


def parse_context_packet_payload(payload: str | Mapping[str, object]) -> ContextPacket:
    data = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(data, Mapping):
        raise ContextAssemblyError("context packet payload must be a JSON object.")
    claim_cards = tuple(
        ResearchClaimCard.from_json_payload(item)
        for item in _optional_list(data, "claim_cards")
    )
    learning_cards = tuple(
        parse_learning_card(_require_mapping(item, "learning_cards"))
        for item in _optional_list(data, "learning_cards")
    )
    analysis_workbench_payload = data.get("analysis_workbench")
    analysis_workbench = (
        None
        if analysis_workbench_payload is None
        else AnalysisWorkbench.from_json_payload(analysis_workbench_payload)
    )
    receipts = tuple(_receipt_from_payload(item) for item in _require_list(data, "receipts"))
    packet = ContextPacket(
        packet_id=_require_text(data, "packet_id"),
        stage=_require_text(data, "stage"),
        target_key=_require_text(data, "target_key"),
        business_at=_parse_datetime(data.get("business_at"), "business_at"),
        items=tuple(_context_item_from_payload(item) for item in _require_list(data, "items")),
        receipts=receipts,
        claim_cards=claim_cards,
        learning_cards=learning_cards,
        analysis_workbench=analysis_workbench,
        runtime_scope=_parse_runtime_scope(data.get("runtime_scope", "live")),
        run_id=_optional_text(data, "run_id"),
    )
    expected_hash = (
        _require_text(data, "packet_hash")
        if isinstance(data.get("packet_hash"), str)
        else ""
    )
    if expected_hash and packet.packet_hash != expected_hash:
        raise ContextAssemblyError("context packet hash does not match payload.")
    return packet


def _market_context_items(
    market_context: MarketContextSnapshot | None,
) -> tuple[ContextItem, ...]:
    if market_context is None:
        return ()
    return (
        ContextItem(
            surface_type="market_context",
            source_id=f"market_context:{market_context.target_key}",
            scope=f"target:{market_context.target_key}",
            business_time=market_context.as_of_at,
            visible_time=market_context.as_of_at,
            content_hash=market_context.payload_hash(),
            selection_reason="configured_market_context",
            budget_class="market_context",
            deep_read_allowed=True,
            deep_read_happened=False,
        ),
    )


def _research_memory_read_receipt(
    *,
    stage: ContextPacketStage,
    target_key: str,
    page_path: str,
    business_at: datetime,
    content_sha256: str,
    read_policy: str,
    read_run_id: str = "",
    snapshot_source: str | None = None,
    snapshot_revision_id: str | None = None,
    snapshot_committed_at: str | None = None,
) -> dict[str, object]:
    receipt = {
        "receipt_type": "research_memory_read",
        "stage": stage,
        "target_key": target_key,
        "page_path": page_path,
        "business_at": business_at.isoformat(),
        "read_at": business_at.isoformat(),
        "content_sha256": content_sha256,
        "read_policy": read_policy,
        "read_run_id": read_run_id,
    }
    if snapshot_source is not None:
        receipt["snapshot_source"] = snapshot_source
    if snapshot_revision_id is not None:
        receipt["snapshot_revision_id"] = snapshot_revision_id
    if snapshot_committed_at is not None:
        receipt["snapshot_committed_at"] = snapshot_committed_at
    return receipt


def _default_packet_id(
    *,
    stage: str,
    target_key: str,
    business_at: datetime,
    source_ids: tuple[str, ...] = (),
) -> str:
    base = f"context-packet:{stage}:{target_key}:{business_at.isoformat()}"
    normalized_source_ids = tuple(
        str(source_id).strip() for source_id in source_ids if str(source_id).strip()
    )
    if not normalized_source_ids:
        return base
    if len(normalized_source_ids) == 1:
        return f"{base}:source:{normalized_source_ids[0]}"
    digest = sha256(
        json.dumps(
            normalized_source_ids,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"{base}:sources:{digest}"


def _normalize_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ContextAssemblyError("timestamp must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContextAssemblyError("timestamp must be timezone-aware.")
    return value.astimezone(UTC)


def _receipt_from_payload(payload: object) -> ContextReceipt:
    if not isinstance(payload, Mapping):
        raise ContextAssemblyError("context receipt must be an object.")
    if payload.get("receipt_type") == "research_claim_usage" or "selected_claim_ids" in payload:
        return ResearchClaimUsageReceipt.from_json_payload(dict(payload))
    return dict(payload)


def _receipt_payload(receipt: ContextReceipt) -> dict[str, object]:
    if isinstance(receipt, ResearchClaimUsageReceipt):
        return {
            "receipt_type": "research_claim_usage",
            **receipt.to_json_payload(),
        }
    return dict(receipt)


def _research_memory_read_receipts_by_path(
    packet: ContextPacket,
) -> dict[str, tuple[dict[str, object], ...]]:
    receipts: dict[str, list[dict[str, object]]] = {}
    for receipt in packet.receipts:
        if not isinstance(receipt, dict):
            continue
        if receipt.get("receipt_type") != "research_memory_read":
            continue
        page_path = receipt.get("page_path")
        content_sha256 = receipt.get("content_sha256")
        if not isinstance(page_path, str) or not page_path.strip():
            continue
        if not isinstance(content_sha256, str) or not content_sha256.strip():
            continue
        receipts.setdefault(page_path.strip(), []).append(dict(receipt))
    return {page_path: tuple(values) for page_path, values in receipts.items()}


def _research_memory_read_provenance_violations(
    *,
    read_receipts: tuple[dict[str, object], ...],
    required_memory_read_policy: str | None,
    packet: ContextPacket,
    item: ContextItem,
    page_path: str,
) -> tuple[dict[str, object], ...]:
    violations: list[dict[str, object]] = []
    for receipt in read_receipts:
        read_policy = receipt.get("read_policy")
        expects_snapshot_provenance = (
            read_policy == HISTORICAL_THESIS_SNAPSHOT_MEMORY_READ_POLICY
        )
        if (
            required_memory_read_policy == HISTORICAL_THESIS_SNAPSHOT_MEMORY_READ_POLICY
            and not expects_snapshot_provenance
        ):
            continue
        snapshot_source = receipt.get("snapshot_source")
        snapshot_revision_id = receipt.get("snapshot_revision_id")
        snapshot_committed_at = receipt.get("snapshot_committed_at")
        if expects_snapshot_provenance:
            violation = _historical_snapshot_provenance_violation(
                packet=packet,
                item=item,
                page_path=page_path,
                content_sha256=receipt.get("content_sha256"),
                snapshot_source=snapshot_source,
                snapshot_revision_id=snapshot_revision_id,
                snapshot_committed_at=snapshot_committed_at,
            )
            if violation is not None:
                violations.append(violation)
            continue
        if any(
            value not in {None, ""}
            for value in (
                snapshot_source,
                snapshot_revision_id,
                snapshot_committed_at,
            )
        ):
            violations.append(
                {
                    "violation_type": "unexpected_historical_research_memory_read_provenance",
                    "packet_id": packet.packet_id,
                    "stage": packet.stage,
                    "source_id": item.source_id,
                    "surface_type": item.surface_type,
                    "page_path": page_path,
                    "read_policy": read_policy,
                }
            )
    return tuple(violations)


def _historical_snapshot_provenance_violation(
    *,
    packet: ContextPacket,
    item: ContextItem,
    page_path: str,
    content_sha256: object,
    snapshot_source: object,
    snapshot_revision_id: object,
    snapshot_committed_at: object,
) -> dict[str, object] | None:
    if _is_legacy_operator_bootstrap_read(
        packet=packet,
        item=item,
        page_path=page_path,
        content_sha256=content_sha256,
        snapshot_source=snapshot_source,
        snapshot_revision_id=snapshot_revision_id,
        snapshot_committed_at=snapshot_committed_at,
    ):
        return None
    if snapshot_source == "bootstrap_template":
        if snapshot_revision_id in {None, ""} and snapshot_committed_at in {None, ""}:
            return None
        return {
            "violation_type": "invalid_historical_research_memory_read_provenance",
            "packet_id": packet.packet_id,
            "stage": packet.stage,
            "source_id": item.source_id,
            "surface_type": item.surface_type,
            "page_path": page_path,
            "snapshot_source": snapshot_source,
            "reason": "bootstrap_template reads must not include revision provenance.",
        }
    if snapshot_source == "thesis_revision":
        if not isinstance(snapshot_revision_id, str) or not snapshot_revision_id.strip():
            return {
                "violation_type": "invalid_historical_research_memory_read_provenance",
                "packet_id": packet.packet_id,
                "stage": packet.stage,
                "source_id": item.source_id,
                "surface_type": item.surface_type,
                "page_path": page_path,
                "snapshot_source": snapshot_source,
                "reason": "thesis_revision reads require snapshot_revision_id.",
            }
        if not isinstance(snapshot_committed_at, str) or not snapshot_committed_at.strip():
            return {
                "violation_type": "invalid_historical_research_memory_read_provenance",
                "packet_id": packet.packet_id,
                "stage": packet.stage,
                "source_id": item.source_id,
                "surface_type": item.surface_type,
                "page_path": page_path,
                "snapshot_source": snapshot_source,
                "reason": "thesis_revision reads require snapshot_committed_at.",
            }
        try:
            _parse_datetime(snapshot_committed_at, "snapshot_committed_at")
        except ContextAssemblyError:
            return {
                "violation_type": "invalid_historical_research_memory_read_provenance",
                "packet_id": packet.packet_id,
                "stage": packet.stage,
                "source_id": item.source_id,
                "surface_type": item.surface_type,
                "page_path": page_path,
                "snapshot_source": snapshot_source,
                "reason": "snapshot_committed_at must be an ISO datetime string.",
            }
        return None
    return {
        "violation_type": "invalid_historical_research_memory_read_provenance",
        "packet_id": packet.packet_id,
        "stage": packet.stage,
        "source_id": item.source_id,
        "surface_type": item.surface_type,
        "page_path": page_path,
        "snapshot_source": snapshot_source,
        "reason": (
            "historical thesis snapshot reads require snapshot_source to be "
            "bootstrap_template or thesis_revision."
        ),
    }


def _is_legacy_operator_bootstrap_read(
    *,
    packet: ContextPacket,
    item: ContextItem,
    page_path: str,
    content_sha256: object,
    snapshot_source: object,
    snapshot_revision_id: object,
    snapshot_committed_at: object,
) -> bool:
    return (
        page_path == f"targets/{packet.target_key}/operator.md"
        and item.surface_type == "research_memory_page"
        and item.source_id == page_path
        and item.content_hash == _OPERATOR_BOOTSTRAP_TEMPLATE_HASH
        and content_sha256 == _OPERATOR_BOOTSTRAP_TEMPLATE_HASH
        and snapshot_source in {None, ""}
        and snapshot_revision_id in {None, ""}
        and snapshot_committed_at in {None, ""}
    )


def _research_memory_read_path_for_item(item: ContextItem) -> str | None:
    if item.surface_type == "research_memory_page":
        return item.source_id
    if item.surface_type == "research_memory_section":
        page_path, separator, _section_name = item.source_id.partition(":")
        return page_path if separator and page_path else None
    return None


def _context_item_from_payload(payload: object) -> ContextItem:
    if not isinstance(payload, Mapping):
        raise ContextAssemblyError("context item must be an object.")
    return ContextItem(
        surface_type=_require_text(payload, "surface_type"),
        source_id=_require_text(payload, "source_id"),
        scope=_require_text(payload, "scope"),
        business_time=_parse_datetime(payload.get("business_time"), "business_time"),
        visible_time=_parse_datetime(payload.get("visible_time"), "visible_time"),
        content_hash=_require_text(payload, "content_hash"),
        selection_reason=_require_text(payload, "selection_reason"),
        budget_class=_require_text(payload, "budget_class"),
        deep_read_allowed=payload.get("deep_read_allowed") is True,
        deep_read_happened=payload.get("deep_read_happened") is True,
    )


def _require_list(payload: Mapping[str, object], field_name: str) -> list[object]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise ContextAssemblyError(f"{field_name} must be a list.")
    return value


def _optional_list(payload: Mapping[str, object], field_name: str) -> list[object]:
    value = payload.get(field_name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContextAssemblyError(f"{field_name} must be a list.")
    return value


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContextAssemblyError(f"{field_name} items must be objects.")
    return value


def _require_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ContextAssemblyError(f"{field_name} must be a non-blank string.")
    return value.strip()


def _optional_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ContextAssemblyError(f"{field_name} must be a string.")
    return value.strip()


def _parse_runtime_scope(value: object) -> ContextPacketRuntimeScope:
    if value == "live" or value == "replay":
        return cast(ContextPacketRuntimeScope, value)
    raise ContextAssemblyError("runtime_scope must be live or replay.")


def _require_int(payload: Mapping[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContextAssemblyError(f"{field_name} must be a non-negative integer.")
    return value


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ContextAssemblyError(f"{field_name} must be an ISO datetime string.")
    try:
        return _normalize_datetime(datetime.fromisoformat(value))
    except ValueError as exc:
        raise ContextAssemblyError(f"{field_name} must be an ISO datetime string.") from exc


__all__ = [
    "CURRENT_MEMORY_READ_POLICY",
    "HISTORICAL_THESIS_SNAPSHOT_MEMORY_READ_POLICY",
    "ContextAssemblyError",
    "ContextItem",
    "ContextPacket",
    "ContextPacketStage",
    "ContextPacketRuntimeScope",
    "AnalysisWorkbench",
    "ReplayVisibilityAuditReport",
    "ResearchClaimCard",
    "ResearchClaimUsageReceipt",
    "VisibilityAudit",
    "build_replay_visibility_audit_report",
    "hash_context_packet",
    "parse_context_packet_payload",
    "serialize_context_packet",
    "validate_context_packet_visibility",
]
