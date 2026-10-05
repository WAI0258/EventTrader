"""Deterministic PM-owned position review trigger helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal, cast

from event_trader.contracts.pm_review_reason import PMReviewReason
from event_trader.contracts.price_level_role import MaterialLevelRole, PriceLevelRole
from event_trader.contracts.view_state_change import MarketDataBar, ViewState
from event_trader.pm_review.contracts import (
    PMPositionReviewTriggers,
    PMReviewContractError,
    PMReviewInput,
    PMReviewRequest,
)
from event_trader.portfolio.contracts import PMDecision

PMPositionReviewTriggerKind = Literal["review", "invalidation"]

_REVIEW_TRIGGER_LEVEL_ROLES = frozenset(
    {
        "entry",
        "add",
        "hold_boundary",
        "de_risk_or_take_profit",
        "exit",
        "reverse_or_cover",
    }
)


class PMPositionReviewError(ValueError):
    """Raised when deterministic PM position review trigger handling fails."""


@dataclass(frozen=True, slots=True)
class PMPositionReviewTriggerHit:
    """One deterministic trigger hit for an open position."""

    trigger_kind: PMPositionReviewTriggerKind
    target_key: str
    business_at: datetime
    touched_level_ids: tuple[str, ...]
    touched_market_bar_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.trigger_kind not in {"review", "invalidation"}:
            raise PMPositionReviewError("trigger_kind is not supported.")
        if not isinstance(self.target_key, str) or not self.target_key.strip():
            raise PMPositionReviewError("target_key must be a non-blank string.")
        if not isinstance(self.business_at, datetime) or self.business_at.tzinfo is None:
            raise PMPositionReviewError("business_at must be a timezone-aware datetime.")
        if not isinstance(self.touched_level_ids, tuple) or not self.touched_level_ids:
            raise PMPositionReviewError("touched_level_ids must be a non-empty tuple.")
        if not isinstance(self.touched_market_bar_ids, tuple) or not self.touched_market_bar_ids:
            raise PMPositionReviewError(
                "touched_market_bar_ids must be a non-empty tuple."
            )


def build_pm_position_review_triggers(
    *,
    pm_review_input: PMReviewInput,
    requested_state: ViewState,
) -> PMPositionReviewTriggers | None:
    """Derive persisted PM-owned re-review triggers from deterministic input only."""

    if not isinstance(pm_review_input, PMReviewInput):
        raise PMPositionReviewError("pm_review_input must be a PMReviewInput instance.")
    if requested_state == "flat":
        return None
    review_level_ids: list[str] = []
    invalidation_level_ids: list[str] = []
    trigger_reason_codes: list[str] = []
    path_context_required = False
    market_confirmation_required = False
    snapshot = pm_review_input.analysis_snapshot
    if snapshot is not None:
        for level in snapshot.price_level_roles:
            level_role = _position_side_role(level=level, requested_state=requested_state)
            if level_role == "invalidates":
                invalidation_level_ids.append(level.level_id)
            elif level_role in _REVIEW_TRIGGER_LEVEL_ROLES:
                review_level_ids.append(level.level_id)
            else:
                continue
            path_context_required = path_context_required or level.path_context_required
            market_confirmation_required = True
            trigger_reason_codes.append(f"level_role_{level_role}")
        if review_level_ids:
            trigger_reason_codes.append("review_levels_present")
        if invalidation_level_ids:
            trigger_reason_codes.append("invalidation_levels_present")
    if path_context_required:
        trigger_reason_codes.append("path_context_required")
    if market_confirmation_required:
        trigger_reason_codes.append("market_confirmation_required")
    if not review_level_ids and not invalidation_level_ids:
        return None
    try:
        return PMPositionReviewTriggers(
            pm_review_request_id=pm_review_input.pm_review_request_id,
            target_key=pm_review_input.target_key,
            business_at=pm_review_input.business_at,
            requested_state=requested_state,
            review_trigger_level_ids=_stable_unique_tuple(review_level_ids),
            invalidation_trigger_level_ids=_stable_unique_tuple(invalidation_level_ids),
            path_context_required=path_context_required,
            market_confirmation_required=market_confirmation_required,
            trigger_reason_codes=_stable_unique_tuple(trigger_reason_codes),
        )
    except PMReviewContractError as exc:
        raise PMPositionReviewError(str(exc)) from exc


def evaluate_pm_position_review_triggers(
    *,
    triggers: PMPositionReviewTriggers,
    price_level_roles: tuple[PriceLevelRole, ...],
    market_bars: tuple[MarketDataBar, ...],
    evaluation_start_at: datetime | None = None,
) -> PMPositionReviewTriggerHit | None:
    """Evaluate persisted PM triggers against deterministic market bars."""

    if not isinstance(triggers, PMPositionReviewTriggers):
        raise PMPositionReviewError("triggers must be a PMPositionReviewTriggers instance.")
    if evaluation_start_at is not None:
        if not isinstance(evaluation_start_at, datetime) or evaluation_start_at.tzinfo is None:
            raise PMPositionReviewError(
                "evaluation_start_at must be a timezone-aware datetime when provided."
            )
        trigger_effective_at = max(triggers.business_at, evaluation_start_at)
    else:
        trigger_effective_at = triggers.business_at
    level_lookup = {level.level_id: level for level in price_level_roles}
    invalidation_hit = _trigger_hit(
        trigger_kind="invalidation",
        target_key=triggers.target_key,
        business_at=trigger_effective_at,
        level_ids=triggers.invalidation_trigger_level_ids,
        level_lookup=level_lookup,
        market_bars=market_bars,
    )
    if invalidation_hit is not None:
        return invalidation_hit
    return _trigger_hit(
        trigger_kind="review",
        target_key=triggers.target_key,
        business_at=trigger_effective_at,
        level_ids=triggers.review_trigger_level_ids,
        level_lookup=level_lookup,
        market_bars=market_bars,
    )


def build_triggered_pm_review_request(
    *,
    prior_request: PMReviewRequest,
    source_decision: PMDecision,
    trigger_hit: PMPositionReviewTriggerHit,
    source_event_ids: tuple[str, ...],
) -> PMReviewRequest:
    """Materialize a trigger-driven PM review request from deterministic runtime truth."""

    if not isinstance(prior_request, PMReviewRequest):
        raise PMPositionReviewError("prior_request must be a PMReviewRequest instance.")
    if not isinstance(source_decision, PMDecision):
        raise PMPositionReviewError("source_decision must be a PMDecision instance.")
    if not isinstance(trigger_hit, PMPositionReviewTriggerHit):
        raise PMPositionReviewError("trigger_hit must be a PMPositionReviewTriggerHit instance.")
    review_reasons = _trigger_review_reasons(trigger_hit.trigger_kind)
    business_at = trigger_hit.business_at
    try:
        return PMReviewRequest(
            request_id=derive_triggered_pm_review_request_id(
                source_decision=source_decision,
                trigger_hit=trigger_hit,
                source_event_ids=source_event_ids,
            ),
            target_key=prior_request.target_key,
            business_at=business_at,
            source="open_position_material_update",
            source_assessment_id=prior_request.source_assessment_id,
            source_episode_id=source_decision.decision_episode_id,
            source_event_ids=_stable_unique_tuple(source_event_ids),
            review_reasons=review_reasons,
            current_exposure_required=True,
            candidate_review_allowed=False,
            max_visible_event_time=business_at,
            max_visible_market_time=business_at,
            required_price_level_ids=trigger_hit.touched_level_ids,
            required_claim_ids=prior_request.required_claim_ids,
            candidate_anchor_id=None,
        )
    except PMReviewContractError as exc:
        raise PMPositionReviewError(str(exc)) from exc


def derive_triggered_pm_review_request_id(
    *,
    source_decision: PMDecision,
    trigger_hit: PMPositionReviewTriggerHit,
    source_event_ids: tuple[str, ...],
) -> str:
    """Build a deterministic PM review request id for one trigger hit."""

    payload = {
        "source_pm_decision_id": source_decision.decision_id,
        "source_decision_episode_id": source_decision.decision_episode_id,
        "target_key": source_decision.target_key,
        "business_at": trigger_hit.business_at.isoformat(),
        "trigger_kind": trigger_hit.trigger_kind,
        "touched_level_ids": list(trigger_hit.touched_level_ids),
        "touched_market_bar_ids": list(trigger_hit.touched_market_bar_ids),
        "source_event_ids": list(source_event_ids),
    }
    digest = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"pm-review-request:position-trigger:{digest[:24]}"


def _trigger_hit(
    *,
    trigger_kind: PMPositionReviewTriggerKind,
    target_key: str,
    business_at: datetime,
    level_ids: tuple[str, ...],
    level_lookup: dict[str, PriceLevelRole],
    market_bars: tuple[MarketDataBar, ...],
) -> PMPositionReviewTriggerHit | None:
    if not level_ids:
        return None
    touched_level_ids: list[str] = []
    touched_bar_ids: list[str] = []
    latest_touched_at: datetime | None = None
    for level_id in level_ids:
        level = level_lookup.get(level_id)
        if level is None:
            continue
        for bar in market_bars:
            if bar.end_at <= business_at:
                continue
            if not _level_touched(level=level, bar=bar):
                continue
            touched_level_ids.append(level_id)
            touched_bar_ids.append(_market_bar_id(bar))
            if latest_touched_at is None or bar.end_at > latest_touched_at:
                latest_touched_at = bar.end_at
            break
    if not touched_level_ids or latest_touched_at is None:
        return None
    return PMPositionReviewTriggerHit(
        trigger_kind=trigger_kind,
        target_key=target_key,
        business_at=latest_touched_at,
        touched_level_ids=_stable_unique_tuple(touched_level_ids),
        touched_market_bar_ids=_stable_unique_tuple(touched_bar_ids),
    )


def _level_touched(*, level: PriceLevelRole, bar: MarketDataBar) -> bool:
    bar_low = min(bar.low_price, bar.high_price)
    bar_high = max(bar.low_price, bar.high_price)
    if level.value is not None:
        return bar_low <= level.value <= bar_high
    if level.lower is None or level.upper is None:
        return False
    return not (bar_high < level.lower or bar_low > level.upper)


def _position_side_role(
    *,
    level: PriceLevelRole,
    requested_state: ViewState,
) -> MaterialLevelRole:
    if requested_state in {"weak_long", "strong_long"}:
        return cast(MaterialLevelRole, level.role_if_already_long)
    if requested_state in {"weak_short", "strong_short"}:
        return cast(MaterialLevelRole, level.role_if_already_short)
    raise PMPositionReviewError("flat requested_state does not produce position triggers.")


def _trigger_review_reasons(
    trigger_kind: PMPositionReviewTriggerKind,
) -> tuple[PMReviewReason, ...]:
    if trigger_kind == "invalidation":
        return (
            "current_exposure_pressure",
            "invalidation_touched",
            "material_level_used",
        )
    return (
        "current_exposure_pressure",
        "watch_trigger_touched",
        "material_level_used",
    )


def _market_bar_id(bar: MarketDataBar) -> str:
    return f"bar:{bar.start_at.isoformat()}:{bar.end_at.isoformat()}"


def _stable_unique_tuple(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


__all__ = [
    "PMPositionReviewError",
    "PMPositionReviewTriggerHit",
    "PMPositionReviewTriggerKind",
    "build_pm_position_review_triggers",
    "build_triggered_pm_review_request",
    "derive_triggered_pm_review_request_id",
    "evaluate_pm_position_review_triggers",
]
