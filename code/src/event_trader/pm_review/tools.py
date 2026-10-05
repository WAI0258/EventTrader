"""Callable PMReview tool boundary with deterministic visibility enforcement."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Generic, TypeVar

from event_trader.contracts import EvidenceLedgerRecord
from event_trader.contracts.view_state_change import MarketDataBar
from event_trader.episode_memory.projection import (
    PMEpisodeMemoryView,
    build_pm_episode_memory_view_as_of,
)
from event_trader.episode_memory.store import FileBackedEpisodeMemoryStore
from event_trader.learning_cards import LearningCard
from event_trader.pm_review.contracts import (
    CandidateReviewAnchor,
    PMReviewEpisodeMemoryReadReceipt,
    PMReviewRequest,
    PMReviewToolReadReceipt,
)
from event_trader.pm_review.context import (
    DEFAULT_PM_REVIEW_HISTORY_LIMIT,
    validate_candidate_review_anchor_for_request,
)
from event_trader.pm_review.store import (
    PMReviewEpisodeMemoryReadReceiptStore,
    PMReviewToolReadReceiptStore,
)
from event_trader.portfolio.active_exposure import ActiveExposure
from event_trader.portfolio.contracts import PMDecision
from event_trader.storage import WorkspaceLayout

_RecordT = TypeVar("_RecordT")


class PMReviewToolError(ValueError):
    """Raised when a PMReview tool call is invalid or visibility-unsafe."""


@dataclass(frozen=True, slots=True)
class PMReviewToolResult(Generic[_RecordT]):
    """One PMReview tool result plus its persisted read receipt."""

    records: tuple[_RecordT, ...]
    receipt: PMReviewToolReadReceipt


@dataclass(frozen=True, slots=True)
class PMReviewHistory:
    """Bounded PM lifecycle history visible to one PMReview request."""

    requests: tuple[PMReviewRequest, ...]
    pm_decisions: tuple[PMDecision, ...]


@dataclass(frozen=True, slots=True)
class PMReviewToolSession:
    """Inputs bound to one future PMReview MCP/tool session."""

    session_id: str
    layout: WorkspaceLayout
    request: PMReviewRequest
    active_exposure: ActiveExposure
    evidence_records: tuple[EvidenceLedgerRecord, ...] = ()
    market_bars: tuple[MarketDataBar, ...] = ()
    pm_review_requests: tuple[PMReviewRequest, ...] = ()
    pm_decisions: tuple[PMDecision, ...] = ()
    learning_cards: tuple[LearningCard, ...] = ()
    candidate_anchor: CandidateReviewAnchor | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise PMReviewToolError("session_id must be a non-blank string.")
        object.__setattr__(self, "session_id", self.session_id.strip())
        if not isinstance(self.layout, WorkspaceLayout):
            raise PMReviewToolError("layout must be a WorkspaceLayout instance.")
        if not isinstance(self.request, PMReviewRequest):
            raise PMReviewToolError("request must be a PMReviewRequest instance.")
        if not isinstance(self.active_exposure, ActiveExposure):
            raise PMReviewToolError("active_exposure must be an ActiveExposure instance.")
        if self.active_exposure.target_key != self.request.target_key:
            raise PMReviewToolError("active_exposure target_key must match request.")
        _validate_tuple(
            self.evidence_records,
            EvidenceLedgerRecord,
            "evidence_records",
        )
        _validate_tuple(self.market_bars, MarketDataBar, "market_bars")
        _validate_tuple(
            self.pm_review_requests,
            PMReviewRequest,
            "pm_review_requests",
        )
        _validate_tuple(self.pm_decisions, PMDecision, "pm_decisions")
        _validate_tuple(self.learning_cards, LearningCard, "learning_cards")
        for card in self.learning_cards:
            if card.consumer_role != "pm_review":
                raise PMReviewToolError(
                    "PMReview tool session may only contain pm_review learning cards."
                )
            if card.scope_key not in {"shared", f"target:{self.request.target_key}"}:
                raise PMReviewToolError(
                    "PMReview learning card scope must match request target or shared."
                )
            if card.usable_from > self.request.business_at:
                raise PMReviewToolError(
                    "PMReview learning card exceeds request.business_at visibility."
                )
            if card.retired_at is not None and card.retired_at <= self.request.business_at:
                raise PMReviewToolError(
                    "retired PMReview learning card must not be active in session."
                )
        validate_candidate_review_anchor_for_request(
            request=self.request,
            candidate_anchor=self.candidate_anchor,
        )


def read_pm_review_request(
    *,
    session: PMReviewToolSession,
    session_id: str,
    pm_review_request_id: str,
) -> PMReviewToolResult[PMReviewRequest]:
    """Return the PMReviewRequest bound to this tool session."""

    _validate_call(
        session=session,
        session_id=session_id,
        pm_review_request_id=pm_review_request_id,
    )
    receipt = _persist_receipt(
        session=session,
        tool_name="read_pm_review_request",
        requested_start_at=None,
        requested_end_at=None,
        enforced_max_visible_time=session.request.business_at,
        returned_record_ids=(session.request.request_id,),
        returned_times=(session.request.business_at,),
    )
    return PMReviewToolResult(records=(session.request,), receipt=receipt)


def read_pm_review_evidence(
    *,
    session: PMReviewToolSession,
    session_id: str,
    pm_review_request_id: str,
    event_ids: tuple[str, ...] = (),
    requested_start_at: datetime | None = None,
    requested_end_at: datetime | None = None,
) -> PMReviewToolResult[EvidenceLedgerRecord]:
    """Read only evidence visible at request.max_visible_event_time."""

    _validate_call(
        session=session,
        session_id=session_id,
        pm_review_request_id=pm_review_request_id,
    )
    event_filter = set(event_ids)
    records = tuple(
        sorted(
            (
                record
                for record in session.evidence_records
                if record.target_key == session.request.target_key
                and record.ts_event
                <= session.request.decision_visibility.max_visible_event_time
                and (not event_filter or record.event_id in event_filter)
                and _in_optional_window(
                    record.ts_event,
                    start_at=requested_start_at,
                    end_at=requested_end_at,
                )
            ),
            key=lambda record: (record.ts_event, record.ts_init, record.event_id),
        )
    )
    receipt = _persist_receipt(
        session=session,
        tool_name="read_pm_review_evidence",
        requested_start_at=requested_start_at,
        requested_end_at=requested_end_at,
        enforced_max_visible_time=session.request.decision_visibility.max_visible_event_time,
        returned_record_ids=tuple(record.event_id for record in records),
        returned_times=tuple(record.ts_event for record in records),
    )
    return PMReviewToolResult(records=records, receipt=receipt)


def read_pm_review_market_bars(
    *,
    session: PMReviewToolSession,
    session_id: str,
    pm_review_request_id: str,
    requested_start_at: datetime | None = None,
    requested_end_at: datetime | None = None,
) -> PMReviewToolResult[MarketDataBar]:
    """Read only market bars visible at request.max_visible_market_time."""

    _validate_call(
        session=session,
        session_id=session_id,
        pm_review_request_id=pm_review_request_id,
    )
    records = tuple(
        sorted(
            (
                bar
                for bar in session.market_bars
                if bar.end_at
                <= session.request.decision_visibility.max_visible_market_time
                and _bar_in_optional_window(
                    bar,
                    start_at=requested_start_at,
                    end_at=requested_end_at,
                )
            ),
            key=lambda bar: (bar.start_at, bar.end_at),
        )
    )
    receipt = _persist_receipt(
        session=session,
        tool_name="read_pm_review_market_bars",
        requested_start_at=requested_start_at,
        requested_end_at=requested_end_at,
        enforced_max_visible_time=session.request.decision_visibility.max_visible_market_time,
        returned_record_ids=tuple(_market_bar_id(bar) for bar in records),
        returned_times=tuple(bar.end_at for bar in records),
    )
    return PMReviewToolResult(records=records, receipt=receipt)


def read_pm_review_history(
    *,
    session: PMReviewToolSession,
    session_id: str,
    pm_review_request_id: str,
    requested_start_at: datetime | None = None,
    requested_end_at: datetime | None = None,
    limit: int | None = DEFAULT_PM_REVIEW_HISTORY_LIMIT,
) -> PMReviewToolResult[PMReviewHistory]:
    """Read prior PM lifecycle records only before request.business_at."""

    _validate_call(
        session=session,
        session_id=session_id,
        pm_review_request_id=pm_review_request_id,
    )
    if limit is not None:
        _validate_history_limit(limit)
    requests_all = tuple(
        sorted(
            (
                request
                for request in session.pm_review_requests
                if request.target_key == session.request.target_key
                and request.request_id != session.request.request_id
                and request.business_at < session.request.business_at
                and _in_optional_window(
                    request.business_at,
                    start_at=requested_start_at,
                    end_at=requested_end_at,
                )
            ),
            key=lambda request: (request.business_at, request.request_id),
        )
    )
    requests = requests_all if limit is None else requests_all[-limit:]
    pm_decisions_all = tuple(
        sorted(
            (
                decision
                for decision in session.pm_decisions
                if decision.target_key == session.request.target_key
                and decision.business_at < session.request.business_at
                and _in_optional_window(
                    decision.business_at,
                    start_at=requested_start_at,
                    end_at=requested_end_at,
                )
            ),
            key=lambda decision: (decision.business_at, decision.decision_id),
        )
    )
    pm_decisions = pm_decisions_all if limit is None else pm_decisions_all[-limit:]
    returned_ids = (
        *(request.request_id for request in requests),
        *(decision.decision_id for decision in pm_decisions),
    )
    returned_times = (
        *(request.business_at for request in requests),
        *(decision.business_at for decision in pm_decisions),
    )
    receipt = _persist_receipt(
        session=session,
        tool_name="read_pm_review_history",
        requested_start_at=requested_start_at,
        requested_end_at=requested_end_at,
        enforced_max_visible_time=session.request.business_at,
        returned_record_ids=tuple(returned_ids),
        returned_times=tuple(returned_times),
    )
    return PMReviewToolResult(
        records=(
            PMReviewHistory(
                requests=requests,
                pm_decisions=pm_decisions,
            ),
        ),
        receipt=receipt,
    )


def _validate_history_limit(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PMReviewToolError("limit must be a positive integer or None.")


def read_pm_review_learning_cards(
    *,
    session: PMReviewToolSession,
    session_id: str,
    pm_review_request_id: str,
) -> PMReviewToolResult[LearningCard]:
    """Read structured pm_review learning cards selected for this request."""

    _validate_call(
        session=session,
        session_id=session_id,
        pm_review_request_id=pm_review_request_id,
    )
    records = tuple(
        sorted(
            session.learning_cards,
            key=lambda card: (card.usable_from, -card.confidence, card.card_id),
        )
    )
    receipt = _persist_receipt(
        session=session,
        tool_name="read_pm_review_learning_cards",
        requested_start_at=None,
        requested_end_at=None,
        enforced_max_visible_time=session.request.business_at,
        returned_record_ids=tuple(card.card_id for card in records),
        returned_times=tuple(card.usable_from for card in records),
    )
    return PMReviewToolResult(records=records, receipt=receipt)


def read_pm_episode_memory(
    *,
    session: PMReviewToolSession,
    session_id: str,
    pm_review_request_id: str,
) -> PMReviewToolResult[PMEpisodeMemoryView]:
    """Read PM-facing episode memory as of request.business_at."""

    _validate_call(
        session=session,
        session_id=session_id,
        pm_review_request_id=pm_review_request_id,
    )
    request = session.request
    if request.source_episode_id is None:
        _persist_episode_memory_read_receipt(
            session=session,
            receipt=PMReviewEpisodeMemoryReadReceipt(
                request_id=request.request_id,
                target_key=request.target_key,
                business_at=request.business_at,
                source_episode_id=None,
                projection_path=None,
                projection_hash=None,
                built_from_delta_ids=(),
                built_from_delta_hashes=(),
                source_visible_through_max=None,
                usable_from_max=None,
                excluded_delta_ids=(),
                excluded_delta_reasons=(),
                status="skipped",
                failure_reason=None,
            ),
        )
        receipt = _persist_receipt(
            session=session,
            tool_name="read_pm_episode_memory",
            requested_start_at=None,
            requested_end_at=None,
            enforced_max_visible_time=request.business_at,
            returned_record_ids=(),
            returned_times=(),
        )
        return PMReviewToolResult(records=(), receipt=receipt)

    try:
        deltas = tuple(
            persisted.record
            for persisted in FileBackedEpisodeMemoryStore(session.layout).read_deltas(
                target_key=request.target_key,
                episode_id=request.source_episode_id,
            )
        )
        view = build_pm_episode_memory_view_as_of(
            target_key=request.target_key,
            episode_id=request.source_episode_id,
            business_at=request.business_at,
            deltas=deltas,
        )
        projection_path = _write_pm_episode_memory_projection(
            session=session,
            view=view,
        )
        detailed_receipt = PMReviewEpisodeMemoryReadReceipt(
            request_id=request.request_id,
            target_key=request.target_key,
            business_at=request.business_at,
            source_episode_id=request.source_episode_id,
            projection_path=str(projection_path),
            projection_hash=view.projection_hash,
            built_from_delta_ids=view.built_from_delta_ids,
            built_from_delta_hashes=view.built_from_delta_hashes,
            source_visible_through_max=view.source_visible_through_max,
            usable_from_max=view.usable_from_max,
            excluded_delta_ids=view.excluded_delta_ids,
            excluded_delta_reasons=view.excluded_delta_reasons,
            status="read" if view.built_from_delta_ids else "empty",
            failure_reason=None,
        )
        _persist_episode_memory_read_receipt(
            session=session,
            receipt=detailed_receipt,
        )
    except Exception as exc:
        _persist_episode_memory_read_receipt(
            session=session,
            receipt=PMReviewEpisodeMemoryReadReceipt(
                request_id=request.request_id,
                target_key=request.target_key,
                business_at=request.business_at,
                source_episode_id=request.source_episode_id,
                projection_path=None,
                projection_hash=None,
                built_from_delta_ids=(),
                built_from_delta_hashes=(),
                source_visible_through_max=None,
                usable_from_max=None,
                excluded_delta_ids=(),
                excluded_delta_reasons=(),
                status="failed",
                failure_reason=str(exc).strip() or repr(exc),
            ),
        )
        raise PMReviewToolError(
            "failed to read PM episode memory projection."
        ) from exc

    receipt = _persist_receipt(
        session=session,
        tool_name="read_pm_episode_memory",
        requested_start_at=None,
        requested_end_at=None,
        enforced_max_visible_time=request.business_at,
        returned_record_ids=view.built_from_delta_ids,
        returned_times=((view.usable_from_max,) if view.built_from_delta_ids else ()),
    )
    return PMReviewToolResult(records=(view,), receipt=receipt)


def read_pm_review_active_exposure(
    *,
    session: PMReviewToolSession,
    session_id: str,
    pm_review_request_id: str,
) -> PMReviewToolResult[ActiveExposure]:
    """Return the supplied/resolved ActiveExposure for this request."""

    _validate_call(
        session=session,
        session_id=session_id,
        pm_review_request_id=pm_review_request_id,
    )
    returned_ids = _active_exposure_source_ids(session.active_exposure)
    receipt = _persist_receipt(
        session=session,
        tool_name="read_pm_review_active_exposure",
        requested_start_at=None,
        requested_end_at=None,
        enforced_max_visible_time=session.request.business_at,
        returned_record_ids=returned_ids,
        returned_times=(
            (
                session.active_exposure.source_updated_at
                if session.active_exposure.source_updated_at is not None
                else session.request.business_at
            ),
        ),
    )
    return PMReviewToolResult(records=(session.active_exposure,), receipt=receipt)


def _validate_call(
    *,
    session: PMReviewToolSession,
    session_id: str,
    pm_review_request_id: str,
) -> None:
    if not isinstance(session, PMReviewToolSession):
        raise PMReviewToolError("session must be a PMReviewToolSession instance.")
    if session_id != session.session_id:
        raise PMReviewToolError("PMReview tool session_id does not match.")
    if pm_review_request_id != session.request.request_id:
        raise PMReviewToolError("PMReview tool request id does not match.")


def _persist_receipt(
    *,
    session: PMReviewToolSession,
    tool_name: str,
    requested_start_at: datetime | None,
    requested_end_at: datetime | None,
    enforced_max_visible_time: datetime,
    returned_record_ids: tuple[str, ...],
    returned_times: tuple[datetime, ...],
) -> PMReviewToolReadReceipt:
    returned_start = min(returned_times) if returned_times else None
    returned_end = max(returned_times) if returned_times else None
    receipt = PMReviewToolReadReceipt(
        read_id=_derive_read_id(
            session=session,
            tool_name=tool_name,
            requested_start_at=requested_start_at,
            requested_end_at=requested_end_at,
            returned_record_ids=returned_record_ids,
        ),
        pm_review_request_id=session.request.request_id,
        tool_name=tool_name,
        target_key=session.request.target_key,
        business_at=session.request.business_at,
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
    PMReviewToolReadReceiptStore(session.layout).append(receipt)
    return receipt


def _persist_episode_memory_read_receipt(
    *,
    session: PMReviewToolSession,
    receipt: PMReviewEpisodeMemoryReadReceipt,
) -> None:
    PMReviewEpisodeMemoryReadReceiptStore(session.layout).append(receipt)


def _derive_read_id(
    *,
    session: PMReviewToolSession,
    tool_name: str,
    requested_start_at: datetime | None,
    requested_end_at: datetime | None,
    returned_record_ids: tuple[str, ...],
) -> str:
    material = {
        "session_id": session.session_id,
        "request_id": session.request.request_id,
        "tool_name": tool_name,
        "requested_start_at": (
            None if requested_start_at is None else requested_start_at.isoformat()
        ),
        "requested_end_at": (
            None if requested_end_at is None else requested_end_at.isoformat()
        ),
        "returned_record_ids": list(returned_record_ids),
    }
    digest = sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return f"pm-review-read:{digest}"


def _write_pm_episode_memory_projection(
    *,
    session: PMReviewToolSession,
    view: PMEpisodeMemoryView,
) -> Path:
    path = (
        session.layout.runtime_root
        / "pm_review"
        / session.request.target_key
        / _safe_request_id_key(request_id=session.request.request_id)
        / "episode_memory_view.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = view.to_json_payload()
    payload["projection_hash"] = view.projection_hash
    with path.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return path


def _safe_request_id_key(*, request_id: str) -> str:
    normalized = request_id.strip()
    if not normalized:
        raise PMReviewToolError(
            "request_id cannot be blank when deriving safe request key."
        )
    return f"request_{sha256(normalized.encode('utf-8')).hexdigest()}"


def _validate_tuple(values: object, expected_type: type, field_name: str) -> None:
    if not isinstance(values, tuple):
        raise PMReviewToolError(f"{field_name} must be a tuple.")
    if not all(isinstance(value, expected_type) for value in values):
        raise PMReviewToolError(f"{field_name} contains invalid record type.")


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


def _active_exposure_source_ids(exposure: ActiveExposure) -> tuple[str, ...]:
    values: list[str] = []
    for value in exposure.source_ids.values():
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    if values:
        return tuple(dict.fromkeys(values))
    return (f"active-exposure:{exposure.target_key}:{exposure.source}",)


__all__ = [
    "PMReviewHistory",
    "PMReviewToolError",
    "PMReviewToolResult",
    "PMReviewToolSession",
    "read_pm_review_active_exposure",
    "read_pm_review_evidence",
    "read_pm_review_history",
    "read_pm_review_learning_cards",
    "read_pm_review_market_bars",
    "read_pm_review_request",
    "read_pm_episode_memory",
]
