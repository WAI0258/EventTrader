"""Deterministic PMReview context and visibility-bounded read helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Generic, TypeVar

from event_trader.contracts import EvidenceLedgerRecord
from event_trader.contracts.view_state_change import MarketDataBar
from event_trader.learning_cards import (
    FileBackedLearningCardStore,
    LearningCard,
    select_learning_cards,
)
from event_trader.pm_review.contracts import (
    CandidateReviewAnchor,
    PMReviewRequest,
    PMReviewToolReadReceipt,
)
from event_trader.pm_review.store import CandidateReviewAnchorStore
from event_trader.portfolio.active_exposure import ActiveExposure
from event_trader.storage import WorkspaceLayout

_RecordT = TypeVar("_RecordT")
DEFAULT_PM_REVIEW_HISTORY_LIMIT = 20


class PMReviewContextError(ValueError):
    """Raised when PMReview context inputs violate visibility boundaries."""


@dataclass(frozen=True, slots=True)
class PMReviewBoundedRead(Generic[_RecordT]):
    records: tuple[_RecordT, ...]
    receipt: PMReviewToolReadReceipt


@dataclass(frozen=True, slots=True)
class PMReviewContext:
    """Exposure-aware PMReview context assembled behind deterministic read bounds."""

    request: PMReviewRequest
    evidence_records: tuple[EvidenceLedgerRecord, ...]
    market_bars: tuple[MarketDataBar, ...]
    prior_pm_review_requests: tuple[PMReviewRequest, ...]
    read_receipts: tuple[PMReviewToolReadReceipt, ...]
    learning_cards: tuple[LearningCard, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.request, PMReviewRequest):
            raise PMReviewContextError("request must be a PMReviewRequest instance.")
        for record in self.evidence_records:
            if not isinstance(record, EvidenceLedgerRecord):
                raise PMReviewContextError(
                    "evidence_records must contain EvidenceLedgerRecord values."
                )
            if record.target_key != self.request.target_key:
                raise PMReviewContextError("evidence target_key must match request.")
            if record.ts_event > self.request.decision_visibility.max_visible_event_time:
                raise PMReviewContextError("evidence exceeds max_visible_event_time.")
        for bar in self.market_bars:
            if not isinstance(bar, MarketDataBar):
                raise PMReviewContextError("market_bars must contain MarketDataBar values.")
            if bar.end_at > self.request.decision_visibility.max_visible_market_time:
                raise PMReviewContextError("market bar exceeds max_visible_market_time.")
        for prior_request in self.prior_pm_review_requests:
            if not isinstance(prior_request, PMReviewRequest):
                raise PMReviewContextError(
                    "prior_pm_review_requests must contain PMReviewRequest values."
                )
            if prior_request.target_key != self.request.target_key:
                raise PMReviewContextError("PM history target_key must match request.")
            if prior_request.business_at >= self.request.business_at:
                raise PMReviewContextError("PM history must be before request.business_at.")
        for card in self.learning_cards:
            if not isinstance(card, LearningCard):
                raise PMReviewContextError(
                    "learning_cards must contain LearningCard values."
                )
            if card.consumer_role != "pm_review":
                raise PMReviewContextError(
                    "PMReview context may only contain pm_review learning cards."
                )
            if card.scope_key not in {"shared", f"target:{self.request.target_key}"}:
                raise PMReviewContextError(
                    "PMReview learning card scope must match request target or shared."
                )
            if card.usable_from > self.request.business_at:
                raise PMReviewContextError(
                    "PMReview learning card exceeds request.business_at visibility."
                )
            if card.retired_at is not None and card.retired_at <= self.request.business_at:
                raise PMReviewContextError(
                    "retired PMReview learning card must not be active in context."
                )
        for receipt in self.read_receipts:
            if not isinstance(receipt, PMReviewToolReadReceipt):
                raise PMReviewContextError(
                    "read_receipts must contain PMReviewToolReadReceipt values."
                )
            if receipt.pm_review_request_id != self.request.request_id:
                raise PMReviewContextError("read receipt request id must match context.")
            if receipt.business_at != self.request.business_at:
                raise PMReviewContextError("read receipt business_at must match request.")


@dataclass(frozen=True, slots=True)
class CandidatePMReviewContext(PMReviewContext):
    candidate_anchor: CandidateReviewAnchor | None = None

    def __post_init__(self) -> None:
        PMReviewContext.__post_init__(self)
        validate_candidate_review_anchor_for_request(
            request=self.request,
            candidate_anchor=self.candidate_anchor,
        )


@dataclass(frozen=True, slots=True)
class OpenPositionPMReviewContext(PMReviewContext):
    actual_exposure: ActiveExposure | None = None
    current_exposure_required: bool = True

    def __post_init__(self) -> None:
        PMReviewContext.__post_init__(self)
        if self.current_exposure_required is not True:
            raise PMReviewContextError("open-position PMReview context requires exposure.")
        if not self.request.current_exposure_required:
            raise PMReviewContextError(
                "open-position PMReview context requires request.current_exposure_required."
            )
        if not isinstance(self.actual_exposure, ActiveExposure):
            raise PMReviewContextError(
                "open-position PMReview context requires resolved actual_exposure."
            )
        if self.actual_exposure.target_key != self.request.target_key:
            raise PMReviewContextError("actual exposure target_key must match request.")


def read_visible_evidence(
    *,
    request: PMReviewRequest,
    records: Iterable[EvidenceLedgerRecord],
    requested_event_ids: tuple[str, ...] = (),
    requested_start_at: datetime | None = None,
    requested_end_at: datetime | None = None,
) -> PMReviewBoundedRead[EvidenceLedgerRecord]:
    event_id_filter = set(requested_event_ids)
    bounded = tuple(
        sorted(
            (
                record
                for record in records
                if record.target_key == request.target_key
                and record.ts_event <= request.decision_visibility.max_visible_event_time
                and (not event_id_filter or record.event_id in event_id_filter)
                and _in_optional_window(
                    record.ts_event,
                    start_at=requested_start_at,
                    end_at=requested_end_at,
                )
            ),
            key=lambda record: (record.ts_event, record.ts_init, record.event_id),
        )
    )
    return PMReviewBoundedRead(
        records=bounded,
        receipt=_read_receipt(
            request=request,
            tool_name="read_evidence",
            requested_start_at=requested_start_at,
            requested_end_at=requested_end_at,
            enforced_max_visible_time=request.decision_visibility.max_visible_event_time,
            returned_record_ids=tuple(record.event_id for record in bounded),
            returned_times=tuple(record.ts_event for record in bounded),
        ),
    )


def load_candidate_review_anchor_for_request(
    *,
    layout: WorkspaceLayout,
    request: PMReviewRequest,
) -> CandidateReviewAnchor | None:
    """Load and validate the persisted candidate anchor for a candidate PMReview."""

    if not isinstance(layout, WorkspaceLayout):
        raise PMReviewContextError("layout must be a WorkspaceLayout instance.")
    if not isinstance(request, PMReviewRequest):
        raise PMReviewContextError("request must be a PMReviewRequest instance.")
    if request.candidate_anchor_id is None:
        if _request_requires_candidate_anchor(request):
            raise PMReviewContextError(
                "candidate PMReviewRequest requires candidate_anchor_id."
            )
        return None
    anchor = next(
        (
            persisted.record
            for persisted in CandidateReviewAnchorStore(layout).read_records(
                target_key=request.target_key
            )
            if persisted.record.anchor_id == request.candidate_anchor_id
        ),
        None,
    )
    if anchor is None:
        raise PMReviewContextError("candidate review anchor is missing.")
    validate_candidate_review_anchor_for_request(
        request=request,
        candidate_anchor=anchor,
    )
    return anchor


def validate_candidate_review_anchor_for_request(
    *,
    request: PMReviewRequest,
    candidate_anchor: CandidateReviewAnchor | None,
) -> None:
    if not isinstance(request, PMReviewRequest):
        raise PMReviewContextError("request must be a PMReviewRequest instance.")
    if candidate_anchor is None:
        if _request_requires_candidate_anchor(request):
            raise PMReviewContextError(
                "candidate PMReviewRequest requires candidate_anchor_id."
            )
        return
    if not isinstance(candidate_anchor, CandidateReviewAnchor):
        raise PMReviewContextError(
            "candidate_anchor must be a CandidateReviewAnchor when provided."
        )
    if request.candidate_anchor_id != candidate_anchor.anchor_id:
        raise PMReviewContextError("candidate anchor id must match request.")
    if candidate_anchor.target_key != request.target_key:
        raise PMReviewContextError("candidate anchor target_key must match request.")
    if not request.candidate_review_allowed:
        raise PMReviewContextError(
            "candidate_anchor requires candidate_review_allowed=true."
        )
    if candidate_anchor.expires_at <= request.business_at:
        raise PMReviewContextError("candidate review anchor is expired.")
    if candidate_anchor.source_assessment_id is not None:
        if candidate_anchor.source_assessment_id != request.source_assessment_id:
            raise PMReviewContextError(
                "candidate anchor source_assessment_id must match request."
            )
    if not set(candidate_anchor.source_event_ids).issubset(request.source_event_ids):
        raise PMReviewContextError(
            "candidate anchor source_event_ids must be visible to request."
        )


def _request_requires_candidate_anchor(request: PMReviewRequest) -> bool:
    return (
        request.source == "flat_candidate"
        or "flat_candidate_setup" in request.review_reasons
    )


def read_visible_market_bars(
    *,
    request: PMReviewRequest,
    bars: Iterable[MarketDataBar],
    requested_start_at: datetime | None = None,
    requested_end_at: datetime | None = None,
) -> PMReviewBoundedRead[MarketDataBar]:
    bounded = tuple(
        sorted(
            (
                bar
                for bar in bars
                if bar.end_at <= request.decision_visibility.max_visible_market_time
                and _bar_in_optional_window(
                    bar,
                    start_at=requested_start_at,
                    end_at=requested_end_at,
                )
            ),
            key=lambda bar: (bar.start_at, bar.end_at),
        )
    )
    return PMReviewBoundedRead(
        records=bounded,
        receipt=_read_receipt(
            request=request,
            tool_name="read_market_bars",
            requested_start_at=requested_start_at,
            requested_end_at=requested_end_at,
            enforced_max_visible_time=request.decision_visibility.max_visible_market_time,
            returned_record_ids=tuple(_market_bar_id(bar) for bar in bounded),
            returned_times=tuple(bar.end_at for bar in bounded),
        ),
    )


def read_visible_pm_history(
    *,
    request: PMReviewRequest,
    requests: Iterable[PMReviewRequest],
    requested_start_at: datetime | None = None,
    requested_end_at: datetime | None = None,
    limit: int | None = DEFAULT_PM_REVIEW_HISTORY_LIMIT,
) -> PMReviewBoundedRead[PMReviewRequest]:
    if limit is not None:
        _validate_positive_limit(limit, "limit")
    bounded_all = tuple(
        sorted(
            (
                item
                for item in requests
                if item.target_key == request.target_key
                and item.request_id != request.request_id
                and item.business_at < request.business_at
                and _in_optional_window(
                    item.business_at,
                    start_at=requested_start_at,
                    end_at=requested_end_at,
                )
            ),
            key=lambda item: (item.business_at, item.request_id),
        )
    )
    bounded = bounded_all if limit is None else bounded_all[-limit:]
    return PMReviewBoundedRead(
        records=bounded,
        receipt=_read_receipt(
            request=request,
            tool_name="read_pm_history",
            requested_start_at=requested_start_at,
            requested_end_at=requested_end_at,
            enforced_max_visible_time=request.business_at,
            returned_record_ids=tuple(item.request_id for item in bounded),
            returned_times=tuple(item.business_at for item in bounded),
        ),
    )


def _validate_positive_limit(value: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PMReviewContextError(f"{field_name} must be a positive integer or None.")


def read_visible_pm_review_learning_cards(
    *,
    request: PMReviewRequest,
    store: FileBackedLearningCardStore,
    budget: int | None = None,
) -> PMReviewBoundedRead[LearningCard]:
    records = select_learning_cards(
        store=store,
        consumer_role="pm_review",
        target_key=request.target_key,
        business_at=request.business_at,
        budget=budget,
    )
    return PMReviewBoundedRead(
        records=records,
        receipt=_read_receipt(
            request=request,
            tool_name="read_pm_review_learning_cards",
            requested_start_at=None,
            requested_end_at=None,
            enforced_max_visible_time=request.business_at,
            returned_record_ids=tuple(card.card_id for card in records),
            returned_times=tuple(card.usable_from for card in records),
        ),
    )


def _read_receipt(
    *,
    request: PMReviewRequest,
    tool_name: str,
    requested_start_at: datetime | None,
    requested_end_at: datetime | None,
    enforced_max_visible_time: datetime,
    returned_record_ids: tuple[str, ...],
    returned_times: tuple[datetime, ...],
) -> PMReviewToolReadReceipt:
    returned_start = min(returned_times) if returned_times else None
    returned_end = max(returned_times) if returned_times else None
    return PMReviewToolReadReceipt(
        read_id=_derive_read_id(
            request=request,
            tool_name=tool_name,
            requested_start_at=requested_start_at,
            requested_end_at=requested_end_at,
            returned_record_ids=returned_record_ids,
        ),
        pm_review_request_id=request.request_id,
        tool_name=tool_name,
        target_key=request.target_key,
        business_at=request.business_at,
        requested_start_at=requested_start_at,
        requested_end_at=requested_end_at,
        enforced_max_visible_time=enforced_max_visible_time,
        returned_record_ids=returned_record_ids,
        returned_time_range_start=returned_start,
        returned_time_range_end=returned_end,
        visibility_passed=all(
            returned_time <= enforced_max_visible_time
            for returned_time in returned_times
        ),
    )


def _derive_read_id(
    *,
    request: PMReviewRequest,
    tool_name: str,
    requested_start_at: datetime | None,
    requested_end_at: datetime | None,
    returned_record_ids: tuple[str, ...],
) -> str:
    material = "|".join(
        (
            request.request_id,
            tool_name,
            "" if requested_start_at is None else requested_start_at.isoformat(),
            "" if requested_end_at is None else requested_end_at.isoformat(),
            ",".join(returned_record_ids),
        )
    )
    return f"pm-review-read:{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _in_optional_window(
    value: datetime,
    *,
    start_at: datetime | None,
    end_at: datetime | None,
) -> bool:
    if start_at is not None and value < start_at:
        return False
    if end_at is not None and value > end_at:
        return False
    return True


def _bar_in_optional_window(
    bar: MarketDataBar,
    *,
    start_at: datetime | None,
    end_at: datetime | None,
) -> bool:
    if start_at is not None and bar.end_at <= start_at:
        return False
    if end_at is not None and bar.start_at >= end_at:
        return False
    return True


def _market_bar_id(bar: MarketDataBar) -> str:
    return f"bar:{bar.start_at.isoformat()}:{bar.end_at.isoformat()}"


__all__ = [
    "CandidatePMReviewContext",
    "DEFAULT_PM_REVIEW_HISTORY_LIMIT",
    "OpenPositionPMReviewContext",
    "PMReviewBoundedRead",
    "PMReviewContext",
    "PMReviewContextError",
    "load_candidate_review_anchor_for_request",
    "read_visible_evidence",
    "read_visible_market_bars",
    "read_visible_pm_history",
    "read_visible_pm_review_learning_cards",
    "validate_candidate_review_anchor_for_request",
]
