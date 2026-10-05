"""Deterministic research-claim card selection for analysis context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, cast

from event_trader.research_memory import FileBackedClaimRegistry, ResearchClaim

ResearchClaimCardStatus = Literal["active", "contested"]

_POLICY_VERSION = "analysis_research_claim_cards_v1"
_DEFAULT_BUDGET = 12
_SELECTABLE_STATUSES = {"active", "contested"}


class ResearchClaimCardError(ValueError):
    """Raised when research claim-card selection inputs are malformed."""


@dataclass(frozen=True, slots=True)
class ResearchClaimCard:
    claim_id: str
    status: ResearchClaimCardStatus
    claim_text: str
    target_key: str
    page_path: str
    section_name: str
    supporting_event_ids: tuple[str, ...]
    contradicting_event_ids: tuple[str, ...]
    visible_from: datetime
    updated_at: datetime
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "visible_from", _normalize_datetime(self.visible_from))
        object.__setattr__(self, "updated_at", _normalize_datetime(self.updated_at))
        object.__setattr__(
            self,
            "supporting_event_ids",
            tuple(self.supporting_event_ids),
        )
        object.__setattr__(
            self,
            "contradicting_event_ids",
            tuple(self.contradicting_event_ids),
        )
        if self.status not in _SELECTABLE_STATUSES:
            raise ResearchClaimCardError("claim card status must be active or contested.")
        if not self.content_hash:
            object.__setattr__(self, "content_hash", _claim_card_hash(self))

    @property
    def scope(self) -> str:
        return f"target:{self.target_key}"

    def to_json_payload(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "status": self.status,
            "claim_text": self.claim_text,
            "target_key": self.target_key,
            "page_path": self.page_path,
            "section_name": self.section_name,
            "supporting_event_ids": list(self.supporting_event_ids),
            "contradicting_event_ids": list(self.contradicting_event_ids),
            "visible_from": self.visible_from.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> ResearchClaimCard:
        if not isinstance(payload, dict):
            raise ResearchClaimCardError("claim card payload must be an object.")
        return cls(
            claim_id=_require_text(payload, "claim_id"),
            status=_parse_card_status(_require_text(payload, "status")),
            claim_text=_require_text(payload, "claim_text"),
            target_key=_require_text(payload, "target_key"),
            page_path=_require_text(payload, "page_path"),
            section_name=_optional_text(payload.get("section_name")),
            supporting_event_ids=_text_tuple(payload.get("supporting_event_ids")),
            contradicting_event_ids=_text_tuple(payload.get("contradicting_event_ids")),
            visible_from=_parse_datetime(payload.get("visible_from"), "visible_from"),
            updated_at=_parse_datetime(payload.get("updated_at"), "updated_at"),
            content_hash=_optional_text(payload.get("content_hash")),
        )


@dataclass(frozen=True, slots=True)
class ResearchClaimUsageReceipt:
    candidate_count: int
    selected_count: int
    skipped_count: int
    skipped_status_counts: dict[str, int]
    selected_claim_ids: tuple[str, ...]
    cards_injected: tuple[str, ...]
    selection_policy_version: str = _POLICY_VERSION

    def to_json_payload(self) -> dict[str, object]:
        return {
            "candidate_count": self.candidate_count,
            "selected_count": self.selected_count,
            "skipped_count": self.skipped_count,
            "skipped_status_counts": dict(self.skipped_status_counts),
            "selected_claim_ids": list(self.selected_claim_ids),
            "cards_injected": list(self.cards_injected),
            "selection_policy_version": self.selection_policy_version,
        }

    @classmethod
    def from_json_payload(cls, payload: object) -> ResearchClaimUsageReceipt:
        if not isinstance(payload, dict):
            raise ResearchClaimCardError("claim usage receipt must be an object.")
        skipped = payload.get("skipped_status_counts")
        return cls(
            candidate_count=_require_int(payload, "candidate_count"),
            selected_count=_require_int(payload, "selected_count"),
            skipped_count=_require_int(payload, "skipped_count"),
            skipped_status_counts=(
                {str(key): int(value) for key, value in skipped.items()}
                if isinstance(skipped, dict)
                else {}
            ),
            selected_claim_ids=_text_tuple(payload.get("selected_claim_ids")),
            cards_injected=_text_tuple(payload.get("cards_injected")),
            selection_policy_version=_require_text(payload, "selection_policy_version"),
        )


def select_research_claim_cards(
    *,
    registry: FileBackedClaimRegistry,
    target_key: str,
    business_at: datetime,
    budget: int = _DEFAULT_BUDGET,
) -> tuple[tuple[ResearchClaimCard, ...], ResearchClaimUsageReceipt]:
    if budget < 0:
        raise ResearchClaimCardError("budget must be non-negative.")
    business_at = _normalize_datetime(business_at)
    candidates = registry.read_claims(scope_key=target_key, as_of=business_at)
    selected: list[ResearchClaimCard] = []
    skipped_status_counts: dict[str, int] = {}
    for claim in candidates:
        if claim.status not in _SELECTABLE_STATUSES:
            skipped_status_counts[claim.status] = (
                skipped_status_counts.get(claim.status, 0) + 1
            )
            continue
        selected.append(_claim_to_card(claim, target_key=target_key))
    selected.sort(
        key=lambda card: (
            0 if card.status == "active" else 1,
            -card.visible_from.timestamp(),
            card.claim_id,
        )
    )
    if len(selected) > budget:
        skipped_status_counts["over_budget"] = len(selected) - budget
        selected = selected[:budget]
    selected_ids = tuple(card.claim_id for card in selected)
    receipt = ResearchClaimUsageReceipt(
        candidate_count=len(candidates),
        selected_count=len(selected),
        skipped_count=sum(skipped_status_counts.values()),
        skipped_status_counts=skipped_status_counts,
        selected_claim_ids=selected_ids,
        cards_injected=selected_ids,
    )
    return tuple(selected), receipt


def _claim_to_card(claim: ResearchClaim, *, target_key: str) -> ResearchClaimCard:
    return ResearchClaimCard(
        claim_id=claim.claim_id,
        status=_parse_card_status(claim.status),
        claim_text=claim.claim_text,
        target_key=target_key,
        page_path=claim.page_path,
        section_name=claim.section_name,
        supporting_event_ids=claim.supporting_event_ids,
        contradicting_event_ids=claim.contradicting_event_ids,
        visible_from=claim.visible_from,
        updated_at=claim.updated_at,
    )


def _claim_card_hash(card: ResearchClaimCard) -> str:
    payload = {
        "claim_id": card.claim_id,
        "status": card.status,
        "claim_text": card.claim_text,
        "target_key": card.target_key,
        "page_path": card.page_path,
        "section_name": card.section_name,
        "supporting_event_ids": list(card.supporting_event_ids),
        "contradicting_event_ids": list(card.contradicting_event_ids),
        "visible_from": card.visible_from.isoformat(),
        "updated_at": card.updated_at.isoformat(),
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _parse_card_status(value: str) -> ResearchClaimCardStatus:
    if value == "active" or value == "contested":
        return cast(ResearchClaimCardStatus, value)
    raise ResearchClaimCardError("claim card status must be active or contested.")


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _require_text(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ResearchClaimCardError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _optional_text(value: object) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else ""


def _require_int(payload: dict[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ResearchClaimCardError(f"{field_name} must be a non-negative integer.")
    return value


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ResearchClaimCardError(f"{field_name} must be an ISO datetime string.")
    try:
        return _normalize_datetime(datetime.fromisoformat(value))
    except ValueError as exc:
        raise ResearchClaimCardError(
            f"{field_name} must be an ISO datetime string."
        ) from exc


def _normalize_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ResearchClaimCardError("timestamp must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ResearchClaimCardError("timestamp must be timezone-aware.")
    return value.astimezone(UTC)
