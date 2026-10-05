"""Deterministic receipts for committed ResearchMemory page writes."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from event_trader.contracts.ports import ResearchMemoryPageWritePort
from event_trader.contracts.research_memory import resolve_page_path, resolve_page_ref
from event_trader.research_memory.claim_registry import (
    FileBackedClaimRegistry,
    apply_claim_updates_for_write,
)
from event_trader.research_memory.page_writes import FileBackedResearchMemoryPageWriter
from event_trader.storage import WorkspaceLayout

ResearchMemoryWriteOperation = Literal[
    "create_page",
    "rewrite_page",
    "update_page_section",
]
ResearchMemoryWriteActorType = Literal["analysis", "reflection", "operator"]
ResearchMemoryClaimCoverageStatus = Literal["covered", "missing", "not_applicable"]


class ResearchMemoryWriteReceiptError(ValueError):
    """Raised when a ResearchMemory write receipt is malformed."""


@dataclass(frozen=True, slots=True)
class ResearchMemoryWriteAttribution:
    actor_type: ResearchMemoryWriteActorType
    actor_id: str
    business_at: datetime
    committed_at: datetime
    event_ids: tuple[str, ...] = ()
    context_packet_id: str | None = None
    context_packet_hash: str | None = None

    def __post_init__(self) -> None:
        if self.actor_type not in {"analysis", "reflection", "operator"}:
            raise ResearchMemoryWriteReceiptError(
                "actor_type must be analysis, reflection, or operator."
            )
        actor_id = self.actor_id.strip() if isinstance(self.actor_id, str) else ""
        if not actor_id:
            raise ResearchMemoryWriteReceiptError("actor_id must not be blank.")
        context_packet_id = _optional_stripped_text(self.context_packet_id)
        context_packet_hash = _optional_stripped_text(self.context_packet_hash)
        if self.actor_type == "analysis" and (
            context_packet_id is None or context_packet_hash is None
        ):
            raise ResearchMemoryWriteReceiptError(
                "analysis ResearchMemory writes require context packet linkage."
            )
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "business_at", _ensure_datetime(self.business_at))
        object.__setattr__(self, "committed_at", _ensure_datetime(self.committed_at))
        object.__setattr__(self, "event_ids", tuple(self.event_ids))
        object.__setattr__(self, "context_packet_id", context_packet_id)
        object.__setattr__(self, "context_packet_hash", context_packet_hash)


@dataclass(frozen=True, slots=True)
class ResearchMemoryWriteReceipt:
    receipt_id: str
    operation: ResearchMemoryWriteOperation
    page_path: str
    target_key: str | None
    section_name: str
    actor_type: ResearchMemoryWriteActorType
    actor_id: str
    business_at: datetime
    committed_at: datetime
    before_exists: bool
    after_exists: bool
    before_sha256: str
    after_sha256: str
    diff_sha256: str
    context_packet_id: str | None
    context_packet_hash: str | None
    event_ids: tuple[str, ...]
    changed_claim_ids: tuple[str, ...] = ()
    claim_coverage_status: ResearchMemoryClaimCoverageStatus = "not_applicable"
    claim_coverage_reason: str | None = None

    def __post_init__(self) -> None:
        page_ref = resolve_page_ref(self.page_path)
        if self.operation not in {
            "create_page",
            "rewrite_page",
            "update_page_section",
        }:
            raise ResearchMemoryWriteReceiptError(
                "operation must be create_page, rewrite_page, or update_page_section."
            )
        if page_ref.target_key != self.target_key:
            raise ResearchMemoryWriteReceiptError(
                "target_key must match the resolved page_path target."
            )
        object.__setattr__(self, "page_path", page_ref.page_path)
        object.__setattr__(self, "section_name", self.section_name.strip())
        object.__setattr__(self, "event_ids", tuple(self.event_ids))
        object.__setattr__(self, "changed_claim_ids", tuple(self.changed_claim_ids))
        if self.claim_coverage_status not in {
            "covered",
            "missing",
            "not_applicable",
        }:
            raise ResearchMemoryWriteReceiptError(
                "claim_coverage_status must be covered, missing, or not_applicable."
            )
        if self.claim_coverage_reason is not None:
            reason = self.claim_coverage_reason.strip()
            object.__setattr__(self, "claim_coverage_reason", reason or None)
        if self.actor_type not in {"analysis", "reflection", "operator"}:
            raise ResearchMemoryWriteReceiptError(
                "actor_type must be analysis, reflection, or operator."
            )
        actor_id = self.actor_id.strip() if isinstance(self.actor_id, str) else ""
        if not actor_id:
            raise ResearchMemoryWriteReceiptError("actor_id must not be blank.")
        context_packet_id = _optional_stripped_text(self.context_packet_id)
        context_packet_hash = _optional_stripped_text(self.context_packet_hash)
        if self.actor_type == "analysis" and (
            context_packet_id is None or context_packet_hash is None
        ):
            raise ResearchMemoryWriteReceiptError(
                "analysis ResearchMemory write receipts require context packet linkage."
            )
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "context_packet_id", context_packet_id)
        object.__setattr__(self, "context_packet_hash", context_packet_hash)
        object.__setattr__(self, "business_at", _ensure_datetime(self.business_at))
        object.__setattr__(self, "committed_at", _ensure_datetime(self.committed_at))
        for field_name in (
            "receipt_id",
            "diff_sha256",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ResearchMemoryWriteReceiptError(
                    f"{field_name} must be a non-empty string."
                )
        for field_name in ("before_sha256", "after_sha256"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ResearchMemoryWriteReceiptError(
                    f"{field_name} must be a string."
                )
            if value and len(value) != 64:
                raise ResearchMemoryWriteReceiptError(
                    f"{field_name} must be a sha256 digest when present."
                )

    @property
    def scope_key(self) -> str:
        return self.target_key or "shared"

    def to_json_payload(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "operation": self.operation,
            "page_path": self.page_path,
            "target_key": self.target_key,
            "section_name": self.section_name,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "business_at": self.business_at.isoformat(),
            "committed_at": self.committed_at.isoformat(),
            "before_exists": self.before_exists,
            "after_exists": self.after_exists,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "diff_sha256": self.diff_sha256,
            "context_packet_id": self.context_packet_id,
            "context_packet_hash": self.context_packet_hash,
            "event_ids": list(self.event_ids),
            "changed_claim_ids": list(self.changed_claim_ids),
            "claim_coverage_status": self.claim_coverage_status,
            "claim_coverage_reason": self.claim_coverage_reason,
        }


class ReceiptedResearchMemoryPageWriter(ResearchMemoryPageWritePort):
    """Wrap canonical page writes with durable diff receipts."""

    def __init__(
        self,
        *,
        layout: WorkspaceLayout,
        attribution: ResearchMemoryWriteAttribution,
        page_writer: ResearchMemoryPageWritePort | None = None,
        receipt_store: FileBackedResearchMemoryWriteReceiptStore | None = None,
        claim_registry: FileBackedClaimRegistry | None = None,
    ) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise ResearchMemoryWriteReceiptError(
                "layout must be a WorkspaceLayout instance."
            )
        if not isinstance(attribution, ResearchMemoryWriteAttribution):
            raise ResearchMemoryWriteReceiptError(
                "attribution must be a ResearchMemoryWriteAttribution instance."
            )
        self._layout = layout
        self._attribution = attribution
        self._page_writer = page_writer or FileBackedResearchMemoryPageWriter(layout)
        self._receipt_store = receipt_store or FileBackedResearchMemoryWriteReceiptStore(
            layout
        )
        self._claim_registry = claim_registry or FileBackedClaimRegistry(layout)
        self._receipt_ids: list[str] = []
        self._receipts: list[ResearchMemoryWriteReceipt] = []

    @property
    def receipt_ids(self) -> tuple[str, ...]:
        return tuple(self._receipt_ids)

    @property
    def receipts(self) -> tuple[ResearchMemoryWriteReceipt, ...]:
        return tuple(self._receipts)

    def update_page_section(
        self,
        page_path: str,
        section_name: str,
        new_content_md: str,
    ) -> Path:
        before_content = _read_existing_page_content(self._layout, page_path)
        written_path = self._page_writer.update_page_section(
            page_path,
            section_name,
            new_content_md,
        )
        self._append_receipt(
            operation="update_page_section",
            page_path=page_path,
            section_name=section_name,
            before_content_md=before_content,
            claim_content_md=new_content_md,
        )
        return written_path

    def rewrite_page(self, page_path: str, new_content_md: str) -> Path:
        before_content = _read_existing_page_content(self._layout, page_path)
        written_path = self._page_writer.rewrite_page(page_path, new_content_md)
        self._append_receipt(
            operation="rewrite_page",
            page_path=page_path,
            section_name="",
            before_content_md=before_content,
            claim_content_md=None,
        )
        return written_path

    def create_page(self, page_path: str, initial_content_md: str) -> Path:
        before_content = _read_optional_page_content(self._layout, page_path)
        written_path = self._page_writer.create_page(page_path, initial_content_md)
        self._append_receipt(
            operation="create_page",
            page_path=page_path,
            section_name="",
            before_content_md=before_content,
            claim_content_md=None,
        )
        return written_path

    def _append_receipt(
        self,
        *,
        operation: ResearchMemoryWriteOperation,
        page_path: str,
        section_name: str,
        before_content_md: str | None,
        claim_content_md: str | None,
    ) -> None:
        after_content = _read_existing_page_content(self._layout, page_path)
        if before_content_md is not None and before_content_md == after_content:
            return
        receipt = build_research_memory_write_receipt(
            operation=operation,
            page_path=page_path,
            section_name=section_name,
            attribution=self._attribution,
            before_content_md=before_content_md,
            after_content_md=after_content,
        )
        changed_claim_ids = apply_claim_updates_for_write(
            registry=self._claim_registry,
            page_path=page_path,
            section_name=section_name,
            content_md=claim_content_md if claim_content_md is not None else after_content,
            source_write_receipt_id=receipt.receipt_id,
            business_at=receipt.business_at,
            committed_at=receipt.committed_at,
        )
        claim_coverage_status, claim_coverage_reason = _claim_coverage_for_write(
            page_path=page_path,
            section_name=section_name,
            changed_claim_ids=changed_claim_ids,
        )
        receipt = replace(
            receipt,
            changed_claim_ids=changed_claim_ids,
            claim_coverage_status=claim_coverage_status,
            claim_coverage_reason=claim_coverage_reason,
        )
        self._receipt_ids.append(self._receipt_store.append_receipt(receipt))
        self._receipts.append(receipt)


class FileBackedResearchMemoryWriteReceiptStore:
    """Append-only JSONL store for ResearchMemory write receipts."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise ResearchMemoryWriteReceiptError(
                "layout must be a WorkspaceLayout instance."
            )
        self._layout = layout

    def append_receipt(self, receipt: ResearchMemoryWriteReceipt) -> str:
        path = self.receipt_path(
            scope_key=receipt.scope_key,
            year_month=receipt.business_at.strftime("%Y-%m"),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if _jsonl_record_exists(path, receipt_id=receipt.receipt_id):
            return receipt.receipt_id
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(receipt.to_json_payload(), ensure_ascii=False))
                handle.write("\n")
        except OSError as exc:
            raise ResearchMemoryWriteReceiptError(
                "Failed to append ResearchMemory write receipt."
            ) from exc
        return receipt.receipt_id

    def read_receipts(
        self,
        *,
        scope_key: str,
        year_month: str,
    ) -> tuple[dict[str, object], ...]:
        path = self.receipt_path(scope_key=scope_key, year_month=year_month)
        if not path.exists():
            return ()
        receipts: list[dict[str, object]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                normalized = line.strip()
                if not normalized:
                    continue
                payload = json.loads(normalized)
                if not isinstance(payload, dict):
                    raise ResearchMemoryWriteReceiptError(
                        "ResearchMemory write receipt entries must be JSON objects."
                    )
                receipts.append(payload)
        return tuple(receipts)

    def receipt_path(self, *, scope_key: str, year_month: str) -> Path:
        normalized_scope = scope_key.strip() if isinstance(scope_key, str) else ""
        if not normalized_scope:
            raise ResearchMemoryWriteReceiptError("scope_key must not be blank.")
        normalized_month = year_month.strip() if isinstance(year_month, str) else ""
        if not normalized_month:
            raise ResearchMemoryWriteReceiptError("year_month must not be blank.")
        return (
            self._layout.runtime_root
            / "research_memory_writes"
            / normalized_scope
            / f"{normalized_month}.jsonl"
        ).resolve(strict=False)


def parse_research_memory_write_receipt(
    payload: dict[str, object],
) -> ResearchMemoryWriteReceipt:
    if not isinstance(payload, dict):
        raise ResearchMemoryWriteReceiptError(
            "ResearchMemory write receipt payload must be a JSON object."
        )
    return ResearchMemoryWriteReceipt(
        receipt_id=_required_text(payload, "receipt_id"),
        operation=_required_text(payload, "operation"),
        page_path=_required_text(payload, "page_path"),
        target_key=_optional_text(payload.get("target_key")),
        section_name=_required_text_allow_empty(payload, "section_name"),
        actor_type=_required_text(payload, "actor_type"),
        actor_id=_required_text(payload, "actor_id"),
        business_at=_required_datetime(payload, "business_at"),
        committed_at=_required_datetime(payload, "committed_at"),
        before_exists=_required_bool(payload, "before_exists"),
        after_exists=_required_bool(payload, "after_exists"),
        before_sha256=_required_text_allow_empty(payload, "before_sha256"),
        after_sha256=_required_text_allow_empty(payload, "after_sha256"),
        diff_sha256=_required_text(payload, "diff_sha256"),
        context_packet_id=_optional_text(payload.get("context_packet_id")),
        context_packet_hash=_optional_text(payload.get("context_packet_hash")),
        event_ids=tuple(_required_string_list(payload, "event_ids")),
        changed_claim_ids=tuple(_required_string_list(payload, "changed_claim_ids")),
        claim_coverage_status=_required_text(payload, "claim_coverage_status"),
        claim_coverage_reason=_optional_text(payload.get("claim_coverage_reason")),
    )


def build_research_memory_write_receipt(
    *,
    operation: ResearchMemoryWriteOperation,
    page_path: str,
    section_name: str,
    attribution: ResearchMemoryWriteAttribution,
    before_content_md: str | None,
    after_content_md: str | None,
    changed_claim_ids: tuple[str, ...] = (),
    claim_coverage_status: ResearchMemoryClaimCoverageStatus = "not_applicable",
    claim_coverage_reason: str | None = None,
) -> ResearchMemoryWriteReceipt:
    page_ref = resolve_page_ref(page_path)
    before = before_content_md or ""
    after = after_content_md or ""
    before_sha = _content_sha256(before) if before_content_md is not None else ""
    after_sha = _content_sha256(after) if after_content_md is not None else ""
    diff_sha = _content_sha256(_unified_diff(before=before, after=after))
    receipt_id = _receipt_id(
        operation=operation,
        page_path=page_ref.page_path,
        section_name=section_name,
        attribution=attribution,
        after_sha256=after_sha,
    )
    return ResearchMemoryWriteReceipt(
        receipt_id=receipt_id,
        operation=operation,
        page_path=page_ref.page_path,
        target_key=page_ref.target_key,
        section_name=section_name,
        actor_type=attribution.actor_type,
        actor_id=attribution.actor_id,
        business_at=attribution.business_at,
        committed_at=attribution.committed_at,
        before_exists=before_content_md is not None,
        after_exists=after_content_md is not None,
        before_sha256=before_sha,
        after_sha256=after_sha,
        diff_sha256=diff_sha,
        context_packet_id=attribution.context_packet_id,
        context_packet_hash=attribution.context_packet_hash,
        event_ids=attribution.event_ids,
        changed_claim_ids=changed_claim_ids,
        claim_coverage_status=claim_coverage_status,
        claim_coverage_reason=claim_coverage_reason,
    )


def _claim_coverage_for_write(
    *,
    page_path: str,
    section_name: str,
    changed_claim_ids: tuple[str, ...],
) -> tuple[ResearchMemoryClaimCoverageStatus, str]:
    if changed_claim_ids:
        return "covered", "write updated one or more canonical research-claim blocks"
    if _is_durable_claim_surface(page_path=page_path, section_name=section_name):
        return (
            "missing",
            "durable thesis/risk/invalidation write did not update a canonical "
            "research-claim block",
        )
    return "not_applicable", "write target is not a durable claim surface"


def _is_durable_claim_surface(*, page_path: str, section_name: str) -> bool:
    page_ref = resolve_page_ref(page_path)
    leaf_name = Path(page_ref.page_path).name.lower()
    if leaf_name in {"thesis.md", "risks.md"}:
        return True
    normalized_section = " ".join(section_name.lower().split())
    durable_terms = (
        "thesis",
        "current view",
        "why now",
        "key evidence",
        "invalidation",
        "risk",
        "failure condition",
        "contradictory evidence",
    )
    return any(term in normalized_section for term in durable_terms)


def _required_text(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ResearchMemoryWriteReceiptError(f"{field_name} must be a non-blank string.")
    return value


def _required_text_allow_empty(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise ResearchMemoryWriteReceiptError(f"{field_name} must be a string.")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResearchMemoryWriteReceiptError("optional text field must be a string.")
    return value or None


def _required_bool(payload: dict[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise ResearchMemoryWriteReceiptError(f"{field_name} must be a boolean.")
    return value


def _required_datetime(payload: dict[str, object], field_name: str) -> datetime:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise ResearchMemoryWriteReceiptError(f"{field_name} must be an ISO timestamp.")
    try:
        return _ensure_datetime(datetime.fromisoformat(value))
    except ValueError as exc:
        raise ResearchMemoryWriteReceiptError(
            f"{field_name} must be an ISO timestamp."
        ) from exc


def _required_string_list(payload: dict[str, object], field_name: str) -> list[str]:
    value = payload.get(field_name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ResearchMemoryWriteReceiptError(f"{field_name} must be a list of strings.")
    return value


def _receipt_id(
    *,
    operation: str,
    page_path: str,
    section_name: str,
    attribution: ResearchMemoryWriteAttribution,
    after_sha256: str,
) -> str:
    identity = {
        "operation": operation,
        "page_path": page_path,
        "section_name": section_name,
        "actor_type": attribution.actor_type,
        "actor_id": attribution.actor_id,
        "business_at": attribution.business_at.isoformat(),
        "context_packet_id": attribution.context_packet_id,
        "context_packet_hash": attribution.context_packet_hash,
        "event_ids": list(attribution.event_ids),
        "after_sha256": after_sha256,
    }
    digest = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"research-memory-write:{digest}"


def _content_sha256(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _read_existing_page_content(layout: WorkspaceLayout, page_path: str) -> str:
    content = _read_optional_page_content(layout, page_path)
    if content is None:
        raise ResearchMemoryWriteReceiptError(
            f"Expected ResearchMemory page to exist before rewrite: {page_path}"
        )
    return content


def _read_optional_page_content(layout: WorkspaceLayout, page_path: str) -> str | None:
    resolved = resolve_page_path(layout, page_path)
    if not resolved.exists():
        return None
    return resolved.read_text(encoding="utf-8")


def _ensure_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ResearchMemoryWriteReceiptError("timestamp fields must be datetimes.")
    if value.tzinfo is None:
        raise ResearchMemoryWriteReceiptError("timestamp fields must be timezone-aware.")
    return value.astimezone(UTC)


def _optional_stripped_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip() if isinstance(value, str) else ""
    return normalized or None


def _unified_diff(*, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="before.md",
            tofile="after.md",
            lineterm="",
        )
    )


def _jsonl_record_exists(path: Path, *, receipt_id: str) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                normalized = line.strip()
                if not normalized:
                    continue
                try:
                    payload = json.loads(normalized)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("receipt_id") == receipt_id:
                    return True
    except OSError as exc:
        raise ResearchMemoryWriteReceiptError(
            "Failed to read existing ResearchMemory write receipts."
        ) from exc
    return False
